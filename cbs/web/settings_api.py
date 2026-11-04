"""Validated settings overlay and provider credentials (never returned)."""
import json
import os
import re
import time
import tomllib
from contextlib import ExitStack
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from cbs.config import Config, PersonaConfig, overlay, key_env, key_set, embedding_model, read_secrets, apply_llm_mode, research_ready, research_provider
from cbs.character import effective_persona, write_persona
from cbs.llm import FakeLLMClient, RoutingLLMClient

SETTINGS_LOCK = RLock()

ALLOWED = {
    'display': {'split_replies', 'delay_user', 'read_seconds', 'typing_seconds', 'user_delay_seconds', 'bubble_max_seconds', 'split_min_chars', 'split_three_chars'},
    'costs': {'monthly_guide_usd', 'pause_autonomy'},
    'stt': {'auto_send', 'max_seconds', 'silence_seconds', 'silence_rms', 'device'},
    "early": {"notify"},
    "tts": {"enabled", "autoplay", "autoplay_proactive", "engine", "base_url", "model", "local_model", "device", "duration_scale", "cache_days", "styles"},
    "inner": {"show_monologue"},
    "autonomy": {"inner_life", "inner_life_interval_hours", "research", "research_daily_max", "activities_daily_max", "tools",
                 "quiet_start_hour", "quiet_end_hour", "opening", "reserved_gap_hours"},
    "pipeline": {"post_check"}, "models": {"actor", "judge", "steward", "embedding", "research", "vision", "stt"},
    "llm": {"providers", "mode", "zdr_only"},
    "simple": {"actor", "judge", "stt"},
}


def settings_candidate(cfg, body, data_dir):
    base = cfg.model_dump()
    if cfg.llm.mode == "simple":
        base["models"] = cfg.llm.advanced_models or base["models"]
    for name, values in body.get("llm", {}).get("providers", {}).items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or name in ("gemini", "openai", "openrouter", "anthropic"):
            raise ValueError()
        if set(values) - {"base_url", "api"} or values.get("api", "chat") != "chat":
            raise ValueError()
        url = urlsplit(values.get("base_url", ""))
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError()
        values["api"] = "chat"
    new = apply_llm_mode(Config.model_validate(overlay(base, body), strict=True))
    new._data_dir = data_dir
    for role, value in body.get("models", {}).items():
        if role in {"vision", "stt"} and value == "":
            continue
        provider, sep, model = value.partition("/")
        providers = {new.llm.provider, "gemini", *new.llm.providers}
        if role == "embedding":
            providers.add("local")
            providers.discard('anthropic')
        if role == 'stt':
            providers.add('local')
        if not sep or not model.strip() or provider not in providers:
            raise ValueError()
    return new


def toml_text(values):
    lines = []
    def section(data, parts):
        scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
        if scalars:
            lines.append("[" + ".".join(json.dumps(p) for p in parts) + "]")
            for k, v in scalars.items():
                lines.append(json.dumps(k) + " = " + ("true" if v is True else "false" if v is False else json.dumps(v, ensure_ascii=False)))
        for k, v in data.items():
            if isinstance(v, dict):
                section(v, parts + [k])
    for key, value in values.items():
        section(value, [key])
    return "\n".join(lines) + "\n"


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError as error:
            # Windows readers/scanners can briefly prevent an otherwise valid save.
            # Keep the previous file intact; do not retry other filesystem errors.
            if getattr(error, 'winerror', None) not in {5, 32, 33} or attempt == 4:
                raise
            time.sleep(.02 * 2 ** attempt)


def remember_partner(data_dir, character_id):
    """Keep the current partner in the app's settings, not only in one browser's storage."""
    path = Path(data_dir) / "settings.toml"
    with SETTINGS_LOCK:
        previous = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if previous.get("character", {}).get("id") != character_id:
            atomic_write(path, toml_text(overlay(previous, {"character": {"id": character_id}})))


def install_settings(app, pipeline, pipelines, data_dir, selected):
    from cbs.web.requests_api import install_requests
    install_requests(app, selected)
    from cbs.web.days_api import install_days
    install_days(app, selected)
    data_dir = Path(data_dir)
    app.state.reindex = {"status": "idle", "completed": 0, "total": 0}
    from cbs.web.embedding_api import install_embedding
    install_embedding(app, pipeline, data_dir)
    from cbs.web.backup_api import install_backup
    install_backup(app, pipeline, pipelines, data_dir)

    def current():
        cfg = pipeline.config
        providers = {cfg.llm.provider, *cfg.llm.providers.keys()}
        values = {k: cfg.model_dump()[k] for k in ALLOWED if k != "llm"}
        values["llm"] = {"mode": cfg.llm.mode, "zdr_only": cfg.llm.zdr_only, "providers": {name: {"api": p.api, "base_url": p.base_url} for name, p in cfg.llm.providers.items()}}
        values["onboarding"] = cfg.onboarding.model_dump()
        values["advanced_models"] = cfg.llm.advanced_models or cfg.models.model_dump()
        values['advanced_presets'] = [p.model_dump() for p in cfg.advanced.presets]
        values["simple"] = {role: getattr(cfg.simple, role) or cfg.simple.models.get(role) for role in ("actor", "judge", "stt")}
        values["model_candidates"] = {role: [item.model_dump() for item in getattr(cfg.simple, role + "_candidates") if item.status != "pending"]
                                      for role in ("actor", "judge")}
        values["models"] = {role: (model if "/" in model else cfg.llm.provider + "/" + model)
                            for role, model in values["models"].items() if role in ALLOWED["models"] and model}
        values["advanced_models"] = {role: (model if "/" in model else cfg.llm.provider + "/" + model)
                                     for role, model in values["advanced_models"].items() if model}
        values["keys"] = {p: "set" if key_set(cfg, p) else "unset" for p in providers if key_env(cfg, p)}
        values["ai_ready"] = all(provider == "local" or not key_env(cfg, provider) or key_set(cfg, provider)
                                 for provider in [RoutingLLMClient(cfg)._split(getattr(cfg.models, role))[0] for role in ("actor", "judge", "steward", "embedding")])
        from cbs.photos import vision_status
        values['photos'] = vision_status(cfg)
        from cbs.stt import stt_status
        values['stt_status'] = {**stt_status(cfg, installer=app.state.stt_installer), 'notice_seen': app.state.stt_notice}
        with pipeline.store.connect() as db:
            values['photos']['notice_seen'] = bool(db.execute('SELECT 1 FROM photo_notice').fetchone())
        values["connection_verified"] = app.state.connection_verified()
        values['voice_setup_done'] = app.state.voice_setup_done()
        values['knowing_available'] = True
        values['backup'] = cfg.backup.model_dump()
        values['backup_status'] = app.state.backup.status()
        values['costs'] = {k: getattr(cfg.costs, k) for k in ALLOWED['costs']}
        values['cost_summary'] = pipeline.store.costs.summary()
        values["warnings"] = ([f"{research_provider(cfg)} のキーまたはサービスの設定により調べ物を行いません。"] if cfg.autonomy.research and not research_ready(cfg) else [])
        from cbs.autonomy import shopping_ready
        if cfg.autonomy.tools.browse_shopping.enabled and not shopping_ready(pipeline):
            values['warnings'].append('買い物の検索にはドメイン制限に対応するOpenRouterの調べ物設定が必要です。')
        try:
            pipeline.store.require_embedding_model(embedding_model(cfg))
            values["reindex_required"] = False
        except ValueError:
            values["reindex_required"] = True
            values["warnings"].append("記憶の検索を停止しています。索引を作り直してください。")
        return values

    from cbs.web.setup_api import install_setup
    install_setup(app, pipeline, data_dir, current, atomic_write)
    from cbs.web.onboarding_api import install_onboarding
    install_onboarding(app, data_dir, atomic_write, pipeline.config.setup_helper)

    @app.get("/api/settings")
    def settings(request: Request):
        from cbs.web.resident import is_resident
        cid = request.query_params.get('character_id')
        current_pipeline = selected(cid) if cid else pipeline
        values = current()
        from cbs.autonomy import autonomy_status
        values['autonomy_status'] = autonomy_status(current_pipeline)
        values['days'] = {**current_pipeline.config.days.model_dump(), 'has_habits': any(h['enabled'] for h in current_pipeline.store.days_state(current_pipeline.config.character.id)['habits']), 'poll_seconds': current_pipeline.config.autonomy.idle_poll_seconds}
        if not is_resident(request):
            values['autonomy'].pop('idle_nudge', None)
            values['autonomy'].pop('idle_poll_seconds', None)
            values['autonomy'].pop('resident_opening_delay_seconds', None)
        return values

    @app.get('/api/persona')
    def persona(current_pipeline=Depends(selected)):
        return effective_persona(current_pipeline.character, current_pipeline.config).model_dump(exclude_none=True)

    @app.post('/api/costs/notice')
    def cost_notice():
        return {'notice': pipeline.store.costs.notice(consume=True)}

    @app.put('/api/persona')
    def update_persona(body: PersonaConfig, current_pipeline=Depends(selected)):
        if 'ai_question' not in body.model_fields_set:
            raise HTTPException(422, '答え方を選んでください。')
        with current_pipeline._lock:
            current_pipeline.wait_for_steward()
            directory = current_pipeline.characters_root / current_pipeline.config.character.id
            if 'open_style' not in body.model_fields_set:
                body = body.model_copy(update={'open_style': effective_persona(current_pipeline.character, current_pipeline.config).open_style})
            write_persona(directory, body)
            current_pipeline.character.persona = body
        return body.model_dump(exclude_none=True)

    @app.put("/api/settings")
    def update_settings(body: dict, request: Request):
        try:
            for section, values in body.items():
                from cbs.web.resident import is_resident
                extra = {'idle_nudge'} if section == 'autonomy' and (is_resident(request) or 'opening' in values) else set()
                if section not in ALLOWED or not isinstance(values, dict) or set(values) - (ALLOWED[section] | extra):
                    raise ValueError()
            autonomy = body.get('autonomy', {})
            if 'opening' in autonomy:
                # One shared switch controls web greetings and desktop idle speech.
                # Legacy idle-only clients still require the desktop owner header.
                if 'idle_nudge' in autonomy and autonomy['idle_nudge'] is not (autonomy['opening'] != 'off'):
                    raise ValueError()
                autonomy['idle_nudge'] = autonomy['opening'] != 'off'
            if 'research_daily_max' in autonomy and autonomy['research_daily_max'] is None:
                raise ValueError()
            if 'daily_max' in autonomy.get('tools', {}).get('research', {}):
                # Keep legacy overlays effective while new tool settings are edited.
                autonomy['research_daily_max'] = autonomy['tools']['research']['daily_max']
            new = settings_candidate(pipeline.config, body, data_dir)
            if "inner_life_interval_hours" in body.get("autonomy", {}) and new.autonomy.inner_life_interval_hours not in (0.5, 1, 2, 4, 8):
                raise ValueError()
            path = data_dir / "settings.toml"
            previous = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            with ExitStack() as stack:
                for p in list(pipelines.values()):
                    stack.enter_context(p._lock)
                    p.wait_for_steward()
                saved = overlay(body, {"llm": {"mode": new.llm.mode}})
                with SETTINGS_LOCK:
                    job = app.state.stt_download
                    installer = app.state.stt_installer
                    if installer.device != new.stt.device and job.thread and job.thread.is_alive():
                        raise ValueError('文字起こしモデルの取得中は装置を変更できません。')
                    previous = tomllib.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
                    # Backup preferences are saved independently while this request waits.
                    from cbs.config import BackupConfig
                    new.backup = BackupConfig.model_validate(previous.get('backup', new.backup.model_dump()))
                    persisted = overlay(previous, saved)
                    if persisted.get('costs', {}).get('monthly_guide_usd', 0) is None:
                        persisted['costs'].pop('monthly_guide_usd')
                    atomic_write(path, toml_text(persisted))
                    for p in list(pipelines.values()):
                        own = new.model_copy(deep=True)
                        own.character.id = p.config.character.id
                        p.config = own
                        if not isinstance(p.client, FakeLLMClient):
                            p.client = RoutingLLMClient(own)
                        from cbs.costs import bind
                        bind(p.client, p.store, own)
                        p.store.embedding_model = embedding_model(own)
                        p.store.autonomy_config = own.autonomy
                        p.store.inner_config = own.inner
                    if not pipelines:
                        # Before the first partner, only the bootstrap host is active.
                        pipeline.config = new
                        pipeline.store.embedding_model = embedding_model(new)
                        pipeline.store.autonomy_config = new.autonomy
                        pipeline.store.inner_config = new.inner
                        if not isinstance(pipeline.client, FakeLLMClient):
                            pipeline.client = RoutingLLMClient(new)
                        from cbs.costs import bind
                        bind(pipeline.client, pipeline.store, new)
                    installer.config = new
                    job.installer.config = new
                    if installer.device != new.stt.device:
                        installer.device = new.stt.device
                        installer.engine = None
                        job.installer.device = new.stt.device
                        job.installer.engine = None
                        with job.lock:
                            job.quotes.clear()
        except Exception:
            raise HTTPException(422, "設定値を確認してください。") from None
        if 'tts' in body:
            app.state.tts.reconfigure()
            app.state.start_tts(pipeline)
        app.state.prepare_stt()
        return settings(request)

    def save_key(provider, value):
        name = key_env(pipeline.config, provider)
        if not name or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or any(c in value for c in "\n\r\x00") or len(value) > 4096:
            raise ValueError()
        with ExitStack() as stack:
            hosts = list({id(p): p for p in [pipeline, *pipelines.values()]}.values())
            for p in hosts:
                if hasattr(p, "_lock"):
                    stack.enter_context(p._lock)
                    p.wait_for_steward()
            path = data_dir / "secrets.env"
            keys = read_secrets(path)
            if value:
                keys[name] = value
            else:
                # An empty override also disconnects a key inherited from the environment after restart.
                keys[name] = ""
            atomic_write(path, "".join(k + "=" + json.dumps(v) + "\n" for k, v in keys.items()))
            os.environ[name] = value
            for p in hosts:
                if not isinstance(p.client, FakeLLMClient):
                    p.client = RoutingLLMClient(p.config)
                from cbs.costs import bind
                bind(p.client, p.store, p.config)

    from cbs.web.openrouter_oauth import install_openrouter_oauth
    install_openrouter_oauth(app, pipeline, save_key)

    @app.put("/api/settings/keys")
    async def update_keys(request: Request):
        # Avoid validation errors echoing credential input into an API response.
        try:
            body = await request.json()
            if set(body) != {"provider", "key"} or not isinstance(body["key"], str):
                raise ValueError()
            save_key(body["provider"], body["key"])
        except Exception:
            raise HTTPException(422, "サービスとキーを確認してください。") from None
        return {"provider": body["provider"], "status": "set" if body["key"] else "unset"}

    @app.get("/api/maintenance/reindex")
    def reindex_status():
        return dict(app.state.reindex)

    @app.post("/api/maintenance/reindex", status_code=202)
    async def start_reindex():
        import asyncio
        if app.state.reindex["status"] == "running":
            return dict(app.state.reindex)
        app.state.reindex = {"status": "running", "completed": 0, "total": 0}
        def rebuild():
            from cbs.reindex import reindex
            from cbs.steward import submit
            try:
                with ExitStack() as stack:
                    for p in list(pipelines.values()):
                        stack.enter_context(p._lock)
                        p.wait_for_steward()
                    def progress(done, total):
                        app.state.reindex.update(completed=done, total=total)
                    submit(pipeline.store, reindex, pipeline.store, pipeline.client, pipeline.config, progress).result()
                app.state.reindex["status"] = "completed"
            except Exception:
                app.state.reindex.update(status="failed", error="索引の作り直しに失敗しました。設定を確認して再試行してください。")
        task = asyncio.create_task(asyncio.to_thread(rebuild))
        app.state.bootstrap_tasks.add(task)
        task.add_done_callback(app.state.bootstrap_tasks.discard)
        return dict(app.state.reindex)
