import uvicorn
import argparse
import os
import socket
from contextlib import ExitStack
from pathlib import Path

from cbs.llm import LLMError
from cbs.web import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--character')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--characters-root', type=Path)
    parser.add_argument('--first-run', nargs='?', const='new', choices=('new', 'keep', 'bare'))
    parser.add_argument('--first-run-dir', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.first_run_dir and not args.first_run:
        parser.error('--first-run-dir requires --first-run')
    if args.first_run and (args.db or args.character or args.characters_root):
        parser.error('--first-run cannot be combined with --db, --character or --characters-root')
    with ExitStack() as stack:
        run(args, stack)


def run(args, stack):
    from cbs.config import ROOT
    from cbs.paths import resolve_paths
    from cbs.first_run import prepare, prune, trial_info, trial_lock
    # An explicit check DB also isolates settings, keys, cache and bootstrap files.
    data_dir = args.data_dir or (args.db.resolve().parent if args.db else None)
    source, bare, listener = None, False, None
    if args.first_run:
        source = resolve_paths(data_dir)[0].resolve()
        root = source / 'first-run'
        if root.is_symlink() or root.is_junction():
            raise ValueError('お試しの保存先にリンクは使えません。')
        root.mkdir(parents=True, exist_ok=True)
        # Serialize selection/pruning against trial startup, but never the normal app.
        with trial_lock(root):
            data_dir = prepare(source, args.first_run, directory=args.first_run_dir)
            stack.enter_context(trial_lock(data_dir))
            args.db = data_dir / 'cbs.sqlite3'
            args.characters_root = data_dir / 'characters'
            bare = trial_info(data_dir)['bare']
            # Read retention without loading the real credentials.
            import tomllib
            from cbs.config import FirstRunConfig, overlay
            base = tomllib.loads((ROOT / 'config.toml').read_text(encoding='utf-8'))
            saved = tomllib.loads((data_dir / 'settings.toml').read_text(encoding='utf-8'))
            keep = FirstRunConfig.model_validate(overlay(base, saved).get('first_run', {})).keep
            prune(source, keep, data_dir)
        # Port 0 reserves a distinct port, including alongside other trial servers.
        # A desktop parent already selected its port and passes a pinned trial dir.
        listener = stack.enter_context(socket.socket())
        listener.bind(('127.0.0.1', args.port if args.first_run_dir else 0))
        args.port = listener.getsockname()[1]
        os.environ['CBS_DATA_DIR'] = str(data_dir)
        print(f'Awalia お試し: http://127.0.0.1:{listener.getsockname()[1]}/', flush=True)
    try:
        app = create_app(character_id=args.character, db_path=args.db, settings_dir=data_dir,
                         characters_root=args.characters_root or (resolve_paths(data_dir)[1] if data_dir else None),
                         work_root=data_dir / 'web_bootstrap' if data_dir else None,
                         resident_token=os.environ.pop('CBS_DESKTOP_TOKEN', None),
                         **({'secrets_dir': source, 'without_keys': bare} if source else {}))
    except LLMError as error:
        print(error)
        raise SystemExit(1) from None
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False,
                                          log_config=None, timeout_graceful_shutdown=15))
    app.state.resident_shutdown = lambda: setattr(server, 'should_exit', True)
    if listener is not None:
        server.run(sockets=[listener])
    else:
        server.run()


if __name__ == "__main__":
    main()
