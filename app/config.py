"""Environment configuration (secrets and server settings).

Trading rules are NOT here - they live in the database and are edited from the
settings page (see app/settings_store.py). This file only holds things that
should never change from the UI: API keys, paths, the paper-only flag.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Env:
    paper_only: bool
    starting_balance: float

    anthropic_api_key: str
    ai_model: str
    ai_price_in_per_m: float
    ai_price_out_per_m: float
    ai_strong_model: str
    ai_strong_price_in_per_m: float
    ai_strong_price_out_per_m: float

    telegram_bot_token: str
    telegram_chat_id: str

    dashboard_user: str
    dashboard_password: str

    host: str
    port: int
    db_path: Path
    backup_dir: Path
    backup_to_telegram: bool

    @property
    def has_ai(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name) or default)
    return p if p.is_absolute() else ROOT / p


ENV = Env(
    paper_only=_bool("PAPER_ONLY", True),
    starting_balance=_float("STARTING_BALANCE", 250.0),
    anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
    ai_model=os.getenv("AI_MODEL", "claude-haiku-5-5"),
    ai_price_in_per_m=_float("AI_PRICE_IN_PER_M", 0.10),
    ai_price_out_per_m=_float("AI_PRICE_OUT_PER_M", 0.50),
    ai_strong_model=os.getenv("AI_STRONG_MODEL", "claude-sonnet-5-5"),
    ai_strong_price_in_per_m=_float("AI_STRONG_PRICE_IN_PER_M", 2.0),
    ai_strong_price_out_per_m=_float("AI_STRONG_PRICE_OUT_PER_M", 10.0),
    telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    dashboard_user=os.getenv("DASHBOARD_USER", "admin"),
    dashboard_password=os.getenv("DASHBOARD_PASSWORD", ""),
    host=os.getenv("HOST", "127.0.0.1"),
    port=int(os.getenv("PORT", "8000")),
    db_path=_path("DB_PATH", "data/trader.db"),
    backup_dir=_path("BACKUP_DIR", "backups"),
    backup_to_telegram=_bool("BACKUP_TO_TELEGRAM", True),
)
