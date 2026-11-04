"""BK1 snapshots. No provider clients, foreground locks, or exception text."""
from contextlib import closing, contextmanager, nullcontext
from contextvars import copy_context
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import struct
import tempfile
from threading import Condition, Thread
import time
import tomllib
from uuid import uuid4
import zipfile

from cbs import clock

SCHEMA_VERSION = 6  # AU1 research candidates and explicit activity interest links.
FORMAT_VERSION = 1
MAGIC = b'AWALIA-BK1\x00'
OWNER = 'awalia-bk1'
ERROR = 'バックアップに失敗しました。空き容量・フォルダの権限・人物ファイルを確認してください。'


def error_type(error):
    """Only the exception's class name is kept as a clue; its message may carry paths or secrets."""
    name = type(error).__name__
    return name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name) else 'Exception'
# Credential-shaped names only: the whole name or its last segment ("api_key", "refresh-token", "password"),
# not counts such as "completion_tokens" or words that merely contain "key".
SECRET_NAME = re.compile(r'(?:^|[_-])(?:api[_-]?key|key|secret|password|passwd|signature|credential|credentials|'
                         r'authorization|cookie|access[_-]?token|refresh[_-]?token|auth[_-]?token|id[_-]?token|token|private[_-]?key)$', re.I)
# Provider key prefixes, bearer/JWT and private keys. Plain hex digests (file SHA-256s) are not secrets here.
SECRET_VALUE = re.compile(r'(?i)(?:\b(?:sk-|AIza|Bearer\s|gh[pousr]_|eyJ)[A-Za-z0-9_./+\-=]{8,}|-----BEGIN .*PRIVATE KEY|[a-z0-9_-]{24,}\.[a-z0-9_-]{24,}\.[a-z0-9_-]{10,})')
OPAQUE = re.compile(r'[A-Za-z0-9_+/=-]{32,}')
EXCLUDED = {'models', 'cache', 'tts_cache', 'audio_cache', '__pycache__', '.git', 'probes', 'probe', 'tests', 'test', 'dev', 'tmp', '.tmp', 'backup', 'node_modules'}
EXCLUDED.add('first-run')


def linked(path):
    return path.is_symlink() or path.is_junction()


def retry_locked(function, *args, **kwargs):
    """Windows scanners/indexers briefly hold just-written files; wait (up to ~2.5 s) instead of failing."""
    for attempt in range(7):
        try:
            return function(*args, **kwargs)
        except PermissionError as error:
            if getattr(error, 'winerror', None) not in {5, 32, 33} or attempt == 6:
                raise
            time.sleep(.02 * 2 ** attempt)


@contextmanager
def temporary_directory(directory):
    path = Path(tempfile.mkdtemp(prefix='.awalia-bk1-', dir=directory))
    try:
        yield path
    finally:
        retry_locked(shutil.rmtree, path)


def key_like(value):
    if not isinstance(value, str):
        return False
    if SECRET_VALUE.search(value) or re.search(r'https?://[^/\s]*@|[?&](?:key|token|sig|auth)=', value, re.I):
        return True
    # Long opaque strings mixing digits, upper and lower case look like keys; lowercase hex ids do not.
    return any(all((any(c.isdigit() for c in t), any(c.isupper() for c in t), any(c.islower() for c in t)))
               for t in OPAQUE.findall(value))


def clean_settings(value, *, references=()):
    """Drop the entire item containing a credential-shaped value, including lists."""
    if isinstance(value, dict):
        return {k: clean_settings(v, references=references) for k, v in value.items()
                if ((k in {'reference', 'sample_reference', 'id'} and isinstance(v, str) and v in references)
                    or (k == 'api_key_env' and isinstance(v, str) and re.fullmatch(r'[A-Z][A-Z0-9_]*', v) and not key_like(v))
                    or (not (SECRET_NAME.search(k) and isinstance(v, str)) and not key_like(k) and not contains_key(v)))}
    if isinstance(value, list):
        return [clean_settings(v, references=references) for v in value]
    return value


def contains_key(value):
    if isinstance(value, dict):
        return False  # Each child is filtered independently.
    if isinstance(value, list):
        return any(contains_key(v) for v in value)
    return key_like(value)


def toml_bytes(value):
    """Small TOML writer, including config's arrays of tables (no new package)."""
    lines = []
    scalar = lambda v: str(v).lower() if isinstance(v, bool) else (v.isoformat() if hasattr(v, 'isoformat') else json.dumps(v, ensure_ascii=False))
    def write(data, parts=(), array=False):
        if parts:
            title = '.'.join(json.dumps(k) for k in parts)
            lines.append(('[' * (2 if array else 1)) + title + (']' * (2 if array else 1)))
        for k, v in data.items():
            if not isinstance(v, dict) and not (isinstance(v, list) and v and isinstance(v[0], dict)):
                lines.append(json.dumps(k) + ' = ' + scalar(v))
        for k, v in data.items():
            if isinstance(v, dict):
                write(v, (*parts, k))
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                for item in v:
                    write(item, (*parts, k), True)
    write(value)
    result = ('\n'.join(lines) + '\n').encode('utf-8')
    tomllib.loads(result.decode('utf-8'))
    return result


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)


def snapshot_db(source, target):
    with closing(readonly(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst, pages=128, sleep=.01)
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('invalid snapshot')


def recorded_characters(db):
    ids = set()
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        quoted = '"' + table.replace('"', '""') + '"'
        if 'character_id' in {r[1] for r in db.execute(f'PRAGMA table_info({quoted})')}:
            ids.update(r[0] for r in db.execute(f'SELECT DISTINCT character_id FROM {quoted}') if r[0])
    if any(not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', cid) for cid in ids):
        raise ValueError('invalid character ID')
    return ids


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_zip(path, *, max_bytes=None, max_files=100000):
    with zipfile.ZipFile(path) as archive:
        items = archive.infolist()
        names = [item.filename for item in items]
        if len(items) > max_files or (max_bytes is not None and sum(i.file_size for i in items) > max_bytes):
            raise ValueError('archive too large')
        # Validate before reading/hashing any member, including the manifest.
        if archive.getinfo('manifest.json').file_size > 8 * 1024 * 1024:
            raise ValueError('manifest too large')
        folded = set()
        for item in items:
            name = item.filename
            parts = name.split('/')
            if (name.casefold() in folded or '\\' in name or any(
                    not p or p in {'.', '..'} or p.endswith((' ', '.')) or
                    re.search(r'[\x00-\x1f:<>"|?*]', p) or
                    re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?', p, re.I)
                    for p in parts) or (item.external_attr >> 16) & 0o170000 not in (0, 0o100000)):
                raise ValueError('invalid path')
            folded.add(name.casefold())
        manifest = json.loads(archive.read('manifest.json'))
        if manifest['format_version'] != FORMAT_VERSION or manifest['producer'] != OWNER:
            raise ValueError('invalid manifest')
        if len(names) != len(set(names)) or set(names) != {'manifest.json', *manifest['files']}:
            raise ValueError('invalid inventory')
        for name, item in manifest['files'].items():
            with archive.open(name) as stream:
                hashed = hashlib.file_digest(stream, 'sha256').hexdigest()
            if archive.getinfo(name).file_size != item['size'] or hashed != item['sha256']:
                raise ValueError('invalid hash')
    return manifest


def _crypt(source, target, password, *, decrypt=False):
    """Envelope v1: magic, BE header length, authenticated JSON, ciphertext, GCM tag.

    Parameters are fixed/bounded on reading. This is format verification, not restore.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    with source.open('rb') as src, target.open('wb') as dst:
        if decrypt:
            if src.read(len(MAGIC)) != MAGIC:
                raise ValueError('invalid envelope')
            length = struct.unpack('>I', src.read(4))[0]
            if not 1 <= length <= 1024:
                raise ValueError('invalid header')
            header = src.read(length)
            meta = json.loads(header)
            if {k: meta[k] for k in ('version', 'kdf', 'n', 'r', 'p', 'cipher')} != dict(version=1, kdf='scrypt', n=32768, r=8, p=1, cipher='AES-256-GCM'):
                raise ValueError('invalid parameters')
            salt, nonce = bytes.fromhex(meta['salt']), bytes.fromhex(meta['nonce'])
            if len(salt) != 16 or len(nonce) != 12:
                raise ValueError('invalid parameters')
            remaining = source.stat().st_size - src.tell() - 16
            if remaining < 0:
                raise ValueError('truncated envelope')
            src.seek(-16, 2)
            tag = src.read(16)
            src.seek(len(MAGIC) + 4 + length)
        else:
            salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
            header = json.dumps(dict(version=1, kdf='scrypt', n=32768, r=8, p=1, cipher='AES-256-GCM', salt=salt.hex(), nonce=nonce.hex()), separators=(',', ':')).encode()
            dst.write(MAGIC + struct.pack('>I', len(header)) + header)
            remaining = source.stat().st_size
        key = Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(password.encode('utf-8'))
        mode = modes.GCM(nonce, tag) if decrypt else modes.GCM(nonce)
        cipher = Cipher(algorithms.AES(key), mode)
        context = cipher.decryptor() if decrypt else cipher.encryptor()
        context.authenticate_additional_data(MAGIC + header)
        while remaining:
            chunk = src.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError('truncated payload')
            dst.write(context.update(chunk))
            remaining -= len(chunk)
        dst.write(context.finalize())
        if not decrypt:
            dst.write(context.tag)


def build_export(db_path, characters_root, data_dir, current_id, destination, *, password='', kind='manual', config_path=None):
    """Build/verify in a sibling temporary directory; publish by one atomic rename."""
    from cbs.first_run import trial_info
    if trial_info(data_dir):
        raise ValueError('お試しではバックアップを行いません。')
    destination, characters_root, data_dir = Path(destination), Path(characters_root), Path(data_dir)
    if destination.exists():
        raise FileExistsError('destination exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = data_dir / 'backup' / 'work' if password else destination.parent
    staging.mkdir(parents=True, exist_ok=True)
    # An encrypted export must never stage plaintext in a cloud-sync destination.
    with temporary_directory(staging) as temporary, \
            (temporary_directory(destination.parent) if password else nullcontext()) as publish:
        work = Path(temporary)
        snap = work / 'cbs.sqlite3'
        snapshot_db(db_path, snap)
        with closing(readonly(snap)) as db:
            ids = recorded_characters(db)
            version = db.execute('PRAGMA user_version').fetchone()[0]
            schema = '\n'.join(r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"))
        if current_id and (characters_root / current_id).is_dir():
            if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', current_id):
                raise ValueError('invalid current ID')
            ids.add(current_id)
        with (Path(__file__).resolve().parents[1] / 'pyproject.toml').open('rb') as stream:
            app_version = tomllib.load(stream)['project']['version']
        manifest = dict(format_version=FORMAT_VERSION, producer=OWNER, kind=kind, app_version=app_version,
                        schema_version=version, schema_sha256=hashlib.sha256(schema.encode()).hexdigest(),
                        created_at=clock.iso(), characters=sorted(ids), files={})
        plain = work / 'payload.zip'
        with zipfile.ZipFile(plain, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            def add(name, path=None, content=None):
                if content is not None:
                    archive.writestr(name, content)
                else:
                    archive.write(path, name)
                # Hash the bytes actually written, avoiding file-change races.
            add('data/cbs.sqlite3', snap)
            # Only snapshot-referenced normalized photos; never arbitrary files under data/.
            with closing(readonly(snap)) as photo_db:
                tables = {r[0] for r in photo_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                photo_ids = [r[0] for r in photo_db.execute('SELECT id FROM photos')] if 'photos' in tables else []
            for photo_id in photo_ids:
                if not re.fullmatch(r'[0-9a-f]{32}', photo_id):
                    raise ValueError('invalid photo ID')
                photo = Path(db_path).parent / 'photos' / (photo_id + '.jpg')
                if linked(photo.parent) or linked(photo) or not photo.is_file():
                    raise ValueError('missing or linked photo')
                add('data/photos/' + photo.name, photo)
            for cid in sorted(ids):
                directory = characters_root / cid
                if not directory.is_dir() or linked(directory):
                    raise ValueError('missing character')
                references = {p.name for p in (directory / 'voice').glob('*') if p.is_file() and not p.is_symlink()
                              and re.fullmatch(r'(?:clone-|sample-)?[0-9a-f]{32}\.(?:wav|gguf)', p.name)}
                for path in sorted(directory.rglob('*')):
                    relative = path.relative_to(directory)
                    if any(p.lower() in EXCLUDED or p.startswith('.') for p in relative.parts) or path.name.lower() in {'connection.json', 'secrets.env'}:
                        continue
                    if linked(path) or any(linked(p) for p in path.parents if p != characters_root.parent):
                        raise ValueError('linked character file')
                    if not path.is_file():
                        continue
                    name = 'characters/' + cid + '/' + relative.as_posix()
                    settings_file = path.suffix.lower() in {'.toml', '.json'} or path.name in {'persona.yaml', 'voice.yaml', 'look.yaml', 'expression.yaml'}
                    if settings_file and relative.parts[0] != 'source':
                        if path.suffix.lower() == '.toml':
                            value = tomllib.loads(path.read_text(encoding='utf-8'))
                            add(name, content=toml_bytes(clean_settings(value)))
                        else:
                            import yaml
                            value = yaml.safe_load(path.read_text(encoding='utf-8'))
                            clean = clean_settings(value, references=references if path.name in {'voice.yaml', 'references.json'} else ())
                            add(name, content=(json.dumps(clean, ensure_ascii=False).encode() if path.suffix.lower() == '.json'
                                               else yaml.safe_dump(clean, allow_unicode=True).encode()))
                    else:
                        add(name, path)
            for name, path in [('config.toml', config_path), ('data/settings.toml', data_dir / 'settings.toml')]:
                if path is not None and Path(path).is_file():
                    with Path(path).open('rb') as stream:
                        add(name, content=toml_bytes(clean_settings(tomllib.load(stream))))
        with zipfile.ZipFile(plain, 'a', compression=zipfile.ZIP_DEFLATED) as archive:
            for item in archive.infolist():
                with archive.open(item) as stream:
                    hashed = hashlib.file_digest(stream, 'sha256').hexdigest()
                manifest['files'][item.filename] = dict(size=item.file_size, sha256=hashed)
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        verify_zip(plain)
        result = plain
        if password:
            result = Path(publish) / 'payload.awalia'
            _crypt(plain, result, password)
            verified = work / 'verified.zip'
            try:
                _crypt(result, verified, password, decrypt=True)
                verify_zip(verified)
            finally:
                verified.unlink(missing_ok=True)
        retry_locked(result.replace, destination)
    return manifest


def validate_folder(folder):
    path = Path(folder).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('invalid folder')
    with tempfile.TemporaryFile(prefix='.awalia-write-', dir=path) as stream:
        stream.write(b'BK1')
        stream.flush()
    return str(path.resolve())


def prune(folder, kind, *, keep=None, days=7, weeks=4):
    """Only our exact names AND producer/kind manifests qualify for deletion."""
    candidates = []
    for path in Path(folder).glob(f'awalia-{kind}-*.zip'):
        if path.is_symlink() or not re.fullmatch(r'awalia-' + re.escape(kind) + r'-\d{8}T\d{12}-[0-9a-f]{32}\.zip', path.name):
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                meta = json.loads(archive.read('manifest.json'))
            if meta['producer'] == OWNER and meta['kind'] == kind:
                candidates.append((clock.parse(meta['created_at']), path))
        except (ValueError, KeyError, OSError, zipfile.BadZipFile):
            continue
    candidates.sort(reverse=True)
    preserved = set()
    if keep is not None:
        preserved = {p for _, p in candidates[:keep]}
    else:
        seen_days, seen_weeks = set(), set()
        today = clock.now().date()
        for at, path in candidates:
            day = at.date()
            week = day - timedelta(days=day.weekday())
            this_week = today - timedelta(days=today.weekday())
            if 0 <= (today - day).days < days and day not in seen_days:
                preserved.add(path)
                seen_days.add(day)
            elif 1 <= (this_week - week).days // 7 <= weeks and week not in seen_weeks:
                preserved.add(path)
                seen_weeks.add(week)
    for _, path in reversed(candidates):
        if path not in preserved:
            path.unlink()


def export_name(kind, encrypted=False):
    return f'awalia-{kind}-{clock.now():%Y%m%dT%H%M%S%f}-{uuid4().hex}' + ('.awalia' if encrypted else '.zip')


class BackupWorker:
    """One coalesced daemon per Store, entirely outside turn/Extract/steward waits."""
    def __init__(self, store, characters_root, data_dir, config, *, config_path=None):
        from cbs.first_run import trial_info
        self.trial = trial_info(data_dir) is not None
        self.store, self.characters_root, self.data_dir = store, Path(characters_root), Path(data_dir)
        self.config, self.config_path = config, config_path
        self.condition = Condition()
        self.thread = None
        self.jobs = {}
        self.pending = []
        self.closed = False
        self.state_path = self.data_dir / 'backup' / 'status.json'
        self.state = {}
        try:
            if self.trial:
                return
            saved = json.loads(self.state_path.read_text(encoding='utf-8'))
            for key in ('auto_success', 'scheduled_success'):
                if saved.get(key):
                    self.state[key] = clock.parse(saved[key]).isoformat()
            for key in ('auto_error', 'scheduled_error', 'migration_error'):
                if saved.get(key):
                    self.state[key] = ERROR
                kind = saved.get(key + '_type')
                if isinstance(kind, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', kind):
                    self.state[key + '_type'] = kind
        except (OSError, ValueError, TypeError):
            pass

    def status(self):
        with self.condition:
            return {**self.state, 'trial': self.trial, 'jobs': [{k: v for k, v in job.items() if k != 'path'} for job in self.jobs.values()],
                    'scheduled_password_available': False}

    def request(self, kind='manual', password=''):
        if self.trial:
            raise ValueError('お試しではバックアップを行いません。')
        with self.condition:
            if self.closed:
                raise ValueError('worker closed')
            for job in self.jobs.values():
                if job['kind'] == kind and job['status'] in ('queued', 'running'):
                    if kind == 'manual':
                        # A different tab may have requested another password policy.
                        raise ValueError('manual export already running')
                    return job['id']
            ident = uuid4().hex
            self.jobs[ident] = dict(id=ident, kind=kind, status='queued')
            self.pending.append((ident, password))
            if self.thread is None:
                self.thread = Thread(target=copy_context().run, args=(self._run,), name='awalia-backup', daemon=True)
                self.thread.start()
            return ident

    def request_due(self):
        if self.trial:
            return
        cfg = self.config().backup
        today = clock.now().date()
        with self.condition:
            auto = self.state.get('auto_success')
            scheduled = self.state.get('scheduled_success')
        if not auto or clock.parse(auto).date() < today:
            self.request('auto')
        gap = 1 if cfg.frequency == 'daily' else 7
        if cfg.frequency != 'off' and cfg.folder and (not scheduled or (today - clock.parse(scheduled).date()).days >= gap):
            self.request('scheduled')

    def _save_state(self):
        with self.condition:
            saved = dict(self.state)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix('.tmp')
        try:
            temp.write_text(json.dumps(saved), encoding='utf-8')
            temp.replace(self.state_path)
        finally:
            temp.unlink(missing_ok=True)

    def _run(self):
        while True:
            with self.condition:
                if not self.pending:
                    self.thread = None
                    self.condition.notify_all()
                    return
                ident, password = self.pending.pop(0)
                job = self.jobs[ident]
                job['status'] = 'running'
            kind = job['kind']
            try:
                config = self.config().model_copy(deep=True)
                cfg = config.backup
                folder = self.data_dir / 'backup' / ('downloads' if kind == 'manual' else 'auto')
                if kind == 'scheduled':
                    folder = Path(validate_folder(cfg.folder))
                path = folder / export_name(kind, bool(password))
                build_export(self.store.path, self.characters_root, self.data_dir, config.character.id, path,
                             password=password, kind=kind, config_path=self.config_path)
                if kind == 'auto':
                    prune(folder, kind, days=cfg.daily_keep, weeks=cfg.weekly_keep)
                elif kind == 'scheduled':
                    prune(folder, kind, keep=cfg.export_keep)
                with self.condition:
                    job.update(status='completed', path=path, filename=path.name)
                    if kind != 'manual':
                        self.state[kind + '_success'] = clock.iso()
                        self.state.pop(kind + '_error', None)
                        self.state.pop(kind + '_error_type', None)
                        # A full backup now exists, so an earlier failed pre-migration copy no longer matters.
                        self.state.pop('migration_error', None)
                        self.state.pop('migration_error_type', None)
                if kind != 'manual':
                    self._save_state()
                self._expire_downloads()
            except Exception as error:
                from cbs.telemetry import record_error
                record_error(self.store, 'backup', error)
                with self.condition:
                    job.update(status='failed', error=ERROR)
                    if kind != 'manual':
                        self.state[kind + '_error'] = ERROR
                        self.state[kind + '_error_type'] = error_type(error)
                if kind != 'manual':
                    try:
                        self._save_state()
                    except OSError:
                        pass
            finally:
                password = ''

    def _expire_downloads(self):
        expired = []
        with self.condition:
            finished = [j for j in self.jobs.values() if j['status'] in ('completed', 'failed')]
            for job in finished[:-8]:
                if job['kind'] == 'manual' and job.get('path'):
                    expired.append(job['path'])
                self.jobs.pop(job['id'], None)
        for path in expired:
            path.unlink(missing_ok=True)

    def download(self, ident):
        with self.condition:
            job = self.jobs.get(ident, {})
            if job.get('kind') != 'manual' or job.get('status') != 'completed':
                raise KeyError(ident)
            return job['path']

    def wait(self, timeout=60):
        with self.condition:
            if not self.condition.wait_for(lambda: self.thread is None, timeout):
                raise TimeoutError('backup still running')

    def close(self):
        with self.condition:
            self.closed = True
        self.wait()


def pre_migration(db_path, characters_root, data_dir, current_id, *, config_path=None):
    """Startup only: preserve the old shape before any schema writes."""
    from cbs.first_run import trial_info
    if trial_info(data_dir):
        return
    folder = Path(data_dir) / 'backup' / 'migration'
    return build_export(db_path, characters_root, data_dir, current_id, folder / export_name('migration'),
                        kind='migration', config_path=config_path)
