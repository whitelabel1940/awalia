"""FR1 exercises synthetic sources and FakeLLMClient only; never the live data tree."""
import json
from pathlib import Path
import socket
import sys
import tomllib

from fastapi.testclient import TestClient
import pytest

from cbs.config import load_config
from cbs.first_run import MARKER, prepare, prune, trial_info, trial_lock, trials
from cbs.llm import FakeLLMClient


@pytest.fixture
def source(tmp_path):
    data = tmp_path / 'synthetic-real'
    data.mkdir()
    (data / 'cbs.sqlite3').write_bytes(b'real database sentinel')
    (data / 'characters').mkdir()
    (data / 'characters' / 'sentinel').write_text('real character', encoding='utf-8')
    (data / 'settings.toml').write_text('''[character]
id = "existing_partner"
[llm]
mode = "simple"
[llm.providers.custom]
api_key_env = "FR1_CUSTOM_KEY"
[simple]
actor = "openrouter/test-actor"
[first_run]
keep = 2
[backup]
folder = "C:/real-exports"
frequency = "daily"
''', encoding='utf-8')
    (data / 'secrets.env').write_text('OPENROUTER_API_KEY="fake-source-key"\nFR1_CUSTOM_KEY="fake-custom-key"\n', encoding='utf-8')
    return data


def inventory(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and 'first-run' not in p.relative_to(root).parts}


def test_new_keep_bare_copy_only_settings_and_never_modify_source(source):
    before = inventory(source)
    first = prepare(source)
    assert first.parent == source / 'first-run'
    assert not (first / 'secrets.env').exists()
    assert (first / 'cbs.sqlite3').read_bytes() == b''
    assert list((first / 'characters').iterdir()) == []
    values = tomllib.loads((first / 'settings.toml').read_text(encoding='utf-8'))
    assert 'id' not in values.get('character', {})
    assert values['simple']['actor'] == 'openrouter/test-actor'
    assert values['backup']['frequency'] == 'off' and values['backup']['folder'] == ''
    (first / 'connection.json').write_text('trial progress', encoding='utf-8')
    assert prepare(source, 'keep') == first
    assert (first / 'connection.json').read_text(encoding='utf-8') == 'trial progress'
    second = prepare(source)
    assert second != first and not (second / 'connection.json').exists()
    bare = prepare(source, 'bare')
    assert trial_info(bare)['bare'] is True
    assert prepare(source, 'keep') == bare
    assert inventory(source) == before


def test_keep_without_history_creates_new_trial(source):
    assert trial_info(prepare(source, 'keep')) == {'version': 1, 'bare': False}


def test_credentials_read_normally_without_copy_and_bare_clears_custom_env(source, monkeypatch):
    monkeypatch.delenv('FR1_CUSTOM_KEY', raising=False)
    directory = prepare(source)
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fake-inherited-key')
    cfg = load_config(data_dir=directory, secrets_dir=source)
    import os
    assert os.environ['OPENROUTER_API_KEY'] == 'fake-inherited-key'
    assert os.environ['FR1_CUSTOM_KEY'] == 'fake-custom-key'
    assert cfg.simple.actor == 'openrouter/test-actor'
    assert not (directory / 'secrets.env').exists()
    bare = prepare(source, 'bare')
    load_config(data_dir=bare, secrets_dir=source, without_keys=True)
    assert 'OPENROUTER_API_KEY' not in os.environ and 'FR1_CUSTOM_KEY' not in os.environ
    # A key explicitly entered in the trial is resumable and never written back.
    (bare / 'secrets.env').write_text('OPENROUTER_API_KEY="fake-trial-key"', encoding='utf-8')
    load_config(data_dir=bare, secrets_dir=source, without_keys=True)
    assert os.environ['OPENROUTER_API_KEY'] == 'fake-trial-key'
    assert 'fake-trial-key' not in (source / 'secrets.env').read_text(encoding='utf-8')


def test_settings_copy_filters_accidental_secrets(source):
    with (source / 'settings.toml').open('a', encoding='utf-8') as stream:
        stream.write('\n[accidental]\napi_key = "do-not-copy"\nvalue = "sk-12345678901234567890123456789012345"\n')
    text = (prepare(source) / 'settings.toml').read_text(encoding='utf-8')
    assert 'do-not-copy' not in text and 'sk-123' not in text
    assert 'FR1_CUSTOM_KEY' in text  # Env variable names are references, not keys.


@pytest.mark.parametrize('source_key', ['fake-source-key', ''])
def test_trial_entered_key_is_read_before_source_file(source, monkeypatch, source_key):
    import os
    (source / 'secrets.env').write_text(f'OPENROUTER_API_KEY="{source_key}"', encoding='utf-8')
    directory = prepare(source)
    (directory / 'secrets.env').write_text('OPENROUTER_API_KEY="fake-trial-key"', encoding='utf-8')
    load_config(data_dir=directory, secrets_dir=source)
    assert os.environ['OPENROUTER_API_KEY'] == 'fake-trial-key'
    assert 'fake-trial-key' not in (source / 'secrets.env').read_text(encoding='utf-8')


def test_pinned_backend_restart_reuses_directory_and_retention_bounds(source):
    from cbs.config import FirstRunConfig
    directory = prepare(source)
    latest = prepare(source)
    assert prepare(source, 'new', directory=directory) == directory
    assert trials(source) == [latest, directory]
    assert FirstRunConfig().keep == 5
    for value in (0, 101):
        with pytest.raises(ValueError):
            FirstRunConfig(keep=value)


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows file sharing')
def test_prune_preserves_native_shell_during_backend_restart(source):
    import ctypes
    from ctypes import wintypes
    old = prepare(source)
    latest = prepare(source)
    create = ctypes.windll.kernel32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = ctypes.windll.kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    handle = create(str(old / '.shell-active'), 0x40000000, 0, None, 2, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        prune(source, 1, latest)
        assert old.exists()
    finally:
        close(handle)
    prune(source, 1, latest)
    assert not old.exists()


def test_pruning_respects_limit_active_trials_and_unmanaged_dirs(source):
    old = prepare(source)
    active = prepare(source)
    latest = prepare(source)
    unmanaged = source / 'first-run' / 'unmanaged'
    unmanaged.mkdir()
    (unmanaged / 'sentinel').write_text('keep', encoding='utf-8')
    with trial_lock(active):
        prune(source, 1, latest)
        assert not old.exists() and active.exists() and latest.exists()
    prune(source, 1, latest)
    assert trials(source) == [latest]
    assert unmanaged.exists()


def test_pinned_resume_cannot_escape_trial_root(source, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / MARKER).write_text(json.dumps({'version': 1, 'bare': False}), encoding='utf-8')
    with pytest.raises(ValueError):
        prepare(source, directory=outside)


@pytest.mark.parametrize('mode', ['new', 'keep', 'bare'])
def test_cli_isolates_paths_reserves_port_and_keeps_progress(source, monkeypatch, mode):
    from cbs.web import __main__ as main
    from cbs.web import create_app
    before = inventory(source)
    client = FakeLLMClient()
    monkeypatch.setattr('cbs.llm.create_client', lambda config: client)
    captured = {}
    def build(**kwargs):
        captured.update(kwargs)
        app = create_app(**kwargs)
        assert app.state.pipeline.client is client
        with TestClient(app) as web:
            assert web.get('/api/partner').json() == {'partner': None}
            assert web.get('/api/settings').json()['connection_verified'] is False
            for path in ('/', '/wizard', '/dev'):
                text = web.get(path).text
                assert 'お試し</title>' in text and 'trial-banner' in text
            assert web.post('/api/backup/exports', json={}).status_code == 409
            assert web.put('/api/backup/settings', json={'folder': 'C:/real-exports'}).status_code == 409
            app.state.backup.request_due()
            assert app.state.backup.thread is None
        assert not (kwargs['settings_dir'] / 'backup').exists()
        return app
    monkeypatch.setattr(main, 'create_app', build)
    class Server:
        def __init__(self, config):
            self.config = config
        def run(self, *, sockets):
            port = sockets[0].getsockname()[1]
            assert port != 8765 and self.config.port == port
            with socket.socket() as other:
                with pytest.raises(OSError):
                    other.bind(('127.0.0.1', port))
    monkeypatch.setattr(main.uvicorn, 'Server', Server)
    monkeypatch.setattr(sys, 'argv', ['cbs.web', f'--first-run={mode}', '--data-dir', str(source)])
    main.main()
    directory = captured['settings_dir']
    assert captured['db_path'] == directory / 'cbs.sqlite3'
    assert captured['characters_root'] == directory / 'characters'
    assert captured['work_root'] == directory / 'web_bootstrap'
    assert captured['secrets_dir'] == source and captured['without_keys'] == (mode == 'bare')
    assert inventory(source) == before
    assert not client.calls and not client.embedding_calls


def test_trial_migration_and_direct_export_never_create_backups(source, tmp_path):
    from cbs.backup import build_export, pre_migration
    directory = prepare(source)
    assert pre_migration(directory / 'cbs.sqlite3', directory / 'characters', directory, None) is None
    with pytest.raises(ValueError, match='お試し'):
        build_export(directory / 'cbs.sqlite3', directory / 'characters', directory, None, tmp_path / 'export.zip')
    assert not (directory / 'backup').exists() and not (tmp_path / 'export.zip').exists()


def test_normal_export_does_not_include_trial_directories(pipeline, tmp_path):
    from cbs.backup import build_export
    import zipfile
    data = pipeline.store.path.parent
    trial = prepare(data)
    (trial / 'sentinel').write_text('do not export trial', encoding='utf-8')
    destination = tmp_path / 'normal.zip'
    build_export(pipeline.store.path, pipeline.characters_root, data, pipeline.config.character.id, destination)
    with zipfile.ZipFile(destination) as archive:
        assert not any('first-run' in name or 'sentinel' in name for name in archive.namelist())


def test_welcomed_trial_partner_and_conversation_survive_keep(source, monkeypatch):
    from cbs.web import create_app
    from test_bootstrap import minimal_input, package_data
    from test_web_bootstrap import finish
    before = inventory(source)
    fake = FakeLLMClient(json_responses=[package_data(), {'problems': []},
                                       {'introduction': 'Trial partner', 'open_now': []}])
    monkeypatch.setattr('cbs.llm.create_client', lambda config: fake)
    directory = prepare(source, 'bare')
    def app():
        return create_app(db_path=directory / 'cbs.sqlite3', settings_dir=directory,
                          characters_root=directory / 'characters', work_root=directory / 'web_bootstrap',
                          secrets_dir=source, without_keys=True)
    first = app()
    with TestClient(first) as web:
        body = minimal_input().model_dump(mode='json', by_alias=True)
        job = finish(web, web.post('/api/bootstrap/create', json=body))
        assert job['status'] == 'done', job
        assert web.post(f"/api/bootstrap/jobs/{job['id']}/confirm").status_code == 200
        fake.json_responses = [{}, {'problems': []}]
        session = web.post('/api/sessions', json={}).json()
        response = web.post(f"/api/sessions/{session['id']}/messages", json={'content': 'trial greeting'})
        assert response.status_code == 200, response.text
        turn = response.json()['turn_id']
    assert prepare(source, 'keep') == directory
    fake = FakeLLMClient()
    with TestClient(app()) as web:
        assert web.get('/api/partner').json()['partner']['id'] == 'test_character'
        history = web.get(f"/api/sessions/{session['id']}/turns").json()
        assert any(t['id'] == turn and t['user_text'] == 'trial greeting' for t in history)
        assert web.get('/api/settings').json()['backup_status']['trial'] is True
    fresh = prepare(source)
    assert list((fresh / 'characters').iterdir()) == []
    assert inventory(source) == before


def test_cli_rejects_mixed_live_paths(source, monkeypatch):
    from cbs.web import __main__ as main
    for extra in [['--db', str(source / 'cbs.sqlite3')], ['--characters-root', str(source / 'characters')], ['--character', 'fixture']]:
        monkeypatch.setattr(sys, 'argv', ['cbs.web', '--first-run', *extra])
        with pytest.raises(SystemExit) as error:
            main.main()
        assert error.value.code == 2
