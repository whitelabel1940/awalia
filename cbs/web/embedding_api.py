"""Explicit local embedding download, independent of settings persistence."""
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from cbs.embedding_download import EmbeddingInstaller
from cbs.llm.local import MODELS
from cbs.tts import TTSError
from cbs.tts_download import DownloadJob


class EmbeddingDownloadInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    choice: str
    token: str = ''
    confirmed: bool = False


def install_embedding(app, pipeline, data_dir):
    job = app.state.embedding_download = DownloadJob(
        EmbeddingInstaller(Path(data_dir) / 'models', pipeline.config.llm.tls_strict), name='記憶検索モデル')

    def check(choice):
        if choice not in MODELS:
            raise HTTPException(422, 'ローカルの記憶検索モデル名を確認してください。')

    @app.get('/api/embedding/download')
    def status(choice: str):
        check(choice)
        return job.status(choice)

    @app.post('/api/embedding/download/plan')
    def plan(body: EmbeddingDownloadInput):
        check(body.choice)
        return job.quote(body.choice)

    @app.post('/api/embedding/download')
    def start(body: EmbeddingDownloadInput):
        check(body.choice)
        with job.lock:
            if job.quotes.get(body.token, {}).get('choice') != body.choice:
                raise HTTPException(409, '取得するモデルと容量をもう一度確認してください。')
            try:
                return job.start(body.token, body.confirmed)
            except TTSError as error:
                raise HTTPException(409, str(error)) from None

    @app.post('/api/embedding/download/cancel')
    def cancel():
        job.cancel()
        return {'cancelled': True}
