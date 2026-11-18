"""Settings from the environment (and `.env` in the project root)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDERS = {"", "change-me", "changeme"}


class ConfigError(RuntimeError):
    pass


def _flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_password: str
    secret_key: str
    database_path: Path
    cookie_secure: bool = False
    # step 2: scanning, classification, alerts
    apify_token: str = ""
    anthropic_key: str = ""
    claude_model: str = "claude-haiku-5-5"
    claude_concurrency: int = 15        # most Claude calls in flight at once (also kept under the account's limits)
    discord_webhook: str = ""
    discord_user_id: str = ""            # show alerts mention (ping) this Discord user; "" = no ping
    discord_scan_notices: bool = False   # silent "scan started / finished" messages (on by default in .env)
    apify_max_usd_per_run: float = 1.0
    scan_every_hours: float = 12.0       # scheduled scans; default only, the Scans page's Schedule wins
    instagram_cooldown_hours: float = 0.0  # skip Instagram if read less than this long ago (0 = every scan, like X);
                                           # default only, as above
    retries: int = 2                     # tries again after a timeout / 5xx / 429 / DNS error: 3 s, then 9 s
    apify_concurrency: int = 5           # most Apify runs at once (also kept within the account's limits)
    first_scan_days: int = 120
    browser_fallback: bool = True
    app_url: str = ""

    @property
    def log_dir(self) -> Path:
        """Logs sit next to the database (data/logs for data/concerts.db)."""
        return self.database_path.parent / "logs"

    @property
    def log_file(self) -> Path:
        """watch.log for the real database; watch-<name>.log for any other (data/preview.db sits in data/ too), so
        a test or preview scan never writes into the real log."""
        name = self.database_path.stem
        return self.log_dir / ("watch.log" if name == "concerts" else f"watch-{name}.log")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        db = Path(os.environ.get("DATABASE_PATH") or "data/concerts.db")
        if not db.is_absolute():
            db = ROOT / db
        return cls(
            app_password=os.environ.get("APP_PASSWORD", ""),
            secret_key=os.environ.get("SECRET_KEY", ""),
            database_path=db,
            cookie_secure=_flag("COOKIE_SECURE"),
            apify_token=os.environ.get("APIFY_API_TOKEN", "").strip(),
            anthropic_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
            claude_model=os.environ.get("CLAUDE_MODEL", "").strip() or "claude-haiku-5-5",
            claude_concurrency=min(max(int(_float("CLAUDE_CONCURRENCY", 15)), 1), 32),
            discord_webhook=os.environ.get("DISCORD_WEBHOOK_URL", "").strip(),
            discord_user_id="".join(ch for ch in os.environ.get("DISCORD_USER_ID", "") if ch.isdigit()),
            discord_scan_notices=_flag("DISCORD_SCAN_NOTICES", "true"),
            apify_max_usd_per_run=_float("APIFY_MAX_USD_PER_RUN", 1.0),
            scan_every_hours=min(max(_float("SCAN_EVERY_HOURS", 12.0), 1.0), 168.0),
            instagram_cooldown_hours=max(_float("INSTAGRAM_COOLDOWN_HOURS", 0.0), 0.0),
            retries=min(max(int(_float("RETRIES", 2)), 0), 5),
            apify_concurrency=min(max(int(_float("APIFY_CONCURRENCY", 5)), 1), 32),
            first_scan_days=int(_float("FIRST_SCAN_DAYS", 120)),
            browser_fallback=_flag("BROWSER_FALLBACK", "true"),
            app_url=os.environ.get("APP_URL", "").strip(),
        )

    def check(self) -> None:
        """Refuse to run with a missing or example password/secret."""
        problems = []
        if self.app_password.strip() in PLACEHOLDERS:
            problems.append("APP_PASSWORD is missing or still the example value")
        if self.secret_key.strip() in PLACEHOLDERS or len(self.secret_key) < 32:
            problems.append("SECRET_KEY is missing, the example value, or shorter than 32 characters")
        if problems:
            raise ConfigError("; ".join(problems) + ". Set them in .env (see .env.example).")
