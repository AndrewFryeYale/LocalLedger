"""War-room crew: one prompt file per agent, a variable builder per agent, and a deterministic mock per agent."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from localledger.llm.mock import register_mock

if TYPE_CHECKING:
    from localledger.warroom.crew import Crew

PROMPTS = {
    agent: f"warroom_{agent}"
    for agent in ["reader", "model", "thesis", "read_across", "bull", "bear", "moderator", "note_writer"]
}


def _j(value: dict | list | None, limit: int = 6000) -> str:
    text = json.dumps(value or {}, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def _signal_dicts(signals) -> list[dict]:
    return [
        {"id": s.id, "ticker": s.ticker, "source_id": s.source_id, "as_of": str(s.as_of), "summary": s.summary}
        for s in signals
    ]


def build_vars(agent: str, crew: Crew) -> dict:
    out = crew.outputs
    base = {"brand": crew.brand, "company": crew.company, "instructions": crew.instructions(agent)}
    if agent == "reader":
        return {**base, "documents": crew.inputs.documents}
    if agent == "model":
        return {**base, "model_yaml": crew.model_yaml, "reader_json": _j(out.get("reader"))}
    if agent == "thesis":
        return {**base, "thesis": crew.thesis, "reader_json": _j(out.get("reader"))}
    if agent == "read_across":
        return {
            **base,
            "reader_json": _j(out.get("reader"), 3000),
            "own_signals": _signal_dicts(crew.inputs.signals),
            "peer_signals": _signal_dicts(crew.inputs.peer_signals),
        }
    if agent in ("bull", "bear"):
        return {
            **base,
            "reader_json": _j(out.get("reader")),
            "model_json": _j(out.get("model"), 2500),
            "thesis_json": _j(out.get("thesis"), 2500),
            "read_across_json": _j(out.get("read_across"), 2500),
        }
    if agent == "moderator":
        return {
            **base,
            "reader_json": _j(out.get("reader"), 3000),
            "thesis_json": _j(out.get("thesis"), 2000),
            "bull_json": _j(out.get("bull"), 2000),
            "bear_json": _j(out.get("bear"), 2000),
        }
    if agent == "note_writer":
        return {
            **base,
            "reader_json": _j(out.get("reader")),
            "model_json": _j(out.get("model"), 2000),
            "moderator_json": _j(out.get("moderator"), 3000),
            "documents": crew.inputs.documents,
        }
    raise KeyError(agent)


# --- mocks: deterministic, extractive, and they visibly follow room instructions ---------------------
_NUMERIC = re.compile(r"\d")
_YEARISH = re.compile(r"^(?:\(?in thousands|\w+ \d{1,2}, \d{4}|\d{4})", re.IGNORECASE)


def _kpi_rows(doc: dict, limit: int = 12) -> list[dict]:
    rows = []
    for line in doc["text"].splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 3 or len(cells[0]) < 3 or _YEARISH.match(cells[0]) or _NUMERIC.search(cells[0][:1] or "x"):
            continue
        if not (_NUMERIC.search(cells[1]) and _NUMERIC.search(cells[2])):
            continue
        rows.append(
            {
                "kpi": cells[0],
                "current": cells[1],
                "prior": cells[2],
                "change": "",
                "unit": "as reported",
                "document_id": doc["document_id"],
                "quote": line.strip(),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"“])", text) if 8 <= len(s.split()) <= 60]


@register_mock("warroom_reader")
def _mock_reader(v: dict) -> dict:
    docs = v["documents"]
    kpis = _kpi_rows(docs[0]) if docs else []
    guidance = []
    for d in docs[:1]:
        for line in d["text"].splitlines():
            for s in _sentences(line):
                if re.search(r"\b(expect|anticipate|outlook|guidance|plan to|we believe)\b", s, re.IGNORECASE):
                    guidance.append({"item": "outlook", "text": s, "document_id": d["document_id"], "quote": s})
            if len(guidance) >= 3:
                break
    first = next((s for line in docs[0]["text"].splitlines() for s in _sentences(line)), "") if docs else ""
    return {
        "period": "latest reported quarter",
        "kpis": kpis,
        "guidance": guidance,
        "summary": first or "Release text unavailable.",
    }


@register_mock("warroom_model")
def _mock_model(v: dict) -> dict:
    import yaml

    model = yaml.safe_load(v["model_yaml"] or "{}") or {}
    kpis = json.loads(v["reader_json"].split("...(truncated)")[0] or "{}").get("kpis", []) if v["reader_json"] else []
    drivers = []
    for d in model.get("drivers", []):
        aliases = [a.lower() for a in d.get("kpi_aliases", [])] + [d["name"].replace("_", " ").lower()]
        hit = next((k for k in kpis if any(a in k["kpi"].lower() for a in aliases)), None)
        drivers.append(
            {
                "name": d["name"],
                "prior": d.get("value"),
                "new": hit["current"] if hit else d.get("value"),
                "delta": "n/a",
                "unit": d.get("unit"),
                "source_kpi": hit["kpi"] if hit else None,
                "status": "updated" if hit else "unchanged",
            }
        )
    moved = [d["name"] for d in drivers if d["status"] == "updated"]
    return {
        "drivers": drivers,
        "takeaways": [f"Updated from reported KPIs: {', '.join(moved) or 'none'}"],
        "summary": f"{len(moved)} of {len(drivers)} drivers updated from the release.",
    }


@register_mock("warroom_thesis")
def _mock_thesis(v: dict) -> dict:
    checks = []
    section = None
    for line in (v["thesis"] or "").splitlines():
        if line.startswith("## "):
            section = "kill" if "kill" in line.lower() else ("pillar" if "thesis" in line.lower() else None)
        elif section and line.strip().startswith(("-", "*", "1", "2", "3", "4", "5")):
            checks.append(
                {
                    "criterion": line.strip(" -*").strip(),
                    "kind": section,
                    "status": "not tested",
                    "evidence": "mock: needs analyst review",
                }
            )
    return {"checks": checks[:8], "kill_criteria_hit": [], "summary": f"{len(checks)} thesis items listed for review."}


@register_mock("warroom_read_across")
def _mock_read_across(v: dict) -> dict:
    own = [{"about": "this company", "text": s["summary"], "signal_ids": [s["id"]]} for s in v["own_signals"][:3]]
    peers = [{"about": s["ticker"], "text": s["summary"], "signal_ids": [s["id"]]} for s in v["peer_signals"][:3]]
    return {"implications": own + peers, "summary": f"{len(own)} own and {len(peers)} peer signals on the board."}


def _debate(v: dict, stance: str) -> dict:
    reader = json.loads(v["reader_json"].split("...(truncated)")[0] or "{}") if v["reader_json"] else {}
    kpis = reader.get("kpis", [])[:3]
    facts = [f"{k['kpi']} {k['current']} vs {k['prior']}" for k in kpis]
    lead = "Constructive read" if stance == "bull" else "Skeptical read"
    instr = v.get("instructions") or ""
    assumption = f"Assuming, per the room's instruction, {instr.splitlines()[-1].split(': ', 1)[-1]}" if instr else ""
    argument = f"{lead}: " + "; ".join(facts) + "."
    if assumption:
        argument = f"{assumption}. " + argument
    return {
        "argument": argument[:900],
        "key_points": facts or ["no KPIs extracted"],
        "assumptions": [assumption] if assumption else ["as reported"],
    }


@register_mock("warroom_bull")
def _mock_bull(v: dict) -> dict:
    return _debate(v, "bull")


@register_mock("warroom_bear")
def _mock_bear(v: dict) -> dict:
    return _debate(v, "bear")


@register_mock("warroom_moderator")
def _mock_moderator(v: dict) -> dict:
    bull = json.loads(v["bull_json"] or "{}")
    bear = json.loads(v["bear_json"] or "{}")
    return {
        "agreed_facts": bull.get("key_points", [])[:2],
        "open_disagreements": [
            {
                "topic": "durability of the quarter",
                "bull": bull.get("argument", "")[:200],
                "bear": bear.get("argument", "")[:200],
                "what_would_resolve": "next quarterly report and the public-records signals",
            }
        ],
        "summary": "Bull and Bear agree on the reported numbers and disagree on durability.",
    }


@register_mock("warroom_note_writer")
def _mock_note_writer(v: dict) -> dict:
    reader = json.loads(v["reader_json"].split("...(truncated)")[0] or "{}") if v["reader_json"] else {}
    urls = {d["document_id"]: d["url"] for d in v["documents"]}
    claims = []
    for k in reader.get("kpis", [])[:5]:
        claims.append(
            {
                "text": f"{k['kpi']}: {k['current']} versus {k['prior']} in the comparable period.",
                "citation": {
                    "document_id": k["document_id"],
                    "url": urls.get(k["document_id"], ""),
                    "quote": k["quote"],
                },
            }
        )
    for g in reader.get("guidance", [])[:2]:
        claims.append(
            {
                "text": f'The company said: "{g["quote"]}"',
                "citation": {
                    "document_id": g["document_id"],
                    "url": urls.get(g["document_id"], ""),
                    "quote": g["quote"],
                },
            }
        )
    ticker = v["company"]["ticker"]
    return {"headline": f"{ticker}: reported results versus the prior period", "claims": claims}
