import asyncio
from datetime import timedelta
import json
from pathlib import Path
import re
import tomllib
from uuid import UUID
import zipfile

from fastapi.testclient import TestClient
import httpx
import pytest

from cbs import clock
from cbs.config import load_config
from cbs.telemetry import ONBOARDING_STEPS, Telemetry, explanation, send_batch


@pytest.fixture
def telemetry(tmp_path, store, monkeypatch):
    monkeypatch.setenv('AWALIA_TELEMETRY_DEV', '1')
    config = load_config(data_dir=tmp_path)
    config.telemetry.key = 'synthetic-transport-credential'
    value = Telemetry(config, tmp_path, store)
    with clock.at('2026-10-01T12:00:00+09:00'):
        value.set_enabled(True, explanation()['version'])
    return value


def queued(value):
    return value._read('queue.jsonl', [])


@pytest.mark.parametrize('gate', ['consent', 'key', 'trial', 'development'])
def test_no_collection_under_each_gate(telemetry, monkeypatch, gate):
    t = telemetry
    if gate == 'consent':
        t.set_enabled(False, '')
    elif gate == 'key':
        t.config.telemetry.key = ''
    elif gate == 'trial':
        t.first_run = True
    else:
        monkeypatch.delenv('AWALIA_TELEMETRY_DEV')
    before = sorted(p.name for p in t.root.glob('*'))
    async def forbidden(*args):
        pytest.fail('must not send')
    monkeypatch.setattr('cbs.telemetry.send_batch', forbidden)
    t.app_started()
    t.step('ai', True)
    t.error('server', RuntimeError('private'))
    t.daily_turns()
    asyncio.run(t.flush())
    assert not queued(t)
    assert before == sorted(p.name for p in t.root.glob('*'))


def test_old_users_and_disabled_start_create_nothing(tmp_path, store, monkeypatch):
    monkeypatch.setenv('AWALIA_TELEMETRY_DEV', '1')
    cfg = load_config(data_dir=tmp_path)
    cfg.telemetry.key = 'synthetic'
    t = Telemetry(cfg, tmp_path, store)
    t.app_started()
    t.step('welcome', True)
    asyncio.run(t.flush())
    assert not t.snapshot()['enabled']
    assert not t.root.exists()


def test_fixed_fields_and_persistent_dedup(telemetry, monkeypatch):
    t = telemetry
    monkeypatch.setattr('cbs.telemetry.platform.system', lambda: 'Windows')
    monkeypatch.setattr('cbs.telemetry.platform.version', lambda: '10.0.26200')
    t.config.models.actor = 'gemini/gemini-3-flash'
    t.config.models.judge = 'anthropic/has spaces and private text'
    t.config.models.vision = 'gemini/' + 'x' * 81
    t.config.models.stt = 'local/valid._:/-'
    t.config.models.research = 'gemini/' + t.config.telemetry.key
    with clock.at('2026-11-04T10:00:00+09:00'):
        t.app_started()
        t.app_started()
        t.step('ai', True)
        t.step('not-a-screen', True)
        t.error('bootstrap', ValueError('sensitive message'), 'building')
        t.error('bootstrap', ValueError('another'), 'reading')
        t.error('unknown', RuntimeError('private'))
        t.error('voice', type('bad name', (Exception,), {})())
        # A restart retains the daily suppression independently of sent.jsonl retention.
        again = Telemetry(t.config, t.root.parent, t.store)
        again.app_started()
        again.error('bootstrap', ValueError(), 'saving')
    events = queued(t)
    assert [e['event'] for e in events] == ['app_started', 'onboarding_step', 'error', 'error']
    common = {'version', '$process_person_profile', '$geoip_disable', 'distinct_id'}
    expected = [{'os', 'ai'}, {'step', 'first'}, {'where', 'type', 'stage'}, {'where', 'type'}]
    for event, fields in zip(events, expected):
        assert set(event) == {'event', 'timestamp', 'properties'}
        assert set(event['properties']) == common | fields
        assert event['properties']['$process_person_profile'] is False
        assert event['properties']['$geoip_disable'] is True
    props = events[0]['properties']
    assert props['os'] == 'Windows 10.0.26200'
    assert props['ai']['actor'] == 'gemini/gemini-3-flash'
    assert props['ai']['judge'] == 'anthropic/other'
    assert props['ai']['vision'] == 'gemini/other'
    assert props['ai']['research'] == 'gemini/other'
    assert events[-1]['properties']['type'] == 'other'
    assert 'sensitive' not in json.dumps(events)
    with clock.at('2026-11-05T10:00:00+09:00'):
        t.app_started()
        t.error('bootstrap', ValueError(), 'building')
    assert len(queued(t)) == 6


def test_daily_bounds_all_partners_no_zero_and_no_repeats(telemetry, monkeypatch):
    t = telemetry
    session1 = t.store.create_session('one', 'private')
    session2 = t.store.create_session('two', 'private')
    with clock.at('2026-10-27T12:00:00+09:00'):
        t.set_enabled(False, '')
        t.set_enabled(True, explanation()['version'])
    for offset, session in [(0, session1), (1, session1), (2, session1), (2, session2), (8, session1), (9, session1)]:
        date = f'2026-10-{27 + offset:02d}' if offset < 5 else f'2026-11-{offset - 4:02d}'
        with clock.at(date + 'T12:00:00+09:00'):
            t.store.add_turn(session['id'], 'private text', 'reply', {})
            t.store.add_turn(session['id'], '', 'autonomous', {}, kind='proactive')
    # Nov 5: Oct 28 is older than seven days; Oct 29 has two turns across partners.
    batches = []
    async def fake(host, key, batch):
        batches.extend(batch)
    monkeypatch.setattr('cbs.telemetry.send_batch', fake)
    with clock.at('2026-11-05T12:00:00+09:00'):
        asyncio.run(t.flush())
        asyncio.run(t.flush())
    props = [e['properties'] for e in batches]
    assert [(p['date'], p['turns']) for p in props] == [('2026-10-29', 2), ('2026-11-04', 1)]
    assert set(props[0]) == {'date', 'turns', 'version', '$process_person_profile', '$geoip_disable', 'distinct_id'}


def test_consent_day_excluded_rotation_and_retention(telemetry, monkeypatch):
    t = telemetry
    original = (t.root / 'id').read_text()
    assert UUID(original).version == 4
    t.step('ai', False)
    t.set_enabled(False, '')
    assert all(not (t.root / name).exists() for name in ['id', 'queue.jsonl', 'state.json'])
    with clock.at('2026-11-04T10:00:00+09:00'):
        t.set_enabled(True, explanation()['version'])
        session = t.store.create_session('one', 'private')
        t.store.add_turn(session['id'], 'private', 'reply', {})
    assert (t.root / 'id').read_text() != original
    with clock.at('2026-11-05T10:00:00+09:00'):
        t.daily_turns()
    assert not queued(t)
    consent = tomllib.loads(t.settings_path.read_text(encoding='utf-8'))['telemetry']
    assert consent == t.consent and consent['version'] == explanation()['version']


def test_retry_expiry_and_latest_200(telemetry, monkeypatch, caplog):
    t = telemetry
    with clock.at('2026-10-01T10:00:00+09:00'):
        t.step('welcome', True)
    with clock.at('2026-11-04T10:00:00+09:00'):
        t.step('ai', True)
        async def failed(*args):
            raise httpx.ConnectError(t.config.telemetry.key)
        monkeypatch.setattr('cbs.telemetry.send_batch', failed)
        asyncio.run(t.flush())
        assert [e['properties']['step'] for e in queued(t)] == ['ai']
        calls = []
        async def fake(host, key, batch):
            calls.append(batch)
        monkeypatch.setattr('cbs.telemetry.send_batch', fake)
        for _ in range(205):
            t.step('partner', False)
        asyncio.run(t.flush())
        assert not queued(t) and len(calls) == 1
        assert len(t.sent()) == 200
        assert t.sent()[0]['properties']['step'] == 'partner'
    assert t.config.telemetry.key not in (t.root / 'sent.jsonl').read_text(encoding='utf-8')
    assert t.config.telemetry.key not in caplog.text


def test_api_and_server_failure(telemetry, pipeline, tmp_path, monkeypatch):
    from cbs.web import create_app
    pipeline.config.telemetry.key = telemetry.config.telemetry.key
    app = create_app(pipeline, settings_dir=tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    snapshot = client.get('/api/telemetry').json()
    assert snapshot['available'] and snapshot['enabled']
    assert client.post('/api/telemetry', json={'enabled': True, 'version': 'old'}).status_code == 409
    assert client.post('/api/telemetry/step', json={'step': 'anything', 'first': True}).status_code == 200
    assert client.post('/api/telemetry/step', json={'step': 'ai', 'first': False}).status_code == 200
    assert client.post('/api/telemetry/step', json={'step': 'ai', 'first': False, 'text': 'private'}).status_code == 422
    for path, body in [('/api/telemetry', {'enabled': telemetry.config.telemetry.key}),
                       ('/api/telemetry/step', {'step': 'ai', 'first': telemetry.config.telemetry.key}),
                       ('/api/telemetry', {'enabled': False, 'key': telemetry.config.telemetry.key})]:
        response = client.post(path, json=body)
        assert response.status_code == 422
        assert telemetry.config.telemetry.key not in response.text
    @app.get('/api/r6-failure')
    def fail():
        raise LookupError('private message')
    assert client.get('/api/r6-failure').status_code == 500
    assert queued(app.state.telemetry)[-1]['properties']['type'] == 'LookupError'
    for path in ['/api/telemetry', '/api/telemetry/sent', '/api/settings']:
        assert telemetry.config.telemetry.key not in client.get(path).text
    assert client.post('/api/telemetry', json={'enabled': False}).json()['enabled'] is False


def test_transport_contract_with_mock_transport(monkeypatch):
    original = httpx.AsyncClient
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200)
    def fake_client(**kwargs):
        assert kwargs == {'timeout': 4, 'follow_redirects': False}
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', fake_client)
    # The imported original boundary itself is tested only over a fake HTTP transport.
    asyncio.run(send_batch('https://eu.i.posthog.com', 'synthetic', [{'event': 'app_started'}]))
    assert str(seen[0].url) == 'https://eu.i.posthog.com/batch/'
    assert json.loads(seen[0].content) == {'api_key': 'synthetic', 'batch': [{'event': 'app_started'}]}


def test_steps_match_client_list():
    source = Path('cbs/web/static/onboarding.js').read_text(encoding='utf-8')
    match = re.search(r'ONBOARDING_STEPS = `([^`]+)`', source)
    assert set(match[1].split()) == ONBOARDING_STEPS


def test_startup_then_hourly_background_schedule(telemetry, monkeypatch):
    t = telemetry
    sent = []
    waits = []
    async def fake(host, key, batch):
        sent.extend(batch)
    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 1:
            t.step('ai', True)
        else:
            raise asyncio.CancelledError()
    monkeypatch.setattr('cbs.telemetry.send_batch', fake)
    monkeypatch.setattr('cbs.telemetry.asyncio.sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(t.run())
    assert waits == [3600, 3600]
    assert [e['event'] for e in sent] == ['app_started', 'onboarding_step']


def test_opt_out_during_network_does_not_block_or_touch_new_id(telemetry, monkeypatch):
    t = telemetry
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def fake(*args):
            started.set()
            await release.wait()
        monkeypatch.setattr('cbs.telemetry.send_batch', fake)
        t.step('ai', True)
        task = asyncio.create_task(t.flush())
        await started.wait()
        # These operations finish even while the transport is still pending.
        t.set_enabled(False, '')
        t.set_enabled(True, explanation()['version'])
        t.step('partner', False)
        release.set()
        await task
    asyncio.run(scenario())
    assert not t.sent()
    assert [e['properties']['step'] for e in queued(t)] == ['partner']


def test_reply_and_import_failures_are_captured(telemetry, pipeline, tmp_path, monkeypatch):
    from cbs.web import create_app
    pipeline.config.telemetry.key = telemetry.config.telemetry.key
    app = create_app(pipeline, settings_dir=tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    def fail(*args, **kwargs):
        raise OSError('private failure')
    monkeypatch.setattr(pipeline.client, 'generate_text', fail)
    session = pipeline.store.create_session(pipeline.config.character.id, 'private')
    assert client.post(f"/api/sessions/{session['id']}/messages", json={'content': 'test'}).status_code == 502
    response = client.post('/api/import/preview', json={'filename': 'bad.json', 'payload': '!invalid!'})
    assert response.status_code == 422
    places = {e['properties']['where'] for e in queued(app.state.telemetry)}
    assert {'reply', 'import'} <= places


def test_backup_excludes_id_and_queue(telemetry, pipeline, tmp_path):
    from cbs import backup
    t = telemetry
    # An actual BK1 export explores the same storage root containing telemetry.
    t.step('ai', True)
    out = tmp_path / 'r6.zip'
    backup.build_export(pipeline.store.path, pipeline.characters_root, tmp_path, pipeline.config.character.id, out)
    with zipfile.ZipFile(out) as archive:
        assert not any('telemetry/' in name for name in archive.namelist())
        assert (t.root / 'id').read_bytes() not in b''.join(archive.read(name) for name in archive.namelist())
    # Consent settings without their machine-local ID do not display a misleading on switch.
    restored = tmp_path / 'restored'
    restored.mkdir()
    (restored / 'settings.toml').write_bytes(t.settings_path.read_bytes())
    other = Telemetry(t.config, restored, pipeline.store)
    assert not other.snapshot()['enabled']
    other.step('ai', True)
    assert not other.root.exists()
    other.set_enabled(True, explanation()['version'])
    assert (other.root / 'id').read_text() != (t.root / 'id').read_text()
