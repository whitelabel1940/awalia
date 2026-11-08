"""BK2 round trips and failure injection. Synthetic data and FakeLLMClient only."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tomllib
from urllib.parse import quote
import zipfile

from fastapi.testclient import TestClient
import pytest

from cbs import backup, restore
from cbs.config import BackupConfig, ROOT
from cbs.store import Store
from cbs.web import create_app


@pytest.fixture
def sample(pipeline, tmp_path):
    p = pipeline
    data, chars = p.store.path.parent, p.characters_root
    config = tmp_path / 'config.toml'
    shutil.copyfile(ROOT / 'config.toml', config)
    sid = p.store.create_session('fixture')['id']
    tid = p.store.add_turn(sid, '往復する会話', '覚えているよ', {})
    state = p.store.current_requests(p.character)
    p.store.save_requests(p.character, state['fields'], [{'text': '短く答えて'}], p.config.requests, state['version'])
    p.store.save_days('fixture', [dict(kind='anniversary', name='出会った日', month=10, day=21)],
                      [dict(name='挨拶', time='07:00', weekdays=[0, 1], enabled=True, allow_quiet=False)], 0)
    with p.store.connect() as db:
        conversation = db.execute("INSERT INTO conversations(session_id,started_at,last_activity_at) VALUES (?,?,?)",
                                  (sid, '2026-10-21T12:00:00+09:00', '2026-10-21T12:00:00+09:00')).lastrowid
        db.execute("""INSERT INTO memory_episodes(character_id,conversation_id,started_at,ended_at,summary,details,importance,pinned,created_at)
                      VALUES ('fixture',?,'2026-10-21','2026-10-21','ふたりの記憶','保存した内容',.7,1,'2026-10-21')""", (conversation,))
        db.execute("INSERT INTO canon_facts(character_id,kind,content,source_turn,created_at) VALUES ('fixture','past_detail','保存する記憶',?,?)", (tid, '2026-10-21T12:00:00+09:00'))
        db.execute("INSERT INTO photos VALUES (?, 'fixture', ?, '写真の説明', 1, 1)", ('a' * 32, tid))
    (data / 'photos').mkdir()
    (data / 'photos' / ('a' * 32 + '.jpg')).write_bytes(b'synthetic photo bytes')
    (data / 'settings.toml').write_text('[ui]\ntheme="dark"\n[character]\nid="fixture"\n', encoding='utf-8')
    archive = tmp_path / 'original.zip'
    manifest = backup.build_export(p.store.path, chars, data, 'fixture', archive, config_path=config)
    return p, data, chars, config, archive, manifest


def prepare(sample, password='', archive=None):
    p, data, chars, config, source, manifest = sample
    work = data / 'restore/previews' / ('b' * 32)
    work.mkdir(parents=True)
    upload = work / 'upload'
    shutil.copyfile(archive or source, upload)
    result = restore.preview(upload, work, password, p.config.backup)
    return work, result


def apply(sample):
    p, data, chars, config, *_ = sample
    # Startup has no live Store or background SQLite handles. Collect the fixture's
    # completed sqlite context-manager cycles to model the process boundary on Windows.
    import gc
    gc.collect()
    restore.apply_staged(data, chars, p.store.path, config_path=config)


def test_restore_leaves_the_old_backup_folder_and_says_so_once(sample, tmp_path):
    p, data, chars, config, _, _ = sample
    (data / 'settings.toml').write_text("[character]\nid=\"fixture\"\n[backup]\nfolder='D:\\Sync\\Awalia'\nfrequency=\"daily\"\n", encoding='utf-8')
    archive = tmp_path / 'with_folder.zip'
    backup.build_export(p.store.path, chars, data, 'fixture', archive, config_path=config)
    (data / 'settings.toml').write_text('[character]\nid="fixture"\n', encoding='utf-8')  # this PC: no folder yet
    work, _ = prepare(sample, archive=archive)
    restore.stage(work, data)
    apply(sample)
    saved = tomllib.loads((data / 'settings.toml').read_text(encoding='utf-8'))
    assert 'folder' not in saved.get('backup', {}) and 'frequency' not in saved.get('backup', {})
    assert json.loads((data / 'restore/notice.json').read_text(encoding='utf-8'))['backup_folder'] == 'D:\\Sync\\Awalia'


def contents(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_round_trip_all_records_settings_photos_secrets_and_untouched_resources(sample):
    p, data, chars, config, archive, manifest = sample
    work, summary = prepare(sample)
    assert summary['turns'] == 1 and summary['partners'][0]['name'] == p.character.model.display_name
    with closing(backup.readonly(work / 'content/data/cbs.sqlite3')) as db:
        expected = {t: db.execute(f'SELECT * FROM {t}').fetchall() for t in ('turns', 'memory_episodes', 'canon_facts', 'request_versions', 'partner_days', 'photos')}
    extra = chars / 'absent'
    extra.mkdir(); (extra / 'original.txt').write_text('move me')
    for relative in ('models/model.bin', 'tools/whisper.exe', 'tts_cache/cached.wav', 'first-run/trial/marker'):
        path = data / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'untouched')
    char_cache = chars / 'fixture/cache/keep.bin'
    char_cache.parent.mkdir(); char_cache.write_bytes(b'untouched')
    secret = data / 'secrets.env'; secret.write_bytes(b'LOCAL_SENTINEL=synthetic\n')
    connection = data / 'connection.json'; connection.write_bytes(b'{"keep":true}')
    (data / 'settings.toml').write_text('[ui]\ntheme="light"\n[private]\npassword="synthetic current secret"\n', encoding='utf-8')
    expression = chars / 'fixture/expression.yaml'
    expression.write_text(expression.read_text(encoding='utf-8') + '\napi_key: synthetic-partner-secret\n', encoding='utf-8')
    p.store.add_turn(p.store.list_sessions()[0]['id'], '後の記録', 'バックアップに残す', {})
    restore.stage(work, data)
    # Confirmation is staging only; current live records stay live until startup.
    with p.store.connect() as db:
        assert db.execute('SELECT count(*) FROM turns').fetchone()[0] == 2
    apply(sample)
    with closing(backup.readonly(p.store.path)) as db:
        assert {t: db.execute(f'SELECT * FROM {t}').fetchall() for t in expected} == expected
    saved = tomllib.loads((data / 'settings.toml').read_text(encoding='utf-8'))
    assert saved['ui']['theme'] == 'dark' and saved['private']['password'] == 'synthetic current secret'
    assert secret.read_bytes() == b'LOCAL_SENTINEL=synthetic\n' and connection.read_bytes() == b'{"keep":true}'
    assert config.read_bytes() == (ROOT / 'config.toml').read_bytes()
    assert not extra.exists()
    moved = list((data / 'backup').glob('restore-*/characters/absent/original.txt'))
    assert len(moved) == 1 and moved[0].read_text() == 'move me'
    assert char_cache.read_bytes() == b'untouched'
    import yaml
    assert yaml.safe_load(expression.read_text(encoding='utf-8'))['api_key'] == 'synthetic-partner-secret'
    for relative in ('models/model.bin', 'tools/whisper.exe', 'tts_cache/cached.wav', 'first-run/trial/marker'):
        assert (data / relative).read_bytes() == b'untouched'
    before = list((data / 'backup/pre-restore').glob('*.zip'))
    assert len(before) == 1 and backup.verify_zip(before[0])['kind'] == 'pre-restore'
    with zipfile.ZipFile(before[0]) as z:
        target = data / 'pre.sqlite3'; target.write_bytes(z.read('data/cbs.sqlite3'))
    with closing(backup.readonly(target)) as db:
        assert db.execute('SELECT count(*) FROM turns').fetchone()[0] == 2
    assert json.loads((data / 'restore/notice.json').read_text())['created_at'] == manifest['created_at']
    assert not (data / 'restore/staged').exists()
    apply(sample)  # No stage means no repeat replacement or extra pre-restore copy.
    assert len(list((data / 'backup/pre-restore').glob('*.zip'))) == 1


def rewrite(source, target, change):
    with zipfile.ZipFile(source) as z:
        files = {n: z.read(n) for n in z.namelist()}
    manifest = json.loads(files.pop('manifest.json'))
    change(files, manifest)
    manifest['files'] = {n: dict(size=len(b), sha256=hashlib.sha256(b).hexdigest()) for n, b in files.items()}
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for n, b in files.items():
            z.writestr(n, b)
        z.writestr('manifest.json', json.dumps(manifest))


@pytest.mark.parametrize('password', ['', 'wrong', 'パスワード'])
def test_encrypted_retries_and_cleanup(sample, tmp_path, password):
    encrypted = tmp_path / 'export.awalia'
    backup._crypt(sample[4], encrypted, 'パスワード')
    if password == 'wrong':
        with pytest.raises(Exception):
            prepare(sample, password, encrypted)
    else:
        work, result = prepare(sample, password, encrypted)
        assert result.get('password_required', False) == (not password)
        assert not (work / 'upload').exists() and not (work / 'decoded.zip').exists()


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/outside', 'data/secrets.env', 'data/first-run/keep',
    'data/tools/model.bin', 'characters/fixture/cache/item', 'characters/fixture/CON.txt', 'characters/fixture/a:stream',
    'characters/fixture/a.', 'characters/fixture/a\\b', 'characters/FIXTURE/model.yaml'])
def test_reject_paths_and_out_of_scope_even_with_matching_manifest(sample, tmp_path, name):
    target = tmp_path / 'bad.zip'
    rewrite(sample[4], target, lambda files, manifest: files.update({name: b'payload'}))
    with pytest.raises(Exception):
        prepare(sample, archive=target)
    assert not (tmp_path / 'escape').exists()


@pytest.mark.parametrize('field,value', [('format_version', 999), ('schema_version', 999), ('schema_sha256', '0' * 64)])
def test_reject_future_formats_schema_and_hash(sample, tmp_path, field, value):
    target = tmp_path / 'future.zip'
    rewrite(sample[4], target, lambda files, manifest: manifest.update({field: value}))
    with pytest.raises(Exception):
        prepare(sample, archive=target)


def test_tamper_limits_and_invalid_database(sample, tmp_path):
    p = sample[0]
    p.config.backup.restore_max_bytes = 1024
    with pytest.raises(ValueError, match='upload too large'):
        prepare(sample)
    shutil.rmtree(sample[1] / 'restore/previews')
    p.config.backup = BackupConfig(restore_max_expanded_bytes=1024)
    with pytest.raises(ValueError, match='archive too large'):
        prepare(sample)
    shutil.rmtree(sample[1] / 'restore/previews')
    p.config.backup = BackupConfig()
    target = tmp_path / 'corrupt.zip'
    rewrite(sample[4], target, lambda files, manifest: files.update({'data/cbs.sqlite3': b'not sqlite'}))
    with pytest.raises(sqlite3.DatabaseError):
        prepare(sample, archive=target)
    shutil.rmtree(sample[1] / 'restore/previews')
    work, _ = prepare(sample)
    (work / 'content/characters/fixture/model.yaml').write_bytes(b'tampered')
    with pytest.raises(ValueError, match='changed stage'):
        restore.stage(work, sample[1])


def test_legacy_version_zero_uses_hash_and_shapes_then_store_migrates(sample, tmp_path):
    old = tmp_path / 'legacy.sqlite3'
    with closing(sqlite3.connect(old)) as db:
        from cbs.store import SCHEMA
        db.executescript(SCHEMA)
    def legacy(files, manifest):
        files['data/cbs.sqlite3'] = old.read_bytes()
        for name in list(files):
            if name.startswith('data/photos/'):
                del files[name]
        with closing(backup.readonly(old)) as db:
            schema = '\n'.join(r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"))
        manifest.update(schema_version=0, schema_sha256=hashlib.sha256(schema.encode()).hexdigest())
    archive = tmp_path / 'legacy.zip'; rewrite(sample[4], archive, legacy)
    work, _ = prepare(sample, archive=archive)
    restore.stage(work, sample[1]); apply(sample)
    store = Store(sample[0].store.path, characters_root=sample[2])
    with store.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == backup.SCHEMA_VERSION
        assert db.execute('SELECT count(*) FROM partner_days').fetchone()[0] == 0


@pytest.mark.parametrize('fail_at', [1, 3, 7, 12])
def test_mid_apply_failure_rolls_everything_back(sample, monkeypatch, fail_at):
    p, data, chars, *_ = sample
    work, _ = prepare(sample); restore.stage(work, data)
    original_db, original_chars, original_settings = p.store.path.read_bytes(), contents(chars), (data / 'settings.toml').read_bytes()
    move = restore.move; calls = 0
    def failed(source, destination):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise OSError('synthetic failure')
        move(source, destination)
    monkeypatch.setattr(restore, 'move', failed)
    with pytest.raises(ValueError, match='元の記録を保護'):
        apply(sample)
    assert calls >= fail_at
    assert p.store.path.read_bytes() == original_db and contents(chars) == original_chars
    assert (data / 'settings.toml').read_bytes() == original_settings
    assert not (data / 'restore/transaction.json').exists()


def test_interrupted_startup_recovers_before_app_opens_db(sample, monkeypatch):
    p, data, chars, *_ = sample
    work, _ = prepare(sample); restore.stage(work, data)
    original = p.store.path.read_bytes()
    move = restore.move
    def crash(source, target):
        move(source, target)
        raise SystemExit('power interruption')
    # Simulate process death rather than the normal exception rollback handler.
    monkeypatch.setattr(restore, 'move', crash)
    monkeypatch.setattr(restore, 'rollback', lambda *args: None)
    with pytest.raises(SystemExit):
        apply(sample)
    monkeypatch.undo()
    with pytest.raises(ValueError):
        apply(sample)
    assert p.store.path.read_bytes() == original
    assert not (data / 'restore/transaction.json').exists()


def test_pre_restore_backup_failure_prevents_any_replacement(sample, monkeypatch):
    p, data, chars, *_ = sample
    work, _ = prepare(sample); restore.stage(work, data)
    original = p.store.path.read_bytes()
    monkeypatch.setattr(backup, 'build_export', lambda *args, **kw: (_ for _ in ()).throw(OSError('failed')))
    with pytest.raises(ValueError):
        apply(sample)
    assert p.store.path.read_bytes() == original and not (data / 'restore/transaction.json').exists()


def test_failure_at_last_settings_move_restores_new_partner_photos_and_absent_folder(sample, monkeypatch):
    p, data, chars, *_ = sample
    work, _ = prepare(sample); restore.stage(work, data)
    # Introduce a partner and photo that must survive a failure after all earlier moves.
    (chars / 'absent').mkdir(); (chars / 'absent/keep.txt').write_bytes(b'preserve')
    (data / 'photos' / ('b' * 32 + '.jpg')).write_bytes(b'current photo')
    before_db, before_chars, before_photos = p.store.path.read_bytes(), contents(chars), contents(data / 'photos')
    before_settings = (data / 'settings.toml').read_bytes()
    original, failed = restore.move, False
    def last_move(source, destination):
        nonlocal failed
        if destination == data / 'settings.toml' and not failed:
            failed = True
            raise OSError('last replacement failed')
        original(source, destination)
    monkeypatch.setattr(restore, 'move', last_move)
    with pytest.raises(ValueError, match='元の記録を保護'):
        apply(sample)
    assert failed
    assert p.store.path.read_bytes() == before_db and contents(chars) == before_chars
    assert contents(data / 'photos') == before_photos and (data / 'settings.toml').read_bytes() == before_settings


def test_api_preview_cancel_stage_owner_restart_and_one_time_notice(sample):
    p, data, chars, config, archive, manifest = sample
    app = create_app(p, settings_dir=data, characters_root=chars, resident_token='synthetic-owner', shutdown=lambda: None)
    client = TestClient(app, client=('127.0.0.1', 1234))
    headers = {'X-Awalia-Desktop': 'synthetic-owner'}
    assert client.get('/api/resident/restore-ready').status_code == 403
    assert not client.get('/api/resident/restore-ready', headers=headers).json()['ready']
    result = client.post('/api/restore/preview', content=archive.read_bytes())
    assert result.status_code == 200, result.text
    ident = result.json()['id']
    assert client.delete(f'/api/restore/previews/{ident}').status_code == 200
    assert client.post(f'/api/restore/previews/{ident}/stage').status_code == 404
    ident = client.post('/api/restore/preview', content=archive.read_bytes()).json()['id']
    assert client.post(f'/api/restore/previews/{ident}/stage').status_code == 202
    assert client.get('/api/resident/restore-ready', headers=headers).json()['ready']
    assert client.post('/api/resident/shutdown', headers=headers).status_code == 202
    apply(sample)
    app.state.resident_draining = False
    assert client.post('/api/restore/notice').json() == {'created_at': manifest['created_at'], 'backup_folder': None}
    assert client.post('/api/restore/notice').json() == {'created_at': None, 'backup_folder': None}
    app.state.backup.close()


def test_api_password_retry_limits_errors_and_trial(sample, tmp_path):
    p, data, chars, config, archive, manifest = sample
    encrypted = tmp_path / 'encrypted'; backup._crypt(archive, encrypted, 'パスワード')
    app = create_app(p, settings_dir=data, characters_root=chars)
    client = TestClient(app)
    assert client.post('/api/restore/preview', content=encrypted.read_bytes()).json() == {'password_required': True}
    headers = {'X-Awalia-Restore-Password': quote(json.dumps('SENSITIVE_SENTINEL'))}
    response = client.post('/api/restore/preview', content=encrypted.read_bytes(), headers=headers)
    assert response.status_code == 422 and 'SENSITIVE_SENTINEL' not in response.text
    headers = {'X-Awalia-Restore-Password': quote(json.dumps('パスワード'))}
    assert client.post('/api/restore/preview', content=encrypted.read_bytes(), headers=headers).status_code == 200
    p.config.backup.restore_max_bytes = 1024
    assert client.post('/api/restore/preview', content=b'x' * 1025).status_code == 413
    app.state.first_run = True
    assert client.post('/api/restore/preview', content=b'ignored').status_code == 409
    assert client.post('/api/restore/previews/' + 'b' * 32 + '/stage').status_code == 409
    assert not any(path.name in {'upload', 'decoded.zip'} for path in (data / 'restore').rglob('*'))
    app.state.backup.close()


def test_actual_app_startup_applies_before_store_and_config_load(sample, monkeypatch):
    from cbs.llm import FakeLLMClient
    import cbs.llm
    import cbs.store
    p, data, chars, *_ = sample
    work, _ = prepare(sample); restore.stage(work, data)
    p.store.add_turn(p.store.list_sessions()[0]['id'], '適用前だけ', '退避する', {})
    import gc
    gc.collect()
    original = cbs.store.Store
    opened = []
    def checked(path, *args, **kwargs):
        assert not (data / 'restore/staged').exists()
        assert tomllib.loads((data / 'settings.toml').read_text(encoding='utf-8'))['ui']['theme'] == 'dark'
        opened.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(cbs.store, 'Store', checked)
    monkeypatch.setattr(cbs.llm, 'create_client', lambda config: FakeLLMClient())
    app = create_app(db_path=p.store.path, settings_dir=data, characters_root=chars, work_root=data / 'bootstrap')
    current = app.state.pipeline
    assert opened
    with current.store.connect() as db:
        assert db.execute('SELECT count(*) FROM turns').fetchone()[0] == 1
    current.wait_for_steward(); current._embedding_warmer.wait(); app.state.backup.close()


def test_no_partner_receiving_pc_and_separate_db_photo_location(sample, tmp_path):
    p, _, _, config, archive, _ = sample
    data, chars, dbdir = tmp_path / 'receiving', tmp_path / 'new-partners', tmp_path / 'database'
    data.mkdir(); dbdir.mkdir()
    # An empty receiving app has a DB already, but no partner folders or credentials.
    Store(dbdir / 'cbs.sqlite3')
    work = data / 'restore/previews' / ('d' * 32); work.mkdir(parents=True)
    upload = work / 'upload'; shutil.copyfile(archive, upload)
    restore.preview(upload, work, '', p.config.backup); restore.stage(work, data)
    import gc
    gc.collect()
    restore.apply_staged(data, chars, dbdir / 'cbs.sqlite3', config_path=config)
    assert (chars / 'fixture/model.yaml').is_file()
    assert (dbdir / 'photos' / ('a' * 32 + '.jpg')).read_bytes() == b'synthetic photo bytes'
    assert not (data / 'photos').exists()


def test_reject_duplicate_case_symlink_hash_tamper_and_member_count(sample, tmp_path):
    archive = tmp_path / 'duplicates.zip'
    with zipfile.ZipFile(sample[4]) as z:
        files = {n: z.read(n) for n in z.namelist()}
    for kind in ('duplicate', 'case', 'symlink', 'hash'):
        with zipfile.ZipFile(archive, 'w') as z:
            for name, content in files.items():
                z.writestr(name, content + (b'changed' if kind == 'hash' and name.endswith('model.yaml') else b''))
            if kind in ('duplicate', 'case'):
                z.writestr('manifest.json' if kind == 'duplicate' else 'MANIFEST.JSON', files['manifest.json'])
            if kind == 'symlink':
                info = zipfile.ZipInfo('characters/fixture/link'); info.create_system = 3
                info.external_attr = 0o120777 << 16; z.writestr(info, '../elsewhere')
        with pytest.raises(ValueError):
            backup.verify_zip(archive, max_bytes=8 * 1024**3)
    with pytest.raises(ValueError, match='archive too large'):
        backup.verify_zip(sample[4], max_files=1)


def test_abandoned_previews_expire_without_blocking_new_uploads(sample):
    p, data, chars, _, archive, _ = sample
    app = create_app(p, settings_dir=data, characters_root=chars)
    client = TestClient(app)
    identifiers = [client.post('/api/restore/preview', content=archive.read_bytes()).json()['id'] for _ in range(5)]
    assert len(list((data / 'restore/previews').iterdir())) == 4
    assert client.post(f'/api/restore/previews/{identifiers[0]}/stage').status_code == 404
    assert client.post(f'/api/restore/previews/{identifiers[-1]}/stage').status_code == 202
    assert client.post('/api/restore/preview', content=archive.read_bytes()).status_code == 409
    app.state.backup.close()


@pytest.mark.parametrize('winerror,retried', [(5, True), (32, True), (33, True), (None, False)])
def test_restore_move_waits_out_a_temporary_windows_lock(tmp_path, monkeypatch, winerror, retried):
    source, destination = tmp_path / 'cbs.sqlite3', tmp_path / 'old/cbs.sqlite3'
    source.write_bytes(b'live')
    replace, attempts, waits = Path.replace, [], []
    def locked(path, target):
        attempts.append(path)
        if len(attempts) <= 2:
            error = PermissionError('scanner holds the file'); error.winerror = winerror
            raise error
        return replace(path, target)
    monkeypatch.setattr(Path, 'replace', locked)
    monkeypatch.setattr(backup.time, 'sleep', waits.append)
    if not retried:
        with pytest.raises(PermissionError):
            restore.move(source, destination)
        assert len(attempts) == 1 and not waits and source.read_bytes() == b'live'
        return
    restore.move(source, destination)
    assert destination.read_bytes() == b'live' and not source.exists()
    assert len(attempts) == 3 and 0 < sum(waits) < 1
