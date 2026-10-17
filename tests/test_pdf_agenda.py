import json
import re
from datetime import date

import yaml
from sqlmodel import select

from localledger.connectors.pdf_agenda import (
    PdfAgendaConnector,
    clean_url,
    find_links,
    load_towns,
    parse_date,
    select_recent,
)
from localledger.connectors.watchlist import load_watchlists
from localledger.http import RobotsDisallowed, read_document_text
from localledger.models import Document, Signal
from tests.conftest import FIXTURES, ROOT

P = FIXTURES / "pdf_agenda"
WATCHLISTS = load_watchlists(ROOT / "config" / "watchlists")
TOWNS = {t["id"]: t for t in load_towns(ROOT / "config" / "agenda_sources.yaml")}
SMITHTOWN_API = "https://smithtownny.api.civicclerk.com/v1"
SMITHTOWN_FILE = SMITHTOWN_API + "/Meetings/GetMeetingFileStream(fileId={},plainText=false)"
AP_INDEX = "https://www.cityofasburypark.com/AgendaCenter/Planning-Board-9"
AP_FILE = "https://www.cityofasburypark.com/AgendaCenter/ViewFile/Agenda/_08032026-1313"


def test_parse_date_formats():
    assert parse_date("Town Board Meeting, 10/20/2026, 7:00 PM Agenda") == date(2026, 10, 20)
    assert parse_date("Planning Board Regular Meeting Agenda 10/5/26") == date(2026, 10, 5)
    assert parse_date("October 05, 2026, Planning Board Regular Meeting Agenda") == date(2026, 10, 5)
    assert parse_date("/uppages/09_01_2026 PB Agenda - FINAL.pdf") == date(2026, 9, 1)
    assert parse_date("/AgendaCenter/ViewFile/Agenda/_08032026-1313") == date(2026, 8, 3)
    assert parse_date("2026-10-06T14:00:00Z") == date(2026, 10, 6)
    assert parse_date("AGENDA-OCT26-FOR POSTING.pdf") is None


def test_clean_url_resolves_encodes_and_drops_default_port():
    base = "https://www.townofcortlandtny.gov/cn/meetings/"
    assert clean_url("/uppages/AGENDA-OCT26-FOR POSTING.pdf", base) == (
        "https://www.townofcortlandtny.gov/uppages/AGENDA-OCT26-FOR%20POSTING.pdf"
    )
    assert clean_url("https://assets-us-01.kc-usercontent.com:443/x/Agenda%20102026_links.pdf", base) == (
        "https://assets-us-01.kc-usercontent.com/x/Agenda%20102026_links.pdf"
    )


def test_find_links_on_real_index_pages():
    cortlandt = find_links(
        (P / "cortlandt_meetings.html").read_bytes(),
        TOWNS["cortlandt-ny"]["index_url"],
        TOWNS["cortlandt-ny"]["link_pattern"],
    )
    assert [(x["date"], x["url"].rsplit("/", 1)[-1]) for x in cortlandt] == [
        (date(2026, 10, 20), "AGENDA-OCT26-FOR%20POSTING.pdf"),
        (date(2026, 9, 15), "AGENDA-SEPT26.pdf"),
        (date(2026, 6, 9), "AGENDA-JUNE26-FOR%20POSTING.pdf"),
    ]  # Planning Board packets (40-53 MB) and work-session pages don't match the Town Board pattern
    asbury = find_links(
        (P / "asburypark_pb_index.html").read_bytes(), AP_INDEX, TOWNS["asbury-park-nj-planning"]["link_pattern"]
    )
    assert len(asbury) == len({x["url"] for x in asbury}) == 8  # each agenda is linked twice on the page
    assert asbury[0]["date"] == date(2026, 10, 19) and asbury[0]["text"] == "Planning Board Regular Meeting Agenda"
    la = find_links(
        (P / "labos_index.html").read_bytes(),
        TOWNS["la-county-bos"]["index_url"],
        TOWNS["la-county-bos"]["link_pattern"],
    )
    assert [x["date"] for x in la] == [date(2026, 10, 20), date(2026, 10, 6), date(2026, 9, 29), date(2026, 9, 22)]
    assert all(x["url"].startswith("https://assets-us-01.kc-usercontent.com/0234f496") for x in la)


def test_select_recent_window_cap_and_undated_fallback():
    links = [{"url": str(i), "date": date(2026, 10, 1 + i)} for i in range(10)] + [
        {"url": "old", "date": date(2025, 1, 1)}
    ]
    chosen = select_recent(links, date(2026, 10, 17), 75, 6)
    assert [x["url"] for x in chosen] == ["9", "8", "7", "6", "5", "4"]
    undated = [{"url": str(i), "date": None} for i in range(9)]
    assert [x["url"] for x in select_recent(undated, date(2026, 10, 17), 75, 6)] == ["0", "1", "2", "3", "4", "5"]


def smithtown_routes():
    return {
        f"{SMITHTOWN_API}/Events?*": P / "smithtown_events.json",
        SMITHTOWN_FILE.format(11582): P / "smithtown_pb_2026-10-07.pdf",
        SMITHTOWN_FILE.format(11566): (404, b"not found"),
    }


def test_civicclerk_town_end_to_end(seeded, fake_http):
    http = fake_http(smithtown_routes())
    conn = PdfAgendaConnector(
        seeded,
        http=http,
        towns=[TOWNS["smithtown-ny"]],
        watchlists=WATCHLISTS,
        today=date(2026, 10, 17),
        origin="fixture",
    )
    result = conn.run()
    assert result.documents == 1 and result.signals == 1, result.errors
    assert not any("fileId=11579" in c for c in http.calls)  # Board of Zoning Appeals: not in `bodies`
    sig = seeded.exec(select(Signal).where(Signal.source_id == "pdf_agenda")).one()
    assert sig.ticker == "GYRO" and sig.metric == "agenda_match" and sig.as_of == date(2026, 10, 7)
    assert (
        sig.summary
        == "Planning Board, Town of Smithtown, NY (2026-10-07): 1 agenda item mentions Gyrodyne (public hearing)."
    )
    assert sig.citation_url == SMITHTOWN_FILE.format(11582)
    assert sig.significance == 0.95
    detail = json.loads(sig.detail_json)
    doc = seeded.get(Document, sig.document_id)
    assert doc.content_type == "application/pdf" and detail["excerpt"] in read_document_text(doc)
    assert "#1178 Gyrodyne LLC, St. James" in detail["excerpt"]
    assert detail["meeting_page"] == "https://smithtownny.portal.civicclerk.com/event/4463/files/agenda/11582"


def test_index_page_town_end_to_end(seeded, fake_http):
    http = fake_http({AP_INDEX: P / "asburypark_pb_index.html", AP_FILE: P / "asburypark_pb_2026-08-03.pdf"})
    town = TOWNS["asbury-park-nj-planning"]
    conn = PdfAgendaConnector(seeded, http=http, towns=[town], watchlists=WATCHLISTS, today=date(2026, 8, 10))
    result = conn.run()
    assert result.signals == 1, result.errors
    sig = seeded.exec(select(Signal).where(Signal.ticker == "STHO")).one()
    assert sig.citation_url == AP_FILE and sig.as_of == date(2026, 8, 3)
    assert "Asbury Partners LLC" in json.loads(sig.detail_json)["excerpt"]
    assert sig.summary == (
        "Planning Board, City of Asbury Park, NJ (2026-08-03): 1 agenda item mentions "
        "320 Asbury Avenue / Block 3801 / Asbury Partners (agenda item)."
    )
    # Window = 75 days back + 30 ahead: 08-17, 08-03, 07-20, 07-06, 06-15 (newest first, <= max_docs); only
    # 08-03 is routed, the others 404 and are skipped.
    assert sum("/AgendaCenter/ViewFile/" in c for c in http.calls) == 5


def test_explicit_urls_on_real_council_agenda(seeded, fake_http):
    url = "https://asburyparknj.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId=5749,plainText=false)"
    town = {
        "id": "asbury-test",
        "name": "City of Asbury Park, NJ",
        "tickers": ["STHO"],
        "urls": [url],
        "body": "City Council",
    }
    http = fake_http({url: P / "asburypark_cc_2026-10-14_p2.pdf"})
    result = PdfAgendaConnector(seeded, http=http, towns=[town], watchlists=WATCHLISTS, today=date(2026, 10, 17)).run()
    assert result.signals == 1, result.errors
    sig = seeded.exec(select(Signal).where(Signal.ticker == "STHO")).one()
    detail = json.loads(sig.detail_json)
    text = read_document_text(seeded.get(Document, sig.document_id))
    assert [m["term"] for item in detail["items"] for m in item["matched"]] == ["Waterfront Redevelopment Plan"]
    assert "Vacating A Portion Of St. James Place" in detail["excerpt"] and detail["excerpt"] in text
    assert detail["date_known"] is False and re.fullmatch(r"asbury-test:[0-9a-f]{10}:City Council", sig.period)


def test_max_mb_skips_big_files_after_head(seeded, fake_http):
    town = {**TOWNS["asbury-park-nj-planning"], "max_mb": 0.01}
    http = fake_http({AP_INDEX: P / "asburypark_pb_index.html", AP_FILE: P / "asburypark_pb_2026-08-03.pdf"})
    result = PdfAgendaConnector(seeded, http=http, towns=[town], watchlists=WATCHLISTS, today=date(2026, 8, 10)).run()
    assert result.signals == 0 and result.documents == 0
    assert http.calls.count(AP_FILE) == 1  # the HEAD only


def test_failed_downloads_and_robots_degrade_gracefully(seeded, fake_http):
    town = TOWNS["cortlandt-ny"]
    http = fake_http({town["index_url"]: P / "cortlandt_meetings.html"})
    result = PdfAgendaConnector(seeded, http=http, towns=[town], watchlists=WATCHLISTS, today=date(2026, 10, 17)).run()
    assert result.status == "unavailable" and "all 2 agenda downloads failed" in result.errors[0]

    class BlockedHttp:
        def get_ok(self, url, **kw):
            raise RobotsDisallowed(url)

    result = PdfAgendaConnector(seeded, http=BlockedHttp(), towns=[town], watchlists=WATCHLISTS).run()
    assert result.status == "unavailable" and "RobotsDisallowed" in result.errors[0]


def test_blocked_towns_are_never_fetched(seeded, fake_http):
    http = fake_http({})
    result = PdfAgendaConnector(seeded, http=http, towns=[TOWNS["shakopee-mn"]], watchlists=WATCHLISTS).run()
    assert result.status == "unavailable" and http.calls == []


def test_agenda_sources_config_is_one_line_per_town():
    text = (ROOT / "config" / "agenda_sources.yaml").read_text()
    lines = [x for x in text.splitlines() if x.startswith("  - {")]
    towns = load_towns(ROOT / "config" / "agenda_sources.yaml")
    assert len(lines) == len(towns) >= 4 and yaml.safe_load("\n".join(["towns:", *lines]))["towns"] == towns
    for t in towns:
        assert t["id"] and t["name"] and t["tickers"]
        assert t.get("platform") == "civicclerk" or t.get("urls") or (t["index_url"] and re.compile(t["link_pattern"]))
        if t.get("status", "ok") == "ok":
            assert all(x in WATCHLISTS for x in t["tickers"]), t["id"]
