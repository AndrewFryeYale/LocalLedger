"""Company models: history extraction, forecast probabilities, valuation, signal impacts, LLM updater bounds."""

import json
from dataclasses import dataclass
from datetime import date

import pytest
from sqlmodel import select

from localledger.compliance import lint_impersonal
from localledger.modeling.engine import (
    apply_valuation,
    build_base,
    clean_series,
    quarter_label,
    value,
)
from localledger.modeling.history import History, Period, annual_as_quarters, parse_companyfacts, quarterly
from localledger.modeling.impacts import apply_adjustments, compute_impact, llm_adjustments, rule
from tests.conftest import FIXTURES

TODAY = date(2026, 10, 17)
CF = json.loads((FIXTURES / "modeling" / "companyfacts_PCYO.json").read_text())


def row(start, end, val, form="10-Q", filed="2026-01-01"):
    return {"start": start, "end": end, "val": val, "form": form, "filed": filed, "accn": "a"}


def test_quarter_derivation_from_ytd_and_annual():
    rows = [
        row("2025-01-01", "2025-03-31", 10),
        row("2025-01-01", "2025-06-30", 25),
        row("2025-01-01", "2025-09-30", 45),
        row("2025-01-01", "2025-12-31", 70, "10-K"),
    ]
    qs = quarterly(rows)
    assert [round(q.value) for q in qs] == [10, 15, 20, 25]
    assert [q.derived for q in qs] == [False, True, True, True]
    assert len(annual_as_quarters([row("2025-01-01", "2025-12-31", 80, "40-F")])) == 4


def test_parse_real_companyfacts_fiscal_quarters():
    h = parse_companyfacts("PCYO", CF, "Regulated utility")
    rev = h.series("revenue")
    assert h.shares == 24097370
    assert rev[-1].end == date(2026, 5, 31) and round(rev[-1].value) == 8_222_000
    q4 = next(q for q in rev if q.end == date(2025, 8, 31))
    assert q4.derived  # fiscal Q4 = FY - 9M YTD
    assert "equity" in h.instants and "cash" in h.instants


def test_outlier_quarters_are_dropped():
    ps = [
        Period(date(2024, m, 1), date(2024, m, 28), v, "a", "f")
        for m, v in zip(range(1, 10), [70, 72, 1.5, 74, 75, 73, 76, 77, 75], strict=True)
    ]
    kept, dropped = clean_series(ps)
    assert len(kept) == 8 and len(dropped) == 1


@dataclass
class Co:
    ticker: str = "PCYO"
    name: str = "Pure Cycle"
    archetype: str = "Regulated utility"
    mkt_cap_musd: float = 266.0


def base_model(overlay=None, archetype="Regulated utility"):
    h = parse_companyfacts("PCYO", CF, archetype)
    return build_base(Co(archetype=archetype), h, overlay or {}, TODAY)


def test_forecast_quarters_probabilities_and_determinism():
    m = base_model()
    vis = m.visible_quarters()
    assert [q.status for q in vis] == ["unreported", "current", "next"]
    assert vis[1].label == quarter_label(date(2026, 11, 30))
    for q in vis:
        assert q.p10 <= q.p50 <= q.p90 and 0 <= q.p_yoy <= 1
    assert [q.p50 for q in base_model().visible_quarters()] == [q.p50 for q in vis]  # seeded: reproducible


def test_events_add_expected_revenue_by_quarter():
    plain = base_model()
    with_event = base_model({"events": [{"name": "land sale", "amount": 20_000_000, "probs": {"current": 0.9}}]})
    cur_plain = next(q for q in plain.forecast if q.status == "current")
    cur_event = next(q for q in with_event.forecast if q.status == "current")
    assert cur_event.mean - cur_plain.mean == pytest.approx(18_000_000, rel=0.08)
    assert cur_event.p_yoy > cur_plain.p_yoy


def test_valuation_methods_and_ordering():
    m = base_model()
    stats = apply_valuation([m], {})
    v = m.valuation
    assert v["method"] in ("ev_ebitda", "ev_revenue") and v["multiples"]["peers"] == "default range"
    assert v["per_share"]["low"] <= v["per_share"]["base"] <= v["per_share"]["high"]
    bank = base_model(archetype="Community bank")
    bv = value(bank, stats)
    assert bv["method"] == "p_tbv" and bv["per_share"]["low"] <= bv["per_share"]["base"] <= bv["per_share"]["high"]
    nav = value(m, stats, {"valuation": {"method": "nav", "nav_per_share": 12.3, "source": "10-Q"}})
    assert nav["per_share"]["base"] == 12.3


def test_stale_history_has_no_revenue_forecast():
    h = History(ticker="GYRO")
    h.quarters["revenue"] = [Period(date(2019, m, 1), date(2019, m, 28), 1e6, "a", "f") for m in range(1, 10, 3)]
    m = build_base(
        Co(ticker="GYRO", archetype="Land / entitlement developer"),
        h,
        {"events": [{"name": "approval", "kind": "milestone", "keywords": ["Flowerfield"], "probs": {"current": 0.3}}]},
        TODAY,
    )
    assert m.status == "stale" and all(q.p50 == 0 for q in m.forecast)
    assert sum(m.events[0].probs.values()) == pytest.approx(0.3)  # relative key resolved to a real quarter


@dataclass
class Sig:
    id: int
    metric: str
    source_id: str
    as_of: date
    value: float | None = None
    prior_value: float | None = None
    period: str | None = None
    summary: str = "summary"
    detail_json: str = "{}"


def test_bank_loan_growth_rule_moves_future_quarters_only():
    m = base_model(archetype="Community bank")
    apply_valuation([m], {})
    s = Sig(1, "net_loans_qoq", "ffiec", date(2026, 6, 30))
    adj, method, why = rule(m, s, {"pct_change": 0.10})
    assert method == "rule:loans" and adj
    after, impact = compute_impact(m, adj, {}, None, why, method)
    moved = [q for q in impact["quarters"] if q["p50_after"] > q["p50_before"]]
    assert moved and impact["sensitivity"]["per_share"] > 0
    assert "Why:" in impact["summary_public"] and "model value" not in impact["summary_public"]  # impact only
    assert lint_impersonal(impact["summary_public"]) == []


def test_credit_rule_raises_risk_premium_and_lowers_value():
    m = base_model(archetype="Community bank")
    apply_valuation([m], {})
    s = Sig(2, "noncurrent_loans", "ffiec", date(2026, 6, 30))
    adj, method, why = rule(m, s, {"change_pts": 1.6, "noncurrent_usd": 2.4e6, "noncurrent_pct_change": 7.5})
    after, impact = compute_impact(m, adj, {}, None, why, method)
    assert impact["risk_after"] == pytest.approx(0.01)
    assert impact["fair_value"]["after"] < impact["fair_value"]["before"]


def test_gaming_month_lands_in_the_quarter_containing_it():
    m = base_model()
    s = Sig(
        3,
        "gaming_agr_month_yoy",
        "gaming_state",
        date(2026, 10, 1),
        3_600_000,
        3_200_000,
        period="in:rising-star:2026-09",
    )
    adj, method, _ = rule(m, s, {"property": "Rising Star Casino"})
    assert method == "rule:gaming" and len(adj[0].quarters) == 1
    q = m.quarter_containing(date(2026, 9, 15))
    assert adj[0].quarters == [q.end]


def test_agenda_approval_shifts_event_into_a_future_quarter():
    m = base_model(
        {
            "events": [
                {
                    "name": "Phase 2E",
                    "kind": "milestone",
                    "keywords": ["Sky Ranch"],
                    "probs": {"current": 0.3, "next": 0.4},
                }
            ]
        }
    )
    s = Sig(4, "agenda_match", "legistar", date(2026, 8, 1))
    detail = {
        "keywords": ["Sky Ranch"],
        "items": [{"label": "approval"}],
        "meeting_date": "2026-08-01",
        "excerpt": "Sky Ranch Filing 7 easement vacation. Adopted.",
    }
    adj, method, _ = rule(m, s, detail)
    after = apply_adjustments(m, adj)
    cur = next(q.end for q in after.forecast if q.status == "current")
    probs = after.events[0].probs
    assert probs[cur] > 0.3 and sum(probs.values()) <= 1.0 + 1e-9
    assert all(date.fromisoformat(k) >= TODAY or v == 0 for k, v in probs.items() if v > 0)


def test_regained_compliance_lowers_risk():
    m = base_model()
    s = Sig(5, "8k_item_3.01", "edgar", date(2026, 9, 16), summary="received a listing notice")
    text = "The Company regained compliance with the continued listing standards."
    adj, method, _ = rule(m, s, {}, text=text)
    assert adj == [] and method == "note"  # nothing to remove when no listing-risk premium is applied
    m.risk_premium = 0.01
    adj, method, _ = rule(m, s, {}, text=text)
    assert adj[0].kind == "risk" and adj[0].value < 0


class FakeLLM:
    def __init__(self, data):
        self.data = data

    def json(self, name, variables):
        from localledger.llm.client import LLMResult

        return LLMResult(data=self.data, text="", model="fake", provider="mock")


def test_llm_updater_requires_verbatim_quotes_and_bounds():
    m = base_model()
    key = m.visible_quarters()[1].end
    doc = "The Company sold its Lot 2-D parcel for $10,000,000 and closed the sale on October 3, 2026."
    data = {
        "adjustments": [
            {
                "kind": "factor",
                "quarters": [key],
                "factor": 1.25,
                "rationale": "r",
                "quote": "sold its Lot 2-D parcel for $10,000,000",
            },
            {
                "kind": "factor",
                "quarters": [key],
                "factor": 3.0,
                "rationale": "too big",
                "quote": "sold its Lot 2-D parcel for $10,000,000",
            },
            {
                "kind": "add",
                "quarters": [key],
                "amount_usd": 1e6,
                "rationale": "invented",
                "quote": "a quote that is not there",
            },
        ],
        "so_what": "Adds to the current quarter.",
    }
    adj, why = llm_adjustments(FakeLLM(data), m, Sig(6, "8k_item_2.01", "edgar", TODAY), doc, "", "https://x", "LL")
    assert len(adj) == 1 and adj[0].value == 1.25
    assert "verbatim" in why


def test_build_models_end_to_end(seeded, fake_http):
    from localledger.connectors.base import ensure_source
    from localledger.connectors.edgar import EdgarConnector
    from localledger.http import DocStore
    from localledger.llm.client import LLM
    from localledger.modeling.history import COMPANYFACTS_URL
    from localledger.modeling.store import build_models, load_model
    from localledger.models import Company, ModelImpact
    from localledger.signals.store import SignalDraft, make_signal

    pcyo = seeded.get(Company, "PCYO")
    pcyo.cik = "276720"
    seeded.add(pcyo)
    url = COMPANYFACTS_URL.format(cik="276720")
    http = fake_http({url: (FIXTURES / "modeling" / "companyfacts_PCYO.json")})
    ensure_source(seeded, EdgarConnector)
    doc = DocStore(seeded).save(
        source_id="edgar",
        url="https://www.sec.gov/x",
        content=b"filing text",
        content_type="text/plain",
        origin="fixture",
    )
    make_signal(
        seeded,
        "edgar",
        SignalDraft(
            ticker="PCYO",
            metric="8k_item_4.02",
            as_of=date(2026, 9, 1),
            summary="non-reliance",
            citation_url="https://www.sec.gov/x",
            document_id=doc.id,
            significance=0.9,
        ),
    )
    seeded.commit()
    from unittest.mock import patch

    with patch("localledger.modeling.store.HttpClient", return_value=http):
        result = build_models(seeded, tickers=["PCYO"], llm=LLM("test"), today=TODAY)
    assert result.models == 237 and result.modeled >= 1
    m = load_model(seeded, "PCYO")
    assert m.status == "modeled" and m.risk_premium == pytest.approx(0.015)
    imp = seeded.exec(select(ModelImpact)).one()
    assert imp.changed and "risk premium" in imp.summary_public
