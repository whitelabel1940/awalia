import json

import pytest
from fastapi.testclient import TestClient

from cbs.bootstrap import Bootstrapper
from cbs.bootstrap_schema import BootstrapInput
from cbs.llm import FakeLLMClient
from cbs.web.bootstrap_api import ConfirmInput, save_welcome_details
from test_bootstrap import minimal_input, package_data
from test_web_bootstrap import app_for, finish


def test_age_gender_are_author_sources_and_builder_input(pipeline, tmp_path):
    data = minimal_input().model_dump(by_alias=True)
    data['basics'].update(age='200歳くらい', gender='未定')
    data = BootstrapInput.model_validate(data)
    pipeline.client = FakeLLMClient(json_responses=[package_data(), {'problems': []}, {'introduction': '紹介', 'open_now': []}])
    Bootstrapper(pipeline.config, pipeline.client, characters_root=tmp_path, store=pipeline.store).create(data)
    call = next(c for c in pipeline.client.calls if 'input.basics.age' in c['system'])
    payload = json.loads(call['messages'][0]['content'])
    assert payload['input']['basics']['gender'] == '未定'
    assert payload['input']['basics']['age'] == '200歳くらい'
    assert payload['valid_evidence']['basics:age'] == '200歳くらい'


def test_confirm_followup_failure_preserves_partner_and_retries_only_failed(pipeline, tmp_path, monkeypatch):
    app, root = app_for(pipeline, tmp_path)
    data = minimal_input().model_dump(mode='json', by_alias=True)
    pipeline.client = FakeLLMClient(json_responses=[package_data(), {'problems': []}, {'introduction': '紹介', 'open_now': []}])
    body = {'days': [{'kind': 'partner_birthday', 'name': '誕生日', 'month': 2, 'day': 29},
                     {'kind': 'user_birthday', 'name': 'あなたの誕生日', 'month': 11, 'day': 1, 'year': 2000}],
            'user_name': 'アキ', 'rules': ['絵文字を使わない', '短く話す']}
    from cbs.store import Store
    original = Store.save_requests
    calls = []
    def flaky(self, *args, **kwargs):
        calls.append('requests')
        if len(calls) == 1:
            raise ValueError('fake failure')
        return original(self, *args, **kwargs)
    with TestClient(app) as client:
        created = finish(client, client.post('/api/bootstrap/create', json=data))
        assert created['status'] == 'done', created
        endpoint = f"/api/bootstrap/jobs/{created['id']}/confirm"
        monkeypatch.setattr(Store, 'save_requests', flaky)
        response = client.post(endpoint, json=body)
        assert response.status_code == 502
        assert '迎えました' in response.json()['detail']
        assert (root / data['id'] / 'model.yaml').exists()
        scope = '?character_id=' + data['id']
        saved = client.get('/api/partner/days' + scope).json()
        assert len(saved['days']) == 2
        assert saved['days'][0]['year'] is None
        assert client.post(endpoint, json=body).status_code == 200
        assert client.post(endpoint, json=body).status_code == 200
        assert len(calls) == 2
        assert client.get('/api/partner/days' + scope).json() == saved
        requests = client.get('/api/partner/requests' + scope).json()
        assert requests['fields']['user_name'] == 'アキ'
        assert [l['text'] for l in requests['lines']] == body['rules']
        assert client.get(f"/api/bootstrap/jobs/{created['id']}").status_code == 200


def test_about_uses_existing_judgment_and_idempotent_save(pipeline):
    from cbs.import_profiles import review_about
    category = next(iter(pipeline.config.knowing.private_categories))
    fact = {'content': '紅茶が好き', 'quote': '紅茶が好き', 'private': True, 'private_category': category}
    pipeline.client = FakeLLMClient(json_responses=[{'facts': [fact], 'requests': []}])
    review = review_about(pipeline, '紅茶が好き')
    body = ConfirmInput(user_name='アキ', rules=['短く話す'], about_token=review['token'], about_keys=['0'])
    done = set()
    save_welcome_details(pipeline, body, done)
    save_welcome_details(pipeline, body, done)
    facts = pipeline.store.list_user_facts(pipeline.config.character.id, current_only=True)
    assert len(facts) == 1 and facts[0]['private'] and facts[0]['private_category'] == category
    assert len(pipeline.client.calls) == len(pipeline.client.embedding_calls) == 1
    assert done == {'days', 'requests', 'about'}


def test_invalid_birthday_rejected_before_publication():
    with pytest.raises(ValueError):
        ConfirmInput(days=[{'kind': 'user_birthday', 'name': '誕生日', 'month': 2, 'day': 30}])


def test_provisional_about_review_saved_on_confirm_to_knowing(pipeline, tmp_path, monkeypatch):
    errors = []
    def checked(*args):
        try:
            return save_welcome_details(*args)
        except Exception as error:
            errors.append(repr(error))
            raise
    monkeypatch.setattr('cbs.web.bootstrap_api.save_welcome_details', checked)
    app, _ = app_for(pipeline, tmp_path)
    data = minimal_input().model_dump(mode='json', by_alias=True)
    pipeline.client = FakeLLMClient(json_responses=[package_data(), {'problems': []}, {'introduction': '紹介', 'open_now': []}])
    with TestClient(app) as client:
        created = finish(client, client.post('/api/bootstrap/create', json=data))
        category = next(iter(pipeline.config.knowing.private_categories))
        pipeline.client.json_responses = [{'facts': [{'content': '紅茶が好き', 'quote': '紅茶が好き', 'private': True,
                                                     'private_category': category}],
                                           'requests': [{'quote': '絵文字なしで', 'request': {'operation': 'add', 'text': '絵文字を使わない'}}]}]
        review = client.post(f"/api/bootstrap/import/{data['id']}/about/review", json={'text': '紅茶が好き。絵文字なしで'}).json()
        response = client.post(f"/api/bootstrap/jobs/{created['id']}/confirm", json={
            'about_token': review['token'], 'about_keys': ['0', '1'], 'user_name': 'アキ', 'rules': ['短く話す']})
        assert response.status_code == 200, (response.text, errors)
        scope = '?character_id=' + data['id']
        facts = client.get('/api/note/private' + scope).json()['facts']
        assert len(facts) == 1 and facts[0]['content'] == '紅茶が好き'
        assert facts[0]['private_category'] == category
        requests = client.get('/api/partner/requests' + scope).json()
        assert requests['fields']['user_name'] == 'アキ'
        assert [r['text'] for r in requests['lines']] == ['絵文字を使わない', '短く話す']
