"""D2: optional, local developer research. Nothing here supplies application state."""
from contextlib import contextmanager, closing
from datetime import timedelta
from functools import wraps
from functools import lru_cache
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
from threading import Lock, RLock, Thread
from time import monotonic
import tomllib
import uuid
import zlib

from cbs import clock
from cbs.config import ROOT

SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL, zlib BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS records (
 id INTEGER PRIMARY KEY, turn_id INTEGER NOT NULL, session_id TEXT, conversation_id INTEGER,
 character_id TEXT, created_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
 app_version TEXT, git_commit TEXT, prompt_hashes TEXT NOT NULL,
 system_sha256 TEXT NOT NULL REFERENCES blobs(sha256), messages_zlib BLOB NOT NULL,
 input_sha256 TEXT NOT NULL, decision_questions_zlib BLOB,
 live_used TEXT, live_reason TEXT, decision_model TEXT, decision_route TEXT, judge_model TEXT,
 assistant_text TEXT, check_parsed TEXT, rewrite INTEGER NOT NULL,
 shadow_status TEXT NOT NULL DEFAULT 'pending', shadow_attempts INTEGER NOT NULL DEFAULT 0,
 shadow_next_at TEXT, source_deleted_at TEXT, source_deleted_reason TEXT,
 UNIQUE(turn_id,session_id,created_at)
);
CREATE INDEX IF NOT EXISTS research_pending ON records(shadow_status,shadow_next_at,id);
CREATE TABLE IF NOT EXISTS judgments (
 id INTEGER PRIMARY KEY, record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
 judge_key TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL, ok INTEGER NOT NULL,
 error_kind TEXT, latency_ms REAL, cost_usd REAL, parsed TEXT, raw TEXT, projected TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
 id INTEGER PRIMARY KEY, record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
 field TEXT NOT NULL, label TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS source_deletions (
 turn_id INTEGER NOT NULL, session_id TEXT NOT NULL, created_at TEXT NOT NULL, at TEXT NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(turn_id,session_id,created_at)
);
INSERT OR IGNORE INTO meta VALUES ('version',1),('record_failures',0),('shadow_failures',0);
"""
LABELS = ('decision が妥当', 'judge が妥当', 'どちらも違う', 'どちらでもよい', '分からない')


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def packed(value):
    return zlib.compress(dumps(value).encode('utf-8'))


def unpacked(value):
    return json.loads(zlib.decompress(value))


def clean(client, value):
    """Use the existing credential sanitizer on every string, including nested JSON strings."""
    if isinstance(value, str):
        return client.sanitize_text(value)
    if isinstance(value, dict):
        return {client.sanitize_text(str(k)): clean(client, v) for k, v in value.items()
                if not re.search(r'(?i)^(authorization|proxy.authorization|headers?|credentials?|api[_-]?key|access[_-]?token)$', str(k))}
    if isinstance(value, (list, tuple)):
        return [clean(client, v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def raw_answers(answers):
    # Only the Decision answer contract, never an arbitrary provider response.
    allowed = {'type', 'choice', 'score', 'noul', 'probabilities', 'confidence'}
    return {k: {f: v for f, v in answer.items() if f in allowed}
            for k, answer in (answers or {}).items() if isinstance(answer, dict)}


def projected(parsed, ctx):
    """App-owned port of J1 baseline_values; private candidates add their fourth feature."""
    out = {'emotional_weight': str(['low', 'medium', 'high'].index(parsed['emotional_weight'])),
           'asks_about_past': parsed['past_reference']['asks_about_past'],
           'user_disclosure': parsed['user_disclosure'] == 'yes',
           'reveal_secret': (parsed['reveal_request'] or {}).get('secret_id', 'none'),
           'reveal_manner': (parsed['reveal_request'] or {}).get('manner', 'casual')}
    domains = {d['id']: d['strength'] for d in parsed['domains_touched']}
    for i, d in enumerate(ctx['domains']):
        out[f'domain_{i}'] = domains.get(d['id'], 'none')
    for i, s in enumerate(ctx['signatures']):
        out[f'signature_{i}'] = s['id'] in parsed['signatures_relevant']
    targets = {e['life_ref']: e for e in parsed['past_reference']['targets']}
    for i, e in enumerate(ctx['life_history_elements']):
        t = targets.get(e['id'])
        out[f'past_{i}'] = ('known' if t['known'] else 'unknown') + ('_major' if t['major'] else '_minor') if t else 'none'
    memories = {m['ref']: m for m in parsed['memory_use']}
    for i, m in enumerate(ctx['candidate_memories']):
        for field in ('relevant_now', 'counterevidence', 'continuation', *(['private_topic_current'] if m.get('private') else [])):
            out[f'memory_{i}_{field}'] = memories.get(m['ref'], {}).get(field, 'no')
    return out


def cloud_path(path):
    name = str(Path(path).resolve()).casefold().replace(' ', '').replace('_', '').replace('-', '')
    return any(part in name for part in ('onedrive', 'googledrive', 'googleドライブ', 'dropbox', 'icloud', 'boxsync'))


def blocked_path(path, config):
    if cloud_path(path):
        return 'クラウド同期フォルダのため記録を停止しています。'
    if config.backup.folder and Path(path).resolve().is_relative_to(Path(config.backup.folder).resolve()):
        return 'バックアップ先の下にあるため記録を停止しています。'
    return None


class ResearchDB:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = RLock()

    @contextmanager
    def connect(self, *, create=False):
        with self.lock:
            if create:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            uri = self.path.resolve().as_uri() + ('?mode=rwc' if create else '?mode=rw')
            with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                if create:
                    db.executescript(SCHEMA)
                with db:
                    yield db

    def blob(self, db, kind, value):
        data = dumps(value).encode('utf-8')
        digest = sha256(data).hexdigest()
        db.execute('INSERT OR IGNORE INTO blobs VALUES (?,?,?)', (digest, kind, zlib.compress(data)))
        return digest

    def judgment(self, db, record_id, model, source, *, parsed=None, raw=None, ctx=None,
                 error_kind=None, latency_ms=None, cost_usd=None):
        db.execute('INSERT INTO judgments(record_id,judge_key,source,created_at,ok,error_kind,latency_ms,cost_usd,parsed,raw,projected) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (record_id, judge_key(model), source, clock.iso(), int(parsed is not None and not error_kind), error_kind,
                    latency_ms, cost_usd, dumps(parsed) if parsed is not None else None,
                    dumps(raw_answers(raw)) if raw is not None else None,
                    dumps(projected(parsed, ctx)) if parsed is not None else None))

    def input(self, record):
        with self.connect() as db:
            hashes = json.loads(record['prompt_hashes'])
            blobs = {r['sha256']: unpacked(r['zlib']) for r in db.execute('SELECT * FROM blobs WHERE sha256 IN (' +
                     ','.join('?' for _ in hashes) + ')', list(hashes.values()))}
        return {'system': blobs[record['system_sha256']], 'messages': unpacked(record['messages_zlib']),
                'questions': unpacked(record['decision_questions_zlib']),
                'choices': blobs[hashes['decision_choices']], 'past_text': blobs[hashes['decision_past_text']]}

    def delete(self, before=None):
        with self.connect() as db:
            if before:
                db.execute('DELETE FROM source_deletions WHERE created_at<?', (before,))
            else:
                db.execute('DELETE FROM source_deletions')
            db.execute('DELETE FROM records' + (' WHERE created_at<?' if before else ''), (before,) if before else ())
            used = {v for row in db.execute('SELECT prompt_hashes FROM records') for v in json.loads(row[0]).values()}
            for row in db.execute('SELECT sha256 FROM blobs').fetchall():
                if row[0] not in used:
                    db.execute('DELETE FROM blobs WHERE sha256=?', (row[0],))
        with self.connect() as db:
            db.execute('VACUUM')


def judge_key(model):
    provider, _, bare = model.partition('/')
    return f'{bare}@{provider}'


@lru_cache(maxsize=1)
def startup_version():
    version, commit = 'unknown', None
    if (ROOT / '.git').exists():
        try:
            version = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
            commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True,
                                    text=True, timeout=2, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            pass
    return version, commit


def activity(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        research = getattr(self.store, 'research', None) or attach(self)
        with research.activity():
            return method(self, *args, **kwargs)
    return call


def attach(host):
    if not hasattr(host.store, 'research'):
        host.store.research = Research(lambda: host.config, lambda: host.client, host.store.upkeep_stop,
                                       lambda: getattr(host.store, 'costs', getattr(host.client, 'costs', None)))
    host.store.research.start()
    return host.store.research


class Research:
    def __init__(self, config, client, stop, costs=None):
        self.config, self.client, self.stop = config, client, stop
        self.costs = costs or (lambda: None)
        self.db = ResearchDB(config()._data_dir / 'research' / 'decisions.sqlite3')
        self.error = None
        self.record_failures = 0
        self.lock, self.work_lock = Lock(), Lock()
        self.active, self.last_end, self.next_work = 0, monotonic(), 0
        self.calls = {}
        self.thread = None
        self.app_version, self.git_commit = startup_version()
        if self.enabled() and self.db.path.exists():
            self.safely(self._check)

    def enabled(self):
        return (ROOT / '.git').exists() and self.config().research.enabled and not blocked_path(self.db.path, self.config()) and not self.error

    def _check(self):
        with self.db.connect() as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise sqlite3.DatabaseError()
            db.execute('SELECT * FROM meta').fetchall()

    def safely(self, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            # Never copy provider exceptions or paths containing credentials into status/logs.
            self.error = '判断の記録を開けません。ファイルと保存先を確認してください。'
            return None

    @contextmanager
    def activity(self):
        with self.lock:
            self.active += 1
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1
                self.last_end = monotonic()

    def snapshot(self, request, parsed, decision, capture, *, live_used, latency_ms=None, usage=None):
        if not self.enabled():
            return None
        try:
            from cbs.decision import questions_for
            from cbs.runtime import read_prompt
            cfg = self.config()
            ctx = json.loads(request['messages'][-1]['content'])
            from cbs.llm import RoutingLLMClient
            route = RoutingLLMClient(cfg)
            qualified = lambda m: '/'.join(route._split(m)) if m else ''
            from cbs.costs import estimate
            live_model = cfg.models.decision if live_used == 'decision' else cfg.models.judge
            cost = estimate(*route._split(live_model), usage or {}, cfg.costs)[1]
            if live_used == 'decision' and decision.get('past_reference'):
                extra = estimate(*route._split(cfg.models.judge), decision['past_reference'].get('usage', {}), cfg.costs)[1]
                cost = cost + extra if cost is not None and extra is not None else None
            return clean(self.client(), {'request': request, 'parsed': parsed,
                'decision': decision, 'raw': raw_answers(capture.get('raw')),
                'questions': capture.get('questions') or questions_for(ctx),
                'prompts': {name: read_prompt(name) for name in ('decision_questions', 'decision_choices', 'decision_past_text')},
                'decision_model': qualified(cfg.models.decision), 'judge_model': qualified(cfg.models.judge),
                'live_used': live_used, 'latency_ms': latency_ms, 'cost_usd': cost,
                'live_reason': (decision.get('failure_kind') or decision.get('skip_reason') or '未設定') if live_used == 'judge' else None})
        except Exception:
            self.failed_record()
            return None

    def failed_record(self):
        self.record_failures += 1
        try:
            with self.db.connect(create=True) as db:
                db.execute("UPDATE meta SET value=value+1 WHERE key='record_failures'")
        except Exception:
            self.error = '判断の記録を開けません。ファイルと保存先を確認してください。'

    def record(self, snapshot, refs, trace, reply):
        if snapshot is None or not self.enabled():
            return
        try:
            snap, refs, trace, reply = clean(self.client(), [snapshot, refs, trace, reply])
            request = snap['request']
            ctx = json.loads(request['messages'][-1]['content'])
            with self.db.connect(create=True) as db:
                hashes = {name: self.db.blob(db, name, text) for name, text in snap['prompts'].items()}
                hashes['pre_judge'] = system_sha = self.db.blob(db, 'pre_judge', request['system'])
                hashes['PreJudge'] = self.db.blob(db, 'PreJudge', request['schema'])
                cursor = db.execute('INSERT OR IGNORE INTO records(turn_id,session_id,conversation_id,character_id,created_at,recorded_at,app_version,git_commit,'
                    'prompt_hashes,system_sha256,messages_zlib,input_sha256,decision_questions_zlib,live_used,live_reason,decision_model,decision_route,judge_model,'
                    'assistant_text,check_parsed,rewrite) VALUES (' + ','.join('?' for _ in range(21)) + ')',
                    (refs['turn_id'], refs['session_id'], refs['conversation_id'], refs['character_id'], refs['created_at'], clock.iso(),
                     self.app_version, self.git_commit, dumps(hashes), system_sha, packed(request['messages']),
                     sha256(dumps([request['system'], request['messages']]).encode('utf-8')).hexdigest(), packed(snap['questions']),
                     snap['live_used'], snap['live_reason'], snap['decision_model'], snap['decision_model'].partition('/')[0], snap['judge_model'],
                     reply, dumps({k: v.get('response', {}).get('parsed') for k, v in trace.items() if k.startswith('Check')}),
                     int(bool(trace.get('Safety', {}).get('rewrite_count') or 'Rewrite' in trace))))
                if cursor.rowcount:
                    record_id = cursor.lastrowid
                    db.execute('UPDATE records SET source_deleted_at=(SELECT at FROM source_deletions d WHERE '
                               'd.turn_id=records.turn_id AND d.session_id=records.session_id AND d.created_at=records.created_at),'
                               'source_deleted_reason=(SELECT reason FROM source_deletions d WHERE '
                               'd.turn_id=records.turn_id AND d.session_id=records.session_id AND d.created_at=records.created_at) WHERE id=?',
                               (record_id,))
                    model = snap['decision_model'] if snap['live_used'] == 'decision' else snap['judge_model']
                    self.db.judgment(db, record_id, model, 'live', parsed=snap['parsed'], ctx=ctx,
                                     raw=snap['raw'] if snap['live_used'] == 'decision' else None,
                                     error_kind='pre_judge_failed' if trace.get('Pre-Judge-Retry', {}).get('fallback') else None,
                                     latency_ms=snap['latency_ms'], cost_usd=snap['cost_usd'])
                    if snap['live_used'] == 'judge' and snap['raw']:
                        self.db.judgment(db, record_id, snap['decision_model'], 'live', raw=snap['raw'],
                                         error_kind=snap['decision'].get('failure_kind') or 'assembly')
        except Exception:
            self.failed_record()

    def mark_deleted(self, turns, reason):
        if not (ROOT / '.git').exists() or not (self.db.path.exists() or self.enabled()):
            return
        def mark():
            with self.db.connect(create=self.enabled()) as db:
                db.executemany('INSERT OR REPLACE INTO source_deletions VALUES (?,?,?,?,?)',
                               [(t['turn_id'], t['session_id'], t['created_at'], clock.iso(), reason) for t in turns])
                db.executemany('UPDATE records SET source_deleted_at=?,source_deleted_reason=? WHERE turn_id=? AND session_id=? AND created_at=?',
                               [(clock.iso(), reason, t['turn_id'], t['session_id'], t['created_at']) for t in turns])
        self.safely(mark)

    def status(self):
        cfg = self.config()
        result = {**cfg.research.model_dump(), 'count': 0, 'bytes': 0, 'pending': 0, 'failed': 0,
                  'last_recorded_at': None, 'record_failures': self.record_failures, 'shadow_failures': 0,
                  'reason': self.error or blocked_path(self.db.path, cfg), 'large': False}
        if not (ROOT / '.git').exists():
            return {**result, 'enabled': False, 'shadow': False, 'reason': '開発版でだけ使えます。'}
        if self.db.path.exists() and not result['reason']:
            def read():
                with self.db.connect() as db:
                    result.update(dict(db.execute('SELECT key,value FROM meta')))
                    row = db.execute("SELECT count(*) AS count,max(recorded_at) AS last_recorded_at,"
                                     "sum(shadow_status='pending') AS pending,sum(shadow_status='failed') AS failed FROM records").fetchone()
                    result.update(dict(row))
                    result['pending'], result['failed'] = result['pending'] or 0, result['failed'] or 0
                result['bytes'] = sum(p.stat().st_size for p in self.db.path.parent.glob('decisions.sqlite3*'))
                result['large'] = result['bytes'] > 500 * 1024**2
            self.safely(read)
            result['reason'] = self.error
        return result

    def start(self):
        if self.enabled() and self.config().research.shadow:
            with self.lock:
                if self.thread is None or not self.thread.is_alive():
                    self.thread = Thread(target=self._loop, name='shadow-judge', daemon=True)
                    self.thread.start()

    def _loop(self):
        from cbs.shutdown import run_upkeep
        # run_upkeep only needs this stop event; no Store or app DB enters the worker.
        while not self.stop.wait(1):
            run_upkeep(self, self.safely, self.work_once)

    @property
    def upkeep_stop(self):
        return self.stop

    def permitted(self):
        costs = self.costs()
        with self.lock:
            return (self.enabled() and self.config().research.shadow and not self.stop.is_set()
                    and not self.active and monotonic() - self.last_end >= 60 and monotonic() >= self.next_work
                    and not (costs is not None and costs.paused()))

    def guard(self, model):
        from cbs.shutdown import UpkeepStopped
        # A fixed-size history per model; actual Decision batches also pass this gate.
        while True:
            if self.stop.is_set() or not self.enabled() or not self.config().research.shadow:
                raise UpkeepStopped()
            costs = self.costs()
            if costs is not None and costs.paused():
                raise UpkeepStopped()
            now = monotonic()
            history = self.calls[model] = [t for t in self.calls.get(model, []) if now-t < 60]
            if len(history) < 6:
                history.append(now)
                return
            if self.stop.wait(min(1, max(.01, 60-(now-history[0])))):
                raise UpkeepStopped()

    def work_once(self):
        if not self.work_lock.acquire(blocking=False):
            return False
        attempted = False
        try:
            if not self.permitted() or not self.db.path.exists():
                return False
            with self.db.connect() as db:
                row = db.execute("SELECT * FROM records WHERE shadow_status='pending' AND (shadow_next_at IS NULL OR shadow_next_at<=?) ORDER BY id LIMIT 1",
                                 (clock.iso(),)).fetchone()
            if row is None:
                return False
            attempted = True
            record = dict(row)
            cfg = self.config().model_copy(deep=True)
            model = record['judge_model'] if record['live_used'] == 'decision' else record['decision_model'] or decision_model(cfg)
            from cbs.config import key_set
            provider = model.partition('/')[0]
            from cbs.llm import FakeLLMClient, RoutingLLMClient
            original = self.client()
            unavailable = not model or (record['live_used'] == 'judge' and provider == 'openrouter' and cfg.llm.zdr_only)
            if not isinstance(original, FakeLLMClient) and not unavailable:
                unavailable = not key_set(cfg, provider)
            if unavailable:
                with self.db.connect() as db:
                    db.execute("UPDATE records SET shadow_status='skipped' WHERE id=?", (record['id'],))
                return True
            # Never reuse a Routing client bound to FailureState (which writes app DB).
            if isinstance(original, FakeLLMClient):
                from copy import copy
                client = copy(original)
                client.__dict__.pop('failures', None)
            else:
                client = RoutingLLMClient(cfg, credentials=original._credentials)
            client.costs, client._cost_config = self.costs(), cfg
            first = True
            def guard(model):
                nonlocal first
                if first:
                    from cbs.shutdown import UpkeepStopped
                    if not self.permitted():
                        raise UpkeepStopped()
                    first = False
                self.guard(model)
            result = evaluate(self.db, record, cfg, client, model, decision=record['live_used'] == 'judge', guard=guard)
            if self.stop.is_set():
                return False
            with self.db.connect() as db:
                if self.stop.is_set():
                    return False
                if not db.execute('SELECT 1 FROM records WHERE id=? AND recorded_at=? AND input_sha256=?',
                                  (record['id'], record['recorded_at'], record['input_sha256'])).fetchone():
                    return False  # Manually removed while the network call was in flight.
                save_evaluation(self.db, db, record, result, 'shadow')
                attempts = record['shadow_attempts'] + 1
                if result['error_kind']:
                    db.execute("UPDATE meta SET value=value+1 WHERE key='shadow_failures'")
                    retry = result['retryable'] and attempts <= 3
                    next_at = (clock.now() + timedelta(seconds=(300, 1800, 7200)[attempts-1])).isoformat() if retry else None
                    status = 'pending' if retry else 'failed'
                else:
                    status, next_at = 'done', None
                db.execute('UPDATE records SET shadow_status=?,shadow_attempts=?,shadow_next_at=? WHERE id=?',
                           (status, attempts, next_at, record['id']))
            return True
        finally:
            # Space out records only. Pushing this on every idle poll (once a
            # second) would keep permitted() false forever.
            if attempted:
                self.next_work = monotonic() + 5
            self.work_lock.release()

    def export(self, folder, *, cloud_confirmed=False):
        if not (ROOT / '.git').exists() or self.error or blocked_path(self.db.path, self.config()):
            raise ValueError('判断の記録を利用できません。')
        target = Path(folder).resolve()
        if not target.is_dir():
            raise ValueError('書き出し先のフォルダを確認してください。')
        if cloud_path(target) and not cloud_confirmed:
            raise ValueError('cloud_confirmation_required')
        path = target / f'decisions-{uuid.uuid4().hex}.sqlite3'
        try:
            with self.db.connect() as src, closing(sqlite3.connect(path)) as dst:
                src.backup(dst)
        except Exception:
            path.unlink(missing_ok=True)
            raise ValueError('判断の記録を書き出せませんでした。') from None
        return path.name


def decision_model(config):
    from cbs.config import key_set
    if config.models.decision:
        return config.models.decision
    if key_set(config, 'gemini'):
        return 'gemini/gemini-decision-1'
    if key_set(config, 'openrouter'):
        return 'openrouter/google/gemini-decision-1'
    return ''


def evaluate(db, record, config, client, model, *, decision=False, guard=None):
    """Shared by the isolated shadow worker and the explicitly invoked replay CLI."""
    from cbs.decision import prejudge
    from cbs.llm.cost import for_step
    from cbs.llm.decision import request_guard
    from cbs.pipeline import PreJudge
    from cbs.shutdown import UpkeepStopped
    from copy import copy
    from cbs.costs import estimate
    delegate = getattr(client, 'costs', None)
    amounts = []
    class Costs:
        def paused(self):
            return delegate is not None and delegate.paused()

        def record(self, provider, model, role, purpose, usage):
            amounts.append(estimate(provider, model, usage, config.costs)[1])
            if delegate is not None:
                delegate.record(provider, model, role, purpose, usage)
    client = copy(client)
    client.__dict__.pop('failures', None)
    client._cost_default = False
    client._cost_config, client.costs = config, Costs()
    data = db.input(record)
    ctx = json.loads(data['messages'][-1]['content'])
    capture, metadata = {}, {}
    result = {'model': model, 'ctx': ctx, 'parsed': None, 'raw': None, 'error_kind': None,
              'retryable': False, 'latency_ms': None, 'cost_usd': None}
    token = request_guard.set(guard)
    started = monotonic()
    try:
        if decision:
            config = config.model_copy(deep=True)
            config.models.decision = model
            response = prejudge(client, config, data['system'], data['messages'], metadata, capture=capture,
                                purpose='research', questions=data['questions'], choices=data['choices'], past_text=data['past_text'])
        else:
            if guard:
                guard(model)
            response = for_step(client, 'pre_judge', 'research').generate_json(model, data['system'], data['messages'], PreJudge)
        result['parsed'] = response.parsed.model_dump(mode='json')
    except UpkeepStopped:
        raise
    except Exception as error:
        import httpx
        status = getattr(error, 'status_code', None)
        kind = metadata.get('failure_kind') or getattr(error, 'kind', None) or 'schema'
        result['error_kind'] = f'http_{status}' if isinstance(status, int) else kind
        result['retryable'] = (status == 429 or isinstance(status, int) and 500 <= status < 600 or
                               kind in ('timeout', 'transport', 'network') or isinstance(error, (TimeoutError, ConnectionError, httpx.TransportError)))
    finally:
        request_guard.reset(token)
    result['raw'] = raw_answers(capture.get('raw')) if decision else None
    result['cost_usd'] = sum(amounts) if amounts and all(a is not None for a in amounts) else None
    result['latency_ms'] = round((monotonic()-started)*1000, 2)
    return clean(client, result)


def save_evaluation(research_db, connection, record, result, source):
    research_db.judgment(connection, record['id'], result['model'], source, parsed=result['parsed'], raw=result['raw'],
                         ctx=result['ctx'], error_kind=result['error_kind'], latency_ms=result['latency_ms'], cost_usd=result['cost_usd'])
