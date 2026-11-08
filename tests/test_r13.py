"""R13 trace storage, backup compatibility, and interruptible conversion."""
from contextlib import closing
import json
import sqlite3
from threading import Event

from fastapi.testclient import TestClient
import pytest

from cbs import backup, clock, restore
from cbs.config import StorageConfig
from cbs.probe import describe
from cbs.shutdown import run_upkeep
from cbs.storage import (TRACE_MAGIC, TRACE_POLICIES, dump_trace, vacuum_traces)
from cbs.store import Store
from cbs.web import create_app


def full_trace():
    call = {'request': {'system': 'synthetic request ' * 1000},
            'response': {'text': 'raw', 'parsed': {'problems': []}},
            'usage': {'input_tokens': 100}, 'latency_ms': 12}
    return {'Packet': {'input': {'emotional_weight': 'high', 'domains_touched': []},
                       'output': {'partner_requests': {'version': 0}, 'secrets': []}},
            'Load': {'output': {'candidate_memories': ['synthetic'] * 100}},
            'Check': call, 'inner': call, 'judgment': call,
            'voice_attempts': [{**call, 'check': call}],
            'Commit': {'input': {'post_check': None}, 'output': {'warnings': []}}}


def test_trace_policy_covers_database_schema(store):
    with store.connect() as db:
        actual = {(table, row['name'])
                  for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  for row in db.execute(f'PRAGMA table_info({table})')
                  if row['name'] in ('trace_json', 'input_json')}
    assert actual == {(table, policy[0]) for table, policy in TRACE_POLICIES.items()}
    assert StorageConfig().full_trace_days == 2
    assert StorageConfig(full_trace_days=0).full_trace_days == 0
    with pytest.raises(ValueError):
        StorageConfig(full_trace_days=-1)


def test_author_augmentation_input_is_lossless_even_in_backups(store, tmp_path):
    original = '{  "author_text": "' + ('synthetic author input ' * 100) + '" }\n'
    with store.connect() as db:
        version = db.execute("INSERT INTO model_versions(character_id,base_version,diff_json,rationale,episode_refs_json,created_at) VALUES ('fixture',1,'{}','synthetic','[]',?)", (clock.iso(),)).lastrowid
        db.execute("INSERT INTO bootstrap_augments(character_id,input_json,answers_json,readable_md,model_version_id,created_at) VALUES ('fixture',?,'{}','synthetic',?,?)", (original, version, clock.iso()))
    store.trim_traces(0, vacuum=False)
    with store.connect() as db:
        assert db.execute('SELECT input_json FROM bootstrap_augments').fetchone()[0] == original
    backup.snapshot_db(store.path, tmp_path / 'author.sqlite3')
    with closing(backup.readonly(tmp_path / 'author.sqlite3')) as db:
        assert db.execute('SELECT input_json FROM bootstrap_augments').fetchone()[0] == original


def test_compressed_and_legacy_reads_inspector_probes_and_late_writes(pipeline, tmp_path):
    s = pipeline.store
    sid = s.create_session('fixture')['id']
    trace = full_trace()
    tid = s.add_turn(sid, 'synthetic input', 'synthetic reply', trace)
    with closing(sqlite3.connect(s.path)) as db:
        stored = db.execute('SELECT trace_json FROM turns WHERE id=?', (tid,)).fetchone()[0]
        assert stored.startswith(TRACE_MAGIC)
    assert s.get_trace(tid) == trace
    with s.connect() as db:
        db.execute('UPDATE turns SET trace_json=? WHERE id=?', (json.dumps(trace), tid))
    assert s.get_trace(tid) == trace
    s.finish_expression(tid, 'smile', {'Expression': {'usage': {'tokens': 1}}})
    assert s.get_trace(tid)['Packet'] == trace['Packet']
    client = TestClient(create_app(pipeline, characters_root=pipeline.characters_root,
                                   settings_dir=tmp_path / 'settings'))
    assert client.get(f'/api/turns/{tid}/trace?spoilers=true').status_code == 200
    before = describe(s.get_trace(tid), pipeline.character.model)
    with clock.at('2030-01-01'):
        s.trim_traces(vacuum=False)
    short = s.get_trace(tid)
    assert short['Packet']['input'] == trace['Packet']['input']
    assert describe(short, pipeline.character.model) == before
    assert 'request' not in short['inner']
    assert 'request' not in short['voice_attempts'][0]['check']
    assert short['voice_attempts'][0]['usage'] == trace['voice_attempts'][0]['usage']
    assert client.get(f'/api/turns/{tid}/trace').status_code == 200
    assert client.get('/api/inner').status_code == 200


def test_inner_update_retention_and_zero_days(pipeline):
    s = pipeline.store
    with clock.at('2026-01-01'):
        sid = s.create_session('fixture')['id']
        old = s.add_turn(sid, 'u', 'a', full_trace())
        pending = s.add_turn(sid, 'u', 'a', {**full_trace(), 'Extract': {'state': 'pending'}})
        with s.connect() as db:
            db.execute("INSERT INTO inner_update_runs(character_id,created_at,trigger_ref,status,trace_json) VALUES ('fixture',?,'r13','completed',?)",
                       (clock.iso(), dump_trace(full_trace())))
    with clock.at('2026-01-02'):
        s.trim_traces(2, vacuum=False)
        assert 'request' in s.get_trace(old)['Check']
        s.trim_traces(0, vacuum=False)
    assert 'request' not in s.get_trace(old)['Check']
    assert 'request' in s.get_trace(pending)['Check']
    with s.connect() as db:
        trace = json.loads(db.execute('SELECT trace_json FROM inner_update_runs').fetchone()[0])
    assert 'request' not in trace['inner']
    assert trace['inner']['response']['parsed'] == {'problems': []}


@pytest.mark.parametrize('kind', ['auto', 'manual', 'pre-restore', 'migration'])
def test_backup_compacts_even_recent_and_pending_traces(pipeline, tmp_path, kind):
    s = pipeline.store
    sid = s.create_session('fixture')['id']
    trace = full_trace()
    tid = s.add_turn(sid, 'u', 'a', trace)
    pending = s.add_turn(sid, 'u', 'a', {**trace, 'Extract': {'state': 'pending'}})
    target = tmp_path / (kind + '.zip')
    backup.build_export(s.path, pipeline.characters_root, s.path.parent, 'fixture', target, kind=kind)
    work = tmp_path / 'content'
    manifest = backup.verify_zip(target)
    restore.inspect_zip(target, work, pipeline.config.backup.restore_max_expanded_bytes)
    restored = Store(work / 'data/cbs.sqlite3', characters_root=work / 'characters')
    assert 'request' in s.get_trace(tid)['Check']  # Export never alters the source.
    assert 'request' not in restored.get_trace(tid)['Check']
    short = restored.get_trace(pending)
    assert short['Extract']['state'] == 'pending'
    assert short['Packet'] == trace['Packet']
    assert short['Check']['response']['parsed'] == trace['Check']['response']['parsed']
    assert 'request' not in short['Check']
    assert restored.history(sid)[0]['assistant_text'] == 'a'
    restored.finish_post_check(pending, {'Extract': {'response': {'parsed': {}}}},
                               {'ledger_updates': [], 'new_canon': []}, [])
    assert restored.get_trace(pending)['Extract']['state'] == 'completed'
    assert manifest['format_version'] == 1


def test_old_uncompressed_backup_restores(pipeline, tmp_path, monkeypatch):
    s = pipeline.store
    sid = s.create_session('fixture')['id']
    tid = s.add_turn(sid, 'u', 'a', full_trace())
    with s.connect() as db:
        db.execute('UPDATE turns SET trace_json=?', (json.dumps(full_trace()),))
        db.execute('PRAGMA user_version=6')
    def old_snapshot(source, target):
        with closing(backup.readonly(source)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
    monkeypatch.setattr(backup, 'snapshot_db', old_snapshot)
    archive = tmp_path / 'old.zip'
    backup.build_export(s.path, pipeline.characters_root, s.path.parent, 'fixture', archive)
    work = tmp_path / 'old'
    summary = restore.inspect_zip(archive, work, pipeline.config.backup.restore_max_expanded_bytes)
    assert summary['turns'] == 1
    restored = Store(work / 'data/cbs.sqlite3', characters_root=work / 'characters')
    assert restored.get_trace(tid) == full_trace()


def test_backup_preserves_memory_followups_and_sql_json_updates(pipeline, tmp_path):
    from cbs.memory import episodize, finish_memory_followups
    p = pipeline
    sid = p.store.create_session('fixture')['id']
    p.store.add_turn(sid, 'synthetic', 'synthetic', {})
    p.store.sweep_conversations(force=True)
    job = p.store.memory_jobs(character_id='fixture')[0]
    episodize(p, job, followups=False)
    with p.store.connect() as db:
        trace = json.loads(db.execute('SELECT trace_json FROM memory_jobs WHERE id=?', (job['id'],)).fetchone()[0])
        trace['followups_pending'] = True
        db.execute('UPDATE memory_jobs SET trace_json=? WHERE id=?', (dump_trace(trace), job['id']))
        count = db.execute('SELECT count(*) FROM memory_episodes WHERE job_id=?', (job['id'],)).fetchone()[0]
    target = tmp_path / 'followups.sqlite3'
    backup.snapshot_db(p.store.path, target)
    original_store = p.store
    try:
        p.store = Store(target, characters_root=p.characters_root)
        with p.store.connect() as db:
            trace = json.loads(db.execute('SELECT trace_json FROM memory_jobs WHERE id=?', (job['id'],)).fetchone()[0])
            assert 'request' not in trace and trace['followups_pending']
            # Exercise SQL JSON reads with a compressed value as well as plain backup JSON.
            db.execute('UPDATE memory_jobs SET trace_json=? WHERE id=?', (dump_trace(trace), job['id']))
            assert db.execute("SELECT json_extract(trace_text(trace_json),'$.followups_pending') FROM memory_jobs WHERE id=?", (job['id'],)).fetchone()[0] == 1
        finish_memory_followups(p, job)
        with p.store.connect() as db:
            trace = json.loads(db.execute('SELECT trace_json FROM memory_jobs WHERE id=?', (job['id'],)).fetchone()[0])
            assert not trace['followups_pending']
            assert db.execute('SELECT count(*) FROM memory_episodes WHERE job_id=?', (job['id'],)).fetchone()[0] == count
    finally:
        p.store = original_store


def test_relocated_backup_is_compact_and_retries_after_interrupt(pipeline, tmp_path, monkeypatch):
    s = pipeline.store
    sid = s.create_session('fixture')['id']
    tid = s.add_turn(sid, 'u', 'a', full_trace())
    moved = tmp_path / 'retained.sqlite3'
    with closing(backup.readonly(s.path)) as src, closing(sqlite3.connect(moved)) as dst:
        src.backup(dst)
    journal = {'moves': [[str(s.path.absolute()), str(moved.absolute())]], 'committed': True}
    import cbs.storage as storage
    original = storage.vacuum_traces
    def interrupt(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(storage, 'vacuum_traces', interrupt)
    with pytest.raises(KeyboardInterrupt):
        restore.compact_relocated_database(journal, s.path)
    monkeypatch.setattr(storage, 'vacuum_traces', original)
    restore.compact_relocated_database(journal, s.path)
    with closing(backup.readonly(moved)) as db:
        trace = json.loads(db.execute('SELECT trace_json FROM turns WHERE id=?', (tid,)).fetchone()[0])
        assert 'request' not in trace['Check']
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert 'request' in s.get_trace(tid)['Check']


def test_compressed_import_journal_can_undo_after_backup(pipeline, tmp_path):
    s = pipeline.store
    before = {'request': {'system': 'before ' * 1000}, 'response': {'parsed': {'revision': 1}}}
    after = {'request': {'system': 'after ' * 1000}, 'response': {'parsed': {'revision': 2}}}
    with s.connect() as db:
        db.execute("INSERT INTO import_batches VALUES ('r13','fixture','completed','{}',?,?,NULL,0)", (clock.iso(), clock.iso()))
        ident = db.execute("INSERT INTO knowing_runs(character_id,local_day,status,trace_json,created_at) VALUES ('fixture','2026-01-01','completed',?,?)",
                           (dump_trace(before), clock.iso())).lastrowid
        with s.import_journal(db, 'r13'):
            db.execute('UPDATE knowing_runs SET trace_json=? WHERE id=?', (dump_trace(after), ident))
    backup.snapshot_db(s.path, tmp_path / 'journal.sqlite3')
    restored = Store(tmp_path / 'journal.sqlite3', characters_root=pipeline.characters_root)
    restored.undo_import('fixture', 'r13')
    with restored.connect() as db:
        trace = json.loads(db.execute('SELECT trace_json FROM knowing_runs WHERE id=?', (ident,)).fetchone()[0])
    assert trace['response']['parsed']['revision'] == 1


def test_shrink_stops_between_rows_and_retries(store, monkeypatch):
    sid = store.create_session('fixture')['id']
    with clock.at('2020-01-01'):
        ids = [store.add_turn(sid, 'u', 'a', full_trace()) for _ in range(4)]
    import cbs.storage as storage
    original = storage.dump_trace
    calls = 0
    def stop_second(value):
        nonlocal calls
        calls += 1
        if calls == 2:
            store.upkeep_stop.set()
        return original(value)
    monkeypatch.setattr(storage, 'dump_trace', stop_second)
    assert run_upkeep(store, store.shrink_once) is None
    assert 'request' not in store.get_trace(ids[0])['Check']
    assert 'request' in store.get_trace(ids[1])['Check']  # In-flight row rolled back.
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM storage_migrations').fetchone()[0] == 0
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    store.upkeep_stop.clear()
    monkeypatch.setattr(storage, 'dump_trace', original)
    assert run_upkeep(store, store.shrink_once) is True
    assert run_upkeep(store, store.shrink_once) is None
    assert all('request' not in store.get_trace(tid)['Check'] for tid in ids)


def test_vacuum_interrupt_rolls_back_and_can_retry(tmp_path):
    stop = Event()
    with closing(sqlite3.connect(tmp_path / 'vacuum.sqlite3')) as db:
        db.execute('CREATE TABLE payload(value BLOB)')
        db.executemany('INSERT INTO payload VALUES (zeroblob(65536))', [()] * 100)
        db.commit()
        class Interrupting:
            def set_progress_handler(self, handler, n):
                def interrupt():
                    stop.set()
                    return handler()
                db.set_progress_handler(interrupt if handler else None, n)
            def execute(self, sql):
                return db.execute(sql)
        from cbs.shutdown import UpkeepStopped
        with pytest.raises(UpkeepStopped):
            vacuum_traces(Interrupting(), stop)
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT count(*) FROM payload').fetchone()[0] == 100
        stop.clear()
        vacuum_traces(db, stop)
