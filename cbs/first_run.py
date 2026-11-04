"""FR1 trial directories. Only marked direct children may be resumed or pruned."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import tomllib

MARKER = 'first-run.json'


def linked(path):
    return path.is_symlink() or path.is_junction()


def trial_info(directory):
    path = Path(directory) / MARKER
    try:
        if linked(Path(directory)) or linked(path):
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if value == {'version': 1, 'bare': value.get('bare')} and type(value['bare']) is bool else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def trials(source):
    root = Path(source).resolve() / 'first-run'
    if linked(root):
        raise ValueError('お試しの保存先にリンクは使えません。')
    return sorted((p for p in root.glob('*') if re.fullmatch(r'\d{8}T\d{12}', p.name)
                   and p.is_dir() and trial_info(p)), reverse=True)


def prepare(source, mode='new', *, directory=None):
    from cbs.backup import clean_settings, toml_bytes
    source = Path(source).resolve()
    existing = trials(source)
    if directory is not None:
        directory = Path(directory).absolute()
        if directory.parent != source / 'first-run' or directory not in existing:
            raise ValueError('お試しの保存先を確認してください。')
        return directory
    if mode == 'keep' and existing:
        return existing[0]
    root = source / 'first-run'
    root.mkdir(parents=True, exist_ok=True)
    # Microseconds plus exclusive mkdir avoid overwriting another launch.
    directory = root / datetime.now().strftime('%Y%m%dT%H%M%S%f')
    directory.mkdir()
    values = {}
    if (source / 'settings.toml').is_file():
        values = tomllib.loads((source / 'settings.toml').read_text(encoding='utf-8'))
    values = clean_settings(values)
    values.get('character', {}).pop('id', None)
    values.pop('onboarding', None)
    # Never inherit a real automatic export destination.
    values['backup'] = {**values.get('backup', {}), 'folder': '', 'frequency': 'off'}
    (directory / 'settings.toml').write_bytes(toml_bytes(values))
    (directory / 'characters').mkdir()
    (directory / 'cbs.sqlite3').touch()
    (directory / MARKER).write_text(json.dumps({'version': 1, 'bare': mode == 'bare'}), encoding='utf-8')
    return directory


@contextmanager
def trial_lock(directory):
    """OS lock releases even after a crash; an active trial must never be pruned."""
    path = Path(directory) / '.active'
    if linked(path):
        raise ValueError('お試しの保存先にリンクは使えません。')
    with path.open('a+b') as stream:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def prune(source, keep, current):
    candidates = trials(source)
    preserved = {Path(current), *candidates[:keep]}
    for directory in candidates:
        if directory in preserved:
            continue
        try:
            # The native shell keeps this file exclusively open across backend restarts.
            if (directory / '.shell-active').exists():
                with (directory / '.shell-active').open('a+b'):
                    pass
            # Windows cannot remove an open lock file: release after checking,
            # then rename atomically so keep/new launches cannot select it.
            with trial_lock(directory):
                pass
            if directory.resolve().parent != Path(source).resolve() / 'first-run' or linked(directory):
                continue
            retired = directory.with_name('.pruning-' + directory.name)
            directory.rename(retired)
            if retired.resolve().parent == Path(source).resolve() / 'first-run' and not linked(retired):
                shutil.rmtree(retired)
        except OSError:
            pass  # Active trials can temporarily exceed the configured count.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', choices=('new', 'keep', 'bare'), required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    # Native launcher only reads the path, never settings or secrets.
    root = args.data_dir.resolve() / 'first-run'
    if linked(root):
        raise ValueError('お試しの保存先にリンクは使えません。')
    root.mkdir(parents=True, exist_ok=True)
    with trial_lock(root):
        directory = prepare(args.data_dir, args.prepare)
    print(json.dumps(str(directory)))


if __name__ == '__main__':
    main()
