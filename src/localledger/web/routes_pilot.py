"""Gated pilot app: universe, company pages, signal feed."""

from __future__ import annotations

import json
from collections import Counter, defaultdict

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, func, select

from localledger.config import get_settings
from localledger.modeling.store import load_model
from localledger.models import Archetype, Company, Document, ModelImpact, Note, Signal, Source, WarroomSession
from localledger.web.deps import get_session, render, require_pilot

router = APIRouter(prefix="/app")


def compact_impact(impact: ModelImpact) -> dict:
    """Short feed label: the biggest per-quarter revenue move and the per-share sensitivity."""
    data = json.loads(impact.impact_json or "{}")
    best = None
    for q in data.get("quarters", []):
        b, a = q.get("p50_before"), q.get("p50_after")
        if b and a and abs(a / b - 1) >= 0.001 and (best is None or abs(a / b - 1) > abs(best[1])):
            best = (q["label"].replace("Q ending ", "Q/E "), a / b - 1)
    risk = (data.get("risk_after") or 0) - (data.get("risk_before") or 0)
    sens = data.get("sensitivity") or {}
    return {
        "changed": impact.changed,
        "quarter": best[0] if best else None,
        "pct": best[1] if best else None,
        "risk_bp": round(risk * 1e4) if abs(risk) >= 0.0001 else None,
        "per_share": sens.get("per_share") if abs(sens.get("pct") or 0) >= 0.001 else None,
        "summary": impact.summary_app,
    }


def _signal_counts(session: Session) -> tuple[Counter, dict]:
    counts: Counter = Counter()
    latest: dict = {}
    for ticker, n, last in session.exec(
        select(Signal.ticker, func.count(Signal.id), func.max(Signal.as_of)).group_by(Signal.ticker)
    ).all():
        counts[ticker] = n
        latest[ticker] = last
    return counts, latest


@router.get("/universe")
def universe(
    request: Request,
    archetype: str = "",
    coverage: str = "",
    size: str = "",
    min_priority: float = 0,
    q: str = "",
    user: dict = Depends(require_pilot),
    session: Session = Depends(get_session),
):
    stmt = select(Company)
    if archetype:
        stmt = stmt.where(Company.archetype == archetype)
    if coverage:
        stmt = stmt.where(Company.analyst_coverage == coverage)
    if size:
        stmt = stmt.where(Company.size_band == size)
    if min_priority:
        stmt = stmt.where(Company.priority >= min_priority)
    companies = session.exec(stmt.order_by(Company.priority.desc(), Company.sheet_rank)).all()
    if q:
        ql = q.lower()
        companies = [c for c in companies if ql in c.ticker.lower() or ql in c.name.lower()]
    counts, latest = _signal_counts(session)
    ctx = {
        "companies": companies,
        "counts": counts,
        "latest": latest,
        "archetypes": [a.name for a in session.exec(select(Archetype).order_by(Archetype.name)).all()],
        "coverages": sorted({c for c in session.exec(select(Company.analyst_coverage)).all() if c}),
        "sizes": ["Nano (<$25M)", "$25-100M", "$100-250M", "$250-500M"],
        "f": {"archetype": archetype, "coverage": coverage, "size": size, "min_priority": min_priority, "q": q},
    }
    if request.headers.get("HX-Request"):
        return render(request, "pilot/_universe_rows.html", **ctx)
    return render(request, "pilot/universe.html", **ctx)


def _company_sources(ticker: str) -> list[dict]:
    """Which mapped public datasets feed this ticker (from the hand-maintained config files)."""
    cfg = get_settings().config_dir
    out = []
    for name, key, label in (
        ("geo_map.yaml", "geographies", "Census BPS"),
        ("agenda_sources.yaml", "towns", "Agenda (PDF)"),
        ("legistar_clients.yaml", "clients", "Legistar"),
    ):
        path = cfg / name
        if not path.exists():
            continue
        for entry in (yaml.safe_load(path.read_text()) or {}).get(key) or []:
            if ticker in (entry.get("tickers") or []):
                out.append(
                    {
                        "source": label,
                        "name": entry.get("name") or entry.get("client"),
                        "why": entry.get("why") or entry.get("note"),
                    }
                )
    return out


@router.get("/company/{ticker}")
def company_page(
    request: Request, ticker: str, user: dict = Depends(require_pilot), session: Session = Depends(get_session)
):
    company = session.get(Company, ticker.upper())
    if company is None:
        raise HTTPException(404, "unknown ticker")
    signals = session.exec(
        select(Signal).where(Signal.ticker == company.ticker).order_by(Signal.as_of.desc(), Signal.significance.desc())
    ).all()
    notes = session.exec(select(Note).where(Note.ticker == company.ticker).order_by(Note.updated_at.desc())).all()
    docs = session.exec(
        select(Document)
        .where(Document.ticker == company.ticker, Document.source_id == "edgar")
        .order_by(Document.id.desc())
    ).all()
    filings = []
    for d in docs:
        meta = json.loads(d.meta_json or "{}")
        if meta.get("kind") in ("10-k", "10-q", "ex99", "8k"):
            filings.append(
                {
                    "title": d.title,
                    "url": d.url,
                    "kind": meta.get("kind"),
                    "date": meta.get("filing_date"),
                    "index_url": meta.get("index_url"),
                }
            )
    thesis_path = get_settings().theses_dir / f"{company.ticker}.md"
    sources = {s.id: s for s in session.exec(select(Source)).all()}
    by_source = defaultdict(int)
    for s in signals:
        by_source[s.source_id] += 1
    sessions = session.exec(
        select(WarroomSession).where(WarroomSession.ticker == company.ticker).order_by(WarroomSession.id.desc())
    ).all()
    archetype = session.get(Archetype, company.archetype)
    model = load_model(session, company.ticker)
    impacts = {
        i.signal_id: i for i in session.exec(select(ModelImpact).where(ModelImpact.ticker == company.ticker)).all()
    }
    moved = [(s, impacts[s.id]) for s in signals if s.id in impacts and impacts[s.id].changed]
    return render(
        request,
        "pilot/company.html",
        company=company,
        model=model,
        impacts=impacts,
        moved=moved,
        signals=signals,
        notes=notes,
        filings=filings,
        thesis=thesis_path.read_text() if thesis_path.exists() else None,
        sources=sources,
        by_source=dict(by_source),
        mapped=_company_sources(company.ticker),
        archetype=archetype,
        sessions=sessions,
    )


@router.get("/signals")
def signal_feed(
    request: Request,
    source: str = "",
    archetype: str = "",
    min_sig: float = 0.0,
    limit: int = 150,
    user: dict = Depends(require_pilot),
    session: Session = Depends(get_session),
):
    stmt = select(Signal, Company).join(Company, Company.ticker == Signal.ticker)
    if source:
        stmt = stmt.where(Signal.source_id == source)
    if archetype:
        stmt = stmt.where(Company.archetype == archetype)
    if min_sig:
        stmt = stmt.where(Signal.significance >= min_sig)
    rows = session.exec(stmt.order_by(Signal.rank_score.desc()).limit(limit)).all()
    notes = {n.signal_id: n for n in session.exec(select(Note).where(Note.kind == "signal")).all()}
    ids = [sig.id for sig, _ in rows]
    impacts = (
        {
            i.signal_id: compact_impact(i)
            for i in session.exec(select(ModelImpact).where(ModelImpact.signal_id.in_(ids))).all()
        }
        if ids
        else {}
    )
    ctx = {
        "rows": rows,
        "notes": notes,
        "impacts": impacts,
        "sources": session.exec(select(Source).order_by(Source.id)).all(),
        "archetypes": [a.name for a in session.exec(select(Archetype).order_by(Archetype.name)).all()],
        "f": {"source": source, "archetype": archetype, "min_sig": min_sig},
    }
    if request.headers.get("HX-Request"):
        return render(request, "pilot/_signal_rows.html", **ctx)
    return render(request, "pilot/signals.html", **ctx)


@router.get("/note/{note_id}")
def note_page(
    request: Request, note_id: int, user: dict = Depends(require_pilot), session: Session = Depends(get_session)
):
    note = session.get(Note, note_id)
    if note is None:
        raise HTTPException(404)
    signal = session.get(Signal, note.signal_id) if note.signal_id else None
    return render(
        request,
        "pilot/note.html",
        note=note,
        signal=signal,
        dropped=json.loads(note.factcheck_log or "[]"),
        claims=json.loads(note.claims_json or "[]"),
    )
