"""Admin: GTM metrics, CSV exports, leads contacted toggle, blackout reminders."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlmodel import Session, func, select

from localledger.compliance import blackout_reminders, load_holdings
from localledger.config import get_settings
from localledger.llm.client import spent_usd
from localledger.models import Lead, NewsletterIssue, Note, PilotRequest, Subscriber, utcnow
from localledger.web.deps import get_session, render, require_admin

router = APIRouter(prefix="/admin")


@router.get("")
def admin_home(request: Request, user: dict = Depends(require_admin), session: Session = Depends(get_session)):
    metrics = {
        "waitlist": session.exec(select(func.count()).select_from(Subscriber)).one(),
        "pilot_requests": session.exec(select(func.count()).select_from(PilotRequest)).one(),
        "issues": session.exec(select(func.count()).select_from(NewsletterIssue)).one(),
        "leads": session.exec(select(func.count()).select_from(Lead)).one(),
        "contacted": session.exec(
            select(func.count()).select_from(Lead).where(Lead.contact_status == "contacted")
        ).one(),
        "llm_spent": spent_usd(),
        "llm_budget": get_settings().budget_usd,
    }
    published = session.exec(
        select(Note).where(Note.status.in_(["approved", "published"])).order_by(Note.updated_at.desc()).limit(20)
    ).all()
    reminders = []
    for n in published:
        if n.ticker:
            reminders += blackout_reminders([n.ticker], n.updated_at.date())
    leads = session.exec(select(Lead).order_by(Lead.rank).limit(100)).all()
    issues = session.exec(select(NewsletterIssue).order_by(NewsletterIssue.created_at.desc())).all()
    pilots = session.exec(select(PilotRequest).order_by(PilotRequest.created_at.desc()).limit(20)).all()
    return render(
        request,
        "admin/home.html",
        metrics=metrics,
        leads=leads,
        issues=issues,
        pilots=pilots,
        reminders=reminders[:20],
        holdings=load_holdings(),
    )


@router.post("/leads/{lead_id}/toggle")
def toggle_lead(
    request: Request, lead_id: int, user: dict = Depends(require_admin), session: Session = Depends(get_session)
):
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404)
    lead.contact_status = "not_contacted" if lead.contact_status == "contacted" else "contacted"
    lead.updated_at = utcnow()
    session.add(lead)
    session.commit()
    return render(request, "admin/_lead_row.html", lead=lead)


@router.get("/deck", response_class=HTMLResponse)
def pitch_deck(cik: str | None = None, user: dict = Depends(require_admin), session: Session = Depends(get_session)):
    """The pitch deck, generic or for one lead, rendered live (same generator as `ll deck`)."""
    from localledger.gtm.deck import find_lead, render_deck

    lead = find_lead(session, cik) if cik else None
    if cik and not lead:
        raise HTTPException(404, "no such lead")
    return HTMLResponse(render_deck(session, lead))


def _csv_response(name: str, header: list[str], rows: list[list]) -> StreamingResponse:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )


@router.get("/export/waitlist.csv")
def export_waitlist(user: dict = Depends(require_admin), session: Session = Depends(get_session)):
    rows = session.exec(select(Subscriber).order_by(Subscriber.created_at)).all()
    return _csv_response(
        "waitlist.csv",
        ["email", "name", "firm", "role", "created_at"],
        [[r.email, r.name, r.firm, r.role, r.created_at.isoformat()] for r in rows],
    )


@router.get("/export/pilot_requests.csv")
def export_pilots(user: dict = Depends(require_admin), session: Session = Depends(get_session)):
    rows = session.exec(select(PilotRequest).order_by(PilotRequest.created_at)).all()
    return _csv_response(
        "pilot_requests.csv",
        ["name", "email", "fund", "aum_band", "interest", "message", "created_at"],
        [[r.name, r.email, r.fund, r.aum_band, r.interest, r.message, r.created_at.isoformat()] for r in rows],
    )
