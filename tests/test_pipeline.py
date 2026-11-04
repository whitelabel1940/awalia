from response_helpers import problems, extraction
import json

import pytest

from cbs.llm import FakeLLMClient, LLMError

EMPTY_CHECK = {"claims": [], "ledger_updates": [], "new_canon": []}

def test_copied_time_label_is_removed_from_reply(pipeline):
    pipeline.client = FakeLLMClient(text_responses=["こんにちは。", "[2026/10/17(土) 18:02; 2分ぶり] またね。"],
                                    json_responses=[{}, problems(EMPTY_CHECK), {}, problems(EMPTY_CHECK)],
                                    extract_responses=[extraction(EMPTY_CHECK), extraction(EMPTY_CHECK)])
    session = pipeline.store.create_session('fixture')
    pipeline.run(session["id"], "こんにちは")
    pipeline.wait_for_steward()
    second = pipeline.run(session["id"], "またね")
    pipeline.wait_for_steward()
    assert second["assistant_text"] == "またね。"
    actors = [c for c in pipeline.client.calls if "schema" not in c]
    assert actors[1]["messages"][2]["content"].endswith("] こんにちは。")
    assert pipeline.store.get_trace(second["turn_id"])["Actor"]["response"]["text"].startswith("[")


def test_round_trip_full_trace_and_history(pipeline):
    pipeline.client = FakeLLMClient(text_responses=["こんにちは。", "またね。"], json_responses=[{}, problems(EMPTY_CHECK), {}, problems(EMPTY_CHECK)], extract_responses=[extraction(EMPTY_CHECK), extraction(EMPTY_CHECK)])
    session = pipeline.store.create_session('fixture')
    first = pipeline.run(session["id"], "こんにちは")
    pipeline.wait_for_steward()
    second = pipeline.run(session["id"], "またね")
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(first["turn_id"])
    assert set(trace) == {"milestone", "Load", "Pre-Judge", "Packet", "Actor", "Check", "Extract", "Commit", "Expression", "Timeline"}
    assert trace["Pre-Judge"]["response"]["parsed"]["emotional_weight"] == "low"
    assert trace["Actor"]["response"]["text"] == "こんにちは。"
    assert trace["Commit"]["output"] == {"turn_id": first["turn_id"], "status": "completed", "ledger_events": [], "canon_facts": [], "warnings": [], "used_memories": [], "requests": {"applied": [], "rejected": []}, "days": {"applied": [], "rejected": []}}
    assert all("latency_ms" in trace[s] for s in ("Load", "Pre-Judge", "Packet", "Actor", "Check", "Extract", "Commit"))
    actors = [c for c in pipeline.client.calls if "schema" not in c]
    assert [m["role"] for m in actors[1]["messages"]] == ["user", "user", "assistant", "user"]
    assert second["assistant_text"] == "またね。"
    for table in ("ledger_events", "canon_facts", "episodes", "model_versions", "steward_runs"):
        with pipeline.store.connect() as connection:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_bad_judge_json_falls_back_and_turn_completes(pipeline):
    pipeline.client = FakeLLMClient(text_responses=["うん。"], json_responses=["not JSON", "not JSON", problems(EMPTY_CHECK)], extract_responses=[extraction(EMPTY_CHECK)])
    session = pipeline.store.create_session('fixture')
    result = pipeline.run(session["id"], "判断が壊れる入力")
    pipeline.wait_for_steward()
    assert result["assistant_text"] == "うん。"
    turn = pipeline.store.history(session["id"])[0]
    trace = pipeline.store.get_trace(turn["id"])
    assert turn["status"] == "completed"
    assert "error" in trace["Pre-Judge"]
    assert trace["Pre-Judge"]["response"] == {"text": "not JSON", "parsed": None}
    assert trace["Packet"]["input"]["domains_touched"] == []


def test_failed_actor_turn_saved_and_excluded_from_next_turn(pipeline):
    class FailingActor(FakeLLMClient):
        def generate_text(self, model, system, messages):
            if not self.text_responses:
                raise LLMError("actor failed", trace={"model": model, "request": {}, "response": None})
            return super().generate_text(model, system, messages)
    pipeline.client = FailingActor(text_responses=[], json_responses=[{}, {}, problems(EMPTY_CHECK)], extract_responses=[extraction(EMPTY_CHECK)])
    session = pipeline.store.create_session('fixture')
    with pytest.raises(LLMError):
        pipeline.run(session["id"], "失敗する入力")
        pipeline.wait_for_steward()
    failed = pipeline.store.history(session["id"])[0]
    assert failed["status"] == "failed" and failed["assistant_text"] is None
    assert "error" in pipeline.store.get_trace(failed["id"])["Actor"]
    pipeline.client.text_responses.append("うん。")
    pipeline.run(session["id"], "次の入力")
    pipeline.wait_for_steward()
    actor = [call for call in pipeline.client.calls if "schema" not in call][-1]
    assert len(actor["messages"]) == 2
    assert json.loads(pipeline.client.calls[-1]["messages"][0]["content"])["recent_turns"] == []


def test_blank_message_not_saved(pipeline):
    session = pipeline.store.create_session('fixture')
    with pytest.raises(ValueError):
        pipeline.run(session["id"], "  ")
        pipeline.wait_for_steward()
    assert pipeline.store.history(session["id"]) == []
    assert pipeline.client.calls == []


def test_key_redaction_in_all_persisted_turn_data(pipeline):
    class RedactingFake(FakeLLMClient):
        def sanitize_text(self, text):
            return text.replace("synthetic-secret", "[REDACTED]")
    pipeline.client = RedactingFake(text_responses=["synthetic-secret"], json_responses=[{}, problems(EMPTY_CHECK)], extract_responses=[extraction(EMPTY_CHECK)])
    session = pipeline.store.create_session('fixture')
    result = pipeline.run(session["id"], "synthetic-secret")
    pipeline.wait_for_steward()
    assert "synthetic-secret" not in json.dumps(pipeline.store.history(session["id"]))
    assert "synthetic-secret" not in json.dumps(pipeline.store.get_trace(result["turn_id"]))
    assert result["assistant_text"] == "[REDACTED]"


def test_failed_judge_trace_is_redacted(pipeline):
    class RedactingFake(FakeLLMClient):
        def sanitize_text(self, text):
            return text.replace("synthetic-secret", "[REDACTED]")
    pipeline.client = RedactingFake(json_responses=["synthetic-secret: invalid JSON", {}, problems(EMPTY_CHECK)], extract_responses=[extraction(EMPTY_CHECK)])
    session = pipeline.store.create_session('fixture')
    pipeline.run(session["id"], "こんにちは")
    pipeline.wait_for_steward()
    turn = pipeline.store.history(session["id"])[0]
    trace = pipeline.store.get_trace(turn["id"])
    assert trace["Pre-Judge"]["response"]["text"] == "[REDACTED]: invalid JSON"
    assert "synthetic-secret" not in json.dumps(trace)


def test_internal_notes_in_actor_reply_are_retried_then_fail(pipeline):
    from cbs.llm import FakeLLMClient
    pipeline.client = FakeLLMClient(text_responses=["secret_lottery は allowed_level=1 なので", "うん、そうだね。"],
                                    json_responses=[{}, {"problems": []}] * 3)
    session = pipeline.store.create_session('fixture')
    result = pipeline.run(session["id"], "前の場所のこと聞いていい？")
    assert result["assistant_text"] == "うん、そうだね。"
