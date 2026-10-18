from sqlmodel import select

from localledger.config import get_settings, reset_settings
from localledger.llm import client as llm_client
from localledger.llm.client import LLM, LLMResult, _extract_json
from localledger.llm.prompts import load_prompt
from localledger.models import LlmCall

VARS = {"brand": "Local Ledger", "claims": [{"text": "t", "quote": "q", "document_id": 1, "context": "c"}]}


def test_prompts_load_from_files():
    p = load_prompt("factcheck_claims")
    assert p.tier == "fast" and p.max_tokens > 0
    system, user = p.render(VARS)
    assert "Local Ledger" in system and "claim 0" in user


def test_extract_json_tolerates_fences():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('noise {"b": [1,2]} trailing') == {"b": [1, 2]}


def test_mock_when_no_key(env, session):
    res = LLM("test").json("factcheck_claims", VARS)
    assert res.provider == "mock"
    rows = session.exec(select(LlmCall)).all()
    assert rows and rows[0].provider == "mock" and rows[0].est_cost_usd == 0


def _fake_live(monkeypatch, cost=0.01):
    monkeypatch.setenv("LLM_PROVIDER", "portkey")
    monkeypatch.setenv("PORTKEY_API_KEY", "test-not-a-real-key")
    reset_settings()
    calls = []

    def fake_call(self, prompt_name, system, user, model, max_tokens, **_kw):
        calls.append(prompt_name)
        return LLMResult(
            data={"results": []},
            text="{}",
            model=model,
            provider="portkey",
            input_tokens=100,
            output_tokens=50,
            cost_usd=cost,
        )

    monkeypatch.setattr(LLM, "_call", fake_call)
    return calls


def test_disk_cache_makes_reruns_free(env, session, monkeypatch):
    calls = _fake_live(monkeypatch)
    first = LLM("test").json("factcheck_claims", VARS)
    second = LLM("test").json("factcheck_claims", VARS)
    assert first.provider == "portkey" and not first.cached
    assert second.cached and len(calls) == 1
    costs = [r.est_cost_usd for r in session.exec(select(LlmCall).order_by(LlmCall.id)).all()]
    assert costs == [0.01, 0.0]


def test_budget_cap_degrades_to_mock(env, session, monkeypatch):
    calls = _fake_live(monkeypatch, cost=0.02)
    monkeypatch.setenv("LLM_BUDGET_USD", "0.05")
    reset_settings()
    assert get_settings().budget_usd == 0.05
    session.add(LlmCall(command="x", prompt_name="p", provider="portkey", model="m", est_cost_usd=0.049))
    session.commit()
    res = LLM("test").json("factcheck_claims", VARS)
    assert res.provider == "mock" and not calls
    assert any("budget cap" in n for n in llm_client.RUN_USAGE.notes)


def test_cost_bills_cached_input_at_the_cached_rate(env):
    s = get_settings()
    full = llm_client._cost(s, 10_000, 1_000)
    cached = llm_client._cost(s, 10_000, 1_000, cached_in=8_000)
    assert cached < full
    expected = (2_000 * s.price_in_per_mtok + 8_000 * s.price_cached_in_per_mtok + 1_000 * s.price_out_per_mtok) / 1e6
    assert cached == expected


def test_reasoning_effort_comes_from_the_prompt_unless_overridden(env, monkeypatch):
    p = load_prompt("warroom_bear")
    assert p.reasoning_effort == "none" and load_prompt("analyst_signal_note").reasoning_effort == "low"
    assert LLM("t").effort_for(p) == "none"
    monkeypatch.setenv("LLM_REASONING_EFFORT", "high")
    reset_settings()
    assert LLM("t").effort_for(p) == "high"


class _Resp:
    def __init__(self, content, finish):
        from types import SimpleNamespace as NS

        self.choices = [NS(message=NS(content=content), finish_reason=finish)]
        self.usage = NS(
            prompt_tokens=1000,
            completion_tokens=500,
            prompt_tokens_details=NS(cached_tokens=200),
            completion_tokens_details=NS(reasoning_tokens=480),
        )


def test_truncated_output_retries_with_less_reasoning(env, monkeypatch):
    sent = []

    class FakeCompletions:
        def create(self, **kw):
            sent.append(kw.get("reasoning_effort"))
            return _Resp("", "length") if len(sent) == 1 else _Resp('{"ok": true}', "stop")

    from types import SimpleNamespace as NS

    llm = LLM("t")
    llm._client = NS(chat=NS(completions=FakeCompletions()))
    res = llm._call("x", "sys", "user", "gpt-6-luna", 100, effort="low")
    assert sent == ["low", "none"] and res.data == {"ok": True}
    assert res.input_tokens == 2000 and res.cached_input_tokens == 400 and res.reasoning_tokens == 960


def test_disk_cache_hit_records_tokens_saved(env, session, monkeypatch):
    _fake_live(monkeypatch)
    LLM("test").json("factcheck_claims", VARS)
    LLM("test").json("factcheck_claims", VARS)
    hit = session.exec(select(LlmCall).where(LlmCall.cached == True)).one()  # noqa: E712
    assert hit.saved_input_tokens == 100 and hit.saved_output_tokens == 50 and hit.est_cost_usd == 0
