"""Connector contract: discover() -> fetch() -> parse() -> to_signals().

`run()` wraps the four steps with graceful degradation: a failure while discovering marks the source
unavailable; a failure on one target is logged and the run continues. Nothing here ever raises to the CLI.
"""

from __future__ import annotations

import logging
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from sqlmodel import Session, select

from localledger.config import Settings, get_settings
from localledger.http import DocStore, HttpClient
from localledger.models import Company, Document, Source, utcnow
from localledger.signals.store import CitationError, SignalDraft, make_signal

log = logging.getLogger(__name__)


class ConnectorUnavailable(Exception):
    """Source can't be reached or needs a key that isn't set."""


@dataclass
class RunResult:
    source_id: str
    status: str = "unknown"
    targets: int = 0
    documents: int = 0
    signals: int = 0
    tickers: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        err = f" | {len(self.errors)} errors" if self.errors else ""
        return (
            f"[{self.source_id}] {self.status}: {self.targets} targets, {self.documents} docs, "
            f"{self.signals} signals across {len(self.tickers)} tickers{err}"
        )


class Connector(ABC):
    source_id: ClassVar[str]
    name: ClassVar[str]
    type: ClassVar[str]
    access_method: ClassVar[str]
    cadence: ClassVar[str]
    terms_note: ClassVar[str]

    def __init__(
        self,
        session: Session,
        http: HttpClient | Any | None = None,
        settings: Settings | None = None,
        origin: str = "real",
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.http = http or HttpClient(self.settings)
        self.store = DocStore(session, self.settings)
        self.origin = origin
        self.tickers: set[str] | None = None
        ensure_source(session, type(self))
        session.commit()  # a later per-target rollback must not discard the Source row

    # --- contract -------------------------------------------------------------------------------
    @abstractmethod
    def discover(self) -> list[Any]:
        """Return the targets to fetch (tickers, geographies, agendas...)."""

    @abstractmethod
    def fetch(self, target: Any) -> list[Document]:
        """Fetch raw items for a target and store them as Documents."""

    @abstractmethod
    def parse(self, doc: Document) -> list[dict]:
        """Turn one stored document into structured records (each carries document_id)."""

    @abstractmethod
    def to_signals(self, records: list[dict]) -> list[SignalDraft]:
        """Turn records into cited signal drafts."""

    # --- helpers --------------------------------------------------------------------------------
    def save(self, **kwargs: Any) -> Document:
        kwargs.setdefault("origin", self.origin)
        return self.store.save(source_id=self.source_id, **kwargs)

    def universe(self) -> dict[str, Company]:
        companies = {c.ticker: c for c in self.session.exec(select(Company)).all()}
        if self.tickers:
            companies = {t: c for t, c in companies.items() if t in self.tickers}
        return companies

    def run(self, tickers: list[str] | None = None, limit: int | None = None) -> RunResult:
        self.tickers = {t.upper() for t in tickers} if tickers else None
        result = RunResult(source_id=self.source_id)
        source = self.session.get(Source, self.source_id)
        try:
            targets = self.discover()
        except Exception as exc:
            msg = f"discover failed: {type(exc).__name__}: {exc}"
            log.warning("[%s] %s", self.source_id, msg)
            result.status = "unavailable"
            result.errors.append(msg)
            self._finish(source, result)
            return result
        if limit:
            targets = targets[:limit]
        result.targets = len(targets)
        records: list[dict] = []
        for target in targets:
            try:
                docs = self.fetch(target)
                for doc in docs:
                    records.extend(self.parse(doc))
                result.documents += len(docs)
                self.session.commit()
            except Exception as exc:
                self.session.rollback()
                msg = f"{target!r}: {type(exc).__name__}: {exc}"
                log.info("[%s] target failed %s", self.source_id, msg)
                log.debug(traceback.format_exc())
                result.errors.append(msg[:300])
        try:
            drafts = self.to_signals(records)
        except Exception as exc:
            result.errors.append(f"to_signals failed: {type(exc).__name__}: {exc}")
            drafts = []
        for draft in drafts:
            try:
                make_signal(self.session, self.source_id, draft)
                result.signals += 1
                result.tickers.add(draft.ticker)
            except (CitationError, ValueError) as exc:
                result.errors.append(f"signal rejected: {exc}")
        self.session.commit()
        if result.documents == 0 and result.errors:
            result.status = "unavailable"
        elif result.errors:
            result.status = "degraded"
        else:
            result.status = "available"
        self._finish(source, result)
        return result

    def _finish(self, source: Source | None, result: RunResult) -> None:
        if source is None:
            return
        source.status = result.status
        source.last_run_at = utcnow()
        source.last_error = "\n".join(result.errors[:5]) if result.errors else None
        self.session.add(source)
        self.session.commit()


def ensure_source(session: Session, cls: type[Connector]) -> Source:
    source = session.get(Source, cls.source_id)
    if source is None:
        source = Source(
            id=cls.source_id,
            name=cls.name,
            type=cls.type,
            access_method=cls.access_method,
            cadence=cls.cadence,
            terms_note=cls.terms_note,
        )
    else:
        source.name, source.type = cls.name, cls.type
        source.access_method, source.cadence, source.terms_note = cls.access_method, cls.cadence, cls.terms_note
    session.add(source)
    session.flush()
    return source
