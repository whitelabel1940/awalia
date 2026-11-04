"""Candidate auditions never apply voice/config; every speech boundary is offline."""
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cbs.tts import read_voice
from test_m10_tts import FakeServer, FakeSpeech
from test_m10b_voice_settings import recording
from test_web_bootstrap import app_for


@pytest.mark.parametrize('prefix', ['/api/voice', '/api/setup/voice'])
def test_candidate_design_preview_sample_and_apply(pipeline, tmp_path, prefix):
    app, _ = app_for(pipeline, tmp_path)
    service = app.state.tts
    service.transport, service.server = FakeSpeech(), FakeServer()
    with TestClient(app) as client:
        original = client.get(prefix).json()['voice']
        tts = pipeline.config.tts.model_dump()
        settings_path = pipeline.store.path.parent / 'settings.toml'
        original_settings = settings_path.read_bytes() if settings_path.exists() else None
        candidate_voice = {**original, 'description': '候補の低く落ち着いた声', 'mode': 'design'}
        payload = {'voice': candidate_voice, 'tts': {**tts, 'enabled': True}}
        result = client.post(prefix + '/candidate', json=payload)
        assert result.status_code == 200, result.text
        token = result.json()['candidate_id']
        preview = {'text': '候補の声を聞きます。', 'candidate_id': token}
        assert client.post(prefix + '/preview', json=preview).status_code == 200
        assert client.post(prefix + '/preview', json=preview).headers['X-Voice-Sample'] == 'ready'
        assert [p['voice'] for _, p in service.transport.calls] == ['none', 'partner', 'partner']
        assert client.get(prefix).json()['voice'] == original
        assert pipeline.config.tts.model_dump() == tts
        assert (settings_path.read_bytes() if settings_path.exists() else None) == original_settings

        sample = client.post(prefix + '/sample', json={'candidate_id': token})
        assert sample.status_code == 200
        sample_name = sample.json()['voice']['sample_reference']
        service.transport.fail = True
        assert client.post(prefix + '/sample', json={'candidate_id': token}).status_code == 503
        service.transport.fail = False
        assert client.get(prefix).json()['voice'] == original
        # Apply requires the same engine settings used for the audition.
        assert client.put(prefix, json={**candidate_voice, 'candidate_id': token}).status_code == 409
        assert client.put('/api/settings', json={'tts': payload['tts']}).status_code == 200
        result = client.put(prefix, json={**candidate_voice, 'candidate_id': token})
        assert result.status_code == 200, result.text
        assert result.json()['voice']['sample_reference'] == sample_name
        # Resolve from the candidate's copied WAV rather than relying on a real tree.
        copies = list(tmp_path.rglob(sample_name))
        assert len(copies) == 2 and len({hashlib.sha256(p.read_bytes()).hexdigest() for p in copies}) == 1
        before = len(service.transport.calls)
        assert client.post(prefix + '/preview', json={'text': '決めた声です。'}).status_code == 200
        assert len(service.transport.calls) == before + 1
        candidate_root = app.state.voice_candidates.name
    assert not Path(candidate_root).exists()


def test_candidate_clone_uses_pending_list_without_applying(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    service = app.state.tts
    service.transport, service.server = FakeSpeech(), FakeServer()
    with TestClient(app) as client:
        original = client.get('/api/voice').json()['voice']
        client.delete('/api/voice/references')
        clip = client.put('/api/voice/reference', content=recording(2)).json()['uploaded']
        tts = {**pipeline.config.tts.model_dump(), 'enabled': True}
        payload = {'voice': {**original, 'mode': 'clone'}, 'tts': tts}
        result = client.post('/api/voice/candidate', json=payload)
        assert result.status_code == 200, result.text
        token = result.json()['candidate_id']
        assert not service.server.starts  # No synthesis until preview is requested.
        assert client.post('/api/voice/preview', json={'candidate_id': token}).status_code == 200
        assert service.server.starts[-1][1].reference == 'fake-clone.gguf'
        assert (service.server.starts[-1][2] / 'voice' / clip).is_file()
        assert read_voice(root / 'fixture').model_dump() == original
        client.delete('/api/voice/references')
        # A prepared audition remains playable, but preparing the empty list is rejected.
        assert client.post('/api/voice/preview', json={'candidate_id': token}).status_code == 200
        assert client.post('/api/voice/candidate', json={**payload, 'candidate_id': token}).status_code == 422
        assert client.post('/api/setup/voice/preview', json={'candidate_id': token}).status_code == 404
        assert client.post('/api/voice/preview', json={'candidate_id': '../unsafe'}).status_code == 422
        assert client.put('/api/settings', json={'tts': tts}).status_code == 200
        assert client.put('/api/voice', json={**payload['voice'], 'candidate_id': token}).status_code == 200
        assert read_voice(root / 'fixture').reference == 'fake-clone.gguf'
        assert (root / 'fixture/voice/fake-clone.gguf').is_file()


def test_candidate_external_settings_and_changed_description(pipeline, tmp_path):
    app, _ = app_for(pipeline, tmp_path)
    service = app.state.tts
    service.transport, service.server = FakeSpeech(), FakeServer()
    with TestClient(app) as client:
        original = client.get('/api/voice').json()['voice']
        tts = pipeline.config.tts.model_dump()
        candidate = {**tts, 'enabled': True, 'engine': 'external', 'base_url': 'http://candidate.invalid/v1', 'model': 'candidate-model'}
        candidate['styles'] = {**candidate['styles'], 'external': {'mode': 'instructions', 'values': {'sad': '静かに'}}}
        voice = {**original, 'mode': 'design', 'description': '最初の説明', 'external_voice': 'candidate-voice'}
        result = client.post('/api/voice/candidate', json={'voice': voice, 'tts': candidate}).json()
        token = result['candidate_id']
        for description in ('最初の説明', '変えた説明'):
            voice['description'] = description
            assert client.post('/api/voice/candidate', json={'voice': voice, 'tts': candidate, 'candidate_id': token}).status_code == 200
            assert client.post('/api/voice/preview', json={'candidate_id': token, 'expression': 'sad'}).status_code == 200
            url, request = service.transport.calls[-1]
            assert url == candidate['base_url'] and request['model'] == 'candidate-model'
            assert request['instructions'] == description + '\n静かに'
            assert request['voice'] == 'candidate-voice'
        assert not service.server.starts
        assert pipeline.config.tts.model_dump() == tts
        assert client.get('/api/voice').json()['voice'] == original
