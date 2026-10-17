import pytest

from localledger.research.factcheck import (
    deterministic_check,
    extract_numbers,
    fact_check,
    numbers_supported,
    validate_claims,
)
from localledger.signals.store import CitationError

DOC = (
    "Pure Cycle reported $2.9 million and $8.6 million of net income for the three and nine months ended "
    "May 31, 2026, respectively, which is a 31% and 23% increase in net income from the same periods in 2025.\n"
    "(In thousands) | May 31, 2026 | May 31, 2025\nNet Income | $2,948 | $2,256"
)
URL = "https://www.sec.gov/doc"


def claim(text, quote, doc_id=1, url=URL):
    return {"text": text, "citation": {"document_id": doc_id, "url": url, "quote": quote}}


def test_note_with_uncited_claim_fails():
    good = claim("Net income was $2.9 million.", "reported $2.9 million and $8.6 million of net income")
    uncited = {"text": "Management expects strong growth next year."}
    with pytest.raises(CitationError):
        validate_claims([good, uncited], {1: URL})
    with pytest.raises(CitationError):
        validate_claims([claim("x y z", "a quote here")], {2: URL})  # cites a document that wasn't provided
    with pytest.raises(CitationError):
        validate_claims([claim("x y z", "")], {1: URL})  # no quote
    with pytest.raises(CitationError):
        validate_claims([], {1: URL})
    validate_claims([good], {1: URL})


def test_number_extraction_and_scaling():
    nums = extract_numbers("revenue of $14.3 million, up 72% from 8,307,000; 1.5B")
    assert [(n.value, n.scale, n.is_pct) for n in nums][:3] == [
        (14.3, 1e6, False),
        (72, 1.0, True),
        (8307000, 1, False),
    ]
    assert numbers_supported("Net income was $2.9 million.", DOC, []) == []
    assert numbers_supported("Net income was $2,948 thousand, i.e. $2.95 million.", DOC, []) == []  # in-thousands table
    assert numbers_supported("Net income rose 31%.", DOC, []) == []
    assert numbers_supported("Net income rose 45%.", DOC, []) == ["45%"]
    assert numbers_supported("Units rose 60.0% to 8.2 million.", "x", [8_178_000, 5_110_000, 0.60]) == []
    assert numbers_supported("In fiscal 2026 the third quarter...", DOC, []) == []  # years/quarter labels are context
    assert numbers_supported("Net income was $31 million.", DOC, []) == ["$31 1e+06x"]  # "May 31" is not $31M


def test_deterministic_check_rejects_unsupported_claims():
    q = "which is a 31% and 23% increase in net income from the same periods in 2025"
    assert deterministic_check(claim("Net income rose 31% year over year.", q), DOC, []) is None
    assert "verbatim" in deterministic_check(claim("Net income rose 31%.", "net income soared to records"), DOC, [])
    assert "numbers" in deterministic_check(claim("Net income rose 40%.", q), DOC, [])
    reason = deterministic_check(claim("Net income rose 31%, so investors should buy the stock.", q), DOC, [])
    assert reason and "recommendation" in reason


def test_fact_check_drops_and_logs(env):
    q = "which is a 31% and 23% increase in net income from the same periods in 2025"
    claims = [claim("Net income rose 31% year over year.", q), claim("Net income tripled to $31 million.", q)]
    kept, dropped = fact_check(claims, {1: DOC}, [], llm=None)
    assert len(kept) == 1 and len(dropped) == 1
    assert dropped[0]["stage"] == "deterministic"
