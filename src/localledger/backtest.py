"""Backtest: what would Local Ledger have known, and when, over the last 12 months?

Three studies, all point-in-time and all deterministic (no LLM calls, so the backtest costs $0 in tokens):

1. forecast   Every modeled company, each of the last 4 reported quarters: rebuild the model as of the first day of
              that quarter using only filings available then, and score its revenue forecast against what was later
              reported (error vs naive baselines, P10-P90 coverage, Brier score of P(up y/y)).
2. banks      Call Reports (FFIEC) publish each bank's quarterly interest income ~30 days after quarter end. How much
              earlier than the holding company's 10-Q is that, how often is it the first public number, and how much
              does a call-report nowcast cut forecast error?
3. events     Every 8-K risk item and late-filing notice for the universe in the last 12 months (what the EDGAR
              connector flags the day it's filed), plus agenda matches backfilled 12 months, with the stock's move
              afterwards vs the Russell 2000 (IWM).

`ll backtest` -> out/backtest/results.json + summary.md, and the /app/backtest page.
"""

from __future__ import annotations

import io
import json
import logging
import math
import statistics
import zipfile
from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from sqlmodel import Session, select

from localledger.config import get_settings
from localledger.http import read_document_bytes
from localledger.modeling.engine import build_base, quarter_label
from localledger.modeling.history import History, Period, load_history
from localledger.modeling.store import load_overlay
from localledger.models import Company, Document

log = logging.getLogger(__name__)

AVAILABLE_LAG_DAYS = 95  # a value filed later (e.g. a comparative) was public by its original deadline at the latest
CALL_REPORT_LAG_DAYS = 30  # Call Reports are due 30 days after quarter end (optimistic availability)
CALL_REPORT_LAG_CONSERVATIVE = 45  # what the FFIEC connector waits before using a quarter (CDR posting lag)
RISK_ITEMS = {
    "1.03": "Bankruptcy or receivership",
    "2.04": "Debt acceleration trigger",
    "2.06": "Material impairment",
    "3.01": "Listing deficiency / delisting notice",
    "4.01": "Auditor change",
    "4.02": "Non-reliance on prior financials",
}
LATE_FORMS = {"NT 10-K": "Late 10-K notice", "NT 10-Q": "Late 10-Q notice"}
HORIZONS = (1, 5, 20, 60)


def out_dir() -> Path:
    return get_settings().out_dir / "backtest"


# --- 1. forecast accuracy ----------------------------------------------------------------------------------------
def _available(p: Period) -> date:
    filed = date.fromisoformat(p.filed[:10]) if p.filed else p.end + timedelta(days=AVAILABLE_LAG_DAYS)
    return min(filed, p.end + timedelta(days=AVAILABLE_LAG_DAYS))


def as_of(history: History, day: date) -> History:
    """The history as it stood on `day`: only quarters already public."""
    quarters = {k: [p for p in v if _available(p) < day] for k, v in history.quarters.items()}
    return replace(history, quarters=quarters)


def _ape(forecast: float, actual: float) -> float | None:
    return abs(forecast - actual) / abs(actual) if actual else None


def forecast_study(session: Session, quarters_back: int = 4, today: date | None = None) -> dict:
    today = today or date.today()
    rows = []
    for company in session.exec(select(Company)).all():
        h = load_history(session, company, None)
        if h is None or h.kind != "xbrl":
            continue
        rev = h.series("revenue")
        actual = {p.end: p.value for p in rev}
        overlay = load_overlay(company.ticker)
        overlay = {k: v for k, v in overlay.items() if k != "events"}  # curated events postdate the backtest window
        for target in [p for p in rev if p.end >= today - timedelta(days=380)][-quarters_back:]:
            t0 = target.start
            m = build_base(company, as_of(h, t0), overlay, t0)
            if m.status != "modeled":
                continue
            q = next((q for q in m.forecast if q.end == target.end.isoformat()), None)
            if q is None or not q.p50:
                continue
            past = sorted(e for e in actual if _available(Period(e, e, 0, "", "")) and e < t0)
            prior_year = next((actual[e] for e in actual if abs((target.end - e).days - 365) <= 12), None)
            last_known = [p for p in as_of(h, t0).series("revenue")]
            last = last_known[-1].value if last_known else None
            rows.append(
                {
                    "ticker": company.ticker,
                    "archetype": company.archetype,
                    "quarter": quarter_label(target.end),
                    "made_on": t0.isoformat(),
                    "actual": target.value,
                    "p10": q.p10,
                    "p50": q.p50,
                    "p90": q.p90,
                    "p_yoy": q.p_yoy,
                    "prior_year": prior_year,
                    "last_known": last,
                    "ape_model": _ape(q.p50, target.value),
                    "ape_seasonal_naive": _ape(prior_year, target.value) if prior_year else None,
                    "ape_last_quarter": _ape(last, target.value) if last else None,
                    "in_band": q.p10 <= target.value <= q.p90,
                    "up_yoy": (target.value > prior_year) if prior_year else None,
                    "history_quarters": len(past),
                }
            )
    return {"rows": rows, "summary": summarize_forecast(rows)}


def _median(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None and not math.isnan(x)]
    return statistics.median(xs) if xs else None


def summarize_forecast(rows: list[dict]) -> dict:
    paired = [r for r in rows if r["ape_seasonal_naive"] is not None and r["ape_last_quarter"] is not None]
    brier = [(r["p_yoy"] - float(r["up_yoy"])) ** 2 for r in rows if r["p_yoy"] is not None and r["up_yoy"] is not None]
    base_rate = statistics.mean(float(r["up_yoy"]) for r in rows if r["up_yoy"] is not None) if rows else None
    brier_base = (
        statistics.mean((base_rate - float(r["up_yoy"])) ** 2 for r in rows if r["up_yoy"] is not None)
        if base_rate is not None
        else None
    )
    by_arch = defaultdict(list)
    for r in rows:
        by_arch[r["archetype"]].append(r)
    return {
        "company_quarters": len(rows),
        "companies": len({r["ticker"] for r in rows}),
        "median_ape_model": _median([r["ape_model"] for r in paired]),
        "median_ape_seasonal_naive": _median([r["ape_seasonal_naive"] for r in paired]),
        "median_ape_last_quarter": _median([r["ape_last_quarter"] for r in paired]),
        "model_beats_both_naive_share": (
            sum(r["ape_model"] <= min(r["ape_seasonal_naive"], r["ape_last_quarter"]) for r in paired) / len(paired)
            if paired
            else None
        ),
        "p10_p90_coverage": sum(r["in_band"] for r in rows) / len(rows) if rows else None,
        "brier_p_up_yoy": statistics.mean(brier) if brier else None,
        "brier_base_rate": brier_base,
        "base_rate_up_yoy": base_rate,
        "by_archetype": {
            a: {
                "n": len(rs),
                "median_ape_model": _median([r["ape_model"] for r in rs]),
                "coverage": sum(r["in_band"] for r in rs) / len(rs),
            }
            for a, rs in sorted(by_arch.items(), key=lambda kv: -len(kv[1]))
            if len(rs) >= 8
        },
    }


# --- 2. banks: call report vs 10-Q ------------------------------------------------------------------------------
RI_ITEMS = {"RIAD4107": "interest_income", "RIAD4079": "noninterest_income", "RIAD4340": "net_income"}


def _ri_from_zip(content: bytes, rssds: set[str]) -> tuple[str, dict[str, dict[str, float]]]:
    """Schedule RI (income statement, calendar year-to-date, $ thousands) for the given banks."""
    z = zipfile.ZipFile(io.BytesIO(content))
    name = next(n for n in z.namelist() if n.startswith("FFIEC CDR Call Schedule RI ") and n.endswith(".txt"))
    period = name.rsplit(" ", 1)[1].removesuffix(".txt")
    lines = z.read(name).decode("latin-1").splitlines()
    cols = lines[0].replace('"', "").split("\t")
    idx = {c: i for i, c in enumerate(cols)}
    out: dict[str, dict[str, float]] = {}
    for line in lines[2:]:
        cells = line.replace('"', "").split("\t")
        rssd = cells[idx["IDRSSD"]].strip()
        if rssd not in rssds:
            continue
        vals = {}
        for code, key in RI_ITEMS.items():
            raw = cells[idx[code]].strip() if code in idx and idx[code] < len(cells) else ""
            if raw:
                vals[key] = float(raw) * 1000
        out[rssd] = vals
    return period, out


def _call_report_zips(session: Session, periods: list[str], fetch: bool) -> dict[str, bytes]:
    from localledger.connectors.ffiec import FfiecConnector, zip_doc_url

    found: dict[str, bytes] = {}
    missing = []
    for p in periods:
        doc = session.exec(select(Document).where(Document.url == zip_doc_url(p)).order_by(Document.id.desc())).first()
        if doc is not None:
            found[p] = read_document_bytes(doc)
        else:
            missing.append(p)
    if missing and fetch:
        conn = FfiecConnector(session)
        conn._list_periods()
        conn.periods = (periods[-1], periods[-1], None)  # fetch only what's missing (closed quarters don't change)
        for p in missing:
            if p not in conn._options:
                log.info("CDR doesn't list %s", p)
                continue
            doc = conn._fetch_period(p)
            session.commit()
            found[p] = read_document_bytes(doc)
    return found


def _qend(year: int, q: int) -> date:
    return {1: date(year, 3, 31), 2: date(year, 6, 30), 3: date(year, 9, 30), 4: date(year, 12, 31)}[q]


def _period_str(d: date) -> str:
    return f"{d.month:02d}{d.day:02d}{d.year}"


def _submissions(session: Session, ticker: str) -> list[dict]:
    doc = session.exec(
        select(Document)
        .where(Document.ticker == ticker, Document.source_id == "edgar", Document.title.like("EDGAR submissions%"))
        .order_by(Document.id.desc())
    ).first()
    if doc is None:
        return []
    recent = json.loads(read_document_bytes(doc)).get("filings", {}).get("recent", {})
    return [dict(zip(recent.keys(), v, strict=False)) for v in zip(*recent.values(), strict=False)]


def bank_study(session: Session, today: date | None = None, fetch: bool = True) -> dict:
    from localledger.connectors.ffiec import load_bank_map, resolve_bank_map

    today = today or date.today()
    settings = get_settings()
    bank_map = resolve_bank_map(load_bank_map(settings.config_dir / "bank_map.yaml"))
    rssd_by_ticker = {t: str(e.get("rssd") or e.get("fed_rssd") or "") for t, e in bank_map.items()}
    rssd_by_ticker = {t: r for t, r in rssd_by_ticker.items() if r}
    # 6 quarter-ends back from the latest complete one: YTD differencing needs the quarter before each target
    latest = today - timedelta(days=CALL_REPORT_LAG_DAYS + 15)
    ends = []
    y, q = latest.year, (latest.month - 1) // 3
    if q == 0:
        y, q = y - 1, 4
    for _ in range(6):
        ends.append(_qend(y, q))
        y, q = (y, q - 1) if q > 1 else (y - 1, 4)
    ends.sort()
    zips = _call_report_zips(session, [_period_str(e) for e in ends], fetch)
    ytd: dict[str, dict[date, dict]] = defaultdict(dict)
    rssds = set(rssd_by_ticker.values())
    for p, content in zips.items():
        _, rows = _ri_from_zip(content, rssds)
        e = date(int(p[4:]), int(p[:2]), int(p[2:4]))
        for rssd, vals in rows.items():
            ytd[rssd][e] = vals

    def bank_quarter(rssd: str, e: date) -> float | None:
        cur = ytd[rssd].get(e, {}).get("interest_income")
        if cur is None:
            return None
        if e.month == 3:
            return cur
        prev = ytd[rssd].get(_qend(e.year, e.month // 3 - 1), {}).get("interest_income")
        return cur - prev if prev is not None else None

    rows = []
    for ticker, rssd in sorted(rssd_by_ticker.items()):
        company = session.get(Company, ticker)
        h = load_history(session, company, None) if company else None
        if h is None:
            continue
        hold = {p.end: p for p in h.series("revenue")}
        subs = _submissions(session, ticker)
        overlay = {k: v for k, v in load_overlay(ticker).items() if k != "events"}
        for e in ends[2:]:
            prev_e = ends[ends.index(e) - 1]
            b, b_prev = bank_quarter(rssd, e), bank_quarter(rssd, prev_e)
            actual, actual_prev = hold.get(e), hold.get(prev_e)
            if not (b and b_prev and actual and actual_prev):
                continue
            ratio = actual_prev.value / b_prev
            if not 0.7 <= ratio <= 1.5:  # holding company isn't mostly this bank: comparison isn't meaningful
                continue
            nowcast = b * ratio
            cr_day = e + timedelta(days=CALL_REPORT_LAG_DAYS)
            m = build_base(company, as_of(h, cr_day), overlay, cr_day)
            q = next((q for q in m.forecast if q.end == e.isoformat()), None)
            tenq_filed = date.fromisoformat(actual.filed[:10]) if actual.filed else None
            if tenq_filed and tenq_filed > e + timedelta(days=AVAILABLE_LAG_DAYS):
                tenq_filed = None  # the value we have came from a later filing; we don't know the original date
            releases = [
                date.fromisoformat(f["filingDate"])
                for f in subs
                if f["form"] == "8-K"
                and "2.02" in (f.get("items") or "")
                and e < date.fromisoformat(f["filingDate"]) <= e + timedelta(days=AVAILABLE_LAG_DAYS)
            ]
            first_public = min([d for d in [tenq_filed, *releases] if d], default=None)
            rows.append(
                {
                    "ticker": ticker,
                    "quarter": quarter_label(e),
                    "call_report_due": cr_day.isoformat(),
                    "tenq_filed": tenq_filed.isoformat() if tenq_filed else None,
                    "earnings_release": min(releases).isoformat() if releases else None,
                    "lead_days_vs_10q": (tenq_filed - cr_day).days if tenq_filed else None,
                    "lead_days_vs_first_public": (first_public - cr_day).days if first_public else None,
                    "call_report_first": bool(first_public and first_public > cr_day),
                    "call_report_first_conservative": bool(
                        first_public and first_public > e + timedelta(days=CALL_REPORT_LAG_CONSERVATIVE)
                    ),
                    "lead_days_vs_10q_conservative": (
                        (tenq_filed - e - timedelta(days=CALL_REPORT_LAG_CONSERVATIVE)).days if tenq_filed else None
                    ),
                    "actual": actual.value,
                    "nowcast": nowcast,
                    "model_p50": q.p50 if q else None,
                    "ape_nowcast": _ape(nowcast, actual.value),
                    "ape_model": _ape(q.p50, actual.value) if q and q.p50 else None,
                    "direction_right": (nowcast > actual_prev.value) == (actual.value > actual_prev.value),
                }
            )
    paired = [r for r in rows if r["ape_model"] is not None]
    leads = [r["lead_days_vs_10q"] for r in rows if r["lead_days_vs_10q"] is not None]
    first = [r for r in rows if r["call_report_first"]]
    first_c = [r for r in rows if r["call_report_first_conservative"]]
    summary = {
        "median_lead_days_vs_10q_conservative": _median(
            [r["lead_days_vs_10q_conservative"] for r in rows if r["lead_days_vs_10q_conservative"] is not None]
        ),
        "call_report_first_n": len(first),
        "call_report_first_banks": sorted({r["ticker"] for r in first}),
        "median_lead_days_when_first": _median([r["lead_days_vs_first_public"] for r in first]),
        "call_report_first_conservative_share": len(first_c) / len(rows) if rows else None,
        "median_lead_days_when_first_conservative": _median(
            [r["lead_days_vs_first_public"] - (CALL_REPORT_LAG_CONSERVATIVE - CALL_REPORT_LAG_DAYS) for r in first_c]
        ),
        "median_ape_nowcast_when_first": _median([r["ape_nowcast"] for r in first]),
        "bank_quarters": len(rows),
        "banks": len({r["ticker"] for r in rows}),
        "quarters": sorted({r["quarter"] for r in rows}),
        "median_lead_days_vs_10q": _median(leads),
        "call_report_first_share": sum(r["call_report_first"] for r in rows) / len(rows) if rows else None,
        "median_ape_model": _median([r["ape_model"] for r in paired]),
        "median_ape_nowcast": _median([r["ape_nowcast"] for r in paired]),
        "nowcast_better_share": (
            sum(r["ape_nowcast"] < r["ape_model"] for r in paired) / len(paired) if paired else None
        ),
        "direction_hit_rate": sum(r["direction_right"] for r in rows) / len(rows) if rows else None,
    }
    return {"rows": rows, "summary": summary}


# --- 3. events: risk filings and agenda matches, with the stock's move afterwards ---------------------------------
def risk_events(session: Session, today: date, days: int = 365) -> list[dict]:
    cutoff = today - timedelta(days=days)
    out = []
    for company in session.exec(select(Company)).all():
        for f in _submissions(session, company.ticker):
            filed = date.fromisoformat(f["filingDate"])
            if filed < cutoff:
                continue
            kinds = []
            if f["form"] in LATE_FORMS:
                kinds.append(LATE_FORMS[f["form"]])
            if f["form"].startswith("8-K"):
                kinds += [RISK_ITEMS[i.strip()] for i in (f.get("items") or "").split(",") if i.strip() in RISK_ITEMS]
            for kind in kinds:
                acc = f["accessionNumber"].replace("-", "")
                out.append(
                    {
                        "ticker": company.ticker,
                        "archetype": company.archetype,
                        "date": filed.isoformat(),
                        "kind": kind,
                        "form": f["form"],
                        "source": "edgar",
                        "url": f"https://www.sec.gov/Archives/edgar/data/{int(company.cik)}/{acc}/"
                        f"{f.get('primaryDocument', '')}",
                    }
                )
    return out


def backfill_db() -> Path:
    return out_dir() / "agenda_backfill.db"


def backfill_agendas(days: int = 365) -> Path:
    """Re-run the Legistar and PDF-agenda connectors over the last `days` into a copy of the database, so the
    backtest sees a full year of agenda matches without adding historical signals to the live feed."""
    import os
    import sqlite3
    import subprocess
    import sys

    settings = get_settings()
    dst_path = backfill_db()
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if not dst_path.exists():
        src = sqlite3.connect(Path(settings.database_url.removeprefix("sqlite:///")))
        dst = sqlite3.connect(dst_path)
        src.backup(dst)
        src.close()
        dst.close()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{dst_path}"}
    ll = Path(sys.executable).parent / "ll"
    subprocess.run([str(ll), "backfill-worker", "--days", str(days)], env=env, check=True)
    return dst_path


def backfill_worker(days: int) -> None:
    from localledger.connectors.legistar import LegistarConnector
    from localledger.connectors.pdf_agenda import PdfAgendaConnector
    from localledger.db import session_scope

    for cls, kwargs in (
        (LegistarConnector, {"lookback_days": days}),
        (PdfAgendaConnector, {"lookback_days": days, "max_docs": 60}),
    ):
        with session_scope() as s:
            result = cls(s, **kwargs).run()
            print(f"{cls.source_id}: {result}")


def agenda_events(session: Session) -> list[dict]:
    from sqlalchemy import create_engine
    from sqlmodel import Session as SqlSession

    from localledger.models import Signal

    path = backfill_db()
    if path.exists():  # a year of agenda matches from the backfill copy; otherwise the live feed's
        with SqlSession(create_engine(f"sqlite:///{path}")) as bs:
            sigs = bs.exec(select(Signal).where(Signal.metric == "agenda_match")).all()
    else:
        sigs = session.exec(select(Signal).where(Signal.metric == "agenda_match")).all()
    out = []
    for s in sigs:
        detail = json.loads(s.detail_json or "{}")
        out.append(
            {
                "ticker": s.ticker,
                "date": str(detail.get("meeting_date") or s.as_of),
                "kind": "Local agenda item: " + ", ".join(detail.get("keywords", [])[:3]),
                "summary": s.summary,
                "source": s.source_id,
                "url": s.citation_url,
                "keywords": detail.get("keywords", []),
            }
        )
    return out


def sec_mention_lag(session: Session, events: list[dict], fetch: bool = True, horizon_days: int = 240) -> None:
    """For each agenda match, the company's first later SEC filing (8-K, 10-Q, 10-K) whose text names the matched
    project: how long a reader who follows only SEC filings would wait. Adds sec_mention_* fields in place."""
    from localledger.connectors.edgar import EdgarConnector
    from localledger.http import read_document_text

    conn = EdgarConnector(session, frames=False, full_text=False) if fetch else None
    texts: dict[str, str] = {}
    for e in events:
        if e.get("source") == "edgar" or not e.get("keywords"):
            continue
        company = session.get(Company, e["ticker"])
        day = date.fromisoformat(e["date"][:10])
        later = sorted(
            (
                f
                for f in _submissions(session, e["ticker"])
                if f["form"] in ("8-K", "10-Q", "10-K", "8-K/A")
                and day <= date.fromisoformat(f["filingDate"]) <= day + timedelta(days=horizon_days)
            ),
            key=lambda f: f["filingDate"],
        )
        for f in later[:8]:
            acc = f["accessionNumber"]
            if acc not in texts:
                docs = [
                    d
                    for d in session.exec(select(Document).where(Document.ticker == e["ticker"])).all()
                    if json.loads(d.meta_json or "{}").get("accession") == acc
                ]
                if not docs and conn is not None:
                    try:
                        docs = conn._fetch_primary(company, f, f["form"].lower())
                        if f["form"].startswith("8-K"):
                            docs += conn._fetch_exhibit_991(company, f)
                        session.commit()
                    except Exception as exc:  # a missing filing never stops the backtest
                        log.info("filing fetch failed %s %s: %s", e["ticker"], acc, exc)
                texts[acc] = " ".join(read_document_text(d) for d in docs).lower()
            hit = next((k for k in e["keywords"] if k.lower() in texts[acc]), None)
            if hit:
                e["sec_mention_date"] = f["filingDate"]
                e["sec_mention_form"] = f["form"]
                e["sec_mention_keyword"] = hit
                e["sec_mention_lag_days"] = (date.fromisoformat(f["filingDate"]) - day).days
                break


def load_prices(tickers: list[str], start: date, end: date) -> dict[str, dict[date, float]]:
    """Daily closes (Yahoo Finance via yfinance; internal research use, see LEGAL_REVIEW.md question 26)."""
    import yfinance as yf

    cache = out_dir() / "prices.json"
    cached = json.loads(cache.read_text()) if cache.exists() else {}
    need = [t for t in tickers if t not in cached]
    if need:
        data = yf.download(
            need,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            progress=False,
            auto_adjust=True,
            group_by="ticker",
            threads=True,
        )
        for t in need:
            try:
                col = data[t]["Close"] if len(need) > 1 else data["Close"]
                if hasattr(col, "columns"):
                    col = col.iloc[:, 0]
                cached[t] = {str(k.date()): float(v) for k, v in col.dropna().items()}
            except (KeyError, AttributeError):
                cached[t] = {}
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(cached))
    return {t: {date.fromisoformat(k): v for k, v in cached.get(t, {}).items()} for t in tickers}


def _returns(prices: dict[date, float], bench: dict[date, float], day: date) -> dict:
    days = sorted(d for d in prices if d in bench)
    if not days:
        return {}
    # Day 0 = first trading close on/after the filing date (the connector flags filings the day they appear).
    i0 = next((i for i, d in enumerate(days) if d >= day), None)
    if i0 is None or i0 == 0:
        return {}
    base, bbase = prices[days[i0 - 1]], bench[days[i0 - 1]]  # close before the news
    out = {}
    for h in HORIZONS:
        j = i0 + h - 1
        if j < len(days):
            r = prices[days[j]] / base - 1
            out[f"r{h}"] = r
            out[f"ar{h}"] = r - (bench[days[j]] / bbase - 1)
    return out


def event_study(session: Session, today: date | None = None, fetch: bool = True) -> dict:
    today = today or date.today()
    agendas = [e for e in agenda_events(session) if e["date"] >= (today - timedelta(days=365)).isoformat()]
    sec_mention_lag(session, agendas, fetch=fetch)
    events = risk_events(session, today) + agendas
    tickers = sorted({e["ticker"] for e in events})
    prices = load_prices(tickers + ["IWM"], today - timedelta(days=400), today)
    for e in events:
        e.update(_returns(prices.get(e["ticker"], {}), prices["IWM"], date.fromisoformat(e["date"])))
    by_kind = defaultdict(list)
    for e in events:
        by_kind[e["kind"] if e["source"] == "edgar" else "Local agenda item"].append(e)
    summary = {}
    for kind, es in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        row = {"n": len(es), "tickers": len({e["ticker"] for e in es})}
        for h in HORIZONS:
            ars = [e[f"ar{h}"] for e in es if f"ar{h}" in e]
            row[f"median_ar{h}"] = _median(ars)
            row[f"n_ar{h}"] = len(ars)
            row[f"share_down_ar{h}"] = sum(a < 0 for a in ars) / len(ars) if ars else None
        summary[kind] = row
    risk = [e for e in events if e["source"] == "edgar"]
    for h in HORIZONS:
        ars = [e[f"ar{h}"] for e in risk if f"ar{h}" in e]
        summary.setdefault("_all_risk", {})[f"median_ar{h}"] = _median(ars)
        summary["_all_risk"][f"n_ar{h}"] = len(ars)
        summary["_all_risk"][f"share_down_ar{h}"] = sum(a < 0 for a in ars) / len(ars) if ars else None
    lags = [e["sec_mention_lag_days"] for e in agendas if "sec_mention_lag_days" in e]
    summary["_agenda_lead"] = {
        "n": len(agendas),
        "tickers": len({e["ticker"] for e in agendas}),
        "with_later_sec_mention": len(lags),
        "median_days_before_sec_mention": _median(lags),
        "max_days_before_sec_mention": max(lags) if lags else None,
    }
    summary["_all_risk"]["n"] = len(risk)
    summary["_all_risk"]["tickers"] = len({e["ticker"] for e in risk})
    return {"events": sorted(events, key=lambda e: e["date"], reverse=True), "summary": summary}


# --- 4. reading load: what a person would have to read to see the same things ----------------------------------
READ_MINUTES = {"8-K": 5, "10-Q": 30, "10-K": 60, "agenda": 10, "call_report": 10}  # stated assumptions


def workload_study(session: Session, events: dict, today: date | None = None) -> dict:
    from sqlalchemy import create_engine
    from sqlmodel import Session as SqlSession

    from localledger.connectors.ffiec import load_bank_map, resolve_bank_map

    today = today or date.today()
    cutoff = (today - timedelta(days=365)).isoformat()
    filings: dict[str, int] = defaultdict(int)
    for company in session.exec(select(Company)).all():
        for f in _submissions(session, company.ticker):
            if f["filingDate"] >= cutoff and f["form"] in ("8-K", "10-Q", "10-K"):  # skip Forms 3/4, proxies etc.
                filings[f["form"]] += 1
    agendas = 0
    if backfill_db().exists():
        with SqlSession(create_engine(f"sqlite:///{backfill_db()}")) as bs:
            agendas = len(bs.exec(select(Document.id).where(Document.source_id.in_(["legistar", "pdf_agenda"]))).all())
    banks = len(resolve_bank_map(load_bank_map(get_settings().config_dir / "bank_map.yaml")))
    docs = {**filings, "agenda": agendas, "call_report": banks * 4}
    total = sum(docs.values())
    flagged = len(events["events"])
    return {
        "documents": docs,
        "documents_total": total,
        "reading_hours": sum(READ_MINUTES.get(k, 5) * n for k, n in docs.items()) / 60,
        "flagged": flagged,
        "flag_rate": flagged / total if total else None,
        "minutes_per_document": READ_MINUTES,
    }


# --- run everything -----------------------------------------------------------------------------------------------
def run_backtest(session: Session, today: date | None = None, fetch: bool = True) -> dict:
    today = today or date.today()
    results = {
        "as_of": today.isoformat(),
        "forecast": forecast_study(session, today=today),
        "banks": bank_study(session, today=today, fetch=fetch),
        "events": event_study(session, today=today, fetch=fetch),
    }
    results["workload"] = workload_study(session, results["events"], today)
    d = out_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "results.json").write_text(json.dumps(results, indent=1, default=str))
    (d / "summary.md").write_text(render_summary(results))
    return results


def _pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def render_summary(r: dict) -> str:
    f, b, ev = r["forecast"]["summary"], r["banks"]["summary"], r["events"]["summary"]
    lines = [
        f"# Backtest (as of {r['as_of']})",
        "",
        "## 1. Revenue forecasts, point in time",
        f"{f['company_quarters']} company-quarters across {f['companies']} companies; each forecast made on the first "
        "day of the quarter from filings public at the time.",
        "",
        "| metric | value |",
        "|---|---|",
        f"| median abs. error, our model | {_pct(f['median_ape_model'])} |",
        f"| median abs. error, same quarter last year | {_pct(f['median_ape_seasonal_naive'])} |",
        f"| median abs. error, last reported quarter | {_pct(f['median_ape_last_quarter'])} |",
        f"| model at least as good as both naive forecasts | {_pct(f['model_beats_both_naive_share'], 0)} |",
        f"| actual inside our P10-P90 range (target 80%) | {_pct(f['p10_p90_coverage'], 0)} |",
        f"| Brier score, P(revenue up y/y) (lower is better) | {f['brier_p_up_yoy']:.3f} vs {f['brier_base_rate']:.3f} "
        "for always forecasting the base rate |"
        if f["brier_p_up_yoy"] is not None
        else "| Brier | n/a |",
        "",
        "## 2. Banks: call report vs the 10-Q",
        f"{b['bank_quarters']} bank-quarters, {b['banks']} banks ({', '.join(b['quarters'])}).",
        "",
        "| metric | value |",
        "|---|---|",
        f"| median days the call report lands before the 10-Q (due date / +45-day posting) "
        f"| {b['median_lead_days_vs_10q']} / {b['median_lead_days_vs_10q_conservative']} |",
        f"| call report is the first public revenue number (due date / +45 days) "
        f"| {_pct(b['call_report_first_share'], 0)} / {_pct(b['call_report_first_conservative_share'], 0)} |",
        f"| ...in those cases, median days ahead of the company (due date / +45 days) "
        f"| {b['median_lead_days_when_first']} / {b['median_lead_days_when_first_conservative']} |",
        f"| ...and the nowcast's median error | {_pct(b['median_ape_nowcast_when_first'])} |",
        f"| median abs. error, statistical model | {_pct(b['median_ape_model'])} |",
        f"| median abs. error, call-report nowcast | {_pct(b['median_ape_nowcast'])} |",
        f"| nowcast beats the model | {_pct(b['nowcast_better_share'], 0)} |",
        f"| q/q direction right | {_pct(b['direction_hit_rate'], 0)} |",
        "",
        "## 3. Events flagged in the last 12 months (abnormal return = stock minus IWM, from the close before)",
        "",
        "| event | n | tickers | median 5d | median 20d | median 60d | share down at 20d |",
        "|---|---|---|---|---|---|---|",
    ]
    lead = ev.get("_agenda_lead", {})
    w = r.get("workload")
    for kind, row in ev.items():
        if kind.startswith("_agenda"):
            continue
        lines.append(
            f"| {'All EDGAR risk events' if kind == '_all_risk' else kind} | {row['n']} | {row['tickers']} "
            f"| {_pct(row.get('median_ar5'))} | {_pct(row.get('median_ar20'))} | {_pct(row.get('median_ar60'))} "
            f"| {_pct(row.get('share_down_ar20'), 0)} |"
        )
    lines += [
        "",
        f"Agenda items: {lead.get('n', 0)} matches for {lead.get('tickers', 0)} companies; "
        f"{lead.get('with_later_sec_mention', 0)} were later named in an SEC filing, a median "
        f"{lead.get('median_days_before_sec_mention')} days after the meeting "
        f"(max {lead.get('max_days_before_sec_mention')}).",
    ]
    if w:
        docs = ", ".join(f"{k} {v:,}" for k, v in w["documents"].items())
        lines += [
            "",
            "## 4. Reading load",
            f"{w['documents_total']:,} documents in 12 months ({docs}) would take about {w['reading_hours']:,.0f} "
            f"hours to read at the stated minutes per document; the system surfaced {w['flagged']} events from them "
            f"({w['flag_rate']:.1%}).",
        ]
    return "\n".join(lines) + "\n"
