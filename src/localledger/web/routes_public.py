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
            select(func.count()).select_from(Company).where(Company.analyst_coverage.in_(["0 (checked)", "Unverified"]))
        ).one(),
        "signals": session.exec(select(func.count()).select_from(Signal)).one(),
        "sources": session.exec(select(func.count()).select_from(Source).where(Source.status != "unavailable")).one(),
    }
    return render(request, "public/landing.html", sig=sig, company=company, note=note, stats=stats)


def latest_issue(session: Session) -> NewsletterIssue | None:
    return (
        session.exec(
            select(NewsletterIssue)
            .where(NewsletterIssue.factcheck_passed == True)  # noqa: E712
            .order_by(NewsletterIssue.created_at.desc())
        ).first()
        or session.exec(select(NewsletterIssue).order_by(NewsletterIssue.created_at.desc())).first()
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
