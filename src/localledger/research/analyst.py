"""Per-signal analyst agent: a fast-model note on what changed and why it matters, then the fact-check gate."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from sqlmodel import Session, select

from localledger.config import get_settings
from localledger.http import read_document_text
from localledger.llm.client import LLM
from localledger.llm.mock import register_mock
from localledger.models import Company, Document, Note, Signal, utcnow
from localledger.research.factcheck import fact_check, validate_claims
from localledger.research.retrieval import filing_passages, signal_excerpt
from localledger.signals.store import CitationError

log = logging.getLogger(__name__)

MIN_CLAIMS_TO_PASS = 2


@dataclass
class ResearchSummary:
    attempted: int = 0
    passed: int = 0
    failed: int = 0
    rejected_claims: int = 0
    errors: list[str] = field(default_factory=list)


def signal_vars(signal: Signal) -> dict:
    detail = json.loads(signal.detail_json or "{}")
    pct = detail.get("pct_change")
    return {
        "metric": signal.metric,
        "as_of": str(signal.as_of),
        "period": signal.period,
        "summary": signal.summary,
        "value": signal.value,
        "prior_value": signal.prior_value,
        "delta": signal.delta,
        "pct": f"{pct:+.1%}" if isinstance(pct, (int, float)) else None,
    }


def computed_fields(signal: Signal) -> list[float]:
    detail = json.loads(signal.detail_json or "{}")
    vals = [signal.value, signal.prior_value, signal.delta, detail.get("pct_change")]
    for k in ("units_1", "prior_units_1", "units_5plus", "prior_units_5plus"):
        vals.append(detail.get(k))
    return [float(v) for v in vals if isinstance(v, (int, float))]


def build_context(session: Session, signal: Signal) -> tuple[list[dict], dict[int, str]]:
    """Documents shown to the analyst (verbatim excerpts) and the fact-check corpus per document id."""
    company = session.get(Company, signal.ticker)
    doc = session.get(Document, signal.document_id)
    excerpt = signal_excerpt(session, signal, doc)
    docs = [{"document_id": doc.id, "url": signal.citation_url, "title": doc.title or doc.url, "text": excerpt}]
    corpora = {doc.id: read_document_text(doc) + "\n" + excerpt}
    detail = json.loads(signal.detail_json or "{}")
    if detail.get("prior_document_id") and detail["prior_document_id"] != doc.id:
        prior = session.get(Document, detail["prior_document_id"])
        if prior is not None:
            prior_excerpt = excerpt
            if detail.get("prior_accn"):
                fake = Signal(
                    **{
                        **signal.model_dump(),
                        "document_id": prior.id,
                        "detail_json": json.dumps({"accn": detail["prior_accn"]}),
                    }
                )
                prior_excerpt = signal_excerpt(session, fake, prior)
            docs.append(
                {
                    "document_id": prior.id,
                    "url": detail.get("prior_url") or prior.url,
                    "title": prior.title or prior.url,
                    "text": prior_excerpt,
                }
            )
            corpora[prior.id] = read_document_text(prior) + "\n" + prior_excerpt
    query = " ".join(
        filter(
            None,
            [
                signal.metric.replace("_", " "),
                signal.summary,
                company.local_gov_driver,
                company.note,
                detail.get("why"),
                detail.get("geo_name"),
            ],
        )
    )
    for p in filing_passages(session, signal.ticker, query, k=3):
        docs.append(p)
        corpora.setdefault(p["document_id"], read_document_text(session.get(Document, p["document_id"])))
    return docs, corpora


def render_note_md(claims: list[dict], docs: list[dict]) -> str:
    order: dict[int, int] = {}
    sentences = []
    for c in claims:
        doc_id = c["citation"]["document_id"]
        n = order.setdefault(doc_id, len(order) + 1)
        sentences.append(f"{c['text'].rstrip()} [{n}]")
    titles = {d["document_id"]: (d["title"], d["url"]) for d in docs}
    sources = [
        f"[{n}] [{titles.get(doc_id, ('source', ''))[0]}]({titles.get(doc_id, ('', ''))[1]})"
        for doc_id, n in order.items()
    ]
    return " ".join(sentences) + "\n\nSources: " + " · ".join(sources)


def write_signal_note(session: Session, signal: Signal, llm: LLM) -> Note | None:
    settings = get_settings()
    company = session.get(Company, signal.ticker)
    docs, corpora = build_context(session, signal)
    variables = {
        "brand": settings.brand,
        "company": company.model_dump(),
        "signal": signal_vars(signal),
        "documents": docs,
    }
    res = llm.json("analyst_signal_note", variables)
    claims = res.data.get("claims") or []
    allowed = {d["document_id"]: d["url"] for d in docs}
    validate_claims(claims, allowed)  # raises CitationError: a note with an uncited claim fails outright
    kept, dropped = fact_check(claims, corpora, computed_fields(signal), llm, settings.brand)
    note = session.exec(select(Note).where(Note.signal_id == signal.id, Note.kind == "signal")).first() or Note(
        kind="signal", signal_id=signal.id, ticker=signal.ticker, title="", body_md=""
    )
    note.title = str(res.data.get("headline") or signal.summary)[:200]
    note.body_md = render_note_md(kept, docs) if kept else "(all claims rejected by fact-check)"
    note.claims_json = json.dumps(kept)
    note.factcheck_log = json.dumps(dropped)
    note.factcheck_passed = len(kept) >= MIN_CLAIMS_TO_PASS
    note.model = res.model
    note.updated_at = utcnow()
    session.add(note)
    session.flush()
    return note


def select_signals(session: Session, limit: int, threshold: float, redo: bool = False) -> list[Signal]:
    noted = set() if redo else {n.signal_id for n in session.exec(select(Note).where(Note.kind == "signal")).all()}
    rows = session.exec(select(Signal).where(Signal.significance >= threshold).order_by(Signal.rank_score.desc())).all()
    return interleave_by_source([s for s in rows if s.id not in noted])[:limit]


def interleave_by_source(signals: list[Signal]) -> list[Signal]:
    """Round-robin across sources (each in rank order) so one prolific source can't crowd out local records."""
    queues: dict[str, list[Signal]] = {}
    for s in signals:
        queues.setdefault(s.source_id, []).append(s)
    out: list[Signal] = []
    while any(queues.values()):
        for source_id in list(queues):
            if queues[source_id]:
                out.append(queues[source_id].pop(0))
    return out


def run_research(
    session: Session,
    llm: LLM,
    limit: int | None = None,
    threshold: float | None = None,
    tickers: list[str] | None = None,
    redo: bool = False,
) -> ResearchSummary:
    settings = get_settings()
    limit = limit if limit is not None else settings.max_notes_per_run
    threshold = threshold if threshold is not None else settings.signal_note_threshold
    summary = ResearchSummary()
    signals = select_signals(session, 10_000 if tickers else limit, threshold, redo)
    if tickers:
        signals = [s for s in signals if s.ticker in tickers][:limit]
    for signal in signals:
        summary.attempted += 1
        try:
            note = write_signal_note(session, signal, llm)
            session.commit()
        except CitationError as exc:
            session.rollback()
            summary.failed += 1
            summary.errors.append(f"{signal.ticker}/{signal.metric}: citation enforcement failed: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            summary.failed += 1
            summary.errors.append(f"{signal.ticker}/{signal.metric}: {type(exc).__name__}: {exc}")
            log.exception("note failed")
            continue
        summary.rejected_claims += len(json.loads(note.factcheck_log))
        if note.factcheck_passed:
            summary.passed += 1
        else:
            summary.failed += 1
    return summary


# --- mock (no LLM): extractive claims quoting the documents verbatim ---------------------------------
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“(])")


def _verbatim_span(text: str, must: list[str] | None = None, max_words: int = 45) -> str | None:
    lines = [ln.strip() for ln in text.splitlines() if len(ln.split()) >= 6]
    pool = []
    for ln in lines:
        pool.extend(s.strip() for s in _SENT.split(ln) if len(s.split()) >= 6)
    if must:
        for s in pool:
            if all(m.lower() in s.lower() for m in must):
                return " ".join(s.split()[:max_words])
    return " ".join(pool[0].split()[:max_words]) if pool else None


@register_mock("analyst_signal_note")
def _mock_analyst(variables: dict) -> dict:
    docs = variables["documents"]
    signal = variables["signal"]
    claims = []
    src = docs[0]
    lines = [ln for ln in src["text"].splitlines() if ln.strip()]
    quote = next((ln for ln in reversed(lines) if any(ch.isdigit() for ch in ln) and len(ln.split()) >= 3), None)
    if quote:
        claims.append(
            {
                "text": signal["summary"],
                "citation": {
                    "document_id": src["document_id"],
                    "url": src["url"],
                    "quote": " ".join(quote.split()[:60]),
                },
            }
        )
    for d in docs[1:]:
        if len(claims) >= 4:
            break
        span = _verbatim_span(d["text"])
        if span:
            claims.append(
                {
                    "text": f'{d["title"]} states: "{span}"',
                    "citation": {"document_id": d["document_id"], "url": d["url"], "quote": span},
                }
            )
    company = variables["company"]
    return {"headline": f"{company['ticker']}: {signal['metric'].replace('_', ' ')}", "claims": claims}
