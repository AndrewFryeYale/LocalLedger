from datetime import date

import pytest

from localledger.compliance import (
    DISCLOSURE_MARKER,
    Holdings,
    PublishBlocked,
    blackout_reminders,
    check_publish,
    disclosures,
    lint_impersonal,
    load_holdings,
)


def test_publish_blocked_for_held_ticker_without_disclosure():
    h = Holdings(policy="p", blackout_days=3, tickers={"PCYO"})
    with pytest.raises(PublishBlocked):
        check_publish(["PCYO"], disclosure_flag=False, holdings=h)
    check_publish(["PCYO"], disclosure_flag=True, holdings=h)  # disclosed: allowed
    check_publish(["TRC"], disclosure_flag=False, holdings=h)  # not held: allowed


def test_holdings_file_and_blackout(tmp_path):
    path = tmp_path / "holdings.yaml"
    path.write_text(
        "policy: 'No trades {blackout_days} days around publication.'\nblackout_days: 5\n"
        "positions:\n  - {ticker: mlp, since: 2026-01-01}\n"
    )
    h = load_holdings(path)
    assert h.tickers == {"MLP"} and h.blackout_days == 5 and "5 days" in h.policy
    with pytest.raises(PublishBlocked):
        check_publish(["MLP"], holdings=h)
    reminders = blackout_reminders(["MLP"], date(2026, 10, 17), holdings=h)
    assert reminders == [
        "Trading blackout: no personal trades in MLP from 2026-10-12 to 2026-10-22 (publication 2026-10-17)."
    ]


def test_disclosures_cover_required_points():
    text = " ".join(disclosures()).lower()
    assert DISCLOSURE_MARKER.lower() in text
    for phrase in ("never paid for by any issuer", "not investment advice", "holdings policy", "data may be wrong"):
        assert phrase in text


def test_impersonal_lint():
    assert lint_impersonal("Builders buy lots; the utility sells water.") == []
    assert "price target" in lint_impersonal("Our price target is $20.")
