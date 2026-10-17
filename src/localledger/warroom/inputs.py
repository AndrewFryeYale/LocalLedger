"""Real public inputs for a war-room replay: the latest 8-K Ex. 99.1, the prior 10-Q, the 10-K, notes and signals."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlmodel import Session, select

from localledger.http import read_document_bytes, read_document_text
from localledger.models import Company, Document, Note, Signal
from localledger.research.retrieval import BM25, chunk_text, tokenize

log = logging.getLogger(__name__)

RELEASE_CHARS = 26_000


@dataclass
class WarroomInputs:
    ticker: str
    release: Document | None = None
    prior_quarterly: Document | None = None
    annual: Document | None = None
    documents: list[dict] = field(default_factory=list)  # what the agents see (verbatim excerpts)
    corpora: dict[int, str] = field(default_factory=dict)  # full text per document id, for the fact-check
    notes: list[Note] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    peer_signals: list[Signal] = field(default_factory=list)
    period_label: str = ""


def _meta(doc: Document) -> dict:
    return json.loads(doc.meta_json or "{}")


def _edgar_docs(session: Session, ticker: str) -> list[Document]:
    return list(
        session.exec(
            select(Document).where(Document.ticker == ticker, Document.source_id == "edgar").order_by(Document.id)
        ).all()
    )


def _ensure_prior_quarterly(session: Session, company: Company, release: Document | None) -> Document | None:
    """The 10-Q (or 10-K) filed before the release's quarter, fetched from EDGAR if we don't have it yet."""
    docs = _edgar_docs(session, company.ticker)
    subs = [d for d in docs if _meta(d).get("kind") == "submissions"]
    release_date = _meta(release).get("filing_date") if release else None
    if not subs or not release_date:
        return next((d for d in reversed(docs) if _meta(d).get("kind") == "10-q"), None)
    sub = json.loads(read_document_bytes(subs[-1]))
    recent = sub.get("filings", {}).get("recent", {})
    filings = [dict(zip(recent.keys(), vals, strict=False)) for vals in zip(*recent.values(), strict=False)]
    # Skip the 10-Q filed alongside this release (same quarter); take the next older periodic report.
    candidates = [f for f in filings if f["form"] in ("10-Q", "10-K") and f["filingDate"] < _shift(release_date, -14)]
    if not candidates:
        return None
    target = candidates[0]
    known = next((d for d in docs if _meta(d).get("accession") == target["accessionNumber"]), None)
    if known:
        return known
    try:
        from localledger.connectors.edgar import EdgarConnector

        conn = EdgarConnector(session, frames=False, full_text=False)
        fetched = conn._fetch_primary(company, target, kind=target["form"].lower())
        session.commit()
        return fetched[0] if fetched else None
    except Exception as exc:  # network down etc: run with what we have
        log.warning("could not fetch prior periodic report for %s: %s", company.ticker, exc)
        return None


def _shift(iso: str, days: int) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(iso) + timedelta(days=days)).isoformat()


def _top_chunks(text: str, query: str, k: int, size: int = 1500) -> str:
    chunks = [c for c in chunk_text(text, size) if len(c) > 200]
    if not chunks:
        return text[: k * size]
    scores = BM25([tokenize(c) for c in chunks]).score(tokenize(query))
    best = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)[:k]
    return "\n...\n".join(chunks[i] for i in sorted(best))


def gather_inputs(session: Session, ticker: str, fetch: bool = True) -> WarroomInputs:
    company = session.get(Company, ticker)
    docs = _edgar_docs(session, ticker)
    releases = [d for d in docs if _meta(d).get("kind") == "ex99"]
    releases.sort(key=lambda d: _meta(d).get("filing_date") or "")
    inputs = WarroomInputs(ticker=ticker)
    inputs.release = releases[-1] if releases else None
    inputs.annual = next((d for d in reversed(docs) if _meta(d).get("kind") == "10-k"), None)
    if fetch:
        inputs.prior_quarterly = _ensure_prior_quarterly(session, company, inputs.release)
    else:
        inputs.prior_quarterly = next((d for d in reversed(docs) if _meta(d).get("kind") == "10-q"), None)

    query = f"{company.local_gov_driver} {company.note or ''} revenue results operations segment"
    if inputs.release is not None:
        text = read_document_text(inputs.release)
        inputs.corpora[inputs.release.id] = text
        inputs.documents.append(
            {
                "document_id": inputs.release.id,
                "url": inputs.release.url,
                "title": inputs.release.title,
                "text": text[:RELEASE_CHARS],
            }
        )
        query += " " + " ".join(text[:3000].split()[:200])
        inputs.period_label = _meta(inputs.release).get("filing_date", "")
    for doc, k, label in ((inputs.prior_quarterly, 4, "prior quarter"), (inputs.annual, 3, "annual report")):
        if doc is None or doc.id in inputs.corpora:
            continue
        text = read_document_text(doc)
        inputs.corpora[doc.id] = text
        inputs.documents.append(
            {
                "document_id": doc.id,
                "url": doc.url,
                "title": f"{doc.title} ({label})",
                "text": _top_chunks(text, query, k),
            }
        )
    inputs.notes = list(
        session.exec(
            select(Note)
            .where(Note.ticker == ticker, Note.factcheck_passed == True)  # noqa: E712
            .order_by(Note.updated_at.desc())
            .limit(5)
        ).all()
    )
    inputs.signals = list(
        session.exec(select(Signal).where(Signal.ticker == ticker).order_by(Signal.rank_score.desc()).limit(12)).all()
    )
    peers = [
        c.ticker
        for c in session.exec(
            select(Company).where(Company.archetype == company.archetype, Company.ticker != ticker)
        ).all()
    ]
    inputs.peer_signals = (
        list(
            session.exec(
                select(Signal).where(Signal.ticker.in_(peers)).order_by(Signal.rank_score.desc()).limit(15)
            ).all()
        )
        if peers
        else []
    )
    return inputs
