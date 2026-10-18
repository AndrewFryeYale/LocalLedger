"""LLM access through the Portkey gateway (OpenAI-compatible API), with a disk cache and a hard budget cap.

- Prompts come only from /prompts/*.md (see prompts.py).
- Cache key = sha256(prompt name + prompt file text + model + rendered inputs): reruns cost nothing.
- Every call (live, cached or mock) is written to the llm_calls ledger. Before a live call, the estimated
  cost is reserved against LLM_BUDGET_USD; if it would exceed the cap we degrade to the mock provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from localledger.config import Settings, get_settings
from localledger.db import session_scope
from localledger.llm.mock import mock_response
from localledger.llm.prompts import load_prompt
from localledger.models import LlmCall

log = logging.getLogger(__name__)

_budget_lock = threading.Lock()
_reserved_usd = 0.0


@dataclass
class LLMResult:
    data: dict
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False


@dataclass
class RunUsage:
    calls: int = 0
    live: int = 0
    cached: int = 0
    mock: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.calls:
            return "LLM: no calls"
        return (
            f"LLM: {self.calls} calls ({self.live} live, {self.cached} cached, {self.mock} mock) | "
            f"tokens in={self.input_tokens:,} out={self.output_tokens:,} | est. cost ${self.cost_usd:.4f}"
        )


RUN_USAGE = RunUsage()


class BudgetExceeded(Exception):
    pass


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.2))


_pending: list[LlmCall] = []  # ledger rows not yet written (database busy); they still count toward the cap


def spent_usd() -> float:
    try:
        with session_scope() as s:
            total = s.exec(select(func.sum(LlmCall.est_cost_usd)).where(LlmCall.provider == "portkey")).one()
    except OperationalError:
        log.warning("ledger read failed (database busy); budget check uses this run's spend only")
        return RUN_USAGE.cost_usd
    return float(total or 0.0) + sum(r.est_cost_usd for r in _pending if r.provider == "portkey")


_pending_lock = threading.Lock()


def flush_pending() -> None:
    """Write ledger rows that couldn't be written earlier (called on every new call and at exit). Thread-safe:
    war-room agents record calls concurrently."""
    with _pending_lock:
        while _pending:
            try:
                with session_scope() as s:
                    s.add(LlmCall(**_pending[0].model_dump(exclude={"id"})))
            except OperationalError:
                return
            _pending.pop(0)


def _cost(settings: Settings, tokens_in: int, tokens_out: int) -> float:
    return (tokens_in * settings.price_in_per_mtok + tokens_out * settings.price_out_per_mtok) / 1_000_000


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start : end + 1])


class LLM:
    def __init__(self, command: str = "cli", settings: Settings | None = None) -> None:
        self.command = command
        self.settings = settings or get_settings()
        self._client = None

    @property
    def live_available(self) -> bool:
        return self.settings.llm_provider == "portkey" and self.settings.has_llm_key

    def _openai(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=os.environ["PORTKEY_API_KEY"], base_url=self.settings.llm_base_url)
        return self._client

    def model_for(self, tier: str) -> str:
        return self.settings.model_heavy if tier == "heavy" else self.settings.model_fast

    def json(self, prompt_name: str, variables: dict, *, tier: str | None = None) -> LLMResult:
        global _reserved_usd
        prompt = load_prompt(prompt_name)
        tier = tier or prompt.tier
        model = self.model_for(tier)
        system, user = prompt.render(variables)
        key = hashlib.sha256("\x1f".join([prompt_name, prompt.raw, model, system, user]).encode("utf-8")).hexdigest()
        cache_path = self.settings.cache_dir / "llm" / key[:2] / f"{key}.json"

        if cache_path.exists():
            cached = json.loads(cache_path.read_text())
            result = LLMResult(
                data=cached["data"],
                text=cached["text"],
                model=cached["model"],
                provider=cached.get("provider", "portkey"),
                input_tokens=cached.get("input_tokens", 0),
                output_tokens=cached.get("output_tokens", 0),
                cached=True,
            )
            self._record(prompt_name, result)
            return result

        if not self.live_available:
            return self._mock(prompt_name, variables, model, reason="no PORTKEY_API_KEY or LLM_PROVIDER=mock")

        est_in = estimate_tokens(system + user)
        est_cost = _cost(self.settings, est_in, prompt.max_tokens)
        with _budget_lock:
            spent = spent_usd()
            if spent + _reserved_usd + est_cost > self.settings.budget_usd:
                note = (
                    f"budget cap ${self.settings.budget_usd:.2f} reached (spent ${spent:.2f}); "
                    f"using mock for {prompt_name}"
                )
                return self._mock(prompt_name, variables, model, reason=note)
            _reserved_usd += est_cost
        try:
            result = self._call(prompt_name, system, user, model, prompt.max_tokens)
        except Exception as exc:
            log.warning("LLM call failed for %s (%s); degrading to mock", prompt_name, type(exc).__name__)
            return self._mock(prompt_name, variables, model, reason=f"live call failed: {type(exc).__name__}")
        finally:
            with _budget_lock:
                _reserved_usd -= est_cost
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "data": result.data,
                    "text": result.text,
                    "model": result.model,
                    "provider": result.provider,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                }
            )
        )
        self._record(prompt_name, result)
        return result

    async def ajson(self, prompt_name: str, variables: dict, *, tier: str | None = None) -> LLMResult:
        return await asyncio.to_thread(self.json, prompt_name, variables, tier=tier)

    def _call(self, prompt_name: str, system: str, user: str, model: str, max_tokens: int) -> LLMResult:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        client = self._openai()
        tokens_in = tokens_out = 0
        last_err: Exception | None = None
        for _attempt in range(2):
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                max_completion_tokens=max_tokens,
            )
            if resp.usage:
                tokens_in += resp.usage.prompt_tokens or 0
                tokens_out += resp.usage.completion_tokens or 0
            text = resp.choices[0].message.content or ""
            try:
                data = _extract_json(text)
            except (ValueError, json.JSONDecodeError) as exc:
                last_err = exc
                continue
            return LLMResult(
                data=data,
                text=text,
                model=model,
                provider="portkey",
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cost_usd=_cost(self.settings, tokens_in, tokens_out),
            )
        # Both attempts returned unparseable output: bill the tokens, then let the caller degrade.
        self._record(
            prompt_name,
            LLMResult({}, "", model, "portkey", tokens_in, tokens_out, _cost(self.settings, tokens_in, tokens_out)),
        )
        raise ValueError(f"unparseable JSON from model: {last_err}")

    def _mock(self, prompt_name: str, variables: dict, model: str, reason: str) -> LLMResult:
        if reason not in RUN_USAGE.notes:
            RUN_USAGE.notes.append(reason)
        data = mock_response(prompt_name, variables)
        result = LLMResult(data=data, text=json.dumps(data), model=f"mock:{model}", provider="mock")
        self._record(prompt_name, result)
        return result

    def _record(self, prompt_name: str, result: LLMResult) -> None:
        RUN_USAGE.calls += 1
        if result.provider == "mock":
            RUN_USAGE.mock += 1
        elif result.cached:
            RUN_USAGE.cached += 1
        else:
            RUN_USAGE.live += 1
            RUN_USAGE.input_tokens += result.input_tokens
            RUN_USAGE.output_tokens += result.output_tokens
            RUN_USAGE.cost_usd += result.cost_usd
        row = LlmCall(
            command=self.command,
            prompt_name=prompt_name,
            provider=result.provider,
            model=result.model,
            input_tokens=0 if result.cached else result.input_tokens,
            output_tokens=0 if result.cached else result.output_tokens,
            est_cost_usd=0.0 if result.cached else result.cost_usd,
            cached=result.cached,
        )
        with _pending_lock:
            _pending.append(row)
        for attempt in range(3):  # never fail a note because another writer holds the lock
            flush_pending()
            if not _pending:
                return
            time.sleep(1 + attempt)
        log.warning("LLM ledger write for %s deferred (database busy); it still counts toward the cap", prompt_name)
