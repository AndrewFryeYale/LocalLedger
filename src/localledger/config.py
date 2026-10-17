"""Runtime settings. Everything comes from env (or `.env` at the repo root); nothing is hardcoded."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(os.environ.get("LL_ROOT") or Path(__file__).resolve().parents[2])
load_dotenv(ROOT / ".env", override=False)


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


@dataclass(frozen=True)
class Settings:
    root: Path
    database_url: str
    raw_dir: Path
    out_dir: Path
    cache_dir: Path
    brand: str
    contact_email: str
    llm_provider: str
    llm_base_url: str
    model_heavy: str
    model_fast: str
    budget_usd: float
    price_in_per_mtok: float
    price_out_per_mtok: float
    pilot_passcode: str
    admin_passcode: str
    secret_key: str
    signal_note_threshold: float
    max_notes_per_run: int
    priority_weight: float
    blackout_days: int
    feature_live_audio: bool
    offline: bool

    @property
    def user_agent(self) -> str:
        return f"{self.brand} research {self.contact_email}"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def prompts_dir(self) -> Path:
        return self.root / "prompts"

    @property
    def theses_dir(self) -> Path:
        return self.root / "theses"

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    @property
    def has_llm_key(self) -> bool:
        return bool(os.environ.get("PORTKEY_API_KEY"))

    @property
    def universe_xlsx(self) -> Path:
        checked = self.data_dir / "Microcap_Local_Gov_Universe_checked.xlsx"
        return checked if checked.exists() else self.data_dir / "Microcap_Local_Gov_Universe.xlsx"


@lru_cache
def get_settings() -> Settings:
    return Settings(
        root=ROOT,
        database_url=_env("DATABASE_URL", f"sqlite:///{ROOT / 'localledger.db'}"),
        raw_dir=_path("LL_RAW_DIR", ROOT / "raw"),
        out_dir=_path("LL_OUT_DIR", ROOT / "out"),
        cache_dir=_path("LL_CACHE_DIR", ROOT / ".cache"),
        brand=_env("BRAND_NAME", "Local Ledger"),
        contact_email=_env("CONTACT_EMAIL", "aceamf25@gmail.com"),
        llm_provider=_env("LLM_PROVIDER", "portkey"),
        llm_base_url=_env("LLM_BASE_URL", "https://api.portkey.ai/v1"),
        model_heavy=_env("LLM_MODEL_HEAVY", "gpt-5.6-luna"),
        model_fast=_env("LLM_MODEL_FAST", "gpt-5.6-luna"),
        budget_usd=float(_env("LLM_BUDGET_USD", "10")),
        price_in_per_mtok=float(_env("LLM_PRICE_IN_PER_MTOK", "3.0")),
        price_out_per_mtok=float(_env("LLM_PRICE_OUT_PER_MTOK", "15.0")),
        pilot_passcode=_env("PILOT_PASSCODE", "ledger-pilot"),
        admin_passcode=_env("ADMIN_PASSCODE", "ledger-admin"),
        secret_key=_env("SECRET_KEY", "dev-only-change-me"),
        signal_note_threshold=float(_env("SIGNAL_NOTE_THRESHOLD", "0.55")),
        max_notes_per_run=int(_env("MAX_NOTES_PER_RUN", "20")),
        priority_weight=float(_env("PRIORITY_WEIGHT", "0.35")),
        blackout_days=int(_env("BLACKOUT_DAYS", "3")),
        feature_live_audio=_env("FEATURE_LIVE_AUDIO", "0") == "1",
        offline=_env("LL_OFFLINE", "0") == "1",
    )


def reset_settings() -> None:
    get_settings.cache_clear()
