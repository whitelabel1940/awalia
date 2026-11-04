"""ST1 candidate checks never publish settings or credentials before success."""
from cbs.llm import FakeLLMClient
from shared_helpers import api
def test_candidate_connection_uses_draft_without_changing_live_settings(pipeline, tmp_path):
    web = api(pipeline, tmp_path)
    before = pipeline.config.model_dump()
    candidate = {'llm': {'mode': 'advanced'}, 'models': {'actor': 'gemini/draft-actor'}}
    result = web.post('/api/settings/connection', json={'settings': candidate, 'keys': {'gemini': 'synthetic-draft-key'}})
    assert result.status_code == 200
    assert pipeline.config.model_dump() == before
    assert not (tmp_path / 'settings/settings.toml').exists()
    assert not (tmp_path / 'settings/secrets.env').exists()
    assert any(call['model'] == 'gemini/draft-actor' for call in pipeline.client.calls)
    assert 'synthetic-draft-key' not in (tmp_path / 'settings/connection.json').read_text()


def test_candidate_connection_failure_keeps_previous_settings_and_check(pipeline, tmp_path):
    web = api(pipeline, tmp_path)
    class Broken(FakeLLMClient):
        def generate_text(self, *args):
            raise RuntimeError('private-upstream-error')
    pipeline.client = Broken()
    before = pipeline.config.model_dump()
    path = tmp_path / 'settings/connection.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"previous": true}', encoding='utf-8')
    result = web.post('/api/settings/connection', json={'settings': {'models': {'actor': 'gemini/draft'}}})
    assert result.status_code == 422
    assert 'private-upstream-error' not in result.text
    assert pipeline.config.model_dump() == before
    assert path.read_text() == '{"previous": true}'
    assert not (tmp_path / 'settings/secrets.env').exists()


def test_candidate_routing_credentials_stay_in_memory(tmp_path, monkeypatch):
    import os
    from cbs.config import load_config
    from cbs.llm import RoutingLLMClient
    import cbs.llm.gemini
    seen = []
    def fake_client(config, *, api_key=None):
        seen.append(api_key)
        return FakeLLMClient()
    monkeypatch.setattr(cbs.llm.gemini, 'GeminiLLMClient', fake_client)
    cfg = load_config(data_dir=tmp_path)
    cfg.llm.providers.pop('gemini', None)
    before = dict(os.environ)
    router = RoutingLLMClient(cfg, credentials={'gemini': 'synthetic-memory-key'})
    assert isinstance(router._client('gemini'), FakeLLMClient)
    assert seen == ['synthetic-memory-key']
    assert router.sanitize_text('synthetic-memory-key') == '[REDACTED]'
    assert dict(os.environ) == before
    assert not (tmp_path / 'secrets.env').exists()


def test_candidate_preview_with_audio_off_does_not_enable_live_audio(pipeline, tmp_path):
    from fastapi.testclient import TestClient
    from test_m10_tts import FakeServer, FakeSpeech
    from test_web_bootstrap import app_for
    app, _ = app_for(pipeline, tmp_path)
    app.state.tts.transport, app.state.tts.server = FakeSpeech(), FakeServer()
    pipeline.config.tts.enabled = False
    with TestClient(app) as web:
        voice = web.get('/api/voice').json()['voice']
        tts = pipeline.config.tts.model_dump()
        candidate = web.post('/api/voice/candidate', json={'voice': {**voice, 'mode': 'design', 'description': '落ち着いた声'}, 'tts': tts})
        assert candidate.status_code == 200
        result = web.post('/api/voice/preview', json={'text': '声を確かめます。', 'candidate_id': candidate.json()['candidate_id']})
        assert result.status_code == 200
        assert pipeline.config.tts.enabled is False
        assert web.get('/api/voice').json()['voice'] == voice
