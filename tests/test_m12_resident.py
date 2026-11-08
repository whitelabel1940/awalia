"""Resident lifecycle uses isolated Stores and FakeLLMClient from conftest only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from types import SimpleNamespace
import sys

import pytest
from fastapi.testclient import TestClient

from cbs.config import AutonomyConfig
from cbs.web import create_app

TOKEN = 'test-desktop-owner'
HEADERS = {'X-Awalia-Desktop': TOKEN}


def make_app(pipeline, **kwargs):
    return create_app(pipeline, settings_dir=pipeline.store.path.parent, **kwargs)


@pytest.mark.parametrize('host,token', [('testclient', TOKEN), ('::1', TOKEN), ('192.0.2.1', TOKEN),
                                         ('127.0.0.1', ''), ('127.0.0.1', 'wrong')])
def test_shutdown_requires_loopback_and_owner(pipeline, host, token):
    called = []
    app = make_app(pipeline, resident_token=TOKEN, shutdown=lambda: called.append(True))
    with TestClient(app, client=(host, 12345)) as client:
        headers = {'X-Awalia-Desktop': token}
        assert client.get('/api/resident/health', headers=headers).status_code == 403
        assert client.post('/api/resident/shutdown', headers=headers).status_code == 403
        assert not app.state.resident_draining
    assert not called


def test_browser_server_has_no_shutdown_channel(pipeline):
    with TestClient(make_app(pipeline), client=('127.0.0.1', 1)) as client:
        assert client.post('/api/resident/shutdown', headers=HEADERS).status_code == 403


def test_shutdown_finishes_extract_and_backup_before_lifespan_returns(pipeline, tmp_path):
    requested, release, saving, finished = Event(), Event(), Event(), Event()
    app = make_app(pipeline, resident_token=TOKEN, shutdown=requested.set)
    marker = tmp_path / 'saved.txt'
    failures = []

    waiting = Event()
    original_wait = pipeline.wait_for_steward
    def wait_for_steward():
        waiting.set()
        original_wait()
    pipeline.wait_for_steward = wait_for_steward

    def run():
        try:
            with ThreadPoolExecutor() as pool:
                with TestClient(app, client=('127.0.0.1', 12345)) as client:
                    assert client.get('/api/resident/health', headers=HEADERS).json() == {'ready': True}
                    def extract():
                        saving.set()
                        assert release.wait(15)
                        marker.write_text('extract saved', encoding='utf-8')
                    pipeline._post_future = pool.submit(extract)
                    assert saving.wait(10)
                    original = app.state.backup.close
                    def close():
                        original()
                        marker.write_text(marker.read_text(encoding='utf-8') + '; backup closed', encoding='utf-8')
                    app.state.backup.close = close
                    assert client.post('/api/resident/shutdown', headers=HEADERS).status_code == 202
                    assert requested.is_set()
                    assert client.get('/api/settings').status_code == 503
                finished.set()
        except BaseException as error:
            failures.append(error)

    worker = Thread(target=run)
    worker.start()
    try:
        assert requested.wait(15)
        assert waiting.wait(10)
        assert not finished.is_set()
        assert not marker.exists()
    finally:
        release.set()
        worker.join(20)
    assert not worker.is_alive()
    assert not failures
    assert finished.is_set()
    assert marker.read_text(encoding='utf-8') == 'extract saved; backup closed'


def test_idle_only_setting_requires_desktop_and_shared_switch_works_in_browser(pipeline):
    app = make_app(pipeline, resident_token=TOKEN)
    with TestClient(app, client=('127.0.0.1', 12345)) as client:
        assert 'idle_nudge' not in client.get('/api/settings').json()['autonomy']
        assert 'resident_opening_delay_seconds' not in client.get('/api/settings').json()['autonomy']
        cfg = client.get('/api/settings', headers=HEADERS).json()
        assert cfg['autonomy']['idle_nudge'] is False
        assert cfg['autonomy']['idle_poll_seconds'] == 180
        assert cfg['autonomy']['resident_opening_delay_seconds'] == 90
        assert client.put('/api/settings', json={'autonomy': {'idle_nudge': True}}).status_code == 422
        result = client.put('/api/settings', headers=HEADERS, json={'autonomy': {'idle_nudge': True}})
        assert result.status_code == 200
        assert result.json()['autonomy']['idle_nudge'] is True
        browser = client.put('/api/settings', json={'autonomy': {'opening': 'off'}})
        assert browser.status_code == 200
        assert 'idle_nudge' not in browser.json()['autonomy']
        assert pipeline.config.autonomy.idle_nudge is False
        assert 'idle_nudge' in (pipeline.store.path.parent / 'settings.toml').read_text(encoding='utf-8')
        assert not any(call for call in pipeline.client.calls if call.get('role') == 'actor')


def test_poll_interval_is_bounded():
    assert AutonomyConfig(idle_poll_seconds=60).idle_poll_seconds == 60
    for interval in (0, 59, 3601):
        with pytest.raises(ValueError):
            AutonomyConfig(idle_poll_seconds=interval)


def test_idle_preserves_stale_turn_guard(pipeline, monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, 'proactive', lambda *args, **kwargs: calls.append((args, kwargs)) or {'speak': False})
    with TestClient(make_app(pipeline)) as client:
        assert client.post('/api/proactive/idle', json={'session_id': 'session', 'after_turn_id': 7}).status_code == 200
    assert calls == [(('session', 'idle'), {'after_turn_id': 7})]


def test_check_db_cli_isolates_all_paths_and_uses_graceful_server(tmp_path, monkeypatch):
    from cbs.web import __main__ as main
    captured = {}
    app = SimpleNamespace(state=SimpleNamespace(shutdown_clock=SimpleNamespace(started=None)))
    monkeypatch.setattr(sys, 'argv', ['cbs.web', '--db', str(tmp_path / 'check.sqlite3'), '--port', '18765'])
    monkeypatch.setattr(main, 'create_app', lambda **kwargs: captured.update(kwargs) or app)
    class Server:
        def __init__(self, config):
            assert config.host == '127.0.0.1'
            assert config.port == 18765
            assert config.access_log is False
            assert config.timeout_graceful_shutdown == 5
            self.should_exit = False
        def run(self):
            app.state.resident_shutdown()
            assert self.should_exit
    monkeypatch.setattr(main.uvicorn, 'Server', Server)
    monkeypatch.setenv('CBS_DESKTOP_TOKEN', TOKEN)
    main.main()
    assert captured['settings_dir'] == tmp_path
    assert captured['characters_root'] == tmp_path / 'characters'
    assert captured['work_root'] == tmp_path / 'web_bootstrap'
    assert captured['resident_token'] == TOKEN


def test_static_files_always_revalidate(tmp_path):
    from fastapi.testclient import TestClient
    from cbs.web import create_app
    from cbs.web.bootstrap_host import BootstrapHost
    from cbs.config import load_config
    from cbs.llm import FakeLLMClient
    from cbs.store import Store
    host = BootstrapHost(load_config(data_dir=tmp_path), Store(tmp_path / 'cbs.sqlite3'), FakeLLMClient())
    app = create_app(host, characters_root=tmp_path / 'characters', work_root=tmp_path / 'work', settings_dir=tmp_path)
    with TestClient(app) as client:
        response = client.get('/static/ui.js')
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-cache'
