"""`ll` command line. Every command degrades gracefully and prints its LLM token/cost estimate."""

from __future__ import annotations

import atexit
import logging
import subprocess
import sys
from typing import Annotated

import typer
from sqlalchemy import func
from sqlmodel import select

from localledger.config import get_settings
from localledger.db import init_db, session_scope
from localledger.llm.client import RUN_USAGE

app = typer.Typer(
    add_completion=False,
    pretty_exceptions_enable=False,
    help="Local Ledger: independent microcap research from public records.",
)


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    init_db()
    atexit.register(_print_usage)


def _print_usage() -> None:
    from localledger.llm.client import flush_pending

    try:
        flush_pending()
    except Exception:  # noqa: BLE001
        pass
    typer.echo(RUN_USAGE.summary())
    for note in RUN_USAGE.notes:
        typer.echo(f"  note: {note}")


def _csv(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


@app.command()
def seed() -> None:
    """Load the universe workbook (237 companies + archetypes) and register sources."""
    from localledger.connectors import REGISTRY, load_connector
    from localledger.connectors.base import ensure_source
    from localledger.seed import seed_universe

    settings = get_settings()
    with session_scope() as s:
        n, a = seed_universe(s, settings.universe_xlsx)
        for source_id in REGISTRY:
            ensure_source(s, load_connector(source_id))
    typer.echo(f"Seeded {n} companies and {a} archetypes from {settings.universe_xlsx.name}")


@app.command()
def coverage() -> None:
    """Run data/coverage_check.py (yfinance analyst counts). Failure is a warning, never fatal."""
    settings = get_settings()
    script = settings.data_dir / "coverage_check.py"
    xlsx = settings.data_dir / "Microcap_Local_Gov_Universe.xlsx"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), str(xlsx)],
            cwd=settings.data_dir,
            timeout=1800,
            capture_output=True,
            text=True,
        )
        tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        if proc.returncode == 0:
            typer.echo(f"coverage_check ok: {tail[0]}")
        else:
            typer.echo(
                f"WARNING: coverage_check failed (exit {proc.returncode}); continuing with sheet values. "
                f"{(proc.stderr or '').strip().splitlines()[-1:]}"
            )
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"WARNING: coverage_check could not run ({type(exc).__name__}); continuing.")


@app.command()
def ingest(
    source: Annotated[str | None, typer.Option(help="Comma list of source ids (default: all)")] = None,
    tickers: Annotated[str | None, typer.Option(help="Comma list of tickers to limit to")] = None,
    limit: Annotated[int | None, typer.Option(help="Max targets per connector")] = None,
) -> None:
    """Fetch public records from every connector and write cited signals."""
    from localledger.connectors import REGISTRY, load_connector
    from localledger.http import HttpClient

    http = HttpClient()
    for source_id in _csv(source) or list(REGISTRY):
        if source_id not in REGISTRY:
            typer.echo(f"unknown source {source_id}; known: {', '.join(REGISTRY)}")
            continue
        with session_scope() as s:
            connector = load_connector(source_id)(s, http=http)
            result = connector.run(tickers=_csv(tickers), limit=limit)
        typer.echo(result.line())
        for err in result.errors[:3]:
            typer.echo(f"    {err[:200]}")


@app.command()
def signals(
    limit: Annotated[int | None, typer.Option(help="Max analyst notes this run (default MAX_NOTES_PER_RUN)")] = None,
    threshold: Annotated[float | None, typer.Option(help="Min significance for a note")] = None,
    tickers: Annotated[str | None, typer.Option(help="Comma list of tickers")] = None,
    redo: Annotated[bool, typer.Option(help="Rewrite notes that already exist")] = False,
) -> None:
    """Re-rank every signal, then write fact-checked analyst notes for the top signals."""
    from localledger.llm.client import LLM
    from localledger.models import Company, Signal
    from localledger.research.analyst import run_research
    from localledger.signals.scoring import rank_score

    with session_scope() as s:
        priorities = {c.ticker: c.priority for c in s.exec(select(Company)).all()}
        rows = s.exec(select(Signal)).all()
        for sig in rows:
            sig.rank_score = rank_score(sig.significance, priorities.get(sig.ticker), sig.as_of)
            s.add(sig)
        s.commit()
        typer.echo(f"Re-ranked {len(rows)} signals.")
        summary = run_research(s, LLM("signals"), limit=limit, threshold=threshold, tickers=_csv(tickers), redo=redo)
    typer.echo(
        f"Analyst notes: {summary.attempted} attempted, {summary.passed} passed fact-check, "
        f"{summary.failed} failed; {summary.rejected_claims} claims rejected and logged."
    )
    for err in summary.errors[:5]:
        typer.echo(f"    {err[:220]}")


@app.command()
def newsletter(
    week: Annotated[str | None, typer.Option(help="ISO week, e.g. 2026-W42 (default: this week)")] = None,
    no_new_notes: Annotated[bool, typer.Option(help="Use existing notes only (no analyst calls)")] = False,
) -> None:
    """Build this week's issue (Markdown + email-safe HTML) in ./out/newsletter. Never sends anything."""
    from localledger.gtm.newsletter import build_issue
    from localledger.llm.client import LLM

    with session_scope() as s:
        result = build_issue(s, week, LLM("newsletter"), ensure_notes=not no_new_notes)
    typer.echo(
        f"Issue {result.week}: {result.items} signal items, fact-check "
        f"{'PASSED' if result.factcheck_passed else 'FAILED'}"
    )
    typer.echo(f"  markdown: {result.path_md}\n  html:     {result.path_html}")
    for line in result.excluded:
        typer.echo(f"  excluded by compliance: {line}")
    for line in result.warnings:
        typer.echo(f"  warning: {line}")
    typer.echo("  Not sent. Review, then paste the HTML into your email platform.")


@app.command()
def warroom(
    ticker: Annotated[str | None, typer.Option(help="Ticker to open a room on")] = None,
    replay: Annotated[bool, typer.Option(help="Replay the most recent real quarter end-to-end")] = False,
    session: Annotated[int | None, typer.Option(help="Existing room id (for --redirect)")] = None,
    redirect: Annotated[str | None, typer.Option(help="Redirect text to post into an existing room")] = None,
    agent: Annotated[str, typer.Option(help="Agent the redirect targets (or 'room')")] = "bear",
    actor: Annotated[str, typer.Option("--as", help="Name to post as")] = "cli",
    no_fetch: Annotated[bool, typer.Option(help="Don't fetch missing filings from EDGAR")] = False,
) -> None:
    """Earnings war room: the agent crew works a real 8-K Ex. 99.1 against the prior 10-Q, notes and signals."""
    from localledger.warroom import board
    from localledger.warroom.crew import create_session, run_sync

    def show(event) -> None:
        import json as _json

        p = _json.loads(event.payload_json or "{}")
        detail = p.get("text") or p.get("status") or p.get("error") or ""
        if event.type == "note":
            detail = f"{p.get('kept')} claims kept, {p.get('dropped')} rejected, fact-check " + (
                "PASSED" if p.get("passed") else "FAILED"
            )
        typer.echo(
            f"  [{event.created_at:%H:%M:%S}] {board.LABELS.get(event.agent or '', 'room'):12} {event.type:8} {detail}"
        )

    if session and redirect:
        target = agent if agent in board.AGENTS else "room"
        board.emit(session, "redirect", target, actor, text=redirect)
        agents = board.AGENTS if target == "room" else board.downstream(target)
        typer.echo(f"Redirect posted to room #{session} ({target}); rerunning {', '.join(agents)}")
        run_sync(session, agents, actor=actor, on_event=show, fetch=not no_fetch)
        room_id = session
    elif ticker:
        if not replay:
            typer.echo("Tip: --replay runs the full crew on the latest real quarter.")
        room_id = create_session(ticker, created_by=actor, title=f"{ticker.upper()} earnings replay")
        typer.echo(f"War room #{room_id} for {ticker.upper()}: running the crew")
        run_sync(room_id, actor=actor, on_event=show, fetch=not no_fetch)
    else:
        typer.echo("Pass --ticker TICKER --replay, or --session ID --redirect TEXT.")
        raise typer.Exit(1)

    from localledger.models import Note, WarroomSession

    with session_scope() as s:
        ws = s.get(WarroomSession, room_id)
        note = s.get(Note, ws.final_note_id) if ws.final_note_id else None
        if note:
            typer.echo(
                f"\nFinal note ({'fact-check PASSED' if note.factcheck_passed else 'fact-check FAILED'}): "
                f"{note.title}\n{note.body_md}\n"
            )
        typer.echo(
            f"Room #{room_id}: {ws.status}. Open http://127.0.0.1:8000/warroom/{room_id} (make serve) "
            "to watch, redirect and approve."
        )


@app.command()
def leads() -> None:
    """Fund target list from SEC 13F data sets (funds holding >= 2 universe names) -> ./out/leads.csv."""
    from localledger.gtm.leads13f import build_leads

    with session_scope() as s:
        r = build_leads(s)
    typer.echo(f"13F set: {r.zip_url}")
    typer.echo(f"CUSIPs mapped: {r.mapped} ({r.verified} verified via OpenFIGI); report period {r.report_period}")
    typer.echo(f"Funds holding >= 2 universe names: {r.funds_with_overlap} -> {r.path}")


@app.command()
def outreach(top: Annotated[int, typer.Option(help="How many top leads to draft for")] = 50) -> None:
    """Personalized ~90-word outreach drafts for the top leads -> ./out/outreach (drafts only, never sent)."""
    from localledger.gtm.outreach import build_outreach
    from localledger.llm.client import LLM

    with session_scope() as s:
        r = build_outreach(s, LLM("outreach"), top=top)
    typer.echo(
        f"{r.drafts} drafts written to {r.out_dir} ({r.fallbacks} used the deterministic template). Nothing was sent."
    )


@app.command()
def pdfs() -> None:
    """Fund one-pager and sample-issue PDFs -> ./out/pdf (same templates as the site and newsletter)."""
    from localledger.gtm.pdfs import build_pdfs

    with session_scope() as s:
        for path in build_pdfs(s):
            typer.echo(f"wrote {path}")


@app.command()
def publish(
    note_id: Annotated[int, typer.Option(help="Note to mark published")],
    disclose: Annotated[bool, typer.Option(help="Note carries the author-holdings disclosure")] = False,
) -> None:
    """Mark a fact-checked note published, after the compliance checks. Prints trading-blackout reminders."""
    from localledger.compliance import PublishBlocked, blackout_reminders, check_publish, lint_impersonal
    from localledger.models import Note, utcnow

    with session_scope() as s:
        note = s.get(Note, note_id)
        if note is None:
            typer.echo("no such note")
            raise typer.Exit(1)
        problems = [] if note.factcheck_passed else ["note did not pass fact-check"]
        problems += [f"recommendation language: {p}" for p in lint_impersonal(note.body_md)]
        try:
            check_publish([note.ticker] if note.ticker else [], disclosure_flag=disclose or note.disclosure_flag)
        except PublishBlocked as exc:
            problems.append(str(exc))
        if problems:
            typer.echo("BLOCKED: " + "; ".join(problems))
            raise typer.Exit(2)
        note.status, note.disclosure_flag, note.updated_at = "published", note.disclosure_flag or disclose, utcnow()
        s.add(note)
    typer.echo(f"Note {note_id} marked published (this does not send or post anything).")
    for line in blackout_reminders([note.ticker] if note.ticker else []):
        typer.echo(f"  {line}")


@app.command("check-citations")
def check_citations(limit: Annotated[int | None, typer.Option(help="Check at most N URLs")] = None) -> None:
    """HTTP-check every distinct signal citation URL (polite, rate-limited). Writes ./out/citation_check.csv."""
    import csv

    from localledger.http import HttpClient
    from localledger.models import Signal

    settings = get_settings()
    with session_scope() as s:
        urls = sorted({u for u in s.exec(select(Signal.citation_url)).all()})
    if limit:
        urls = urls[:limit]
    http, rows, bad = HttpClient(), [], 0
    for url in urls:
        try:
            resp = http.get(url, check_robots=True, retries=2)
            code = resp.status_code
        except Exception as exc:  # noqa: BLE001
            code = f"error: {type(exc).__name__}"
        ok = code == 200
        bad += not ok
        rows.append({"url": url, "status": code, "ok": ok})
    path = settings.out_dir / "citation_check.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["url", "status", "ok"])
        w.writeheader()
        w.writerows(rows)
    typer.echo(f"Checked {len(urls)} citation URLs: {len(urls) - bad} OK, {bad} not OK -> {path}")


@app.command()
def models(
    tickers: Annotated[str | None, typer.Option(help="Comma list of tickers to (re)fetch and replay")] = None,
    no_fetch: Annotated[bool, typer.Option(help="Use stored XBRL company facts only")] = False,
    no_llm: Annotated[bool, typer.Option(help="Deterministic rules only (no LLM model updates)")] = False,
    max_llm: Annotated[int | None, typer.Option(help="Cap on LLM model updates this run")] = None,
) -> None:
    """Build every company's model + valuation, then replay signals to record each one's impact (the so-what)."""
    from localledger.llm.client import LLM
    from localledger.modeling.store import build_models

    with session_scope() as s:
        r = build_models(
            s, tickers=_csv(tickers), fetch=not no_fetch, llm=None if no_llm else LLM("models"), max_llm=max_llm
        )
    typer.echo(f"Models: {r.models} ({r.modeled} modeled, {r.no_xbrl} without XBRL, {r.pre_revenue} pre-revenue).")
    typer.echo(f"Signal impacts: {r.impacts} replayed, {r.changed} moved a model; {r.llm_updates} LLM model updates.")
    for err in r.errors[:5]:
        typer.echo(f"    {err}")


@app.command()
def stats() -> None:
    """Counts by table and connector (real vs fixture), for the Definition of Done."""
    from localledger.models import Company, Document, Note, Signal, Source

    with session_scope() as s:
        typer.echo(f"companies: {s.exec(select(func.count()).select_from(Company)).one()}")
        typer.echo(f"documents: {s.exec(select(func.count()).select_from(Document)).one()}")
        typer.echo(f"notes:     {s.exec(select(func.count()).select_from(Note)).one()}")
        rows = s.exec(
            select(Signal.source_id, func.count(Signal.id), func.count(func.distinct(Signal.ticker)))
            .join(Document, Document.id == Signal.document_id)
            .where(Document.origin == "real")
            .group_by(Signal.source_id)
        ).all()
        all_tickers = s.exec(
            select(func.count(func.distinct(Signal.ticker)))
            .join(Document, Document.id == Signal.document_id)
            .where(Document.origin == "real")
        ).one()
        typer.echo("real signals by source:")
        for source_id, n, t in rows:
            src = s.get(Source, source_id)
            typer.echo(f"  {source_id:14} {n:5} signals  {t:4} tickers  status={src.status if src else '?'}")
        typer.echo(f"connectors with real signals: {len(rows)} | distinct tickers: {all_tickers}")


@app.command()
def costs() -> None:
    """LLM ledger: spend so far vs the LLM_BUDGET_USD cap."""
    from localledger.llm.client import spent_usd
    from localledger.models import LlmCall

    settings = get_settings()
    with session_scope() as s:
        rows = s.exec(
            select(
                LlmCall.command,
                LlmCall.provider,
                func.count(LlmCall.id),
                func.sum(LlmCall.input_tokens),
                func.sum(LlmCall.output_tokens),
                func.sum(LlmCall.est_cost_usd),
            ).group_by(LlmCall.command, LlmCall.provider)
        ).all()
    for command, provider, n, tin, tout, cost in rows:
        typer.echo(f"{command:12} {provider:8} {n:5} calls  in={tin or 0:>9,} out={tout or 0:>8,}  ${cost or 0:.4f}")
    typer.echo(f"Spent ${spent_usd():.4f} of ${settings.budget_usd:.2f} budget (LLM_BUDGET_USD).")


if __name__ == "__main__":
    app()
