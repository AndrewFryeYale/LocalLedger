"""The "so what": translate each signal into model adjustments, then diff the model before/after.

Deterministic rules come first; each states its assumptions in plain English. Signals no rule covers can go to a
bounded LLM updater (prompts/model_update.md) whose proposals must quote the source verbatim and stay inside hard
limits. Every impact records revenue estimates per quarter (the quarter the news lands in vs the next),
P(revenue up y/y), and value per share, before and after.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import date

from localledger.llm.mock import register_mock
from localledger.modeling.engine import Adjustment, CompanyModel, Event, recompute, resolve_event_quarters, value
from localledger.research.factcheck import normalize

APPROVAL_LABELS = {
    "approval",
    "public hearing",
    "entitlement",
    "rezoning",
    "ordinance",
    "vote",
    "contract",
    "subdivision",
    "plat",
    "resolution",
}
POSITIVE_ACTION = re.compile(r"\b(adopted|approved|passed|granted|carried|extend(?:ed)?)\b", re.IGNORECASE)
NEGATIVE_ACTION = re.compile(r"\b(tabled|denied|continued|postponed|withdrawn|rejected|failed)\b", re.IGNORECASE)
EDGAR_RISK = {
    "8k_item_3.01": 0.01,
    "8k_item_4.02": 0.015,
    "8k_item_1.03": 0.05,
    "form_nt_10-k": 0.0075,
    "form_nt_10-q": 0.005,
    "8k_item_4.01": 0.005,
}
LLM_METRICS = {
    "8k_item_1.01",
    "8k_item_1.02",
    "8k_item_2.01",
    "8k_item_2.05",
    "8k_item_2.06",
    "agenda_match",
    "form_schedule_13d",
    "form_prec14a",
    "form_defc14a",
}
NOTE_ONLY = {
    "revenue_yoy": "Reported actuals are already in the base model (XBRL company facts).",
    "net_income_yoy": "Reported actuals are already in the base model (XBRL company facts).",
    "earnings_release": "Reported results flow into the base model when the 10-Q/10-K XBRL lands.",
    "gaming_agr_ytd_yoy": "Captured by the monthly AGR signal (no double count).",
    "gaming_handle_month_yoy": "Handle informs hold, not revenue directly; monthly AGR carries the revenue effect.",
    "og_production_month_yoy": "Captured by the 3-month production signal (no double count).",
    "og_production_3m_yoy": "Year-over-year view; the sequential 3-month production signal drives the model "
    "(forecasts are anchored on last quarter's run-rate).",
    "permits_units_month_yoy": "Monthly permits are noisy; the year-to-date permits signal carries the model effect.",
    "8k_item_5.02": "Leadership change: no direct revenue effect modeled.",
    "8k_item_5.01": "Change in control: tracked; no revenue effect modeled.",
}


def _quarter_q(m: CompanyModel) -> float:
    return (1 + m.trend_growth) ** 0.25 - 1


def _future_after(m: CompanyModel, day: date) -> list[str]:
    return [q.end for q in m.future_quarters_after(day)]


def _signal_day(signal) -> date:
    period = str(signal.period or "")
    month = re.search(r"(\d{4})-(\d{2})$", period)
    if month and signal.source_id in ("gaming_state",):
        return date(int(month.group(1)), int(month.group(2)), 15)
    return signal.as_of


def rule(m: CompanyModel, signal, detail: dict, text: str = "") -> tuple[list[Adjustment], str, str]:
    """(adjustments, method, explanation). method 'llm' means: no rule; eligible for the LLM updater."""
    metric = signal.metric
    if metric in NOTE_ONLY:
        return [], "note", NOTE_ONLY[metric]
    if m.status == "no_xbrl":
        if metric == "og_implied_distribution_direction":
            return (
                [],
                "note",
                (
                    f"Trust distributions aren't in XBRL; implied next distribution: "
                    f"{detail.get('direction', 'n/a')} (see signal)."
                ),
            )
        return [], "note", "No XBRL financial model for this company (e.g. a royalty trust); tracked via signals."
    day = _signal_day(signal)
    tag = f"signal:{signal.id}"

    if metric == "net_loans_qoq":
        g = detail.get("total_loans_pct_change") if (detail.get("held_for_sale_share") or 0) > 0.05 else None
        g = g if g is not None else (detail.get("pct_change") or 0.0)
        g = max(-0.2, min(0.2, g))
        inc = g - _quarter_q(m)
        if abs(inc) < 0.005:
            return [], "rule:loans", f"Loans {g:+.1%} q/q, in line with the ~{_quarter_q(m):+.1%}/qtr trend: no change."
        fut = _future_after(m, day)
        if not fut:
            return [], "note", "No future quarters to adjust."
        f1, f2 = 1 + 0.5 * 0.85 * inc, 1 + 0.85 * inc
        adj = [Adjustment("factor", f1, [fut[0]], f"loans {g:+.1%} q/q vs trend: x{f1:.3f}", tag)]
        if len(fut) > 1:
            adj.append(Adjustment("factor", f2, fut[1:], f"loans {g:+.1%} q/q vs trend: x{f2:.3f}", tag))
        return (
            adj,
            "rule:loans",
            (
                f"Loans {g:+.1%} q/q at {signal.as_of} vs a ~{_quarter_q(m):+.1%}/qtr trend: earning "
                f"assets drive ~85% of bank revenue, so revenue x{f1:.3f} next quarter (half-quarter "
                f"effect) and x{f2:.3f} after; assumes stable yields."
            ),
        )

    if metric == "deposit_mix":
        pts = detail.get("change_pts") or 0.0
        if abs(pts) < 1.0:
            return [], "rule:deposits", f"Noninterest-bearing share moved {pts:+.1f} pts: below the 1-pt threshold."
        nii_pct = (pts / 100) * 0.03 / 0.035  # mix shift x ~3% rate gap / ~3.5% NIM
        f = 1 + 0.85 * nii_pct
        fut = _future_after(m, day)
        return (
            [Adjustment("factor", f, fut, f"deposit mix {pts:+.1f} pts: x{f:.3f}", tag)],
            "rule:deposits",
            f"Noninterest-bearing deposits {pts:+.1f} pts of total: funding cost moves ~{nii_pct:+.1%} of NII "
            f"(assumes a ~3% rate gap and ~3.5% NIM), revenue x{f:.3f} in future quarters.",
        )

    if metric == "noncurrent_loans":
        pts = detail.get("change_pts") or 0.0
        adj = []
        if pts >= 0.5:
            prem = 0.01 if pts >= 1.0 else 0.005
            adj.append(Adjustment("risk", prem, [], f"noncurrent loans +{pts:.2f} pts: +{prem * 1e4:.0f} bp", tag))
            cur = detail.get("noncurrent_usd") or 0.0
            chg = detail.get("noncurrent_pct_change")
            prior = cur / (1 + chg) if chg is not None and chg > -1 else cur
            hit = -0.25 * 0.75 * max(0.0, cur - prior)
            fut = _future_after(m, day)
            if fut and hit:
                adj.append(
                    Adjustment("ni_add", hit, [fut[0]], f"provision on new noncurrent loans {hit / 1e6:,.2f}M", tag)
                )
            return (
                adj,
                "rule:credit",
                (
                    f"Noncurrent loans up {pts:.2f} pts: +{prem * 1e4:.0f} bp cost of equity, and "
                    f"a provision of ~25% loss severity on the increase (after tax) next quarter."
                ),
            )
        if pts <= -0.5:
            return (
                [Adjustment("risk", -0.0025, [], f"noncurrent loans {pts:.2f} pts: -25 bp", tag)],
                "rule:credit",
                f"Noncurrent loans down {abs(pts):.2f} pts: -25 bp cost of equity.",
            )
        return [], "rule:credit", f"Noncurrent loans moved {pts:+.2f} pts: below the 0.5-pt threshold."

    if metric == "cre_concentration":
        ratio, prior = (signal.value or 0) * 100, (signal.prior_value or 0) * 100
        if ratio >= 300 > prior:
            prem, why = 0.0075, f"crossed the 300% guideline ({prior:.0f}% -> {ratio:.0f}%)"
        elif ratio >= 300 and ratio > prior:
            prem, why = 0.0025, f"above 300% and rising ({prior:.0f}% -> {ratio:.0f}%)"
        elif prior >= 300 > ratio:
            prem, why = -0.0025, f"fell back below 300% ({prior:.0f}% -> {ratio:.0f}%)"
        else:
            return [], "rule:cre", f"CRE concentration {ratio:.0f}% of capital: no change to risk premium."
        return (
            [Adjustment("risk", prem, [], f"CRE {why}: {prem * 1e4:+.0f} bp", tag)],
            "rule:cre",
            f"CRE concentration {why}: {prem * 1e4:+.0f} bp cost of equity (regulatory scrutiny risk).",
        )

    if metric == "gaming_agr_month_yoy":
        q = m.quarter_containing(day)
        if q is None:
            return [], "note", "Month falls in an already-reported quarter; it's in the actuals."
        cur, prior = signal.value or 0.0, signal.prior_value or 0.0
        delta = (cur - prior) - prior * m.trend_growth
        return (
            [
                Adjustment(
                    "add", delta, [q.end], f"{detail.get('property', 'property')} AGR vs trend {delta / 1e6:+.2f}M", tag
                )
            ],
            "rule:gaming",
            f"{detail.get('property', 'Property')} {day:%b %Y} AGR ${cur / 1e6:.2f}M vs ${prior / 1e6:.2f}M a year "
            f"earlier; vs the company's {m.trend_growth:+.1%} trend that's {delta / 1e6:+.2f}M for the "
            f"{q.label} (AGR as a proxy for net gaming revenue).",
        )

    if metric == "og_implied_distribution_direction" and m.status == "trust":
        pct = max(-0.5, min(0.5, detail.get("pct_change") or 0.0))
        fut = _future_after(m, day)[:1]
        if not fut or abs(pct) < 0.02:
            return (
                [],
                "rule:distribution",
                (
                    f"Implied next distribution {detail.get('direction', 'n/a')}; "
                    "below the 2% threshold or no quarter to adjust."
                ),
            )
        f = 1 + 0.8 * pct
        return (
            [Adjustment("factor", f, fut, f"production feeding next distributions {pct:+.1%}: x{f:.3f}", tag)],
            "rule:distribution",
            f"Production feeding the next distributions {pct:+.1%} (operator proxy, 2-month lag): distributable "
            f"income x{f:.3f} next quarter (80% pass-through; prices and costs held flat).",
        )

    if metric == "og_production_3m_vs_prior":
        pct = max(-0.4, min(0.4, detail.get("pct_change") or 0.0))
        if abs(pct) < 0.02:
            return [], "rule:production", f"Production {pct:+.1%} vs the prior 3 months: below the 2% threshold."
        f = max(0.75, min(1.25, 1 + 0.6 * pct))
        fut = _future_after(m, day)[:3]
        return (
            [Adjustment("factor", f, fut, f"NM production {pct:+.1%} vs prior 3 months: x{f:.3f}", tag)],
            "rule:production",
            f"Operator NM production {pct:+.1%} in the latest 3 months vs the prior 3: revenue x{f:.3f} for the "
            "next 3 quarters (forecasts are anchored on last quarter's run-rate; 60% weight because NM's share of "
            "revenue isn't split out; prices held flat).",
        )

    if metric == "permits_units_ytd_yoy":
        pct = detail.get("pct_change") or 0.0
        e = {"Regulated utility": 0.15, "Land / entitlement developer": 0.25}.get(m.archetype, 0.2)
        f = max(0.85, min(1.15, 1 + e * pct))
        fut = _future_after(m, day)[1:4]
        return (
            [Adjustment("factor", f, fut, f"permits YTD {pct:+.1%}: x{f:.3f}", tag)],
            "rule:permits",
            f"{detail.get('geo_name', 'Area')} permits YTD {pct:+.1%} y/y: revenue x{f:.3f} two to four quarters "
            f"out (elasticity {e} for {m.archetype.lower()}; permits lead taps and lot sales).",
        )

    if metric in EDGAR_RISK:
        t = text.lower()
        if "regain" in t and "compliance" in t:
            if m.risk_premium <= 0:
                return [], "note", "Regained listing compliance; no listing-risk premium was being applied."
            return (
                [Adjustment("risk", -0.005, [], "regained listing compliance: -50 bp", tag)],
                "rule:filing",
                "Regained listing compliance: -50 bp cost of equity.",
            )
        if metric == "8k_item_3.01" and "voluntar" in t:
            return [], "note", "Voluntary delisting (strategic, e.g. a liquidation): no risk-premium change modeled."
        prem = EDGAR_RISK[metric]
        return (
            [Adjustment("risk", prem, [], f"{metric}: +{prem * 1e4:.0f} bp", tag)],
            "rule:filing",
            f"{signal.summary.rstrip('.')}: +{prem * 1e4:.0f} bp cost of equity / multiple haircut.",
        )

    if metric == "agenda_match":
        return agenda_rule(m, signal, detail, text)

    if metric in LLM_METRICS:
        return [], "llm", "No deterministic rule; eligible for the LLM model updater."
    return [], "note", "No model effect defined for this signal type."


def agenda_rule(m: CompanyModel, signal, detail: dict, text: str) -> tuple[list[Adjustment], str, str]:
    kws = {k.lower() for k in detail.get("keywords", [])}
    labels = {i.get("label", "") for i in detail.get("items", []) if isinstance(i, dict)}
    events = [e for e in m.events if {k.lower() for k in e.keywords} & kws]
    if not events:
        return [], "llm", "Agenda item not linked to a modeled event; eligible for the LLM model updater."
    excerpt = detail.get("excerpt", "") + " " + text
    step = 0.2 if labels & APPROVAL_LABELS else 0.1
    sign = -1 if NEGATIVE_ACTION.search(excerpt) and not POSITIVE_ACTION.search(excerpt) else 1
    meeting = date.fromisoformat(str(detail.get("meeting_date") or signal.as_of)[:10])
    adj = [
        Adjustment(
            "event_shift",
            sign * step,
            [],
            f"{e.name}: {'earlier' if sign > 0 else 'later'} by {step:.0%}",
            f"signal:{signal.id}",
            {"name": e.name, "after": meeting.isoformat()},
        )
        for e in events
    ]
    return (
        adj,
        "rule:agenda",
        (
            f"{', '.join(sorted(labels)) or 'agenda'} item on "
            f"{'/'.join(sorted(kws))} ({'positive' if sign > 0 else 'negative'} action): shifts "
            f"{step:.0%} of the probability of {', '.join(e.name for e in events)} "
            f"{'into the nearest quarter' if sign > 0 else 'out to later quarters'}."
        ),
    )


# --- applying adjustments ------------------------------------------------------------------------------
def _shift_event(e: Event, step: float, after: date, ends: list[str]) -> None:
    """Positive news pulls `step` of the *remaining later* probability into the nearest quarter on/after `after`
    (so repeated items converge instead of saturating); negative news pushes `step` of it one quarter later."""
    ends = [x for x in ends if date.fromisoformat(x) >= after]
    if not ends:
        return
    target = ends[0]
    if step > 0:
        later = [x for x in ends[1:] if e.probs.get(x, 0.0) > 0]
        pool = sum(e.probs[x] for x in later)
        if pool:
            for x in later:
                e.probs[x] -= step * e.probs[x]
            e.probs[target] = e.probs.get(target, 0.0) + step * pool
        else:
            e.probs[target] = e.probs.get(target, 0.0) + step * max(0.0, 1.0 - sum(e.probs.values()))
    else:
        move = -step * e.probs.get(target, 0.0)
        e.probs[target] = e.probs.get(target, 0.0) - move
        if len(ends) > 1:
            e.probs[ends[1]] = e.probs.get(ends[1], 0.0) + move


def apply_adjustments(m: CompanyModel, adjustments: list[Adjustment]) -> CompanyModel:
    out = copy.deepcopy(m)
    ends = [q.end for q in out.forecast]
    for a in adjustments:
        if a.kind in ("factor", "add", "ni_add"):
            out.adjustments.append(a)
        elif a.kind == "risk":
            out.risk_premium = round(max(0.0, min(0.06, out.risk_premium + a.value)), 5)
        elif a.kind == "event_shift" and a.event:
            for e in out.events:
                if e.name == a.event.get("name"):
                    after = max(date.fromisoformat(a.event["after"]), date.fromisoformat(out.as_of))
                    _shift_event(e, a.value, after, ends)
        elif a.kind == "event_new" and a.event:
            ev = a.event
            out.events.append(
                Event(
                    name=ev["name"],
                    amount=float(ev.get("amount", 0)),
                    kind=ev.get("kind", "revenue"),
                    probs={str(k): float(v) for k, v in (ev.get("probs") or {}).items()},
                    keywords=list(ev.get("keywords", [])),
                    note=a.note,
                    source=a.source,
                )
            )
            resolve_event_quarters(out)
    return recompute(out)


def diff(before: CompanyModel, after: CompanyModel) -> dict:
    qs = []
    for b, a in zip(before.visible_quarters(), after.visible_quarters(), strict=False):
        qs.append(
            {
                "label": a.label,
                "status": a.status,
                "end": a.end,
                "p50_before": b.p50,
                "p50_after": a.p50,
                "p10_after": a.p10,
                "p90_after": a.p90,
                "p_yoy_before": b.p_yoy,
                "p_yoy_after": a.p_yoy,
                "prior_year": a.prior_year,
            }
        )
    vb, va = before.valuation, after.valuation
    price = va.get("price")
    sens = None
    mb, ma = vb.get("sensitivity_metric"), va.get("sensitivity_metric")
    if price and mb and ma and mb > 0:
        sens = {
            "per_share": round(price * (ma / mb - 1), 4),
            "pct": round(ma / mb - 1, 5),
            "basis": va.get("sensitivity_basis"),
        }
    fair = None
    if vb.get("per_share") and va.get("per_share"):
        fair = {"before": vb["per_share"]["base"], "after": va["per_share"]["base"]}
    labels = {q.end: q.label for q in after.forecast}
    events = []
    before_events = {e.name: e for e in before.events}
    for e in after.events:
        prior = before_events.get(e.name)
        for end, p in e.probs.items():
            p0 = prior.probs.get(end, 0.0) if prior else 0.0
            if abs(p - p0) >= 0.01:
                events.append(
                    {
                        "name": e.name,
                        "kind": e.kind,
                        "quarter": labels.get(end, end),
                        "before": round(p0, 3),
                        "after": round(p, 3),
                    }
                )
    return {
        "quarters": qs,
        "sensitivity": sens,
        "fair_value": fair,
        "events": events,
        "risk_before": before.risk_premium,
        "risk_after": after.risk_premium,
    }


def _money(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"${v / 1e6:,.1f}M" if abs(v) >= 1e5 else f"${v:,.0f}"


MATERIAL = 0.005  # moves under 0.5% are reported as "no material change"
STATUS_WORD = {"unreported": "ended, not yet reported", "current": "current quarter", "next": "next quarter"}


def summarize(impact: dict, explanation: str, public: bool) -> str:
    parts = []
    for q in impact["quarters"]:
        b, a = q["p50_before"], q["p50_after"]
        moved = b and abs(a / b - 1) >= MATERIAL
        yoy = (
            q["p_yoy_before"] is not None
            and q["p_yoy_after"] is not None
            and abs(q["p_yoy_after"] - q["p_yoy_before"]) >= 0.01
        )
        if not (moved or yoy):
            continue
        s = f"{q['label']} ({STATUS_WORD.get(q['status'], q['status'])}) revenue estimate {_money(b)} -> {_money(a)}"
        if moved:
            s += f" ({a / b - 1:+.1%})"
        if q["p_yoy_after"] is not None:
            s += f"; P(up y/y) {q['p_yoy_before']:.0%} -> {q['p_yoy_after']:.0%}"
        parts.append(s)
    for ev in sorted(impact.get("events", []), key=lambda e: -abs(e["after"] - e["before"]))[:2]:
        parts.append(f"P({ev['name']} in the {ev['quarter']}) {ev['before']:.0%} -> {ev['after']:.0%}")
    risk = impact["risk_after"] - impact["risk_before"]
    if abs(risk) >= 0.0001:
        parts.append(f"risk premium in our valuation {risk * 1e4:+.0f} bp")
    sens = impact.get("sensitivity")
    if sens and abs(sens["pct"]) >= MATERIAL:
        parts.append(
            f"value sensitivity at the current multiple {sens['per_share']:+.2f}/share ({sens['pct']:+.1%} "
            f"of NTM {sens['basis'].replace('_', ' ')})"
        )
    if (
        not public
        and impact.get("fair_value")
        and abs(impact["fair_value"]["after"] - impact["fair_value"]["before"]) >= 0.005
    ):
        fv = impact["fair_value"]
        parts.append(f"model value (base) ${fv['before']:.2f} -> ${fv['after']:.2f}/share")
    if not parts:
        return f"No change to modeled revenue or value. {explanation}"
    return "; ".join(parts) + f". Why: {explanation}"


def compute_impact(
    before: CompanyModel,
    adjustments: list[Adjustment],
    stats: dict,
    overlay: dict | None,
    explanation: str,
    method: str,
) -> tuple[CompanyModel, dict]:
    after = apply_adjustments(before, adjustments)
    after.valuation = value(after, stats, overlay)
    impact = diff(before, after)
    impact.update(
        method=method,
        explanation=explanation,
        adjustments=[{"kind": a.kind, "value": a.value, "quarters": a.quarters, "note": a.note} for a in adjustments],
    )
    impact["summary_public"] = summarize(impact, explanation, public=True)
    impact["summary_app"] = summarize(impact, explanation, public=False)
    return after, impact


# --- LLM updater (bounded, quote-verified) ---------------------------------------------------------------
def llm_adjustments(
    llm, m: CompanyModel, signal, doc_text: str, excerpt: str, doc_url: str, brand: str
) -> tuple[list[Adjustment], str]:
    quarters = [{"key": q.end, "label": q.label, "status": q.status, "p50": q.p50} for q in m.visible_quarters()]
    variables = {
        "brand": brand,
        "company": {"ticker": m.ticker, "name": m.name, "archetype": m.archetype},
        "ttm": m.ttm,
        "quarters": quarters,
        "history": m.history[-4:],
        "events": [{"name": e.name, "amount": e.amount, "probs": e.probs} for e in m.events],
        "signal": {"metric": signal.metric, "as_of": str(signal.as_of), "summary": signal.summary},
        "excerpt": excerpt[:6000],
        "url": doc_url,
    }
    res = llm.json("model_update", variables)
    base = {q.end: q.base for q in m.forecast}
    corpus = normalize(doc_text + "\n" + excerpt)
    out, notes = [], []
    for item in (res.data.get("adjustments") or [])[:3]:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote", ""))
        if len(quote.split()) < 4 or normalize(quote) not in corpus:
            notes.append("dropped an adjustment whose quote isn't verbatim in the source")
            continue
        kind, keys = item.get("kind"), item.get("quarters") or []
        keys = [k for k in (keys if isinstance(keys, list) else [keys]) if k in base]
        why = str(item.get("rationale", ""))[:300]
        tag = f"llm:signal:{signal.id}"
        if kind == "factor" and 0.7 <= float(item.get("factor", 1)) <= 1.3:
            out.append(Adjustment("factor", float(item["factor"]), keys, why, tag))
        elif kind == "add" and keys:
            amt = float(item.get("amount_usd", 0))
            cap = 0.5 * max(base[k] for k in keys)
            out.append(Adjustment("add", max(-cap, min(cap, amt)), keys, why, tag))
        elif kind == "risk" and -0.01 <= float(item.get("premium", 0)) <= 0.03:
            out.append(Adjustment("risk", float(item["premium"]), [], why, tag))
        elif kind == "event" and isinstance(item.get("event"), dict):
            ev = item["event"]
            amt = float(ev.get("amount_usd", 0))
            probs = {k: max(0.0, min(1.0, float(v))) for k, v in (ev.get("probabilities") or {}).items() if k in base}
            if 0 < amt <= 2 * (m.ttm.get("revenue") or amt) and probs and sum(probs.values()) <= 1.0:
                out.append(
                    Adjustment(
                        "event_new",
                        0.0,
                        [],
                        why,
                        tag,
                        {
                            "name": str(ev.get("name", "event"))[:80],
                            "amount": amt,
                            "probs": probs,
                            "keywords": [str(k) for k in ev.get("keywords", [])][:5],
                        },
                    )
                )
    so_what = str(res.data.get("so_what", "")).strip()
    explanation = so_what or "LLM model updater found no quantifiable effect."
    if notes:
        explanation += f" ({'; '.join(sorted(set(notes)))})"
    return out, explanation


@register_mock("model_update")
def _mock_model_update(variables: dict) -> dict:
    return {"adjustments": [], "so_what": "No quantifiable model effect identified (deterministic mode)."}


def as_json(d: dict) -> str:
    return json.dumps(d, default=str)
