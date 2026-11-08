from cbs.config import ROOT
from response_helpers import problems, extraction
import json
from threading import Event

import pytest

from cbs.llm import FakeLLMClient
from cbs.pipeline import PreJudge, assemble_packet
from cbs.pipeline import TurnPipeline
from cbs.steward import StewardOutput, apply_output, episode_counts
from cbs.store import Store


def episode(mode="enacted", domain="CBS10", repeat_of=None):
    return {"domain": domain, "mode": mode, "situation": "好意を伝えられた", "reaction": "照れながら聞いた",
            "salient_conditions": ["親しい相手"], "repeat_of": repeat_of}


def update(status="tentative", refs=("new:0",), target="CBS10", **kwargs):
    return {"target": target, "interpretation": "reinforcement", "proposed_status": status,
            "proposed_known": "親しい相手からの好意に照れながら理由を聞く", "conditions": "親しい相手",
            "episode_refs": list(refs), "rationale": "会話中の反応", **kwargs}


def apply(store, character, episodes=(), updates=(), session=None):
    session = session or store.create_session('fixture')
    turn_id = store.add_turn(session["id"], "好き", "えっ", {})
    result = apply_output(store, character, turn_id, StewardOutput.model_validate({"episodes": list(episodes), "updates": list(updates)}))
    return result, turn_id, session


def status(store, character, domain="CBS10"):
    return next(d.status for d in store.current_model(character).domains if d.id == domain)


@pytest.mark.parametrize("mode", ["enacted", "hypothetical"])
def test_open_first_observation_and_db_isolation(store, character, tmp_path, mode):
    result, _, _ = apply(store, character, [episode(mode)], [update()])
    assert status(store, character) == "tentative" and result["applied"]
    assert character.model.domains[9].status == "open"
    assert status(Store(tmp_path / "other.sqlite3"), character) == "open"


@pytest.mark.parametrize("mode,same_session,allowed", [("enacted", False, True), ("enacted", True, False), ("hypothetical", False, False)])
def test_supported_requires_enacted_in_two_sessions(store, character, mode, same_session, allowed):
    _, _, session = apply(store, character, [episode(mode)], [update()])
    result, _, _ = apply(store, character, [episode(mode)], [update("supported", ["episode:1", "new:0"])], session if same_session else None)
    assert status(store, character) == ("supported" if allowed else "tentative")
    assert bool(result["rejected"]) != allowed


@pytest.mark.parametrize("count,sessions,mode,allowed", [(3, 2, "enacted", True), (2, 2, "enacted", False),
                                                       (3, 1, "enacted", False), (3, 2, "hypothetical", False)])
def test_well_supported_thresholds(store, character, count, sessions, mode, allowed):
    character.model.domains[9].status = "supported"
    first = store.create_session('fixture')
    second = store.create_session('fixture') if sessions == 2 else first
    for n in range(count):
        apply(store, character, [episode(mode)], session=first if n == 0 else second)
    refs = [f"episode:{n + 1}" for n in range(count)]
    result, _, _ = apply(store, character, updates=[update("well_supported", refs)])
    assert status(store, character) == ("well_supported" if allowed else "supported")
    assert bool(result["rejected"]) != allowed


@pytest.mark.parametrize("opposing,mode,interpretation,allowed", [(2, "enacted", "unresolved", True),
    (1, "enacted", "unresolved", False), (2, "hypothetical", "unresolved", False), (2, "enacted", "differentiation", False)])
def test_contested_requires_opposing_enacted(store, character, opposing, mode, interpretation, allowed):
    apply(store, character, [episode()], [update()])
    for _ in range(opposing):
        apply(store, character, [episode(mode)])
    opposing_refs = [f"episode:{n + 2}" for n in range(opposing)]
    result, _, _ = apply(store, character, updates=[update("contested", ["episode:1", *opposing_refs],
                              opposing_episode_refs=opposing_refs, interpretation=interpretation)])
    assert status(store, character) == ("contested" if allowed else "tentative")
    assert bool(result["rejected"]) != allowed


def test_repeated_answers_count_once_and_cannot_inflate_contested(store, character):
    _, _, session = apply(store, character, [episode()], [update()])
    apply(store, character, [episode(repeat_of="episode:1")], session=session)
    apply(store, character, [episode(repeat_of="episode:2")], session=session)
    assert episode_counts(store.list_episodes('fixture'))["enacted"] == 1
    result, _, _ = apply(store, character, updates=[update("contested", ["episode:1", "episode:2", "episode:3"],
                           opposing_episode_refs=["episode:2", "episode:3"], interpretation="unresolved")])
    assert result["rejected"] and status(store, character) == "tentative"


@pytest.mark.parametrize("target", ["CBS11", "sig_autonomy_respect"])
def test_author_text_preserved_with_learned(store, character, target):
    # Use an actual author-evidence signature irrespective of its name.
    if target.startswith("sig_"):
        original = next(s for s in character.model.signatures if s.evidence and s.status == "well_supported")
        target = original.id
    else:
        original = next(d for d in character.model.domains if d.id == target)
    before = original.model_dump(mode="json")
    result, _, _ = apply(store, character, [episode(domain="CBS11")], [update(before["status"], target=target, interpretation="differentiation")])
    assert result["applied"]
    items = store.current_model(character).signatures if target.startswith("sig_") else store.current_model(character).domains
    after = next(item.model_dump(mode="json") for item in items if item.id == target)
    for field in ("known", "when", "tends_to"):
        assert after.get(field) == before.get(field)
    assert after["evidence"] == before["evidence"]
    assert after["learned"][0]["conditions"] == "親しい相手"


@pytest.mark.parametrize("same_session,proposed,allowed", [(False, "tentative", True), (True, "tentative", False), (False, "supported", False)])
def test_new_signature_two_sessions_and_tentative(store, character, same_session, proposed, allowed):
    _, _, first = apply(store, character, [episode()])
    apply(store, character, [episode()], session=first if same_session else None)
    result, _, _ = apply(store, character, updates=[update(proposed, ["episode:1", "episode:2"], "new_signature",
                                  signature={"id": "sig_new", "when": "好意", "tends_to": "理由を聞く"})])
    assert any(s.id == "sig_new" for s in store.current_model(character).signatures) == allowed
    assert bool(result["rejected"]) != allowed


@pytest.mark.parametrize("proposed,refs", [("supported", ["new:0"]), ("tentative", []), ("tentative", ["canon:1"]), ("tentative", ["episode:999"])])
def test_invalid_proposals_are_logged_without_versions(store, character, proposed, refs):
    result, turn_id, _ = apply(store, character, [episode()], [update(proposed, refs)])
    assert result["rejected"] and not store.list_model_versions('fixture')
    assert store.list_steward_runs('fixture')[0]["application"] == result
    assert store.get_trace(turn_id)["Steward"]["output"] == result


def test_revert_is_append_only_and_new_changes_still_apply(store, character):
    apply(store, character, [episode()], [update()])
    apply(store, character, [episode()], [update("supported", ["episode:1", "new:0"])])
    assert status(store, character) == "supported"
    store.revert_model(character, 2)
    assert status(store, character) == "tentative"
    assert len(store.list_episodes('fixture')) == 2 and len(store.list_model_versions('fixture')) == 3
    store.revert_model(character, 1)
    assert status(store, character) == "open"
    assert len(store.list_model_versions('fixture')) == 4
    apply(store, character, updates=[update(refs=["episode:1"])])
    assert status(store, character) == "tentative"
    assert Store(store.path).current_model(character) == store.current_model(character)
    with pytest.raises(ValueError):
        store.revert_model(character, 999)


def test_packet_selects_five_episodes_and_current_model(store, character):
    for _ in range(6):
        apply(store, character, [episode()], [update()])
    episodes = store.list_episodes('fixture')
    memories = [{"ref": f"episode:{e['id']}", **e} for e in episodes]
    judge = PreJudge.model_validate({"domains_touched": [{"id": "CBS10", "strength": "direct"}], "memory_use": [
        {"ref": e["ref"], "relevant_now": "yes", "counterevidence": "no", "continuation": "no"} for e in memories]})
    packet = assemble_packet(character, judge, model=store.current_model(character), candidate_memories=memories, episodes=episodes)
    assert len(packet["episodes"]) == 5
    assert packet["touched_domains"][0]["status"] == "tentative"
    assert packet["touched_domains"][0]["episode_counts"]["enacted"] == 6


EMPTY = {"claims": [], "ledger_updates": [], "new_canon": []}
DIRECT = {"domains_touched": [{"id": "CBS10", "strength": "direct"}]}


def test_background_does_not_wait_and_next_turn_sees_model(pipeline):
    entered, release = Event(), Event()
    class BlockingFake(FakeLLMClient):
        def generate_json(self, model, system, messages, schema):
            if schema is StewardOutput:
                entered.set()
                assert release.wait(5)
            return super().generate_json(model, system, messages, schema)
    pipeline.client = BlockingFake(text_responses=["えっ", "覚えてる"], json_responses=[DIRECT, problems(EMPTY), {"episodes": [episode()], "updates": [update()]}, {}, problems(EMPTY)], extract_responses=[extraction(EMPTY), extraction(EMPTY)])
    first = pipeline.store.create_session('fixture')
    result = pipeline.run(first["id"], "好き")
    assert entered.wait(3)
    assert pipeline.store.get_trace(result["turn_id"])["Steward"]["state"] == "pending"
    release.set()
    pipeline.wait_for_steward()
    second = pipeline.run(pipeline.store.create_session('fixture')["id"], "覚えてる？")
    trace = pipeline.store.get_trace(second["turn_id"])
    assert trace["Load"]["output"]["domains"][9]["status"] == "tentative"
    assert trace["Load"]["output"]["candidate_memories"][0]["ref"] == "episode:1"
    # In the session where that turn is still in recent_turns, its episode is not offered again.
    pipeline.wait_for_steward()
    pipeline.client = FakeLLMClient(text_responses=["うん"], json_responses=[DIRECT, problems(EMPTY), {"episodes": [], "updates": []}],
                                    extract_responses=[extraction(EMPTY)])
    same = pipeline.run(first["id"], "さっきの話")
    refs = [m["ref"] for m in pipeline.store.get_trace(same["turn_id"])["Load"]["output"]["candidate_memories"]]
    assert result["turn_id"] in [t["id"] for t in pipeline.store.get_trace(same["turn_id"])["Load"]["output"]["recent_turns"]]
    assert "episode:1" not in refs


@pytest.mark.parametrize("judge", [DIRECT, {"emotional_weight": "high"}])
def test_steward_failure_never_blocks_and_is_logged(pipeline, judge):
    pipeline.client = FakeLLMClient(json_responses=[judge, problems(EMPTY), "invalid JSON"], extract_responses=[extraction(EMPTY)])
    result = pipeline.run(pipeline.store.create_session('fixture')["id"], "好き")
    pipeline.wait_for_steward()
    assert result["assistant_text"]
    trace = pipeline.store.get_trace(result["turn_id"])
    assert trace["Steward"]["state"] == "failed" and trace["Steward"]["warnings"]
    assert pipeline.store.list_model_versions('fixture') == pipeline.store.list_episodes('fixture') == []


@pytest.mark.parametrize("setting", ["immersive", "playful", "honest"])
def test_ai_question_prompts_and_post_check_setting(pipeline, setting):
    pipeline.config.persona.ai_question = setting
    result = pipeline.run(pipeline.store.create_session('fixture')["id"], "AI なの？")
    trace = pipeline.store.get_trace(result["turn_id"])
    assert setting in trace["Actor"]["request"]["system"]
    assert json.loads(trace["Check"]["request"]["messages"][0]["content"])["ai_question"] == setting


def test_same_plan_state_not_appended_and_rescheduled_is_active(pipeline):
    session = pipeline.store.create_session('fixture')
    plan = pipeline.store.add_plan_event(session["id"], "agree", "カフェ")
    assert pipeline.store.add_plan_event(session["id"], "agree", plan_id=plan["plan_id"]) is None
    pipeline.store.add_plan_event(session["id"], "reschedule", plan_id=plan["plan_id"], scheduled_date="2026-10-24")
    assert len(pipeline.store.list_plans('fixture')[0]["events"]) == 2
    result = pipeline.run(session["id"], "予定は？")
    assert pipeline.store.get_trace(result["turn_id"])["Packet"]["output"]["unfinished_ledger"][0]["state"] == "rescheduled"
    assert pipeline.store.add_plan_event(session["id"], "reschedule", plan_id=plan["plan_id"], scheduled_date="2026-10-24") is None
    pipeline.store.add_plan_event(session["id"], "reschedule", plan_id=plan["plan_id"], scheduled_date="2026-10-31")
    assert len(pipeline.store.list_plans('fixture')[0]["events"]) == 3


def test_model_admin_list_and_revert_cli(store, character, monkeypatch, capsys):
    from cbs import model_admin
    from cbs.config import load_config
    config = load_config()
    config.character.id = character.model.character_id
    monkeypatch.setattr(model_admin, "load_config", lambda: config)
    monkeypatch.setattr(model_admin, "load_character", lambda cid: character)
    main = model_admin.main
    apply(store, character, [episode()], [update()])
    monkeypatch.setattr("sys.argv", ["model_admin", "list", "--db", str(store.path)])
    main()
    assert json.loads(capsys.readouterr().out)[0]["id"] == 1
    monkeypatch.setattr("sys.argv", ["model_admin", "revert", "1", "--db", str(store.path)])
    main()
    assert "invalidation record: 2" in capsys.readouterr().out
    assert status(store, character) == "open"


def test_one_episode_per_domain_per_turn(store, character):
    result, _, _ = apply(store, character, [episode(), episode()])
    assert len(result["episodes"]) == 1 and result["rejected"]
    assert len(store.list_episodes('fixture')) == 1


def test_probe_waits_and_reports_growth(tmp_path, monkeypatch):
    from cbs import probe
    suite = tmp_path / "suite.yaml"
    suite.write_text('probes:\n  - id: G1\n    category: 好意\n    expect: 成長\n    sessions:\n      - ["好き"]\n      - ["好き"]\n', encoding="utf-8")
    client = FakeLLMClient(text_responses=["えっ", "ありがとう"], json_responses=[DIRECT, problems(EMPTY), {"episodes": [episode()], "updates": [update()]}, DIRECT, problems(EMPTY), {"episodes": [episode()], "updates": [update("supported", ["episode:1", "new:0"])]}], extract_responses=[extraction(EMPTY), extraction(EMPTY)])
    monkeypatch.setattr(probe, "create_client", lambda config: client)
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    text = probe.run_suite(suite, "growth", character_id="fixture", characters_root=ROOT / "tests/fixtures/characters").read_text(encoding="utf-8")
    assert "経験+1(enacted)" in text and "CBS10 open→tentative" in text and "CBS10 tentative→supported" in text
    assert "人物像の変化（初期値との差）" in text and "episode:2" in text


def test_steward_per_db_is_serialized_across_pipeline_instances(pipeline):
    entered, release, second_entered = Event(), Event(), Event()
    class FirstFake(FakeLLMClient):
        def generate_json(self, model, system, messages, schema):
            if schema is StewardOutput:
                entered.set()
                assert release.wait(5)
            return super().generate_json(model, system, messages, schema)
    class SecondFake(FakeLLMClient):
        def generate_json(self, model, system, messages, schema):
            if schema is StewardOutput:
                second_entered.set()
                context = json.loads(messages[0]["content"])
                assert context["domains"][0]["status"] == "tentative"
            return super().generate_json(model, system, messages, schema)
    pipeline.client = FirstFake(json_responses=[DIRECT, problems(EMPTY), {"episodes": [episode()], "updates": [update()]}], extract_responses=[extraction(EMPTY)])
    other = TurnPipeline(pipeline.config, pipeline.character, Store(pipeline.store.path),
                         SecondFake(json_responses=[DIRECT, problems(EMPTY), {"episodes": [], "updates": []}], extract_responses=[extraction(EMPTY)]))
    other.characters_root = pipeline.characters_root
    first = pipeline.run(pipeline.store.create_session('fixture')["id"], "好き")
    assert entered.wait(3)
    second = other.run(other.store.create_session('fixture')["id"], "好き")
    try:
        assert not second_entered.is_set()
    finally:
        release.set()
    other.wait_for_steward()
    assert second_entered.is_set()
    assert pipeline.store.get_trace(first["turn_id"])["Steward"]["state"] == "completed"
    assert pipeline.store.get_trace(second["turn_id"])["Steward"]["state"] == "completed"


def test_steward_data_and_failure_traces_are_redacted(pipeline):
    class RedactingFake(FakeLLMClient):
        def sanitize_text(self, text):
            return text.replace("synthetic-secret", "[REDACTED]")
    secret_episode = {**episode(), "reaction": "synthetic-secret"}
    pipeline.client = RedactingFake(json_responses=[DIRECT, problems(EMPTY), {"episodes": [secret_episode], "updates": [
        {**update(), "proposed_known": "synthetic-secret", "rationale": "synthetic-secret"}]}], extract_responses=[extraction(EMPTY)])
    result = pipeline.run(pipeline.store.create_session('fixture')["id"], "synthetic-secret")
    pipeline.wait_for_steward()
    saved = [pipeline.store.get_trace(result["turn_id"]), pipeline.store.list_episodes('fixture'), pipeline.store.list_model_versions('fixture'), pipeline.store.list_steward_runs('fixture')]
    assert "synthetic-secret" not in json.dumps(saved)


def test_existing_episode_table_migration_preserves_records(tmp_path):
    import sqlite3
    from cbs.store import SCHEMA
    path = tmp_path / "legacy_episodes.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("INSERT INTO sessions VALUES ('old', 'fixture', 'old', '2026-10-01')")
        connection.execute("INSERT INTO turns VALUES (1, 'old', 'user', 'reply', 'completed', '{}', '2026-10-01')")
        connection.execute("INSERT INTO episodes VALUES (1, 'fixture', 'CBS10', 'situation', 'reaction', '[]', 1, '2026-10-01')")
    store = Store(path)
    assert Store(path).list_episodes('fixture')[0]["session_id"] == "old"
    assert store.list_episodes('fixture')[0]["mode"] == "hypothetical"
    assert store.list_episodes('fixture')[0]["reaction"] == "reaction"


def test_web_exposes_versions_rejections_episodes_and_current_model(pipeline):
    from fastapi.testclient import TestClient
    from cbs.web import create_app
    apply(pipeline.store, pipeline.character, [episode()], [update(), update("well_supported")])
    with TestClient(create_app(pipeline)) as client:
        assert client.get("/api/model/versions").json()[0]["diff"]["changes"][0]["target"] == "CBS10"
        changes = client.get("/api/model/changes").json()
        assert changes["changes"][0]["after"]["status"] == "tentative"
        assert changes["steward_runs"][0]["application"]["rejected"]
        assert client.get("/api/episodes").json()[0]["mode"] == "enacted"
        fixture = client.get("/api/character").json()
        assert fixture["initial_status"]["CBS10"] == "open"
        assert fixture["episode_counts"]["CBS10"]["enacted"] == 1


def test_reinforcement_records_episode_without_growing_learned(store, character):
    result, _, _ = apply(store, character, [episode(domain="CBS11")], [update("supported", target="CBS11")])
    assert result["episodes"] and not result["applied"]
    assert "reinforcement" in result["rejected"][0]["reason"]
    domain = next(d for d in store.current_model(character).domains if d.id == "CBS11")
    assert not getattr(domain, "learned", None)
