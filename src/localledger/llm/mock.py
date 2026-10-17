"""Deterministic stand-in for the LLM.

Used in tests, when PORTKEY_API_KEY is not set, and once the budget cap is reached. Each pipeline module
registers an extractive mock for its prompt so the product still works (less eloquently) with no LLM.
"""

from __future__ import annotations

from collections.abc import Callable

MockFn = Callable[[dict], dict]
_MOCKS: dict[str, MockFn] = {}


def register_mock(prompt_name: str) -> Callable[[MockFn], MockFn]:
    def deco(fn: MockFn) -> MockFn:
        _MOCKS[prompt_name] = fn
        return fn

    return deco


def mock_response(prompt_name: str, variables: dict) -> dict:
    fn = _MOCKS.get(prompt_name)
    if fn is None:
        return {"mock": True, "text": f"[mock output for {prompt_name}]"}
    return fn(variables)
