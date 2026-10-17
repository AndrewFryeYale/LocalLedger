"""War room on fixtures with the mock LLM: crew run, redirects, claims, and two live SSE clients on one board."""

import json
import socket
import threading
import time
from datetime import date

import httpx
import pytest
import uvicorn
from sqlmodel import select

from localledger.connectors.edgar import EdgarConnector
from localledger.models import Note, WarroomEvent, WarroomSession
from localledger.warroom import board
from localledger.warroom.crew import create_session, run_sync
from tests.test_edgar import routes


@pytest.fixture()
def room(seeded, fake_http):
    EdgarConnector(seeded, http=fake_http(routes()), today=date(2026, 10, 17), origin="fixture").run(tickers=["PCYO"])
    seeded.commit()
    return create_session("PCYO", created_by="test")


def test_crew_runs_end_to_end(room, session):
    seen = []
    run_sync(room, on_event=lambda e: seen.append((e.type, e.agent)), fetch=False)
    outputs = {a: board.thread_output(t) for a, t in board.get_threads(room).items()}
    assert outputs["reader"]["kpis"], "reader extracted KPIs from the real Ex. 99.1 fixture"
    assert ("output", "bear") in seen and ("note", "note_writer") in seen
    order = [a for t, a in seen if t == "output"]
    assert order.index("reader") < order.index("bull") < order.index("moderator") < order.index("note_writer")
    ws = session.get(WarroomSession, room)
    session.refresh(ws)
    note = session.get(Note, ws.final_note_id)
    assert note.kind == "warroom" and note.factcheck_passed and "Sources:" in note.body_md


def test_redirect_changes_output_and_claims_are_respected(room):
    run_sync(room, fetch=False)
    before = board.thread_output(board.get_threads(room)["bear"])["argument"]
    board.emit(room, "redirect", "bear", "Alice", text="rerun bear case assuming lower volumes")
    run_sync(room, board.downstream("bear"), actor="Alice", fetch=False)
    after = board.thread_output(board.get_threads(room)["bear"])["argument"]
    assert before != after and "lower volumes" in after
    board.update_thread(room, "bull", claimed_by="Bob", status="claimed")
    v = board.get_threads(room)["bull"].version
    run_sync(room, ["bull"], fetch=False)
    assert board.get_threads(room)["bull"].version == v  # held by a human: the agent didn't overwrite it


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_two_browsers_share_one_live_board(room):
    from localledger.web.app import create_app

    run_sync(room, fetch=False)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(f"{base}/healthz", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    after = max(e.id for e in board.events_after(room))
    got: dict[str, list] = {"Alice": [], "Bob": []}
    clients = {}
    for name in got:
        c = httpx.Client(base_url=base, timeout=30)
        c.post("/login", data={"passcode": "ledger-pilot", "name": name})
        clients[name] = c

    def listen(name: str) -> None:
        with clients[name].stream("GET", f"/warroom/{room}/stream", params={"after": after}, timeout=None) as r:
            kind = None
            for line in r.iter_lines():
                if line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("data: "):
                    got[name].append((kind, json.loads(line[6:])))
                    if kind == "board" and got[name][-1][1]["type"] == "note":
                        return

    listeners = [threading.Thread(target=listen, args=(n,), daemon=True) for n in got]
    for t in listeners:
        t.start()
    time.sleep(1.5)
    for name in got:  # both browsers are in the room before anyone acts
        present = clients[name].post(f"/warroom/{room}/heartbeat").json()["present"]
    assert set(present) == {"Alice", "Bob"}
    r = clients["Alice"].post(f"/warroom/{room}/redirect", data={"agent": "bear", "text": "assume lower volumes"})
    assert r.status_code == 200
    for t in listeners:
        t.join(timeout=60)
    server.should_exit = True
    seqs = {n: [(d["id"], d["type"], d["agent"]) for k, d in got[n] if k == "board"] for n in got}
    assert seqs["Alice"] == seqs["Bob"] and ("redirect" in [t for _, t, _ in seqs["Bob"]])
    assert all(any(k == "presence" for k, _ in got[n]) for n in got)  # presence is streamed to each browser
    bear = clients["Bob"].get(f"/warroom/{room}/card/bear").text
    assert "lower volumes" in bear


def test_approval_runs_compliance(room, session, monkeypatch):
    from fastapi.testclient import TestClient

    from localledger import compliance
    from localledger.web.app import create_app

    run_sync(room, fetch=False)
    client = TestClient(create_app())
    client.post("/login", data={"passcode": "ledger-pilot", "name": "Andrew"})
    monkeypatch.setattr(
        compliance,
        "load_holdings",
        lambda path=None: compliance.Holdings(policy="p", blackout_days=3, tickers={"PCYO"}),
    )
    blocked = client.post(f"/warroom/{room}/approve").json()
    assert blocked["ok"] is False and "author holds PCYO" in blocked["problems"][0]
    ok = client.post(f"/warroom/{room}/approve", data={"disclose": "true"}).json()
    assert ok["ok"] is True
    events = session.exec(
        select(WarroomEvent).where(WarroomEvent.session_id == room, WarroomEvent.type == "approve")
    ).all()
    assert events and "Trading blackout" in events[0].payload_json
