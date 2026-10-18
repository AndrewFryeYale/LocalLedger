"""Token benchmark: one fixed, realistic workload run live against a sandbox copy of the database.

Workload (same every time): 8 analyst notes with fact-check across five sources, one full war-room replay (PCYO),
and 5 outreach drafts. Each run gets a fresh disk cache so every call is live, its own copy of the database (so
notes and rooms it writes never touch the real ones), and the live ledger rows are copied back into the real
ledger afterwards so the spend still counts toward LLM_BUDGET_USD.

`ll bench --label NAME [--effort minimal|low|medium|high|default|prompt]` -> out/bench/NAME/result.json, and
out/bench/summary.md compares every run recorded so far.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from localledger.config import get_settings

BENCH_SIGNALS = [37, 210, 943, 970, 1057, 1065, 609, 1058]  # ELME, EONR, PBFS, PNBK, PCYO, MLP, EP, GYRO
BENCH_WARROOM = "PCYO"
BENCH_OUTREACH = 5


def bench_dir() -> Path:
    return get_settings().out_dir / "bench"


def _sqlite_path(url: str) -> Path:
    return Path(url.removeprefix("sqlite:///"))


def run_bench(label: str, effort: str) -> dict:
    settings = get_settings()
    sandbox = bench_dir() / label
    if sandbox.exists():
        shutil.rmtree(sandbox)
    (sandbox / "cache").mkdir(parents=True)
    src = sqlite3.connect(_sqlite_path(settings.database_url))
    dst = sqlite3.connect(sandbox / "bench.db")
    src.backup(dst)  # consistent copy, WAL included
    src.close()
    dst.close()
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{sandbox / 'bench.db'}",
        "LL_CACHE_DIR": str(sandbox / "cache"),
        "LL_OUT_DIR": str(sandbox / "out"),
        "LLM_REASONING_EFFORT": effort,
        "LL_FORCE_FRESH": "1",
    }
    started = time.time()
    ll = Path(sys.executable).parent / "ll"
    proc = subprocess.run([str(ll), "bench-worker", "--label", label], env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"bench worker failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    result = json.loads((sandbox / "result.json").read_text())
    result["wall_seconds"] = round(time.time() - started, 1)
    result["effort"] = effort
    (sandbox / "result.json").write_text(json.dumps(result, indent=2))
    _copy_ledger_back(sandbox / "bench.db", result["ledger_from_id"], label)
    write_summary()
    return result


def _copy_ledger_back(bench_db: Path, from_id: int, label: str) -> None:
    from localledger.db import session_scope
    from localledger.models import LlmCall

    con = sqlite3.connect(bench_db)
    con.row_factory = sqlite3.Row
    rows = con.execute("select * from llm_calls where id > ? and provider = 'portkey'", (from_id,)).fetchall()
    con.close()
    with session_scope() as s:
        for r in rows:
            data = {k: r[k] for k in r.keys() if k not in ("id", "created_at")}
            data["command"] = f"bench:{label}"
            s.add(LlmCall(**data))


def worker(label: str) -> None:
    """Runs inside the sandbox (env points DATABASE_URL / LL_CACHE_DIR at it)."""
    from sqlalchemy import func
    from sqlmodel import select

    from localledger.db import session_scope
    from localledger.gtm.outreach import build_outreach
    from localledger.llm.client import LLM
    from localledger.models import LlmCall, Note, Signal, WarroomSession
    from localledger.research.analyst import write_signal_note
    from localledger.warroom.crew import create_session, run_sync

    with session_scope() as s:
        from_id = s.exec(select(func.max(LlmCall.id))).one() or 0
    llm = LLM("bench")
    notes = []
    with session_scope() as s:
        for sid in BENCH_SIGNALS:
            sig = s.get(Signal, sid)
            note = write_signal_note(s, sig, llm)
            s.commit()
            dropped = json.loads(note.factcheck_log or "[]")
            notes.append(
                {
                    "signal_id": sid,
                    "ticker": sig.ticker,
                    "source": sig.source_id,
                    "kept": len(json.loads(note.claims_json or "[]")),
                    "dropped": len(dropped),
                    "dropped_reasons": [d.get("stage") or d.get("reason", "")[:40] for d in dropped],
                    "passed": note.factcheck_passed,
                }
            )
    room = create_session(BENCH_WARROOM, created_by="bench", title=f"{BENCH_WARROOM} bench replay")
    run_sync(room, actor="bench", fetch=False)
    with session_scope() as s:
        ws = s.get(WarroomSession, room)
        wnote = s.get(Note, ws.final_note_id) if ws.final_note_id else None
        warroom = {
            "status": ws.status,
            "kept": len(json.loads(wnote.claims_json or "[]")) if wnote else 0,
            "dropped": len(json.loads(wnote.factcheck_log or "[]")) if wnote else 0,
            "passed": bool(wnote and wnote.factcheck_passed),
        }
        out = build_outreach(s, llm, top=BENCH_OUTREACH)
        outreach = {"drafts": out.drafts, "template_fallbacks": out.fallbacks}
        rows = s.exec(select(LlmCall).where(LlmCall.id > from_id)).all()
    by_prompt: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        p = by_prompt[r.prompt_name]
        p["calls"] += 1
        p["live"] += r.provider == "portkey" and not r.cached
        p["mock"] += r.provider == "mock"
        for k in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "latency_ms"):
            p[k] += getattr(r, k)
        p["cost_usd"] += r.est_cost_usd
    totals = defaultdict(float)
    for p in by_prompt.values():
        for k, v in p.items():
            totals[k] += v
    result = {
        "label": label,
        "model": get_settings().model_fast,
        "ledger_from_id": from_id,
        "notes": notes,
        "warroom": warroom,
        "outreach": outreach,
        "by_prompt": {k: dict(v) for k, v in sorted(by_prompt.items())},
        "totals": dict(totals),
        "quality": {
            "notes_passed": sum(n["passed"] for n in notes),
            "notes": len(notes),
            "claims_kept": sum(n["kept"] for n in notes),
            "claims_written": sum(n["kept"] + n["dropped"] for n in notes),
        },
    }
    (get_settings().out_dir.parent / "result.json").write_text(json.dumps(result, indent=2))


def write_summary() -> Path:
    runs = []
    for path in sorted(bench_dir().glob("*/result.json")):
        runs.append(json.loads(path.read_text()))
    runs.sort(key=lambda r: r.get("order", 0) or r["label"])
    lines = [
        "# Token benchmark",
        "",
        f"Workload: {len(BENCH_SIGNALS)} analyst notes + fact-check, 1 war-room replay ({BENCH_WARROOM}), "
        f"{BENCH_OUTREACH} outreach drafts. Every call live (fresh cache). Costs at the ledger prices in .env.",
        "",
        "| run | effort | calls | input tok | cached in | output tok | reasoning | cost | wall s "
        "| notes passed | claims kept/written | war-room note |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        t, q, w = r["totals"], r["quality"], r["warroom"]
        lines.append(
            f"| {r['label']} | {r.get('effort', '')} | {int(t.get('calls', 0))} | {int(t.get('input_tokens', 0)):,} "
            f"| {int(t.get('cached_input_tokens', 0)):,} | {int(t.get('output_tokens', 0)):,} "
            f"| {int(t.get('reasoning_tokens', 0)):,} | ${t.get('cost_usd', 0):.4f} | {r.get('wall_seconds', '')} "
            f"| {q['notes_passed']}/{q['notes']} | {q['claims_kept']}/{q['claims_written']} "
            f"| {'passed' if w['passed'] else 'failed'} ({w['kept']} kept) |"
        )
    path = bench_dir() / "summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path
