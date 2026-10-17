"""Multiplayer earnings war room: shared board, SSE stream, presence, redirects, claims, approval."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlmodel import Session, select

from localledger.compliance import PublishBlocked, blackout_reminders, check_publish, lint_impersonal
from localledger.models import Company, Document, Note, WarroomSession, utcnow
from localledger.warroom import board
from localledger.warroom.crew import create_session, run_in_background
from localledger.web.deps import TEMPLATES, get_session, render, require_pilot

router = APIRouter(prefix="/warroom")


def _session_or_404(session: Session, session_id: int) -> WarroomSession:
    ws = session.get(WarroomSession, session_id)
    if ws is None:
        raise HTTPException(404, "no such war room")
    return ws


@router.get("")
def warroom_list(request: Request, user: dict = Depends(require_pilot), session: Session = Depends(get_session)):
    sessions = session.exec(select(WarroomSession).order_by(WarroomSession.id.desc()).limit(50)).all()
    with_release = sorted(
        {
            d.ticker
            for d in session.exec(select(Document).where(Document.source_id == "edgar")).all()
            if d.ticker and '"kind": "ex99"' in (d.meta_json or "")
        }
    )
    return render(request, "warroom/list.html", sessions=sessions, tickers=with_release)


@router.api_route("/new", methods=["GET", "POST"])
async def warroom_new(request: Request, user: dict = Depends(require_pilot)):
    form = await request.form() if request.method == "POST" else {}
    ticker = (form.get("ticker") or request.query_params.get("ticker") or "").upper()
    if not ticker:
        raise HTTPException(400, "ticker required")
    session_id = create_session(ticker, created_by=user["name"], title=f"{ticker} earnings room")
    run_in_background(session_id, actor=user["name"])
    return RedirectResponse(f"/warroom/{session_id}", status_code=303)


def _cards(session_id: int) -> list[dict]:
    threads = board.get_threads(session_id)
    return [
        {"agent": a, "label": board.LABELS[a], "thread": threads[a], "out": board.thread_output(threads[a])}
        for a in board.AGENTS
    ]


@router.get("/{session_id}")
def warroom_view(
    request: Request, session_id: int, user: dict = Depends(require_pilot), session: Session = Depends(get_session)
):
    ws = _session_or_404(session, session_id)
    board.touch_presence(session_id, user["name"])
    note = session.get(Note, ws.final_note_id) if ws.final_note_id else None
    events = board.events_after(session_id, 0)[-40:]
    last_id = events[-1].id if events else 0
    return render(
        request,
        "warroom/session.html",
        ws=ws,
        company=session.get(Company, ws.ticker),
        cards=_cards(session_id),
        note=note,
        events=events,
        last_id=last_id,
        present=board.present_users(session_id),
        inputs=json.loads(ws.inputs_json or "{}"),
    )


@router.get("/{session_id}/card/{agent}")
def agent_card(request: Request, session_id: int, agent: str, user: dict = Depends(require_pilot)):
    if agent not in board.AGENTS:
        raise HTTPException(404)
    card = next(c for c in _cards(session_id) if c["agent"] == agent)
    return render(request, "warroom/_card.html", c=card, session_id=session_id)


@router.get("/{session_id}/note")
def note_panel(
    request: Request, session_id: int, user: dict = Depends(require_pilot), session: Session = Depends(get_session)
):
    ws = _session_or_404(session, session_id)
    session.refresh(ws)
    note = session.get(Note, ws.final_note_id) if ws.final_note_id else None
    return render(request, "warroom/_note.html", ws=ws, note=note)


@router.get("/{session_id}/stream")
async def stream(request: Request, session_id: int, after: int = 0, user: dict = Depends(require_pilot)):
    """Server-Sent Events: every board event in order, plus presence snapshots. Reads the DB, so events written
    by any process (CLI replay, web reruns) reach every connected browser."""
    last_event = request.headers.get("last-event-id")
    cursor = int(last_event) if last_event and last_event.isdigit() else after
    name = user["name"]

    async def gen():
        nonlocal cursor
        last_presence: list[str] | None = None
        tick = 0
        yield "retry: 2000\n\n"
        while True:
            if await request.is_disconnected():
                break
            if tick % 5 == 0:
                await asyncio.to_thread(board.touch_presence, session_id, name)
                present = await asyncio.to_thread(board.present_users, session_id)
                if present != last_presence:
                    last_presence = present
                    yield f"event: presence\ndata: {json.dumps(present)}\n\n"
            events = await asyncio.to_thread(board.events_after, session_id, cursor)
            for e in events:
                cursor = e.id
                data = {
                    "id": e.id,
                    "type": e.type,
                    "agent": e.agent,
                    "actor": e.actor,
                    "payload": json.loads(e.payload_json or "{}"),
                    "at": e.created_at.strftime("%H:%M:%S"),
                }
                yield f"id: {e.id}\nevent: board\ndata: {json.dumps(data, default=str)}\n\n"
            if tick % 15 == 0:
                yield ": keepalive\n\n"
            tick += 1
            await asyncio.sleep(1)

    return StreamingResponse(
        gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.post("/{session_id}/heartbeat")
def heartbeat(session_id: int, user: dict = Depends(require_pilot)):
    board.touch_presence(session_id, user["name"])
    return {"present": board.present_users(session_id)}


@router.post("/{session_id}/redirect")
def redirect(
    session_id: int,
    agent: str = Form(...),
    text: str = Form(...),
    user: dict = Depends(require_pilot),
    session: Session = Depends(get_session),
):
    _session_or_404(session, session_id)
    text = text.strip()[:500]
    if not text:
        raise HTTPException(400, "redirect text required")
    target = agent if agent in board.AGENTS else "room"
    board.emit(session_id, "redirect", target, user["name"], text=text)
    agents = board.AGENTS if target == "room" else board.downstream(target)
    run_in_background(session_id, agents, actor=user["name"])
    return {"ok": True, "rerunning": agents}


@router.post("/{session_id}/claim")
def claim(session_id: int, agent: str = Form(...), user: dict = Depends(require_pilot)):
    thread = board.get_threads(session_id).get(agent)
    if thread is None:
        raise HTTPException(404)
    if thread.claimed_by and thread.claimed_by != user["name"]:
        raise HTTPException(409, f"already claimed by {thread.claimed_by}")
    board.update_thread(session_id, agent, claimed_by=user["name"], status="claimed")
    board.emit(session_id, "claim", agent, user["name"])
    return {"ok": True}


@router.post("/{session_id}/release")
def release(session_id: int, agent: str = Form(...), rerun: bool = Form(False), user: dict = Depends(require_pilot)):
    thread = board.get_threads(session_id).get(agent)
    if thread is None:
        raise HTTPException(404)
    if thread.claimed_by and thread.claimed_by != user["name"]:
        raise HTTPException(409, f"claimed by {thread.claimed_by}")
    board.update_thread(session_id, agent, claimed_by=None, status="done")
    board.emit(session_id, "release", agent, user["name"], rerun=rerun)
    if rerun:
        run_in_background(session_id, board.downstream(agent), actor=user["name"])
    return {"ok": True}


@router.post("/{session_id}/edit")
def edit(session_id: int, agent: str = Form(...), text: str = Form(...), user: dict = Depends(require_pilot)):
    """A human who holds a thread replaces that agent's output; downstream agents rerun on the human's version."""
    thread = board.get_threads(session_id).get(agent)
    if thread is None or thread.claimed_by != user["name"]:
        raise HTTPException(409, "claim the thread before editing it")
    out = board.thread_output(thread)
    key = "argument" if agent in ("bull", "bear") else "summary"
    out[key] = text.strip()[:2000]
    out["edited_by"] = user["name"]
    board.update_thread(session_id, agent, output_json=json.dumps(out), version=thread.version + 1)
    board.emit(session_id, "output", agent, user["name"], version=thread.version + 1, edited=True)
    rest = [a for a in board.downstream(agent) if a != agent]
    if rest:
        run_in_background(session_id, rest, actor=user["name"])
    return {"ok": True}


@router.post("/{session_id}/approve")
def approve(
    session_id: int,
    disclose: bool = Form(False),
    user: dict = Depends(require_pilot),
    session: Session = Depends(get_session),
):
    ws = _session_or_404(session, session_id)
    note = session.get(Note, ws.final_note_id) if ws.final_note_id else None
    if note is None:
        raise HTTPException(409, "no final note yet")
    problems = []
    if not note.factcheck_passed:
        problems.append("the note did not pass the fact-check gate")
    lint = lint_impersonal(note.body_md)
    if lint:
        problems.append(f"recommendation language: {', '.join(lint)}")
    try:
        check_publish([ws.ticker], disclosure_flag=disclose or note.disclosure_flag)
    except PublishBlocked as exc:
        problems.append(str(exc))
    if problems:
        board.emit(session_id, "error", "note_writer", user["name"], error="approval blocked: " + "; ".join(problems))
        return {"ok": False, "problems": problems}
    note.status = "approved"
    note.approved_by = user["name"]
    note.disclosure_flag = note.disclosure_flag or disclose
    note.updated_at = utcnow()
    ws.approved_by, ws.approved_at = user["name"], utcnow()
    session.add(note)
    session.add(ws)
    session.commit()
    board.emit(
        session_id, "approve", "note_writer", user["name"], note_id=note.id, reminders=blackout_reminders([ws.ticker])
    )
    return {"ok": True}


TEMPLATES.env.globals["warroom_labels"] = board.LABELS
