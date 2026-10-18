"""Build every company's model, value it against peers, then replay its signals to get each signal's impact.

Base models are deterministic (filings + curated overlays in models/{TICKER}.yaml, maintained by Claude Code over
time). The Portkey LLM is only used to *update* a model for news no deterministic rule covers, within a per-run cap.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date

import yaml
from sqlmodel import Session, select

from localledger.config import get_settings
from localledger.http import HttpClient, read_document_text
from localledger.modeling.alt_sources import call_report_model, is_trust, trust_history
from localledger.modeling.engine import CompanyModel, apply_valuation, build_base
from localledger.modeling.history import load_history
from localledger.modeling.impacts import LLM_METRICS, compute_impact, diff, llm_adjustments, rule, summarize
from localledger.models import Company, CompanyModelRow, Document, ModelImpact, Signal, utcnow
from localledger.research.retrieval import signal_excerpt

log = logging.getLogger(__name__)
TEXT_METRICS = {
    "8k_item_3.01",
    "8k_item_4.02",
    "8k_item_1.03",
    "form_nt_10-k",
    "form_nt_10-q",
    "8k_item_4.01",
    "agenda_match",
}


@dataclass
class BuildResult:
    models: int = 0
    modeled: int = 0
    no_xbrl: int = 0
    pre_revenue: int = 0
    impacts: int = 0
    changed: int = 0
    llm_updates: int = 0
    errors: list[str] = field(default_factory=list)


def load_overlay(ticker: str) -> dict:
    path = get_settings().models_dir / f"{ticker}.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _save(session: Session, ticker: str, kind: str, m: CompanyModel) -> None:
    row = session.exec(
        select(CompanyModelRow).where(CompanyModelRow.ticker == ticker, CompanyModelRow.kind == kind)
    ).first() or CompanyModelRow(ticker=ticker, kind=kind, model_json="{}")
    row.model_json, row.status, row.built_at = json.dumps(m.to_dict(), default=str), m.status, utcnow()
    session.add(row)


def load_model(session: Session, ticker: str, kind: str = "current") -> CompanyModel | None:
    row = session.exec(
        select(CompanyModelRow).where(CompanyModelRow.ticker == ticker, CompanyModelRow.kind == kind)
    ).first()
    return CompanyModel.from_dict(json.loads(row.model_json)) if row else None


def _llm_allowed(session: Session, limit: int) -> set[int]:
    rows = session.exec(
        select(Signal)
        .where(Signal.metric.in_(LLM_METRICS), Signal.significance >= 0.5)
        .order_by(Signal.rank_score.desc())
    ).all()
    return {s.id for s in rows[:limit]}


def build_models(
    session: Session,
    tickers: list[str] | None = None,
    fetch: bool = True,
    llm=None,
    max_llm: int | None = None,
    today: date | None = None,
) -> BuildResult:
    settings = get_settings()
    today = today or date.today()
    max_llm = max_llm if max_llm is not None else int(os.environ.get("MAX_MODEL_UPDATES_PER_RUN", "15"))
    http = HttpClient() if fetch else None
    result = BuildResult()
    companies = session.exec(select(Company).order_by(Company.sheet_rank)).all()
    overlays = {c.ticker: load_overlay(c.ticker) for c in companies}
    bases: dict[str, CompanyModel] = {}
    for c in companies:  # every company gets a base model; peers need the whole universe
        try:
            fetcher = http if (not tickers or c.ticker in tickers) else None
            history = None if is_trust(c) else load_history(session, c, fetcher)
            if history is None or not history.series("revenue"):
                history = trust_history(session, c, fetcher, today) if is_trust(c) else history
            if history is None and c.archetype == "Community bank" and not c.cik:
                bases[c.ticker] = call_report_model(session, c, today) or build_base(c, None, overlays[c.ticker], today)
                continue
            bases[c.ticker] = build_base(c, history, overlays[c.ticker], today)
        except Exception as exc:  # one bad filing never stops the build
            log.exception("model build failed for %s", c.ticker)
            result.errors.append(f"{c.ticker}: {type(exc).__name__}: {exc}"[:200])
            bases[c.ticker] = build_base(c, None, overlays[c.ticker], today)
        session.commit()
    stats = apply_valuation(list(bases.values()), overlays)
    for t, m in bases.items():
        _save(session, t, "base", m)
        result.models += 1
        result.modeled += m.status == "modeled"
        result.no_xbrl += m.status in ("no_xbrl", "call_report_only")
        result.pre_revenue += m.status == "pre_revenue"
    session.commit()

    allowed = _llm_allowed(session, max_llm) if llm is not None else set()
    targets = [c for c in companies if not tickers or c.ticker in tickers]
    for c in targets:
        current = bases[c.ticker]
        signals = session.exec(select(Signal).where(Signal.ticker == c.ticker).order_by(Signal.as_of, Signal.id)).all()
        for s in signals:
            detail = json.loads(s.detail_json or "{}")
            doc = session.get(Document, s.document_id)
            text = read_document_text(doc)[:30000] if s.metric in TEXT_METRICS and doc else ""
            adjustments, method, explanation = rule(current, s, detail, text)
            if method == "llm":
                if s.id in allowed and current.status != "no_xbrl":
                    session.commit()  # never hold the write lock across an LLM call (the cost ledger must write)
                    excerpt = signal_excerpt(session, s, doc)
                    adjustments, explanation = llm_adjustments(
                        llm, current, s, read_document_text(doc), excerpt, s.citation_url, settings.brand
                    )
                    method = "llm"
                    result.llm_updates += 1
                else:
                    method, explanation = (
                        "note",
                        explanation.replace("eligible for the LLM model updater", "not quantified this run"),
                    )
            if adjustments:
                current, impact = compute_impact(current, adjustments, stats, overlays[c.ticker], explanation, method)
            else:
                impact = diff(current, current)
                impact.update(method=method, explanation=explanation, adjustments=[])
                impact["summary_public"] = impact["summary_app"] = summarize(impact, explanation, public=True)
            changed = bool(adjustments) and not impact["summary_public"].startswith("No change")
            row = session.exec(select(ModelImpact).where(ModelImpact.signal_id == s.id)).first() or ModelImpact(
                signal_id=s.id, ticker=c.ticker, method=method, impact_json="{}", summary_public="", summary_app=""
            )
            row.method, row.impact_json = method, json.dumps(impact, default=str)
            row.summary_public, row.summary_app, row.changed = impact["summary_public"], impact["summary_app"], changed
            row.created_at = utcnow()
            session.add(row)
            result.impacts += 1
            result.changed += changed
        _save(session, c.ticker, "current", current)
        session.commit()
    return result
