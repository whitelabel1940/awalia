"""LK2 expression suggestions: offline FakeLLMClient only."""
import json

import pytest
import yaml
from fastapi.testclient import TestClient

from cbs.llm import FakeLLMClient, LLMError
from cbs.look import (author_look, draft_look, field_catalog, image_prompt,
                      normalize_look, suggest_expressions, validate_look)
from test_bootstrap import minimal_input, package_data
from test_web_bootstrap import app_for, finish


def item(path, value='口元を控えめに結ぶ', quote='感情を控えめに表す', source='text'):
    return dict(path=path, value=value, quote=quote, source=source)


def test_expression_fields_storage_prompts_and_actor_exclusion(pipeline, tmp_path):
    fields = [f for f in field_catalog()['fields'] if f.get('suggestion')]
    assert len([f for f in fields if f['path'].startswith('person.expressions.')]) == 8
    assert normalize_look({})['person']['face']['habits'] == ''
    assert all(v == '' for v in normalize_look({})['person']['expressions'].values())
    data = {'person': {'face': {'eyes': '切れ長', 'habits': '共通のくせ'},
                       'expressions': {'smile': '控えめな口元 {{fields}} {{expressions}}', 'pout': '横目'}}}
    validate_look(data)
    sheet = image_prompt(data)
    assert sheet.index('下段の左から') < sheet.index('各表情の描き方:') < sheet.index('笑顔:') < sheet.index('すねる:')
    assert 'ふつう:' not in sheet and '表情のくせ: 共通のくせ' in sheet
    assert '{{fields}} {{expressions}}' in sheet  # Author text remains literal.
    data['image'] = {'use': 'single', 'from_reference': True}
    single = image_prompt(data)
    assert single.startswith('添付した画像の人物と同じ人物で')
    assert '表情のくせ: 共通のくせ' in single
    assert '各表情の描き方:' not in single and '横目' not in single
    assert author_look(data) == '目: 切れ長'
    assert '各表情の描き方:' not in image_prompt({})
    app, root = app_for(pipeline, tmp_path)
    with TestClient(app) as web:
        assert web.put('/api/look/meta', json=data).status_code == 200
        meta = web.get('/api/look').json()
        assert meta['person'] == normalize_look(data)['person']
        assert yaml.safe_load((root / 'fixture/look.yaml').read_text(encoding='utf-8'))['person'] == meta['person']
        data['image']['use'] = 'sheet'
        assert '笑顔:' in web.post('/api/look/prompt', json=data).json()['prompt']
        # Empty values overwrite saved descriptions explicitly.
        assert web.put('/api/look/meta', json={'person': {'face': {'habits': ''}, 'expressions': {'smile': ''}}}).status_code == 200
        assert web.get('/api/look').json()['person']['expressions']['smile'] == ''


def test_suggestions_share_quote_checks_and_never_assign(pipeline):
    client = FakeLLMClient(json_responses=[{'suggestions': [
        item('person.face.habits'), item('person.expressions.smile', quote='感情を\n控えめに表す'),
        item('person.expressions.smile'), item('person.expressions.sad', quote='架空の引用'),
        item('person.expressions.shy', source='missing'), item('person.expressions.pout', value=' '),
        item('person.expressions.angry'), item('person.face.marks'), item('person.profile.vibe')]}])
    checked, _ = suggest_expressions(pipeline.config, client, {'text': '感情を控えめに表す'})
    assert set(checked) == {'suggestions', 'dropped'}
    assert [i['path'] for i in checked['suggestions']] == ['person.face.habits', 'person.expressions.smile']
    assert checked['dropped'] == 7
    call = client.calls[0]
    assert call['model'] == pipeline.config.models.steward
    assert '1枚の静止画' in call['system'] and '言えない表情は返さない' in call['system']
    assert len(json.loads(call['messages'][0]['content'])['fields']) == 9
    client.json_responses = [{'suggestions': [item('person.face.habits', value='x' * 3000),
                                             item('person.expressions.smile', value='y' * 1001)]}]
    checked, _ = suggest_expressions(pipeline.config, client, {'text': '感情を控えめに表す'})
    assert len(checked['suggestions']) == 1 and checked['dropped'] == 1
    client.json_responses = [{'assignments': [item('person.face.habits')],
                              'suggestions': [item('person.expressions.smile')]}]
    checked, _ = draft_look(pipeline.config, client, {'text': '感情を控えめに表す'})
    assert checked == {'assignments': [], 'suggestions': [], 'dropped': 2}
    assert all('expressions.' not in f['path'] and f['path'] != 'person.face.habits'
               for f in json.loads(client.calls[-1]['messages'][0]['content'])['fields'])


@pytest.mark.parametrize('person', [
    {'expressions': {'angry': 'bad'}}, {'expressions': {'smile': True}},
    {'face': {'habits': 1}}, {'expressions': {'smile': 'x' * 2001}, 'face': {'habits': 'y' * 2000}},
])
def test_expression_invalid_values(pipeline, tmp_path, person):
    app, _ = app_for(pipeline, tmp_path)
    with TestClient(app) as web:
        assert web.put('/api/look/meta', json={'person': person}).status_code == 422
        assert web.post('/api/look/prompt', json={'person': person}).status_code == 422


def test_established_and_bootstrap_suggestions_use_original_sources_without_saving(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    original = minimal_input().model_dump(mode='json', by_alias=True)
    original['text'] = '感情を控えめに表す'
    (root / 'fixture/source/input.json').write_text(json.dumps(original, ensure_ascii=False), encoding='utf-8')
    with TestClient(app) as web:
        before = web.get('/api/look').json()
        pipeline.client.json_responses = [{'suggestions': [item('person.face.habits', source='input.json')]}]
        result = web.post('/api/look/expression-suggestions')
        assert result.status_code == 200 and len(result.json()['suggestions']) == 1
        context = json.loads(pipeline.client.calls[-1]['messages'][0]['content'])
        assert context['sources']['model'] and context['sources']['expression'] and context['sources']['world']
        assert context['sources']['input.json'] == json.dumps(original, ensure_ascii=False)
        assert web.get('/api/look').json() == before
        pipeline.client.json_responses = [{}, package_data(), {'problems': []}, {'introduction': '確認用', 'open_now': []}]
        job = finish(web, web.post('/api/bootstrap/create', json=original))
        assert job['status'] == 'done', job
        pipeline.client.json_responses = [{'suggestions': [item('person.expressions.smile', source='input.json')]}]
        endpoint = f"/api/bootstrap/jobs/{job['id']}/look-expressions"
        result = web.post(endpoint)
        assert result.status_code == 200 and len(result.json()['suggestions']) == 1
        context = json.loads(pipeline.client.calls[-1]['messages'][0]['content'])
        assert json.loads(context['sources']['input.json'])['text'] == original['text']
        assert not (root / minimal_input().id).exists()
        assert web.post('/api/bootstrap/jobs/missing/look-expressions').status_code == 409
        assert web.post(f"/api/bootstrap/jobs/{job['id']}/confirm").status_code == 200
        assert web.post(endpoint).status_code == 409


def test_expression_api_failure_preserves_state(pipeline, tmp_path):
    class FailedClient(FakeLLMClient):
        def generate_json(self, *args, **kwargs):
            raise LLMError('private provider details')
    app, _ = app_for(pipeline, tmp_path)
    with TestClient(app) as web:
        before = web.get('/api/look').json()
        pipeline.client = FailedClient()
        response = web.post('/api/look/expression-suggestions')
        assert response.status_code == 502 and 'private' not in response.text
        assert web.get('/api/look').json() == before
