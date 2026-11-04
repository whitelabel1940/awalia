import pytest
from pydantic import ValidationError

from cbs.llm import LLMError
from cbs.pipeline import PreJudge, assemble_packet


def test_always_full_summary_signatures_expression(character):
    packet = assemble_packet(character, PreJudge())
    assert len(packet["signatures"]) == 8
    assert packet["summary"] == character.model.summary
    assert packet["expression"]["voice"]["first_person"] == "私"
    assert packet["expression"]["voice"]["register"] == "簡潔なですます調"
    assert packet["touched_domains"] == []
    assert packet["life_history"] == []


def test_open_domain_guidance_and_judge_selection(character):
    judge = PreJudge.model_validate({"domains_touched": [{"id": "CBS09", "strength": "direct"}], "signatures_relevant": ["sig_sorting"], "emotional_weight": "high"})
    packet = assemble_packet(character, judge)
    domain = packet["touched_domains"][0]
    assert domain["status"] == "open"
    assert "昔からそうだったと語らない" in domain["guidance"]
    assert [s["id"] for s in domain["judge_relevant_signatures"]] == ["sig_sorting"]
    assert len(packet["signatures"]) == 8
    assert packet["length_hint"] == character.expression.voice.length["expand_when"]
    assert character.model.domains[8].status == "open"


def test_soft_prior_only_passed_for_touched_open_domain_when_present(character):
    domain = next(d for d in character.model.domains if d.id == 'CBS09')
    domain.soft_prior = '慎重に状況を確かめそう。'
    domain.soft_prior_evidence = ['author:Q2']
    judge = PreJudge(domains_touched=[{'id': 'CBS09', 'strength': 'direct'}, {'id': 'CBS23', 'strength': 'direct'}])
    packet = assemble_packet(character, judge)
    touched = {d['id']: d for d in packet['touched_domains']}
    assert touched['CBS09']['soft_prior'] == domain.soft_prior
    assert touched['CBS09']['soft_prior_evidence'] == ['author:Q2']
    assert 'soft_prior' not in touched['CBS23']
    assert not assemble_packet(character, PreJudge())['touched_domains']
    domain.status = 'tentative'
    assert 'soft_prior' not in assemble_packet(character, judge)['touched_domains'][0]


def test_unknown_past_selects_only_structural_context(character):
    judge = PreJudge.model_validate({"past_reference": {"asks_about_past": True, "targets": [{"life_ref": "transition_archive", "aspect": "原因", "known": False, "major": True}]}})
    packet = assemble_packet(character, judge)
    assert {e["id"] for e in packet["life_history"]} == {"transition_archive", "phase_apprentice", "phase_archive"}
    assert "確定させず" in packet["past_guidance"]
    assert packet["past_reference"]["targets"][0]["major"]
    assert packet["episodes"] == packet["canon_facts"] == packet["unfinished_ledger"] == []


@pytest.mark.parametrize("judgment", [
    {"domains_touched": [{"id": "missing", "strength": "direct"}]},
    {"signatures_relevant": ["missing"]},
    {"past_reference": {"asks_about_past": True, "targets": [{"life_ref": "missing", "aspect": "x", "known": False, "major": True}]}},
])
def test_unknown_judge_ids_are_dropped(character, judgment):
    packet = assemble_packet(character, PreJudge.model_validate(judgment))
    assert packet["judge_warnings"] == ["unknown id dropped: missing"]
    assert packet["touched_domains"] == [] and packet["past_reference"]["targets"] == []


def test_schema_enums_and_unclear():
    judge = PreJudge.model_validate({"memory_use": [{"ref": "episode:12", "relevant_now": "unclear", "counterevidence": "no", "continuation": "yes"}]})
    assert judge.memory_use[0].relevant_now == "unclear"
    with pytest.raises(ValidationError):
        PreJudge.model_validate({"emotional_weight": "very high"})
    with pytest.raises(ValidationError):
        PreJudge.model_validate({"made_up_field": 1})
    with pytest.raises(ValidationError):
        PreJudge.model_validate({"ledger_signals": []})


def test_memory_selection_counterevidence_and_all_active_plans(character):
    memories = [{"ref": "canon:1", "content": "口癖A", "life_ref": "rel_binder"},
                {"ref": "canon:2", "content": "食べ物B"}, {"ref": "canon:3", "content": "食べ物C"},
                {"ref": "plan:1", "state": "agreed", "scheduled_date": "2026-10-17"}]
    judge = PreJudge.model_validate({"memory_use": [
        {"ref": "canon:1", "relevant_now": "yes", "counterevidence": "no", "continuation": "no"},
        {"ref": "canon:2", "relevant_now": "no", "counterevidence": "yes", "continuation": "yes"},
        {"ref": "canon:3", "relevant_now": "unclear", "counterevidence": "unclear", "continuation": "no"},
        {"ref": "plan:1", "relevant_now": "yes", "counterevidence": "yes", "continuation": "no"},
        {"ref": "canon:999", "relevant_now": "yes", "counterevidence": "yes", "continuation": "no"}]})
    packet = assemble_packet(character, judge, candidate_memories=memories, active_plans=[memories[-1]],
                             current_datetime={"iso": "2026-10-11T12:00:00+09:00", "weekday": "日"},
                             previous_session_datetime="2026-10-10T12:00:00+09:00")
    assert packet["canon_facts"] == [memories[0]]
    assert packet["counterevidence_memories"][0] == memories[1]
    assert packet["counterevidence_memories"][1]["scheduled_date"] == "2026-10-17"
    assert packet["counterevidence_memories"][1]["scheduled_time"]["absolute"].startswith("2026/10/17")
    assert packet["unfinished_ledger"][0]["state"] == "agreed"
    assert packet["unfinished_ledger"][0]["scheduled_time"]["relative"] == "6日後（10/17）"
    assert packet["elapsed_since_last_session_seconds"] == 86400
    assert packet["judge_warnings"] == ["unknown id dropped: canon:999"]
