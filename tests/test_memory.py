from cbs.config import ROOT
from response_helpers import problems, extraction
import json
import math

from fastapi.testclient import TestClient
import pytest

from cbs import clock, probe
from cbs.config import load_config
from cbs.llm import FakeLLMClient, RoutingLLMClient
from cbs.memory import retrieve, strength
from cbs.pipeline import PreJudge, TurnPipeline, assemble_packet
from cbs.store import Store
from cbs.web import create_app

EMPTY = {"claims": [], "ledger_updates": [], "new_canon": []}
EPISODE = {"summary": "食事について話した", "details": "ユーザーは食事制限を始めた。テスト係は応援した。", "importance": 0.7, "pinned": False}
FACT = {"operation": "new", "fact_id": None, "content": "ユーザーはダイエット中", "importance": 0.8, "pinned": False}


def add(store, sid, at, status="completed"):
    with clock.at(at):
        return store.add_turn(sid, "u", "a", {}, status=status)


@pytest.mark.parametrize("second,same", [("2026-10-12 10:29:59", True), ("2026-10-12 10:30:00", False)])
def test_gap_boundary(store, second, same):
    with clock.at("2026-10-12 10:00"):
        sid = store.create_session('fixture')["id"]
        add(store, sid, clock.iso())
    add(store, sid, second)
    turns = store.history(sid)
    assert (turns[0]["conversation_id"] == turns[1]["conversation_id"]) == same
    assert len(store.memory_jobs()) == (0 if same else 1)


def test_four_am_and_relationship_days(store):
    with clock.at("2026-10-12 03:59"):
        sid = store.create_session('fixture')["id"]
    add(store, sid, "2026-10-12 03:59")
    add(store, sid, "2026-10-12 04:00")
    assert len({t["conversation_id"] for t in store.history(sid)}) == 2
    assert store.relationship_metrics('fixture')["days"] == 2
    add(store, sid, "2026-10-12 23:59")
    add(store, sid, "2026-10-13 00:01")
    assert store.history(sid)[-1]["conversation_id"] == store.history(sid)[-2]["conversation_id"]
    assert store.relationship_metrics('fixture')["days"] == 2


def test_actor_reply_crossing_four_am_still_closes_conversation(store):
    with clock.at("2026-10-12 03:59"):
        sid = store.create_session('fixture')["id"]
        store.add_turn(sid, "u", "a", {}, created_at=clock.iso(), assistant_at="2026-10-12T04:01:00+09:00")
    add(store, sid, "2026-10-12 04:02")
    assert store.history(sid)[0]["conversation_id"] != store.history(sid)[1]["conversation_id"]
    add(store, sid, "2026-10-13 04:01", status="failed")
    assert store.relationship_metrics('fixture')["days"] == 2


def test_steward_counts_different_conversations_inside_one_session(store, character):
    from cbs.steward import apply_output, StewardOutput
    ep = {"domain": "CBS10", "mode": "enacted", "situation": "好意を告げられた", "reaction": "理由を聞いた", "salient_conditions": []}
    proposal = {"target": "CBS10", "interpretation": "first_observation", "proposed_status": "tentative",
                "proposed_known": "理由を聞く", "episode_refs": ["new:0"], "rationale": "観察"}
    with clock.at("2026-10-12 10:00"):
        sid = store.create_session('fixture')["id"]
        tid = store.add_turn(sid, "u", "a", {})
        apply_output(store, character, tid, StewardOutput(episodes=[ep], updates=[proposal]))
    with clock.at("2026-10-12 11:00"):
        tid = store.add_turn(sid, "u", "a", {})
        applied = apply_output(store, character, tid, StewardOutput(episodes=[ep], updates=[{**proposal, "proposed_status": "supported", "episode_refs": ["episode:1", "new:0"]}]))
        assert applied["applied"]
        assert len({e["conversation_id"] for e in store.list_episodes('fixture')}) == 2


def test_forty_exchanges_chunk_open_and_no_duplicates(store):
    with clock.at("2026-10-12 10:00"):
        sid = store.create_session('fixture')["id"]
        store.add_turn(sid, "bad", None, {}, status="failed")
        for _ in range(39):
            store.add_turn(sid, "u", "a", {})
        assert not store.memory_jobs()
        store.add_turn(sid, "u", "a", {})
        assert len(store.memory_jobs()) == 1
        assert store.list_conversations()[0]["status"] == "open"
        store.add_turn(sid, "u", "a", {})
        store.sweep_conversations(force=True)
        store.sweep_conversations(force=True)
        jobs = store.memory_jobs()
        assert len(jobs) == 2 and jobs[0]["last_turn"] < jobs[1]["first_turn"]
        assert len({t["conversation_id"] for t in store.history(sid)}) == 1


def test_new_session_sweep_and_startup(character, store):
    with clock.at("2026-10-12 10:00"):
        a = store.create_session('fixture')["id"]
        store.add_turn(a, "u", "a", {})
        b = store.create_session('fixture')["id"]
        store.add_turn(b, "u", "a", {})
        assert store.history(a)[0]["conversation_id"] != store.history(b)[0]["conversation_id"]
        assert store.list_conversations()[0]["status"] == "closed"
    with clock.at("2026-10-12 10:30"):
        config = load_config()
        config.character.id = character.model.character_id
        pipeline = TurnPipeline(config, character, store, FakeLLMClient())
        pipeline.wait_for_steward()
        assert len(store.list_memory_episodes('fixture')) == 2
        assert all(c["status"] == "episodized" for c in store.list_conversations())


@pytest.mark.parametrize("then,expected", [
    ("2026-10-12 13:45", "さっき"), ("2026-10-12 09:00", "今朝"),
    ("2026-10-11 23:00", "昨日"), ("2026-10-09 12:00", "3日前"),
    ("2026-10-04 12:00", "先週"), ("2026-09-20 12:00", "3週間前（9/20）"),
    ("2026-08-01 12:00", "2か月前（8月）"), ("2025-08-01 12:00", "去年の夏ごろ"),
    ("2026-10-13 12:00", "明日"), ("2026-10-17 12:00", "5日後（10/17）")])
def test_relative_labels(then, expected):
    with clock.at("2026-10-12 14:00"):
        assert clock.relative(then) == expected
        assert clock.absolute(then).startswith(then[:4] + "/")
        assert clock.calendar(clock.iso())["来週月曜"] == "2026-10-19"


def test_clock_gap_and_reset():
    before = clock.now()
    with clock.at("2026-10-01 20:00"):
        assert clock.gap("2026-09-29 20:00") == "2日ぶり"
        assert clock.gap("2026-10-01 17:00") == "3時間ぶり"
        assert clock.calendar(clock.iso())["来週月曜"] == "2026-10-05"
    assert clock.now() >= before


def test_strength_formula():
    memory = {"importance": 0.8, "recall_count": 2, "pinned": False, "created_at": "2026-09-01T20:00:00+09:00"}
    expected = 0.8 + 0.3 * math.log(3)
    assert strength(memory, "2026-10-01T20:00:00+09:00") == pytest.approx(expected / 2)
    memory["last_recalled_at"] = "2026-10-01T20:00:00+09:00"
    assert strength(memory, "2026-10-01T20:00:00+09:00") == pytest.approx(expected)
    memory["pinned"] = True
    assert strength(memory, "2027-10-01T20:00:00+09:00") == pytest.approx(expected)


def remember(pipeline, at, facts):
    with clock.at(at):
        sid = pipeline.store.create_session('fixture')["id"]
        pipeline.store.add_turn(sid, "u", "a", {})
        pipeline.client.episodizer_responses.append({"episodes": [EPISODE], "user_facts": facts})
        pipeline.close_conversation(sid)
        pipeline.wait_for_steward()


def test_fact_new_confirm_update_and_recall_final_only(pipeline):
    remember(pipeline, "2026-09-01 20:00", [FACT])
    remember(pipeline, "2026-09-20 20:00", [{**FACT, "operation": "confirm", "fact_id": 1}])
    fact = pipeline.store.list_user_facts('fixture')[0]
    assert fact["learned_at"].startswith("2026-09-01") and fact["last_confirmed_at"].startswith("2026-09-20")
    remember(pipeline, "2026-10-10 20:00", [{**FACT, "operation": "update", "fact_id": 1, "content": "ダイエットをやめた"}])
    old, current = pipeline.store.list_user_facts('fixture')
    assert old["status"] == "superseded" and old["superseded_by"] == current["id"]
    assert [f["id"] for f in pipeline.store.list_user_facts('fixture', current_only=True)] == [2]
    use = lambda ref: {"ref": ref, "relevant_now": "yes", "counterevidence": "no", "continuation": "no"}
    first = {**EMPTY, "used_memories": ["memory:1", "fact:2"], "claims": [{"text": "昨日", "verdict": "contradicted", "reason": "9月の記憶"}]}
    final = {**EMPTY, "used_memories": ["fact:2", "fact:2", "fact:1", "memory:999"]}
    pipeline.client.text_responses = ["昨日の話だね", "今はやめたんだね"]
    pipeline.client.json_responses = [{"memory_use": [use("memory:1"), use("fact:2"), use("fact:1")]}, problems(first), problems(final)]
    pipeline.client.extract_responses = [extraction(final)]
    with clock.at("2026-10-12 19:00"):
        result = pipeline.run(pipeline.store.create_session('fixture')["id"], "ラーメン食べたい")
        pipeline.wait_for_steward()
        packet = pipeline.store.get_trace(result["turn_id"])["Packet"]["output"]
        assert [f["ref"] for f in packet["user_facts"]] == ["fact:2"]
        assert packet["memory_episodes"][0]["time"]["relative"] != "昨日"
        assert pipeline.store.list_user_facts('fixture')[1]["recall_count"] == 1
        assert pipeline.store.list_user_facts('fixture')[0]["recall_count"] == 0
        assert all(m["recall_count"] == 0 for m in pipeline.store.list_memory_episodes('fixture'))
        assert pipeline.store.list_user_facts('fixture')[1]["last_recalled_at"] == clock.iso()


def test_superseded_never_in_packet_even_as_counterevidence(character):
    memory = {"ref": "fact:9", "status": "superseded", "content": "古い状態"}
    judge = PreJudge(memory_use=[{"ref": "fact:9", "relevant_now": "yes", "counterevidence": "yes", "continuation": "no"}])
    packet = assemble_packet(character, judge, candidate_memories=[memory])
    assert not packet["user_facts"] and not packet["counterevidence_memories"]


def test_episodizer_failure_retry_idempotence_and_event_date(pipeline):
    with clock.at("2026-08-01 22:00"):
        sid = pipeline.store.create_session('fixture')["id"]
        pipeline.store.add_turn(sid, "昨日、猫が亡くなった", "悲しいね", {})
        pipeline.client.episodizer_responses = ["invalid"]
        pipeline.close_conversation(sid)
        pipeline.wait_for_steward()
        assert not pipeline.store.list_memory_episodes('fixture')
        pipeline.client.episodizer_responses = [{"episodes": [EPISODE], "user_facts": [{**FACT, "content": "7/31に猫が亡くなった", "event_date": "2026-07-31"}]}]
        pipeline.maintenance()
        pipeline.wait_for_steward()
        pipeline.maintenance()
        pipeline.wait_for_steward()
        assert len(pipeline.store.list_memory_episodes('fixture')) == 1
        assert pipeline.store.list_user_facts('fixture')[0]["event_date"] == "2026-07-31"
        context = json.loads(next(c for c in pipeline.client.calls if c.get("schema", {}).get("title") == "EpisodizerOutput")["messages"][0]["content"])
        assert context["turns"][0]["date_lookup"]["昨日"] == "2026-07-31"


def test_embedding_failure_rolls_back_memory_and_turn_can_continue(pipeline):
    with clock.at("2026-10-12 10:00"):
        sid = pipeline.store.create_session('fixture')["id"]
        pipeline.store.add_turn(sid, "u", "a", {})
        pipeline.client.episodizer_responses = [{"episodes": [EPISODE], "user_facts": [FACT]}]
        pipeline.client.embedding_responses = [[[1.0, 0.0]]]  # Two texts require two independent vectors.
        pipeline.close_conversation(sid)
        pipeline.wait_for_steward()
        assert not pipeline.store.list_memory_episodes('fixture') and not pipeline.store.list_user_facts('fixture')
        pipeline.client.json_responses = [{}, problems(EMPTY)]
        pipeline.client.extract_responses = [extraction(EMPTY)]
        result = pipeline.run(sid, "会話は続けられる？")
        pipeline.wait_for_steward()
        assert result["assistant_text"]
        pipeline.client.episodizer_responses = [{"episodes": [EPISODE], "user_facts": [FACT]}]
        pipeline.maintenance()
        pipeline.wait_for_steward()
        assert len(pipeline.store.list_memory_episodes('fixture')) == 1 and len(pipeline.store.list_user_facts('fixture')) == 1


def test_fact_learned_time_uses_the_actual_source_turn(pipeline):
    with clock.at("2026-10-12 10:00"):
        sid = pipeline.store.create_session('fixture')["id"]
        first = pipeline.store.add_turn(sid, "ダイエット中", "うん", {})
    with clock.at("2026-10-12 10:05"):
        last = pipeline.store.add_turn(sid, "別の話題", "うん", {})
        pipeline.client.episodizer_responses = [{"episodes": [{**EPISODE, "first_turn": first, "last_turn": last}],
                                               "user_facts": [{**FACT, "learned_turn": first}]}]
        pipeline.close_conversation(sid)
        pipeline.wait_for_steward()
        assert pipeline.store.list_user_facts('fixture')[0]["learned_at"].startswith("2026-10-12T10:00")
        assert pipeline.store.list_memory_episodes('fixture')[0]["ended_at"].startswith("2026-10-12T10:05")


def test_retrieval_rank_top_eight_and_embedding_failure_nonblocking(pipeline):
    with clock.at("2026-10-12 12:00"):
        remember(pipeline, clock.iso(), [FACT])
        with pipeline.store.connect() as connection:
            for i in range(10):
                connection.execute("INSERT INTO memory_episodes (character_id, conversation_id, started_at, ended_at, summary, details, importance, pinned, embedding, created_at) "
                    "SELECT character_id, conversation_id, started_at, ended_at, ?, details, ?, pinned, ?, created_at FROM memory_episodes WHERE id=1",
                    (str(i), i / 10, json.dumps([0, 1])))
        pipeline.client.embedding_responses = [[[0, 1]]]
        candidates = retrieve(pipeline, "test", [{"user_text": "前の話", "assistant_text": "テスト係の文脈"}])
        assert "前の話" in pipeline.client.embedding_calls[-1]["texts"][0]
        assert "テスト係の文脈" in pipeline.client.embedding_calls[-1]["texts"][0]
        memories = [m for m in candidates if m["ref"].startswith("memory:")]
        assert len(memories) == 8 and memories[0]["summary"] == "9"
        assert memories[0]["rank"] > memories[-1]["rank"]
        pipeline.client.embedding_responses = [[]]
        pipeline.client.json_responses = [{}, problems(EMPTY)]
        pipeline.client.extract_responses = [extraction(EMPTY)]
        result = pipeline.run(pipeline.store.create_session('fixture')["id"], "test")
        pipeline.wait_for_steward()
        assert pipeline.store.get_trace(result["turn_id"])["Load"]["warnings"]


def test_all_utterances_have_time_and_memory_endpoint(pipeline):
    with clock.at("2026-10-01 20:00"):
        sid = pipeline.store.create_session('fixture')["id"]
        pipeline.client.json_responses = [{}, problems(EMPTY), {}, problems(EMPTY)]
        pipeline.client.extract_responses = [extraction(EMPTY), extraction(EMPTY)]
        pipeline.client.text_responses = ["a", "b"]
        pipeline.run(sid, "first")
        pipeline.wait_for_steward()
    with clock.at("2026-10-01 20:05"):
        result = pipeline.run(sid, "second")
        pipeline.wait_for_steward()
        trace = pipeline.store.get_trace(result["turn_id"])
        assert trace["Load"]["output"]["current_utterance"]["gap"] == "5分ぶり"
        assert trace["Load"]["output"]["recent_turns"][0]["assistant_time"]["absolute"] == "2026/10/1(木) 20:00"
        assert all("2026/10/1" in m["content"] for m in trace["Actor"]["request"]["messages"][1:])
        post = json.loads(trace["Check"]["request"]["messages"][0]["content"])
        assert post["assistant_time"]["iso"] == clock.iso()
        pipeline.close_conversation(sid)
        pipeline.wait_for_steward()
        with TestClient(create_app(pipeline)) as client:
            data = client.get("/api/memory").json()
            assert data["episodes"][0]["recall_count"] == 0
            assert "embedding" not in data["episodes"][0]


def test_probe_clock_formats_and_maintenance(tmp_path, monkeypatch):
    suite = tmp_path / "suite.yaml"
    suite.write_text('probes:\n- id: T\n  category: 時刻\n  expect: 区切り\n  sessions:\n  - at: "2026-10-12 10:00"\n    step_minutes: 5\n    messages: ["a", "b", {text: "c", at: "2026-10-12 11:00"}]\n  - at: "2026-10-13 12:00"\n    maintenance: true\n    messages: ["d"]\n  - ["e"]\n', encoding="utf-8")
    fake = FakeLLMClient(text_responses=["reply"] * 5, json_responses=[v for _ in range(5) for v in ({}, problems(EMPTY))])
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    monkeypatch.setattr(probe, "create_client", lambda config: fake)
    report = probe.run_suite(suite, "clock", character_id="fixture", characters_root=ROOT / "tests/fixtures/characters")
    text = report.read_text(encoding="utf-8")
    assert "2026/10/12(月) 10:05" in text and "会話エピソード" in text and "ユーザーの事実（履歴つき）" in text
    db = next((tmp_path / "data").glob("*/*.sqlite3"))
    store = Store(db)
    assert len(store.list_memory_episodes('fixture')) == 4
    assert len({c["id"] for c in store.list_conversations()}) == 4


def test_embedding_provider_route():
    config = load_config()
    config.models.embedding = "openai/test-embedding"
    route = RoutingLLMClient(config)
    fake = FakeLLMClient(embedding_responses=[[[0.2, 0.8]]])
    route._clients["openai"] = fake
    assert route.embed(["text"]) == [[0.2, 0.8]]
    assert fake.embedding_calls[0]["model"] == "test-embedding"


def test_gemini_embeddings_use_separate_contents():
    from types import SimpleNamespace
    from cbs.llm.gemini import GeminiLLMClient
    from google.genai import types
    client = GeminiLLMClient.__new__(GeminiLLMClient)
    client._key = "secret-key"
    def embed_content(model, contents):
        assert all(isinstance(c, types.Content) for c in contents)
        assert [c.parts[0].text for c in contents] == ["first", "[REDACTED]"]
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[1, 0]), SimpleNamespace(values=[0, 1])])
    client._client = SimpleNamespace(models=SimpleNamespace(embed_content=embed_content))
    assert client.embed(["first", "secret-key"]) == [[1, 0], [0, 1]]


def test_openai_embedding_endpoint_and_order():
    from types import SimpleNamespace
    from cbs.llm.openai_client import OpenAILLMClient
    client = OpenAILLMClient.__new__(OpenAILLMClient)
    client._zdr = False
    client._key = "secret-key"
    def create(model, input):
        assert model == "embedding" and input == ["first", "[REDACTED]"]
        return SimpleNamespace(data=[SimpleNamespace(index=1, embedding=[0, 1]), SimpleNamespace(index=0, embedding=[1, 0])])
    client._client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    assert client.embed(["first", "secret-key"], "embedding") == [[1, 0], [0, 1]]
