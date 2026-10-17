"""The only way signals get written. A signal that can't be traced to a stored document doesn't exist."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from sqlmodel import Session, select

from localledger.models import Company, Document, Signal, as_date, utcnow
from localledger.signals.scoring import rank_score


class CitationError(ValueError):
    """Raised when a signal or claim lacks a citation URL or a stored source document."""


@dataclass
class SignalDraft:
    ticker: str
    metric: str
    as_of: date
    summary: str
    citation_url: str | None
    document_id: int | None
    value: float | None = None
    prior_value: float | None = None
    unit: str | None = None
    period: str | None = None
    significance: float = 0.0
    detail: dict = field(default_factory=dict)


def make_signal(session: Session, source_id: str, draft: SignalDraft) -> Signal:
    if not draft.citation_url or not str(draft.citation_url).startswith(("http://", "https://")):
        raise CitationError(f"signal {draft.ticker}/{draft.metric} has no citation URL")
    if not draft.document_id or session.get(Document, draft.document_id) is None:
        raise CitationError(f"signal {draft.ticker}/{draft.metric} has no stored source document")
    company = session.get(Company, draft.ticker)
    if company is None:
        raise ValueError(f"unknown ticker {draft.ticker}")
    delta = None
    if draft.value is not None and draft.prior_value is not None:
        delta = draft.value - draft.prior_value
    as_of = as_date(draft.as_of)
    significance = max(0.0, min(1.0, float(draft.significance)))
    key = f"{source_id}:{draft.ticker}:{draft.metric}:{draft.period or as_of.isoformat()}"
    signal = session.exec(select(Signal).where(Signal.dedupe_key == key)).first()
    if signal is None:
        signal = Signal(
            dedupe_key=key,
            ticker=draft.ticker,
            source_id=source_id,
            as_of=as_of,
            metric=draft.metric,
            summary=draft.summary,
            citation_url=draft.citation_url,
            document_id=draft.document_id,
        )
    signal.as_of = as_of
    signal.period = draft.period
    signal.unit = draft.unit
    signal.value = draft.value
    signal.prior_value = draft.prior_value
    signal.delta = delta
    signal.significance = significance
    signal.summary = draft.summary
    signal.citation_url = draft.citation_url
    signal.document_id = draft.document_id
    signal.detail_json = json.dumps(draft.detail, default=str) if draft.detail else None
    signal.rank_score = rank_score(significance, company.priority, as_of)
    signal.created_at = signal.created_at or utcnow()
    session.add(signal)
    session.flush()
    return signal
