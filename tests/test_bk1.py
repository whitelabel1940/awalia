from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from threading import Event, Thread
from time import perf_counter
import tomllib
import zipfile

from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient
import pytest

from cbs import clock
from cbs import backup
from cbs.config import load_config
from cbs.store import Store


@pytest.fixture
def sample(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    chars = tmp_path / 'characters'
    for cid in ('current', 'previous', 'sample'):
        directory = chars / cid
        (directory / 'source').mkdir(parents=True)
        (directory / 'source' / 'profile.md').write_text('作者原資料', encoding='utf-8')
        (directory / 'model.yaml').write_text('signatures: [{id: sig_kind, when: rain}]\n', encoding='utf-8')
        (directory / 'look').mkdir()
        (directory / 'look' / 'neutral.png').write_bytes(b'image')
        (directory / 'voice').mkdir()
        (directory / 'voice' / 'reference.wav').write_bytes(b'voice reference')
        generated = 'a123456789abcdef0123456789abcdef.wav'
        (directory / 'voice' / generated).write_bytes(b'voice reference')
        (directory / 'voice.yaml').write_text(f'reference: {generated}\n', encoding='utf-8')
        (directory / 'voice' / 'references.json').write_text(json.dumps([{'id': generated, 'name': 'my reference'}]), encoding='utf-8')
        (directory / 'reveal.yaml').write_text('secrets: [{id: past, about: character history}]\n', encoding='utf-8')
        for excluded in ('cache', 'models', 'probes', '.tmp'):
            (directory / excluded).mkdir()
            (directory / excluded / 'discard.bin').write_bytes(b'not exported')
    for name in ('secrets.env', 'connection.json'):
        (data / name).write_text('SENSITIVE_SENTINEL', encoding='utf-8')
    (data / 'settings.toml').write_text('[models]\nactor="chosen/model"\n[extra]\ntoken="SENSITIVE_SENTINEL"\ninnocent="sk-' + 'X' * 45 + '"\n[tts]\nenabled=true\n', encoding='utf-8')
    store = Store(data / 'cbs.sqlite3')
    sid = store.create_session('previous')['id']
    store.add_turn(sid, 'hello', 'hi', {})
    cfg = load_config(data_dir=tmp_path / 'config')
    cfg.character.id = 'current'
    return data, chars, store, cfg


def export(sample, path, **options):
    data, chars, store, cfg = sample
    return backup.build_export(store.path, chars, data, cfg.character.id, path, **options)


def test_inventory_secrets_and_hashes(sample, tmp_path):
    path = tmp_path / 'out.zip'
    manifest = export(sample, path, config_path=Path('config.toml'))
    assert backup.verify_zip(path) == manifest
    assert manifest['characters'] == ['current', 'previous']
    assert manifest['schema_version'] == backup.SCHEMA_VERSION
    assert manifest['app_version'] and manifest['schema_sha256']
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert 'characters/current/source/profile.md' in names
        assert 'characters/previous/voice/reference.wav' in names
        assert 'characters/current/look/neutral.png' in names
        assert not any('sample/' in n or 'discard.bin' in n for n in names)
        assert not any('secrets.env' in n or 'connection.json' in n for n in names)
        settings = tomllib.loads(archive.read('data/settings.toml').decode())
        assert settings['models']['actor'] == 'chosen/model'
        assert settings['extra'] == {}
        assert b'SENSITIVE_SENTINEL' not in b''.join(archive.read(n) for n in names)
        assert b'signatures:' in archive.read('characters/current/model.yaml')
        assert b'secrets:' in archive.read('characters/current/reveal.yaml')
        import yaml
        voice = yaml.safe_load(archive.read('characters/current/voice.yaml'))
        assert voice['reference'].endswith('.wav')
        refs = json.loads(archive.read('characters/current/voice/references.json'))
        assert refs[0]['id'] == voice['reference']
        assert 'characters/current/voice/' + voice['reference'] in names
        assert tomllib.loads(archive.read('config.toml').decode())['simple']['actor_candidates']


def test_clean_nested_settings():
    opaque = 'AbcDef0123456789' * 4
    data = {'safe': 'normal', 'api_key': 'fake', 'signature': 'fake', 'nested': {'innocent': opaque, 'safe': True},
            'list': ['normal', opaque], 'providers': [{'name': 'local', 'token': 'fake'}],
            'url': 'https://user:pass@example.invalid/', 'bearer': 'Bearer ' + opaque}
    assert backup.clean_settings(data) == {'safe': 'normal', 'nested': {'safe': True}, 'providers': [{'name': 'local'}]}


def test_snapshot_during_concurrent_atomic_writes(sample, tmp_path):
    _, _, store, _ = sample
    with store.connect() as db:
        db.execute('CREATE TABLE paired (id INTEGER PRIMARY KEY, value INTEGER)')
        db.executemany('INSERT INTO paired VALUES (?, 0)', [(1,), (2,)])
    stop, running = Event(), Event()
    def writer():
        with closing(sqlite3.connect(store.path)) as db:
            while not stop.is_set():
                with db:
                    db.execute('UPDATE paired SET value=value+1')
                running.set()
                stop.wait(.002)
    thread = Thread(target=writer)
    thread.start()
    try:
        assert running.wait(5)
        export(sample, tmp_path / 'copy.zip')
        with zipfile.ZipFile(tmp_path / 'copy.zip') as archive:
            (tmp_path / 'copy.sqlite3').write_bytes(archive.read('data/cbs.sqlite3'))
        with closing(backup.readonly(tmp_path / 'copy.sqlite3')) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert len({r[0] for r in db.execute('SELECT value FROM paired')}) == 1
    finally:
        stop.set()
        thread.join(5)


def test_encryption_correct_password_only_and_tamper(sample, tmp_path):
    path, decoded = tmp_path / 'out.awalia', tmp_path / 'decoded.zip'
    export(sample, path, password='unit test password')
    assert path.read_bytes().startswith(backup.MAGIC)
    backup._crypt(path, decoded, 'unit test password', decrypt=True)
    backup.verify_zip(decoded)
    with pytest.raises(InvalidTag):
        backup._crypt(path, decoded, 'incorrect', decrypt=True)
    payload = bytearray(path.read_bytes())
    payload[-20] ^= 1
    path.write_bytes(payload)
    with pytest.raises(InvalidTag):
        backup._crypt(path, decoded, 'unit test password', decrypt=True)


@pytest.mark.parametrize('stage', ['snapshot_db', 'verify_zip', '_crypt'])
def test_failure_removes_all_partial_files(sample, tmp_path, monkeypatch, stage):
    def fail(*args, **kwargs):
        raise OSError('fake sensitive provider error')
    monkeypatch.setattr(backup, stage, fail)
    destination = tmp_path / 'output' / 'copy.awalia'
    with pytest.raises(OSError):
        export(sample, destination, password='unit password')
    assert list(destination.parent.iterdir()) == []


def test_manifest_tampering_detected(sample, tmp_path):
    path = tmp_path / 'out.zip'
    export(sample, path)
    with zipfile.ZipFile(path, 'a') as archive:
        archive.writestr('unexpected.txt', 'unexpected')
    with pytest.raises(ValueError, match='inventory'):
        backup.verify_zip(path)


def test_daily_weekly_and_scheduled_retention(sample, tmp_path):
    folder = tmp_path / 'retention'
    for day in [1, 8, 15, 22, 25, 26, 27, 28, 29, 30, 31]:
        with clock.at(f'2026-08-{day:02d} 12:00'):
            export(sample, folder / backup.export_name('auto'), kind='auto')
    unknown = folder / 'personal.zip'
    unknown.write_bytes(b'my file')
    fake = folder / backup.export_name('auto')
    fake.write_bytes(b'not an archive')
    with clock.at('2026-08-31 18:00'):
        backup.prune(folder, 'auto', days=7, weeks=4)
    owned = [p for p in folder.glob('awalia-auto-*.zip') if p != fake]
    assert len(owned) == 10  # seven days + three older weeks present
    assert unknown.exists() and fake.exists()
    for n in range(6):
        with clock.at(f'2026-09-0{n+1} 12:00'):
            export(sample, folder / backup.export_name('scheduled'), kind='scheduled')
    backup.prune(folder, 'scheduled', keep=4)
    assert len(list(folder.glob('awalia-scheduled-*.zip'))) == 4


def test_pre_migration_captures_old_shape_and_only_when_needed(sample):
    data, chars, store, cfg = sample
    with store.connect() as db:
        db.execute('PRAGMA user_version=0')
        db.execute('CREATE TABLE legacy_marker (value TEXT)')
        db.execute("INSERT INTO legacy_marker VALUES ('preserve')")
    Store(store.path, characters_root=chars, current_character_id=cfg.character.id)
    files = list((data / 'backup' / 'migration').glob('*.zip'))
    assert len(files) == 1
    meta = backup.verify_zip(files[0])
    assert meta['schema_version'] == 0
    assert meta['characters'] == ['current', 'previous']
    with zipfile.ZipFile(files[0]) as archive:
        old = data / 'old.sqlite3'
        old.write_bytes(archive.read('data/cbs.sqlite3'))
    with closing(backup.readonly(old)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
        assert db.execute('SELECT value FROM legacy_marker').fetchone()[0] == 'preserve'
    Store(store.path, characters_root=chars, current_character_id=cfg.character.id)
    assert len(list((data / 'backup' / 'migration').glob('*.zip'))) == 1


def test_worker_coalesces_is_independent_and_errors_are_fixed(sample, monkeypatch):
    data, chars, store, cfg = sample
    worker = backup.BackupWorker(store, chars, data, lambda: cfg)
    entered, release = Event(), Event()
    original = backup.build_export
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(backup, 'build_export', blocked)
    with clock.at('2026-10-20 12:00'):
        worker.request_due()
        assert entered.wait(5)
        started = perf_counter()
        worker.request_due()
        sid = store.create_session('current')['id']
        store.add_turn(sid, 'while backing up', 'still works', {})
        assert perf_counter() - started < 1
        assert len(worker.status()['jobs']) == 1
        release.set()
        worker.wait()
        worker.request_due()
        assert len(worker.status()['jobs']) == 1
        assert worker.status()['auto_success'].startswith('2026-10-20')
    def broken(*args, **kwargs):
        raise RuntimeError('SENSITIVE_SENTINEL')
    monkeypatch.setattr(backup, 'build_export', broken)
    worker.request('auto')
    worker.wait()
    state = worker.status()
    assert state['auto_success']
    assert state['auto_error'] == backup.ERROR and state['auto_error_type'] == 'RuntimeError'
    assert 'SENSITIVE_SENTINEL' not in json.dumps(state)
    assert 'SENSITIVE_SENTINEL' not in worker.state_path.read_text()
    worker.close()


def test_schedule_persistence_and_retry(sample, tmp_path):
    data, chars, store, cfg = sample
    folder = tmp_path / 'scheduled'
    folder.mkdir()
    cfg.backup.folder, cfg.backup.frequency = str(folder), 'weekly'
    worker = backup.BackupWorker(store, chars, data, lambda: cfg)
    with clock.at('2026-10-01 12:00'):
        worker.request_due()
        worker.wait()
    assert len(list(folder.glob('*.zip'))) == 1
    other = backup.BackupWorker(store, chars, data, lambda: cfg)
    with clock.at('2026-10-07 12:00'):
        other.request_due()
        other.wait()
    assert len(list(folder.glob('*.zip'))) == 1
    with clock.at('2026-10-08 12:00'):
        other.request_due()
        other.wait()
    assert len(list(folder.glob('*.zip'))) == 2
    assert not worker.status()['scheduled_password_available']
    worker.close()
    other.close()


def test_api_export_and_settings_dont_wait_on_foreground(pipeline, tmp_path, monkeypatch):
    from cbs.web import create_app
    chars = tmp_path / 'characters'
    (chars / 'fixture').mkdir(parents=True)
    (chars / 'fixture' / 'profile.md').write_text('fixture', encoding='utf-8')
    app = create_app(pipeline, characters_root=chars, settings_dir=tmp_path / 'settings')
    worker = app.state.backup
    folder = tmp_path / 'sync'
    folder.mkdir()
    # No lifespan/TTS/provider jobs are required for these endpoints.
    client = TestClient(app)
    pipeline._lock.acquire()
    try:
        with ThreadPoolExecutor(1) as pool:
            response = pool.submit(client.put, '/api/backup/settings', json={'frequency': 'daily', 'folder': str(folder)}).result(3)
        assert response.status_code == 200
        assert response.json()['config']['frequency'] == 'daily'
        response = client.post('/api/backup/exports', json={'password': 'unit test password'})
        assert response.status_code == 202
        ident = response.json()['id']
    finally:
        pipeline._lock.release()
    worker.wait()
    job = client.get(f'/api/backup/exports/{ident}').json()
    assert job['status'] == 'completed'
    result = client.get(f'/api/backup/exports/{ident}/download')
    assert result.status_code == 200 and result.content.startswith(backup.MAGIC)
    assert 'path' not in job
    assert 'unit test password' not in worker.state_path.read_text() if worker.state_path.exists() else True
    assert client.get('/api/backup/exports/invalid/download').status_code == 404
    assert client.put('/api/backup/settings', json={'frequency': 'daily', 'folder': str(folder / 'missing')}).status_code == 422
    assert client.put('/api/backup/settings', json={'frequency': 'daily', 'folder': str(folder), 'password': 'SENSITIVE_SENTINEL'}).status_code == 422
    assert 'SENSITIVE_SENTINEL' not in client.post('/api/backup/exports', json={'password': 'SENSITIVE_SENTINEL', 'extra': 1}).text
    for value in ['SENSITIVE_SENTINEL', ['SENSITIVE_SENTINEL']]:
        for url in ('/api/backup/exports', '/api/backup/settings'):
            result = client.request('PUT' if url.endswith('settings') else 'POST', url, json=value)
            assert result.status_code == 422 and 'SENSITIVE_SENTINEL' not in result.text
    worker.close()


def test_daily_hook_even_if_already_maintained(pipeline, monkeypatch):
    from cbs import memory
    class Worker:
        closed = False
        called = 0
        def request_due(self):
            self.called += 1
    pipeline.store.backup_worker = Worker()
    monkeypatch.setattr(memory, '_daily_maintenance', lambda p: None)
    memory.daily_maintenance(pipeline)
    assert pipeline.store.backup_worker.called == 1


def test_backup_partial_settings_preserve_other_fields(pipeline, tmp_path):
    from cbs.web import create_app
    app = create_app(pipeline, settings_dir=tmp_path / 'settings')
    folder = tmp_path / 'sync'
    folder.mkdir()
    try:
        client = TestClient(app)
        initial = {'folder': str(folder), 'frequency': 'weekly', 'daily_keep': 9,
                   'weekly_keep': 5, 'export_keep': 6}
        first = client.put('/api/backup/settings', json=initial)
        assert first.status_code == 200
        initial = first.json()['config']
        updated = client.put('/api/backup/settings', json={'frequency': 'daily'})
        assert updated.status_code == 200
        assert updated.json()['config'] == {**initial, 'frequency': 'daily'}
        other = tmp_path / 'other'
        other.mkdir()
        updated = client.put('/api/backup/settings', json={'folder': str(other)})
        assert updated.status_code == 200
        assert updated.json()['config'] == {**initial, 'frequency': 'daily', 'folder': str(other)}
        assert client.put('/api/backup/settings', json={'frequency': 'unknown'}).status_code == 422
        assert client.get('/api/backup').json()['config'] == updated.json()['config']
    finally:
        app.state.backup.close()


def test_missing_character_fails_without_incomplete_export(sample, tmp_path):
    data, chars, store, cfg = sample
    store.create_session('missing')
    destination = tmp_path / 'out' / 'copy.zip'
    with pytest.raises(ValueError, match='missing character'):
        export(sample, destination)
    assert list(destination.parent.iterdir()) == []


def test_slow_export_does_not_block_turn_opening_or_requests(pipeline, tmp_path, monkeypatch):
    from cbs.web import create_app
    app = create_app(pipeline, settings_dir=tmp_path / 'settings')
    worker = app.state.backup
    entered, release = Event(), Event()
    original = backup.build_export
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(*args, **kwargs)
    monkeypatch.setattr(backup, 'build_export', slow)
    ident = worker.request()
    assert entered.wait(3)
    try:
        client = TestClient(app)
        duplicate = client.post('/api/backup/exports', json={'password': 'different unit password'})
        assert duplicate.status_code == 409
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(client.get, '/api/settings').result(3).status_code == 200
            session = pool.submit(client.post, '/api/sessions', json={}).result(3).json()
            opening = pool.submit(client.post, '/api/proactive/opening', json={'session_id': session['id']}).result(5)
            assert opening.status_code == 200
            turn = pool.submit(client.post, f"/api/sessions/{session['id']}/messages", json={'content': 'hello'}).result(5)
            assert turn.status_code == 200
        assert client.get(f'/api/backup/exports/{ident}').json()['status'] == 'running'
    finally:
        release.set()
        worker.close()


def test_settings_concurrent_update_preserves_backup_choice(pipeline, tmp_path, monkeypatch):
    from cbs.web import create_app
    from cbs.web import settings_api
    app = create_app(pipeline, settings_dir=tmp_path / 'settings')
    client = TestClient(app)
    ready, release = Event(), Event()
    original = settings_api.apply_llm_mode
    def paused(config):
        result = original(config)
        ready.set()
        assert release.wait(10)
        return result
    monkeypatch.setattr(settings_api, 'apply_llm_mode', paused)
    folder = tmp_path / 'sync'
    folder.mkdir()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(client.put, '/api/settings', json={'early': {'notify': False}})
        try:
            assert ready.wait(3)
            assert client.put('/api/backup/settings', json={'folder': str(folder), 'frequency': 'weekly'}).status_code == 200
        finally:
            release.set()
        assert future.result(5).status_code == 200
    assert pipeline.config.backup.frequency == 'weekly'
    persisted = tomllib.loads((tmp_path / 'settings' / 'settings.toml').read_text(encoding='utf-8'))
    assert persisted['backup']['frequency'] == 'weekly'
    assert persisted['early']['notify'] is False
    app.state.backup.close()


def test_newer_schema_is_never_downgraded(sample):
    _, chars, store, _ = sample
    with store.connect() as db:
        db.execute(f'PRAGMA user_version={backup.SCHEMA_VERSION+1}')
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        Store(store.path, characters_root=chars)
    assert store.path.read_bytes() == before


def test_migration_failure_is_safe_and_does_not_stop_store(sample, monkeypatch):
    data, chars, store, _ = sample
    with store.connect() as db:
        db.execute('PRAGMA user_version=0')
    def fail(*args, **kwargs):
        raise OSError('SENSITIVE_SENTINEL')
    monkeypatch.setattr(backup, 'pre_migration', fail)
    Store(store.path, characters_root=chars)
    saved = (data / 'backup' / 'status.json').read_text(encoding='utf-8')
    assert 'SENSITIVE_SENTINEL' not in saved
    assert json.loads(saved)['migration_error'] == backup.ERROR
    assert json.loads(saved)['migration_error_type'] == 'OSError'


def test_successful_backup_clears_an_old_migration_error(sample):
    data, chars, store, cfg = sample
    status = data / 'backup' / 'status.json'
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps({'migration_error': backup.ERROR, 'migration_error_type': 'PermissionError'}), encoding='utf-8')
    worker = backup.BackupWorker(store, chars, data, lambda: cfg)
    assert worker.status()['migration_error_type'] == 'PermissionError'
    worker.request('auto')
    worker.wait()
    state = worker.status()
    assert state['auto_success'] and 'migration_error' not in state and 'migration_error_type' not in state
    assert 'migration_error' not in json.loads(status.read_text(encoding='utf-8'))
    worker.close()


def test_encrypted_export_never_stages_plaintext_in_sync_folder(sample, tmp_path, monkeypatch):
    data, _, _, _ = sample
    destination = tmp_path / 'cloud' / 'copy.awalia'
    original = backup._crypt
    calls = []
    def inspect(source, target, password, *, decrypt=False):
        if not decrypt:
            assert source.is_relative_to(data / 'backup' / 'work')
            assert target.is_relative_to(data / 'backup' / 'work')
            assert not list(destination.parent.rglob('*.zip'))
            assert not list(destination.parent.rglob('*.sqlite3'))
        calls.append(decrypt)
        return original(source, target, password, decrypt=decrypt)
    monkeypatch.setattr(backup, '_crypt', inspect)
    export(sample, destination, password='unit password')
    assert calls == [False, True]
    assert list(destination.parent.iterdir()) == [destination]
    assert list((data / 'backup' / 'work').iterdir()) == []


def test_sync_folder_sees_only_the_finished_backup(sample, tmp_path, monkeypatch):
    # A sync client holds and uploads whatever appears in its folder; work files stay local.
    data, _, _, _ = sample
    folder = tmp_path / 'GoogleDrive'
    folder.mkdir()
    original = backup.verify_zip
    def inspect(path):
        assert Path(path).is_relative_to(data / 'backup' / 'work')
        assert list(folder.iterdir()) == []
        return original(path)
    monkeypatch.setattr(backup, 'verify_zip', inspect)
    destination = folder / backup.export_name('scheduled')
    export(sample, destination, kind='scheduled')
    assert list(folder.iterdir()) == [destination]
    assert list((data / 'backup' / 'work').iterdir()) == []


def test_work_folder_cleanup_failure_does_not_fail_a_finished_backup(sample, tmp_path, monkeypatch):
    data, _, _, _ = sample
    def held(path, *args, **kwargs):
        raise PermissionError(5, 'held by another process')
    monkeypatch.setattr(backup.shutil, 'rmtree', held)
    destination = tmp_path / 'out' / 'copy.zip'
    export(sample, destination)
    assert backup.verify_zip(destination)


def test_old_leftovers_are_swept_and_recent_or_foreign_files_are_kept(sample, tmp_path):
    import os, stat
    data, _, _, _ = sample
    folder = tmp_path / 'GoogleDrive'
    old, recent = folder / '.awalia-bk1-old', folder / '.awalia-bk1-recent'
    for directory in (old, recent):
        directory.mkdir(parents=True)
        (directory / 'cbs.sqlite3').write_bytes(b'stale copy')
    partial = folder / '.awalia-bk1-0123.partial'
    partial.write_bytes(b'partial')
    mine = folder / '.awalia-bk1-notes.txt'
    mine.write_bytes(b'not ours to delete')
    os.chmod(old / 'cbs.sqlite3', stat.S_IREAD)
    os.chmod(old, stat.S_IREAD | stat.S_IEXEC)  # sync clients mark folders read-only
    past = clock.now().timestamp() - 2 * backup.LEFTOVER_AGE
    for path in (old, partial, mine):
        os.utime(path, (past, past))
    export(sample, folder / backup.export_name('scheduled'), kind='scheduled')
    assert not old.exists() and not partial.exists()
    assert recent.exists() and mine.exists()


def test_publish_across_volumes_copies_then_renames(tmp_path, monkeypatch):
    import errno
    source, destination = tmp_path / 'work.zip', tmp_path / 'other' / 'copy.zip'
    destination.parent.mkdir()
    source.write_bytes(b'finished')
    original = Path.replace
    def cross(self, target):
        if self == source:
            raise OSError(errno.EXDEV, 'cross-device link')
        return original(self, target)
    monkeypatch.setattr(Path, 'replace', cross)
    backup.publish(source, destination)
    assert destination.read_bytes() == b'finished'
    assert list(destination.parent.iterdir()) == [destination]


def test_failed_automatic_backup_retries_hourly_not_on_every_upkeep(sample, tmp_path, monkeypatch):
    data, chars, store, cfg = sample
    folder = tmp_path / 'scheduled'
    folder.mkdir()
    cfg.backup.folder, cfg.backup.frequency = str(folder), 'daily'
    calls = []
    def broken(*args, kind, **kwargs):
        calls.append(kind)
        raise PermissionError('held')
    monkeypatch.setattr(backup, 'build_export', broken)
    worker = backup.BackupWorker(store, chars, data, lambda: cfg)
    now = [1000.0]
    monkeypatch.setattr(backup.time, 'monotonic', lambda: now[0])
    for minutes in (0, 5, 10):
        now[0] = 1000.0 + minutes * 60
        worker.request_due()
        worker.wait()
    assert sorted(calls) == ['auto', 'scheduled']
    now[0] = 1000.0 + backup.RETRY_AFTER + 1
    worker.request_due()
    worker.wait()
    assert sorted(calls) == ['auto', 'auto', 'scheduled', 'scheduled']
    assert worker.status()['scheduled_error'] == backup.ERROR
    worker.close()


def test_clean_settings_keeps_hashes_and_token_counts_but_drops_credentials():
    from cbs.backup import clean_settings
    value = {
        'sample_description': 'b1514ebc099765e39fa37eb780b92a140a94c86bb0b3b3d98226b38825979732',
        'usage': {'completion_tokens': 120, 'prompt_tokens_details': {'cached_tokens': 10}},
        'model': 'openrouter/google/gemini-3.8-flash',
        'api_key_env': 'OPENROUTER_API_KEY',
        'api_key': 'sk-or-v1-' + 'a' * 40,
        'refresh_token': 'opaque-refresh-value',
        'note': 'Bearer abcdefghijklmnop',
        'mixed': 'Ab3' * 12,
    }
    cleaned = clean_settings(value)
    assert cleaned['sample_description'] == value['sample_description']
    assert cleaned['usage'] == value['usage']
    assert cleaned['model'] == value['model'] and cleaned['api_key_env'] == 'OPENROUTER_API_KEY'
    for dropped in ('api_key', 'refresh_token', 'note', 'mixed'):
        assert dropped not in cleaned
