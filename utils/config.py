"""Central configuration for dibbs-bot.

All runtime settings are read from environment variables (optionally loaded from
a local `.env` file via python-dotenv). Keeping config in one place means the
rest of the code never has to know where a path or URL came from.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Project root is two levels up from this file: <root>/utils/config.py -> <root>
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# Load .env (if present) before reading any env vars. override=False so real
# shell env vars still win, which is the standard 12-factor behavior.
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _resolve(p: str | os.PathLike[str]) -> Path:
    """Return an absolute Path, treating relative inputs as relative to repo root."""
    path = Path(p)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


@dataclass(frozen=True)
class Settings:
    """Immutable settings object passed around the app."""

    project_root: Path
    db_path: Path
    inbox_dir: Path
    log_file: Path
    log_level: str
    dibbs_base_url: str
    scrape_delay_seconds: float
    user_agent: str
    sam_gov_api_key: str  # blank when unused


def get_settings() -> Settings:
    """Build a Settings object from current environment variables.

    This is a function (not a module-level constant) so tests and notebooks
    can mutate the env and re-read config without reimporting.
    """
    db_path = _resolve(os.getenv("DIBBS_DB_PATH", "data/dibbs.db"))
    inbox_dir = _resolve(os.getenv("DIBBS_INBOX_DIR", "scraper/sample_data"))
    log_file = _resolve(os.getenv("DIBBS_LOG_FILE", "logs/dibbs-bot.log"))

    # Make sure the directories the app writes to actually exist.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        project_root=PROJECT_ROOT,
        db_path=db_path,
        inbox_dir=inbox_dir,
        log_file=log_file,
        log_level=os.getenv("DIBBS_LOG_LEVEL", "INFO").upper(),
        dibbs_base_url=os.getenv(
            "DIBBS_BASE_URL", "https://www.dibbs.bsm.dla.mil"
        ).rstrip("/"),
        scrape_delay_seconds=float(os.getenv("DIBBS_SCRAPE_DELAY", "2.0")),
        user_agent=os.getenv(
            "DIBBS_USER_AGENT", "dibbs-bot/0.1 (local research tool)"
        ),
        sam_gov_api_key=os.getenv("SAM_GOV_API_KEY", ""),
    )


# A convenient module-level singleton for code that doesn't need to re-read env.
SETTINGS: Settings = get_settings()
