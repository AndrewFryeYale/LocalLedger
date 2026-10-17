import json

from sqlmodel import select

from localledger.connectors.gaming_state import (
    ARCHIVE_PAGE,
    CURRENT_PAGE,
    GamingStateConnector,
    archive_pages,
    money,
    parse_report,
    report_links,
    ym,
)
from localledger.http import read_document_text
from localledger.models import Document, Signal
from localledger.pdftext import pdf_to_text
from tests.conftest import FIXTURES

F = FIXTURES / "gaming_state"
MAP = [
    {
        "ticker": "FLL",
        "state": "IN",
        "property": "Rising Star Casino",
        "licensee": "Gaming Entertainment (Indiana) LLC",
        "location": "Rising Sun, IN",
    }
]
DA = "https://www.in.gov/dA/"
URLS = {
    ym(2026, 1): DA + "e33cdb7933/2026-01-Revenue.pdf?language_id=1",
    ym(2026, 2): DA + "d453f21a16/2026-02-Revenue.pdf?language_id=1",
    ym(2025, 1): DA + "2b94d67a5e/2025-01-Revenue.pdf?language_id=1",
    ym(2025, 2): DA + "3a002312c9/2025-02-Revenue.pdf?language_id=1",
}
ARCHIVE_2025 = "https://www.in.gov/igc/publications/archived-monthly-revenue-reports/igc-monthly-revenue-reports-2025"


def routes():
    out = {
        CURRENT_PAGE: F / "monthly-revenue.html",
        ARCHIVE_PAGE: F / "archived-index.html",
        ARCHIVE_2025: F / "archived-2025.html",
    }
    for m, url in URLS.items():
        out[url] = F / f"{m // 12}-{m % 12 + 1:02d}-Revenue.pdf"
    return out


def test_money_and_links():
    assert money("$3,606,652") == 3_606_652 and money("($1,500,000)") == -1_500_000
    links = report_links((F / "monthly-revenue.html").read_bytes(), CURRENT_PAGE)
    assert links == {ym(2026, 1): URLS[ym(2026, 1)], ym(2026, 2): URLS[ym(2026, 2)]}  # PDFs only, not XLSX
    pages = archive_pages((F / "archived-index.html").read_bytes(), ARCHIVE_PAGE)
    assert pages[2025] == ARCHIVE_2025 and 2024 in pages


def test_parse_september_report():
    text = pdf_to_text((F / "2026-09Revenue.pdf").read_bytes())
    report = parse_report(text)
    assert report["month"] == ym(2026, 9) and len(report["properties"]) == 13
    rs = report["properties"]["Rising Star Casino"]
    assert rs["agr"] == 3_608_697 and rs["win"] == 3_606_652 and rs["free_play"] == 0
    assert rs["table_win"] == 199_589 and rs["egd_win"] == 3_407_063 and rs["tables"] == 16
    assert rs["egd_units"] == 625 and rs["coin_in"] == 32_160_447
    assert all(line in text for line in rs["lines"].values())
    hoosier = report["properties"]["Harrah's Hoosier Park"]  # racino row printed with "**"
    assert hoosier["free_play"] == -1_577_236 and hoosier["agr"] == 17_452_525


def test_title_with_split_words():
    # pdfplumber prints this report's title as "reported fo rJanuary 2026".
    report = parse_report(pdf_to_text((F / "2026-01-Revenue.pdf").read_bytes()))
    assert report["month"] == ym(2026, 1) and report["properties"]["Rising Star Casino"]["agr"] == 3_428_017


def test_gaming_signals_from_fixtures(seeded, fake_http):
    conn = GamingStateConnector(seeded, http=fake_http(routes()), gaming_map=MAP, origin="fixture")
    result = conn.run()
    assert result.status == "available", result.errors
    assert result.targets == 4 and result.documents == 4 and result.signals == 3
    sigs = {s.metric: s for s in seeded.exec(select(Signal).where(Signal.ticker == "FLL")).all()}
    month = sigs["gaming_agr_month_yoy"]
    assert month.value == 2_421_239 and month.prior_value == 2_262_602
    assert month.citation_url == URLS[ym(2026, 2)] and month.period == "in:rising-star-casino:2026-02"
    assert "Rising Star Casino (Rising Sun, IN; licensee Gaming Entertainment (Indiana) LLC)" in month.summary
    assert "+7.0% y/y" in month.summary
    detail = json.loads(month.detail_json)
    prior = seeded.get(Document, detail["prior_document_id"])
    assert prior.url == URLS[ym(2025, 2)]
    doc = seeded.get(Document, month.document_id)
    current, previous = detail["excerpt"].split(f"\n\n[prior-year report {prior.url}]\n")
    assert all(line in read_document_text(doc) for line in current.split("\n"))
    assert all(line in read_document_text(prior) for line in previous.split("\n"))
    assert doc.content_type == "application/pdf" and doc.origin == "fixture"

    ytd = sigs["gaming_agr_ytd_yoy"]
    assert ytd.value == 3_428_017 + 2_421_239 and ytd.prior_value == 3_239_842 + 2_262_602
    assert "Jan-Feb 2026" in ytd.summary and "Jan-Feb 2025" in ytd.summary

    handle = sigs["gaming_handle_month_yoy"]
    assert handle.value == 30_872_123 and handle.prior_value == 28_119_829
    assert "coin-in" in handle.summary


def test_closed_months_are_reused(seeded, fake_http):
    GamingStateConnector(seeded, http=fake_http(routes()), gaming_map=MAP, origin="fixture").run()
    http = fake_http(routes())
    result = GamingStateConnector(seeded, http=http, gaming_map=MAP, origin="fixture").run()
    assert result.signals == 3
    pdfs = [u for u in http.calls if u.endswith("language_id=1")]
    assert pdfs == [URLS[ym(2026, 2)]]  # only the latest month is re-downloaded


def test_blocked_listing_is_unavailable(seeded, fake_http):
    blocked = (403, b"<html><h1>403 ERROR</h1>Request blocked. Generated by cloudfront (CloudFront)</html>")
    result = GamingStateConnector(seeded, http=fake_http({CURRENT_PAGE: blocked}), gaming_map=MAP).run()
    assert result.status == "unavailable" and "403" in result.errors[0]
