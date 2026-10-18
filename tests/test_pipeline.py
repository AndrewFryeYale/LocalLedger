"""Vertical slice on fixtures: EDGAR -> signals -> analyst note (mock LLM) -> fact-check -> newsletter -> web."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from localledger.compliance import DISCLOSURE_MARKER
from localledger.connectors.edgar import EdgarConnector
from localledger.gtm.newsletter import build_issue, parse_week
from localledger.llm.client import LLM
from localledger.models import NewsletterIssue, Note, PilotRequest, Signal, Subscriber
from localledger.research.analyst import run_research
from tests.test_edgar import routes


@pytest.fixture()
def ingested(seeded, fake_http):
    EdgarConnector(seeded, http=fake_http(routes()), today=date(2026, 10, 17), origin="fixture").run(tickers=["PCYO"])
    return seeded


def test_parse_week():
    assert parse_week("2026-W42")[0] == "2026-W42"
    week, start, end = parse_week("2026-42")
    assert week == "2026-W42" and start == date(2026, 10, 12) and end == date(2026, 10, 18)


def test_notes_are_cited_and_fact_checked(ingested):
    summary = run_research(ingested, LLM("test"), limit=5, threshold=0.0)
    assert summary.attempted >= 2
    notes = ingested.exec(select(Note)).all()
    assert notes and any(n.factcheck_passed for n in notes)
    for n in notes:
        assert "Sources:" in n.body_md


def test_newsletter_from_signals_has_disclosures(ingested):
    result = build_issue(ingested, "2026-W42", LLM("test"))
    text = result.path_md.read_text()
    assert result.items >= 1
    assert DISCLOSURE_MARKER in text and "not investment advice" in text.lower()
    assert "## This week's signals" in text and "Methodology" in text
    html = result.path_html.read_text()
    assert '<table role="presentation"' in html and "style=" in html
    issue = ingested.exec(select(NewsletterIssue)).one()
    assert issue.week == "2026-W42"


def test_web_pages_and_forms(ingested):
    from localledger.web.app import create_app

    build_issue(ingested, "2026-W42", LLM("test"))
    client = TestClient(create_app())
    for path in ("/", "/sample", "/methodology", "/pricing", "/disclosures"):
        r = client.get(path)
        assert r.status_code == 200 and DISCLOSURE_MARKER in r.text, path
    assert client.get("/app/signals", follow_redirects=False).status_code == 303  # gated
    client.post("/waitlist", data={"email": "pm@fund.com", "firm": "Fund"})
    client.post("/pilot-request", data={"name": "P M", "email": "pm@fund.com", "fund": "Fund LP"})
    assert ingested.exec(select(Subscriber)).one().email == "pm@fund.com"
    assert ingested.exec(select(PilotRequest)).one().fund == "Fund LP"
    r = client.post("/login", data={"passcode": "wrong", "name": "x"})
    assert "Wrong passcode" in r.text
    client.post("/login", data={"passcode": "ledger-admin", "name": "Andrew"})
    for path in (
        "/app/signals",
        "/app/universe",
        "/app/company/PCYO",
        "/admin",
        "/app/backtest",
        "/app/costs",
        "/app/safety",
        "/how-it-works",
    ):
        assert client.get(path).status_code == 200, path
    csv_text = client.get("/admin/export/waitlist.csv").text
    assert "pm@fund.com" in csv_text
    sig = ingested.exec(select(Signal)).first()
    assert sig.citation_url in client.get(f"/app/company/{sig.ticker}").text
