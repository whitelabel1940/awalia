"""One short connectivity check per effective configuration, resumable on disk."""
from cbs.llm.cost import for_step
import hashlib
import json
import os
from threading import Lock
from typing import ClassVar

from fastapi import HTTPException, Body
from pydantic import BaseModel, ConfigDict

from cbs.config import key_env
from cbs.llm import LLMError, RoutingLLMClient, FakeLLMClient
from cbs.llm.errors import failure_notice
from fastapi.responses import JSONResponse
from cbs.runtime import read_prompt


class ConnectionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool


class StewardConnectionCheck(ConnectionCheck):
    llm_role: ClassVar[str] = "steward"


class VoiceStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    skipped: bool = False


def install_setup(app, pipeline, data_dir, current, atomic_write):
    path = data_dir / "connection.json"
    lock = Lock()

    def signature(cfg=None, credentials=None):
        cfg = cfg or pipeline.config
        credentials = credentials or {}
        # STT has its own capability/key check. Changing it must not repeat the
        # four foreground conversation checks (SP1), or invalidate existing checks.
        models = cfg.models.model_dump(exclude={'stt'})
        providers = {RoutingLLMClient(cfg)._split(model)[0] for model in models.values() if model}
        # Only the digest is persisted. It invalidates a successful check after a
        # key, model or endpoint change, including externally changed env values.
        payload = {"models": models, "llm": cfg.llm.model_dump(),
                   "credentials": {p: credentials.get(p, os.environ.get(key_env(cfg, p) or "", "")) for p in providers}}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def verified():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("signature") == signature()
        except (OSError, ValueError):
            return False

    app.state.connection_verified = verified
    voice_step = data_dir / 'voice_step.json'
    app.state.voice_setup_done = lambda: voice_step.is_file()

    @app.post('/api/setup/voice-step')
    def finish_voice_step(body: VoiceStep):
        if not verified():
            raise HTTPException(409, '先に AI の接続を確認してください。')
        if not body.skipped and pipeline.config.tts.enabled and pipeline.config.tts.engine == 'speech.cpp':
            try:
                ready = app.state.voice_download.installer.plan(pipeline.config.tts.local_model)['ready']
            except Exception:
                ready = False
            if not ready:
                raise HTTPException(409, '音声モデルの取得を完了するか、声をあとで決めてください。')
        atomic_write(voice_step, json.dumps({'skipped': body.skipped}))
        return {'status': 'done'}

    @app.post("/api/settings/connection")
    def check_connection(body: dict = Body(default={})):
        with lock:
            candidate = body.get("settings")
            if candidate is not None:
                try:
                    if set(body) - {"settings", "keys"} or set(candidate) - {"llm", "models", "simple"}:
                        raise ValueError()
                    from cbs.web.settings_api import ALLOWED, settings_candidate
                    if any(not isinstance(v, dict) or set(v) - ALLOWED[k] for k, v in candidate.items()):
                        raise ValueError()
                    cfg = settings_candidate(pipeline.config, candidate, data_dir)
                    credentials = body.get("keys", {})
                    if any(not isinstance(v, str) or len(v) > 4096 or any(c in v for c in "\n\r\x00") for v in credentials.values()):
                        raise ValueError()
                    client = pipeline.client
                    if not isinstance(client, FakeLLMClient):
                        from cbs.costs import bind
                        client = bind(RoutingLLMClient(cfg, credentials=credentials), pipeline.store, cfg)
                except Exception:
                    raise HTTPException(422, "設定値を確認してください。") from None
            else:
                cfg, client = pipeline.config, pipeline.client
            if candidate is None and verified():
                return {"status": "connected", "cached": True}
            if candidate is None and not current()["ai_ready"]:
                raise HTTPException(409, "選んだサービスの API キーが足りません。キーを保存してください。")
            before = signature()
            stage = "会話"
            try:
                models = cfg.models
                prompt = read_prompt("connection_check")
                messages = [{"role": "user", "content": "接続確認"}]
                if not for_step(client, 'connection', 'other').generate_text(models.actor, prompt, messages).text.strip():
                    raise ValueError()
                stage = "判断"
                if not ConnectionCheck.model_validate(for_step(client, 'connection', 'other').generate_json(models.judge, prompt, messages, ConnectionCheck).parsed).ok:
                    raise ValueError()
                stage = "記憶の整理"
                if not StewardConnectionCheck.model_validate(for_step(client, 'connection', 'other').generate_json(models.steward, prompt, messages, StewardConnectionCheck).parsed).ok:
                    raise ValueError()
                stage = "記憶の検索"
                vectors = for_step(client, 'connection', 'other').embed(["接続確認"], models.embedding, role="query")
                if not vectors or not vectors[0]:
                    raise ValueError()
            except LLMError as error:
                character = getattr(getattr(pipeline, 'active', None) or pipeline, 'character', None)
                notice = failure_notice(error, character.model.display_name if character else 'パートナー')
                return JSONResponse({"detail": notice["message"], **notice}, status_code=422)
            except Exception:
                raise HTTPException(422, f"{stage}の接続を確認できませんでした。モデル名・接続先・通信状態を確認してください。") from None
            if candidate is not None:
                atomic_write(path, json.dumps({"signature": signature(cfg, credentials)}))
                return {"status": "connected", "cached": False}
            if signature() != before:
                raise HTTPException(409, "確認中に設定が変わりました。もう一度確かめてください。")
            atomic_write(path, json.dumps({"signature": before}))
            return {"status": "connected", "cached": False}
