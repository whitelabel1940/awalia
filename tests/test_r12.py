"""R12: synthetic backup previews and bounded shutdown; no provider/network calls."""
import asyncio
import base64
from io import BytesIO
import re
import shutil
import subprocess
import sys
from threading import Event
from time import monotonic

from PIL import Image
import pytest

from cbs import backup, restore
from cbs.config import ROOT
from cbs.shutdown import Shutdown, run_upkeep
from cbs.store import Store
from cbs.web import create_app


@pytest.mark.parametrize('slots,expected', [
    (['smile', 'neutral', 'shy'], 'neutral'), (['shy', 'smile'], 'smile'), ([], None),
])
def test_preview_distinguishes_same_names_and_reuses_face_order(pipeline, tmp_path, slots, expected):
    p = pipeline
    chars, data = p.characters_root, p.store.path.parent
    shutil.copytree(chars / 'fixture', chars / 'other')
    model = chars / 'other/model.yaml'
    model.write_text(model.read_text(encoding='utf-8').replace('character_id: fixture', 'character_id: other'), encoding='utf-8')
    look = chars / 'fixture/look'
    look.mkdir()
    colors = {'neutral': 'red', 'smile': 'green', 'shy': 'blue'}
    for slot in slots:
        Image.new('RGB', (400, 200), colors[slot]).save(look / (slot + '.png'))
    # A cover must never become a face fallback.
    Image.new('RGB', (400, 200), 'yellow').save(look / 'cover.png')
    first = p.store.create_session('fixture')['id']
    second = p.store.create_session('fixture')['id']
    empty = p.store.create_session('other')['id']
    for sid in [first, first, second]:
        p.store.add_turn(sid, 'synthetic', 'synthetic', {})
    with p.store.connect() as db:
        db.execute('UPDATE sessions SET created_at=? WHERE id=?', ('2026-10-20T12:00:00+09:00', first))
        db.execute('UPDATE sessions SET created_at=? WHERE id=?', ('2026-10-21T12:00:00+09:00', second))
    (data / 'settings.toml').write_text('[character]\nid="other"\n', encoding='utf-8')
    archive = tmp_path / 'backup.zip'
    backup.build_export(p.store.path, chars, data, 'other', archive, config_path=ROOT / 'config.toml')
    work = tmp_path / 'preview'
    work.mkdir()
    summary = restore.preview(archive, work, '', p.config.backup)
    # The partner that opens after the restore comes first, then the most talked-with.
    two, one = summary['partners']
    assert one['name'] == two['name']
    assert (one['turns'], one['first_at'], one['selected']) == (3, '2026-10-20T12:00:00+09:00', False)
    assert (two['turns'], two['first_at'], two['selected'], two['face']) == (0, None, True, None)
    assert summary['turns'] == 3
    if expected is None:
        assert one['face'] is None
    else:
        assert one['face'].startswith('data:image/png;base64,')
        with Image.open(BytesIO(base64.b64decode(one['face'].split(',')[1]))) as image:
            assert image.size == (96, 48)
            assert image.getpixel((0, 0)) == Image.new('RGBA', (1, 1), colors[expected]).getpixel((0, 0))


def test_upkeep_stop_rolls_back_only_unfinished_unit_and_skips_queued_work(store):
    from cbs.steward import submit, wait_for_steward
    entered, release = Event(), Event()
    def work():
        store.create_session('fixture', 'committed')
        with store.connect() as db:
            db.execute("UPDATE sessions SET title='partial'")
            entered.set()
            assert release.wait(5)
    future = submit(store, work)
    assert entered.wait(5)
    queued = submit(store, store.create_session, 'fixture', 'must not run')
    store.upkeep_stop.set()
    release.set()
    assert future.result(5) is None
    assert queued.result(5) is None
    wait_for_steward(store)
    assert [s['title'] for s in store.list_sessions()] == ['committed']
    # A new Store has a fresh stop event and can retry normally.
    reopened = Store(store.path)
    run_upkeep(reopened, reopened.create_session, 'fixture', 'retry')
    assert {s['title'] for s in reopened.list_sessions()} == {'committed', 'retry'}
    with reopened.connect() as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_stopped_memory_job_is_recovered_at_next_start(pipeline, monkeypatch):
    from cbs.memory import episodize
    from cbs.steward import submit
    from cbs.llm import FakeLLMClient
    p = pipeline
    sid = p.store.create_session('fixture')['id']
    p.store.add_turn(sid, 'synthetic', 'synthetic', {})
    p.store.sweep_conversations(force=True)
    job = p.store.memory_jobs(character_id='fixture')[0]
    entered, release = Event(), Event()
    original = FakeLLMClient.generate_json
    def slow(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(FakeLLMClient, 'generate_json', slow)
    future = submit(p.store, episodize, p, job)
    assert entered.wait(5)
    p.store.upkeep_stop.set()
    release.set()
    future.result(5)
    with p.store.connect() as db:
        assert db.execute('SELECT status FROM memory_jobs WHERE id=?', (job['id'],)).fetchone()[0] == 'running'
    reopened = Store(p.store.path)
    assert reopened.memory_jobs(character_id='fixture')[0]['id'] == job['id']
    p.store.upkeep_stop.clear()
    run_upkeep(p.store, episodize, p, job)
    with p.store.connect() as db:
        assert db.execute('SELECT status FROM memory_jobs WHERE id=?', (job['id'],)).fetchone()[0] == 'completed'


def test_unfinished_daily_pass_replays_on_same_day(pipeline):
    import json
    from cbs.memory import daily_maintenance
    p = pipeline
    with p.store.connect() as db:
        db.execute("UPDATE memory_maintenance SET status='running',trace_json='{}' WHERE character_id='fixture'")
    run_upkeep(p.store, daily_maintenance, p)
    with p.store.connect() as db:
        row = db.execute("SELECT status,trace_json FROM memory_maintenance WHERE character_id='fixture'").fetchone()
        assert row['status'] == 'completed'
        assert all(k in json.loads(row['trace_json']) for k in ['now_summary', 'knowing', 'diaries', 'forming_notes'])


def test_stop_after_memory_commit_replays_only_unfinished_followups(pipeline, monkeypatch):
    import json
    from cbs.memory import episodize, enqueue_pending
    from cbs.llm import FakeLLMClient
    from cbs.inner import InnerUpdate
    p = pipeline
    sid = p.store.create_session('fixture')['id']
    p.store.add_turn(sid, 'synthetic', 'synthetic', {})
    p.store.sweep_conversations(force=True)
    job = p.store.memory_jobs(character_id='fixture')[0]
    original = FakeLLMClient.generate_json
    def stop(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if args[-1] is InnerUpdate:
            p.store.upkeep_stop.set()
        return result
    monkeypatch.setattr(FakeLLMClient, 'generate_json', stop)
    run_upkeep(p.store, episodize, p, job)
    with p.store.connect() as db:
        count = db.execute('SELECT count(*) FROM memory_episodes WHERE job_id=?', (job['id'],)).fetchone()[0]
        trace = json.loads(db.execute('SELECT trace_json FROM memory_jobs WHERE id=?', (job['id'],)).fetchone()[0])
        assert count and trace['followups_pending']
        assert db.execute("SELECT status FROM inner_update_runs WHERE trigger_ref=?", (f"memory_job:{job['id']}",)).fetchone()[0] == 'running'
    Store(p.store.path)  # The same durable bytes are valid at the next open.
    monkeypatch.setattr(FakeLLMClient, 'generate_json', original)
    p.store.upkeep_stop.clear()
    enqueue_pending(p)
    p.wait_for_steward()
    with p.store.connect() as db:
        assert db.execute('SELECT count(*) FROM memory_episodes WHERE job_id=?', (job['id'],)).fetchone()[0] == count
        trace = json.loads(db.execute('SELECT trace_json FROM memory_jobs WHERE id=?', (job['id'],)).fetchone()[0])
        assert not trace['followups_pending']
        assert db.execute("SELECT status FROM inner_update_runs WHERE trigger_ref=?", (f"memory_job:{job['id']}",)).fetchone()[0] == 'completed'


def test_stop_preserves_entire_embedding_index_for_retry(pipeline, monkeypatch):
    from cbs.reindex import reindex
    from cbs.llm import FakeLLMClient
    p = pipeline
    with p.store.connect() as db:
        old = db.execute('SELECT model FROM embedding_index').fetchone()[0]
        db.execute("INSERT INTO interests(character_id,topic,kind,detail,origin,status,first_seen,last_seen,embedding) "
                   "VALUES ('fixture','synthetic','hobby','synthetic','user','active','2026-10-20','2026-10-20',?)", (b'old vector',))
        before = db.execute('SELECT id,embedding FROM interests ORDER BY id').fetchall()
    original = FakeLLMClient.embed
    def stop(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        p.store.upkeep_stop.set()
        return result
    monkeypatch.setattr(FakeLLMClient, 'embed', stop)
    run_upkeep(p.store, reindex, p.store, p.client, p.config)
    with p.store.connect() as db:
        assert db.execute('SELECT model FROM embedding_index').fetchone()[0] == old
        assert db.execute('SELECT id,embedding FROM interests ORDER BY id').fetchall() == before
    monkeypatch.setattr(FakeLLMClient, 'embed', original)
    p.store.upkeep_stop.clear()
    assert run_upkeep(p.store, reindex, p.store, p.client, p.config)['completed'] >= len(before)


def test_shutdown_reindex_is_never_reported_completed(pipeline, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from cbs.llm import FakeLLMClient
    entered, release = Event(), Event()
    original = FakeLLMClient.embed
    def slow(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(FakeLLMClient, 'embed', slow)
    app = create_app(pipeline, settings_dir=tmp_path)
    with TestClient(app) as client:
        assert client.post('/api/maintenance/reindex').status_code == 202
        try:
            assert entered.wait(5)
            app.state.shutdown_clock.begin()
        finally:
            release.set()
    assert app.state.reindex['status'] != 'completed'


def test_shared_clock_counts_uvicorn_and_lifespan_and_overwrites_log(tmp_path, monkeypatch):
    import cbs.shutdown as module
    monkeypatch.setattr(module, 'SHUTDOWN_SECONDS', .12)
    (tmp_path / 'shutdown.log').write_text('previous run', encoding='ascii')
    timer = Shutdown(tmp_path, Event())
    async def drain():
        timer.begin()
        await timer.stage('uvicorn', asyncio.sleep(.07))
        await timer.stage('sweep', asyncio.sleep(30))
        await timer.stage('backup', asyncio.sleep(30))
        timer.finish()
    started = monotonic()
    asyncio.run(drain())
    assert monotonic() - started < .5
    text = (tmp_path / 'shutdown.log').read_text()
    assert 'sweep_cutoff' in text and 'backup_cutoff' in text
    assert 'previous' not in text
    assert all(re.fullmatch(r'[a-z_]+ \d+\.\d{3}', line) for line in text.splitlines())


def test_lifespan_bounds_all_slow_resources_and_keeps_log_private(pipeline, tmp_path, monkeypatch):
    import cbs.shutdown as module
    monkeypatch.setattr(module, 'SHUTDOWN_SECONDS', .2)
    app = create_app(pipeline, settings_dir=tmp_path)
    release, entered = Event(), Event()
    def slow():
        entered.set()
        release.wait(5)
    async def run():
        async with app.router.lifespan_context(app):
            for name in ['voice_download', 'embedding_download', 'stt_download', 'stt_server', 'tts', 'backup']:
                target = getattr(app.state, name)
                monkeypatch.setattr(target, 'stop' if name == 'tts' else 'close', slow)
        assert entered.is_set()
    started = monotonic()
    try:
        # asyncio.run's worker join is deliberately outside the lifespan limit.
        # The real host watchdog covers it; release here for test isolation.
        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        assert monotonic() - started < .8
    finally:
        release.set()
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
    log = (tmp_path / 'shutdown.log').read_text()
    for name in ['voice_download', 'embedding_download', 'stt_download', 'stt_server', 'tts', 'backup']:
        assert name + '_cutoff' in log
    assert all(re.fullmatch(r'[a-z_]+ \d+\.\d{3}', line) for line in log.splitlines())


def test_process_deadline_includes_executor_join_and_sqlite_recovers(tmp_path):
    # Real child process, no HTTP/provider. A hanging default executor and an
    # open SQLite transaction model the native call that asyncio.cancel cannot kill.
    script = r'''
import asyncio, os, sqlite3, sys
from pathlib import Path
from threading import Event
import cbs.shutdown as module
module.SHUTDOWN_SECONDS = .35
root = Path(sys.argv[1])
db = sqlite3.connect(root / 'open.sqlite3')
db.execute('CREATE TABLE sample(value TEXT)')
db.execute("INSERT INTO sample VALUES ('committed')")
db.commit()
timer = module.Shutdown(root, Event())
timer.exit_process = os._exit
def hanging():
    db = sqlite3.connect(root / 'open.sqlite3')
    db.execute("INSERT INTO sample VALUES ('partial')")
    Event().wait(60)
async def run():
    timer.begin()
    await timer.stage('blocked', asyncio.to_thread(hanging), limit=.1)
    timer.start('python_workers')
asyncio.run(run())
raise AssertionError('watchdog did not exit')
'''
    started = monotonic()
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert monotonic() - started < 3
    log = (tmp_path / 'shutdown.log').read_text()
    assert 'blocked_cutoff' in log and 'python_workers_cutoff' in log and 'process_cutoff' in log
    import sqlite3
    with sqlite3.connect(tmp_path / 'open.sqlite3') as db:
        assert db.execute('SELECT value FROM sample').fetchall() == [('committed',)]
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_real_uvicorn_wait_and_lifespan_share_clock(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from cbs.web.__main__ import bounded_server
    import uvicorn
    import cbs.shutdown as module
    timer = Shutdown(tmp_path, Event())
    app = SimpleNamespace(state=SimpleNamespace(shutdown_clock=timer))
    monkeypatch.setattr(module, 'SHUTDOWN_SECONDS', .15)
    server = bounded_server(uvicorn.Config(app, timeout_graceful_shutdown=.06, log_config=None))
    server.servers = []
    async def pending():
        await asyncio.sleep(60)
    async def lifespan():
        timer.end('uvicorn')
        await timer.stage('cleanup', asyncio.sleep(60))
    server.lifespan = SimpleNamespace(shutdown=lifespan)
    async def run():
        task = asyncio.create_task(pending())
        server.server_state.tasks.add(task)
        await server.shutdown()
        await asyncio.gather(task, return_exceptions=True)
        timer.finish()
    started = monotonic()
    asyncio.run(run())
    assert monotonic() - started < .5
    assert 'cleanup_cutoff' in (tmp_path / 'shutdown.log').read_text()


def test_main_disarms_watchdog_before_returning_to_host(pipeline, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from cbs.web import __main__ as main
    from time import sleep
    import cbs.shutdown as module
    app = create_app(pipeline, settings_dir=tmp_path)
    exits = []
    monkeypatch.setattr(module, 'SHUTDOWN_SECONDS', 1)
    monkeypatch.setattr(main.os, '_exit', exits.append)
    monkeypatch.setattr(main, 'create_app', lambda **kwargs: app)
    monkeypatch.setattr(sys, 'argv', ['cbs.web', '--db', str(tmp_path / 'cbs.sqlite3')])
    class Server:
        def __init__(self, config):
            self.config = config
        def run(self):
            with TestClient(app):
                pass
    monkeypatch.setattr(main.uvicorn, 'Server', Server)
    main.main()
    assert app.state.shutdown_clock.finished.is_set()
    sleep(1.1)
    assert not exits
    assert 'executor_join' in (tmp_path / 'shutdown.log').read_text()
