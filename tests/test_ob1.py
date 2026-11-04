from pathlib import Path
import hashlib
import tomllib

from cbs.config import load_config
from cbs.first_run import prepare
from cbs.web.onboarding_api import TITLES, explanation
from shared_helpers import api
def test_copy_missing_invalid_utf8_and_literal_html(tmp_path):
    assert explanation('ai', tmp_path)['text'] == '# AI について'
    (tmp_path / '01_ai.md').write_bytes(b'\xff')
    assert explanation('ai', tmp_path)['text'] == '# AI について'
    raw = '# 見出し\n\n{name} <script>alert(1)</script> **強調**\n'.encode()
    (tmp_path / '01_ai.md').write_bytes(raw)
    assert explanation('ai', tmp_path) == {'text': raw.decode(), 'version': hashlib.sha256(raw).hexdigest()}
    for screen in TITLES:
        copy = explanation(screen)
        assert (screen in ('consent', 'done') or copy['text'].startswith('# ')) and '???' not in copy['text']


def test_consent_saved_once_server_timestamp_and_version(pipeline, tmp_path):
    client = api(pipeline, tmp_path)
    assert client.get('/api/onboarding/state').json() == {'consent': None}
    copy = client.get('/api/onboarding/consent').json()
    assert client.post('/api/onboarding/consent', json={'version': 'stale'}).status_code == 409
    value = client.post('/api/onboarding/consent', json={'version': copy['version']}).json()
    assert value['consent']['at'].endswith('+00:00')
    assert client.post('/api/onboarding/consent', json={'version': copy['version']}).json() == value
    assert client.get('/api/onboarding/state').json() == value
    saved = tomllib.loads((tmp_path / 'settings/settings.toml').read_text(encoding='utf-8'))
    assert saved['onboarding']['consent'] == value['consent']
    assert not pipeline.client.calls
    assert client.get('/api/onboarding/unknown').status_code == 404


def test_trial_does_not_inherit_consent(tmp_path):
    (tmp_path / 'settings.toml').write_text('[onboarding.consent]\nat="old"\nversion="old"\n', encoding='utf-8')
    trial = prepare(tmp_path)
    assert 'onboarding' not in tomllib.loads((trial / 'settings.toml').read_text(encoding='utf-8'))
    assert 'onboarding' in tomllib.loads((tmp_path / 'settings.toml').read_text(encoding='utf-8'))


def test_presets_and_actor_choices_exposed(pipeline, tmp_path):
    cfg = load_config(data_dir=tmp_path)
    presets = {p.id: p.models for p in cfg.advanced.presets}
    direct = presets['recommended_direct']
    assert direct.actor == 'gemini/gemini-3.8-flash'
    assert direct.judge == direct.steward == 'openai/gpt-6-luna'
    assert {m.split('/')[0] for m in direct.model_dump().values()} == {'gemini', 'openai'}
    assert len(cfg.onboarding.actor_choices['openai']) == 1
    assert len(cfg.onboarding.actor_choices['gemini']) == 3
    assert len(cfg.onboarding.actor_choices['claude']) == 2
    data = api(pipeline, tmp_path).get('/api/settings').json()
    assert data['onboarding'] == cfg.onboarding.model_dump()


def test_direct_preset_roundtrip_and_connection_use_fake_only(pipeline, tmp_path, monkeypatch):
    client = api(pipeline, tmp_path)
    direct = next(p.models.model_dump() for p in pipeline.config.advanced.presets if p.id == 'recommended_direct')
    monkeypatch.setenv('GEMINI_API_KEY', 'ob1-fake-gemini')
    monkeypatch.setenv('GPT_API_KEY', 'ob1-fake-openai')
    response = client.put('/api/settings', json={'llm': {'mode': 'advanced'}, 'models': direct})
    assert response.status_code == 200, response.text
    assert response.json()['models'] == direct
    assert client.post('/api/settings/connection').json()['status'] == 'connected'
    assert len(pipeline.client.calls) == 3
    assert client.post('/api/setup/voice-step', json={'skipped': True}).status_code == 200
    assert client.get('/api/settings').json()['voice_setup_done']
    saved = load_config(data_dir=tmp_path / 'settings')
    assert saved.models.model_dump() == direct
