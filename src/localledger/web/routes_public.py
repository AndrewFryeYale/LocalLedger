"""Public pages: landing, sample issue, methodology, pricing, disclosures, forms, login."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, func, select

from localledger.config import get_settings
from localledger.models import Company, NewsletterIssue, Note, PilotRequest, Signal, Source, Subscriber
from localledger.web.deps import get_session, render

router = APIRouter()
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _sample_signal(session: Session) -> tuple[Signal | None, Company | None, Note | None]:
    """Best fact-checked signal for the landing-page card."""
    row = session.exec(
        select(Signal, Note)
        .join(Note, Note.signal_id == Signal.id)
        .where(Note.factcheck_passed == True)  # noqa: E712
        .order_by(Signal.rank_score.desc())
    ).first()
    if row is None:
        sig = session.exec(select(Signal).order_by(Signal.rank_score.desc())).first()
        return sig, session.get(Company, sig.ticker) if sig else None, None
    sig, note = row
    return sig, session.get(Company, sig.ticker), note


@router.get("/")
def landing(request: Request, session: Session = Depends(get_session)):
    sig, company, note = _sample_signal(session)
    stats = {
        "companies": session.exec(select(func.count()).select_from(Company)).one(),
        "uncovered": session.exec(
            select(func.count()).select_from(Company).where(Company.analyst_coverage == "0 (checked)")
        ).one(),
        "signals": session.exec(select(func.count()).select_from(Signal)).one(),
        "sources": session.exec(select(func.count()).select_from(Source).where(Source.status != "unavailable")).one(),
    }
    bt_path = get_settings().out_dir / "backtest" / "results.json"
    bt = json.loads(bt_path.read_text()) if bt_path.exists() else None
    return render(request, "public/landing.html", sig=sig, company=company, note=note, stats=stats, bt=bt)


def latest_issue(session: Session) -> NewsletterIssue | None:
    return (
        session.exec(
            select(NewsletterIssue)
            .where(NewsletterIssue.factcheck_passed == True)  # noqa: E712
            .order_by(NewsletterIssue.created_at.desc())
        ).first()
        or session.exec(select(NewsletterIssue).order_by(NewsletterIssue.created_at.desc())).first()
    )


AGENT_ROLES = {
    "analyst_signal_note": (
        "Analyst",
        "Writes 3-5 cited claims: what changed (from the signal's source) and why it "
        "matters (from BM25-retrieved 10-K/10-Q passages).",
    ),
    "factcheck_claims": (
        "Fact-checker",
        "Re-reads the cited text around each quote and rejects claims it doesn't "
        "support. Runs after the deterministic gate (verbatim quote, numbers, taint, advice lint).",
    ),
    "model_update": (
        "Model updater",
        "Proposes at most 3 bounded adjustments to a company model when rules can't "
        "quantify a signal. Each needs a verbatim quote; factors are capped at 0.7-1.3x.",
    ),
    "company_deep_note": ("Deep-note writer", "The newsletter's weekly company note from several signals."),
    "outreach_email": ("Outreach drafter", "90-word drafts to funds from their 13F overlap. Never sent."),
    "warroom_reader": ("Reader", "Extracts reported KPIs, exactly as stated, from the 8-K Ex. 99.1 and prior 10-Q."),
    "warroom_model": ("Model", "Maps the KPIs onto the company model's drivers; reports the surprise vs our range."),
    "warroom_thesis": ("Thesis", "Tests the print against the stored thesis and its kill criteria."),
    "warroom_read_across": ("Read-across", "Connects the print to the company's and peers' public-records signals."),
    "warroom_bull": ("Bull", "The most constructive honest reading, using only facts on the board."),
    "warroom_bear": ("Bear", "The most skeptical honest reading, using only facts on the board."),
    "warroom_moderator": (
        "Moderator",
        "Records agreed facts and open disagreements, and what evidence would settle each. Picks no side.",
    ),
    "warroom_note_writer": ("Note writer", "Writes the cited post-earnings note; fact-check runs on every claim."),
}


def _excerpt(output: dict) -> str:
    for key in ("summary", "argument", "headline", "next_quarter_implication"):
        if isinstance(output.get(key), str) and output[key].strip():
            return output[key].strip()
    return json.dumps(output)[:300]


def _trace(session: Session) -> dict | None:
    """The latest complete war room run on a live model: events, each agent's output and its ledger row."""
    from localledger.models import LlmCall, WarroomEvent, WarroomSession
    from localledger.warroom import board

    rooms = session.exec(
        select(WarroomSession).where(WarroomSession.status == "complete").order_by(WarroomSession.id.desc())
    ).all()
    for ws in rooms:
        events = session.exec(
            select(WarroomEvent).where(WarroomEvent.session_id == ws.id).order_by(WarroomEvent.id)
        ).all()
        outputs = [e for e in events if e.type == "output" and json.loads(e.payload_json).get("provider") == "portkey"]
        if len(outputs) < len(board.AGENTS):
            continue
        start, end = events[0].created_at, events[-1].created_at
        calls = session.exec(
            select(LlmCall).where(LlmCall.command == "warroom", LlmCall.created_at >= start, LlmCall.created_at <= end)
        ).all()
        by_prompt = {c.prompt_name: c for c in calls}
        threads = board.get_threads(ws.id)
        steps = []
        for agent in board.AGENTS:
            started = next((e for e in events if e.agent == agent and e.type == "status"), None)
            done = next((e for e in events if e.agent == agent and e.type == "output"), None)
            c = by_prompt.get(f"warroom_{agent}")
            steps.append(
                {
                    "agent": agent,
                    "label": board.LABELS[agent],
                    "start_s": (started.created_at - start).total_seconds() if started else None,
                    "end_s": (done.created_at - start).total_seconds() if done else None,
                    "excerpt": _excerpt(board.thread_output(threads[agent])),
                    "call": c,
                }
            )
        fc = by_prompt.get("factcheck_claims")
        note_ev = next((e for e in events if e.type == "note"), None)
        return {
            "room": ws,
            "steps": steps,
            "factcheck": fc,
            "note": json.loads(note_ev.payload_json) if note_ev else {},
            "seconds": (end - start).total_seconds(),
            "cost": sum(c.est_cost_usd for c in calls),
            "tokens_in": sum(c.input_tokens for c in calls),
            "tokens_out": sum(c.output_tokens for c in calls),
            "calls": len(calls),
        }
    return None


@router.get("/how-it-works")
def how_it_works(request: Request, session: Session = Depends(get_session)):
    from localledger.llm.prompts import load_prompt
    from localledger.warroom.crew import MAX_RUNS_PER_HOUR

    agents = []
    for name, (label, role) in AGENT_ROLES.items():
        p = load_prompt(name)
        agents.append(
            {
                "name": name,
                "label": label,
                "role": role,
                "tier": p.tier,
                "effort": p.reasoning_effort,
                "max_tokens": p.max_tokens,
            }
        )
    settings = get_settings()
    return render(
        request,
        "public/how_it_works.html",
        agents=agents,
        trace=_trace(session),
        model_fast=settings.model_fast,
        model_heavy=settings.model_heavy,
        max_runs=MAX_RUNS_PER_HOUR,
    )


@router.get("/sample")
def sample_issue(request: Request, session: Session = Depends(get_session)):
    issue = latest_issue(session)
    body = None
    if issue:
        from pathlib import Path

        path = Path(issue.path_html)
        if not path.is_absolute():
            path = get_settings().out_dir / path
        if path.exists():
            body = path.read_text()
    return render(request, "public/sample.html", issue=issue, body=body)


@router.get("/sample/raw")
def sample_issue_raw(session: Session = Depends(get_session)):
    issue = latest_issue(session)
    if not issue:
        return HTMLResponse("<p>No issue generated yet. Run <code>ll newsletter</code>.</p>")
    from pathlib import Path

    path = Path(issue.path_html)
    if not path.is_absolute():
        path = get_settings().out_dir / path
    return HTMLResponse(path.read_text() if path.exists() else "<p>Issue file missing.</p>")


@router.get("/methodology")
def methodology(request: Request, session: Session = Depends(get_session)):
    sources = session.exec(select(Source).order_by(Source.id)).all()
    counts = dict(session.exec(select(Signal.source_id, func.count(Signal.id)).group_by(Signal.source_id)).all())
    return render(request, "public/methodology.html", sources=sources, counts=counts)


@router.get("/pricing")
def pricing(request: Request):
    return render(request, "public/pricing.html")


@router.get("/disclosures")
def disclosures_page(request: Request):
    return render(request, "public/disclosures.html")


@router.post("/waitlist")
def join_waitlist(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    firm: str = Form(""),
    role: str = Form(""),
    session: Session = Depends(get_session),
):
    email = email.strip().lower()
    if not EMAIL.match(email):
        return render(request, "public/_form_result.html", ok=False, message="That email doesn't look right.")
    existing = session.exec(select(Subscriber).where(Subscriber.email == email)).first()
    if not existing:
        session.add(
            Subscriber(email=email, name=name.strip() or None, firm=firm.strip() or None, role=role.strip() or None)
        )
        session.commit()
    return render(
        request,
        "public/_form_result.html",
        ok=True,
        message="You're on the list. The free weekly issue starts with the next edition.",
    )


@router.post("/pilot-request")
def pilot_request(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    fund: str = Form(...),
    aum_band: str = Form(""),
    interest: str = Form(""),
    message: str = Form(""),
    session: Session = Depends(get_session),
):
    if not EMAIL.match(email.strip()):
        return render(request, "public/_form_result.html", ok=False, message="That email doesn't look right.")
    session.add(
        PilotRequest(
            name=name.strip(),
            email=email.strip().lower(),
            fund=fund.strip(),
            aum_band=aum_band or None,
            interest=interest or None,
            message=message.strip() or None,
        )
    )
    session.commit()
    return render(
        request,
        "public/_form_result.html",
        ok=True,
        message="Thanks. We'll reply within two business days to set up a pilot.",
    )


@router.get("/login")
def login_form(request: Request, next: str = "/app/signals"):
    return render(request, "public/login.html", next=next, error=None)


@router.post("/login")
def login(request: Request, passcode: str = Form(...), name: str = Form(...), next: str = Form("/app/signals")):
    settings = get_settings()
    name = name.strip()[:40]
    if not name:
        return render(request, "public/login.html", next=next, error="Add your name so teammates see who's here.")
    if passcode == settings.admin_passcode:
        role = "admin"
    elif passcode == settings.pilot_passcode:
        role = "pilot"
    else:
        return render(request, "public/login.html", next=next, error="Wrong passcode.")
    request.session["user"] = {"name": name, "role": role}
    target = next if next.startswith("/") and not next.startswith("//") else "/app/signals"
    return RedirectResponse(target, status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@router.get("/healthz")
def healthz():
    return {"ok": True}


def issue_items(issue: NewsletterIssue) -> list[int]:
    return [int(x) for x in json.loads(f"[{issue.signal_ids}]")] if issue.signal_ids else []
