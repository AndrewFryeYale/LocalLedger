"""Compliance guardrails, enforced in code (not just documented).

- Disclosure footer on every page, issue and PDF (DISCLOSURES).
- Publish block: no note on a ticker the author holds unless its disclosure flag is set.
- Trading blackout reminder for BLACKOUT_DAYS around publication.
- Impersonal-research lint: no recommendation language anywhere we publish.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

from localledger.config import get_settings
from localledger.research.factcheck import RECOMMENDATION

DISCLOSURE_MARKER = "Independent research. No issuer compensation."
DEFAULT_POLICY = "The author may hold positions in securities discussed; holdings are disclosed."


class PublishBlocked(Exception):
    pass


@dataclass
class Holdings:
    policy: str
    blackout_days: int
    tickers: set[str]


def load_holdings(path: Path | None = None) -> Holdings:
    settings = get_settings()
    path = path or settings.config_dir / "holdings.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    data = data or {}
    days = int(data.get("blackout_days", settings.blackout_days))
    policy = str(data.get("policy", "")).format(blackout_days=days).strip()
    tickers = {str(p["ticker"]).upper() for p in data.get("positions") or [] if p.get("ticker")}
    return Holdings(policy=policy, blackout_days=days, tickers=tickers)


def disclosures() -> list[str]:
    settings = get_settings()
    h = load_holdings()
    return [
        f"{DISCLOSURE_MARKER} {settings.brand} research is independent: it is never paid for by any issuer, "
        "and it is built only from public records.",
        "Impersonal research, not investment advice. Nothing here is a recommendation to buy, sell or hold any "
        "security, and nothing is tailored to any reader's circumstances.",
        f"Holdings policy: {h.policy or DEFAULT_POLICY}",
        "Data may be wrong. Public records are revised, mis-keyed and mapped to companies by our own judgment; "
        "every claim links to its source so you can check it.",
    ]


def check_publish(tickers: list[str], disclosure_flag: bool = False, holdings: Holdings | None = None) -> None:
    """Raise PublishBlocked if any ticker is held by the author and the note isn't flagged as disclosed."""
    holdings = holdings or load_holdings()
    held = sorted({t.upper() for t in tickers if t} & holdings.tickers)
    if held and not disclosure_flag:
        raise PublishBlocked(
            f"author holds {', '.join(held)}; publishing is blocked unless the note carries a holdings disclosure "
            "(set the disclosure flag)"
        )


def blackout_reminders(
    tickers: list[str], publish_date: date | None = None, holdings: Holdings | None = None
) -> list[str]:
    holdings = holdings or load_holdings()
    d = publish_date or date.today()
    start, end = d - timedelta(days=holdings.blackout_days), d + timedelta(days=holdings.blackout_days)
    return [
        f"Trading blackout: no personal trades in {t} from {start} to {end} (publication {d})."
        for t in sorted({t.upper() for t in tickers if t})
    ]


def lint_impersonal(text: str) -> list[str]:
    """Recommendation-style phrases found in text (empty list = clean)."""
    return sorted({m.group(0).lower() for m in RECOMMENDATION.finditer(re.sub(r"\[[^\]]*\]\([^)]*\)", "", text))})
