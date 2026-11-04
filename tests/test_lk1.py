"""LK1 storage/prompt/draft integration, using only FakeLLMClient."""
import json
import yaml
import pytest
from fastapi.testclient import TestClient
from cbs.look import (normalize_look, look_meta, image_prompt, author_look,
                      draft_look, draft_sources, field_catalog)
from cbs.llm import FakeLLMClient
from test_web_bootstrap import app_for, finish
from test_bootstrap import minimal_input, package_data


def test_old_format_migrates_only_on_save_and_preserves_accent(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    path = root / 'fixture' / 'look.yaml'
    old = 'accent: akane\nfeatures: 古い補足\n'
    path.write_text(old, encoding='utf-8')
    with TestClient(app) as web:
        meta = web.get('/api/look').json()
        assert meta['version'] == 2 and meta['features'] == '古い補足'
        assert meta['person']['hair']['color'] == ''
        assert path.read_text(encoding='utf-8') == old
        meta = web.put('/api/look/meta', json={'person': {'hair': {'color': '銀色'}}}).json()
        assert meta['accent'] == 'madder' and meta['features'] == '古い補足'
        assert yaml.safe_load(path.read_text(encoding='utf-8'))['version'] == 2
        assert web.get('/api/look').json()['person']['hair']['color'] == '銀色'
        assert '古い補足' in web.post('/api/look/prompt', json={'features': meta['features']}).json()['prompt']


@pytest.mark.parametrize('payload', [
    {'person': {'hair': {'color': 'x' * 4001}}},
    {'person': {'hair': {'color': 'x' * 2001}, 'face': {'eyes': 'x' * 2000}}},
    {'image': {'use': 'full_body'}}, {'image': {'from_reference': 'yes'}},
    {'person': {'invented': 'value'}}, {'person': {'hair': []}},
])
def test_invalid_fields_are_rejected(pipeline, tmp_path, payload):
    app, _ = app_for(pipeline, tmp_path)
    with TestClient(app) as web:
        assert web.put('/api/look/meta', json=payload).status_code == 422
        assert web.post('/api/look/prompt', json=payload).status_code == 422


def test_catalog_defaults_and_no_placeholder_values():
    meta = normalize_look({}, world={'being': '精霊', 'preset': '和風ファンタジー'})
    assert meta['person']['profile']['species'] == '精霊'
    assert meta['person']['outfit']['mood'] == '和装'
    assert normalize_look({}, world={'being': '人間'})['person']['profile']['species'] == ''
    prompt = image_prompt({})
    assert 'アニメ調' not in prompt and '20代' not in prompt
    assert '見た目の年齢:' not in prompt
    assert len(field_catalog()['tabs']) == 6


def test_prompt_modes_order_and_actor_projection(pipeline, tmp_path):
    data = {'person': {'style': {'base': '水彩風'}, 'profile': {'age': '自由な年齢'},
                       'face': {'eyes': '切れ長'}, 'hair': {'color': '黒'},
                       'outfit': {'mood': '普段着'}, 'must': '眼鏡', 'avoid': '帽子'},
            'image': {'background': '窓辺', 'angle': '正面'}, 'features': '補足'}
    sheet = image_prompt(data)
    assert '背景: 無地の淡い色' in sheet and '窓辺' not in sheet
    assert '横4列×縦2行' in sheet
    assert sheet.index('絵柄:') < sheet.index('見た目の年齢:') < sheet.index('目:') < sheet.index('髪の色:') < sheet.index('服の雰囲気:') < sheet.index('背景:') < sheet.index('必ず入れたいこと:') < sheet.index('避けたいこと:')
    data['image']['use'] = 'single'
    single = image_prompt(data)
    assert '正方形で胸から上' in single and '背景: 窓辺' in single and '8マス' not in single
    data['image']['from_reference'] = True
    assert image_prompt(data).startswith('添付した画像の人物と同じ人物で')
    description = author_look(data)
    for text in ('自由な年齢', '切れ長', '黒', '普段着', '眼鏡', '補足'):
        assert text in description
    for text in ('水彩風', '窓辺', '正面', '帽子'):
        assert text not in description
    app, root = app_for(pipeline, tmp_path)
    with TestClient(app) as web:
        assert web.put('/api/look/meta', json=data).status_code == 200
        reply = pipeline.run(pipeline.store.create_session('fixture')['id'], 'hi')
        assert pipeline.store.get_trace(reply['turn_id'])['Packet']['output']['author_look'] == description


def test_draft_drops_unverified_quotes_and_keeps_suggestions_separate(pipeline):
    item = lambda path, value, quote, source='text': dict(path=path, value=value, quote=quote, source=source)
    client = FakeLLMClient(json_responses=[{
        'assignments': [item('person.hair.color', '銀色', '銀色の髪'),
                        item('person.face.eye_color', '青', '青い目'),
                        item('person.outfit.items', '白シャツ', '白いシャツ', 'missing'),
                        item('person.hair.length', '短い', '短い\n髪'),
                        item('person.unknown', '不正', '銀色の髪')],
        'suggestions': [item('person.profile.vibe', 'やわらかな微笑み', '穏やかな性格'),
                        item('person.face.marks', '傷跡', '穏やかな性格')]}])
    result, _ = draft_look(pipeline.config, client, {'text': '銀色の髪。短い髪。穏やかな性格。'})
    assert [i['path'] for i in result['assignments']] == ['person.hair.color', 'person.hair.length']
    assert len(result['suggestions']) == 1 and result['dropped'] == 4
    assert client.calls[0]['model'] == pipeline.config.models.steward


def test_established_and_unpublished_draft_sources(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    original = minimal_input().model_dump(mode='json', by_alias=True)
    original['world']['preset'] = '和風ファンタジー'
    (root / 'fixture' / 'source' / 'input.json').write_text(json.dumps(original, ensure_ascii=False), encoding='utf-8')
    with TestClient(app) as web:
        # Clear only the copy so old author look fields cannot hide these defaults.
        (root / 'fixture' / 'look.yaml').unlink(missing_ok=True)
        meta = web.get('/api/look').json()
        assert meta['person']['outfit']['mood'] == '和装'
        assert web.put('/api/look/meta', json={**{k: meta[k] for k in ('person', 'image', 'features')},
                    'person': {**meta['person'], 'outfit': {'mood': ''}}}).status_code == 200
        assert web.get('/api/look').json()['person']['outfit']['mood'] == ''
        pipeline.client.json_responses = [{'assignments': [], 'suggestions': []}]
        assert web.post('/api/look/draft').status_code == 200
        context = json.loads(pipeline.client.calls[-1]['messages'][0]['content'])
        assert context['sources']['world'] and context['sources']['expression']
        assert context['sources']['model']
        assert json.loads(context['sources']['input.json'])['world']['preset'] == '和風ファンタジー'
        pipeline.client.json_responses = [package_data(), {'problems': []}, {'introduction': '確認用', 'open_now': []}]
        job = finish(web, web.post('/api/bootstrap/create', json=minimal_input().model_dump(mode='json', by_alias=True)))
        assert job['status'] == 'done', job
        pipeline.client.json_responses = [{'assignments': [], 'suggestions': []}]
        assert web.post(f"/api/bootstrap/jobs/{job['id']}/look-draft").status_code == 200
        context = json.loads(pipeline.client.calls[-1]['messages'][0]['content'])
        assert context['sources']['model'] and context['sources']['world']
        assert json.loads(context['sources']['input.json'])['basics']['name'] == minimal_input().basics.name
        assert not (root / minimal_input().id).exists()
        assert web.post('/api/bootstrap/jobs/missing/look-draft').status_code == 409
