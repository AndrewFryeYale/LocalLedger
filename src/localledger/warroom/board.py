"""The shared war-room board: everything any agent or human does is an event row; the UI streams them via SSE.

Writes use short-lived sessions so CLI runs, web-triggered reruns and SSE readers in other processes all see the
same board through the database.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from sqlalchemy.exc import OperationalError
from sqlmodel import select

from localledger.db import session_scope
from localledger.models import AgentThread, Presence, WarroomEvent, utcnow

AGENTS = ["reader", "model", "thesis", "read_across", "bull", "bear", "moderator", "note_writer"]
LEVELS = [["reader"], ["model", "thesis", "read_across"], ["bull", "bear"], ["moderator"], ["note_writer"]]
LABELS = {
    "reader": "Reader",
    "model": "Model",
    "thesis": "Thesis",
    "read_across": "Read-across",
    "bull": "Bull",
    "bear": "Bear",
    "moderator": "Moderator",
    "note_writer": "Note writer",
}
PRESENCE_WINDOW = timedelta(seconds=30)
log = logging.getLogger(__name__)


def downstream(agent: str) -> list[str]:
    """The agent itself plus everything that consumes its output, in run order."""
    deps = {
        "reader": AGENTS,
        "model": ["model", "bull", "bear", "moderator", "note_writer"],
        "thesis": ["thesis", "bull", "bear", "moderator", "note_writer"],
        "read_across": ["read_across", "bull", "bear", "moderator", "note_writer"],
        "bull": ["bull", "moderator", "note_writer"],
        "bear": ["bear", "moderator", "note_writer"],
        "moderator": ["moderator", "note_writer"],
        "note_writer": ["note_writer"],
    }
    return deps[agent]


def emit(session_id: int, type: str, agent: str | None = None, actor: str = "system", **payload) -> WarroomEvent:
    with session_scope() as s:
        event = WarroomEvent(
            session_id=session_id, agent=agent, type=type, actor=actor, payload_json=json.dumps(payload, default=str)
        )
        s.add(event)
        s.flush()
        return event


def events_after(session_id: int, after_id: int = 0, limit: int = 500) -> list[WarroomEvent]:
    with session_scope() as s:
        return list(
            s.exec(
                select(WarroomEvent)
                .where(WarroomEvent.session_id == session_id, WarroomEvent.id > after_id)
                .order_by(WarroomEvent.id)
                .limit(limit)
            ).all()
        )


def get_threads(session_id: int) -> dict[str, AgentThread]:
    with session_scope() as s:
        rows = s.exec(select(AgentThread).where(AgentThread.session_id == session_id)).all()
        threads = {t.agent: t for t in rows}
        for agent in AGENTS:
            if agent not in threads:
                t = AgentThread(session_id=session_id, agent=agent)
                s.add(t)
                threads[agent] = t
        s.flush()
        return threads


def update_thread(session_id: int, agent: str, **fields) -> AgentThread:
    with session_scope() as s:
        t = s.exec(
            select(AgentThread).where(AgentThread.session_id == session_id, AgentThread.agent == agent)
        ).first() or AgentThread(session_id=session_id, agent=agent)
        for key, value in fields.items():
            setattr(t, key, value)
        t.updated_at = utcnow()
        s.add(t)
        s.flush()
        return t


def thread_output(thread: AgentThread | None) -> dict:
    if thread is None or not thread.output_json:
        return {}
    try:
        return json.loads(thread.output_json)
    except json.JSONDecodeError:
        return {}


def touch_presence(session_id: int, user_name: str) -> None:
    """Best-effort heartbeat: a busy database must never break a page or the event stream."""
    try:
        with session_scope() as s:
            row = s.exec(
                select(Presence).where(Presence.session_id == session_id, Presence.user_name == user_name)
            ).first()
            if row is None:
                row = Presence(session_id=session_id, user_name=user_name)
            row.last_seen = utcnow()
            s.add(row)
    except OperationalError:
        log.info("presence heartbeat skipped (database busy)")


def present_users(session_id: int) -> list[str]:
    cutoff = utcnow() - PRESENCE_WINDOW
    with session_scope() as s:
        rows = s.exec(select(Presence).where(Presence.session_id == session_id)).all()
    out = []
    for r in rows:
        seen = r.last_seen if r.last_seen.tzinfo else r.last_seen.replace(tzinfo=cutoff.tzinfo)
        if seen >= cutoff:
            out.append(r.user_name)
    return sorted(out)


def instructions_for(session_id: int) -> dict[str, list[dict]]:
    """Redirects posted so far, per target agent ('room' = every agent)."""
    out: dict[str, list[dict]] = {}
    with session_scope() as s:
        rows = s.exec(
            select(WarroomEvent)
            .where(WarroomEvent.session_id == session_id, WarroomEvent.type == "redirect")
            .order_by(WarroomEvent.id)
        ).all()
    for r in rows:
        out.setdefault(r.agent or "room", []).append({"actor": r.actor, **json.loads(r.payload_json)})
    return out
