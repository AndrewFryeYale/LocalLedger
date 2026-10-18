"""Backtest building blocks: point-in-time history, event returns, call-report income, summaries."""

import io
import zipfile
from datetime import date, timedelta

from localledger.backtest import _returns, _ri_from_zip, as_of, render_summary, summarize_forecast
from localledger.modeling.history import History, Period


def test_history_as_of_hides_quarters_not_yet_public():
    h = History(ticker="X")
    h.quarters["revenue"] = [
        Period(date(2026, 1, 1), date(2026, 3, 31), 10, "a", "2026-05-10"),
        Period(date(2026, 4, 1), date(2026, 6, 30), 11, "b", "2026-08-12"),
        Period(date(2025, 10, 1), date(2025, 12, 31), 9, "c", "2027-03-01"),  # value from a later filing
    ]
    seen = as_of(h, date(2026, 7, 1)).series("revenue")
    assert [p.accn for p in seen] == ["a", "c"]  # c was public by its original deadline (end + 95 days)


def test_returns_are_abnormal_vs_benchmark_from_the_close_before():
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(30)]
    stock = {d: 100.0 * (0.99**i) for i, d in enumerate(days)}
    bench = {d: 100.0 for d in days}
    r = _returns(stock, bench, days[5])
    assert round(r["r1"], 4) == round(0.99 - 1, 4) and r["ar5"] < 0 and "ar60" not in r


def test_call_report_income_is_read_for_mapped_banks_only():
    header = "IDRSSD\tRIAD4107\tRIAD4079\tRIAD4340\n"
    rows = header + "desc\tTotal interest income\tx\ty\n123\t5000\t100\t50\n999\t1\t1\t1\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("FFIEC CDR Call Schedule RI 06302026.txt", rows)
    period, out = _ri_from_zip(buf.getvalue(), {"123"})
    assert period == "06302026" and out == {
        "123": {"interest_income": 5e6, "noninterest_income": 1e5, "net_income": 5e4}
    }


def test_forecast_summary_and_markdown():
    rows = [
        {
            "ticker": "A",
            "archetype": "Bank",
            "ape_model": 0.05,
            "ape_seasonal_naive": 0.10,
            "ape_last_quarter": 0.08,
            "in_band": True,
            "p_yoy": 0.8,
            "up_yoy": True,
        },
        {
            "ticker": "B",
            "archetype": "Bank",
            "ape_model": 0.20,
            "ape_seasonal_naive": 0.10,
            "ape_last_quarter": 0.30,
            "in_band": False,
            "p_yoy": 0.3,
            "up_yoy": False,
        },
    ]
    s = summarize_forecast(rows)
    assert s["p10_p90_coverage"] == 0.5 and s["model_beats_both_naive_share"] == 0.5
    assert s["brier_p_up_yoy"] < s["brier_base_rate"]
    empty = {
        "rows": [],
        "summary": {
            k: None
            for k in (
                "bank_quarters",
                "banks",
                "median_lead_days_vs_10q",
                "median_lead_days_vs_10q_conservative",
                "call_report_first_share",
                "call_report_first_conservative_share",
                "median_lead_days_when_first",
                "median_lead_days_when_first_conservative",
                "median_ape_nowcast_when_first",
                "median_ape_model",
                "median_ape_nowcast",
                "nowcast_better_share",
                "direction_hit_rate",
            )
        }
        | {"quarters": []},
    }
    md = render_summary(
        {
            "as_of": "2026-10-18",
            "forecast": {"summary": s},
            "banks": empty,
            "events": {"summary": {"_all_risk": {"n": 0, "tickers": 0}}},
        }
    )
    assert "Revenue forecasts" in md
