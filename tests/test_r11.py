"""R11: isolated appearance assets and FakeLLMClient only."""
import base64
import json
from io import BytesIO

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from cbs.look import field_catalog
from cbs.web.onboarding_api import explanation
from test_web_bootstrap import app_for


def png(colour, size=(32, 32)):
    output = BytesIO()
    Image.new('RGB', size, colour).save(output, format='PNG')
    return output.getvalue()


def upload(web, slot, colour):
    assert web.put('/api/look/' + slot, content=png(colour, (64, 36) if slot == 'cover' else (32, 32)),
                   headers={'Content-Type': 'image/png'}).status_code == 200


def test_image_fallback_and_cover_separation(pipeline, tmp_path):
    app, _ = app_for(pipeline, tmp_path)
    with TestClient(app) as web:
        assert web.get('/looks/fixture/sad').status_code == 404
        upload(web, 'shy', 'red')
        shy = web.get('/looks/fixture/shy').content
        assert web.get('/looks/fixture/neutral').content == shy
        upload(web, 'smile', 'blue')
        smile = web.get('/looks/fixture/smile').content
        assert web.get('/looks/fixture/sad').content == smile  # EXPRESSIONS order
        assert web.get('/looks/fixture/shy').content == shy
        upload(web, 'neutral', 'green')
        assert web.get('/looks/fixture/sad').content == web.get('/looks/fixture/neutral').content
        assert web.get('/looks/fixture/shy').content == shy
        assert web.get('/looks/fixture/cover').status_code == 404


@pytest.mark.parametrize('neutral,cover', [(True, True), (False, True), (False, False)])
def test_description_images_empty_only_no_save(pipeline, tmp_path, neutral, cover):
    app, root = app_for(pipeline, tmp_path)
    pipeline.config.models.vision = 'openrouter/google/gemini-3.8-flash'
    pipeline.config.photos.candidates[pipeline.config.models.vision] = True
    with TestClient(app) as web:
        upload(web, 'shy', 'red')
        upload(web, 'smile', 'blue')
        if neutral:
            upload(web, 'neutral', 'green')
        if cover:
            upload(web, 'cover', 'yellow')
        assert web.put('/api/look/meta', json={'person': {'hair': {'color': '作者の黒髪'}}}).status_code == 200
        path = root / 'fixture' / 'look.yaml'
        before = path.read_bytes()
        pipeline.client.json_responses = [{'assignments': [
            {'path': 'person.hair.color', 'value': '上書きしない'},
            {'path': 'person.face.eye_color', 'value': '青'},
            {'path': 'person.style.base', 'value': '水彩'},
            {'path': 'person.profile.height', 'value': '180cm'},
            {'path': 'person.face.habits', 'value': '微笑む'},
            {'path': 'person.expressions.smile', 'value': '笑顔'},
            {'path': 'image.scene', 'value': '背景'},
            {'path': 'invented', 'value': '無効'},
            {'path': 'person.style.base', 'value': '重複'},
            {'path': 'person.hair.length', 'value': '   '},
        ]}]
        response = web.post('/api/look/describe', json={'person': {'face': {'eye_color': '編集中の紫'}}})
        assert response.status_code == 200, response.text
        assert response.json()['assignments'] == [{'path': 'person.style.base', 'value': '水彩'}]
        assert response.json()['filled'] == 1
        assert path.read_bytes() == before
        call = pipeline.client.calls[-1]
        assert call['model'] == pipeline.config.models.vision
        parts = call['messages'][0]['content']
        context = json.loads(parts[0]['text'])
        allowed = {f['path'] for f in context['fields']}
        assert 'person.hair.color' not in allowed and 'person.face.eye_color' not in allowed
        assert all(not p.startswith(('image.', 'person.expressions.')) for p in allowed)
        assert len(parts[1:]) == (2 if cover else 1)
        images = [Image.open(BytesIO(base64.b64decode(p['image_url']['url'].split(',')[1]))) for p in parts[1:]]
        pixel = images[0].getpixel((0, 0))
        assert (pixel[1] > pixel[2]) if neutral else (pixel[2] > pixel[1])
        if cover:
            assert images[1].size == (64, 36)


def test_description_disabled_empty_and_failure(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    with TestClient(app) as web:
        pipeline.config.models.vision = None
        before = len(pipeline.client.calls)
        response = web.post('/api/look/describe')
        assert response.status_code == 409
        assert response.json()['detail'] == '写真の読み取りを使う設定にすると使えます'
        assert len(pipeline.client.calls) == before
        pipeline.config.models.vision = 'openrouter/google/gemini-3.8-flash'
        pipeline.config.photos.candidates[pipeline.config.models.vision] = True
        assert web.post('/api/look/describe').json()['filled'] == 0
        assert len(pipeline.client.calls) == before
        upload(web, 'neutral', 'red')
        pipeline.client.json_responses = [{'assignments': []}]
        assert '読み取れる見た目がありませんでした' in web.post('/api/look/describe').json()['message']
        pipeline.client.json_responses = ['invalid json']
        response = web.post('/api/look/describe')
        assert response.status_code == 502
        assert response.json()['detail'] == '下書きを作れませんでした。もう一度お試しください。'
        assert not (root / 'fixture' / 'look.yaml').exists()


def test_import_help_part(pipeline, tmp_path):
    app, _ = app_for(pipeline, tmp_path)
    with TestClient(app) as web:
        response = web.get('/api/onboarding/look_import_help')
        assert response.status_code == 200
        assert response.json()['text'].startswith('**取り込む画像の形**\n')
        assert '40MB 以下' in response.json()['text']
    assert explanation('look_import_help', tmp_path)['text'] == ''
    assert {f['path'] for f in field_catalog()['fields'] if f.get('describe') is False} == {
        'person.profile.height', 'person.profile.vibe', 'person.face.habits', 'person.must', 'person.avoid'}
