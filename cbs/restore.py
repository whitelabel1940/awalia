"""BK2: bounded BK1 inspection, staging, and recoverable startup-only replacement.

No Store/provider is constructed here. All replacements happen before config/DB load.
"""
from contextlib import closing
import hashlib
import base64
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tomllib
from uuid import uuid4
import zipfile

from cbs import backup, clock

ERROR = 'ファイルを戻せませんでした。パスワード・ファイル・空き容量を確認してください。'
STARTUP_ERROR = '記録の復元を完了できませんでした。元の記録を保護して起動を停止しました。'
ID = re.compile(r'[a-z][a-z0-9_-]{0,63}')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    temporary.replace(path)


def safe_tree(path):
    """Reject links at every ancestor and descendant before moving/reading trees."""
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if backup.linked(parent):
            raise ValueError('linked restore path')
    if path.is_dir() and any(backup.linked(p) for p in path.rglob('*')):
        raise ValueError('linked restore tree')


def allowed(name, ids):
    if name in {'data/cbs.sqlite3', 'config.toml', 'data/settings.toml'}:
        return True
    if re.fullmatch(r'data/photos/[0-9a-f]{32}\.jpg', name):
        return True
    parts = name.split('/')
    return (len(parts) >= 3 and parts[0] == 'characters' and parts[1] in ids and
            not any(p.lower() in backup.EXCLUDED or p.lower() == 'tools' or p.startswith('.') for p in parts[2:]) and
            parts[-1].lower() not in {'secrets.env', 'connection.json'})


def inspect_zip(path, content, limit):
    manifest = backup.verify_zip(path, max_bytes=limit)
    ids = manifest['characters']
    if (not isinstance(ids, list) or len(ids) != len(set(ids)) or
            any(not isinstance(cid, str) or not ID.fullmatch(cid) for cid in ids)):
        raise ValueError('invalid partners')
    clock.parse(manifest['created_at'])
    if not isinstance(manifest['schema_version'], int) or not 0 <= manifest['schema_version'] <= backup.SCHEMA_VERSION:
        raise ValueError('unsupported schema')
    names = manifest['files']
    if 'data/cbs.sqlite3' not in names or any(not allowed(n, ids) for n in names):
        raise ValueError('unsupported content')
    # A file cannot also be a directory. Case-insensitive spelling matters on Windows.
    folded = {n.casefold() for n in names}
    if any('/'.join(n.split('/')[:i]).casefold() in folded for n in names for i in range(1, len(n.split('/')))):
        raise ValueError('conflicting paths')
    content.mkdir()
    with zipfile.ZipFile(path) as archive:
        for name in names:
            target = content / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, target.open('xb') as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
    return inspect_content(content, manifest)


def inspect_content(content, manifest):
    """Revalidate staged bytes at startup; do not trust the preview or stage marker."""
    safe_tree(content)
    names, ids = manifest['files'], manifest['characters']
    if manifest['format_version'] != backup.FORMAT_VERSION or manifest['producer'] != backup.OWNER:
        raise ValueError('invalid format')
    if not 0 <= manifest['schema_version'] <= backup.SCHEMA_VERSION:
        raise ValueError('unsupported schema')
    actual = {p.relative_to(content).as_posix() for p in content.rglob('*') if p.is_file()}
    if actual != set(names) or any(not allowed(n, ids) for n in names):
        raise ValueError('invalid stage inventory')
    for name, item in names.items():
        path = content / name
        if path.stat().st_size != item['size'] or backup.digest(path) != item['sha256']:
            raise ValueError('changed stage')
    with closing(backup.readonly(content / 'data/cbs.sqlite3')) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('invalid database')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        schema = '\n'.join(r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"))
        if version != manifest['schema_version'] or hashlib.sha256(schema.encode()).hexdigest() != manifest['schema_sha256']:
            raise ValueError('invalid schema hash')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1").fetchone():
            raise ValueError('unsupported database objects')
        # BK1's pre-versioned DB (0) uses these shapes; Store runs its usual additive migrations.
        for table, required in {'sessions': {'id', 'character_id', 'title', 'created_at'},
                                'turns': {'id', 'session_id', 'user_text', 'assistant_text', 'status', 'trace_json', 'created_at'}}.items():
            if not required <= {r[1] for r in db.execute(f'PRAGMA table_info({table})')}:
                raise ValueError('unsupported database shape')
        if not backup.recorded_characters(db) <= set(ids):
            raise ValueError('missing partner')
        turns = db.execute('SELECT count(*) FROM turns').fetchone()[0]
        partner_counts = {row[0]: row[1:] for row in db.execute(
            'SELECT s.character_id,count(t.id),CASE WHEN count(t.id)>0 THEN min(s.created_at) END FROM sessions s '
            'LEFT JOIN turns t ON t.session_id=s.id GROUP BY s.character_id')}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        photos = {r[0] for r in db.execute('SELECT id FROM photos')} if 'photos' in tables else set()
        if {f'data/photos/{ident}.jpg' for ident in photos} != {n for n in names if n.startswith('data/photos/')}:
            raise ValueError('missing photos')
    partners = []
    from cbs.character import load_character
    for cid in ids:
        # Validate full character shape before it can make the next startup unusable.
        character = load_character(cid, root=content / 'characters')
        if character.model.character_id != cid:
            raise ValueError('mismatched partner ID')
        count, first_at = partner_counts.get(cid, (0, None))
        partners.append({'id': cid, 'name': character.model.display_name, 'turns': count,
                         'first_at': first_at, 'face': preview_face(content / 'characters' / cid / 'look')})
    for name in ('config.toml', 'data/settings.toml'):
        if name in names:
            value = tomllib.loads((content / name).read_text(encoding='utf-8'))
            if backup.clean_settings(value) != value:
                raise ValueError('credentials in settings')
    from cbs.config import ROOT
    settings = restored_settings(content, tomllib.loads((ROOT / 'config.toml').read_text(encoding='utf-8')), {}, ids)
    for partner in partners:
        partner['selected'] = partner['id'] == settings.get('character', {}).get('id')
    # The partner that opens after the restore first, then the most talked-with.
    partners.sort(key=lambda p: (not p['selected'], -p['turns']))
    return {'created_at': manifest['created_at'], 'partners': partners, 'turns': turns,
            'bytes': sum(v['size'] for v in names.values())}


def preview_face(directory):
    from cbs.look import face_path
    from PIL import Image, UnidentifiedImageError
    path = face_path(directory)
    if path is None:
        return None
    try:
        with Image.open(path) as source:
            source.thumbnail((96, 96))
            image = source.convert('RGBA')
            output = BytesIO()
            image.save(output, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return None


def preview(upload, directory, password, config):
    """The caller owns the bounded upload; plaintext stays in the local restore area."""
    try:
        if upload.stat().st_size > config.restore_max_bytes:
            raise ValueError('upload too large')
        with upload.open('rb') as stream:
            encrypted = stream.read(len(backup.MAGIC)) == backup.MAGIC
        plain = directory / 'decoded.zip' if encrypted else upload
        if encrypted:
            if not password:
                return {'password_required': True}
            backup._crypt(upload, plain, password, decrypt=True)
        summary = inspect_zip(plain, directory / 'content', config.restore_max_expanded_bytes)
        manifest = backup.verify_zip(plain, max_bytes=config.restore_max_expanded_bytes)
        write_json(directory / 'manifest.json', manifest)
        return summary
    finally:
        backup.retry_locked(upload.unlink, missing_ok=True)
        backup.retry_locked((directory / 'decoded.zip').unlink, missing_ok=True)


def stage(directory, data_dir):
    safe_tree(directory)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    inspect_content(directory / 'content', manifest)
    target = data_dir / 'restore/staged'
    safe_tree(target)
    if target.exists():
        raise ValueError('already staged')
    move(directory, target)


def keep_secrets(current, restored, *, references=()):
    """Retain exactly the fields BK1 omits; do not overlay ordinary current choices."""
    if isinstance(current, list) and isinstance(restored, list):
        result = []
        for item in restored:
            match = None
            if isinstance(item, dict):
                identity = next((key for key in ('id', 'name', 'label', 'model') if key in item), None)
                if identity:
                    match = next((old for old in current if isinstance(old, dict) and old.get(identity) == item[identity]), None)
            result.append(keep_secrets(match, item, references=references))
        return result
    if not isinstance(current, dict):
        return restored
    result = dict(restored) if isinstance(restored, dict) else {}
    clean = backup.clean_settings(current, references=references)
    for key, value in current.items():
        if key not in clean:
            result[key] = value
        elif isinstance(value, (dict, list)):
            child = keep_secrets(value, result.get(key, {}), references=references)
            if child or key in result:
                result[key] = child
    return result


def changed_settings(value, base):
    """The part of value that differs from base (tables compared key by key, lists as a whole)."""
    if not isinstance(value, dict) or not isinstance(base, dict):
        return value
    result = {}
    for key, item in value.items():
        if key not in base:
            result[key] = item
        elif isinstance(item, dict) and isinstance(base[key], dict):
            child = changed_settings(item, base[key])
            if child:
                result[key] = child
        elif item != base[key]:
            result[key] = item
    return result


def exported_settings(content):
    """The old PC's effective settings: (its config.toml, that overlaid with its settings.toml)."""
    from cbs.config import overlay
    read = lambda name: tomllib.loads((content / name).read_text(encoding='utf-8')) if (content / name).exists() else {}
    exported = read('config.toml')
    return exported, overlay(exported, read('data/settings.toml'))


def scheduled_folder(settings):
    """The folder of an active "別の場所" backup, or ''."""
    value = settings.get('backup', {})
    return value.get('folder') or '' if value.get('frequency', 'off') != 'off' else ''


def restored_settings(content, base, current, ids):
    from cbs.config import Config, overlay
    exported, merged = exported_settings(content)
    # Keep only what the user chose on the old PC. Its distribution defaults (and values equal to
    # this PC's defaults) are left out, so later updates of config.toml still reach a restored user.
    restored = changed_settings(changed_settings(merged, exported), base)
    # A backup folder belongs to the old PC: on another PC it may not exist, and on the same PC two
    # installs would share it (and prune each other's copies). This PC keeps its own choice.
    for key in ('folder', 'frequency'):
        restored.get('backup', {}).pop(key, None)
    if restored.get('backup') == {}:
        del restored['backup']
    if merged.get('character', {}).get('id'):
        restored.setdefault('character', {})['id'] = merged['character']['id']
    settings = keep_secrets(overlay(base, current), restored)
    own = {key: current['backup'][key] for key in ('folder', 'frequency') if key in current.get('backup', {})}
    if own:
        settings.setdefault('backup', {}).update(own)
    if 'character' not in settings and ids:
        settings['character'] = {'id': ids[0]}
    if ids and settings['character'].get('id') not in ids:
        raise ValueError('selected partner not present')
    Config.model_validate(overlay(base, settings))
    return settings


def prepare_partner_settings(content, staged, characters_root, names):
    """BK1 filters credentials in these partner config files too, outside source/."""
    import yaml
    replacements = {}
    for name in names:
        parts = name.split('/')
        if len(parts) < 3 or parts[0] != 'characters' or parts[2] == 'source':
            continue
        path = content / name
        if not (path.suffix.lower() in {'.toml', '.json'} or path.name in {'persona.yaml', 'voice.yaml', 'look.yaml', 'expression.yaml'}):
            continue
        current = characters_root / Path(name).relative_to('characters')
        if not current.is_file():
            continue
        def read(path):
            text = path.read_text(encoding='utf-8')
            return tomllib.loads(text) if path.suffix.lower() == '.toml' else (json.loads(text) if path.suffix.lower() == '.json' else yaml.safe_load(text))
        references = {p.name for p in (characters_root / parts[1] / 'voice').glob('*') if p.is_file() and
                      re.fullmatch(r'(?:clone-|sample-)?[0-9a-f]{32}\.(?:wav|gguf)', p.name)}
        restored = read(path)
        merged = keep_secrets(read(current), restored, references=references if path.name in {'voice.yaml', 'references.json'} else ())
        if merged != restored:
            target = staged / 'prepared' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(backup.toml_bytes(merged) if path.suffix.lower() == '.toml' else
                               (json.dumps(merged, ensure_ascii=False).encode('utf-8') if path.suffix.lower() == '.json' else
                                yaml.safe_dump(merged, allow_unicode=True).encode('utf-8')))
            replacements[path] = target
    return replacements


def move(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A failed rename changes nothing, so waiting out a scanner keeps forward moves and rollback whole.
    backup.retry_locked(source.replace, destination)


def rollback(journal, journal_path):
    # Save progress for a second interruption while rolling back.
    while journal['moves']:
        source, destination = map(Path, journal['moves'][-1])
        if destination.exists() and not source.exists():
            move(destination, source)
        elif destination.exists():
            raise ValueError('ambiguous rollback')
        journal['moves'].pop()
        write_json(journal_path, journal)
    journal_path.unlink()


def compact_relocated_database(journal, db_path):
    # The rename journal is still intact: before commit it must support rollback;
    # after commit it also makes compaction of the retained old DB resumable.
    from cbs.storage import rewrite_traces, vacuum_traces
    for source, destination in journal['moves']:
        if Path(source) == db_path.absolute() and Path(destination).exists():
            with closing(sqlite3.connect(destination)) as db:
                rewrite_traces(db, backup=True)
                vacuum_traces(db)


def apply_staged(data_dir, characters_root, db_path, *, config_path):
    """Startup transaction: rename journal + reverse moves on any failure/interruption.

    A successful commit leaves old files/absent partners in backup/restore-<time>.
    A process killed before commit is rolled back before any DB/config is opened.
    """
    from cbs.first_run import trial_info
    from cbs.config import load_config
    data_dir, characters_root, db_path, config_path = map(Path, (data_dir, characters_root, db_path, config_path))
    if trial_info(data_dir):
        return
    root = data_dir / 'restore'
    if not root.exists():
        return
    journal_path, staged = root / 'transaction.json', root / 'staged'
    try:
        safe_tree(root)
        if journal_path.exists():
            journal = json.loads(journal_path.read_text(encoding='utf-8'))
            if journal.get('committed'):
                compact_relocated_database(journal, db_path)
                # The durable commit marker doubles as the one-time notice.
                write_json(root / 'notice.json', {'created_at': journal['created_at'], 'backup_folder': journal.get('backup_folder')})
                if staged.exists():
                    backup.retry_locked(shutil.rmtree, staged)
                journal_path.unlink()
            else:
                rollback(journal, journal_path)
                raise ValueError('interrupted restore rolled back')
        if not staged.exists():
            return
        for path in (characters_root, db_path, data_dir / 'settings.toml', config_path, db_path.parent / 'photos', data_dir / 'backup'):
            safe_tree(path)
        manifest = json.loads((staged / 'manifest.json').read_text(encoding='utf-8'))
        inspect_content(staged / 'content', manifest)
        cfg = load_config(config_path, data_dir=data_dir)
        # Only this closed-process point sees all final Extract/Steward/backup writes.
        if db_path.exists():
            backup.build_export(db_path, characters_root, data_dir, cfg.character.id,
                                data_dir / 'backup' / 'pre-restore' / backup.export_name('pre-restore'),
                                kind='pre-restore', config_path=config_path)
        content = staged / 'content'
        base = tomllib.loads(config_path.read_text(encoding='utf-8'))
        current = tomllib.loads((data_dir / 'settings.toml').read_text(encoding='utf-8')) if (data_dir / 'settings.toml').exists() else {}
        settings = restored_settings(content, base, current, manifest['characters'])
        from cbs.config import overlay
        # Tell the user once when the old PC backed up elsewhere and this PC does not (yet).
        left = scheduled_folder(exported_settings(content)[1])
        left = left if left and not scheduled_folder(overlay(base, settings)) else None
        # The receiving PC's distribution config stays put. Its effective overrides
        # contain the old PC's own choices and the receiving PC's secrets.
        prepared = staged / 'settings.toml'
        prepared.write_bytes(backup.toml_bytes(settings))
        partner_settings = prepare_partner_settings(content, staged, characters_root, manifest['files'])
        # Reject incompatible live file/directory shapes before any replacement.
        for name in manifest['files']:
            if not name.startswith('characters/'):
                continue
            target = characters_root / Path(name).relative_to('characters')
            if target.is_dir() or any(p.is_file() for p in target.parents if p != characters_root.parent):
                raise ValueError('incompatible destination shape')
        old = data_dir / 'backup' / f'restore-{clock.now():%Y%m%dT%H%M%S%f}-{uuid4().hex}'
        old.mkdir(parents=True)
        journal = {'moves': [], 'created_at': manifest['created_at'], 'backup_folder': left, 'committed': False}
        write_json(journal_path, journal)

        def relocate(source, target):
            safe_tree(source)
            safe_tree(target)
            if target.exists():
                raise ValueError('restore collision')
            journal['moves'].append([str(source.absolute()), str(target.absolute())])
            write_json(journal_path, journal)  # Write intent before each atomic rename.
            move(source, target)

        try:
            # WAL/SHM belong to the old database; they must never replay into the new one.
            for suffix in ('', '-wal', '-shm', '-journal'):
                source = Path(str(db_path) + suffix)
                if source.exists():
                    relocate(source, old / 'data' / source.name)
            relocate(content / 'data/cbs.sqlite3', db_path)
            # Move absent folders intact. For retained partners, replace only exported
            # file classes so model/tool/cache subtrees are never replaced.
            ids = manifest['characters']
            if characters_root.exists():
                for directory in sorted(characters_root.iterdir()):
                    if directory.is_dir() and ID.fullmatch(directory.name) and directory.name not in ids:
                        relocate(directory, old / 'characters' / directory.name)
            for cid in ids:
                directory = characters_root / cid
                if directory.exists():
                    for path in sorted(directory.rglob('*')):
                        name = 'characters/' + path.relative_to(characters_root).as_posix()
                        if path.is_file() and allowed(name, ids):
                            relocate(path, old / name)
                for path in sorted((content / 'characters' / cid).rglob('*')):
                    if path.is_file():
                        relocate(partner_settings.get(path, path), characters_root / path.relative_to(content / 'characters'))
            photos = db_path.parent / 'photos'
            if photos.exists():
                relocate(photos, old / 'data/photos')
            if (content / 'data/photos').exists():
                relocate(content / 'data/photos', photos)
            if (data_dir / 'settings.toml').exists():
                relocate(data_dir / 'settings.toml', old / 'data/settings.toml')
            relocate(prepared, data_dir / 'settings.toml')
            journal['committed'] = True
            write_json(journal_path, journal)
        except BaseException:
            journal['committed'] = False
            rollback(journal, journal_path)
            raise
        compact_relocated_database(journal, db_path)
        write_json(root / 'notice.json', {'created_at': manifest['created_at'], 'backup_folder': left})
        backup.retry_locked(shutil.rmtree, staged)
        journal_path.unlink()
    except Exception:
        raise ValueError(STARTUP_ERROR) from None
