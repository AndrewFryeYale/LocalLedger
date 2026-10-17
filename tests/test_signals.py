from datetime import date

import pytest

from localledger.connectors.base import ensure_source
from localledger.connectors.edgar import EdgarConnector
from localledger.http import DocStore
from localledger.signals.scoring import (
    move_significance,
    pct_change,
    priority_norm,
    rank_score,
    scale_by_volume,
)
from localledger.signals.store import CitationError, SignalDraft, make_signal


def test_pct_change_and_significance():
    assert pct_change(110, 100) == pytest.approx(0.10)
    assert pct_change(90, -100) == pytest.approx(1.9)
    assert pct_change(5, 0) is None
    assert move_significance(0.02) == 0.1
    assert move_significance(0.6) == 1.0
    mid = move_significance(0.15)
    assert 0.1 < mid < 1.0
    assert move_significance(-0.15) == mid  # symmetric
    assert scale_by_volume(0.8, 2, 10) == pytest.approx(0.2 * 0.8)
    assert scale_by_volume(0.8, 50, 10) == 0.8


def test_rank_score_blends_priority_and_recency():
    assert priority_norm(9) == 1.0 and priority_norm(5) == 0.0
    today = date(2026, 10, 17)
    fresh = rank_score(0.5, 9, date(2026, 10, 10), today)
    stale = rank_score(0.5, 9, date(2026, 3, 1), today)
    low_priority = rank_score(0.5, 5, date(2026, 10, 10), today)
    assert fresh > stale
    assert fresh > low_priority


def _doc(session):
    ensure_source(session, EdgarConnector)
    return DocStore(session).save(
        source_id="edgar", url="https://www.sec.gov/x", content=b"{}", content_type="application/json", origin="fixture"
    )


def test_signal_requires_citation(seeded):
    doc = _doc(seeded)
    base = dict(ticker="PCYO", metric="m", as_of=date(2026, 10, 1), summary="s")
    with pytest.raises(CitationError):
        make_signal(seeded, "edgar", SignalDraft(**base, citation_url=None, document_id=doc.id))
    with pytest.raises(CitationError):
        make_signal(seeded, "edgar", SignalDraft(**base, citation_url="https://www.sec.gov/x", document_id=None))
    with pytest.raises(CitationError):
        make_signal(seeded, "edgar", SignalDraft(**base, citation_url="https://www.sec.gov/x", document_id=99999))
    sig = make_signal(
        seeded,
        "edgar",
        SignalDraft(**base, citation_url="https://www.sec.gov/x", document_id=doc.id, value=12, prior_value=10),
    )
    assert sig.delta == 2


def test_signal_dedupes_on_rerun(seeded):
    doc = _doc(seeded)
    draft = SignalDraft(
        ticker="PCYO",
        metric="m",
        as_of=date(2026, 10, 1),
        summary="s",
        period="P1",
        citation_url="https://www.sec.gov/x",
        document_id=doc.id,
        value=1,
    )
    a = make_signal(seeded, "edgar", draft)
    draft.value = 2
    b = make_signal(seeded, "edgar", draft)
    assert a.id == b.id and b.value == 2
