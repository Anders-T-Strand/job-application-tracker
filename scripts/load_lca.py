"""Load DOL LCA disclosure data into companies.lca_filings (H-1B sponsorship signal).

Run: python scripts/load_lca.py path/to/LCA_Disclosure_Data_FY2024.csv --year 2024

Download the quarterly disclosure files from the DOL performance data page
(https://www.dol.gov/agencies/eta/foreign-labor/performance) and convert to CSV
if they ship as .xlsx (e.g. `in2csv` or Excel's save-as). Needs EMPLOYER_NAME
and CASE_STATUS columns. Counts certified filings per employer, fuzzy-matches
employer names against the companies table, and writes the summed counts.
Repeated runs (e.g. one per quarter of the same year) add up; pass a new --year
to reset counts to that year's data.
"""

import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path

from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.config import load_settings  # noqa: E402

SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|corp|corporation|ltd|limited|co|company|lp|llp|plc|"
    r"holdings|group|technologies|labs)\b\.?",
    re.I,
)
MIN_SCORE = 92


def normalize(name: str) -> str:
    name = SUFFIXES.sub(" ", name.lower())
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def count_certified(csv_path: Path) -> Counter:
    """Certified LCA filings per normalized employer name."""
    counts: Counter = Counter()
    with csv_path.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not {"EMPLOYER_NAME", "CASE_STATUS"} <= {
            field.strip().upper() for field in reader.fieldnames
        }:
            sys.exit(f"{csv_path} lacks EMPLOYER_NAME/CASE_STATUS columns: {reader.fieldnames}")
        fields = {field.strip().upper(): field for field in reader.fieldnames}
        for row in reader:
            if not (row[fields["CASE_STATUS"]] or "").lower().startswith("certified"):
                continue
            employer = normalize(row[fields["EMPLOYER_NAME"]] or "")
            if employer:
                counts[employer] += 1
    return counts


def filings_for(company_name: str, counts: Counter, min_score: int) -> int:
    """Sum counts across employer entities that fuzzy-match the company name."""
    target = normalize(company_name)
    if not target:
        return 0
    return sum(
        count
        for employer, count in counts.items()
        if fuzz.token_set_ratio(target, employer) >= min_score
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, help="DOL LCA disclosure CSV")
    parser.add_argument("--year", type=int, required=True, help="fiscal year of the file")
    parser.add_argument("--min-score", type=int, default=MIN_SCORE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    counts = count_certified(args.csv_path)
    print(f"{sum(counts.values())} certified filings across {len(counts)} employers")

    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    matched = 0
    for company in conn.execute("SELECT id, name, lca_filings, lca_year FROM companies"):
        filings = filings_for(company["name"], counts, args.min_score)
        if not filings:
            continue
        # same year accumulates (quarterly files); a new year starts over
        base = company["lca_filings"] if company["lca_year"] == args.year else 0
        matched += 1
        if args.dry_run:
            print(f"{company['name']}: {base} -> {base + filings}")
            continue
        conn.execute(
            "UPDATE companies SET lca_filings = ?, lca_year = ? WHERE id = ?",
            (base + filings, args.year, company["id"]),
        )
    conn.commit()
    conn.close()
    print(f"{'(dry run) ' if args.dry_run else ''}matched {matched} companies")


if __name__ == "__main__":
    main()
