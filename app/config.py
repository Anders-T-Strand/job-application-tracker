"""Configuration from environment variables, with a tiny .env loader (no python-dotenv dep)."""

import os
from pathlib import Path

from pydantic import BaseModel


class Settings(BaseModel):
    db_path: Path = Path("data/tracker.db")
    secrets_dir: Path = Path("secrets")
    ms_client_id: str = ""
    ms_authority: str = "https://login.microsoftonline.com/consumers"
    llm_provider: str = "ollama"  # "ollama" (local model) or "anthropic"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3.8:27b"
    ollama_num_ctx: int = 16384
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"
    healthcheck_url: str = ""
    poll_enabled: bool = True
    poll_interval_seconds: int = 90
    backfill_days: int = 90
    discovery_enabled: bool = True
    discovery_interval_hours: int = 24
    feeds_enabled: bool = True  # also pull the Zapply/Simplify job lists (app/discovery/feeds.py)
    resume_path: Path = Path("data/resume.txt")
    # Candidate specifics, kept out of the code: free-text constraints for the scorer,
    # a spring graduation year (term buckets on Discover), and places reachable while
    # still in school (with the year, enables the school-term locality rule).
    profile_path: Path = Path("data/profile.md")
    graduation_year: int | None = None
    local_area: str = ""


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_settings(dotenv: Path | None = Path(".env")) -> Settings:
    if dotenv is not None:
        _load_dotenv(dotenv)
    env = os.environ
    return Settings(
        db_path=Path(env.get("DB_PATH", "data/tracker.db")),
        secrets_dir=Path(env.get("SECRETS_DIR", "secrets")),
        ms_client_id=env.get("MS_CLIENT_ID", ""),
        ms_authority=env.get("MS_AUTHORITY", "https://login.microsoftonline.com/consumers"),
        llm_provider=env.get("LLM_PROVIDER", "ollama").lower(),
        ollama_url=env.get("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=env.get("OLLAMA_MODEL", "qwen3.8:27b"),
        ollama_num_ctx=int(env.get("OLLAMA_NUM_CTX", "16384")),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
        anthropic_model=env.get("ANTHROPIC_MODEL", "claude-haiku-4-5"),
        healthcheck_url=env.get("HEALTHCHECK_URL", ""),
        poll_enabled=env.get("POLL_ENABLED", "true").lower() in ("1", "true", "yes"),
        poll_interval_seconds=int(env.get("POLL_INTERVAL_SECONDS", "90")),
        backfill_days=int(env.get("BACKFILL_DAYS", "90")),
        discovery_enabled=env.get("DISCOVERY_ENABLED", "true").lower() in ("1", "true", "yes"),
        discovery_interval_hours=int(env.get("DISCOVERY_INTERVAL_HOURS", "24")),
        feeds_enabled=env.get("FEEDS_ENABLED", "true").lower() in ("1", "true", "yes"),
        resume_path=Path(env.get("RESUME_PATH", "data/resume.txt")),
        profile_path=Path(env.get("PROFILE_PATH", "data/profile.md")),
        graduation_year=int(env["GRADUATION_YEAR"]) if env.get("GRADUATION_YEAR") else None,
        local_area=env.get("LOCAL_AREA", ""),
    )
