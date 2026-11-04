from response_helpers import problems, extraction
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from cbs import clock
from cbs.config import MemoryConfig, PipelineConfig, load_config
from cbs.llm import FakeLLMClient
from cbs.memory import MonthlyDigest, daily_maintenance, digest_candidates, link_episode, retrieve
from cbs.pipeline import PreJudge, TurnPipeline, assemble_packet
from cbs.web import create_app

EMPTY = {"claims": [], "ledger_updates": [], "new_canon": []}


def episode(store, when="2026-06-01 21:00", *, importance=0.5, pinned=False, vector=(1, 0), status="active"):
    with clock.at(when):
        sid = store.create_session('fixture')["id"]
        cid = store.prepare_conversation(sid)
        with store.connect() as db:
            row = db.execute("INSERT INTO memory_episodes (character_id, conversation_id, started_at, ended_at, summary, details, importance, pinned, embedding, status, created_at) VALUES ('fixture', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (cid, clock.iso(), clock.iso(), "花火の思い出", "ユーザーは祖母の家で線香花火をした。テスト係は話を聞いた。", importance, pinned, json.dumps(vector), status, clock.iso()))
            return row.lastrowid


def use(ref, counter="no"):
    return {"ref": ref, "relevant_now": "yes", "counterevidence": counter, "continuation": "no"}


def test_link_top_five_storage_direction_and_failure_isolated(pipeline):
    pipeline.wait_for_steward()
    for i in range(6):
        episode(pipeline.store, vector=(1, i / 10))
    newest = episode(pipeline.store, "2026-06-10 21:00")
    pipeline.client.linker_responses = [{"links": [
        {"target_episode": 1, "relation": "updates", "reason": "状態の変化"},
        {"target_episode": 2, "relation": "continues", "reason": "続き"},
        {"target_episode": 3, "relation": "contradicts", "reason": "食い違い"},
        {"target_episode": 4, "relation": "same_topic", "reason": "別件"},
        {"target_episode": 5, "relation": "none", "reason": "無関係"},
        {"target_episode": 6, "relation": "updates", "reason": "候補外"}]}]
    link_episode(pipeline, newest)
    call = pipeline.client.calls[-1]
    assert call["model"] == pipeline.config.models.judge
    assert [e["id"] for e in json.loads(call["messages"][0]["content"])["neighbours"]] == [1, 2, 3, 4, 5]
    links = pipeline.store.list_episode_links('fixture')
    assert len(links) == 5 and links[0]["source_episode"] == newest
    link_episode(pipeline, newest)
    assert len(pipeline.store.list_episode_links('fixture')) == 5
    pipeline.client.linker_responses = ["bad JSON"]
    link_episode(pipeline, newest)
    assert len(pipeline.store.list_memory_episodes('fixture')) == 7
    with pipeline.store.connect() as db:
        assert db.execute("SELECT status FROM episode_link_runs WHERE source_episode=?", (newest,)).fetchone()[0] == "failed"


def test_packet_follows_updates_chain_even_when_new_not_retrieved(character):
    memories = [{"id": i, "ref": f"memory:{i}", "summary": str(i), "created_at": f"2026-09-0{i}T20:00:00+09:00"} for i in range(1, 4)]
    links = [{"source_episode": 2, "target_episode": 1, "relation": "updates"},
             {"source_episode": 3, "target_episode": 2, "relation": "updates"}]
    packet = assemble_packet(character, PreJudge(memory_use=[use("memory:1", "yes")]),
                             candidate_memories=memories[:1], memory_episodes=memories, episode_links=links)
    for key in ("memory_episodes", "counterevidence_memories"):
        old, middle, new = packet[key]
        assert old["memory_state"] == middle["memory_state"] == "previous"
        assert old["superseded_by"] == "memory:2" and middle["superseded_by"] == "memory:3"
        assert new["ref"] == "memory:3" and new["memory_state"] == "current"
    assert all("time" in m for m in packet["memory_episodes"])


def test_linker_failure_leaves_episodizer_completed(pipeline):
    pipeline.wait_for_steward()
    episode(pipeline.store)
    pipeline.client.linker_responses = ["invalid"]
    with clock.at("2026-06-10 21:00"):
        sid = pipeline.store.create_session('fixture')["id"]
        pipeline.store.add_turn(sid, "近況", "聞いたよ", {})
        pipeline.close_conversation(sid)
        pipeline.wait_for_steward()
        assert len(pipeline.store.list_memory_episodes('fixture')) == 2
        with pipeline.store.connect() as db:
            assert db.execute("SELECT status FROM memory_jobs").fetchone()[0] == "completed"
            assert db.execute("SELECT status FROM episode_link_runs WHERE source_episode=2").fetchone()[0] == "failed"
        assert pipeline.run(sid, "会話は続く？")["assistant_text"]


def test_async_web_background_gate_starts_check_after_reply(pipeline):
    import asyncio
    from fastapi import BackgroundTasks
    pipeline.wait_for_steward()
    pipeline.config.pipeline.post_check = "async"
    tasks = BackgroundTasks()
    sid = pipeline.store.create_session('fixture')["id"]
    result = pipeline.run(sid, "こんにちは", background_tasks=tasks)
    try:
        assert result["assistant_text"]
        assert any(c.get("schema", {}).get("title") == "Check" for c in pipeline.client.calls)
        assert pipeline.store.get_trace(result["turn_id"])["Check"]["state"] == "completed"
    finally:
        asyncio.run(tasks())
    pipeline.wait_for_steward()
    assert pipeline.store.get_trace(result["turn_id"])["Check"]["state"] == "completed"


def test_digest_rules_exact_boundaries(store):
    ids = [episode(store, "2026-07-17 21:00", importance=0.5),  # exactly 60 days
           episode(store, "2026-07-17 21:00:01", importance=0.5),  # one second too young
           episode(store, "2026-07-17 21:00", importance=0.8),  # exactly strength .2
           episode(store, pinned=True), episode(store, status="archived")]
    with clock.at("2026-09-15 21:00"):
        assert [e["id"] for e in digest_candidates(store.list_memory_episodes('fixture'), MemoryConfig())] == ids[:1]
        cfg = MemoryConfig(digest_age_days=0, digest_strength_threshold=1)
        assert [e["id"] for e in digest_candidates(store.list_memory_episodes('fixture'), cfg)] == ids[:3]


def test_daily_digest_once_local_day_merge_month_and_failure(pipeline):
    pipeline.wait_for_steward()
    first = episode(pipeline.store)
    episode(pipeline.store, pinned=True)
    with clock.at("2026-09-15 23:59"):
        pipeline.maintenance()
        pipeline.wait_for_steward()
        digest = pipeline.store.list_monthly_digests('fixture')[0]
        assert digest["episode_ids"] == [first]
        assert pipeline.store.list_memory_episodes('fixture')[0]["status"] == "archived"
        assert pipeline.store.list_memory_episodes('fixture')[1]["status"] == "active"
        calls = len([c for c in pipeline.client.calls if c.get("schema", {}).get("title") == "MonthlyDigest"])
        third = episode(pipeline.store)
        pipeline.maintenance()
        pipeline.wait_for_steward()
        assert len([c for c in pipeline.client.calls if c.get("schema", {}).get("title") == "MonthlyDigest"]) == calls
    with clock.at("2026-09-16 00:01"):
        pipeline.maintenance()
        pipeline.wait_for_steward()
        merged = pipeline.store.list_monthly_digests('fixture')
        assert len(merged) == 1 and merged[0]["id"] == digest["id"]
        assert merged[0]["episode_ids"] == [first, third]
        assert len(merged[0]["details"]) > len(merged[0]["summary"])
    fourth = episode(pipeline.store)
    pipeline.client.digest_responses = ["invalid"]
    with clock.at("2026-09-17 21:00"):
        daily_maintenance(pipeline)
        assert pipeline.store.list_memory_episodes('fixture')[-1]["status"] == "active"
        daily_maintenance(pipeline)
    with clock.at("2026-09-18 21:00"):
        daily_maintenance(pipeline)
        assert fourth in pipeline.store.list_monthly_digests('fixture')[0]["episode_ids"]


def test_archived_threshold_digest_packet_and_actual_recall(pipeline):
    pipeline.wait_for_steward()
    old = episode(pipeline.store)
    with clock.at("2026-09-15 21:00"):
        daily_maintenance(pipeline)
        # Known vectors: archived original [1, 0], digest [0, 1]. Baseline = mean similarity of the query.
        with pipeline.store.connect() as connection:
            connection.execute("UPDATE memory_episodes SET embedding=? WHERE id=?", (json.dumps([1.0, 0.0]), old))
            connection.execute("UPDATE monthly_digests SET embedding=?", (json.dumps([0.0, 1.0]),))
        # query [1,0]: original 1.0, digest 0.0, baseline 0.5 -> returns while margin <= 0.5
        for margin, query, expected in [(0.08, [1.0, 0.0], True), (0.5, [1.0, 0.0], True), (0.51, [1.0, 0.0], False),
                                        # query [0.6,0.8]: original 0.6 < baseline 0.7 -> stays archived
                                        (0.0, [0.6, 0.8], False)]:
            pipeline.config.memory.archived_margin = margin
            pipeline.client.embedding_responses = [[query]]
            candidates = retrieve(pipeline, "花火", [])
            assert any(c["ref"] == f"memory:{old}" for c in candidates) == expected
            assert any(c["ref"] == "digest:1" for c in candidates)
        pipeline.config.memory.archived_margin = 0.6
        pipeline.client.embedding_responses = [[[1.0, 0.0]]]
        assert all(c["ref"] != "memory:1" for c in retrieve(pipeline, "花火", []))
        pipeline.client.json_responses = [{"memory_use": [use("digest:1")]}, problems({**EMPTY, "used_memories": ["digest:1", "memory:1"]})]
        pipeline.client.extract_responses = [extraction({**EMPTY, "used_memories": ["digest:1", "memory:1"]})]
        result = pipeline.run(pipeline.store.create_session('fixture')["id"], "花火")
        pipeline.wait_for_steward()
        packet = pipeline.store.get_trace(result["turn_id"])["Packet"]["output"]
        assert packet["monthly_digests"][0]["time"]["month"] == "2026-06"
        assert not packet["memory_episodes"]
        assert pipeline.store.list_monthly_digests('fixture')[0]["recall_count"] == 1
        assert pipeline.store.list_memory_episodes('fixture')[0]["recall_count"] == 0
        with TestClient(create_app(pipeline)) as web:
            data = web.get("/api/memory").json()
            assert data["digests"] and data["episodes"][0]["status"] == "archived" and "links" in data


class BlockingCheck(FakeLLMClient):
    def __init__(self, store, check, **kwargs):
        super().__init__(**kwargs)
        self.store, self.check = store, check
        self.entered, self.release, self.next_judge = Event(), Event(), Event()
        self.check_count = 0

    def generate_json(self, model, system, messages, schema):
        if schema.__name__ == "Check":
            self.check_count += 1
            if self.check_count == 1:
                self.entered.set()
                assert self.release.wait(5)
                self.json_responses.insert(0, problems(self.check))
                self.extract_responses.insert(0, extraction(self.check))
        elif schema.__name__ == "PreJudge" and self.check_count:
            self.next_judge.set()
        elif schema.__name__ == "StewardOutput":
            assert self.store.list_canon('fixture') and self.store.list_plans('fixture')
            self.json_responses.insert(0, {"episodes": [], "updates": []})
        return super().generate_json(model, system, messages, schema)


def test_legacy_async_check_blocks_publication_until_recovery(pipeline):
    pipeline.wait_for_steward()
    pipeline.config.pipeline.post_check='async'
    fake=BlockingCheck(pipeline.store, {**EMPTY,'claims':[{'text':'bad','verdict':'forbidden','reason':'unshared'}]},
        text_responses=['bad','safe'],json_responses=[{},problems(EMPTY)],extract_responses=[extraction(EMPTY)])
    pipeline.client=fake
    sid=pipeline.store.create_session('fixture')['id']
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(pipeline.run,sid,'question')
        try:
            assert fake.entered.wait(2)
            assert not future.done() and not pipeline.store.history(sid)
        finally: fake.release.set()
        result=future.result(timeout=5)
    pipeline.wait_for_steward()
    assert result['assistant_text']=='safe'
    assert pipeline.store.get_trace(result['turn_id'])['Safety']['accepted_check']=='Check-2'
    assert not pipeline.store.pending_corrections('fixture')

def test_legacy_async_failed_check_requires_recovery(pipeline):
    pipeline.wait_for_steward()
    pipeline.config.pipeline=PipelineConfig(post_check='async',background_wait_seconds=.02)
    fake=BlockingCheck(pipeline.store,'invalid',text_responses=['bad','safe'],json_responses=[{},problems(EMPTY)])
    pipeline.client=fake
    fake.release.set()
    result=pipeline.run(pipeline.store.create_session('fixture')['id'],'question')
    pipeline.wait_for_steward()
    trace=pipeline.store.get_trace(result['turn_id'])
    assert trace['Check']['state']=='failed' and trace['Safety']['accepted_check']=='Check-2'
    assert result['assistant_text']=='safe' and not pipeline.store.pending_corrections('fixture')

def test_async_disclosures_and_used_memories_saved(pipeline):
    pipeline.wait_for_steward()
    pipeline.config.pipeline.post_check = "async"
    episode(pipeline.store, "2026-10-12 10:00")
    pipeline.store.save_secret("fixture", "secret_lottery", {"L1": "温かい気持ち", "L2": "隠れた輪郭", "L3": "隠れた真相"})
    pipeline.client = FakeLLMClient(json_responses=[{"memory_use": [use("memory:1")], "reveal_request": {"secret_id": "secret_lottery", "manner": "sincere"}}, problems({**EMPTY, "used_memories": ["memory:1"], "disclosures": [{"secret_id": "secret_lottery", "level": 1}],
                                                   "claims": []})], extract_responses=[extraction({**EMPTY, "used_memories": ["memory:1"], "disclosures": [{"secret_id": "secret_lottery", "level": 1}], "claims": []})])
    result = pipeline.run(pipeline.store.create_session('fixture')["id"], "話して")
    pipeline.wait_for_steward()
    assert pipeline.store.list_memory_episodes('fixture')[0]["recall_count"] == 1
    assert pipeline.store.list_secrets('fixture')[0]["disclosed_level"] == 1
    assert pipeline.store.get_trace(result["turn_id"])["Commit"]["output"]["reveal_events"]
    # Even when checker reasons mention hidden truth, only the approved guidance reaches the next Packet.
    corrections = pipeline.store.pending_corrections('fixture')
    assert not corrections


def test_pending_correction_survives_restart_and_failed_actor(pipeline):
    from cbs.llm import LLMError
    from cbs.store import Store
    pipeline.wait_for_steward()
    sid = pipeline.store.create_session('fixture')["id"]
    first = pipeline.store.add_turn(sid, "u", "wrong", {})
    with pipeline.store.connect() as db:
        db.execute("INSERT INTO pending_corrections (character_id, source_turn, correction_json, created_at) VALUES ('fixture', ?, ?, ?)",
                   (first, json.dumps([{"text": "wrong", "verdict": "contradicted", "reason": "予定は未実行"}]), clock.iso()))
    reopened = Store(pipeline.store.path)
    fake = FakeLLMClient(text_responses=["", "さっきのは違ったね"], json_responses=[{}, {}, problems(EMPTY)], extract_responses=[extraction(EMPTY)])
    other = TurnPipeline(pipeline.config.model_copy(deep=True), pipeline.character, reopened, fake)
    other.characters_root = pipeline.characters_root
    with pytest.raises(LLMError):
        other.run(sid, "failed")
    assert reopened.pending_corrections('fixture')
    result = other.run(sid, "try again")
    assert reopened.get_trace(result["turn_id"])["Packet"]["output"]["pending_correction"]
    assert not reopened.pending_corrections('fixture')
    other.wait_for_steward()
