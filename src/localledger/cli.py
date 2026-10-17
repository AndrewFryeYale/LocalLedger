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
        proc = subprocess.run([sys.executable, str(script), str(xlsx)], cwd=settings.data_dir, timeout=1800,
                              capture_output=True, text=True)
        tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        if proc.returncode == 0:
            typer.echo(f"coverage_check ok: {tail[0]}")
        else:
            typer.echo(f"WARNING: coverage_check failed (exit {proc.returncode}); continuing with sheet values. "
                       f"{(proc.stderr or '').strip().splitlines()[-1:]}")
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
            select(func.count(func.distinct(Signal.ticker))).join(Document, Document.id == Signal.document_id)
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
            select(LlmCall.command, LlmCall.provider, func.count(LlmCall.id), func.sum(LlmCall.input_tokens),
                   func.sum(LlmCall.output_tokens), func.sum(LlmCall.est_cost_usd))
            .group_by(LlmCall.command, LlmCall.provider)
        ).all()
    for command, provider, n, tin, tout, cost in rows:
        typer.echo(f"{command:12} {provider:8} {n:5} calls  in={tin or 0:>9,} out={tout or 0:>8,}  ${cost or 0:.4f}")
    typer.echo(f"Spent ${spent_usd():.4f} of ${settings.budget_usd:.2f} budget (LLM_BUDGET_USD).")


if __name__ == "__main__":
    app()
