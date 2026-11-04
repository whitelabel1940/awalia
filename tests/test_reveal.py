from cbs.config import ROOT
from response_helpers import problems, extraction
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from cbs.llm import FakeLLMClient, LLMError
from cbs.pipeline import PreJudge, assemble_packet
from cbs.reveal import allowed_level, reveal_packet, secret_states
from cbs.store import Store
from cbs.web import create_app

TRUTH = {"L1": "気持ちだけの手触り", "L2": "隠れた輪郭-222", "L3": "隠れた全経緯-333"}
REQUEST = {"reveal_request": {"secret_id": "secret_lottery", "manner": "sincere"}}
EMPTY = {"claims": [], "ledger_updates": [], "new_canon": [], "disclosures": []}


def disclosure(level=1, secret_id="secret_lottery"):
    return {**EMPTY, "disclosures": [{"secret_id": secret_id, "level": level}]}


def saved(store, status="hidden"):
    store.save_secret("fixture", "secret_lottery", TRUTH, status=status)
    return store.list_secrets('fixture')[0]


@pytest.mark.parametrize("metrics,manner,expected", [
    ((0, 0, 0), None, 0), ((0, 0, 0), "casual", 0), ((0, 0, 0), "pushy", 0),
    ((0, 0, 0), "sincere", 1), ((2, 2, 2), "sincere", 1), ((2, 3, 1), "sincere", 1),
    ((1, 3, 2), "sincere", 1), ((2, 3, 2), None, 2), ((5, 5, 2), None, 2), ((5, 5, 3), None, 3)])
def test_gates(character, store, metrics, manner, expected):
    counts = dict(zip(("days", "deep_moments", "user_disclosures"), metrics))
    assert allowed_level(character.reveal, counts, manner=manner, secret=saved(store)) == expected


def test_metrics_completed_only_character_isolated_and_old_traces(store):
    a, b = store.create_session('fixture'), store.create_session('fixture')
    other = store.create_session("other")
    deep = {"Packet": {"input": {"emotional_weight": "high", "user_disclosure": "yes"}}}
    store.add_turn(a["id"], "u", "a", deep)
    store.add_turn(a["id"], "u", "a", deep)
    store.add_turn(b["id"], "u", None, deep, status="failed")
    store.add_turn(other["id"], "u", "a", deep)
    assert store.relationship_metrics('fixture') == {"sessions": 1, "days": 1, "deep_moments": 2, "user_disclosures": 2}
    store.add_turn(b["id"], "u", "a", {})
    assert store.relationship_metrics('fixture') == {"sessions": 2, "days": 1, "deep_moments": 2, "user_disclosures": 2}


@pytest.mark.parametrize("level", [0, 1, 2, 3])
def test_packet_strictly_bounds_truth(character, store, level):
    saved(store)
    judge = PreJudge.model_validate(REQUEST)
    states = secret_states(character, store, spoilers=True)
    states["secrets"][0]["allowed_level"] = level
    packet = assemble_packet(character, judge, reveal_states=states)
    assert packet["secrets"][0]["truth"] == {f"L{i}": TRUTH[f"L{i}"] for i in range(1, level + 1)}
    for i in range(level + 1, 4):
        assert TRUTH[f"L{i}"] not in json.dumps(packet, ensure_ascii=False)


def test_generation_once_across_sessions_and_reopen(pipeline):
    pipeline.client = FakeLLMClient(text_responses=["最初の話", "同じ話"],
                                   json_responses=[REQUEST, TRUTH, problems(disclosure()), REQUEST, problems(disclosure())], extract_responses=[extraction(disclosure()), extraction(disclosure())])
    first = pipeline.run(pipeline.store.create_session('fixture')["id"], "理由を聞いてもいい？")
    pipeline.wait_for_steward()
    pipeline.store = Store(pipeline.store.path)
    second = pipeline.run(pipeline.store.create_session('fixture')["id"], "理由は？")
    pipeline.wait_for_steward()
    assert len([c for c in pipeline.client.calls if c.get("schema", {}).get("title") == "RevealTruth"]) == 1
    assert pipeline.store.list_secrets('fixture')[0]["generated_turn"] == first["turn_id"]
    assert "Reveal Generator" not in pipeline.store.get_trace(second["turn_id"])
    actor_calls = [c for c in pipeline.client.calls if "schema" not in c]
    assert all(TRUTH["L2"] not in c["system"] and TRUTH["L3"] not in c["system"] for c in actor_calls)
    context = json.loads(pipeline.client.calls[1]["messages"][0]["content"])
    assert context["author_sources"] == pipeline.character.sources
    assert context["secret"]["constraints"]
    assert pipeline.store.list_secrets('fixture')[0]["disclosed_level"] == 1
    assert len(pipeline.store.list_reveal_events('fixture')) == 2


def test_author_approval_stays_l0_until_approved(pipeline):
    pipeline.character.reveal.generation = "author_approval"
    pipeline.client = FakeLLMClient(text_responses=["骨格のみ", "手触りを話す"],
                                   json_responses=[REQUEST, TRUTH, problems(EMPTY), REQUEST, problems(disclosure())], extract_responses=[extraction(EMPTY), extraction(disclosure())])
    first = pipeline.run(pipeline.store.create_session('fixture')["id"], "理由は？")
    pipeline.wait_for_steward()
    packet = pipeline.store.get_trace(first["turn_id"])["Packet"]["output"]
    assert packet["secrets"][0]["allowed_level"] == 0 and packet["secrets"][0]["truth"] == {}
    assert pipeline.store.list_secrets('fixture')[0]["status"] == "pending"
    pipeline.store.approve_secret("fixture", "secret_lottery")
    pipeline.run(pipeline.store.create_session('fixture')["id"], "理由は？")
    pipeline.wait_for_steward()
    assert pipeline.store.list_secrets('fixture')[0]["status"] == "approved"
    assert pipeline.store.list_secrets('fixture')[0]["disclosed_level"] == 1


@pytest.mark.parametrize("error", ["invalid JSON", {"L1": "only one level"}])
def test_generation_failure_never_blocks_and_can_retry(pipeline, error):
    pipeline.client = FakeLLMClient(text_responses=["骨格のみ", "手触り"],
                                   json_responses=[REQUEST, error, problems(EMPTY), REQUEST, TRUTH, problems(disclosure())], extract_responses=[extraction(EMPTY), extraction(disclosure())])
    session = pipeline.store.create_session('fixture')
    first = pipeline.run(session["id"], "理由は？")
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(first["turn_id"])
    assert trace["Reveal Generator"]["error"] and trace["Packet"]["output"]["secrets"][0]["allowed_level"] == 0
    assert pipeline.store.list_secrets('fixture') == []
    pipeline.run(session["id"], "もう一度聞く")
    pipeline.wait_for_steward()
    assert len(pipeline.store.list_secrets('fixture')) == 1


def test_generated_truth_survives_actor_failure(pipeline):
    pipeline.client = FakeLLMClient(text_responses=[""], json_responses=[REQUEST, TRUTH])
    session = pipeline.store.create_session('fixture')
    with pytest.raises(LLMError):
        pipeline.run(session["id"], "理由は？")
        pipeline.wait_for_steward()
    assert pipeline.store.list_secrets('fixture')[0]["generated_turn"] == pipeline.store.history(session["id"])[0]["id"]
    pipeline.client = FakeLLMClient(json_responses=[REQUEST, problems(EMPTY)], extract_responses=[extraction(EMPTY)])
    pipeline.run(session["id"], "もう一度")
    pipeline.wait_for_steward()
    assert len([c for c in pipeline.client.calls if c.get("schema", {}).get("title") != "Expression"]) == 4


def test_post_check_overshoot_rewrites_and_only_final_disclosure_saved(pipeline):
    saved(pipeline.store)
    pipeline.client = FakeLLMClient(text_responses=["輪郭を言い過ぎた", "手触りだけ"],
                                   json_responses=[REQUEST, {"problems": [{"text": "輪郭", "verdict": "forbidden", "reason": "allowed L1 を超える L2"}]}, {"problems": []}], extract_responses=[extraction(disclosure(1))])
    result = pipeline.run(pipeline.store.create_session('fixture')["id"], "理由は？")
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(result["turn_id"])
    assert trace["Check"]["response"]["parsed"]["problems"][0]["verdict"] == "forbidden"
    assert result["assistant_text"] == "手触りだけ"
    assert [e["level"] for e in pipeline.store.list_reveal_events('fixture')] == [1]


def test_rewrite_feedback_cannot_leak_checker_truth(pipeline):
    saved(pipeline.store)
    bad = {**EMPTY, "claims": [{"text": TRUTH["L3"], "reason": TRUTH["L3"], "verdict": "contradicted"}]}
    pipeline.client = FakeLLMClient(text_responses=["不整合な気持ち", "整合する手触り"], json_responses=[REQUEST, problems(bad), problems(disclosure())], extract_responses=[extraction(disclosure())])
    pipeline.run(pipeline.store.create_session('fixture')["id"], "理由は？")
    pipeline.wait_for_steward()
    rewrite = [c for c in pipeline.client.calls if "schema" not in c][1]
    assert TRUTH["L3"] not in json.dumps(rewrite, ensure_ascii=False)


def test_remaining_overshoot_is_blocked_without_disclosure(pipeline):
    from cbs.llm import LLMError
    saved(pipeline.store)
    bad={'problems':[{'text':'overshoot','verdict':'forbidden','reason':'beyond L1'}]}
    pipeline.client=FakeLLMClient(text_responses=['overshoot']*4,json_responses=[REQUEST,bad,bad,bad,bad])
    sid=pipeline.store.create_session('fixture')['id']
    with pytest.raises(LLMError): pipeline.run(sid,'question')
    assert pipeline.store.history(sid)[0]['assistant_text'] is None
    assert pipeline.store.list_secrets('fixture')[0]['disclosed_level']==0
    assert not pipeline.store.list_reveal_events('fixture')

def test_disclosed_level_never_decreases_and_repeated_events_logged(pipeline):
    saved(pipeline.store)
    pipeline.character.reveal.gates["L3"] = pipeline.character.reveal.gates["L1"]
    pipeline.client = FakeLLMClient(text_responses=["全部", "手触り"],
                                   json_responses=[REQUEST, problems(disclosure(3)), REQUEST, problems(disclosure(1))], extract_responses=[extraction(disclosure(3)), extraction(disclosure(1))])
    pipeline.run(pipeline.store.create_session('fixture')["id"], "全部？")
    pipeline.wait_for_steward()
    pipeline.run(pipeline.store.create_session('fixture')["id"], "もう一度？")
    pipeline.wait_for_steward()
    assert pipeline.store.list_secrets('fixture')[0]["disclosed_level"] == 3
    assert [e["level"] for e in pipeline.store.list_reveal_events('fixture')] == [3, 1]
    pipeline.store.save_secret("fixture", "secret_lottery", {**TRUTH, "L3": "作者の修正"}, status="approved", replace=True, action="set")
    assert pipeline.store.list_secrets('fixture')[0]["disclosed_level"] == 3
    assert pipeline.store.list_secrets('fixture')[0]["history"][0]["truth"] == TRUTH


def test_deep_moment_selects_newly_allowed_and_disclosed_context(character, store):
    saved(store)
    character.reveal.gates["L2"] = character.reveal.gates["L1"]
    states = secret_states(character, store, spoilers=True)
    assert reveal_packet(character, PreJudge(), states) == []
    packet = reveal_packet(character, PreJudge(emotional_weight="high"), states)
    assert packet[0]["spontaneous"] and packet[0]["allowed_level"] == 2
    states["secrets"][0].update(disclosed_level=1, allowed_level=1)
    assert reveal_packet(character, PreJudge(), states)[0]["truth"] == {"L1": TRUTH["L1"]}


def test_other_secret_truths_passed_to_generator(pipeline):
    pipeline.store.save_secret("fixture", "secret_delivery", TRUTH)
    pipeline.client = FakeLLMClient(json_responses=[REQUEST, TRUTH, problems(EMPTY)], extract_responses=[extraction(EMPTY)])
    pipeline.run(pipeline.store.create_session('fixture')["id"], "理由は？")
    pipeline.wait_for_steward()
    context = json.loads(pipeline.client.calls[1]["messages"][0]["content"])
    assert context["other_secrets"] == [{"secret_id": "secret_delivery", "truth": TRUTH}]


def test_unknown_reveal_id_dropped(pipeline):
    pipeline.client = FakeLLMClient(json_responses=[{"reveal_request": {"secret_id": "missing", "manner": "sincere"}}, problems(EMPTY)], extract_responses=[extraction(EMPTY)])
    result = pipeline.run(pipeline.store.create_session('fixture')["id"], "何？")
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(result["turn_id"])
    assert trace["Pre-Judge"]["warnings"] and trace["Packet"]["input"]["reveal_request"] is None


def test_reveal_truth_and_history_key_redaction(pipeline):
    class RedactingFake(FakeLLMClient):
        def sanitize_text(self, text):
            return text.replace("synthetic-secret", "[REDACTED]")
    truth = {**TRUTH, "L3": "synthetic-secret"}
    pipeline.client = RedactingFake(json_responses=[REQUEST, truth, problems(EMPTY)], extract_responses=[extraction(EMPTY)])
    result = pipeline.run(pipeline.store.create_session('fixture')["id"], "理由は？")
    pipeline.wait_for_steward()
    assert "synthetic-secret" not in json.dumps(pipeline.store.list_secrets('fixture'))
    assert "synthetic-secret" not in json.dumps(pipeline.store.get_trace(result["turn_id"]))


def test_web_secrets_and_generation_trace_hidden_by_default(pipeline):
    pipeline.client = FakeLLMClient(json_responses=[REQUEST, TRUTH, problems(EMPTY)], extract_responses=[extraction(EMPTY)])
    result = pipeline.run(pipeline.store.create_session('fixture')["id"], "理由は？")
    pipeline.wait_for_steward()
    with TestClient(create_app(pipeline)) as client:
        state = client.get("/api/secrets").json()
        assert state["secrets"][0]["status"] == "hidden" and state["metrics"]["sessions"] == 1
        assert "truth" not in state["secrets"][0]
        assert client.get("/api/secrets?spoilers=true").json()["secrets"][0]["truth"] == TRUTH
        path = f"/api/turns/{result['turn_id']}/trace"
        assert TRUTH["L3"] not in client.get(path).text
        assert TRUTH["L1"] not in client.get(path).text
        assert TRUTH["L3"] in client.get(path + "?spoilers=true").text


def test_reveal_cli_set_show_list_approve_history(store, monkeypatch, tmp_path, capsys):
    from cbs.reveal_admin import main
    import yaml
    path = tmp_path / "truth.yaml"
    path.write_text(yaml.safe_dump(TRUTH, allow_unicode=True), encoding="utf-8")
    for args in (["set", "secret_lottery", str(path)], ["list"], ["show", "secret_lottery"], ["set", "secret_lottery", str(path)]):
        monkeypatch.setattr("sys.argv", ["reveal_admin", *args, "--db", str(store.path)])
        main()
        output = json.loads(capsys.readouterr().out)
        assert ("truth" in output["secrets"][0]) == (args[0] == "show")
    assert store.list_secrets('fixture')[0]["history"][0]["truth"] == TRUTH
    store.save_secret("fixture", "secret_lottery", TRUTH, status="pending", replace=True)
    monkeypatch.setattr("sys.argv", ["reveal_admin", "approve", "secret_lottery", "--db", str(store.path)])
    main()
    assert store.list_secrets('fixture')[0]["status"] == "approved"


def test_existing_database_reveal_migration(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE secrets (character_id TEXT, secret_id TEXT, status TEXT, truth_json TEXT, provenance TEXT, created_at TEXT, updated_at TEXT, PRIMARY KEY(character_id, secret_id))")
    connection.commit()
    connection.close()
    store = Store(path)
    saved(store)
    assert Store(path).list_secrets('fixture')[0]["disclosed_level"] == 0


def test_cli_regenerate_replaces_with_history_and_generation_failure_preserves(store, character, monkeypatch, capsys):
    from cbs import reveal_admin
    saved(store)
    client = FakeLLMClient(json_responses=[{**TRUTH, "L3": "新しい全経緯"}, "invalid JSON"])
    monkeypatch.setattr(reveal_admin, "create_client", lambda config: client)
    monkeypatch.setattr("sys.argv", ["reveal_admin", "regenerate", "secret_lottery", "--db", str(store.path)])
    reveal_admin.main()
    assert store.list_secrets('fixture')[0]["truth"]["L3"] == "新しい全経緯"
    assert store.list_secrets('fixture')[0]["history"][0]["action"] == "regenerate"
    before = store.list_secrets('fixture')
    with pytest.raises(SystemExit):
        reveal_admin.main()
    assert store.list_secrets('fixture') == before


def test_probe_reveal_summary_and_final_truth(tmp_path, monkeypatch):
    from cbs import probe
    suite = tmp_path / "suite.yaml"
    suite.write_text('probes:\n  - id: R1\n    category: 過去\n    expect: 一貫\n    sessions:\n      - ["理由は？"]\n', encoding="utf-8")
    client = FakeLLMClient(json_responses=[REQUEST, TRUTH, problems(disclosure())], extract_responses=[extraction(disclosure())])
    monkeypatch.setattr(probe, "create_client", lambda config: client)
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    text = probe.run_suite(suite, "test", character_id="fixture", characters_root=ROOT / "tests/fixtures/characters").read_text(encoding="utf-8")
    assert "secret_lottery 生成 / allowed L1 / 開示 L1" in text
    assert "days=1 / deep_moments=0 / user_disclosures=0" in text
    assert TRUTH["L3"] in text


@pytest.fixture(autouse=True)
def cli_character(monkeypatch, character, isolated_settings):
    from cbs import reveal_admin
    from cbs.config import load_config
    config = load_config()
    config.character.id = character.model.character_id
    monkeypatch.setattr(reveal_admin, 'load_config', lambda: config)
    monkeypatch.setattr(reveal_admin, 'load_character', lambda cid: character if cid == character.model.character_id else None)
