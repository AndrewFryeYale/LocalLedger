from datetime import date

import pytest

from localledger.connectors.watchlist import (
    Watchlist,
    agenda_signal,
    classify_action,
    event_significance,
    load_watchlists,
    match,
    match_items,
)
from tests.conftest import FIXTURES, ROOT

WATCHLISTS = load_watchlists(ROOT / "config" / "watchlists")
P = FIXTURES / "pdf_agenda"


def wl(**sections) -> Watchlist:
    return Watchlist.from_dict("TEST", sections)


def terms(text: str, watchlist: Watchlist) -> list[str]:
    return [m.term for m in match(text, watchlist)]


def test_whole_word_and_case_insensitive():
    w = wl(projects=["Sky Ranch"])
    assert terms("Final plat for SKY RANCH Filing 4", w) == ["Sky Ranch"]
    assert terms("Sky Rancher Lane and skyranch.com", w) == []


def test_accents_okina_and_apostrophes_are_ignored():
    w = wl(projects=["Kaanapali", "Hali'imaile"])
    text = "A. KĀʻANAPALI 2020 plan\nB. Hāliʻimaile Ranch; Hali‘imaile Farms; Haliimaile"
    found = match(text, w)
    assert [m.matched for m in found] == ["KĀʻANAPALI", "Hāliʻimaile", "Hali‘imaile", "Haliimaile"]


def test_phrase_may_wrap_lines_and_street_suffixes_abbreviate():
    w = wl(projects=["Flowerfield Subdivision"], parcels=["62 Boylston Street"])
    text = "approval of the Flowerfield\nSubdivision; ZBA appeal at 62 Boylston St. (not 62 Boylston Sta)"
    assert [m.matched for m in match(text, w)] == ["Flowerfield\nSubdivision", "62 Boylston St."]


def test_longest_term_wins_overlaps_and_weights_follow_category():
    w = wl(projects=["Kapalua Mauka"], company=["Kapalua"], generic=["Mauka"])
    found = match("Kapalua Mauka phase 2", w)
    assert [(m.term, m.category, m.weight) for m in found] == [("Kapalua Mauka", "project", 1.0)]
    w = wl(company=["Pure Cycle"], parcels=["Lowry Ranch"], generic=["Watkins"])
    weights = {m.term: m.weight for m in match("Pure Cycle water on Lowry Ranch near Watkins", w)}
    assert weights["Lowry Ranch"] > weights["Pure Cycle"] > weights["Watkins"]


def test_snippet_is_exact_substring_and_bounded():
    text = ("Lorem ipsum dolor sit amet " * 40) + "Gyrodyne LLC requests approval. " + ("consectetur adipiscing " * 40)
    (m,) = match(text, WATCHLISTS["GYRO"])
    assert text[m.snippet_start : m.snippet_end] == m.snippet
    assert m.start - m.snippet_start <= 300 and m.snippet_end - m.end <= 300
    assert "Gyrodyne LLC requests approval." in m.snippet


def test_items_stop_at_the_next_agenda_item():
    text = "A. Kapalua resort water rate case\nB. Lahaina recovery update\n\nC. West Maui only"
    items = match_items(text, WATCHLISTS["MLP"])
    assert [i.text for i in items] == ["A. Kapalua resort water rate case"]


def test_generic_place_names_never_trigger_alone():
    # Real Smithtown Town Board agenda, 2026-10-06: "Community Association of St. James Halloween Parade".
    text = (P / "smithtown_tb_2026-10-06_p1.txt").read_text()
    assert "St. James" in [m.term for m in match(text, WATCHLISTS["GYRO"])]
    assert match_items(text, WATCHLISTS["GYRO"]) == []


def test_exclude_phrase_drops_letterhead_address():
    # Real Town of Cortlandt agenda letterhead: "1 Heady Street ... Cortlandt Manor, NY 10567".
    text = (P / "cortlandt_tb_2026-10-20_p2.txt").read_text()
    assert "Cortlandt Manor, NY" in text
    assert match(text, WATCHLISTS["GYRO"]) == []
    assert terms("site plan for the Cortlandt Manor medical campus", WATCHLISTS["GYRO"]) == ["Cortlandt Manor"]


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("Public hearing: Canterbury Park card casino expansion", "Canterbury Park"),
        ("Canterbury racetrack TIF district amendment", "Canterbury"),
        ("Street reconstruction on Canterbury Road and Canterbury Rd. near the park", None),
        ("Canterbury Estates 4th Addition plat", None),  # no track/development word within 80 chars
    ],
)
def test_canterbury_counts_only_near_the_track(text, hit):
    found = terms(text, WATCHLISTS["CPHC"])
    assert (found[0] if found else None) == hit


def test_term_level_exclude_keeps_other_terms():
    w = WATCHLISTS["STHO"]
    assert terms("Sale To The Asbury Park Housing Authority By Quit Claim Deed", w) == []
    assert terms("Liquor license transfer for The Asbury hotel rooftop", w) == ["The Asbury"]


def test_exclude_items_drops_whole_item():
    text = (
        "SPEVLIQ 26-10 Special Event Liquor License application for a gala to be held on December 5, 2026 at "
        "Global Water Resources, 22590 N. Powers Parkway, Maricopa, Arizona 85138."
    )
    assert terms(text, WATCHLISTS["GWRS"]) == ["Global Water Resources"]
    assert match_items(text, WATCHLISTS["GWRS"]) == []


def test_classify_action_ranks_item_types():
    assert classify_action("Public Hearing to consider Proposed Local Law #16-2026") == ("public hearing", 1.0)
    assert classify_action("BILL 141 (2026), CHANGE IN ZONING")[1] == 1.0
    assert classify_action("Presentation from Water Supply")[0] == "presentation"
    hearing = match_items("1. Public hearing on the Sky Ranch rezoning", WATCHLISTS["PCYO"])
    talk = match_items("1. Presentation: Pure Cycle community update", WATCHLISTS["PCYO"])
    assert event_significance(hearing) > event_significance(talk) > 0.15


def test_agenda_signal_shape():
    text = "Planning Board\n\nI. PUBLIC HEARINGS\n1. #1178 Gyrodyne LLC, St. James\nRequest: extend approval\n"
    items = match_items(text, WATCHLISTS["GYRO"])
    draft = agenda_signal(
        ticker="GYRO",
        items=items,
        text=text,
        place="Town of Smithtown, NY",
        body="Planning Board",
        meeting_date=date(2026, 10, 7),
        citation_url="https://example.org/agenda.pdf",
        document_id=1,
        period="smithtown-ny:2026-10-07:Planning Board",
    )
    assert draft.metric == "agenda_match" and draft.value == 1.0
    assert (
        draft.summary
        == "Planning Board, Town of Smithtown, NY (2026-10-07): 1 agenda item mentions Gyrodyne (public hearing)."
    )
    assert draft.detail["excerpt"] in text and "Gyrodyne LLC, St. James" in draft.detail["excerpt"]
    assert draft.detail["keywords"] == ["Gyrodyne", "St. James"]
    assert draft.detail["meeting_date"] == "2026-10-07" and draft.detail["body"] == "Planning Board"


def test_seeded_watchlists_load_and_map_to_universe(seeded):
    from localledger.models import Company

    for ticker in ("TRC", "MLP", "CPHC", "GYRO", "STHO"):
        assert ticker in WATCHLISTS
    for ticker, w in WATCHLISTS.items():
        assert seeded.get(Company, ticker) is not None, ticker
        assert w.terms and all(t.pattern is not None for t in w.terms)
        assert any(t.category != "generic" for t in w.terms)
