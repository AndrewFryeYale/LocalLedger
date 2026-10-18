"""Untrusted-input defenses. Every document that reaches a prompt was written by someone else (a town clerk, an
issuer's IR team, anyone who can get text into a public agenda packet), so it is treated as data, never as
instructions.

Layers (each one works without the others):
1. Prompt rule: every prompt that shows document text says it is untrusted and must not be obeyed.
2. Input filter (this module, applied centrally in LLM.json to `documents[*].text|title` and `excerpt`):
   - strips zero-width characters, and flags + strips text-direction overrides (hidden-text tricks),
   - defangs forged document headers ("=== document_id=..." is our own delimiter),
   - replaces instruction-like spans ("ignore previous instructions", "rate this stock a buy", role tags) with a
     visible marker, and logs a safety event.
3. Output gates that don't trust the model at all: verbatim-quote and number checks, the recommendation lint, the
   LLM fact-checker, bounded model updates. An injected claim has to survive all of them to reach a reader.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

INVISIBLE = re.compile("[\u200b-\u200f\u2060-\u2064\ufeff\u00ad]")  # common in EDGAR HTML: stripped silently
BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")  # direction overrides: used to hide or reorder text
FORGED_HEADER = re.compile(r"^\s*={3,}\s*document_id\s*=", re.IGNORECASE | re.MULTILINE)
_SUBJECT = r"(?:this|the|its|our)\s+(?:stock|shares|company|security|name|ticker|issuer)"
INJECTION = [
    (
        "override",
        r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|your|the)\b"
        r"[^.\n]{0,30}\b(?:instructions?|prompts?|rules?|directions?|guidelines?)\b",
    ),
    ("role_play", r"\b(?:you are now|from now on,? you|pretend (?:to be|you are)|you must now)\b"),
    ("prompt_probe", r"\b(?:system prompt|developer message|hidden instructions?|jailbreak)\b"),
    ("role_tag", r"(?:<\|im_start\|>|<\|im_end\|>|<\|system\|>|^\s*(?:system|assistant|developer)\s*:\s)"),
    (
        "ai_address",
        r"\b(?:attention|note to|message (?:to|for)|instructions? (?:to|for))\s+(?:the\s+|any\s+)?"
        r"(?:ai|llm|language model|assistant|chatbot|gpt|model|analyst bot)s?\b",
    ),
    (
        "rating_demand",
        rf"\b(?:rate|label|call|classify|describe|present)\s+{_SUBJECT}\s+(?:as\s+)?(?:an?\s+)?(?:strong\s+)?"
        r"(?:buy|sell|outperform|must[- ]own)\b|\b(?:tell|advise)\s+(?:readers|investors|subscribers)\s+to\s+"
        r"(?:buy|sell|purchase)\b",
    ),
    (
        "output_demand",
        r"\b(?:respond|reply|output|print)\s+(?:only|exactly)\b|\b(?:do not|don't|never)\s+"
        r"(?:fact[- ]?check|verify|cite)\b",
    ),
]
_INJECTION = [(name, re.compile(rx, re.IGNORECASE | re.MULTILINE)) for name, rx in INJECTION]
MARKER = "[removed by Local Ledger: possible instruction injection]"
UNTRUSTED_PROMPT_KEYS = ("documents", "excerpt")


@dataclass(frozen=True)
class Finding:
    kind: str
    excerpt: str


def scan(text: str) -> list[Finding]:
    findings = []
    if BIDI.search(text):
        findings.append(Finding("bidi_override", f"{len(BIDI.findall(text))} text-direction override characters"))
    if FORGED_HEADER.search(text):
        findings.append(Finding("forged_header", FORGED_HEADER.search(text).group(0).strip()))
    for name, rx in _INJECTION:
        for m in rx.finditer(text):
            findings.append(Finding(name, text[max(0, m.start() - 40) : m.end() + 40].replace("\n", " ").strip()))
    return findings


def sanitize(text: str) -> tuple[str, list[Finding]]:
    """Text safe to place inside a prompt, plus what was found. The stored document is never modified, so the
    fact-check still verifies quotes against the original."""
    if not text:
        return text, []
    text = INVISIBLE.sub("", text)
    findings = scan(text)
    if not findings:
        return text, []
    clean = BIDI.sub("", text)
    clean = FORGED_HEADER.sub("= = = (quoted header) document_id=", clean)
    for _name, rx in _INJECTION:
        clean = rx.sub(MARKER, clean)
    return clean, findings


def guard_variables(prompt_name: str, variables: dict) -> dict:
    """Sanitize the untrusted parts of a prompt's variables (a shallow copy; callers' dicts are untouched)."""
    if not any(k in variables for k in UNTRUSTED_PROMPT_KEYS) or os.environ.get("LL_DISABLE_INPUT_FILTER") == "1":
        return variables  # (the switch exists only for the red-team ablation in safety_demo)
    out = dict(variables)
    found: list[tuple[int | None, Finding]] = []
    if isinstance(out.get("documents"), list):
        docs = []
        for d in out["documents"]:
            if isinstance(d, dict):
                d = dict(d)
                for field in ("text", "title"):
                    if isinstance(d.get(field), str):
                        d[field], f = sanitize(d[field])
                        found += [(d.get("document_id"), x) for x in f]
            docs.append(d)
        out["documents"] = docs
    if isinstance(out.get("excerpt"), str):
        out["excerpt"], f = sanitize(out["excerpt"])
        found += [(variables.get("document_id"), x) for x in f]
    if found:
        record_findings(prompt_name, variables, found)
    return out


def record_findings(prompt_name: str, variables: dict, found: list[tuple[int | None, Finding]]) -> None:
    company = variables.get("company") or {}
    ticker = company.get("ticker") if isinstance(company, dict) else None
    for doc_id, f in found:
        log.warning("possible prompt injection in document %s (%s): %s", doc_id, f.kind, f.excerpt[:120])
        record_event(
            "injection_filtered",
            ticker=ticker,
            document_id=doc_id,
            prompt=prompt_name,
            detail=f.excerpt,
            subkind=f.kind,
        )


def record_event(kind: str, *, ticker: str | None = None, document_id: int | None = None, **detail) -> None:
    """Best-effort write to the safety_events table (never fails the caller)."""
    import json

    from sqlalchemy.exc import OperationalError

    from localledger.db import session_scope
    from localledger.models import SafetyEvent

    try:
        with session_scope() as s:
            s.add(SafetyEvent(kind=kind, ticker=ticker, document_id=document_id, detail_json=json.dumps(detail)[:4000]))
    except OperationalError:
        log.warning("safety event %s not recorded (database busy)", kind)
