"""Personalized outreach drafts for the top 50 leads. Drafts only: nothing here sends email.

Each draft names the fund's actual 13F holdings in the universe and one recent, cited signal on one of them.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from localledger.compliance import lint_impersonal
from localledger.config import get_settings
from localledger.llm.client import LLM
from localledger.llm.mock import register_mock
from localledger.models import Company, Lead, Note, Signal

MAX_WORDS = 90
SENDER = "Andrew"


@dataclass
class OutreachResult:
    drafts: int
    fallbacks: int
    out_dir: Path


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def _period_label(raw: str | None) -> str:
    from datetime import datetime

    try:
        return datetime.strptime(raw or "", "%d-%b-%Y").strftime("%B %-d, %Y")
    except ValueError:
        return raw or "latest"


def body_words(body: str) -> int:
    return len(body.split())


def pick_signal(session: Session, tickers: list[str]) -> Signal | None:
    rows = session.exec(select(Signal).where(Signal.ticker.in_(tickers)).order_by(Signal.rank_score.desc())).all()
    checked = {n.signal_id for n in session.exec(select(Note).where(Note.factcheck_passed == True)).all()}  # noqa: E712
    return next((s for s in rows if s.id in checked), rows[0] if rows else None)


def template_draft(fund: dict, holdings: list[dict], signal: dict, brand: str) -> dict:
    names = [h["ticker"] for h in holdings[:3]]
    held = ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else names[0]
    summary = signal["summary"]
    if len(summary.split()) > 26:
        summary = " ".join(summary.split()[:26]).rstrip(",;") + "..."
    body = (
        f"I run {brand}, independent research on under-covered microcaps built only from public records. "
        f"Your {fund['period']} 13F shows positions in {held}.\n\n"
        f"One recent item: {summary} ({signal['citation_url']})\n\n"
        "Would a free weekly issue, or a four-week fund pilot, be useful? Reply 'no' and I won't follow up."
    )
    return {"subject": f"{names[0]}: public-records signals", "body": body}


_SIGNOFF = re.compile(r"\n\s*(best|regards|thanks|thank you|cheers|sincerely|andrew)\b[\s\S]*$", re.IGNORECASE)


def clean_body(body: str) -> str:
    """Drop any sign-off the model added after the opt-out line (we append our own signature)."""
    body = body.strip()
    cut = body.lower().rfind("won't follow up")
    if cut != -1:
        end = body.find("\n", cut)
        body = body if end == -1 else body[:end]
    return _SIGNOFF.sub("", body).strip()


def valid_draft(draft: dict, tickers: list[str], url: str) -> bool:
    body = str(draft.get("body", ""))
    return (
        bool(draft.get("subject"))
        and body_words(body) <= MAX_WORDS
        and sum(t in body for t in tickers) >= 2
        and url in body
        and not lint_impersonal(body)
    )


def build_outreach(session: Session, llm: LLM, top: int = 50) -> OutreachResult:
    settings = get_settings()
    out_dir = settings.out_dir / "outreach"
    out_dir.mkdir(parents=True, exist_ok=True)
    leads = session.exec(select(Lead).where(Lead.is_mega == False).order_by(Lead.rank)).all()  # noqa: E712
    leads = [lead for lead in leads if len(lead.overlap_tickers.split()) >= 2][:top]
    index_rows, fallbacks = [], 0
    for lead in leads:
        tickers = lead.overlap_tickers.split()
        holdings = [
            {"ticker": t, "name": (session.get(Company, t).name if session.get(Company, t) else t)} for t in tickers[:6]
        ]
        signal = pick_signal(session, tickers[:6])
        if signal is None:
            continue
        holdings.sort(key=lambda h: h["ticker"] != signal.ticker)
        period = _period_label(lead.report_period)
        fund = {"fund_name": lead.fund_name.strip(), "city": lead.city, "state": lead.state, "period": period}
        sig = {
            "ticker": signal.ticker,
            "as_of": str(signal.as_of),
            "summary": signal.summary,
            "citation_url": signal.citation_url,
        }
        res = llm.json(
            "outreach_email",
            {
                "brand": settings.brand,
                "sender": SENDER,
                "period": period,
                "fund": fund,
                "holdings": holdings,
                "signal": sig,
            },
        )
        draft = dict(res.data)
        draft["body"] = clean_body(str(draft.get("body", "")))
        if signal.ticker not in draft["body"] or not valid_draft(
            draft, [h["ticker"] for h in holdings], signal.citation_url
        ):
            draft = template_draft(fund, holdings, sig, settings.brand)
            fallbacks += 1
        path = out_dir / f"{lead.rank:03d}-{_slug(lead.fund_name)}.md"
        path.write_text(
            f"<!-- DRAFT. Not sent. Review before sending from your own email client. -->\n"
            f"To: [find contact: 13F signatory / IR page for {fund['fund_name']}]\n"
            f"Subject: {draft['subject']}\n\n{draft['body'].strip()}\n\n{SENDER}\n{settings.brand}\n"
            f"{settings.contact_email}\n\n---\n"
            f"Fund: {fund['fund_name']} ({lead.city}, {lead.state}) · CIK {lead.cik} · lead rank {lead.rank}\n"
            f"13F holdings in universe (period {period}): {' '.join(tickers)}\n"
            f"Signal used: {signal.summary} Source: {signal.citation_url}\n"
            f"Words in body: {body_words(draft['body'])}\n"
        )
        index_rows.append(
            {
                "rank": lead.rank,
                "fund": fund["fund_name"],
                "cik": lead.cik,
                "file": path.name,
                "subject": draft["subject"],
                "words": body_words(draft["body"]),
                "signal_ticker": signal.ticker,
            }
        )
    with (out_dir / "index.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["rank", "fund", "cik", "file", "subject", "words", "signal_ticker"])
        w.writeheader()
        w.writerows(index_rows)
    return OutreachResult(drafts=len(index_rows), fallbacks=fallbacks, out_dir=out_dir)


@register_mock("outreach_email")
def _mock_outreach(variables: dict) -> dict:
    return template_draft(variables["fund"], variables["holdings"], variables["signal"], variables["brand"])
