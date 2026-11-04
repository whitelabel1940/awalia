"""Global app export endpoints; never acquire turn/steward locks."""
import tomllib

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from cbs.backup import BackupWorker, clean_settings, validate_folder
from cbs.config import BackupConfig, ROOT


def install_backup(app, pipeline, pipelines, data_dir):
    from cbs.web.settings_api import atomic_write, toml_text, SETTINGS_LOCK
    worker = BackupWorker(pipeline.store, pipeline.characters_root, data_dir, lambda: pipeline.config,
                          config_path=ROOT / 'config.toml')
    app.state.backup = pipeline.store.backup_worker = worker

    @app.get('/api/backup')
    def status():
        return {'config': pipeline.config.backup.model_dump(), **worker.status()}

    @app.put('/api/backup/settings')
    async def settings(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, 'バックアップの指定を確認してください。') from None
        return await run_in_threadpool(save_settings, body)

    def save_settings(body):
        if worker.trial:
            raise HTTPException(409, 'お試しではバックアップを行いません。')
        try:
            if not isinstance(body, dict):
                raise ValueError('invalid settings')
            with SETTINGS_LOCK:
                config = BackupConfig.model_validate({**pipeline.config.backup.model_dump(), **body}, strict=True)
                if clean_settings(body) != body:
                    raise ValueError('credential shape')
                if config.folder:
                    config.folder = validate_folder(config.folder)
                elif config.frequency != 'off':
                    raise ValueError('missing folder')
                path = data_dir / 'settings.toml'
                saved = tomllib.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
                saved['backup'] = config.model_dump()
                atomic_write(path, toml_text(saved))
                pipeline.config.backup = config
                for current in list(pipelines.values()):
                    current.config.backup = config.model_copy(deep=True)
        except Exception:
            raise HTTPException(422, 'バックアップ先は存在する書き込み可能なフォルダを指定し、頻度を確認してください。') from None
        return status()

    @app.post('/api/backup/exports', status_code=202)
    async def export(request: Request):
        if worker.trial:
            raise HTTPException(409, 'お試しではバックアップを行いません。')
        # Validate manually so FastAPI never echoes a rejected password value.
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, 'バックアップの指定を確認してください。') from None
        if not isinstance(body, dict):
            raise HTTPException(422, 'バックアップの指定を確認してください。')
        if set(body) - {'password'} or not isinstance(body.get('password', ''), str) or len(body.get('password', '')) > 1024:
            raise HTTPException(422, 'バックアップの指定を確認してください。')
        try:
            return {'id': worker.request(password=body.get('password', ''))}
        except ValueError:
            raise HTTPException(409, 'バックアップ中です。完了してからもう一度お試しください。') from None

    @app.get('/api/backup/exports/{ident}')
    def job(ident: str):
        found = next((j for j in worker.status()['jobs'] if j['id'] == ident and j['kind'] == 'manual'), None)
        if not found:
            raise HTTPException(404, 'バックアップが見つかりません。')
        return found

    @app.get('/api/backup/exports/{ident}/download')
    def download(ident: str):
        try:
            path = worker.download(ident)
        except KeyError:
            raise HTTPException(404, 'バックアップが見つかりません。') from None
        return FileResponse(path, filename=path.name, media_type='application/octet-stream',
                            headers={'Cache-Control': 'no-store'})
