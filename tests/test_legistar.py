import json
import re
from datetime import date

import yaml
from sqlmodel import select

from localledger.connectors.legistar import LegistarConnector, clean_body, load_clients, render_event
from localledger.connectors.watchlist import load_watchlists
from localledger.http import read_document_text
from localledger.models import Document, Signal
from tests.conftest import FIXTURES, ROOT

L = FIXTURES / "legistar"
WATCHLISTS = load_watchlists(ROOT / "config" / "watchlists")
API = "https://webapi.legistar.com/v1"
ITEMS = "/eventitems?AgendaNote=1&MinutesNote=1&Attachments=1"
TODAY = date(2026, 10, 17)
MAUI = {"client": "mauicounty", "name": "Maui County, HI", "tickers": ["MLP"], "status": "ok", "why": "test"}
MARICOPA = {
    "client": "maricopa",
    "name": "City of Maricopa, AZ",
    "tickers": ["GWRS"],
    "mode": "matters",
    "status": "ok",
}


def maui_routes():
    return {
        f"{API}/mauicounty/events?*": L / "mauicounty_events.json",
        f"{API}/mauicounty/events/4837{ITEMS}": L / "mauicounty_eventitems_4837.json",
        f"{API}/mauicounty/events/4925{ITEMS}": L / "mauicounty_eventitems_4925.json",
    }


def test_clean_body_and_render_blocks_are_substrings():
    assert clean_body("Housing and Land Use Committee (2025-2027) ") == "Housing and Land Use Committee"
    event = next(e for e in json.loads((L / "mauicounty_events.json").read_text()) if e["EventId"] == 4837)
    items = json.loads((L / "mauicounty_eventitems_4837.json").read_text())
    text, spans = render_event({"place": "Maui County, HI", "event": event}, items)
    assert text.startswith("Maui County, HI: Disaster Recovery, International Affairs, and Planning Committee\n")
    assert spans and all(text[s:e].startswith("[Item ") for s, e, _ in spans)
    assert "\r" not in text and "\n\n" not in "".join(text[s:e] for s, e, _ in spans)


def test_events_mode_signal_from_fixtures(seeded, fake_http):
    http = fake_http(maui_routes())
    conn = LegistarConnector(seeded, http=http, clients=[MAUI], watchlists=WATCHLISTS, today=TODAY, origin="fixture")
    result = conn.run()
    assert result.status == "available", result.errors
    assert result.documents == 2 and result.signals == 1
    sig = seeded.exec(select(Signal).where(Signal.source_id == "legistar")).one()
    assert sig.ticker == "MLP" and sig.metric == "agenda_match" and sig.value == 1
    assert sig.as_of == date(2026, 7, 29)
    assert sig.citation_url.startswith("https://mauicounty.legistar.com/MeetingDetail.aspx?LEGID=4837&")
    assert sig.summary.startswith(
        "Disaster Recovery, International Affairs, and Planning Committee, Maui County, HI (2026-07-29): "
        "1 agenda item mentions Maui Land & Pineapple ("
    )
    detail = json.loads(sig.detail_json)
    doc = seeded.get(Document, sig.document_id)
    text = read_document_text(doc)
    assert detail["excerpt"] in text
    assert "ACQUISITION OF MAUI LAND & PINEAPPLE COMPANY, INC.’S WATER INFRASTRUCTURE ASSETS" in detail["excerpt"]
    assert detail["keywords"][0] == "Maui Land & Pineapple"
    assert detail["agenda_url"].startswith("https://mauicounty.legistar1.com/")
    assert detail["event_id"] == 4837 and detail["event_item_ids"]
    assert 0.5 < sig.significance <= 0.95
    # Never fetched: the robots-disallowed legistar1.com agenda PDF.
    assert not any("legistar1.com" in c for c in http.calls)


def test_old_events_reuse_stored_items(seeded, fake_http):
    LegistarConnector(seeded, http=fake_http(maui_routes()), clients=[MAUI], watchlists=WATCHLISTS, today=TODAY).run()
    http = fake_http(maui_routes())
    result = LegistarConnector(seeded, http=http, clients=[MAUI], watchlists=WATCHLISTS, today=TODAY).run()
    assert result.signals == 1
    assert not any("/events/4837/" in c for c in http.calls)  # 2026-07-29: older than 14 days, unchanged
    assert any("/events/4925/" in c for c in http.calls)  # upcoming meeting: always refreshed


def test_matters_mode_cites_legislation_pages_and_skips_liquor_licenses(seeded, fake_http):
    http = fake_http({f"{API}/maricopa/matters?*": L / "maricopa_matters.json"})
    conn = LegistarConnector(seeded, http=http, clients=[MARICOPA], watchlists=WATCHLISTS, today=TODAY)
    result = conn.run()
    assert result.status == "available", result.errors
    sigs = {s.as_of: s for s in seeded.exec(select(Signal).where(Signal.ticker == "GWRS")).all()}
    assert set(sigs) == {date(2026, 6, 2), date(2024, 12, 3)}  # not the 2026-09-01 gala liquor license
    contract, talk = sigs[date(2024, 12, 3)], sigs[date(2026, 6, 2)]
    assert re.fullmatch(r"https://maricopa\.legistar\.com/gateway\.aspx\?M=L&ID=13805", contract.citation_url)
    assert "Global Water - Santa Cruz Water Company" in json.loads(contract.detail_json)["excerpt"]
    assert talk.summary.endswith("(discussion).")  # "hear a presentation ... Discussion only."
    assert contract.significance > talk.significance
    for s in sigs.values():
        assert json.loads(s.detail_json)["excerpt"] in read_document_text(seeded.get(Document, s.document_id))


def test_token_required_client_is_unavailable(seeded, fake_http):
    nyc = {"client": "nyc", "name": "New York City Council", "tickers": ["CLPR"], "status": "ok"}
    watchlists = {"CLPR": WATCHLISTS["NEN"]}  # any watchlist; the request never gets that far
    http = fake_http({f"{API}/nyc/events?*": (403, b"")})
    result = LegistarConnector(seeded, http=http, clients=[nyc], watchlists=watchlists, today=TODAY).run()
    assert result.status == "unavailable" and "token required" in result.errors[0]


def test_no_enabled_clients_is_unavailable(seeded, fake_http):
    blocked = {**MAUI, "status": "token_required"}
    result = LegistarConnector(seeded, http=fake_http({}), clients=[blocked], watchlists=WATCHLISTS).run()
    assert result.status == "unavailable" and "no enabled Legistar clients" in result.errors[0]


def test_client_registry_is_well_formed(seeded):
    from localledger.models import Company

    clients = load_clients(ROOT / "config" / "legistar_clients.yaml")
    assert len({c["client"] for c in clients}) == len(clients)
    ok = [c for c in clients if c["status"] == "ok"]
    assert len(ok) >= 4
    for c in clients:
        assert c["status"] in {"ok", "token_required", "not_found", "stale"}
        assert c["name"] and c["tickers"] and all(seeded.get(Company, t) for t in c["tickers"])
    for c in ok:
        assert all(t in WATCHLISTS for t in c["tickers"]), c["client"]
    # One line per client so adding a municipality is a one-line edit.
    lines = [x for x in (ROOT / "config" / "legistar_clients.yaml").read_text().splitlines() if x.startswith("  - {")]
    assert len(lines) == len(clients) and yaml.safe_load("\n".join(["clients:", *lines]))["clients"] == clients
