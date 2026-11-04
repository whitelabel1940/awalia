from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError
import yaml

from cbs import clock
from cbs.bootstrap import (Bootstrapper, coverage_map, decode_answers, domain_catalog,
                           adaptive_parents, expected_status, gap_questions, load_questionnaire, validate_package)
from cbs.bootstrap_schema import AdaptiveOutput, BootstrapInput, CharacterPackage, ReaderOutput
from cbs.character import load_character
from cbs.config import ROOT, load_config
from cbs.interests import timed_interest
from cbs.llm import FakeLLMClient
from cbs.store import Store


def minimal_input():
    return BootstrapInput(id='test_character', basics={'name': 'Test'},
                          now={'likes': [{'topic': 'Tea', 'elaborate': 'keep'}]})


def package_data(input_data=None):
    input_data = input_data or minimal_input()
    return {
        'model': {'version': 1, 'character_id': input_data.id, 'display_name': input_data.basics.name,
                  'summary': ['Test character'], 'signatures': [],
                  'domains': [{**d, 'status': 'open', 'known': None, 'evidence': [], 'related_signatures': []} for d in domain_catalog()],
                  'life_history': {'phases': [], 'relationships': [], 'transitions': [], 'events': [], 'trajectories': [],
                                   'past_detail_policy': {'minor_details': 'Allowed', 'major_unknowns': 'Reveal'}}, 'tensions': []},
        'expression': {'voice': {'first_person': 'I', 'second_person': {'default': 'You', 'when_needed': 'You'},
                                'register': 'Casual', 'length': {'default': 'Short', 'expand_when': 'Emotional'},
                                'allow_disagreement_and_negative_emotion': True},
                       'demeanor': [], 'preferences': ['Tea'], 'relationship_context': {'stance': 'Friend', 'not_fixed': []},
                       'not_adopted_as_goal': [], 'existence': {'form': 'Human', 'daily_life': 'Daily life', 'open': ['Occupation']},
                       'items': [], 'elaborate_default': 'may_elaborate', 'current_life': []},
        'world': {'era': '現代', 'region': '日本', 'reality': '現実どおり', 'being': '人間', 'contact': 'A',
                  'contact_note': 'Remote', 'notes': {'naming': 'Japan', 'daily_life': 'Modern', 'money': 'Yen', 'calendar': 'Modern'}},
        'reveal': {'secrets': []},
        'interests': [{'id': 'interest_tea', 'topic': 'Tea', 'kind': 'like', 'detail': 'Tea', 'elaborate': 'keep',
                       'origin': 'author', 'status': 'established', 'evidence': ['now:like:0']}],
        'seed_memories': [{'id': f'seed_{i}', 'kind': 'daily_life', 'content': f'Daily moment {i}',
                           'event_date': (clock.now().date() - timedelta(days=i + 1)).isoformat(), 'life_ref': None} for i in range(8)]}


def bootstrap(tmp_path, client, store=None):
    return Bootstrapper(load_config(), client, characters_root=tmp_path / 'characters', store=store,
                        trace_root=tmp_path / 'traces')


def test_both_fixtures_and_life_question_copy():
    for path in (ROOT / 'bootstrap' / 'fixtures').glob('*.json'):
        data = BootstrapInput.model_validate_json(path.read_text(encoding='utf-8'))
        assert decode_answers(data, load_questionnaire())
    questions = load_questionnaire()['sections'][-1]['questions']
    assert len(questions) == 7
    assert questions[3]['choices']['G'] == '失った人'
    assert all(q['default'] == 'leave_open' for q in questions)


@pytest.mark.parametrize('update', [
    {'id': '../fixture'}, {'answers': {'R1': ['Z']}}, {'answers': {'R1': ['A', 'B']}},
    {'answers': {'R8': ['A', 'A']}}, {'answers': {'R8': ['A', 'leave_open']}},
    {'answers': {'oops': ['A']}}, {'world': {'contact': 'D'}}, {'world': {'reality': 'unknown'}},
    {'life': {'Q4': {'answer': ['B'], 'relationship_grouping': 'different'}}},
    {'now': {'life': {'L1now': 'Z'}}}, {'now': {'likes': [{'topic': 'Tea', 'elaborate': 'invalid'}]}}
])
def test_input_rejects_bad_choices_and_paths(update):
    with pytest.raises(ValidationError):
        BootstrapInput.model_validate({'id': 'test_character', 'basics': {'name': 'Test'}, **update})


def test_coverage_and_confirmation_questions():
    data = BootstrapInput(id='test_character', basics={'name': 'Test'}, text='Kind. Direct disagreement.', answers={'R3': ['leave_open']})
    reader = ReaderOutput.model_validate({'items': [], 'mentions': [
        {'domain': 'R01', 'kind': 'trait_only', 'quotes': ['Kind.']},
        {'domain': 'R05', 'kind': 'text', 'quotes': ['Direct disagreement.']}], 'conflicts': []})
    coverage = coverage_map(data, reader)
    assert coverage['R01'] == 'trait_only'
    assert coverage['R05'] == 'text'
    assert coverage['R03'] == 'answered'
    assert coverage['CBS09'] == 'open'
    gaps = {q['id']: q for q in gap_questions(data, reader)}
    assert 'Kind.' in gaps['R1']['scene']
    assert '確認' not in gaps['R2']['scene']
    assert 'R3' not in gaps and 'R5' not in gaps


def test_text_covered_life_not_asked():
    data = minimal_input()
    reader = ReaderOutput(items=[{'id': 'text:1', 'category': 'now', 'quote': 'alone', 'content': 'alone', 'covers': ['now:L2now']}])
    assert 'L2now' not in {q['id'] for q in gap_questions(data, reader)}


@pytest.mark.parametrize(('contact', 'answer', 'asked'), [('B2', 'A', True), ('B2', 'E', False), ('A', 'E', True), ('A', 'A', False)])
def test_living_question_follows_contact(tmp_path, contact, answer, asked):
    data = minimal_input()
    data = data.model_copy(update={'world': data.world.model_copy(update={'contact': contact}),
                                   'now': data.now.model_copy(update={'life': {'L2now': [answer]}})})
    gaps = {q['id']: q for q in gap_questions(data, ReaderOutput())}
    assert ('L2now' in gaps) == asked
    if not asked:
        return
    with pytest.raises(ValueError, match='接点'):
        bootstrap(tmp_path, FakeLLMClient()).create(data)


def test_status_counts_questions_not_multiple_choices():
    valid = {'author:Q12': {'open': False}, 'author:A4': {'open': False}, 'author:R1': {'open': True}}
    assert expected_status([], valid) == 'open'
    assert expected_status(['author:Q12', 'author:Q12'], valid) == 'supported'
    assert expected_status(['author:Q12', 'author:A4'], valid) == 'well_supported'
    assert expected_status(['author:R1'], valid) == 'open'


def test_package_validation_status_dates_refs_unknowns_and_keep():
    data = minimal_input().model_copy(update={'answers': {'R1': ['A']}})
    package = package_data(data)
    domain = next(d for d in package['model']['domains'] if d['id'] == 'R01')
    domain.update(status='well_supported', known='Listen', evidence=['author:R1'])
    package['seed_memories'][0]['event_date'] = clock.now().date().isoformat()
    package['seed_memories'][1]['id'] = package['seed_memories'][0]['id']
    package['interests'][0]['detail'] = 'Specific tea origin'
    package['model']['life_history']['events'] = [{'id': 'event_1', 'kind': 'change', 'known': 'Unspecified change',
        'evidence': ['missing:Q1'], 'open': ['cause'], 'major_unknowns': ['cause']}]
    issues = validate_package(CharacterPackage.model_validate(package), data, ReaderOutput(), load_questionnaire(), clock.now().date())
    assert any('status must be supported' in p for p in issues)
    assert any('seed date' in p for p in issues)
    assert any('unique' in p for p in issues)
    assert any('Invalid evidence' in p for p in issues)
    assert any('major_unknowns' in p for p in issues)
    assert any('literal keep' in p for p in issues)


def test_builder_one_fix_and_runtime_registration(tmp_path):
    data = minimal_input()
    good = package_data()
    bad = deepcopy(good)
    bad['seed_memories'][0]['event_date'] = clock.now().date().isoformat()
    client = FakeLLMClient(json_responses=[bad, {'problems': ['seed includes shared past']}, good,
                                             {'introduction': 'A test character', 'open_now': ['Occupation']}])
    runner = bootstrap(tmp_path, client)
    path = runner.create(data)
    assert len(client.calls) == 4
    assert 'seed includes shared past' in client.calls[2]['messages'][0]['content']
    assert len(list((path / 'source').iterdir())) == 3
    character = load_character(data.id, root=path.parent)
    assert character.expression.elaborate_default == 'may_elaborate'
    assert character.interests[0]['elaborate'] == 'keep'
    assert 'Occupation' in (path / 'summary.md').read_text(encoding='utf-8')
    store = Store(tmp_path / 'runtime.sqlite3')
    for _ in range(2):
        store.initialize_seed_memories(character)
        store.initialize_interests(character)
    assert len(store.list_canon(data.id)) == 8
    assert len(store.list_interests(data.id)) == 1
    item = timed_interest(store.list_interests(data.id)[0])
    assert item['elaborate'] == 'keep' and item['origin'] == 'author'
    assert all(f['source_turn'] is None and f['event_date'] for f in store.list_canon(data.id))
    assert not store.list_episodes(data.id)


def test_no_third_builder_and_no_partial_package(tmp_path):
    bad = package_data()
    bad['seed_memories'][0]['event_date'] = clock.now().date().isoformat()
    client = FakeLLMClient(json_responses=[bad, {'problems': []}, bad])
    runner = bootstrap(tmp_path, client)
    with pytest.raises(ValueError, match='after one fix'):
        runner.create(minimal_input())
    assert len(client.calls) == 3
    assert not (runner.characters_root / 'test_character').exists()
    assert len(list(runner.trace_root.glob('*.json'))) == 1


def test_schema_failure_uses_fix_and_still_gets_one_judge(tmp_path):
    bad = package_data()
    bad['model']['version'] = 2
    client = FakeLLMClient(json_responses=[bad, package_data(), {'problems': []},
                                         {'introduction': 'Test', 'open_now': []}])
    bootstrap(tmp_path, client).create(minimal_input())
    assert len(client.calls) == 4
    assert 'model.version' in client.calls[1]['messages'][0]['content']
    assert 'BootstrapJudgment' in json.dumps(client.calls[2]['schema'])


def test_provider_schema_has_required_properties_and_singleton_enums():
    schema = CharacterPackage.model_json_schema()
    assert schema['$defs']['SeedModel']['properties']['version']['enum'] == [1]
    assert 'const' not in schema['$defs']['SeedModel']['properties']['version']
    for definition in schema['$defs'].values():
        if definition.get('type') == 'object':
            assert set(definition['required']) == set(definition['properties'])
            assert definition['additionalProperties'] is False


def test_unknown_answer_is_open_and_explicit_world_rule_is_checked():
    data = minimal_input().model_copy(update={'answers': {'R1': ['unknown']}})
    package = CharacterPackage.model_validate(package_data(data))
    assert not validate_package(package, data, ReaderOutput(), load_questionnaire(), clock.now().date())
    package.world.contact = 'B2'
    package.reveal.gates.L2.days = 30
    errors = validate_package(package, data, ReaderOutput(), load_questionnaire(), clock.now().date())
    assert any('world.contact' in e for e in errors)
    assert any('reveal gates' in e for e in errors)


def test_no_overwrite_and_force_archives_sources(tmp_path):
    data = minimal_input()
    client = FakeLLMClient(json_responses=[package_data(), {'problems': []}, {'introduction': 'Test', 'open_now': []}])
    runner = bootstrap(tmp_path, client)
    path = runner.create(data, raw_input='{"raw": "preserved"}')
    with pytest.raises(FileExistsError):
        runner.create(data)
    assert len(client.calls) == 3
    client.json_responses = [package_data(), {'problems': []}, {'introduction': 'New', 'open_now': []}]
    runner.create(data, force=True)
    archive = next((path / '_history').iterdir())
    assert (archive / 'source' / 'input.json').read_text(encoding='utf-8') == '{"raw": "preserved"}'
    assert (path / 'summary.md').is_file()


def test_conflicts_require_input_revision(tmp_path):
    data = BootstrapInput(id='test_character', basics={'name': 'Test'}, text='contradictory', answers={'R1':['A']})
    client = FakeLLMClient(json_responses=[{'items': [], 'mentions': [], 'conflicts': [{'topic':'距離感', 'quote':'contradictory', 'question_ref':'author:R1', 'answer_keys':['A']}]}])
    runner = bootstrap(tmp_path, client)
    with pytest.raises(ValueError, match='確認'):
        runner.create(data)
    assert len(client.calls) == 2


def test_augment_preserves_original_sources_answers_and_versions(tmp_path):
    root = tmp_path / 'characters'
    shutil.copytree(ROOT / 'tests' / 'fixtures' / 'characters' / 'fixture', root / 'fixture')
    original_bytes = {p.relative_to(root): p.read_bytes() for p in (root / 'fixture').rglob('*') if p.is_file()}
    store = Store(tmp_path / 'runtime.sqlite3')
    with store.connect() as db:
        db.execute("INSERT INTO model_versions (character_id, base_version, diff_json, rationale, episode_refs_json, created_at) VALUES ('fixture', 1, ?, 'prior experience', '[]', ?)",
                   (json.dumps({'changes': [{'collection': 'domains', 'target': 'R01', 'after': {'learned': [{'text': 'prior experience'}]}}]}), clock.iso()))
    prior_versions = store.list_model_versions('fixture')
    definition = next(d.model_dump() for d in load_character('fixture', root=root).model.domains if d.id == 'R01')
    definition.update(description='Support scene', status='supported', known='Listen', evidence=['author:R1'])
    client = FakeLLMClient(json_responses=[{'domains': [definition]}, {'problems': []}])
    runner = Bootstrapper(load_config(), client, characters_root=root, store=store, trace_root=tmp_path / 'traces')
    runner.augment('fixture', {'R1': ['A']})
    assert store.list_model_versions('fixture')[:1] == prior_versions
    character = load_character('fixture', root=root)
    current = store.current_model(character)
    domain = next(d for d in current.domains if d.id == 'R01')
    assert domain.status == 'supported' and domain.learned == [{'text': 'prior experience'}]
    assert next(d for d in current.domains if d.id == 'CBS21') == next(d for d in character.model.domains if d.id == 'CBS21')
    assert all((root / path).read_bytes() == content for path, content in original_bytes.items())
    assert 'author:R1' in store.bootstrap_answers('fixture')
    assert 'augment_1.md' in store.author_sources(character)
    assert runner.augment('fixture', {'R1': ['A']})['version_id'] is None
    with pytest.raises(ValueError, match='overwrite'):
        runner.augment('fixture', {'R1': ['B']})


def test_character_selection_and_session_isolation(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from cbs.pipeline import TurnPipeline
    from cbs.web import create_app
    runner = bootstrap(tmp_path, FakeLLMClient())
    data = minimal_input()
    from cbs.bootstrap_schema import BootstrapSummary
    runner.write(CharacterPackage.model_validate(package_data()), data,
                 BootstrapSummary(introduction='Test', open_now=[]), clock.now().date())
    config = load_config()
    config.character.id = data.id
    store = Store(tmp_path / 'runtime.sqlite3')
    store.create_session('fixture')
    mine = store.create_session(data.id)
    pipeline = TurnPipeline(config, load_character(data.id, root=runner.characters_root), store, FakeLLMClient())
    pipeline.wait_for_steward()
    with TestClient(create_app(pipeline)) as client:
        assert client.get('/api/character').json()['model']['character_id'] == data.id
        assert [s['id'] for s in client.get('/api/sessions').json()] == [mine['id']]
        assert client.get('/').status_code == 200


def test_relationship_ids_are_internal_markers():
    from cbs.pipeline import INTERNAL_MARKERS
    assert INTERNAL_MARKERS.search('R01 supported')
    assert not INTERNAL_MARKERS.search('普通の会話です')


def answered_input():
    return minimal_input().model_copy(update={'answers': {'R1': ['A']}})


def answered_package(data):
    package = package_data(data)
    domain = next(d for d in package['model']['domains'] if d['id'] == 'R01')
    domain.update(status='supported', known='Listen', evidence=['author:R1'])
    return package


def adaptive_question(qid='AQ1', domain='R01', parent=None):
    return {'id': qid, 'domain': domain, 'purpose': '慰めと助言を切り替える条件',
            'parent_question': parent or (domain if domain.startswith('life:') else 'R1'),
            'added_condition': '相手が落ち着いて具体的な助言を求める',
            'scene': '相手が落ち着いてから、明日どうするかを尋ねてきた。',
            'choices': {'A': '一緒に考える', 'B': '少し待つ', 'C': '理由を聞く', 'D': 'そばにいる', 'E': '希望を確かめる'},
            'multi': False}


@pytest.mark.parametrize('questions', [
    [adaptive_question(f'AQ{i}') for i in range(1, 6)],
    [adaptive_question(), adaptive_question()], [adaptive_question(domain='R99')],
    [adaptive_question(domain='CBS25')], [adaptive_question(domain='life:Q8')],
    [adaptive_question(qid='R1')], [{**adaptive_question(), 'choices': {'A': 'one'}}],
])
def test_adaptive_rejects_count_refs_ids_and_choices(questions):
    with pytest.raises(ValidationError):
        AdaptiveOutput(questions=questions)
    with pytest.raises(ValidationError):
        BootstrapInput(id='test_character', basics={'name': 'Test'}, adaptive_questions=questions)


def test_adaptive_generation_context_and_four_question_limit(tmp_path):
    data = BootstrapInput(id='test_character', basics={'name': 'Test'}, text='優しい',
                          answers={'R1': ['A'], 'Q3': ['A'], 'R8': {'answer': ['A', 'E'], 'text': '相手次第'}},
                          life={'Q7': ['B', 'E'], 'Q3': ['B']})
    reader = {'items': [], 'mentions': [{'domain': 'R01', 'kind': 'trait_only', 'quotes': ['優しい']}], 'conflicts': []}
    questions = [adaptive_question(f'AQ{i}', domain) for i, domain in enumerate(['CBS01', 'R01', 'life:Q7', 'life:Q3'], 1)]
    questions[0]['parent_question'] = 'Q3'
    client = FakeLLMClient(json_responses=[reader, {'questions': questions}, {'problems': []}])
    result = bootstrap(tmp_path, client).adaptive(data)
    assert result['questions'] == questions
    context = json.loads(client.calls[2]['messages'][0]['content'])
    assert context['decoded_answers']['author:R8']['text'] == '相手次第'
    assert context['decoded_answers']['life:Q7']['answer'] == ['B', 'E']
    assert context['reader']['mentions'][0]['kind'] == 'trait_only'
    assert client.calls[2]['model'] == load_config().models.steward
    assert len(context['allowed_ids']) == 4


def test_adaptive_rejects_reused_ids_and_unknown_answers(tmp_path):
    data = answered_input().model_dump(mode='json', by_alias=True)
    data.update(adaptive_questions=[adaptive_question()], adaptive_answers={'AQ2': ['A']})
    with pytest.raises(ValidationError, match='Unknown adaptive question'):
        BootstrapInput.model_validate(data)
    data['adaptive_answers'] = {'AQ1': ['A', 'B']}
    with pytest.raises(ValidationError, match='number of choices'):
        BootstrapInput.model_validate(data)
    data['adaptive_answers'] = {'AQ1': ['A']}
    client = FakeLLMClient(json_responses=[{'questions': [adaptive_question()]}])
    with pytest.raises(ValueError, match='unused IDs'):
        bootstrap(tmp_path, client).adaptive(BootstrapInput.model_validate(data))


@pytest.mark.parametrize('answers', [{'AQ1': {'answer': ['B'], 'text': '落ち着いたら'}}, {}, {'AQ1': ['unknown']}])
def test_adaptive_answers_builder_evidence_and_skip_creation(tmp_path, answers):
    data = answered_input().model_dump(mode='json', by_alias=True)
    data.update(adaptive_questions=[adaptive_question()], adaptive_answers=answers)
    data = BootstrapInput.model_validate(data)
    package = answered_package(data)
    answered = bool(answers) and answers['AQ1'] != ['unknown']
    if answered:
        domain = next(d for d in package['model']['domains'] if d['id'] == 'R01')
        domain.update(status='well_supported', known='落ち着いたら待つ', evidence=['author:R1', 'author:AQ1'])
    client = FakeLLMClient(json_responses=[package, {'problems': []}, {'introduction': 'Test', 'open_now': []}])
    path = bootstrap(tmp_path, client).create(data)
    context = json.loads(client.calls[0]['messages'][0]['content'])
    assert context['input']['adaptive_questions'][0]['purpose']
    assert context['valid_evidence']['author:AQ1']['open'] is not answered
    assert context['decoded_answers']['author:AQ1']['question'] == adaptive_question()['scene']
    readable = (path / 'source' / 'answers.md').read_text(encoding='utf-8')
    assert 'author:AQ1' in readable and '確かめる境目' in readable and '- E: 希望を確かめる' in readable
    saved = BootstrapInput.model_validate_json((path / 'source' / 'input.json').read_text(encoding='utf-8'))
    assert saved.adaptive_questions[0].scene == adaptive_question()['scene']


def test_adaptive_augment_refines_prior_domain_and_keeps_sources(tmp_path):
    root = tmp_path / 'characters'
    shutil.copytree(ROOT / 'tests' / 'fixtures' / 'characters' / 'fixture', root / 'fixture')
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    store = Store(tmp_path / 'runtime.sqlite3')
    domain = next(d.model_dump() for d in load_character('fixture', root=root).model.domains if d.id == 'R01')
    domain.update(description='Support scene', status='supported', known='Listen', evidence=['author:R1'])
    refined = {**domain, 'status': 'well_supported', 'known': 'Listen, then advise when asked', 'evidence': ['author:R1', 'author:AQ1']}
    client = FakeLLMClient(json_responses=[{'domains': [domain]}, {'problems': []}, {'domains': [refined]}, {'problems': []},
                                         {'questions': [adaptive_question('AQ2')]}, {'problems': []}])
    runner = Bootstrapper(load_config(), client, characters_root=root, store=store, trace_root=tmp_path / 'traces')
    runner.augment('fixture', {'R1': ['A']})
    runner.augment('fixture', {}, adaptive_questions=[adaptive_question()], adaptive_answers={'AQ1': ['A']})
    context = json.loads(client.calls[2]['messages'][0]['content'])
    assert context['valid_evidence']['author:AQ1']['choices'] == ['一緒に考える']
    assert 'author:R1' in context['prior_answers']
    assert store.bootstrap_answers('fixture')['author:AQ1']['question_choices']['E'] == '希望を確かめる'
    assert '確かめる境目' in store.author_sources(load_character('fixture', root=root))['augment_2.md']
    assert runner.adaptive(BootstrapInput(id='fixture', basics={'name': 'テスト係'}), existing_id='fixture')['questions'][0]['id'] == 'AQ2'
    assert json.loads(client.calls[-2]['messages'][0]['content'])['allowed_ids'] == ['AQ2', 'AQ3', 'AQ4', 'AQ5']
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_life_adaptive_answer_is_evidence_for_builder(tmp_path):
    data = minimal_input().model_dump(mode='json', by_alias=True)
    data['life'] = {'Q7': ['B']}
    data.update(adaptive_questions=[adaptive_question(domain='life:Q7')], adaptive_answers={'AQ1': ['A']})
    data = BootstrapInput.model_validate(data)
    package = package_data(data)
    package['model']['life_history']['trajectories'] = [{'tendency': 'Learned', 'origin': 'acquired', 'evidence': ['life:Q7', 'author:AQ1'], 'linked_to': None}]
    client = FakeLLMClient(json_responses=[package, {'problems': []}, {'introduction': 'Test', 'open_now': []}])
    bootstrap(tmp_path, client).create(data)
    context = json.loads(client.calls[0]['messages'][0]['content'])
    assert context['valid_evidence']['author:AQ1']['domain'] == 'life:Q7'


def test_adaptive_judge_fixes_semantic_problem_once(tmp_path):
    first = adaptive_question(domain='life:Q7')
    fixed = {**first, 'scene': 'この姿勢に関わったものは？'}
    client = FakeLLMClient(json_responses=[{'questions': [first]}, {'problems': ['AQ1: unspecified past premise']},
                                         {'questions': [fixed]}, {'problems': []}])
    data = minimal_input().model_copy(update={'life': {'Q7': ['B']}})
    result = bootstrap(tmp_path, client).adaptive(data)
    assert result['questions'][0]['scene'] == fixed['scene']
    context = json.loads(client.calls[2]['messages'][0]['content'])
    assert context['validation_problems'] == ['AQ1: unspecified past premise']
    assert context['previous_questions']['questions'][0]['scene'] == first['scene']
    assert client.calls[1]['model'] == load_config().models.judge


def test_adaptive_judge_repeated_failure_can_be_skipped(tmp_path):
    client = FakeLLMClient(json_responses=[{'questions': [adaptive_question()]}, {'problems': ['rephrase']},
                                         {'questions': [adaptive_question()]}, {'problems': ['rephrase']}])
    runner = bootstrap(tmp_path, client)
    with pytest.raises(ValueError, match='after one fix'):
        runner.adaptive(answered_input())
    assert len(client.calls) == 4
    client.json_responses = [package_data(), {'problems': []}, {'introduction': 'Test', 'open_now': []}]
    assert runner.create(minimal_input()).is_dir()


def test_adaptive_can_omit_question_in_fix(tmp_path):
    client = FakeLLMClient(json_responses=[{'questions': [adaptive_question()]}, {'problems': ['rephrase']}, {'questions': []}])
    assert bootstrap(tmp_path, client).adaptive(answered_input())['questions'] == []
    assert len(client.calls) == 3


def test_soft_prior_open_with_separate_evidence_and_no_reasking(tmp_path):
    data = answered_input().model_copy(update={'answers': {'R1': ['A'], 'R3': ['unknown'], 'R7': ['leave_open']}})
    package = answered_package(data)
    domain = next(d for d in package['model']['domains'] if d['id'] == 'R03')
    domain.update(soft_prior='まず相手の様子を穏やかに確かめそう。', soft_prior_evidence=['author:R1'])
    assert not validate_package(CharacterPackage.model_validate(package), data, ReaderOutput(), load_questionnaire(), clock.now().date())
    for did in ('R01',):
        bad = deepcopy(package)
        next(d for d in bad['model']['domains'] if d['id'] == did).update(
            soft_prior='Unexpected hint', soft_prior_evidence=['author:R1'])
        assert any('soft_prior is only allowed' in e for e in validate_package(
            CharacterPackage.model_validate(bad), data, ReaderOutput(), load_questionnaire(), clock.now().date()))
    domain['soft_prior_evidence'] = ['author:R3']
    assert any('invalid soft_prior evidence' in e for e in validate_package(
        CharacterPackage.model_validate(package), data, ReaderOutput(), load_questionnaire(), clock.now().date()))
    domain.update(soft_prior=None, soft_prior_evidence=[])
    assert not validate_package(CharacterPackage.model_validate(package), data, ReaderOutput(), load_questionnaire(), clock.now().date())
    domain.update(soft_prior='まず相手の様子を穏やかに確かめそう。', soft_prior_evidence=['author:R1'])
    client = FakeLLMClient(json_responses=[package, {'problems': []}, {'introduction': 'Test', 'open_now': []}])
    path = bootstrap(tmp_path, client).create(data)
    domains = {d.id: d for d in load_character(data.id, root=path.parent).model.domains}
    assert domains['R03'].soft_prior and domains['R03'].status == 'open' and not domains['R03'].evidence
    assert domains['R07'].soft_prior is None and domains['R07'].status == 'open'
    assert not {'R3', 'R7'} & {q['id'] for q in gap_questions(data, ReaderOutput())}


def test_create_settles_status_and_hint_refs_instead_of_failing(tmp_path):
    data = answered_input().model_copy(update={'answers': {'R1': ['A'], 'R3': ['leave_open']}})
    package = answered_package(data)
    domains = {d['id']: d for d in package['model']['domains']}
    domains['R01'].update(status='well_supported', evidence=['author:R1', 'author:R3'])  # R3 was left open
    domains['R03'].update(soft_prior='お茶を淹れながら考えそう。', soft_prior_evidence=['now:like:0', 'author:R3'])
    domains['R07'].update(soft_prior='根拠のないヒント。', soft_prior_evidence=['missing:Q1'])
    client = FakeLLMClient(json_responses=[package, {'problems': []}, {'introduction': 'Test', 'open_now': []}])
    path = bootstrap(tmp_path, client).create(data)
    assert len(client.calls) == 3  # no fix round
    domains = {d.id: d for d in load_character(data.id, root=path.parent).model.domains}
    assert domains['R01'].evidence == ['author:R1'] and domains['R01'].status == 'supported'
    assert domains['R03'].soft_prior and domains['R03'].soft_prior_evidence == ['now:like:0']
    assert domains['R07'].soft_prior is None and domains['R07'].soft_prior_evidence == []


def test_create_narrows_evidence_cited_from_another_scene(tmp_path):
    data = answered_input().model_copy(update={'answers': {'R1': ['A'], 'R3': ['B']}})
    package = answered_package(data)
    domains = {d['id']: d for d in package['model']['domains']}
    domains['R03'].update(status='supported', known='再会を喜ぶ。', evidence=['author:R3'])
    domains['R01'].update(status='well_supported', evidence=['author:R1', 'author:R3'])
    domains['R05'].update(status='supported', known='理由をまず確かめる。', evidence=['author:R1'])
    client = FakeLLMClient(json_responses=[package, {'problems': []}, {'introduction': 'Test', 'open_now': []}])
    path = bootstrap(tmp_path, client).create(data)
    assert len(client.calls) == 3  # no fix round
    domains = {d.id: d for d in load_character(data.id, root=path.parent).model.domains}
    assert domains['R01'].evidence == ['author:R1'] and domains['R01'].status == 'supported' and domains['R01'].known
    assert domains['R05'].status == 'open' and domains['R05'].known is None and domains['R05'].evidence == []
    assert domains['R05'].soft_prior == '理由をまず確かめる。' and domains['R05'].soft_prior_evidence == ['author:R1']


@pytest.mark.parametrize('answer', [None, ['unknown'], ['leave_open']])
def test_adaptive_refuses_open_or_unanswered_parent(tmp_path, answer):
    data = answered_input().model_copy(update={'answers': {'R1': ['A'], **({'R3': answer} if answer else {})}})
    q = adaptive_question(domain='R03', parent='R3')
    client = FakeLLMClient(json_responses=[{'questions': [q]}])
    with pytest.raises(ValueError, match='parent must be answered'):
        bootstrap(tmp_path, client).adaptive(data)
    assert len(client.calls) == 1  # Code rejects before judge.
    invalid = data.model_copy(update={'adaptive_questions': AdaptiveOutput(questions=[q]).questions})
    with pytest.raises(ValueError, match='parent must be answered'):
        bootstrap(tmp_path, FakeLLMClient()).create(invalid)


@pytest.mark.parametrize('condition', ['', '  \n  '])
def test_adaptive_requires_nonempty_added_condition(condition):
    with pytest.raises(ValidationError):
        AdaptiveOutput(questions=[{**adaptive_question(), 'added_condition': condition}])


def test_adaptive_cannot_target_different_unanswered_domain(tmp_path):
    client = FakeLLMClient(json_responses=[{'questions': [adaptive_question(domain='R03', parent='R1')]}])
    with pytest.raises(ValueError, match='domain must match'):
        bootstrap(tmp_path, client).adaptive(answered_input())


def test_adaptive_no_answered_parents_does_not_generate(tmp_path):
    client = FakeLLMClient()
    assert bootstrap(tmp_path, client).adaptive(minimal_input()) == {'questions': [], 'parents': {}}
    assert client.calls == []
    data = minimal_input().model_copy(update={'life': {'Q6': ['F'], 'Q7': ['H']}})
    assert not adaptive_parents(decode_answers(data, load_questionnaire()), load_questionnaire())


def test_augment_unknown_and_leave_open_preserve_sources_and_never_reask(tmp_path):
    root = tmp_path / 'characters'
    shutil.copytree(ROOT / 'tests' / 'fixtures' / 'characters' / 'fixture', root / 'fixture')
    store = Store(tmp_path / 'runtime.sqlite3')
    domains = [{**d, 'status': 'open', 'known': None, 'evidence': [], 'related_signatures': [], 'soft_prior': None}
               for d in domain_catalog() if d['id'] in ('R03', 'R07')]
    next(d for d in domains if d['id'] == 'R03').update(soft_prior='理由をまず確かめそう。', soft_prior_evidence=['author:R5'])
    prior_domain = next(d for d in domain_catalog() if d['id'] == 'R05')
    prior_domain.update(status='supported', known='理由を確かめる', evidence=['author:R5'], related_signatures=[])
    client = FakeLLMClient(json_responses=[{'domains': [prior_domain]}, {'problems': []}, {'domains': domains}, {'problems': []}])
    runner = Bootstrapper(load_config(), client, characters_root=root, store=store, trace_root=tmp_path / 'traces')
    runner.augment('fixture', {'R5': ['B']})
    runner.augment('fixture', {'R3': ['unknown'], 'R7': ['leave_open']})
    current = {d.id: d for d in store.current_model(load_character('fixture', root=root)).domains}
    assert current['R03'].soft_prior_evidence == ['author:R5']
    assert current['R07'].soft_prior is None
    assert store.bootstrap_answers('fixture')['author:R7']['open']
