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
from localledger.safety import guard_variables, record_event

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
    cached_input_tokens: int = 0  # provider-side prompt cache reads (billed at the cached rate)
    reasoning_tokens: int = 0  # part of output_tokens
    latency_ms: int = 0


@dataclass
class RunUsage:
    calls: int = 0
    live: int = 0
    cached: int = 0
    mock: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.calls:
            return "LLM: no calls"
        return (
            f"LLM: {self.calls} calls ({self.live} live, {self.cached} cached, {self.mock} mock) | "
            f"tokens in={self.input_tokens:,} (cached {self.cached_input_tokens:,}) out={self.output_tokens:,} "
            f"(reasoning {self.reasoning_tokens:,}) | est. cost ${self.cost_usd:.4f}"
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


def _cost(settings: Settings, tokens_in: int, tokens_out: int, cached_in: int = 0) -> float:
    fresh = max(0, tokens_in - cached_in)
    return (
        fresh * settings.price_in_per_mtok
        + cached_in * settings.price_cached_in_per_mtok
        + tokens_out * settings.price_out_per_mtok
    ) / 1_000_000


LESS_REASONING = {"default": "low", "xhigh": "medium", "high": "low", "medium": "low", "low": "none"}


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

            # The Portkey gateway may replay an identical earlier request from its own cache; benchmarks force a
            # fresh completion so the measured tokens and latency are real.
            headers = {"x-portkey-cache-force-refresh": "true"} if os.environ.get("LL_FORCE_FRESH") == "1" else None
            self._client = OpenAI(
                api_key=os.environ["PORTKEY_API_KEY"], base_url=self.settings.llm_base_url, default_headers=headers
            )
        return self._client

    def model_for(self, tier: str) -> str:
        return self.settings.model_heavy if tier == "heavy" else self.settings.model_fast

    def effort_for(self, prompt) -> str:
        """LLM_REASONING_EFFORT=prompt uses each prompt's own setting; any other value overrides every prompt."""
        effort = self.settings.reasoning_effort
        return prompt.reasoning_effort if effort == "prompt" else effort

    def json(self, prompt_name: str, variables: dict, *, tier: str | None = None) -> LLMResult:
        global _reserved_usd
        prompt = load_prompt(prompt_name)
        variables = guard_variables(prompt_name, variables)  # untrusted document text is filtered before any prompt
        tier = tier or prompt.tier
        model = self.model_for(tier)
        system, user = prompt.render(variables)
        effort = self.effort_for(prompt)
        parts = [prompt_name, prompt.raw, model, system, user] + ([] if effort == "default" else [effort])
        key = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
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
                cached_input_tokens=cached.get("cached_input_tokens", 0),
                reasoning_tokens=cached.get("reasoning_tokens", 0),
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
                if note not in RUN_USAGE.notes:
                    record_event("budget_cap", prompt=prompt_name, spent_usd=round(spent, 4))
                return self._mock(prompt_name, variables, model, reason=note)
            _reserved_usd += est_cost
        try:
            result = self._call(prompt_name, system, user, model, prompt.max_tokens, effort=effort)
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
                    "cached_input_tokens": result.cached_input_tokens,
                    "reasoning_tokens": result.reasoning_tokens,
                }
            )
        )
        self._record(prompt_name, result)
        return result

    async def ajson(self, prompt_name: str, variables: dict, *, tier: str | None = None) -> LLMResult:
        return await asyncio.to_thread(self.json, prompt_name, variables, tier=tier)

    def _call(
        self,
        prompt_name: str,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        *,
        effort: str = "default",
    ) -> LLMResult:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        client = self._openai()
        tokens_in = tokens_out = cached_in = reasoning = 0
        last_err: Exception | None = None
        started = time.monotonic()
        for _attempt in range(2):
            extra = {} if effort == "default" else {"reasoning_effort": effort}
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                max_completion_tokens=max_tokens,
                **extra,
            )
            if resp.usage:
                tokens_in += resp.usage.prompt_tokens or 0
                tokens_out += resp.usage.completion_tokens or 0
                pd, cd = resp.usage.prompt_tokens_details, resp.usage.completion_tokens_details
                cached_in += (getattr(pd, "cached_tokens", 0) or 0) if pd else 0
                reasoning += (getattr(cd, "reasoning_tokens", 0) or 0) if cd else 0
            text = resp.choices[0].message.content or ""
            try:
                data = _extract_json(text)
            except (ValueError, json.JSONDecodeError) as exc:
                last_err = exc
                if resp.choices[0].finish_reason == "length":
                    # Reasoning used up the output budget. Repeating the same request burns the same tokens again,
                    # so retry with less reasoning rather than more output.
                    effort = LESS_REASONING.get(effort, "none")
                    log.info("%s hit max_completion_tokens; retrying with reasoning_effort=%s", prompt_name, effort)
                continue
            return LLMResult(
                data=data,
                text=text,
                model=model,
                provider="portkey",
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cost_usd=_cost(self.settings, tokens_in, tokens_out, cached_in),
                cached_input_tokens=cached_in,
                reasoning_tokens=reasoning,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        # Both attempts returned unparseable output: bill the tokens, then let the caller degrade.
        self._record(
            prompt_name,
            LLMResult(
                {},
                "",
                model,
                "portkey",
                tokens_in,
                tokens_out,
                _cost(self.settings, tokens_in, tokens_out, cached_in),
                cached_input_tokens=cached_in,
                reasoning_tokens=reasoning,
            ),
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
            RUN_USAGE.cached_input_tokens += result.cached_input_tokens
            RUN_USAGE.reasoning_tokens += result.reasoning_tokens
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
            cached_input_tokens=0 if result.cached else result.cached_input_tokens,
            reasoning_tokens=0 if result.cached else result.reasoning_tokens,
            latency_ms=0 if result.cached else result.latency_ms,
            saved_input_tokens=result.input_tokens if result.cached else 0,
            saved_output_tokens=result.output_tokens if result.cached else 0,
        )
        with _pending_lock:
            _pending.append(row)
        for attempt in range(3):  # never fail a note because another writer holds the lock
            flush_pending()
            if not _pending:
                return
            time.sleep(1 + attempt)
        log.warning("LLM ledger write for %s deferred (database busy); it still counts toward the cap", prompt_name)
