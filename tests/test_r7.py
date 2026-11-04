import json

import pytest
from fastapi.testclient import TestClient

from cbs.config import load_config
from cbs.telemetry import Telemetry, explanation
from cbs.web import create_app


def test_invitation_threshold_imports_partner_and_persistence(pipeline, tmp_path):
    client = TestClient(create_app(pipeline, settings_dir=tmp_path))
    store = pipeline.store
    cid = pipeline.config.character.id
    session = store.create_session(cid)
    state = lambda: client.get('/api/share/state').json()
    assert not state()['invite']
    for _ in range(10):
        store.add_turn(session['id'], 'hello', 'reply', {})
    assert not state()['invite']
    imported = store.create_session(cid)
    for _ in range(12):
        store.add_turn(imported['id'], 'old', 'reply', {})
    with store.connect() as db:
        db.execute("INSERT INTO import_batches VALUES ('batch',?,'done','{}','now','now',NULL,0)", (cid,))
        db.execute("INSERT INTO import_items(batch_id,character_id,format,source_id,content_hash,payload_json,session_id) "
                   "VALUES ('batch',?,'test','old','hash','{}',?)", (cid, imported['id']))
    store.add_turn(session['id'], '', 'proactive', {}, kind='proactive')
    other = store.create_session('other')
    store.add_turn(other['id'], 'other', 'reply', {})
    assert not state()['invite']
    store.add_turn(session['id'], 'eleventh', 'reply', {})
    assert state()['invite'] and state()['available']
    assert state()['name'] == pipeline.character.model.display_name
    # Reading / leaving the page does not consume the invitation.
    assert state()['invite']
    assert client.post('/api/share/start', json={'character_id': cid, 'action': 'invalid'}).status_code == 422
    assert state()['invite']
    for action in ['shared', 'dismissed']:
        path = tmp_path / 'share' / 'state.json'
        path.unlink(missing_ok=True)
        assert client.post('/api/share/start', json={'character_id': cid, 'action': action}).status_code == 200
        assert json.loads(path.read_text())[cid] == {'start': action}
        assert not state()['invite']
        fresh = TestClient(create_app(pipeline, settings_dir=tmp_path))
        assert not fresh.get('/api/share/state').json()['invite']
    # A record belonging to a different partner cannot suppress this partner.
    path.write_text(json.dumps({'other': {'start': 'shared'}}))
    assert state()['invite']


def test_trial_suppresses_invitation_diary_and_writes(pipeline, tmp_path):
    (tmp_path / 'first-run.json').write_text('{"version": 1, "bare": false}')
    session = pipeline.store.create_session(pipeline.config.character.id)
    for _ in range(11):
        pipeline.store.add_turn(session['id'], 'hello', 'reply', {})
    client = TestClient(create_app(pipeline, settings_dir=tmp_path))
    assert not client.get('/api/share/state').json()['available']
    assert not client.get('/api/share/state').json()['invite']
    assert not client.get('/api/note').json()['share_enabled']
    client.post('/api/share/start', json={'character_id': pipeline.config.character.id, 'action': 'shared'})
    assert not (tmp_path / 'share').exists()


@pytest.mark.parametrize('gate', ['enabled', 'consent', 'key', 'trial', 'development'])
def test_share_event_allowlist_and_gates(pipeline, tmp_path, monkeypatch, gate):
    monkeypatch.setenv('AWALIA_TELEMETRY_DEV', '1')
    cfg = load_config(data_dir=tmp_path)
    cfg.telemetry.key = 'synthetic'
    t = Telemetry(cfg, tmp_path, pipeline.store)
    t.set_enabled(True, explanation()['version'])
    if gate == 'consent':
        t.set_enabled(False, '')
    elif gate == 'key':
        cfg.telemetry.key = ''
    elif gate == 'trial':
        t.first_run = True
    elif gate == 'development':
        monkeypatch.delenv('AWALIA_TELEMETRY_DEV')
    for where in ['start', 'diary']:
        for service in ['x', 'bluesky', 'misskey', 'save', 'copy']:
            t.share(where, service)
    for where, service in [('private', 'x'), ('start', 'private')]:
        t.share(where, service)
    events = t._read('queue.jsonl', [])
    assert len(events) == (10 if gate == 'enabled' else 0)
    for event in events:
        assert event['event'] == 'share'
        assert set(event['properties']) == {'where', 'service', 'version', 'distinct_id', '$geoip_disable', '$process_person_profile'}


def test_share_event_api(pipeline, tmp_path, monkeypatch):
    monkeypatch.setenv('AWALIA_TELEMETRY_DEV', '1')
    pipeline.config.telemetry.key = 'synthetic'
    app = create_app(pipeline, settings_dir=tmp_path)
    t = app.state.telemetry
    t.set_enabled(True, explanation()['version'])
    client = TestClient(app)
    for body in [{'where': 'diary', 'service': 'save'}, {'where': 'unknown', 'service': 'x'}]:
        assert client.post('/api/telemetry/share', json=body).status_code == 200
    assert client.post('/api/telemetry/share', json={'where': 'start', 'service': 'x', 'text': 'private'}).status_code == 422
    assert client.post('/api/telemetry/share', json={'where': [], 'service': 'x'}).status_code == 422
    assert len(t._read('queue.jsonl', [])) == 1
