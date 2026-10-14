"""One discovery cycle, for cron or manual runs: python -m app.discovery"""

import asyncio
import logging

from app import db
from app.config import load_settings
from app.discovery.poll import discover_once


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    conn.close()
    asyncio.run(discover_once(settings))


if __name__ == "__main__":
    main()
