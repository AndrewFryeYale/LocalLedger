"""Tokenomics: measured unit costs (from `ll bench` runs and the ledger) rolled up to per-run, per-user and
organization-scale numbers. `ll tokenomics` -> out/tokenomics/tokenomics.json + tokenomics.md.

Everything here is computed from measurements; the only assumptions are the usage mix and volumes in USAGE, which
are stated in the output so a reader can change them.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from sqlalchemy import Integer, cast, func
from sqlmodel import Session, select

from localledger.bench import BENCH_OUTREACH, BENCH_SIGNALS, bench_dir
from localledger.config import get_settings
from localledger.models import LlmCall, ModelImpact
from localledger.warroom.board import downstream

# Stated assumptions (weekly pipeline volumes come from the current feed; see the md output)
USAGE = {
    "notes_per_week": 20,  # MAX_NOTES_PER_RUN: the weekly cap; the feed averaged ~5 new note-worthy documents/week
    "model_llm_updates_per_week": 15,  # MAX_MODEL_UPDATES_PER_RUN (rules handle the rest)
    "deep_notes_per_week": 1,
    "warrooms_per_fund_per_month": 8,  # a fund covering ~25 names, earnings season averaged over the quarter
    "redirects_per_warroom": 2,  # each redirect reruns the target agent and everything downstream
    "outreach_drafts_per_month": 50,
    "mix": {"free": 0.90, "analyst": 0.09, "fund": 0.01},
    "weeks_per_month": 4.33,
}
REDIRECT_TARGET = "bear"  # the usual redirect ("rerun the bear case assuming ..."); its cost is measured below


def _runs(prefix: str) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(bench_dir().glob(f"{prefix}*/result.json"))]


def _per_prompt_cost(runs: list[dict], prompts: list[str]) -> float:
    vals = [sum(r["by_prompt"].get(p, {}).get("cost_usd", 0.0) for p in prompts) for r in runs]
    return statistics.mean(vals) if vals else 0.0


def unit_costs(runs: list[dict]) -> dict:
    n = len(BENCH_SIGNALS)
    warroom = [p for p in (runs[0]["by_prompt"] if runs else {}) if p.startswith("warroom_")]
    room = _per_prompt_cost(runs, warroom)
    return {
        "analyst_note_with_factcheck": _per_prompt_cost(runs, ["analyst_signal_note", "factcheck_claims"]) / n,
        "warroom_full_run": room,
        "warroom_redirect": _per_prompt_cost(runs, [f"warroom_{a}" for a in downstream(REDIRECT_TARGET)]),
        "outreach_draft": _per_prompt_cost(runs, ["outreach_email"]) / BENCH_OUTREACH,
        "workload_total": statistics.mean(r["totals"]["cost_usd"] for r in runs) if runs else 0.0,
        "tokens_in": statistics.mean(r["totals"]["input_tokens"] for r in runs) if runs else 0,
        "tokens_cached_in": statistics.mean(r["totals"]["cached_input_tokens"] for r in runs) if runs else 0,
        "tokens_out": statistics.mean(r["totals"]["output_tokens"] for r in runs) if runs else 0,
        "tokens_reasoning": statistics.mean(r["totals"]["reasoning_tokens"] for r in runs) if runs else 0,
        "wall_seconds": statistics.mean(r["wall_seconds"] for r in runs) if runs else 0,
        "runs": [r["label"] for r in runs],
    }


def ledger_stats(session: Session) -> dict:
    rows = session.exec(
        select(LlmCall.prompt_name, func.count(LlmCall.id), func.sum(cast(LlmCall.cached, Integer)))
        .where(LlmCall.provider == "portkey")
        .group_by(LlmCall.prompt_name)
    ).all()
    model_methods = session.exec(
        select(ModelImpact.method, func.count(ModelImpact.id)).group_by(ModelImpact.method)
    ).all()
    total_impacts = sum(n for _, n in model_methods)
    llm_impacts = sum(n for m, n in model_methods if m == "llm")
    calls = sum(r[1] for r in rows)
    cached = sum(r[2] or 0 for r in rows)
    return {
        "calls": calls,
        "disk_cache_hits": cached,
        "disk_cache_hit_rate": cached / calls if calls else None,
        "model_impacts": total_impacts,
        "model_impacts_by_llm": llm_impacts,
        "model_impacts_without_llm_share": 1 - llm_impacts / total_impacts if total_impacts else None,
        "methods": dict(model_methods),
    }


def model_update_cost(session: Session) -> float:
    """Mean live model_update call, repriced from its tokens at today's prices (older ledger rows used placeholder
    prices)."""
    s = get_settings()
    toks = session.exec(
        select(LlmCall.input_tokens, LlmCall.output_tokens).where(
            LlmCall.prompt_name == "model_update",
            LlmCall.provider == "portkey",
            LlmCall.cached == False,  # noqa: E712
        )
    ).all()
    costs = [(i * s.price_in_per_mtok + o * s.price_out_per_mtok) / 1e6 for i, o in toks if i]
    return statistics.mean(costs) if costs else 0.0


def scenarios(unit: dict, model_update: float) -> dict:
    u = USAGE
    weekly = (
        u["notes_per_week"] * unit["analyst_note_with_factcheck"]
        + u["model_llm_updates_per_week"] * model_update
        + u["deep_notes_per_week"] * unit["analyst_note_with_factcheck"] * 2.5  # deep note ~2.5x a signal note
    )
    pipeline_month = weekly * u["weeks_per_month"]
    fund_month = u["warrooms_per_fund_per_month"] * (
        unit["warroom_full_run"] + u["redirects_per_warroom"] * unit["warroom_redirect"]
    )
    out = {"pipeline_per_week": weekly, "pipeline_per_month": pipeline_month, "fund_variable_per_month": fund_month}
    for users in (1, 100, 10_000):
        funds = users * u["mix"]["fund"]
        total = pipeline_month + funds * fund_month + u["outreach_drafts_per_month"] * unit["outreach_draft"]
        out[f"users_{users}"] = {
            "funds": funds,
            "llm_cost_per_month": total,
            "llm_cost_per_user_per_month": total / users,
        }
    out["per_user_month"] = {
        "free_reader": 0.0,  # the newsletter is computed once and sent to everyone
        "analyst_99": 0.0,  # the app serves precomputed notes and models; no per-user LLM calls
        "fund_pilot": fund_month,
    }
    return out


def build(session: Session) -> dict:
    baseline, tuned = _runs("a_baseline"), _runs("d_tuned")
    ub, ut = unit_costs(baseline), unit_costs(tuned)
    mu = model_update_cost(session)
    result = {
        "model": get_settings().model_fast,
        "prices_per_mtok": {
            "input": get_settings().price_in_per_mtok,
            "cached_input": get_settings().price_cached_in_per_mtok,
            "output": get_settings().price_out_per_mtok,
        },
        "usage_assumptions": USAGE,
        "baseline": ub,
        "tuned": ut,
        "savings": {
            k: 1 - ut[k] / ub[k]
            for k in ("workload_total", "analyst_note_with_factcheck", "warroom_full_run", "outreach_draft")
            if ub.get(k)
        },
        "model_update_call": mu,
        "ledger": ledger_stats(session),
        "scenarios_baseline": scenarios(ub, mu),
        "scenarios_tuned": scenarios(ut, mu),
        "bench_runs": {r["label"]: r for r in _runs("")},
    }
    d = get_settings().out_dir / "tokenomics"
    d.mkdir(parents=True, exist_ok=True)
    (d / "tokenomics.json").write_text(json.dumps(result, indent=1))
    (d / "tokenomics.md").write_text(render_md(result))
    return result


def _usd(x: float) -> str:
    return f"${x:,.4f}" if x < 1 else f"${x:,.2f}"


def render_md(r: dict) -> str:
    b, t, st = r["baseline"], r["tuned"], r["scenarios_tuned"]
    lines = [
        "# Tokenomics",
        "",
        f"Model {r['model']}; prices per million tokens: input ${r['prices_per_mtok']['input']}, cached input "
        f"${r['prices_per_mtok']['cached_input']}, output ${r['prices_per_mtok']['output']}.",
        "",
        "| unit (measured, mean of runs) | baseline | tuned | saving |",
        "|---|---|---|---|",
    ]
    for k, label in (
        ("analyst_note_with_factcheck", "analyst note + fact-check"),
        ("warroom_full_run", "war room, full run (8 agents + fact-check)"),
        ("outreach_draft", "outreach draft"),
        ("workload_total", "benchmark workload"),
    ):
        lines.append(f"| {label} | {_usd(b[k])} | {_usd(t[k])} | {r['savings'].get(k, 0):.0%} |")
    lines += [
        f"| output tokens per workload | {b['tokens_out']:,.0f} | {t['tokens_out']:,.0f} | |",
        f"| ...of which reasoning | {b['tokens_reasoning']:,.0f} | {t['tokens_reasoning']:,.0f} | |",
        f"| wall time per workload | {b['wall_seconds']:.0f}s | {t['wall_seconds']:.0f}s | |",
        "",
        "| scale (tuned) | LLM cost / month | per user / month |",
        "|---|---|---|",
    ]
    for users in (1, 100, 10_000):
        s = st[f"users_{users}"]
        lines.append(
            f"| {users:,} users | {_usd(s['llm_cost_per_month'])} | {_usd(s['llm_cost_per_user_per_month'])} |"
        )
    led = r["ledger"]
    lines += [
        "",
        f"Weekly research pipeline: {_usd(st['pipeline_per_week'])}; fund pilot variable cost "
        f"{_usd(st['fund_variable_per_month'])}/month.",
        f"Model updates handled without an LLM: {led['model_impacts_without_llm_share']:.1%} of "
        f"{led['model_impacts']} replayed signals. Disk-cache hit rate across all live calls: "
        f"{led['disk_cache_hit_rate']:.0%}.",
    ]
    return "\n".join(lines) + "\n"


def write(session: Session) -> Path:
    build(session)
    return get_settings().out_dir / "tokenomics" / "tokenomics.md"
