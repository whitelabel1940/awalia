import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import BackgroundTasks
from pydantic import ValidationError

from cbs.llm import FakeLLMClient, LLMResult
from cbs.pipeline import Check
from cbs.runtime import character_prefix


class BlockingExtract(FakeLLMClient):
    def __init__(self, store, *, failure=False):
        super().__init__(text_responses=["返答1", "返答2"], json_responses=[
            {"domains_touched": [{"id": "CBS10", "strength": "direct"}]}, {"problems": []},
            {}, {"problems": []}])
        self.store = store
        self.failure = failure
        self.entered, self.release, self.next_judge = Event(), Event(), Event()
        self.extractions = 0

    def generate_json(self, model, system, messages, schema):
        if schema.__name__ == "Extract":
            self.extractions += 1
            if self.extractions == 1:
                self.entered.set()
                assert self.release.wait(5)
                self.extract_responses.insert(0, "invalid" if self.failure else {
                    "new_canon": [{"kind": "daily_life", "content": "今日紅茶を飲んだ"}],
                    "ledger_updates": [], "disclosures": [], "used_memories": []})
        if schema.__name__ == "PreJudge" and self.extractions:
            self.next_judge.set()
        if schema.__name__ == "StewardOutput":
            assert bool(self.store.list_canon('fixture')) != self.failure
            result = FakeLLMClient(json_responses=[{'episodes': [], 'updates': []}]).generate_json(model, system, messages, schema)
            self.calls.append(result.raw_request)
            return result
        return super().generate_json(model, system, messages, schema)


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("failure", [False, True])
def test_reply_then_extract_next_turn_waits_and_steward_follows(pipeline, mode, failure):
    pipeline.config.pipeline.post_check = mode
    fake = BlockingExtract(pipeline.store, failure=failure)
    pipeline.client = fake
    sid = pipeline.store.create_session('fixture')["id"]
    first = pipeline.run(sid, "話そう")
    assert first["assistant_text"] == "返答1"
    assert fake.entered.wait(2)
    assert not pipeline.store.list_canon('fixture')
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            next_turn = pool.submit(pipeline.run, sid, "次の話")
            assert not fake.next_judge.wait(.1)
            fake.release.set()
            second = next_turn.result(timeout=5)
    finally:
        fake.release.set()
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(first["turn_id"])
    assert trace["Extract"]["state"] == ("failed" if failure else "completed")
    assert trace["Steward"]["state"] == "completed"
    extraction_input = json.loads(trace["Extract"]["request"]["messages"][0]["content"])
    assert extraction_input["check_result"] == trace["Check"]["response"]["parsed"]
    titles = [c.get("schema", {}).get("title", "Actor") for c in fake.calls if c.get("schema", {}).get("title") != "Expression"]
    assert titles[:4] == ["PreJudge", "Actor", "Check", "Extract"]
    assert titles.index('StewardOutput') > titles.index('Extract')
    assert second["assistant_text"] == "返答2"
    assert bool(pipeline.store.get_trace(second["turn_id"])["Load"]["output"]["candidate_memories"]) != failure


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_web_gate_keeps_extraction_after_response(pipeline, mode):
    pipeline.config.pipeline.post_check = mode
    tasks = BackgroundTasks()
    first = pipeline.run(pipeline.store.create_session('fixture')["id"], "こんにちは", background_tasks=tasks)
    try:
        titles = [c.get("schema", {}).get("title") for c in pipeline.client.calls]
        assert "Extract" not in titles
        assert "Check" in titles
        assert pipeline.store.get_trace(first["turn_id"])["Extract"]["state"] == "pending"
    finally:
        asyncio.run(tasks())
    pipeline.wait_for_steward()
    assert pipeline.store.get_trace(first["turn_id"])["Extract"]["state"] == "completed"


def test_check_schema_only_problems_and_domain_descriptions(pipeline):
    with pytest.raises(ValidationError):
        Check(problems=[{"text": "細部", "verdict": "supported", "reason": "既知"}])
    with pytest.raises(ValidationError):
        Check(problems=[], disclosures=[])
    sid = pipeline.store.create_session('fixture')["id"]
    result = pipeline.run(sid, "こんにちは")
    pipeline.wait_for_steward()
    context = pipeline.store.get_trace(result["turn_id"])["Load"]["output"]
    for domain, seed in zip(context["domains"], pipeline.character.model.domains):
        expected = seed.known if seed.status != "open" and seed.known else getattr(seed, "description", "")
        assert domain["description"] == " ".join((expected or "").split())[:160]
    assert all(set(s) == {"id", "when"} for s in context["signatures"])


@pytest.mark.parametrize("usage,expected", [
    ({"input_tokens": 2048, "input_tokens_details": {"cached_tokens": 1024}}, 1024),
    ({"prompt_token_count": 2048, "cached_content_token_count": 1536}, 1536),
    ({"prompt_tokens_details": [{"modality": "TEXT", "token_count": 2048}], "cached_content_token_count": 1536}, 1536),
    ({"input_tokens_details": None, "prompt_tokens_details": []}, 0),
    ({}, 0),
])
def test_cached_token_trace_preserves_provider_usage(usage, expected):
    trace = LLMResult("ok", None, usage, 1, "fake", {}).trace()
    assert trace["cached_tokens"] == expected
    assert trace["usage"] == usage


def test_static_prefix_stays_before_turn_data_and_current_model_wins(pipeline):
    pipeline.client = FakeLLMClient(text_responses=["1", "2"], json_responses=[{}, {"problems": []}, {}, {"problems": []}])
    sid = pipeline.store.create_session('fixture')["id"]
    first = pipeline.run(sid, "最初の固有入力")
    second = pipeline.run(sid, "次の固有入力")
    pipeline.wait_for_steward()
    traces = [pipeline.store.get_trace(r["turn_id"]) for r in (first, second)]
    for step in ("Pre-Judge", "Check", "Extract"):
        assert traces[0][step]["request"]["system"] == traces[1][step]["request"]["system"]
        assert "最初の固有入力" not in traces[0][step]["request"]["system"]
    actors = [t["Actor"]["request"]["system"] for t in traces]
    prefix = character_prefix(pipeline.character, actor=True)
    assert all(prefix in s for s in actors)
    assert actors[0] == actors[1]
    assert all('"model_version"' not in s for s in actors)
    assert all(json.loads(t["Actor"]["request"]["messages"][0]["content"])["packet"] == t["Packet"]["output"] for t in traces)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_final_accepted_check_forwarded_on_extract_failure(pipeline, mode):
    pipeline.config.pipeline.post_check = mode
    first = {'problems': [{'text':'bad','verdict':'forbidden','reason':'first'}]}
    final = {'problems': [{'text':'bad again','verdict':'forbidden','reason':'final'}]}
    pipeline.client = FakeLLMClient(text_responses=['bad','bad again','safe'],
        json_responses=[{},first,final,{'problems':[]}],extract_responses=['invalid'])
    result = pipeline.run(pipeline.store.create_session('fixture')['id'], 'question')
    pipeline.wait_for_steward()
    trace=pipeline.store.get_trace(result['turn_id'])
    context=json.loads(trace['Extract']['request']['messages'][0]['content'])
    assert result['assistant_text']=='safe' and context['check_result']=={'problems':[]}
    assert trace['Extract']['state']=='failed'
    assert not pipeline.store.pending_corrections('fixture')
