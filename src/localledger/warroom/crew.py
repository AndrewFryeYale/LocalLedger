"""Orchestration: run the crew concurrently where independent (asyncio), honoring human claims and redirects.

Reader -> {Model, Thesis, Read-across} -> {Bull, Bear} -> Moderator -> Note writer -> fact-check.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import defaultdict
from collections.abc import Callable

import yaml
from sqlmodel import select

from localledger.config import get_settings
from localledger.db import session_scope
from localledger.llm.client import LLM
from localledger.models import Company, Note, WarroomEvent, WarroomSession, utcnow
from localledger.research.analyst import MIN_CLAIMS_TO_PASS, render_note_md
from localledger.research.factcheck import fact_check, validate_claims
from localledger.signals.store import CitationError
from localledger.warroom import board
from localledger.warroom.agents import PROMPTS, build_vars
from localledger.warroom.inputs import WarroomInputs, gather_inputs

log = logging.getLogger(__name__)
_session_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)


class Crew:
    def __init__(
        self,
        session_id: int,
        llm: LLM | None = None,
        on_event: Callable[[WarroomEvent], None] | None = None,
        fetch: bool = True,
    ) -> None:
        settings = get_settings()
        self.session_id = session_id
        self.llm = llm or LLM("warroom")
        self.on_event = on_event
        self.brand = settings.brand
        with session_scope() as s:
            ws = s.get(WarroomSession, session_id)
            self.ticker = ws.ticker
            self.company = s.get(Company, ws.ticker).model_dump(mode="json")
            self.inputs: WarroomInputs = gather_inputs(s, ws.ticker, fetch=fetch)
            ws.inputs_json = json.dumps(
                {"documents": [{k: d[k] for k in ("document_id", "url", "title")} for d in self.inputs.documents]}
            )
            s.add(ws)
        thesis_path = settings.theses_dir / f"{self.ticker}.md"
        self.thesis = thesis_path.read_text() if thesis_path.exists() else "(no stored thesis for this company)"
        model_path = settings.out_dir / "models" / f"{self.ticker}.yaml"
        if not model_path.exists():
            model_path = settings.models_dir / f"{self.ticker}.yaml"
        self.model_yaml = model_path.read_text() if model_path.exists() else "drivers: []"
        threads = board.get_threads(session_id)
        self.outputs = {a: board.thread_output(t) for a, t in threads.items()}
        self._instructions = board.instructions_for(session_id)

    def instructions(self, agent: str) -> str:
        items = self._instructions.get("room", []) + self._instructions.get(agent, [])
        return "\n".join(f"- {i['actor']}: {i['text']}" for i in items)

    def _emit(self, type: str, agent: str | None = None, actor: str = "system", **payload) -> None:
        event = board.emit(self.session_id, type, agent, actor, **payload)
        if self.on_event:
            self.on_event(event)

    async def run(self, agents: list[str] | None = None, actor: str = "system") -> dict:
        targets = set(agents or board.AGENTS)
        self._set_status("running")
        self._emit("status", None, actor, status="running", agents=[a for a in board.AGENTS if a in targets])
        for level in board.LEVELS:
            names = [a for a in level if a in targets]
            if names:
                await asyncio.gather(*(self._run_agent(a) for a in names))
        self._set_status("complete")
        self._emit("status", None, actor, status="complete")
        return self.outputs

    def _set_status(self, status: str) -> None:
        with session_scope() as s:
            ws = s.get(WarroomSession, self.session_id)
            ws.status = status
            s.add(ws)

    async def _run_agent(self, agent: str) -> None:
        thread = board.get_threads(self.session_id)[agent]
        if thread.claimed_by:
            self.outputs[agent] = board.thread_output(thread)
            self._emit("status", agent, status="held", by=thread.claimed_by)
            return
        board.update_thread(self.session_id, agent, status="running")
        self._emit("status", agent, status="running")
        try:
            result = await self.llm.ajson(PROMPTS[agent], build_vars(agent, self))
            output = result.data
            if agent == "note_writer":
                output = await asyncio.to_thread(self._finalize_note, output, result.model)
            elif agent == "model":
                self._save_model(output)
        except Exception as exc:  # one agent failing never kills the room
            log.exception("agent %s failed", agent)
            board.update_thread(self.session_id, agent, status="error", output_json=json.dumps({"error": str(exc)}))
            self._emit("error", agent, error=f"{type(exc).__name__}: {exc}"[:300])
            return
        version = thread.version + 1
        board.update_thread(self.session_id, agent, status="done", version=version, output_json=json.dumps(output))
        self.outputs[agent] = output
        self._emit("output", agent, version=version, model=result.model, provider=result.provider, cached=result.cached)

    def _save_model(self, output: dict) -> None:
        settings = get_settings()
        base = yaml.safe_load(self.model_yaml) or {}
        updates = {d.get("name"): d for d in output.get("drivers", []) if isinstance(d, dict)}
        for driver in base.get("drivers", []):
            upd = updates.get(driver.get("name"))
            if upd and upd.get("status") == "updated":
                driver.setdefault("history", []).append({"value": driver.get("value"), "replaced_at": str(utcnow())})
                driver["value"] = upd.get("new")
        path = settings.out_dir / "models" / f"{self.ticker}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(base, sort_keys=False))

    def _finalize_note(self, output: dict, model: str | None = None) -> dict:
        claims = output.get("claims") or []
        allowed = {d["document_id"]: d["url"] for d in self.inputs.documents}
        try:
            validate_claims(claims, allowed)
        except CitationError as exc:
            self._emit("error", "note_writer", error=f"citation enforcement failed: {exc}")
            return {"headline": output.get("headline"), "passed": False, "error": str(exc), "kept": 0, "dropped": 0}
        kept, dropped = fact_check(claims, self.inputs.corpora, [], self.llm, self.brand)
        with session_scope() as s:
            note = s.exec(select(Note).where(Note.session_id == self.session_id, Note.kind == "warroom")).first()
            if note is None:
                note = Note(kind="warroom", session_id=self.session_id, ticker=self.ticker, title="", body_md="")
            note.title = str(output.get("headline") or f"{self.ticker} post-earnings note")[:200]
            note.body_md = render_note_md(kept, self.inputs.documents) if kept else "(all claims rejected)"
            note.claims_json = json.dumps(kept)
            note.factcheck_log = json.dumps(dropped)
            note.factcheck_passed = len(kept) >= MIN_CLAIMS_TO_PASS
            note.status = "draft"  # any rerun invalidates a prior approval
            note.model = model or self.llm.model_for("heavy")
            note.updated_at = utcnow()
            s.add(note)
            s.flush()
            ws = s.get(WarroomSession, self.session_id)
            ws.final_note_id = note.id
            ws.approved_by = None
            s.add(ws)
            result = {
                "note_id": note.id,
                "headline": note.title,
                "passed": note.factcheck_passed,
                "kept": len(kept),
                "dropped": len(dropped),
                "dropped_reasons": [d["reason"] for d in dropped][:6],
            }
        self._emit("note", "note_writer", **result)
        return result


def create_session(ticker: str, created_by: str = "cli", title: str | None = None) -> int:
    with session_scope() as s:
        company = s.get(Company, ticker.upper())
        if company is None:
            raise ValueError(f"unknown ticker {ticker}")
        ws = WarroomSession(
            ticker=company.ticker, created_by=created_by, title=title or f"{company.ticker} earnings replay"
        )
        s.add(ws)
        s.flush()
        session_id = ws.id
    board.get_threads(session_id)
    board.emit(session_id, "status", None, created_by, status="created", ticker=ticker.upper())
    return session_id


def run_sync(
    session_id: int,
    agents: list[str] | None = None,
    actor: str = "system",
    llm: LLM | None = None,
    on_event=None,
    fetch: bool = True,
) -> dict:
    """Run (part of) the crew to completion, serialized per session."""
    with _session_locks[session_id]:
        crew = Crew(session_id, llm=llm, on_event=on_event, fetch=fetch)
        return asyncio.run(crew.run(agents, actor))


def run_in_background(session_id: int, agents: list[str] | None = None, actor: str = "system") -> threading.Thread:
    def target() -> None:
        try:
            run_sync(session_id, agents, actor)
        except Exception as exc:  # surface on the board instead of dying silently
            log.exception("war room run failed")
            board.emit(session_id, "error", None, "system", error=f"{type(exc).__name__}: {exc}"[:300])

    t = threading.Thread(target=target, daemon=True, name=f"warroom-{session_id}")
    t.start()
    return t
