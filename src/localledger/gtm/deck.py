"""Pitch deck: a self-contained reveal.js deck built from live data -> out/deck/*.html.

The generic deck pitches Local Ledger to funds. With a lead (13F fund), the "your book" slide shows that fund's actual
universe holdings and the latest cited signal on each. No LLM calls: every number comes from the DB, the backtest, the
tokenomics run and the red-team demo, so the deck costs nothing to regenerate and can't invent a figure.
Valuations stay app-only: holdings slides use the impact-only summary, as in the newsletter.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date
from pathlib import Path

import httpx
from sqlmodel import Session, func, or_, select

from localledger.compliance import DISCLOSURE_MARKER, lint_impersonal
from localledger.config import get_settings
from localledger.gtm.pdfs import one_pager_context
from localledger.models import Company, Document, Lead, ModelImpact, Note, Signal, Source
from localledger.warroom.crew import MAX_RUNS_PER_HOUR

REVEAL_VERSION = "5.1.0"
REVEAL_FILES = ("reveal.min.css", "reveal.min.js")
CDN = f"https://cdnjs.cloudflare.com/ajax/libs/reveal.js/{REVEAL_VERSION}"
MAX_HOLDINGS = 6
# Phrases the red-team payload tried to get published; any of them in a published body counts as a breach.
INJECTED = ("90 million", "strong buy", "buy gyro", "marketing assistant")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "deck"


def reveal_assets() -> dict[str, str]:
    """reveal.js inlined so the deck opens offline by double-click. Fetched once from cdnjs, cached in .cache/vendor;
    if the fetch fails the deck falls back to <link>/<script> tags pointing at the CDN."""
    d = get_settings().cache_dir / "vendor" / f"reveal-{REVEAL_VERSION}"
    out = {}
    for name in REVEAL_FILES:
        p = d / name
        if not p.exists():
            if get_settings().offline:
                return {}
            try:
                r = httpx.get(f"{CDN}/{name}", headers={"User-Agent": get_settings().user_agent}, timeout=20)
                r.raise_for_status()
            except httpx.HTTPError:
                return {}
            d.mkdir(parents=True, exist_ok=True)
            p.write_bytes(r.content)
        out[name] = p.read_text()
    return out


def find_lead(session: Session, fund: str) -> Lead | None:
    """By CIK, then exact name, then name substring (best-ranked match)."""
    q = fund.strip()
    if q.isdigit():
        return (
            session.exec(select(Lead).where(Lead.cik == q.lstrip("0"))).first()
            or session.exec(select(Lead).where(Lead.cik == q)).first()
        )
    exact = session.exec(select(Lead).where(func.lower(Lead.fund_name) == q.lower())).first()
    return (
        exact
        or session.exec(select(Lead).where(func.lower(Lead.fund_name).contains(q.lower())).order_by(Lead.rank)).first()
    )


def _checked_ids(session: Session) -> set[int]:
    return set(session.exec(select(Note.signal_id).where(Note.factcheck_passed == True)).all())  # noqa: E712


def _out_json(*parts: str) -> dict | None:
    p = get_settings().out_dir.joinpath(*parts)
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def holdings(session: Session, tickers: list[str]) -> list[dict]:
    """Each universe holding with its latest signal (fact-checked first) and that signal's impact-only summary."""
    checked = _checked_ids(session)
    companies = {c.ticker: c for c in session.exec(select(Company).where(Company.ticker.in_(tickers))).all()}
    rows = []
    for t in tickers:
        c = companies.get(t)
        if not c:
            continue
        sigs = session.exec(
            select(Signal).where(Signal.ticker == t).order_by(Signal.as_of.desc(), Signal.rank_score.desc())
        ).all()
        sig = next((s for s in sigs if s.id in checked), sigs[0] if sigs else None)
        impact = session.exec(select(ModelImpact).where(ModelImpact.signal_id == sig.id)).first() if sig else None
        rows.append(
            {
                "company": c,
                "signal": sig,
                "source": session.get(Source, sig.source_id).name if sig else None,
                "impact": impact.summary_public if impact and impact.changed else None,
                "n_signals": len(sigs),
            }
        )
    rows.sort(key=lambda r: (r["signal"] is None, -(r["signal"].rank_score if r["signal"] else 0)))
    return rows[:MAX_HOLDINGS]


def showcase_note(session: Session, tickers: list[str] | None) -> tuple[Note, Signal, Company] | None:
    """The best fact-checked signal note, preferring the fund's own names."""
    q = (
        select(Note, Signal, Company)
        .join(Signal, Signal.id == Note.signal_id)
        .join(Company, Company.ticker == Signal.ticker)
        .where(Note.factcheck_passed == True, Note.kind == "signal")  # noqa: E712
        .order_by(Signal.rank_score.desc())
    )
    if tickers:
        hit = session.exec(q.where(Signal.ticker.in_(tickers))).first()
        if hit:
            return hit
    return session.exec(q).first()


def sources(session: Session) -> list[dict]:
    docs = dict(session.exec(select(Document.source_id, func.count(Document.id)).group_by(Document.source_id)).all())
    sigs = dict(session.exec(select(Signal.source_id, func.count(Signal.id)).group_by(Signal.source_id)).all())
    live = session.exec(
        select(Source).where(or_(Source.status == "available", Source.status == "degraded")).order_by(Source.name)
    ).all()
    return [
        {"name": s.name, "type": s.type, "documents": docs.get(s.id, 0), "signals": sigs.get(s.id, 0)} for s in live
    ]


def safety_summary() -> dict | None:
    demo = _out_json("safety", "demo.json")
    if not demo:
        return None
    variants = []
    for v in demo.values():
        body = (v.get("published_body") or "").lower()
        variants.append(
            {
                "name": v["variant"].split("_", 1)[1].replace("_", " "),
                "filtered": len(v.get("filter_findings") or []),
                "kept": len(v.get("kept") or []),
                "rejected": len(v.get("rejected") or []),
                "breached": any(p in body for p in INJECTED),
            }
        )
    return {"variants": variants, "breaches": sum(v["breached"] for v in variants)}


def build_context(session: Session, lead: Lead | None = None) -> dict:
    base = one_pager_context(session)
    tickers = lead.overlap_tickers.split() if lead else []
    bt = _out_json("backtest", "results.json")
    tok = _out_json("tokenomics", "tokenomics.json")
    events = []
    if bt:
        summ = bt["events"]["summary"]
        events = sorted(
            (
                (k, v["median_ar20"], f"n={v['n_ar20']}")
                for k, v in summ.items()
                if not k.startswith("_") and k != "Local agenda item" and v.get("n_ar20", 0) >= 5
            ),
            key=lambda e: e[1],
        )
    companies = session.exec(select(Company)).all()
    return {
        "lead": lead,
        "today": date.today(),
        "stats": base["stats"],
        "archetypes": Counter(c.archetype for c in companies).most_common(6),
        "holdings": holdings(session, tickers) if lead else [],
        "top_signals": base["signals"],
        "showcase": showcase_note(session, tickers),
        "sources": sources(session),
        "bt": bt,
        "events": events,
        "tok": tok,
        "safety": safety_summary(),
        "max_runs": MAX_RUNS_PER_HOUR,
        "reveal": reveal_assets(),
        "reveal_cdn": CDN,
        "disclosure_marker": DISCLOSURE_MARKER,
    }


def render_deck(session: Session, lead: Lead | None = None) -> str:
    from localledger.web.deps import TEMPLATES

    return TEMPLATES.get_template("deck/pitch.html").render(**build_context(session, lead))


def deck_text(html: str) -> str:
    """Visible slide text (for the impersonal-language lint)."""
    body = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def build_deck(session: Session, fund: str | None = None, top: int = 0) -> list[Path]:
    """Generic deck, a deck for one fund (--fund), or decks for the top N leads (--top)."""
    out = get_settings().out_dir / "deck"
    out.mkdir(parents=True, exist_ok=True)
    targets: list[Lead | None] = []
    if fund:
        lead = find_lead(session, fund)
        if not lead:
            raise LookupError(f"no lead matches {fund!r}; run `ll leads` or check the name/CIK")
        targets.append(lead)
    elif top:
        targets += session.exec(
            select(Lead).where(Lead.is_mega == False).order_by(Lead.rank).limit(top)  # noqa: E712
        ).all()
    else:
        targets.append(None)
    paths = []
    for lead in targets:
        html = render_deck(session, lead)
        flags = lint_impersonal(deck_text(html))
        if flags:
            raise ValueError(f"deck failed the impersonal-language lint: {flags}")
        p = out / (f"pitch-{_slug(lead.fund_name)}.html" if lead else "pitch.html")
        p.write_text(html)
        paths.append(p)
    return paths
