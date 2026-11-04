"""Audio is bounded before JSON parsing and is never persisted or traced."""
from pathlib import Path
import threading
from typing import Literal

from fastapi import Depends, HTTPException, Request

from cbs.llm import FakeLLMClient, LLMError
from cbs.stt import HaasServer, stt_status, transcribe
from cbs.stt_download import HaasInstaller
from cbs.tts import TTSError
from cbs.tts_download import DownloadJob
from cbs.web.embedding_api import EmbeddingDownloadInput


class STTDownloadInput(EmbeddingDownloadInput):
    device: Literal['auto', 'cpu', 'gpu'] | None = None


def install_stt(app, pipeline, selected, data_dir):
    server = app.state.stt_server = HaasServer()
    installer = HaasInstaller(Path(data_dir) / 'tools' / 'speech', pipeline.config)
    download_installer = HaasInstaller(installer.directory, pipeline.config)
    job = app.state.stt_download = DownloadJob(download_installer, name='文字起こし')
    app.state.stt_installer = installer
    app.state.stt_notice = (Path(data_dir) / 'stt-notice-seen').is_file()
    preparing = threading.Lock()

    def check(choice):
        if choice not in installer.config.stt.local_models:
            raise HTTPException(422, 'ローカルの文字起こしモデルを選んでください。')

    def prepare():
        # Only the background worker may wait for model startup. Never block a turn/settings response.
        if isinstance(pipeline.client, FakeLLMClient):
            return
        def work():
            if not preparing.acquire(blocking=False):
                return
            try:
                model = pipeline.config.models.stt or ''
                if model.startswith('local/') and model[6:] in installer.config.stt.local_models and installer.plan(model[6:])['ready']:
                    if server.process is not None:  # First use starts it; later checks restart an exited child.
                        server.start(installer, model[6:])
                else:
                    server.stop()
            except Exception:
                pass  # Transcription returns the safe actionable error if startup failed.
            finally:
                preparing.release()
        threading.Thread(target=work, daemon=True, name='stt-prepare').start()
    app.state.prepare_stt = prepare

    @app.get('/api/stt/status')
    def status(current=Depends(selected)):
        return {**stt_status(current.config, installer=installer), 'notice_seen': app.state.stt_notice}

    @app.post('/api/stt/notice')
    def notice():
        # No audio or text is recorded here, only acknowledgement of the one-time notice.
        path = Path(data_dir) / 'stt-notice-seen'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        app.state.stt_notice = True
        return {'seen': True}

    @app.post('/api/stt/transcribe')
    async def audio(request: Request, current=Depends(selected)):
        maximum = 4 * ((current.config.stt.max_seconds * 32000 + 46) // 3) + 1024
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > maximum:
                raise HTTPException(413, '録音の容量が上限を超えています。')
            data.extend(chunk)
        try:
            import json
            import asyncio
            body = json.loads(data)
            if set(body) != {'audio'} or not isinstance(body['audio'], str):
                raise ValueError('録音を確認してください。')
            return await asyncio.to_thread(transcribe, current, body['audio'], installer, server)
        except (ValueError, LLMError) as error:
            from cbs.telemetry import record_error
            record_error(current.store, 'voice', error)
            message = str(error) if isinstance(error, LLMError) or type(error) is ValueError else '録音を確認してください。'
            raise HTTPException(422, message) from None
        except Exception as error:
            from cbs.telemetry import record_error
            record_error(current.store, 'voice', error)
            raise HTTPException(422, '文字起こしに失敗しました。もう一度話してください。') from None
        finally:
            data.clear()

    @app.get('/api/stt/download')
    def download_status(choice: str, device: Literal['auto', 'cpu', 'gpu'] | None = None):
        check(choice)
        state = job.status(choice)
        target = download_installer.auto_device() if device == 'auto' else device
        if target in {'cpu', 'gpu'} and state.get('engine') != target and state['status'] != 'downloading':
            view = HaasInstaller(installer.directory, installer.config)
            view.device = target
            plan = view.plan(choice)
            state = {**plan, 'status': 'ready' if plan['ready'] else 'missing', 'percent': round(100 * plan['completed'] / plan['total'], 1), 'eta_seconds': None}
        if state['status'] == 'ready':
            prepare()
        return state

    @app.post('/api/stt/download/plan')
    def plan(body: STTDownloadInput):
        check(body.choice)
        from cbs.web.settings_api import SETTINGS_LOCK
        with SETTINGS_LOCK:
            if job.thread and job.thread.is_alive():
                raise HTTPException(409, '文字起こしモデルを取得中です。完了か取り消しを待ってください。')
            download_installer.probe_device(body.device)
            return job.quote(body.choice)

    @app.post('/api/stt/download')
    def download(body: STTDownloadInput):
        check(body.choice)
        from cbs.web.settings_api import SETTINGS_LOCK
        with SETTINGS_LOCK, job.lock:
            if job.quotes.get(body.token, {}).get('choice') != body.choice:
                raise HTTPException(409, '取得するモデルと容量をもう一度確認してください。')
            try:
                return job.start(body.token, body.confirmed)
            except TTSError as error:
                raise HTTPException(409, str(error)) from None

    @app.post('/api/stt/download/cancel')
    def cancel():
        job.cancel()
        return {'cancelled': True}
