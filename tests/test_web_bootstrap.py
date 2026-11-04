import shutil
import threading
import time

from fastapi.testclient import TestClient

from cbs.config import ROOT
from cbs.llm import FakeLLMClient
from cbs.web import create_app
from test_bootstrap import adaptive_question, answered_input, answered_package, minimal_input, package_data


def app_for(pipeline, tmp_path):
    root = tmp_path / 'characters'
    shutil.copytree(ROOT / 'tests' / 'fixtures' / 'characters' / 'fixture', root / 'fixture')
    return create_app(pipeline, characters_root=root, work_root=tmp_path / 'work'), root


def finish(client, response):
    assert response.status_code == 202, response.text
    jid = response.json()['id']
    for _ in range(300):
        job = client.get(f'/api/bootstrap/jobs/{jid}').json()
        if job['status'] != 'running':
            return job
        time.sleep(.01)
    raise AssertionError('job did not finish')


def test_wizard_plan_create_confirm_no_overwrite_and_switch(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    data = minimal_input()
    with TestClient(app) as client:
        assert client.get('/wizard').status_code == 200
        assert len(client.get('/api/bootstrap/questionnaire').json()['domains']) == 34
        assert client.get('/api/characters').json() == [{'id': 'fixture', 'name': 'テスト係'}]
        plan = finish(client, client.post('/api/bootstrap/plan', json=data.model_dump(mode='json', by_alias=True)))
        assert plan['status'] == 'done'
        assert plan['result']['coverage']['R01'] == 'open'
        assert all(q['default'] == 'leave_open' for q in plan['result']['questions'])
        pipeline.client = FakeLLMClient(json_responses=[package_data(), {'problems': []}, {'introduction': '紹介文', 'open_now': ['Occupation']}])
        created = finish(client, client.post('/api/bootstrap/create', json=data.model_dump(mode='json', by_alias=True)))
        assert created['status'] == 'done', created
        assert len(created['result']['seed_memories']) == 8
        assert created['result']['introduction'] == '紹介文'
        assert created['result']['author_review'] == []
        assert all(set(d) == {'description'} for d in created['result']['open_domains'])
        assert not (root / data.id).exists()
        assert len(client.get('/api/characters').json()) == 1
        confirm = f"/api/bootstrap/jobs/{created['id']}/confirm"
        assert client.post(confirm).json() == {'id': data.id}
        assert client.post(confirm).status_code == 200
        assert len(client.get('/api/characters').json()) == 2
        pipeline.wait_for_steward()
        assert client.post('/api/bootstrap/create', json=data.model_dump(mode='json', by_alias=True)).status_code == 409
        assert client.post('/api/characters/missing/switch').status_code == 404
        assert client.post(f'/api/characters/{data.id}/switch').json()['name'] == 'Test'
        assert pipeline.config.character.id == 'fixture'  # selection is browser-local
        session = client.post('/api/sessions?character_id=test_character', json={}).json()
        pipeline.client.json_responses = [{}, {'problems': []}]
        result = client.post(f"/api/sessions/{session['id']}/messages?character_id=test_character", json={'content': 'こんにちは'})
        assert result.status_code == 200, result.text
        for endpoint in ['character', 'model/changes', 'model/versions', 'episodes', 'ledger', 'secrets', 'memory']:
            assert client.get(f'/api/{endpoint}?character_id=test_character').status_code == 200
        assert client.get('/api/character?character_id=test_character').json()['model']['display_name'] == 'Test'
        assert len(client.get('/api/character?character_id=test_character').json()['canon_facts']) == 8
        assert client.get('/api/sessions?character_id=fixture').json() == []
        assert client.get(f"/api/sessions/{session['id']}/turns?character_id=fixture").status_code == 404
        tid = result.json()['turn_id']
        assert client.get(f'/api/turns/{tid}/trace?character_id=fixture').status_code == 404
        assert client.get(f'/api/turns/{tid}/trace?character_id=test_character').status_code == 200


def test_text_quotes_conflict_refusal_and_regeneration(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    data = minimal_input().model_dump(mode='json', by_alias=True)
    data['text'] = '優しい。一人暮らし。'
    data['now']['life'] = {'L2now':['B']}
    reader = {'items': [{'id': 'text:1', 'category': 'now', 'quote': '一人暮らし', 'content': '一人暮らし', 'covers': ['now:L2now']}],
              'mentions': [{'domain': 'R01', 'kind': 'trait_only', 'quotes': ['優しい']}], 'conflicts': [{'topic':'住まい', 'quote':'一人暮らし', 'question_ref':'now:L2now', 'answer_keys':['B']}]}
    pipeline.client = FakeLLMClient(json_responses=[reader, reader])
    with TestClient(app) as client:
        plan = finish(client, client.post('/api/bootstrap/plan', json=data))['result']
        assert plan['coverage']['R01'] == 'trait_only'
        assert '優しい' in next(q['scene'] for q in plan['questions'] if q['id'] == 'R1')
        assert not any(q['id'] == 'L2now' for q in plan['questions'])
        assert plan['confirmation_required']
        create = finish(client, client.post('/api/bootstrap/create', json=data))
        assert create['status'] == 'failed' and '確認' in create['error']
        assert not (root / data['id']).exists()
        assert not list((tmp_path / 'work' / 'drafts').glob('*/test_character/model.yaml'))
        assert len(pipeline.client.calls) == 2  # Exact Reader + review reused; conflict still blocks Builder.
        data['text'] = None
        data['now']['life'] = {}
        pipeline.client = FakeLLMClient(json_responses=[package_data(), {'problems': []}, {'introduction': 'first', 'open_now': []},
                                                     package_data(), {'problems': []}, {'introduction': 'second', 'open_now': []}])
        first = finish(client, client.post('/api/bootstrap/create', json=data))
        second = finish(client, client.post('/api/bootstrap/create', json=data))
        assert first['status'] == second['status'] == 'done'
        assert first['id'] != second['id']
        assert client.post(f"/api/bootstrap/jobs/{second['id']}/confirm").status_code == 200
        assert client.post(f"/api/bootstrap/jobs/{first['id']}/confirm").status_code == 409


def test_augment_only_unanswered_preserves_files_and_prior_answers(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    domains = [{'id': f'R0{i}', 'label': 'label', 'description': 'description', 'status': 'supported',
                'known': '寄り添う', 'evidence': [f'author:R{i}'], 'related_signatures': [], 'soft_prior': None} for i in [1, 2]]
    pipeline.client = FakeLLMClient(json_responses=[{'domains': domains}, {'problems': []}])
    with TestClient(app) as client:
        expected_questions = {f'R{i}' for i in range(1, 11)} | {'Q3', 'Q7', 'Q9', 'Q11', 'Q12', 'A4'}
        assert {q['id'] for q in client.get('/api/characters/fixture/augment').json()['questions']} == expected_questions
        assert client.post('/api/characters/fixture/augment', json={'answers': {'R1': ['Z']}}).status_code == 422
        job = finish(client, client.post('/api/characters/fixture/augment', json={'answers': {'R1': ['A'], 'R2': ['C']}}))
        assert job['status'] == 'done', job
        assert job['result']['domains'] == ['R01', 'R02']
        assert {q['id'] for q in client.get('/api/characters/fixture/augment').json()['questions']} == expected_questions - {'R1', 'R2'}
        assert client.post('/api/characters/fixture/augment', json={'answers': {'R1': ['B']}}).status_code == 409
        assert pipeline.store.bootstrap_answers('fixture')['author:R1']['answer'] == ['A']
        assert any('author:R1' in source for source in pipeline.character.sources.values())
        assert client.get('/api/character').json()['model']['domains'][-10]['status'] == 'supported'
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_slow_job_does_not_block_api(pipeline, tmp_path, monkeypatch):
    from cbs.bootstrap import Bootstrapper
    app, _ = app_for(pipeline, tmp_path)
    release, started = threading.Event(), threading.Event()
    def slow(self, data):
        started.set()
        release.wait(5)
        return {'ok': True}
    monkeypatch.setattr(Bootstrapper, 'plan', slow)
    with TestClient(app) as client:
        try:
            response = client.post('/api/bootstrap/plan', json=minimal_input().model_dump(mode='json', by_alias=True))
            assert started.wait(1)
            assert response.status_code == 202
            assert client.get('/api/bootstrap/jobs/' + response.json()['id']).json()['status'] == 'running'
            assert client.get('/api/characters').status_code == 200
        finally:
            release.set()
        assert finish(client, response)['status'] == 'done'


def test_augment_accepts_the_wizard_payload_shape(pipeline, tmp_path):
    # The wizard sends {"answer": [...], "text": ...} objects, not bare lists.
    app, _ = app_for(pipeline, tmp_path)
    domains = [{'id': 'R01', 'label': 'label', 'description': 'description', 'status': 'supported',
                'known': '寄り添う', 'evidence': ['author:R1'], 'related_signatures': [], 'soft_prior': None}]
    pipeline.client = FakeLLMClient(json_responses=[{'domains': domains}, {'problems': []}])
    with TestClient(app) as client:
        job = finish(client, client.post('/api/characters/fixture/augment',
                                         json={'answers': {'R1': {'answer': ['A'], 'text': '補足'}}}))
        assert job['status'] == 'done', job


def test_adaptive_api_generation_answer_creation_and_skip(pipeline, tmp_path):
    app, _ = app_for(pipeline, tmp_path)
    data = answered_input().model_dump(mode='json', by_alias=True)
    q = adaptive_question()
    pipeline.client = FakeLLMClient(json_responses=[{'questions': [q]}, {'problems': []}])
    with TestClient(app) as client:
        generated = finish(client, client.post('/api/bootstrap/adaptive', json=data))
        assert generated['status'] == 'done', generated
        assert generated['result']['parents']['R1']['choices'] == ['まず気持ちに寄り添って、ゆっくり話を聞く']
        assert generated['result']['parents']['R1']['title'] == '落ち込んでいる相手への寄り添い方'
        data['adaptive_questions'] = generated['result']['questions']
        for answers in [{'AQ1': {'answer': ['A'], 'text': '本人が頼んだら'}}, {}]:
            data['adaptive_answers'] = answers
            package = answered_package(answered_input())
            if answers:
                domain = next(d for d in package['model']['domains'] if d['id'] == 'R01')
                domain.update(status='well_supported', known='一緒に考える', evidence=['author:R1', 'author:AQ1'])
            pipeline.client = FakeLLMClient(json_responses=[package, {'problems': []}, {'introduction': 'Test', 'open_now': []}])
            created = finish(client, client.post('/api/bootstrap/create', json=data))
            assert created['status'] == 'done', created
        data['adaptive_questions'] = [adaptive_question(domain='life:Q99')]
        assert client.post('/api/bootstrap/create', json=data).status_code == 422


def test_augment_offers_adaptive_before_saving_and_preserves_question(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    q = adaptive_question()
    domains = [{'id': 'R01', 'label': 'label', 'description': 'description', 'status': 'well_supported',
                'known': '落ち着いたら助言', 'evidence': ['author:R1', 'author:AQ1'], 'related_signatures': [], 'soft_prior': None}]
    pipeline.client = FakeLLMClient(json_responses=[{'questions': [q]}, {'problems': []}, {'domains': domains}, {'problems': []}])
    with TestClient(app) as client:
        body = {'answers': {'R1': {'answer': ['A'], 'text': '気持ちを聞いてから'}}}
        generated = finish(client, client.post('/api/characters/fixture/adaptive', json=body))
        assert generated['status'] == 'done', generated
        assert not pipeline.store.bootstrap_answers('fixture')
        body.update(adaptive_questions=generated['result']['questions'], adaptive_answers={'AQ1': ['A']})
        saved = finish(client, client.post('/api/characters/fixture/augment', json=body))
        assert saved['status'] == 'done', saved
        assert pipeline.store.bootstrap_answers('fixture')['author:AQ1']['question'] == q['scene']
        assert 'author:AQ1' in pipeline.store.author_sources(pipeline.character)['augment_1.md']
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_web_augment_open_choices_saved_with_labels_and_never_reasked(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    domains = [{'id': f'R0{i}', 'label': 'label', 'description': 'description',
                'status': 'supported' if i == 1 else 'open', 'known': 'Listen' if i == 1 else None,
                'evidence': ['author:R1'] if i == 1 else [], 'related_signatures': [],
                'soft_prior': 'まず気持ちを確かめそう。' if i == 3 else None,
                'soft_prior_evidence': ['author:R1'] if i == 3 else []} for i in (1, 3, 7)]
    pipeline.client = FakeLLMClient(json_responses=[{'domains': domains}, {'problems': []}])
    with TestClient(app) as client:
        catalog = client.get('/api/bootstrap/questionnaire').json()['questionnaire']
        common = catalog['common_choices']
        assert common['unknown'] == 'まだ分からない（会話の中で、それっぽく決まっていく）'
        assert common['leave_open'] == '決めないでおく（話していくうちに分かる）'
        life = next(s for s in catalog['sections'] if s['id'] == 'life')['questions']
        assert life[0]['legacy_choices'] == {'G': 'unknown', 'H': 'leave_open'}
        assert 'G' not in life[0]['choices'] and 'H' not in life[0]['choices']
        assert life[0]['choices']['unknown'] == common['unknown']
        assert life[0]['choices']['leave_open'] == common['leave_open']
        body = {'answers': {'R1': ['A'], 'R3': ['unknown'], 'R7': ['leave_open']}}
        saved = finish(client, client.post('/api/characters/fixture/augment', json=body))
        assert saved['status'] == 'done', saved
        assert not {'R1', 'R3', 'R7'} & {q['id'] for q in client.get('/api/characters/fixture/augment').json()['questions']}
        assert pipeline.store.bootstrap_answers('fixture')['author:R7']['answer'] == ['leave_open']
        current = {d['id']: d for d in client.get('/api/character').json()['model']['domains']}
        assert current['R03']['status'] == 'open' and current['R03']['soft_prior']
        assert current['R07']['status'] == 'open' and current['R07']['soft_prior'] is None
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
