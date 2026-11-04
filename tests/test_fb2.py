"""Partner persona settings: offline, isolated roots, and lazy migration only."""
import json
import tomllib

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
import yaml

from cbs.bootstrap_schema import BootstrapInput
from cbs.character import effective_persona, load_character, write_persona
from cbs.config import PersonaConfig
from cbs.llm import FakeLLMClient
from cbs.runtime import read_prompt
from cbs.web import create_app
from test_bootstrap import bootstrap, minimal_input, package_data
from test_web_bootstrap import app_for, finish


def partner_files(root, character, cid):
    directory = root / cid
    directory.mkdir(parents=True)
    model = character.model.model_dump(mode='json')
    model['character_id'] = cid
    for name, value in {'model': model, 'expression': character.expression.model_dump(mode='json', by_alias=True)}.items():
        (directory / f'{name}.yaml').write_text(yaml.safe_dump(value, allow_unicode=True), encoding='utf-8')
    return directory


@pytest.mark.parametrize('value', ['immersive', 'playful', 'honest'])
def test_missing_persona_migrates_on_save_only(tmp_path, character, pipeline, value):
    directory = partner_files(tmp_path, character, 'legacy')
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    pipeline.config.persona.ai_question = value
    loaded = load_character('legacy', root=tmp_path)
    assert loaded.persona is None
    assert effective_persona(loaded, pipeline.config).ai_question == value
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    write_persona(directory, effective_persona(loaded, pipeline.config))
    pipeline.config.persona.ai_question = 'playful' if value != 'playful' else 'honest'
    assert effective_persona(load_character('legacy', root=tmp_path), pipeline.config).ai_question == value
    assert not (directory / 'persona.yaml.tmp').exists()
    assert all((directory / name).read_bytes() == content for name, content in before.items())


def test_persona_api_is_scoped_persistent_and_not_global(pipeline, tmp_path):
    root = tmp_path / 'characters'
    first = partner_files(root, pipeline.character, 'fixture')
    second = partner_files(root, pipeline.character, 'other')
    pipeline.config.persona.ai_question = 'playful'
    settings = tmp_path / 'settings'
    app = create_app(pipeline, characters_root=root, work_root=tmp_path / 'work', settings_dir=settings)
    with TestClient(app) as client:
        assert client.get('/api/persona').json() == {'ai_question': 'playful'}
        assert client.get('/api/persona?character_id=other').json() == {'ai_question': 'playful'}
        assert not (first / 'persona.yaml').exists() and not (second / 'persona.yaml').exists()
        assert client.put('/api/settings', json={'persona': {'ai_question': 'honest'}}).status_code == 422
        assert 'persona' not in client.get('/api/settings').json()
        for invalid in ({}, {'ai_question': 'unknown'}, {'ai_question': None}, {'ai_question': 'honest', 'extra': True}):
            assert client.put('/api/persona', json=invalid).status_code == 422
        assert not (first / 'persona.yaml').exists()
        assert client.put('/api/persona?character_id=other', json={'ai_question': 'honest'}).json() == {'ai_question': 'honest'}
        assert client.get('/api/persona').json()['ai_question'] == 'playful'
        assert pipeline.config.persona.ai_question == 'playful'
        assert not (settings / 'settings.toml').exists()
        assert not (first / 'persona.yaml').exists()
        assert client.get('/api/persona?character_id=missing').status_code == 404
        # A global setting refresh must preserve partner overrides in loaded pipelines.
        assert client.put('/api/settings', json={'inner': {'show_monologue': False}}).status_code == 200
        saved_global = (settings / 'settings.toml').read_bytes()
        assert client.get('/api/persona?character_id=other').json()['ai_question'] == 'honest'
        assert client.put('/api/persona', json={'ai_question': 'immersive'}).status_code == 200
        assert (settings / 'settings.toml').read_bytes() == saved_global
        assert client.get('/api/persona?character_id=other').json()['ai_question'] == 'honest'
        pipeline.client.calls.clear()
        session = client.post('/api/sessions', json={}).json()
        reply = client.post(f"/api/sessions/{session['id']}/messages", json={'content': 'AI?'}).json()
        trace = pipeline.store.get_trace(reply['turn_id'])
        assert read_prompt('ai_question_immersive') in trace['Actor']['request']['system']
        assert json.loads(trace['Check']['request']['messages'][0]['content'])['ai_question'] == 'immersive'
    assert load_character('fixture', root=root).persona.ai_question == 'immersive'
    assert load_character('other', root=root).persona.ai_question == 'honest'


@pytest.mark.parametrize('value', ['immersive', 'playful', 'honest'])
@pytest.mark.parametrize('mode', ['sync', 'async'])
def test_actor_and_check_use_partner_value_including_rewrite(pipeline, value, mode):
    pipeline.character.persona = PersonaConfig(ai_question=value)
    pipeline.config.persona.ai_question = 'playful' if value != 'playful' else 'honest'
    pipeline.config.pipeline.post_check = mode
    if mode == 'sync':
        pipeline.client.json_responses = [{},
            {'problems': [{'text': 'Test reply.', 'verdict': 'contradicted', 'reason': 'test rewrite'}]},
            {'problems': []}]
        pipeline.client.text_responses = ['Test reply.', 'Rewritten reply.']
    sid = pipeline.store.create_session('fixture')['id']
    reply = pipeline.run(sid, 'AI?')
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(reply['turn_id'])
    for step in ('Actor', 'Rewrite') if mode == 'sync' else ('Actor',):
        assert read_prompt(f'ai_question_{value}') in trace[step]['request']['system']
    for step in ('Check', 'Check-2') if mode == 'sync' else ('Check',):
        assert json.loads(trace[step]['request']['messages'][0]['content'])['ai_question'] == value


@pytest.mark.parametrize('value', ['immersive', 'playful', 'honest'])
def test_reader_review_and_profile_read_existing_partner_persona(tmp_path, character, value):
    fake = FakeLLMClient(json_responses=[{}, {}])
    engine = bootstrap(tmp_path, fake)
    directory = partner_files(engine.characters_root, character, 'legacy')
    write_persona(directory, PersonaConfig(ai_question=value))
    engine.config.persona.ai_question = 'playful' if value != 'playful' else 'honest'
    engine.plan(BootstrapInput(id='legacy', basics={'name': 'Legacy'}, text='A character.'))
    engine.profile('legacy', 'A character.')
    assert len(fake.calls) == 4
    assert all(json.loads(call['messages'][0]['content'])['ai_question'] == value for call in fake.calls)
    (directory / 'persona.yaml').unlink()
    fake.calls.clear()
    fake.json_responses = [{}]
    engine.plan(BootstrapInput(id='legacy', basics={'name': 'Legacy'}, text='A character.'))
    assert all(json.loads(call['messages'][0]['content'])['ai_question'] == engine.config.persona.ai_question for call in fake.calls)
    assert not (directory / 'persona.yaml').exists()


@pytest.mark.parametrize('value', [None, 'immersive', 'playful', 'honest'])
def test_create_and_confirm_save_only_new_partner_persona(pipeline, tmp_path, value):
    app, root = app_for(pipeline, tmp_path)
    pipeline.config.persona.ai_question = 'honest'
    data = minimal_input()
    body = data.model_dump(mode='json', by_alias=True) if value is None else {
        **minimal_input().model_dump(mode='json', by_alias=True), 'persona': {'ai_question': value}}
    pipeline.client = FakeLLMClient(json_responses=[package_data(), {'problems': []}, {'introduction': 'Intro', 'open_now': []}])
    with TestClient(app) as client:
        job = finish(client, client.post('/api/bootstrap/create', json=body))
        assert job['status'] == 'done', job
        draft = tmp_path / 'work' / 'drafts' / job['id'] / data.id
        expected = value or 'immersive'
        assert load_character(data.id, root=draft.parent).persona.ai_question == expected
        assert not (root / data.id).exists()
        assert client.post(f"/api/bootstrap/jobs/{job['id']}/confirm").json() == {'id': data.id}
        assert not draft.exists()
        assert client.get(f'/api/persona?character_id={data.id}').json() == {'ai_question': expected}
        assert client.post(f"/api/bootstrap/jobs/{job['id']}/confirm").status_code == 200
        assert client.get('/api/persona?character_id=fixture').json() == {'ai_question': 'honest'}
    assert not (root / 'fixture' / 'persona.yaml').exists()
    # Only the newly welcomed partner is remembered (no global persona written).
    assert tomllib.loads((pipeline.store.path.parent / 'settings.toml').read_text(encoding='utf-8')) == {'character': {'id': data.id}}
    assert load_character(data.id, root=root).persona.ai_question == expected


def test_new_partner_default_reader_is_independent_of_global(tmp_path):
    fake = FakeLLMClient()
    engine = bootstrap(tmp_path, fake)
    engine.config.persona.ai_question = 'honest'
    engine.plan(BootstrapInput(id='new_partner', basics={'name': 'New'}, text='A character.'))
    assert all(json.loads(call['messages'][0]['content'])['ai_question'] == 'immersive' for call in fake.calls)
    with pytest.raises(ValidationError):
        BootstrapInput(id='new_partner', basics={'name': 'New'}, persona={'ai_question': 'invalid'})


@pytest.mark.parametrize('value', ['immersive', 'playful', 'honest'])
def test_profile_api_passes_selected_value_without_saving(pipeline, tmp_path, value):
    app, root = app_for(pipeline, tmp_path)
    pipeline.config.persona.ai_question = 'playful' if value != 'playful' else 'honest'
    pipeline.client = FakeLLMClient(json_responses=[{}])
    with TestClient(app) as client:
        job = finish(client, client.post('/api/bootstrap/read', json={
            'id': 'new_partner', 'text': 'A character.', 'persona': {'ai_question': value}}))
        assert job['status'] == 'done', job
    assert all(json.loads(call['messages'][0]['content'])['ai_question'] == value for call in pipeline.client.calls)
    assert not (root / 'new_partner').exists()
    assert not (root / 'fixture' / 'persona.yaml').exists()
