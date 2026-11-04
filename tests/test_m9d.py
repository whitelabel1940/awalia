import json
import threading

import pytest
import yaml
from fastapi.testclient import TestClient

from cbs.bootstrap import unresolved_conflicts
from cbs.bootstrap_schema import BootstrapInput, ReaderOutput
from cbs.llm import FakeLLMClient
from test_bootstrap import bootstrap, minimal_input, package_data
from test_web_bootstrap import app_for, finish


def conflict_input():
    data = minimal_input().model_copy(update={'text': '一人暮らし。一人称はぼく。'})
    data.now.life = {'L2now': ['B'], 'L1now': ['leave_open']}
    conflict = {'topic': '住まい', 'quote': '一人暮らし', 'question_ref': 'now:L2now', 'answer_keys': ['B']}
    reader = {'items': [{'id': 'text:1', 'category': 'expression', 'content': '一人称はぼく',
                         'quote': '一人称はぼく', 'covers': []},
                        {'id': 'text:2', 'category': 'now', 'content': '一人暮らし', 'quote': '一人暮らし', 'covers': []}], 'conflicts': [conflict]}
    return data, conflict, reader


@pytest.mark.parametrize('choice', ['text', 'answer'])
@pytest.mark.parametrize('policy', ['keep', 'may_elaborate'])
def test_conflict_choice_reaches_builder_and_persisted_input(tmp_path, choice, policy):
    data, conflict, reader = conflict_input()
    data = BootstrapInput.model_validate({**data.model_dump(by_alias=True), 'conflict_resolutions': [{'conflict': conflict, 'choice': choice}]})
    data.elaborate = {'default': policy}
    package = package_data(data)
    package['expression']['voice'].update(first_person='ぼく', sentence_endings=['だよ'])
    selected = '一人暮らし' if choice == 'text' else '家族と暮らす'
    package['expression']['elaborate_default'] = policy
    package['expression']['items'] = [{'ref': 'text:1', 'content': '一人称はぼく', 'elaborate': policy},
                                      {'ref': 'text:2', 'content': selected, 'elaborate': policy}]
    package['expression']['current_life'] = [{'ref': 'now:L2now', 'content': selected, 'elaborate': policy}]
    fake = FakeLLMClient(json_responses=[reader, reader, package, {'problems': []},
                                       {'introduction': '静かに話す人。', 'open_now': []}])
    runner = bootstrap(tmp_path, fake)
    assert runner.plan(data)['confirmation_required'] == []
    directory = runner.create(data)
    context = json.loads(next(c['messages'][0]['content'] for c in fake.calls if c['schema']['title'] == 'CharacterPackage'))
    assert context['input']['conflict_resolutions'][0]['choice'] == choice
    assert context['decoded_answers']['now:L2now']['answer'] == ['B']
    assert context['decoded_answers']['now:L1now']['answer'] == ['leave_open']
    assert context['reader']['items'][0]['category'] == 'expression'
    assert context['overridden_text_refs'] == (['text:2'] if choice == 'answer' else [])
    saved = BootstrapInput.model_validate_json((directory / 'source/input.json').read_text(encoding='utf-8'))
    assert saved.conflict_resolutions == data.conflict_resolutions
    voice = yaml.safe_load((directory / 'expression.yaml').read_text(encoding='utf-8'))['voice']
    assert voice['first_person'] == 'ぼく' and voice['sentence_endings'] == ['だよ']
    assert yaml.safe_load((directory / 'expression.yaml').read_text(encoding='utf-8'))['items'][1]['content'] == selected
    summary = (directory / 'summary.md').read_text(encoding='utf-8')
    assert 'CBS01' not in summary and 'R01' not in summary


@pytest.mark.parametrize('change', ['answer', 'text', 'duplicate'])
def test_stale_or_duplicate_conflict_choice_rejected(change):
    data, conflict, reader = conflict_input()
    data = BootstrapInput.model_validate({**data.model_dump(by_alias=True), 'conflict_resolutions': [{'conflict': conflict, 'choice': 'answer'}]})
    if change == 'answer':
        data.now.life['L2now'] = ['C']
    elif change == 'text':
        data.text = '別の設定'
    else:
        data.conflict_resolutions *= 2
    from cbs.bootstrap import load_questionnaire
    with pytest.raises(ValueError, match='一致'):
        unresolved_conflicts(data, ReaderOutput.model_validate(reader), load_questionnaire())


def test_unverifiable_conflict_is_retried_then_dropped_with_warning(tmp_path):
    # A conflict that still cannot be traced to the text and an answer after one retry is dropped
    # (like other invalid Reader entries) instead of stopping the whole wizard.
    data, _, reader = conflict_input()
    reader['conflicts'][0]['quote'] = '書いていない引用'
    fake = FakeLLMClient(json_responses=[reader, reader])
    plan = bootstrap(tmp_path, fake).plan(data)
    assert len(fake.calls) == 3
    assert plan['confirmation_required'] == []
    assert any(w.startswith('conflicts[0]') for w in plan['warnings'])


def test_conflict_answer_given_as_choice_text_is_accepted(tmp_path):
    from cbs.bootstrap import decode_answers, load_questionnaire
    data, _, reader = conflict_input()
    decoded = decode_answers(data, load_questionnaire())[reader['conflicts'][0]['question_ref']]
    reader['conflicts'][0]['answer_keys'] = decoded['choices']
    fake = FakeLLMClient(json_responses=[reader])
    plan = bootstrap(tmp_path, fake).plan(data)
    assert len(fake.calls) == 2
    assert [c['answer_keys'] for c in plan['confirmation_required']] == [decoded['answer']]


def test_confirmation_revision_preserves_base_and_notes(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    data = minimal_input().model_dump(mode='json', by_alias=True)
    summary = {'introduction': '紹介だけ。', 'open_now': ['仕事の内容'],
               'author_review': [{'ref': 'world/notes/daily_life', 'supplemented': '身近な道具を補いました。', 'based_on': '現代の日本という設定です。'}]}
    pipeline.client = FakeLLMClient(json_responses=[package_data(), {'problems': []}, summary,
                                                  package_data(), {'problems': []}, summary])
    with TestClient(app) as client:
        draft = finish(client, client.post('/api/bootstrap/create', json=data))
        assert draft['status'] == 'done'
        result = draft['result']
        assert result['introduction'] == '紹介だけ。'
        assert result['author_review'] == summary['author_review']
        assert all(set(d) == {'description'} for d in result['open_domains'])
        data['review_notes'] = {'world/notes/daily_life': '静かな暮らしにする'}
        revised = finish(client, client.post(f'/api/bootstrap/jobs/{draft["id"]}/revise', json=data))
        assert revised['status'] == 'done'
        builder_calls = [c for c in pipeline.client.calls if c['schema']['title'] == 'CharacterPackage']
        context = json.loads(builder_calls[-1]['messages'][0]['content'])
        assert context['review_base']['seed_memories'] == package_data()['seed_memories']
        assert context['input']['review_notes'] == data['review_notes']
        assert not (root / data['id']).exists()
        assert client.post(f'/api/bootstrap/jobs/{revised["id"]}/confirm', json={}).status_code == 200
        assert client.post(f'/api/bootstrap/jobs/{revised["id"]}/revise', json=data).status_code == 409


def test_jobs_report_read_build_validate_review_stages(pipeline, tmp_path):
    app, _ = app_for(pipeline, tmp_path)
    data, _, reader = conflict_input()
    reader['conflicts'] = []
    package = package_data(data)
    package['expression']['items'] = [{'ref': i['id'], 'content': i['content'], 'elaborate': 'may_elaborate'} for i in reader['items']]
    package['expression']['current_life'] = [{'ref': 'now:L2now', 'content': '家族と暮らす', 'elaborate': 'may_elaborate'}]
    entered, proceed = threading.Event(), threading.Event()

    class PausedFake(FakeLLMClient):
        def generate_json(self, *args, **kwargs):
            entered.set()
            assert proceed.wait(10)
            proceed.clear()
            return super().generate_json(*args, **kwargs)

    pipeline.client = PausedFake(json_responses=[reader, package, {'problems': []}, {'introduction': '紹介', 'open_now': []}])
    with TestClient(app) as client:
        response = client.post('/api/bootstrap/create', json=data.model_dump(mode='json', by_alias=True))
        jid = response.json()['id']
        for expected, percent in [('reading', 10), ('reading', 10), ('building', 55), ('validation', 80), ('review', 90), ('saving', 95)]:
            assert entered.wait(10)
            entered.clear()
            job = client.get(f'/api/bootstrap/jobs/{jid}').json()
            assert job['stage'] == expected and job['progress_percent'] == percent
            assert '実行中' in job['progress']
            proceed.set()
        completed = finish(client, response)
        assert completed['status'] == 'done' and completed['progress_percent'] == 100


def test_adaptive_job_reports_question_generation(pipeline, tmp_path):
    app, _ = app_for(pipeline, tmp_path)
    data = minimal_input().model_dump(mode='json', by_alias=True)
    data['answers'] = {'R1': ['A']}
    entered, proceed = threading.Event(), threading.Event()

    class PausedFake(FakeLLMClient):
        def generate_json(self, *args, **kwargs):
            entered.set()
            assert proceed.wait(10)
            return super().generate_json(*args, **kwargs)

    pipeline.client = PausedFake(json_responses=[{'questions': []}])
    with TestClient(app) as client:
        response = client.post('/api/bootstrap/adaptive', json=data)
        assert entered.wait(10)
        job = client.get(f'/api/bootstrap/jobs/{response.json()["id"]}').json()
        assert job['stage'] == 'questions' and job['progress_percent'] == 35
        assert job['progress'] == '質問の生成を実行中です。'
        proceed.set()
        assert finish(client, response)['status'] == 'done'
