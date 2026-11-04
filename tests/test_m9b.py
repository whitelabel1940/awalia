"""Small route/public projection tests; provider construction is forbidden by conftest."""
from fastapi.testclient import TestClient

from cbs.web import create_app
from test_web_bootstrap import app_for
from cbs.llm import FakeLLMClient
from test_bootstrap import minimal_input, package_data
from test_web_bootstrap import finish


def test_user_inspector_wizard_and_module_assets(pipeline, tmp_path):
    app, _ = app_for(pipeline, tmp_path)
    with TestClient(app) as client:
        user = client.get('/')
        assert user.status_code == 200
        assert 'type="module" src="/static/user.js"' in user.text
        assert 'Inspector' not in user.text
        assert 'id="composer"' in user.text
        dev = client.get('/dev')
        assert dev.status_code == 200 and 'INSPECTOR' in dev.text
        assert '/static/app.js' in dev.text
        wizard = client.get('/wizard')
        assert wizard.status_code == 200
        assert 'type="module" src="/static/wizard.js"' in wizard.text
        for name in ['theme.js', 'ui.js', 'user.js', 'settings.js', 'look-editor.js', 'wizard.js', 'screens.css', 'app.js', 'style.css']:
            response = client.get('/static/' + name)
            assert response.status_code == 200
            assert ('javascript' if name.endswith('.js') else 'text/css') in response.headers['content-type']
        assert client.get('/static/missing.js').status_code == 404
        assert client.get('/api/partner').json() == {'partner': {'id': 'fixture', 'name': pipeline.character.model.display_name}}
        assert client.get('/api/partner?character_id=../fixture').status_code == 404


def test_no_published_partner_has_public_empty_state(pipeline, tmp_path):
    root = tmp_path / 'empty'
    root.mkdir()
    with TestClient(create_app(pipeline, characters_root=root)) as client:
        assert client.get('/').status_code == 200
        assert client.get('/wizard').status_code == 200
        assert client.get('/api/partner').json() == {'partner': None}


def test_look_prompt_uses_unpublished_world_without_publishing(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    with TestClient(app) as client:
        response = client.post('/api/look/prompt?character_id=not_yet_created', json={
            'features': '銀色の髪', 'free_text': '旅人',
            'world': {'preset': '西洋ファンタジー', 'region': '北の王国', 'being': '精霊', 'contact': 'C'}})
        assert response.status_code == 200
        prompt = response.json()['prompt']
        assert all(text in prompt for text in ['銀色の髪', '横4列×縦2行'])
        assert '旅人' not in prompt and '北の王国' not in prompt
        assert not (root / 'not_yet_created').exists()
        assert client.post('/api/look/prompt', json={'world': {'contact': 'invalid'}}).status_code == 422


def test_first_partner_boots_without_character_files_and_survives_restart(tmp_path, monkeypatch):
    client = FakeLLMClient(json_responses=[package_data(), {'problems': []},
                                         {'introduction': '初めて迎える人。', 'open_now': []}])
    monkeypatch.setattr('cbs.llm.create_client', lambda config: client)
    root = tmp_path / 'characters'
    options = dict(characters_root=root, db_path=tmp_path / 'runtime.sqlite3',
                   work_root=tmp_path / 'work', settings_dir=tmp_path / 'settings')
    app = create_app(**options)
    with TestClient(app) as web:
        assert web.get('/api/partner').json() == {'partner': None}
        assert web.get('/api/characters').json() == []
        assert web.get('/wizard').status_code == 200
        assert web.post('/api/look/prompt', json={'world': {'preset': '西洋ファンタジー'}}).status_code == 200
        job = finish(web, web.post('/api/bootstrap/create', json=minimal_input().model_dump(mode='json', by_alias=True)))
        assert job['status'] == 'done', job
        assert web.post(f"/api/bootstrap/jobs/{job['id']}/confirm").status_code == 200
        assert web.get('/api/partner').json()['partner'] == {'id': 'test_character', 'name': 'Test'}
        assert web.get('/api/note').json()['name'] == 'Test'
        assert web.put('/api/persona', json={'ai_question': 'honest'}).status_code == 200
        assert web.get('/api/persona').json()['ai_question'] == 'honest'
        session = web.post('/api/sessions', json={}).json()
        assert web.get(f"/api/sessions/{session['id']}/turns").status_code == 200
    with TestClient(create_app(**options)) as web:
        assert web.get('/api/partner').json()['partner']['id'] == 'test_character'
