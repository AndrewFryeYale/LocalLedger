from datetime import date

from sqlmodel import select

from localledger.connectors.edgar import (
    EdgarConnector,
    parse_exhibit_index,
    recent_quarters,
)
from localledger.models import Company, Document, Signal
from tests.conftest import FIXTURES

E = FIXTURES / "edgar"
BASE = "https://www.sec.gov/Archives/edgar/data/276720/"


def routes():
    rev = "https://data.sec.gov/api/xbrl/frames/us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax/USD/"
    return {
        "https://www.sec.gov/files/company_tickers.json": E / "company_tickers.json",
        "https://data.sec.gov/submissions/CIK0000276720.json": E / "submissions_PCYO.json",
        BASE + "000110465926082168/0001104659-26-082168-index.htm": E / "pcyo_8k_index.htm",
        BASE + "000110465926082168/pcyo-20260707xex99d1.htm": E / "pcyo_ex99_1.htm",
        BASE + "000110465926100607/pcyo-20260824x8k.htm": E / "pcyo_8k_502.htm",
        BASE + "000110465926081778/pcyo-20260531x10q.htm": E / "pcyo_10q.htm",
        BASE + "000110465925110312/pcyo-20250831x10k.htm": E / "pcyo_10k.htm",
        rev + "CY2026Q2.json": E / "frame_rev_CY2026Q2.json",
        rev + "CY2025Q2.json": E / "frame_rev_CY2025Q2.json",
    }


def test_recent_quarters():
    assert recent_quarters(date(2026, 10, 17)) == [("CY2026Q3", "CY2025Q3"), ("CY2026Q2", "CY2025Q2")]
    assert recent_quarters(date(2026, 2, 1), 1) == [("CY2025Q4", "CY2024Q4")]


def test_exhibit_index_finds_ex991():
    rows = parse_exhibit_index((E / "pcyo_8k_index.htm").read_bytes())
    ex = [r for r in rows if r["type"] == "EX-99.1"]
    assert ex and ex[0]["url"].endswith("pcyo-20260707xex99d1.htm")


def test_edgar_run_on_fixtures(seeded, fake_http):
    conn = EdgarConnector(seeded, http=fake_http(routes()), today=date(2026, 10, 17), origin="fixture")
    result = conn.run(tickers=["PCYO"])
    assert result.status in ("available", "degraded")
    assert seeded.get(Company, "PCYO").cik == "276720"
    signals = {s.metric: s for s in seeded.exec(select(Signal).where(Signal.ticker == "PCYO")).all()}
    assert "earnings_release" not in signals  # 2026-07-09 is outside the 75-day event window from 10/17
    assert signals["8k_item_5.02"].citation_url.endswith("pcyo-20260824x8k.htm")
    rev = signals["revenue_yoy"]
    assert rev.period == "CY2026Q2"
    assert rev.value == 8_178_000 or rev.value > 8_000_000
    assert rev.delta == rev.value - rev.prior_value
    assert "+60.0% y/y" in rev.summary
    for s in signals.values():
        doc = seeded.get(Document, s.document_id)
        assert doc is not None and doc.origin == "fixture"
        assert s.citation_url.startswith("https://www.sec.gov/")
    kinds = {d.title for d in seeded.exec(select(Document).where(Document.ticker == "PCYO")).all()}
    assert any("Ex. 99.1" in k for k in kinds) and any("10-K" in k for k in kinds)


def test_edgar_unavailable_degrades(seeded, fake_http):
    conn = EdgarConnector(seeded, http=fake_http({}), today=date(2026, 10, 17))
    result = conn.run(tickers=["PCYO"])
    assert result.status == "unavailable"
    assert result.errors
