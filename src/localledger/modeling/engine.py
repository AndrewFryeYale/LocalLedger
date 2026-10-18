"""Deterministic company model: revenue history -> quarterly forecast with probabilities -> valuation.

Forecast: each unreported fiscal quarter starts from a blend of seasonal growth (same quarter last year x trailing
growth) and run-rate (last quarter x seasonal ratio). Volatility comes from backtesting that same method on the
company's own history. A seeded Monte Carlo adds discrete events (with probabilities by quarter) and yields
P10/P50/P90 and P(revenue up year over year). Signals act through `adjustments` (see impacts.py), and every run uses
the same random draws, so before/after differences come from the adjustment, not from noise.

Valuation (NTM-based, by archetype): banks P/TBV (peer range + ROTE-justified), insurers P/B (ROE-justified), land
and farmland P/B, others EV/EBITDA or EV/Revenue against universe peers. Pre-revenue companies aren't valued.
These are model estimates, shown in full only inside the gated app (the newsletter gets impact deltas only).
"""

from __future__ import annotations

import calendar
import hashlib
import math
import random
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from localledger.modeling.history import History, Period

DRAWS = 1500
COE_BANK, COE_INSURER, GROWTH_LT = 0.11, 0.12, 0.03
DEFAULT_MULTIPLES = {
    "p_tbv": (0.8, 1.0, 1.3),
    "p_b": (0.7, 1.0, 1.4),
    "ev_ebitda": (6.0, 8.5, 11.0),
    "ev_revenue": (0.8, 1.5, 2.5),
}
BOOK_ARCHETYPES = {"Land / entitlement developer", "Farmland / water rights"}
MIN_REVENUE_TTM = 1_000_000
STALE_DAYS = 300
NAV_PENDING_PB = 3.0  # land carried at historical cost: book multiples mislead above this market P/B


@dataclass
class Event:
    name: str
    amount: float  # revenue if it happens (USD), or equity value for kind="value"
    probs: dict[str, float]  # quarter end (ISO) -> probability it lands in that quarter
    kind: str = "revenue"  # revenue | value
    keywords: list[str] = field(default_factory=list)
    note: str = ""
    source: str = "overlay"


@dataclass
class Adjustment:
    kind: str  # factor | add | ni_add | risk | event_shift | event_new | note
    value: float = 0.0
    quarters: list[str] = field(default_factory=list)  # quarter ends (ISO); empty = all future quarters
    note: str = ""
    source: str = ""
    event: dict | None = None


@dataclass
class QuarterEstimate:
    start: str
    end: str
    label: str
    status: str  # unreported | current | next | ntm
    base: float
    p10: float = 0.0
    p50: float = 0.0
    p90: float = 0.0
    mean: float = 0.0
    prior_year: float | None = None
    p_yoy: float | None = None
    ni_p50: float | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class CompanyModel:
    ticker: str
    name: str
    archetype: str
    status: str = "modeled"  # modeled | no_xbrl | pre_revenue
    revenue_tag: str | None = None
    history: list[dict] = field(default_factory=list)
    ttm: dict = field(default_factory=dict)
    balance: dict = field(default_factory=dict)
    trend_growth: float = 0.0
    sigma: float = 0.25
    events: list[Event] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)
    risk_premium: float = 0.0
    forecast: list[QuarterEstimate] = field(default_factory=list)
    valuation: dict = field(default_factory=dict)
    overlay_note: str = ""
    sources: list[dict] = field(default_factory=list)
    as_of: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> CompanyModel:
        d = dict(d)
        d["events"] = [Event(**e) for e in d.get("events", [])]
        d["adjustments"] = [Adjustment(**a) for a in d.get("adjustments", [])]
        d["forecast"] = [QuarterEstimate(**q) for q in d.get("forecast", [])]
        return cls(**d)

    def visible_quarters(self) -> list[QuarterEstimate]:
        return [q for q in self.forecast if q.status != "ntm"]

    def quarter_containing(self, day: date) -> QuarterEstimate | None:
        for q in self.forecast:
            if date.fromisoformat(q.start) <= day <= date.fromisoformat(q.end):
                return q
        return None

    def future_quarters_after(self, day: date) -> list[QuarterEstimate]:
        return [q for q in self.forecast if date.fromisoformat(q.end) > day]


# --- helpers ----------------------------------------------------------------------------------------------
def month_end(year: int, month: int) -> date:
    while month > 12:
        year, month = year + 1, month - 12
    return date(year, month, calendar.monthrange(year, month)[1])


def add_quarter(end: date) -> date:
    return month_end(end.year, end.month + 3)


def quarter_label(end: date) -> str:
    return f"Q ending {end:%b %Y}"


def _value_near(series: dict[date, float], target: date, tol: int = 12) -> float | None:
    for d, v in series.items():
        if abs((d - target).days) <= tol:
            return v
    return None


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _seed(ticker: str) -> int:
    return int(hashlib.sha256(ticker.encode()).hexdigest()[:12], 16)


def trailing_growth(values: list[float]) -> float:
    if len(values) < 8:
        return 0.0
    ttm, prior = sum(values[-4:]), sum(values[-8:-4])
    if prior <= 0 or ttm <= 0:
        return 0.0
    return max(-0.3, min(0.4, ttm / prior - 1))


def naive_forecast(series: dict[date, float], target_end: date, last_end: date, growth: float) -> float | None:
    """Blend of seasonal growth and seasonal-ratio run-rate, using only data up to `last_end`."""
    sq = _value_near(series, target_end - timedelta(days=365))
    last = series.get(last_end)
    last_ly = _value_near(series, last_end - timedelta(days=365))
    step_change = bool(last and last_ly and last_ly > 0 and not 0.55 <= last / last_ly <= 1.8)
    candidates = []
    if sq is not None:
        candidates.append(sq * (1 + growth))
    if last is not None:
        ratio = (sq / last_ly) if sq is not None and last_ly and last_ly > 0 and not step_change else 1.0
        candidates.append(last * max(0.6, min(1.6, ratio)))  # bounded: acquisitions distort year-ago bases
    if not candidates:
        return None
    est = sum(candidates) / len(candidates)
    if len(candidates) == 2 and step_change:
        est = 0.15 * candidates[0] + 0.85 * candidates[1]  # step change (acquisition, divestiture): trust run-rate
    if last and last > 0:
        est = max(0.4 * last, min(2.5 * last, est))  # never more than 2.5x / less than 0.4x the last actual
    return max(0.0, est)


def clean_series(periods: list[Period]) -> tuple[list[Period], list[str]]:
    """Drop quarters more than 5x away from the median of their +/-4 neighbours (mis-tagged facts)."""
    keep, dropped = [], []
    for i, p in enumerate(periods):
        neigh = [q.value for j, q in enumerate(periods[max(0, i - 4) : i + 5]) if j + max(0, i - 4) != i]
        med = statistics.median(neigh) if len(neigh) >= 4 else None
        if med and med > 0 and (p.value < 0.2 * med or p.value > 5 * med):
            dropped.append(f"{p.end:%b %Y}")
            continue
        keep.append(p)
    return keep, dropped


def backtest_sigma(periods: list[Period]) -> float:
    errs = []
    for i in range(5, len(periods)):
        known = {p.end: p.value for p in periods[:i]}
        g = trailing_growth([p.value for p in periods[:i]])
        f = naive_forecast(known, periods[i].end, periods[i - 1].end, g)
        if f and f > 0 and periods[i].value > 0:
            errs.append(math.log(periods[i].value / f))
    if len(errs) < 4:
        return 0.25
    return max(0.05, min(0.5, statistics.pstdev(errs[-12:])))


# --- build ------------------------------------------------------------------------------------------------
def build_base(company, history: History | None, overlay: dict | None, today: date) -> CompanyModel:
    m = CompanyModel(ticker=company.ticker, name=company.name, archetype=company.archetype, as_of=today.isoformat())
    overlay = overlay or {}
    m.overlay_note = str(overlay.get("note", "")).strip()
    for e in overlay.get("events", []) or []:
        m.events.append(
            Event(
                name=e["name"],
                amount=float(e.get("amount", 0)),
                kind=e.get("kind", "revenue"),
                probs={str(k): float(v) for k, v in (e.get("probs") or {}).items()},
                keywords=list(e.get("keywords", [])),
                note=e.get("note", ""),
                source=e.get("source", "overlay"),
            )
        )
    m.risk_premium = float(overlay.get("risk_premium", 0.0))
    if history is None or not history.series("revenue"):
        m.status = "no_xbrl"
        m.balance = {"mkt_cap": (company.mkt_cap_musd or 0) * 1e6}
        if history is not None and history.currency != "USD":
            m.overlay_note = f"Reports in {history.currency}; FX conversion isn't modeled yet."
        elif history is not None and history.instants.get("equity"):
            m.status = "balance_only"  # e.g. pre-revenue developers: book-based indicative value
            eq = history.instants["equity"][1]
            mkt_cap = (company.mkt_cap_musd or 0) * 1e6
            m.balance.update(
                equity=eq,
                tbv=eq,
                cash=history.instants.get("cash", (None, None))[1],
                debt=history.instants.get("debt", (None, 0.0))[1] or 0.0,
                shares=history.shares,
                price=(mkt_cap / history.shares) if history.shares and mkt_cap else None,
                balance_date=history.instants["equity"][0].isoformat(),
            )
            m.overlay_note = "No revenue in XBRL (e.g. a pre-revenue developer): balance sheet only."
        return m
    rev, dropped = clean_series(history.series("revenue")[-16:])
    m.revenue_tag = history.revenue_tag
    if dropped:
        m.overlay_note = (m.overlay_note + " " if m.overlay_note else "") + (
            "Dropped outlier quarters (likely mis-tagged XBRL): " + ", ".join(dropped) + "."
        )
    m.sources.append({"document_id": history.document_id, "url": history.source_url, "title": "SEC XBRL company facts"})
    ni = {p.end: p.value for p in history.series("net_income")}
    op = {p.end: p.value for p in history.series("operating_income")}
    dna = {p.end: p.value for p in history.series("dna")}
    for p in rev[-12:]:
        m.history.append(
            {
                "start": p.start.isoformat(),
                "end": p.end.isoformat(),
                "label": quarter_label(p.end),
                "revenue": p.value,
                "net_income": ni.get(p.end),
                "derived": p.derived,
                "accn": p.accn,
            }
        )
    last4 = rev[-4:]
    ttm_rev = sum(p.value for p in last4) if len(last4) == 4 else None
    ttm_ni = (
        sum(ni[p.end] for p in last4 if p.end in ni) if len(last4) == 4 and all(p.end in ni for p in last4) else None
    )
    ttm_ebitda = None
    if len(last4) == 4 and all(p.end in op for p in last4):
        ttm_ebitda = sum(op[p.end] + dna.get(p.end, 0.0) for p in last4)
    m.trend_growth = trailing_growth([p.value for p in rev])
    m.sigma = backtest_sigma(rev)
    m.ttm = {
        "revenue": ttm_rev,
        "net_income": ttm_ni,
        "ebitda": ttm_ebitda,
        "growth": m.trend_growth,
        "through": rev[-1].end.isoformat(),
    }
    inst = history.instants
    equity = inst.get("equity", (None, None))[1]
    intang = (inst.get("goodwill", (None, 0.0))[1] or 0.0) + (inst.get("intangibles", (None, 0.0))[1] or 0.0)
    shares = history.shares
    mkt_cap = (company.mkt_cap_musd or 0) * 1e6
    m.balance = {
        "equity": equity,
        "tbv": (equity - intang) if equity is not None else None,
        "cash": inst.get("cash", (None, None))[1],
        "debt": inst.get("debt", (None, 0.0))[1] or 0.0,
        "shares": shares,
        "shares_as_of": history.shares_as_of,
        "mkt_cap": mkt_cap,
        "price": (mkt_cap / shares) if shares and mkt_cap else None,
        "balance_date": inst.get("equity", (None,))[0].isoformat() if inst.get("equity") else None,
    }
    if history.kind == "trust":
        m.status = "trust"
        m.balance.update(equity=None, tbv=None, debt=0.0, cash=0.0)
    elif ttm_rev is not None and ttm_rev < MIN_REVENUE_TTM and not overlay.get("events"):
        m.status = "pre_revenue"
    # forecast quarters: every unreported quarter through the one after the current, plus enough for NTM
    quarters: list[QuarterEstimate] = []
    end, seen_current, seen_next = rev[-1].end, False, False
    if (today - rev[-1].end).days > STALE_DAYS:  # e.g. liquidation-basis filers stop reporting revenue
        m.status = "stale"
        m.overlay_note = (m.overlay_note + " " if m.overlay_note else "") + (
            f"Revenue history is stale (last XBRL quarter {rev[-1].end:%b %Y}); no revenue forecast."
        )
        end = month_end(today.year, ((today.month - 1) // 3) * 3) if today.month > 3 else month_end(today.year - 1, 12)
    while len(quarters) < 16:
        start, end = end + timedelta(days=1), add_quarter(end)
        if end < today:
            status = "unreported"
        elif start <= today <= end:
            status, seen_current = "current", True
        elif seen_current and not seen_next:
            status, seen_next = "next", True
        else:
            status = "ntm"
        quarters.append(
            QuarterEstimate(
                start=start.isoformat(), end=end.isoformat(), label=quarter_label(end), status=status, base=0.0
            )
        )
        if seen_next and sum(1 for q in quarters if q.status != "unreported") >= 4:
            break
    m.forecast = quarters
    resolve_event_quarters(m)
    if m.status != "stale":
        recompute(m, rev)
    return m


def resolve_event_quarters(m: CompanyModel) -> None:
    """Overlay events may key probabilities by 'current', 'next', '+2'... (offset from the current quarter)."""
    order = [q for q in m.forecast if q.status != "unreported"]
    for e in m.events:
        resolved = {}
        for key, p in e.probs.items():
            k = str(key).strip().lower()
            idx = {"current": 0, "next": 1}.get(k)
            if idx is None and k.startswith("+") and k[1:].isdigit():
                idx = int(k[1:])
            if idx is not None:
                if idx < len(order):
                    resolved[order[idx].end] = resolved.get(order[idx].end, 0.0) + p
            else:
                resolved[str(key)] = p
        e.probs = resolved


def _series(m: CompanyModel) -> dict[date, float]:
    return {date.fromisoformat(h["end"]): h["revenue"] for h in m.history}


def recompute(m: CompanyModel, rev: list[Period] | None = None) -> CompanyModel:
    """(Re)compute baseline, Monte Carlo and NI for every forecast quarter from history + events + adjustments."""
    if m.status in ("no_xbrl", "stale") or not m.forecast:
        return m
    series = _series(m)
    last_end = max(series)
    rng = random.Random(_seed(m.ticker))
    z = [[rng.gauss(0, 1) for _ in range(DRAWS)] for _ in m.forecast]
    u = [[rng.random() for _ in range(DRAWS)] for _ in m.forecast]
    margin = None
    if m.ttm.get("net_income") is not None and m.ttm.get("revenue"):
        margin = max(-1.0, min(0.5, m.ttm["net_income"] / m.ttm["revenue"]))
    for h, q in enumerate(m.forecast, start=1):
        end = date.fromisoformat(q.end)
        base = naive_forecast(series, end, last_end, m.trend_growth) or 0.0
        q.notes = []
        factor, add, ni_add = 1.0, 0.0, 0.0
        for a in m.adjustments:
            if a.quarters and q.end not in a.quarters:
                continue
            if a.kind == "factor":
                factor *= a.value
            elif a.kind == "add":
                add += a.value
            elif a.kind == "ni_add":
                ni_add += a.value
            else:
                continue
            q.notes.append(a.note)
        q.base = round(base * factor + add, 2)
        series[end] = q.base  # later quarters season off this quarter's estimate
        sig = min(0.6, m.sigma * math.sqrt(h))
        events = [(e.amount, e.probs.get(q.end, 0.0)) for e in m.events if e.kind == "revenue" and e.probs.get(q.end)]
        draws = []
        for i in range(DRAWS):
            x = base * factor * math.exp(sig * z[h - 1][i]) + add  # median-centred on the baseline estimate
            for amount, p in events:
                if u[h - 1][i] < p:
                    x += amount
            draws.append(max(0.0, x))
        q.p10, q.p50, q.p90 = (round(_pct(draws, p), 2) for p in (0.1, 0.5, 0.9))
        q.mean = round(sum(draws) / DRAWS, 2)
        q.prior_year = _value_near({d: v for d, v in _series(m).items()}, end - timedelta(days=365))
        q.p_yoy = round(sum(1 for x in draws if x >= q.prior_year) / DRAWS, 3) if q.prior_year else None
        q.ni_p50 = round(margin * q.p50 + ni_add, 2) if margin is not None else None
    return m


# --- valuation --------------------------------------------------------------------------------------------
def method_for(m: CompanyModel, overlay: dict | None = None) -> str:
    if overlay and overlay.get("valuation", {}).get("method"):
        return overlay["valuation"]["method"]
    if m.status == "trust":
        return "distribution_yield"
    if m.status == "call_report_only":
        return "p_tbv"
    if m.status in ("pre_revenue", "stale", "balance_only") and (m.balance.get("equity") or 0) > 0:
        return "p_b"  # indicative only: book-based for companies without a usable revenue history
    if m.status != "modeled":
        return "not_modeled"
    if m.archetype == "Community bank":
        return "p_tbv"
    if m.archetype == "State-regulated P&C insurer":
        return "p_b_roe"
    if m.archetype in BOOK_ARCHETYPES:
        pb = market_multiples(m).get("p_b")
        return "nav_pending" if pb and pb > NAV_PENDING_PB else "p_b"
    if (m.ttm.get("ebitda") or 0) > 0:
        return "ev_ebitda"
    return "ev_revenue"


def ntm(m: CompanyModel) -> dict:
    qs = [q for q in m.forecast if q.status in ("current", "next", "ntm", "unreported")]
    first = [q for q in qs if q.status != "unreported"][:4] or qs[:4]
    rev = sum(q.p50 for q in first)
    ni = sum(q.ni_p50 for q in first) if first and all(q.ni_p50 is not None for q in first) else None
    ebitda = None
    if m.ttm.get("ebitda") is not None and m.ttm.get("revenue"):
        ebitda = rev * (m.ttm["ebitda"] / m.ttm["revenue"])
    # value events (e.g. an entitlement) are probability-weighted into equity value
    value_events = sum(e.amount * min(1.0, sum(e.probs.values())) for e in m.events if e.kind == "value")
    return {
        "revenue": rev,
        "net_income": ni,
        "ebitda": ebitda,
        "value_events": value_events,
        "quarters": [q.label for q in first],
    }


def market_multiples(m: CompanyModel) -> dict[str, float]:
    b, out = m.balance, {}
    cap = b.get("mkt_cap") or 0
    if not cap or m.status in ("no_xbrl", "trust", "call_report_only", "balance_only"):
        return out
    ev = cap + (b.get("debt") or 0) - (b.get("cash") or 0)
    if b.get("tbv") and b["tbv"] > 0:
        out["p_tbv"] = cap / b["tbv"]
    if b.get("equity") and b["equity"] > 0:
        out["p_b"] = cap / b["equity"]
        out["p_b_roe"] = out["p_b"]
    if (m.ttm.get("ebitda") or 0) > 0 and ev > 0:
        out["ev_ebitda"] = ev / m.ttm["ebitda"]
    if (m.ttm.get("revenue") or 0) > 0 and ev > 0:
        out["ev_revenue"] = ev / m.ttm["revenue"]
    return out


def peer_stats(models: list[CompanyModel]) -> dict:
    """Multiple ranges by (archetype, method), with universe-wide fallback. Outliers trimmed at P5/P95."""
    buckets: dict[tuple[str, str], list[float]] = {}
    for m in models:
        for method, v in market_multiples(m).items():
            buckets.setdefault((m.archetype, method), []).append(v)
            buckets.setdefault(("*", method), []).append(v)
    stats = {}
    for key, vals in buckets.items():
        lo, hi = _pct(vals, 0.05), _pct(vals, 0.95)
        vals = [v for v in vals if lo <= v <= hi]
        if len(vals) >= 5:
            stats[f"{key[0]}|{key[1]}"] = {
                "p25": _pct(vals, 0.25),
                "median": _pct(vals, 0.5),
                "p75": _pct(vals, 0.75),
                "n": len(vals),
            }
    return stats


def _range(stats: dict, archetype: str, method: str) -> tuple[float, float, float, str]:
    key = "p_b" if method == "p_b_roe" else method
    for scope in (archetype, "*"):
        s = stats.get(f"{scope}|{key}")
        if s:
            return (
                s["p25"],
                s["median"],
                s["p75"],
                ("archetype peers" if scope != "*" else "universe") + f" (n={s['n']})",
            )
    lo, mid, hi = DEFAULT_MULTIPLES.get(key, (1.0, 1.0, 1.0))
    return lo, mid, hi, "default range"


def value(m: CompanyModel, stats: dict, overlay: dict | None = None) -> dict:
    method = method_for(m, overlay)
    b = m.balance
    shares, price = b.get("shares"), b.get("price")
    out = {
        "method": method,
        "price": price,
        "mkt_cap": b.get("mkt_cap"),
        "shares": shares,
        "risk_premium": m.risk_premium,
        "ntm": ntm(m) if m.status == "modeled" else {},
    }
    if method == "nav":  # curated net asset value per share (e.g. liquidation-basis companies)
        nav = float((overlay or {}).get("valuation", {}).get("nav_per_share") or 0)
        if nav > 0:
            haircut = max(0.5, 1 - 5 * m.risk_premium)
            extra = ntm(m).get("value_events", 0.0) / shares if shares else 0.0
            per = {"low": nav * 0.85 * haircut + extra, "base": nav * haircut + extra, "high": nav * 1.10 + extra}
            out.update(
                per_share={k: round(v, 2) for k, v in per.items()},
                note=(overlay or {}).get("valuation", {}).get("source", "Curated NAV per share."),
                sensitivity_basis="nav",
                sensitivity_metric=None,
            )
            if price:
                out["vs_price"] = {k: round(v / price - 1, 4) for k, v in out["per_share"].items()}
            return out
    if method == "distribution_yield":  # royalty trusts: value = trailing distributions / required yield
        ttm_dist = m.ttm.get("revenue") or 0.0
        units = shares or 0
        if ttm_dist > 0 and units:
            base_y = 0.11 + m.risk_premium
            per = {
                "low": ttm_dist / (base_y + 0.03) / units,
                "base": ttm_dist / base_y / units,
                "high": ttm_dist / max(0.05, base_y - 0.03) / units,
            }
            out.update(
                per_share={k: round(v, 2) for k, v in per.items()},
                yields={"low": base_y + 0.03, "base": base_y, "high": max(0.05, base_y - 0.03)},
                sensitivity_basis="revenue",
                sensitivity_metric=ntm(m).get("revenue"),
                note="Trailing-12-month distributions capitalized at a 8-14% required yield (11% base).",
            )
            if price:
                out["vs_price"] = {k: round(v / price - 1, 4) for k, v in out["per_share"].items()}
        else:
            out.update(
                method="not_modeled",
                note="No distributions in the last 12 months (e.g. excess costs being "
                "recovered); value depends on when distributions resume.",
            )
        return out
    if method == "p_tbv" and m.status == "call_report_only":
        book, cap = b.get("tbv"), b.get("mkt_cap")
        lo_m, mid_m, hi_m, peer_src = _range(stats, m.archetype, "p_tbv")
        if book and cap:
            haircut = max(0.5, 1 - 5 * m.risk_premium)
            vals = {k: book * mult * haircut for k, mult in (("low", lo_m), ("base", mid_m), ("high", hi_m))}
            out.update(
                equity_value={k: round(v) for k, v in vals.items()},
                vs_mkt_cap={k: round(v / cap - 1, 4) for k, v in vals.items()},
                multiples={"low": lo_m, "base": mid_m, "high": hi_m, "peers": peer_src},
                note="Bank-level call report only (holding company isn't an SEC filer): Tier 1 capital as a "
                "tangible book proxy; no share count, so value is shown in total vs market cap.",
            )
        else:
            out.update(method="not_modeled", note="Call report data incomplete.")
        return out
    if method == "nav_pending":
        out["note"] = (
            "Land carried at historical cost (market P/B above "
            f"{NAV_PENDING_PB:.0f}x), so book multiples would mislead. A curated parcel NAV is queued "
            "for this company."
        )
        return out
    if method == "not_modeled" or not shares:
        out["note"] = m.overlay_note or {
            "no_xbrl": "No XBRL financials (e.g. royalty trusts or non-SEC filers); tracked through signals only.",
            "pre_revenue": "Pre-revenue: valuation not modeled; catalysts tracked as events.",
        }.get(m.status, "Insufficient data to value.")
        out["method"] = "not_modeled"
        return out
    n = out["ntm"]
    lo_m, mid_m, hi_m, peer_src = _range(stats, m.archetype, method)
    haircut = max(0.5, 1 - 5 * m.risk_premium)  # 100 bp of extra risk premium ~ 5% lower multiple
    net_debt = (b.get("debt") or 0) - (b.get("cash") or 0)
    extra = n.get("value_events", 0.0)
    if method in ("p_tbv", "p_b", "p_b_roe"):
        book = b.get("tbv") if method == "p_tbv" else b.get("equity")
        if (not book or book <= 0) and m.status == "modeled" and (m.ttm.get("revenue") or 0) > 0:
            # negative book (e.g. heavy leverage or buybacks): fall back to an enterprise-value multiple
            fallback = "ev_ebitda" if (m.ttm.get("ebitda") or 0) > 0 else "ev_revenue"
            out = value(m, stats, {**(overlay or {}), "valuation": {"method": fallback}})
            out["note"] = (
                out.get("note", "") + " Book value is negative, so an enterprise-value multiple is used."
            ).strip()
            return out
        if not book or book <= 0:
            out.update(method="not_modeled", note="Negative or missing book value.")
            return out
        coe = (COE_BANK if method == "p_tbv" else COE_INSURER) + m.risk_premium
        roe = (n["net_income"] / book) if n.get("net_income") is not None else None
        justified = None
        if roe is not None and method in ("p_tbv", "p_b_roe"):
            roe = max(-0.05, min(0.25, roe))
            justified = max(0.3, (roe - GROWTH_LT) / (coe - GROWTH_LT))
        base_m = (0.5 * mid_m + 0.5 * justified) if justified else mid_m
        per = {"low": book * lo_m * haircut, "base": book * base_m * haircut, "high": book * hi_m * haircut}
        out.update(book=book, roe_ntm=roe, coe=coe, justified_multiple=justified)
        out["multiples"] = {"low": lo_m, "base": base_m, "high": hi_m, "peers": peer_src}
        per = {k: (v + extra) / shares for k, v in per.items()}
        if m.status in ("pre_revenue", "stale", "balance_only"):
            out["note"] = "Indicative only: book-based value for a company without a usable revenue history."
    else:
        metric = n.get("ebitda") if method == "ev_ebitda" else n.get("revenue")
        if not metric or metric <= 0:
            out.update(method="not_modeled", note="Non-positive NTM metric.")
            return out
        per = {
            k: (metric * mult * haircut - net_debt + extra) / shares
            for k, mult in (("low", lo_m), ("base", mid_m), ("high", hi_m))
        }
        out["multiples"] = {"low": lo_m, "base": mid_m, "high": hi_m, "peers": peer_src}
        out["net_debt"] = net_debt
    lo_v, base_v, hi_v = per["low"], per["base"], per["high"]
    lo_v, hi_v = min(lo_v, base_v, hi_v), max(lo_v, base_v, hi_v)
    if hi_v <= 0:
        out["note"] = "Equity value is negative at peer multiples (net debt exceeds modeled enterprise value)."
    per = {"low": max(0.0, lo_v), "base": max(0.0, base_v), "high": max(0.0, hi_v)}
    out["per_share"] = {k: round(v, 2) for k, v in per.items()}
    if price and per["base"] and not 0.2 <= per["base"] / price <= 5:
        out["note"] = (
            (out.get("note") or "") + " Low confidence: the model value is more than 5x away from the "
            "market price, so check the inputs (book value basis, one-offs, share count)."
        ).strip()
    if price:
        out["vs_price"] = {k: round(v / price - 1, 4) for k, v in per.items()}
    # sensitivity basis: what the market multiple is applied to (used for "at the current multiple" impacts)
    basis = {
        "p_tbv": "net_income",
        "p_b": "net_income",
        "p_b_roe": "net_income",
        "ev_ebitda": "ebitda",
        "ev_revenue": "revenue",
    }[method]
    out["sensitivity_basis"] = basis
    out["sensitivity_metric"] = n.get(basis)
    return out


def apply_valuation(models: list[CompanyModel], overlays: dict[str, dict]) -> dict:
    stats = peer_stats(models)
    for m in models:
        m.valuation = value(m, stats, overlays.get(m.ticker))
    return stats
