"""Résumé tailoring: the LLM compares a job posting to the résumé and reports what to surface.

Given the candidate's résumé and one posting, it returns the skills/keywords the posting
wants that are already on the résumé (surface these), the ones that are missing or weak
(gaps to address), and concrete tailoring tips. The résumé is read via the same
``load_profile`` the scorer uses, so both stay in sync with ``settings.resume_path``.
"""

import json
import logging

from pydantic import BaseModel, ValidationError, field_validator

from app.discovery.scorer import load_profile  # shared résumé/profile loader
from app.llm import LLM

__all__ = ["TailorAnalysis", "SkillNote", "analyze", "parse_analysis", "load_profile"]

log = logging.getLogger(__name__)

MAX_DESC_CHARS = 6000  # a pasted posting can be long; cap the prompt spend

SYSTEM_PROMPT = """\
You help one job seeker tailor their résumé to a specific job posting.

You are given the candidate's current résumé and one job posting. Compare them and
report what belongs on the résumé they submit for THIS posting.

Respond with ONLY a JSON object — no prose, no code fences:
{"matched": [{"skill": "<skill or keyword>", "note": "<where it already appears>"}],
 "missing": [{"skill": "<skill or keyword>", "note": "<what the posting asks for>"}],
 "advice": ["<one concrete tailoring suggestion>"]}

- "matched": skills, tools, or qualifications the posting emphasizes that the résumé
  already demonstrates. These should be made prominent so they survive keyword screening.
- "missing": skills, tools, or qualifications the posting asks for that are absent or
  only weakly present. Use the posting's own wording so it can be mirrored. Do NOT invent
  experience — list genuine gaps for the candidate to add only if they truly have them.
- "advice": concrete edits — which projects or bullets to emphasize or reorder, summary
  tweaks, phrasing to mirror. One line each.

Focus on skills and qualifications, not visa or location constraints. Be specific and use
the posting's terminology. Omit a list (leave it empty) rather than padding it.
"""


class SkillNote(BaseModel):
    skill: str
    note: str = ""


class TailorAnalysis(BaseModel):
    matched: list[SkillNote] = []
    missing: list[SkillNote] = []
    advice: list[str] = []

    @field_validator("matched", "missing", mode="before")
    @classmethod
    def _coerce_items(cls, value: object) -> object:
        """Tolerate bare strings where the model should have emitted {skill, note}."""
        if isinstance(value, list):
            return [{"skill": item} if isinstance(item, str) else item for item in value]
        return value


def _json_slice(raw: str) -> str | None:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    return raw[start : end + 1]


def parse_analysis(raw: str) -> TailorAnalysis | None:
    """Extract and validate the JSON object from model output. None if unusable."""
    sliced = _json_slice(raw)
    if sliced is None:
        log.warning("unparseable tailor output: %.200s", raw)
        return None
    try:
        return TailorAnalysis.model_validate(json.loads(sliced))
    except (json.JSONDecodeError, ValidationError):
        log.warning("unparseable tailor output: %.200s", raw)
        return None


def _prompt(resume: str, title: str, company: str, description: str) -> str:
    return (
        f"Résumé:\n{resume}\n\n"
        "Job posting:\n"
        f"Company: {company or 'unknown'}\n"
        f"Title: {title or 'unknown'}\n"
        f"{description[:MAX_DESC_CHARS]}"
    )


async def analyze(
    llm: LLM, resume: str, title: str, company: str, description: str
) -> TailorAnalysis | None:
    raw = await llm.complete(
        SYSTEM_PROMPT,
        _prompt(resume, title, company, description),
        max_tokens=1500,
        schema=TailorAnalysis.model_json_schema(),
    )
    return parse_analysis(raw)
