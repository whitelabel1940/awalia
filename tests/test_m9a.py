import base64
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cbs import clock
from cbs.config import load_config, embedding_model, equivalent_embedding_model
from cbs.llm import FakeLLMClient, LLMError
from cbs.memory import retrieve
from cbs.note import fill_past_notes, public_note
from cbs.reindex import reindex
from cbs.storage import pack_vector, unpack_vector
from cbs.store import Store
from cbs.web import create_app


def api(pipeline, tmp_path):
    root = tmp_path / "characters"
    (root / "fixture").mkdir(parents=True)
    return TestClient(create_app(pipeline, characters_root=root, settings_dir=tmp_path / "settings"))


def test_float32_and_legacy_migration_preserve_dimensions_and_retrieval(pipeline):
    s = pipeline.store
    sid = s.create_session('fixture')["id"]
    cid = s.prepare_conversation(sid, clock.iso())
    values = [[1.0, 0.0], [0.25, 0.75], [-1.0, 0.1]]
    with s.connect() as db:
        for n, vector in enumerate(values):
            db.execute("INSERT INTO memory_episodes (character_id,conversation_id,started_at,ended_at,summary,details,importance,pinned,embedding,created_at) VALUES ('fixture',?,?,?,?,?,0.5,1,?,?)",
                       (cid, clock.iso(), clock.iso(), str(n), str(n), json.dumps(vector), clock.iso()))
    before = retrieve(pipeline, "query", [])
    Store(s.path)
    after = retrieve(pipeline, "query", [])
    assert [m["ref"] for m in before] == [m["ref"] for m in after]
    assert [m["similarity"] for m in after] == pytest.approx([m["similarity"] for m in before], abs=1e-7)
    with s.connect() as db:
        assert all(r[0] == "blob" and r[1] == 8 for r in db.execute("SELECT typeof(embedding),length(embedding) FROM memory_episodes"))
    vector = [0.123456789] * 2048
    assert len(pack_vector(vector)) == 2048 * 4
    assert unpack_vector(pack_vector(vector)) == pytest.approx(vector)


def test_trim_preserves_all_reveal_metrics_and_decisions(pipeline):
    s = pipeline.store
    with clock.at("2026-01-01 12:00"):
        sid = s.create_session('fixture')["id"]
        trace = {"Packet": {"input": {"emotional_weight": "high", "user_disclosure": "yes"},
                            "output": {"secrets": [{"secret_id": "secret_lottery", "allowed_level": 1, "truth": {"L1": "hidden"}}]}},
                 "Check": {"request": {"system": "x" * 5000}, "response": {"text": "raw", "parsed": {"problems": []}}, "usage": {"tokens": 10}, "latency_ms": 42},
                 "Commit": {"input": {"assistant_text": "x"}, "output": {"turn_id": 1}}}
        tid = s.add_turn(sid, "user", "reply", trace)
        s.add_turn(sid, "failed", None, trace, status="failed")
        other = s.create_session("other")["id"]
        s.add_turn(other, "u", "a", trace)
        pending = s.add_turn(sid, "u", "a", {**trace, "Extract": {"state": "pending"}})
    metrics = s.relationship_metrics('fixture')
    result = s.trim_traces()
    assert result["bytes_removed"] > 0
    assert s.relationship_metrics('fixture') == metrics
    short = s.get_trace(tid)
    assert short["Check"]["response"]["parsed"] == {"problems": []}
    assert short["Check"]["usage"] == {"tokens": 10}
    assert short["Check"]["latency_ms"] == 42
    assert "request" not in short["Check"] and "text" not in short["Check"]["response"]
    assert "request" in s.get_trace(pending)["Check"]
    assert s.history(sid)[0]["assistant_text"] == "reply"


def test_reindex_atomic_failure_and_model_mismatch(pipeline):
    s = pipeline.store
    old = s.embedding_model
    with s.connect() as db:
        db.execute("INSERT INTO reply_embeddings VALUES ('old', ?)", (pack_vector([1., 0.]),))
        # Startup also warms seed sources; failure must preserve the entire cache.
        cached = [tuple(row) for row in db.execute("SELECT * FROM reply_embeddings ORDER BY cache_key")]
    pipeline.config.models.embedding = "openai/new-embedding"
    s.embedding_model = embedding_model(pipeline.config)
    with pytest.raises(ValueError, match="reindex"):
        retrieve(pipeline, "query", [])
    class Failed(FakeLLMClient):
        def embed(self, texts, model=None, *, role="document"):
            raise LLMError("failure")
    with pytest.raises(LLMError):
        reindex(s, Failed(), pipeline.config)
    with s.connect() as db:
        assert equivalent_embedding_model(db.execute("SELECT model FROM embedding_index").fetchone()[0]) == equivalent_embedding_model(old)
        assert [tuple(row) for row in db.execute("SELECT * FROM reply_embeddings ORDER BY cache_key")] == cached
    progress = []
    result = reindex(s, pipeline.client, pipeline.config, lambda done, total: progress.append((done, total)))
    assert result["completed"] == len(s.list_interests('fixture'))
    s.require_embedding_model()
    assert progress[-1][0] == progress[-1][1]
    with s.connect() as db:
        assert db.execute("SELECT count(*) FROM reply_embeddings").fetchone()[0] == 0


def test_expression_parallel_rewrite_and_final_persistence(pipeline):
    entered, checked = Event(), Event()
    class Parallel(FakeLLMClient):
        def generate_json(self, model, system, messages, schema):
            if schema.__name__ == "Expression":
                entered.set()
                assert checked.wait(2), "Expression and Check must run concurrently"
            if schema.__name__ == "Check":
                assert entered.wait(2)
                checked.set()
            return super().generate_json(model, system, messages, schema)
    pipeline.client = Parallel(text_responses=["original", "rewritten"], json_responses=[{},
        {"problems": [{"text": "original", "verdict": "forbidden", "reason": "unsupported"}]}, {"problems": []}],
        expression_responses=[{"expression": "sad"}, {"expression": "smile"}])
    sid = pipeline.store.create_session('fixture')["id"]
    reply = pipeline.run(sid, "question")
    pipeline.wait_for_steward()
    assert reply["expression"] == "smile"
    assert pipeline.store.history(sid)[0]["expression"] == "smile"
    trace = pipeline.store.get_trace(reply["turn_id"])
    assert trace["Expression"]["output"]["expression"] == "sad"
    assert trace["Expression-2"]["output"]["expression"] == "smile"


@pytest.mark.parametrize("response", [{"expression": "unknown"}, "invalid"])
def test_expression_fails_to_neutral(pipeline, response):
    pipeline.client.expression_responses = [response]
    reply = pipeline.run(pipeline.store.create_session('fixture')["id"], "hello")
    assert reply["expression"] == "neutral"


def test_settings_overlay_and_secret_values_never_returned(pipeline, tmp_path, monkeypatch, caplog):
    client = api(pipeline, tmp_path)
    assert client.put("/api/settings", json={"character": {"id": "other"}}).status_code == 422
    assert client.put("/api/settings", json={"autonomy": {"inner_life_interval_hours": 3}}).status_code == 422
    changes = {"autonomy": {"inner_life": False, "research_daily_max": 1, "activities_daily_max": 2, "opening": "reserved", "quiet_start_hour": 22, "quiet_end_hour": 7}, "pipeline": {"post_check": "async"}}
    assert client.put("/api/settings", json=changes).status_code == 200
    cfg = load_config(data_dir=tmp_path / "settings")
    assert cfg.persona.ai_question == pipeline.config.persona.ai_question == "immersive"
    assert "persona" not in client.get("/api/settings").json()
    assert client.put("/api/settings", json={"persona": {"ai_question": "honest"}}).status_code == 422
    assert pipeline.store.autonomy_budget("fixture")["research_remaining"] == 1
    monkeypatch.setenv("GEMINI_API_KEY", "environment-priority")
    saved = client.put("/api/settings/keys", json={"provider": "gemini", "key": "synthetic-private-key"})
    assert saved.status_code == 200 and "synthetic-private-key" not in saved.text
    assert client.get("/api/settings").json()["keys"]["gemini"] == "set"
    assert "synthetic-private-key" not in client.get("/api/settings").text + caplog.text
    invalid = client.put("/api/settings/keys", json={"provider": "bad", "key": "synthetic-private-key"})
    assert "synthetic-private-key" not in invalid.text
    monkeypatch.setenv("GEMINI_API_KEY", "environment-priority")
    load_config(data_dir=tmp_path / "settings")
    import os
    assert os.environ["GEMINI_API_KEY"] == "environment-priority"


def test_settings_reindex_job_and_local_provider(pipeline, tmp_path):
    client = api(pipeline, tmp_path)
    with client:
        response = client.put("/api/settings", json={"models": {"embedding": "local/embed"},
                    "llm": {"mode": "advanced", "providers": {"local": {"base_url": "http://localhost:1234/v1"}}}})
        assert response.status_code == 200 and response.json()["reindex_required"]
        assert client.post("/api/maintenance/reindex").status_code == 202
    assert client.get("/api/maintenance/reindex").json()["status"] == "completed"
    assert not client.get("/api/settings").json()["reindex_required"]


def test_reserved_off_inner_life_and_missing_research_key(pipeline, tmp_path, monkeypatch):
    from cbs.autonomy import catch_up, research_allowed
    from cbs.llm import RoutingLLMClient
    sid = pipeline.store.create_session('fixture')["id"]
    with clock.at("2026-10-10 12:00"):
        pipeline.store.add_turn(sid, "u", "a", {})
    pipeline.config.autonomy.opening = "reserved"
    with clock.at("2026-10-11 11:59"):
        assert not pipeline.proactive(sid)["speak"]
    pipeline.config.autonomy.opening = "off"
    with clock.at("2026-10-12 12:00"):
        assert not pipeline.proactive(sid)["speak"]
        pipeline.config.autonomy.inner_life = False
        count = len(pipeline.client.calls)
        catch_up(pipeline)
        assert len(pipeline.client.calls) == count
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    pipeline.client = RoutingLLMClient(pipeline.config)
    assert not research_allowed(pipeline)
    assert api(pipeline, tmp_path).get("/api/settings").json()["warnings"]


def test_note_uses_disclosed_turn_only_no_hidden_ids(pipeline, tmp_path):
    client = api(pipeline, tmp_path)
    assert client.get("/api/note").json()["past"] == []
    sid = pipeline.store.create_session('fixture')["id"]
    tid = pipeline.store.add_turn(sid, "question", "昔は港で暮らしていた。", {})
    with pipeline.store.connect() as db:
        db.execute("INSERT INTO reveal_events(character_id,secret_id,level,source_turn,created_at) VALUES ('fixture','secret_lottery',1,?,?)", (tid, clock.iso()))
    pipeline.client.note_responses = [{"text": "昔は港で暮らしていた。"}]
    fill_past_notes(pipeline)
    fill_past_notes(pipeline)
    note = client.get("/api/note").json()
    assert note["past"] == ["昔は港で暮らしていた。"] and note["interests"]
    assert not any(word in json.dumps(note) for word in ("secret_lottery", "character_id", "source_id", "source_change", "allowed_level"))
    inputs = [json.loads(c["messages"][0]["content"]) for c in pipeline.client.calls if c.get("schema", {}).get("title") == "PastNote"]
    assert inputs == [{"assistant_text": "昔は港で暮らしていた。"}]


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


def test_look_raw_image_without_pillow_and_packet(pipeline, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    client = api(pipeline, tmp_path)
    assert client.put("/api/look/neutral", content=PNG, headers={"Content-Type": "image/png"}).status_code == 200
    assert client.get("/looks/fixture/sad").content == PNG
    assert client.put("/api/look/no-slot", content=PNG, headers={"Content-Type": "image/png"}).status_code == 422
    assert client.put("/api/look/smile", content=b"x" * (40 * 1024 * 1024 + 1), headers={"Content-Type": "image/png"}).status_code == 413
    assert client.put("/api/look/meta", json={"accent": "akane", "features": "白い髪、青い服"}).status_code == 200
    prompt = client.post("/api/look/prompt", json={"features": "白い髪、青い服", "free_text": "illustration"}).json()["prompt"]
    assert "白い髪、青い服" in prompt and "4" in prompt and "日本" not in prompt
    reply = pipeline.run(pipeline.store.create_session('fixture')["id"], "hi")
    assert pipeline.store.get_trace(reply["turn_id"])["Packet"]["output"]["author_look"] == "白い髪、青い服"
    assert client.delete("/api/look/neutral").status_code == 200
    assert client.get("/looks/fixture/sad").status_code == 404


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_sse_events_and_safe_delta(pipeline, tmp_path, mode):
    pipeline.config.pipeline.post_check = mode
    pipeline.client.stream_chunks = [["safe " * 30, "Pack", "et internal"], ["correct " * 30]]
    client = api(pipeline, tmp_path)
    sid = pipeline.store.create_session('fixture')["id"]
    response = client.post(f"/api/sessions/{sid}/messages/stream", json={"content": "hello"})
    assert response.status_code == 200 and "text/event-stream" in response.headers["content-type"]
    assert "event: stage" in response.text and "event: reply" in response.text
    assert "Packet" not in response.text
    assert "event: delta" not in response.text
    assert pipeline.client.stream_chunks  # drafts were not streamed before Check
    reply = json.loads(response.text.split("event: reply\ndata: ")[1].split("\n")[0])
    assert reply["expression"] in ("neutral", "thinking") and reply["assistant_at"]
    if reply["expression"] == "thinking":
        expression = json.loads(response.text.split("event: expression\ndata: ")[1].split("\n")[0])
        assert expression == {"turn_id": reply["turn_id"], "expression": "neutral"}


def test_sse_second_leak_is_error_and_failed_turn(pipeline, tmp_path):
    pipeline.config.pipeline.post_check = "async"
    pipeline.client.text_responses = ["CBS09", "life_ref"]
    client = api(pipeline, tmp_path)
    sid = pipeline.store.create_session('fixture')["id"]
    response = client.post(f"/api/sessions/{sid}/messages/stream", json={"content": "hi"})
    assert "event: error" in response.text and "event: reply" not in response.text
    assert "CBS09" not in response.text and "life_ref" not in response.text
    assert pipeline.store.history(sid)[0]["status"] == "failed"


def test_sse_provider_without_stream_returns_reply_only(pipeline, tmp_path):
    class OneShot(FakeLLMClient):
        generate_text_stream = None
    pipeline.client = OneShot()
    pipeline.config.pipeline.post_check = "async"
    client = api(pipeline, tmp_path)
    sid = pipeline.store.create_session('fixture')["id"]
    response = client.post(f"/api/sessions/{sid}/messages/stream", json={"content": "hi"})
    assert "event: reply" in response.text and "event: delta" not in response.text


@pytest.mark.parametrize("marker", ["Packet", "CBS09", "R01", "life_ref", "secret_lottery", "allowed_level", "memory:123", "sig_reason"])
def test_stream_guard_every_chunk_boundary(pipeline, marker):
    pipeline.config.pipeline.post_check = "async"
    for split in range(1, len(marker)):
        pipeline.client = FakeLLMClient(text_responses=["a" * 100 + " " + marker[:split] + marker[split:], "safe reply"])
        events = []
        result = pipeline._actor_text("system", [], {}, "Actor", lambda event, data: events.append((event, data)))
        assert result.text == "safe reply"
        assert not events
        visible = "".join(data["text"] for event, data in events if event == "delta")
        assert marker not in visible


def test_stream_guard_split_credential(pipeline, monkeypatch):
    secret = "private-" * 30
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    class Redacting(FakeLLMClient):
        def sanitize_text(self, text):
            return text.replace(secret, "[REDACTED]")
    pipeline.client = Redacting(text_responses=["start " * 50 + secret[:140] + secret[140:] + " end"])
    pipeline.config.pipeline.post_check = "async"
    events = []
    result = pipeline._actor_text("system", [], {}, "Actor", lambda event, data: events.append((event, data)))
    visible = "".join(data["text"] for event, data in events if event == "delta")
    assert not visible and secret not in pipeline.client.sanitize_text(result.text)


def test_expression_on_proactive_turn(pipeline):
    pipeline.client.json_responses = [{'problems': []}]
    pipeline.client.proactive_responses = [{"speak": True, "reason": "greet"}]
    pipeline.client.expression_responses = [{"expression": "smile"}]
    reply = pipeline.proactive(pipeline.store.create_session('fixture')["id"])
    assert reply["speak"] and reply["expression"] == "smile"
    assert pipeline.store.history(reply["session_id"])[0]["kind"] == "proactive"


def test_expression_timeout_is_neutral():
    from concurrent.futures import Future, TimeoutError
    from cbs.expression import finish_expression
    class TimedOut(Future):
        def result(self, timeout=None):
            raise TimeoutError()
    trace = {}
    assert finish_expression(TimedOut(), trace) == "neutral"
    assert trace["Expression"]["error"]


def test_reserved_opening_at_24_hour_boundary(pipeline):
    sid = pipeline.store.create_session('fixture')["id"]
    with clock.at("2026-10-10 12:00"):
        pipeline.store.add_turn(sid, "u", "a", {})
    pipeline.config.autonomy.opening = "reserved"
    pipeline.client.proactive_responses = [{"speak": True, "reason": "greet"}]
    pipeline.client.json_responses = [{"problems": []}]
    with clock.at("2026-10-11 12:00"):
        assert pipeline.proactive(sid)["speak"]


def test_daily_backfills_past_notes(pipeline):
    from cbs.memory import daily_maintenance
    sid = pipeline.store.create_session('fixture')["id"]
    tid = pipeline.store.add_turn(sid, "u", "祖母と暮らした時期があった。", {})
    with pipeline.store.connect() as db:
        db.execute("INSERT INTO reveal_events(character_id,secret_id,level,source_turn,created_at) VALUES ('fixture','secret_lottery',1,?,?)", (tid, clock.iso()))
    pipeline.client.note_responses = [{"text": "祖母と暮らした時期があった。"}]
    with clock.at("2027-01-01 12:00"):
        daily_maintenance(pipeline)
    assert public_note(pipeline)["past"] == ["祖母と暮らした時期があった。"]


def test_reindex_cli_uses_current_config(pipeline, monkeypatch, capsys):
    import importlib
    module = importlib.import_module("cbs.reindex")
    monkeypatch.setattr(module, "load_config", lambda: pipeline.config)
    monkeypatch.setattr(module, "create_client", lambda cfg: FakeLLMClient())
    monkeypatch.setattr(sys, "argv", ["cbs.reindex", "--db", str(pipeline.store.path)])
    module.main()
    result = json.loads(capsys.readouterr().out)
    assert result["model"] == embedding_model(pipeline.config) and result["total"] > 0


def test_reindex_rejects_concurrent_memory_change_and_stale_writer(pipeline):
    store = pipeline.store
    old = store.embedding_model
    pipeline.config.models.embedding = "openai/new-model"
    class Racing(FakeLLMClient):
        def embed(self, texts, model=None, *, role="document"):
            with store.connect() as db:
                db.execute("UPDATE interests SET detail=detail || ' changed' WHERE id=1")
            return super().embed(texts, model, role=role)
    with pytest.raises(ValueError, match="再試行"):
        reindex(store, Racing(), pipeline.config)
    store.require_embedding_model(old)
    reindex(store, FakeLLMClient(), pipeline.config)
    store.embedding_model = old
    with store.connect() as db, pytest.raises(ValueError, match="reindex"):
        store.check_embedding_model(db, writing=True)


@pytest.mark.parametrize("value", ["plain-test-key", '"quoted-test-key"', "'single-test-key'"])
def test_secrets_env_supports_normal_env_syntax(tmp_path, monkeypatch, value):
    from cbs.config import load_secrets
    monkeypatch.delenv("M9_TEST_KEY", raising=False)
    path = tmp_path / "secrets.env"
    path.write_text("# comment\nM9_TEST_KEY=" + value + "\n", encoding="utf-8")
    load_secrets(path)
    import os
    assert os.environ["M9_TEST_KEY"].endswith("test-key")
    monkeypatch.delenv("M9_TEST_KEY")
