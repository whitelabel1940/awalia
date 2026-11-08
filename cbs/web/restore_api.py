"""BK2 upload/confirmation endpoints. Passwords never enter URLs, errors or state."""
import json
from pathlib import Path
import re
import shutil
from threading import Lock
from uuid import uuid4
from urllib.parse import unquote

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from cbs import restore


def install_restore(app, pipeline, data_dir):
    root = Path(data_dir) / 'restore'
    lock = Lock()
    previews = {}  # Insertion order bounds abandoned confirmations across navigation.

    def enabled():
        if app.state.first_run:
            raise HTTPException(409, 'お試しでは記録を戻せません。通常の起動でお試しください。')

    def directory(ident):
        if not re.fullmatch(r'[0-9a-f]{32}', ident) or ident not in previews:
            raise HTTPException(404, '確認したファイルが見つかりません。もう一度選んでください。')
        return root / 'previews' / ident

    @app.get('/api/restore')
    def status():
        return {'staged': (root / 'staged').exists(), 'trial': app.state.first_run,
                'max_bytes': pipeline.config.backup.restore_max_bytes}

    @app.post('/api/restore/preview')
    async def upload(request: Request):
        enabled()
        if not lock.acquire(blocking=False):
            raise HTTPException(409, 'ファイルを確認しています。少し待ってからお試しください。')
        ident = uuid4().hex
        work = root / 'previews' / ident
        success = False
        try:
            restore.safe_tree(root)
            if (root / 'staged').exists():
                raise HTTPException(409, '再起動を待っています。アプリを再起動してください。')
            # A previous process's unconfirmed previews have no live owner.
            for stale in (root / 'previews').glob('*'):
                if stale.name not in previews and re.fullmatch(r'[0-9a-f]{32}', stale.name):
                    shutil.rmtree(stale)
            if len(previews) >= 4:
                oldest = next(iter(previews))
                await run_in_threadpool(shutil.rmtree, root / 'previews' / oldest)
                previews.pop(oldest)
            work.mkdir(parents=True)
            path = work / 'upload'
            size = 0
            # The optional password is a short UTF-8 JSON header, never a query string.
            password = json.loads(unquote(request.headers.get('X-Awalia-Restore-Password', '""')))
            if not isinstance(password, str) or len(password) > 1024:
                raise ValueError('invalid password')
            with path.open('xb') as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > pipeline.config.backup.restore_max_bytes:
                        raise HTTPException(413, 'ファイルが大きすぎます。バックアップの上限を確認してください。')
                    await run_in_threadpool(stream.write, chunk)
            result = await run_in_threadpool(restore.preview, path, work, password, pipeline.config.backup)
            password = ''
            if result.get('password_required'):
                return result
            previews[ident] = True
            success = True
            return {'id': ident, **result}
        except HTTPException:
            raise
        except Exception as error:
            from cryptography.exceptions import InvalidTag
            if isinstance(error, InvalidTag):
                raise HTTPException(422, 'パスワードが違います。もう一度入力してください。') from None
            raise HTTPException(422, restore.ERROR) from None
        finally:
            if not success and work.exists():
                await run_in_threadpool(shutil.rmtree, work)
            lock.release()

    @app.delete('/api/restore/previews/{ident}')
    def cancel(ident: str):
        enabled()
        with lock:
            work = directory(ident)
            restore.safe_tree(work)
            shutil.rmtree(work)
            previews.pop(ident)
        return {'status': 'cancelled'}

    @app.post('/api/restore/previews/{ident}/stage', status_code=202)
    def confirm(ident: str):
        enabled()
        with lock:
            work = directory(ident)
            try:
                restore.stage(work, Path(data_dir))
            except Exception:
                raise HTTPException(422, restore.ERROR) from None
            previews.pop(ident)
        return {'status': 'staged'}

    @app.post('/api/restore/notice')
    def notice():
        enabled()
        with lock:
            path = root / 'notice.json'
            if not path.exists():
                return {'created_at': None, 'backup_folder': None}
            restore.safe_tree(path)
            value = json.loads(path.read_text(encoding='utf-8'))
            path.unlink()
            folder = value.get('backup_folder')
            return {'created_at': value['created_at'], 'backup_folder': folder if isinstance(folder, str) and folder else None}
