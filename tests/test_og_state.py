import csv
import io
import json
from datetime import date

from sqlmodel import select

from localledger.connectors.base import ensure_source
from localledger.connectors.edgar import EdgarConnector
from localledger.connectors.og_state import (
    COUNTY_PAGE,
    OgStateConnector,
    Series,
    aggregate,
    county_url,
    parse_declaration,
    parse_rows,
    unzip_csv,
    years_needed,
    ym,
)
from localledger.http import DocStore, html_to_text, read_document_text
from localledger.models import Document, Signal
from tests.conftest import FIXTURES

F = FIXTURES / "og_state"
HIL = "HILCORP ENERGY COMPANY"
EX99_URL = "https://www.sec.gov/Archives/edgar/data/319655/000119312526395495/sjt-ex99_1.htm"
MAP = [
    {
        "ticker": "SJT",
        "state": "NM",
        "basin": "San Juan Basin",
        "operators": [HIL],
        "counties": ["San Juan", "Rio Arriba"],
        "primary": "gas",
        "distribution": {"lag_months": 2},
    }
]
TODAY = date(2026, 10, 17)


def routes():
    out = {COUNTY_PAGE: F / "county.aspx.html"}
    for county in ("San Juan", "Rio Arriba"):
        for year in (2025, 2026):
            out[county_url(county, year)] = F / f"{county}{year}.zip"
    return out


def expected(product: str, county: str, year: int, month: int) -> int:
    """Independent re-count straight from the fixture CSV."""
    _, text = unzip_csv((F / f"{county}{year}.zip").read_bytes())
    rows = csv.DictReader(io.StringIO(text))
    return round(sum(float(r[f"{product}{month}"]) for r in rows if r["OgridName"] == HIL))


def hilcorp(product: str, months: list[tuple[int, int]]) -> int:
    return sum(expected(product, c, y, m) for c in ("San Juan", "Rio Arriba") for y, m in months)


def ex99_text() -> str:
    return html_to_text((F / "sjt_ex99_1.htm").read_bytes())


def store_ex99(session) -> Document:
    ensure_source(session, EdgarConnector)
    return DocStore(session).save(
        source_id="edgar",
        url=EX99_URL,
        content=(F / "sjt_ex99_1.htm").read_bytes(),
        content_type="text/html",
        ticker="SJT",
        title="SJT 8-K Ex. 99.1 2026-09-18",
        meta={"kind": "ex99", "form": "8-K", "filing_date": "2026-09-18"},
        origin="fixture",
    )


def test_urls_and_years():
    assert county_url("San Juan", 2026) == "https://octane.nmt.edu/gotech/Petroleum_Data/county/San%20Juan2026.zip"
    assert years_needed(TODAY) == [2025, 2026]
    assert years_needed(date(2027, 2, 1)) == [2025, 2026, 2027]


def test_aggregate_counts_only_the_mapped_operator():
    _, text = unzip_csv((F / "San Juan2026.zip").read_bytes())
    header, idx, rows = parse_rows(text)
    agg = aggregate(rows, idx, [HIL.lower()], "gas", 2026)
    assert agg["rows"] == 8 and len(rows) == 10  # two other operators' rows are ignored
    assert round(agg["gas"][7]) == expected("Gas", "San Juan", 2026, 8)
    assert len(agg["top"][7]) == 3 and all(line in text for line in agg["top"][7])
    assert header.startswith("PropertyName,WellID,API,OgridName")


def test_parse_sjt_declaration():
    text = ex99_text()
    d = parse_declaration(text)
    assert d["amount"] == 0.0
    assert d["month"] == ym(2026, 9) and d["production_month"] == ym(2026, 7)
    assert d["excess_costs_gross"] == 12_553_466
    assert d["trust_gas"] == {
        "month": ym(2026, 7),
        "mcf": 2_046_556,
        "prior_month": ym(2026, 6),
        "prior_mcf": 1_950_221,
    }
    assert d["sentences"] and all(s in text for s in d["sentences"])


def test_parse_declaration_with_amount():
    text = (
        "Cross Timbers Royalty Trust Declares September Cash Distribution\n"
        "The Trust today declared a cash distribution to the holders of its units of beneficial interest of "
        "$0.064277 per unit, payable on October 14, 2026."
    )
    assert parse_declaration(text)["amount"] == 0.064277


def test_og_signals_from_fixtures(seeded, fake_http):
    ex99 = store_ex99(seeded)
    conn = OgStateConnector(seeded, http=fake_http(routes()), og_map=MAP, today=TODAY, origin="fixture")
    result = conn.run()
    assert result.status == "available", result.errors
    assert result.documents == 4 and result.signals == 4
    sigs = {s.metric: s for s in seeded.exec(select(Signal).where(Signal.ticker == "SJT")).all()}
    assert set(sigs) == {
        "og_production_3m_vs_prior",
        "og_production_3m_yoy",
        "og_production_month_yoy",
        "og_implied_distribution_direction",
    }
    jun_aug_26 = [(2026, m) for m in (6, 7, 8)]
    q = sigs["og_production_3m_vs_prior"]
    assert q.value == hilcorp("Gas", jun_aug_26)
    assert q.prior_value == hilcorp("Gas", [(2026, m) for m in (3, 4, 5)])
    assert q.citation_url == county_url("San Juan", 2026) and q.period == "nm:2026-08"
    assert "Jun-Aug 2026" in q.summary and "Mar-May 2026" in q.summary and "not SJT's net share" in q.summary
    yoy = sigs["og_production_3m_yoy"]
    assert yoy.prior_value == hilcorp("Gas", [(2025, m) for m in (6, 7, 8)])
    detail = json.loads(yoy.detail_json)
    prior_doc = seeded.get(Document, detail["prior_document_id"])
    assert prior_doc.url == county_url("San Juan", 2025)
    assert abs(detail["pct_change"] - (yoy.value - yoy.prior_value) / yoy.prior_value) < 1e-9
    # Excerpt rows are verbatim lines of the stored documents they came from.
    doc = seeded.get(Document, yoy.document_id)
    current, prior = detail["excerpt"].split("\n\n[prior-period file " + prior_doc.url + "]\n")
    assert all(line in read_document_text(doc) for line in current.split("\n"))
    assert all(line in read_document_text(prior_doc) for line in prior.split("\n"))
    assert doc.content_type == "application/zip" and doc.origin == "fixture"

    dist = sigs["og_implied_distribution_direction"]
    d = json.loads(dist.detail_json)
    assert d["direction"] == "unchanged (no distribution)"
    assert d["prior_document_id"] == ex99.id and d["feeding_months"] == ["2026-08"] and d["base_month"] == "2026-07"
    assert dist.value == hilcorp("Gas", [(2026, 8)]) and dist.prior_value == hilcorp("Gas", [(2026, 7)])
    assert "$12,553,466" in dist.summary and "2-month production-to-declaration lag" in dist.summary
    assert "prices and costs not modeled" in dist.summary and d["price_modeled"] is False
    assert d["proxy_check"]["trust_reported_mcf"] == [1_950_221, 2_046_556]
    ex_text = read_document_text(ex99)
    rows_part, ex_part = d["excerpt"].split(f"\n\n[last declared distribution: {EX99_URL}]\n")
    assert all(line in ex_text for line in ex_part.split("\n"))
    assert all(line in read_document_text(doc) for line in rows_part.split("\n"))


def test_distribution_needs_a_stored_declaration(seeded, fake_http):
    conn = OgStateConnector(seeded, http=fake_http(routes()), og_map=MAP, today=TODAY, origin="fixture")
    result = conn.run()
    assert result.signals == 3  # production signals only; no 8-K Ex. 99.1 stored yet
    metrics = {s.metric for s in seeded.exec(select(Signal)).all()}
    assert "og_implied_distribution_direction" not in metrics


def test_latest_complete_skips_a_partly_filed_month():
    def rec(reporting, gas):
        return {"reporting": reporting, "gas": gas, "oil": [0.0] * 12, "top": [[] for _ in range(12)]}

    reporting = [100] * 7 + [40] + [0] * 4  # August only 40% filed so far
    gas = [1000.0] * 7 + [420.0] + [0.0] * 4
    s = Series({"counties": ["San Juan"], "primary": "gas"}, {("San Juan", 2026): rec(reporting, gas)})
    assert s.latest_complete() == ym(2026, 7)
    # A small operator with two wells shut in but full volume still counts as reported.
    reporting = [30] * 7 + [25] + [0] * 4
    gas = [1000.0] * 7 + [990.0] + [0.0] * 4
    s = Series({"counties": ["San Juan"], "primary": "gas"}, {("San Juan", 2026): rec(reporting, gas)})
    assert s.latest_complete() == ym(2026, 8)


def test_bot_challenge_is_not_bypassed(seeded, fake_http):
    conn = OgStateConnector(
        seeded, http=fake_http({COUNTY_PAGE: F / "ocd_turnstile.html"}), og_map=MAP, today=TODAY, origin="fixture"
    )
    result = conn.run()
    assert result.status == "unavailable" and "bot-verification" in result.errors[0]


def test_robots_block_is_unavailable(seeded):
    from localledger.http import RobotsDisallowed

    class BlockedHttp:
        def get_ok(self, url, **kw):
            raise RobotsDisallowed(url)

    result = OgStateConnector(seeded, http=BlockedHttp(), og_map=MAP, today=TODAY).run()
    assert result.status == "unavailable" and "robots" in result.errors[0]
