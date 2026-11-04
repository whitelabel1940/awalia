from response_helpers import problems, extraction
import json

import pytest

from cbs.llm import FakeLLMClient, LLMError


EMPTY = {"claims": [], "ledger_updates": [], "new_canon": []}


def check(verdict="new_minor_detail", content="師匠の口癖は『まず一息』", updates=None):
    return {"claims": [{"text": content, "verdict": verdict, "reason": "根拠との比較"}],
            "ledger_updates": updates or [],
            "new_canon": [{"kind": "past_detail", "content": content, "life_ref": None}] if verdict == "new_minor_detail" else []}


def update(operation="agree", plan_id=None):
    return {"operation": operation, "plan_id": plan_id, "content": "カフェに行く",
            "scheduled_text": "今度の土曜日", "scheduled_date": "2026-10-17"}


def test_commit_cross_session_memory_and_provenance(pipeline):
    pipeline.client = FakeLLMClient(text_responses=["まず一息。土曜にカフェへ行こう。", "約束したけどまだだよ。"],
                                   json_responses=[{}, problems(check(updates=[update()])), {"memory_use": [{"ref": "plan:1", "relevant_now": "yes", "counterevidence": "yes", "continuation": "no"}]}, problems(EMPTY)], extract_responses=[extraction(check(updates=[update()])), extraction(EMPTY)])
    first_session = pipeline.store.create_session('fixture')
    first = pipeline.run(first_session["id"], "口癖は？土曜カフェ行こう")
    pipeline.wait_for_steward()
    second_session = pipeline.store.create_session('fixture')
    second = pipeline.run(second_session["id"], "この前行ったね")
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(second["turn_id"])
    load = trace["Load"]["output"]
    assert {"canon:1", "plan:1"} <= {memory["ref"] for memory in load["candidate_memories"]}
    assert load["current_datetime"]["weekday"] in "月火水木金土日"
    assert load["previous_session_datetime"] is not None
    assert load["recent_turns"] == []
    packet = trace["Packet"]["output"]
    assert packet["counterevidence_memories"][0]["state"] == "agreed"
    assert packet["unfinished_ledger"][0]["scheduled_date"] == "2026-10-17"
    assert pipeline.store.list_canon('fixture')[0]["source_turn"] == first["turn_id"]
    assert pipeline.store.list_plans('fixture')[0]["source_turn"] == first["turn_id"]
    assert trace["Check"]["model"] == pipeline.config.models.judge


def test_rewrite_once_and_commit_final_only(pipeline):
    bad = check(updates=[update()])
    bad["claims"].append({"text": "もう行った", "verdict": "forbidden", "reason": "未実行"})
    final = check(content="師匠の口癖は『考えてから』")
    pipeline.client = FakeLLMClient(text_responses=["土曜のカフェにもう行った。まず一息。", "考えてから、だったよ。"], json_responses=[{}, problems(bad), problems(final)], extract_responses=[extraction(final)])
    session = pipeline.store.create_session('fixture')
    result = pipeline.run(session["id"], "昔の口癖は？")
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(result["turn_id"])
    assert result["assistant_text"] == "考えてから、だったよ。"
    assert trace["Rewrite"]["pre_rewrite_text"] == "土曜のカフェにもう行った。まず一息。"
    assert "未実行" in [c for c in pipeline.client.calls if "schema" not in c][1]["messages"][-1]["content"]
    assert trace["Check-2"]["response"]["parsed"] == {"problems": []}
    context = json.loads(trace["Extract"]["request"]["messages"][0]["content"])
    assert context["check_result"] == {"problems": []}
    assert trace["Extract"]["response"]["parsed"] == {**extraction(final),
        "research_questions": [],
        "day_operations": [], "used_days": [],
        "request_operations": [],
        "new_canon": [{**f, "event_date": None} for f in final["new_canon"]]}
    assert [fact["content"] for fact in pipeline.store.list_canon('fixture')] == [final["new_canon"][0]["content"]]
    assert pipeline.store.list_plans('fixture') == []


def test_remaining_bad_claim_requires_further_check(pipeline):
    pipeline.client = FakeLLMClient(text_responses=['bad', 'bad again', 'safe'],
        json_responses=[{}, problems(check('forbidden','bad')), problems(check('forbidden','bad again')), {'problems': []}])
    result = pipeline.run(pipeline.store.create_session('fixture')['id'], 'question')
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(result['turn_id'])
    assert result['assistant_text'] == 'safe'
    assert trace['Safety']['accepted_check'] == 'Check-3'
    assert trace['Safety']['rewrite_count'] == 2

@pytest.mark.parametrize("rewrite", [False, True])
def test_check_failure_requires_success_before_extract(pipeline, rewrite):
    responses = [{}, problems(check('contradicted')), 'invalid JSON', {'problems': []}] if rewrite else [{}, 'invalid JSON', {'problems': []}]
    pipeline.client = FakeLLMClient(text_responses=['original', 'rewritten', 'safe'], json_responses=responses)
    result = pipeline.run(pipeline.store.create_session('fixture')['id'], 'question')
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(result['turn_id'])
    assert result['assistant_text'] == ('safe' if rewrite else 'rewritten')
    assert trace['Check-2' if rewrite else 'Check']['state'] == 'failed'
    assert trace['Extract']['state'] == 'completed'
    context = json.loads(trace['Extract']['request']['messages'][0]['content'])
    assert context['check_result'] == {'problems': []}

def test_invalid_plan_id_warned_and_canon_still_commits(pipeline):
    pipeline.client = FakeLLMClient(json_responses=[{}, problems(check(updates=[update(plan_id=999), update("cancel")]))], extract_responses=[extraction(check(updates=[update(plan_id=999), update("cancel")]))])
    session = pipeline.store.create_session('fixture')
    result = pipeline.run(session["id"], "教えて")
    pipeline.wait_for_steward()
    commit = pipeline.store.get_trace(result["turn_id"])["Commit"]["output"]
    assert commit["warnings"] == ["unknown plan_id ignored: 999", "plan_id required: cancel"]
    assert len(commit["canon_facts"]) == 1 and pipeline.store.list_plans('fixture') == []


@pytest.mark.parametrize("provider_error", [False, True])
def test_rewrite_actor_failure_never_returns_original_problem(pipeline, provider_error):
    class FailingRewrite(FakeLLMClient):
        def generate_text(self, model, system, messages):
            if len(self.text_responses) == 1 and provider_error:
                raise LLMError('failed rewrite')
            return super().generate_text(model, system, messages)
    pipeline.client = FailingRewrite(text_responses=['bad', ''], json_responses=[{}, problems(check('forbidden','bad'))])
    sid = pipeline.store.create_session('fixture')['id']
    with pytest.raises(LLMError): pipeline.run(sid, 'question')
    turn = pipeline.store.history(sid)[0]
    assert turn['status'] == 'failed' and turn['assistant_text'] is None
    trace = pipeline.store.get_trace(turn['id'])
    assert 'error' in trace['Rewrite'] and trace['Safety']['blocked']

def test_post_check_and_canon_redaction(pipeline):
    class RedactingFake(FakeLLMClient):
        def sanitize_text(self, text):
            return text.replace("synthetic-secret", "[REDACTED]")
    pipeline.client = RedactingFake(text_responses=["synthetic-secret"], json_responses=[{}, problems(check(content="synthetic-secret", updates=[{**update(), "content": "synthetic-secret"}]))], extract_responses=[extraction(check(content="synthetic-secret", updates=[{**update(), "content": "synthetic-secret"}]))])
    session = pipeline.store.create_session('fixture')
    result = pipeline.run(session["id"], "synthetic-secret")
    pipeline.wait_for_steward()
    saved = [pipeline.store.list_canon('fixture'), pipeline.store.list_plans('fixture'), pipeline.store.get_trace(result["turn_id"])]
    assert "synthetic-secret" not in json.dumps(saved)
    assert pipeline.store.list_canon('fixture')[0]["content"] == "[REDACTED]"


def test_invalid_iso_date_is_nonblocking_extract_failure(pipeline):
    pipeline.client = FakeLLMClient(json_responses=[{}, problems(check(updates=[{**update(), "scheduled_date": "土曜"}]))], extract_responses=[extraction(check(updates=[{**update(), "scheduled_date": "土曜"}]))])
    session = pipeline.store.create_session('fixture')
    result = pipeline.run(session["id"], "カフェ行こう")
    pipeline.wait_for_steward()
    assert "error" in pipeline.store.get_trace(result["turn_id"])["Extract"]
    assert pipeline.store.list_canon('fixture') == pipeline.store.list_plans('fixture') == []


def test_rewrite_that_invents_forbidden_claim_never_reverts_to_bad_draft(pipeline):
    pipeline.client = FakeLLMClient(text_responses=['wrong', 'forbidden', 'safe'],
        json_responses=[{}, problems(check('contradicted','wrong')), problems(check('forbidden','forbidden')), {'problems': []}])
    result = pipeline.run(pipeline.store.create_session('fixture')['id'], 'question')
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(result['turn_id'])
    assert result['assistant_text'] == 'safe'
    assert 'reverted' not in trace['Rewrite']

def test_check_finding_that_quotes_the_user_does_not_trigger_a_rewrite(pipeline):
    said = "この前一緒に行ったカフェ、楽しかったね！"
    pipeline.client = FakeLLMClient(text_responses=["まだ行ってないよ、土曜の約束だよね。"],
        json_responses=[{}, problems(check("forbidden", said))], extract_responses=[extraction(EMPTY)])
    session = pipeline.store.create_session('fixture')
    result = pipeline.run(session["id"], said)
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(result["turn_id"])
    assert result["assistant_text"] == "まだ行ってないよ、土曜の約束だよね。"
    assert "Rewrite" not in trace and trace["Check"]["ignored"]
