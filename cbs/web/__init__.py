from pathlib import Path
import asyncio
import re
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from cbs.llm import LLMError
from cbs.llm.errors import failure_notice
from cbs.runtime import create_pipeline

STATIC = Path(__file__).parent / "static"


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="新しい会話", min_length=1, max_length=100)


class ProactiveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    after_turn_id: int | None = Field(default=None, ge=0)


class OpeningInput(ProactiveInput):
    background: bool = False


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(default="", max_length=20_000)
    photos: list[str] = Field(default_factory=list, max_length=12)


class DiaryCommentInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    comment: str = Field(max_length=2000)


def create_app(pipeline=None, *, character_id=None, db_path=None, characters_root=None, work_root=None, settings_dir=None,
               resident_token=None, shutdown=None, secrets_dir=None, without_keys=False):
    from threading import Lock
    from cbs.config import ROOT, load_config
    from cbs.character import load_character
    from cbs.pipeline import TurnPipeline
    from cbs.web.bootstrap_host import BootstrapHost
    from cbs.paths import resolve_paths
    explicit_data = settings_dir or (Path(db_path).parent if db_path else None)
    if pipeline is not None and explicit_data is None:
        explicit_data = pipeline.store.path.parent
    data_dir, characters_root = resolve_paths(
        explicit_data, characters_root or getattr(pipeline, 'characters_root', None))
    work_root = Path(work_root) if work_root is not None else data_dir / 'web_bootstrap'
    store_path = Path(db_path) if db_path is not None else data_dir / 'cbs.sqlite3'
    if pipeline is None:
        from cbs.restore import apply_staged
        restore_data = data_dir
        store_path = Path(db_path or restore_data / 'cbs.sqlite3')
        apply_staged(restore_data, characters_root, store_path, config_path=ROOT / 'config.toml')
        config = load_config(data_dir=data_dir, secrets_dir=secrets_dir, without_keys=without_keys)
        cid = character_id or config.character.id
        # A newly welcomed single partner also survives a browser/storage reset.
        if not character_id and not (characters_root / cid / 'model.yaml').is_file():
            available = sorted(characters_root.glob('*/model.yaml'))
            if len(available) == 1:
                cid = available[0].parent.name
        if (characters_root / cid / 'model.yaml').is_file():
            if characters_root == resolve_paths()[1] and explicit_data is None:
                pipeline = create_pipeline(cid, db_path=db_path)
            else:
                from cbs.llm import create_client
                from cbs.store import Store
                from cbs.config import embedding_model
                config.character.id = cid
                pipeline = TurnPipeline(config, load_character(cid, root=characters_root),
                                        Store(store_path, memory_config=config.memory, embedding_model=embedding_model(config),
                                              characters_root=characters_root, current_character_id=cid, config_path=ROOT / 'config.toml'), create_client(config))
        elif character_id:
            raise ValueError('指定したパートナーがありません。')
        else:
            from cbs.llm import create_client
            from cbs.store import Store
            from cbs.config import embedding_model
            pipeline = BootstrapHost(config, Store(store_path, memory_config=config.memory, embedding_model=embedding_model(config),
                                                   characters_root=characters_root, config_path=ROOT / 'config.toml'), create_client(config))
    pipeline.characters_root = characters_root
    pipelines = {} if isinstance(pipeline, BootstrapHost) else {pipeline.config.character.id: pipeline}
    pipeline_lock = Lock()

    def get_pipeline(cid):
        if not cid or not re.fullmatch(r'[a-z][a-z0-9_\-]{0,63}', cid):
            raise HTTPException(404, 'キャラクターがありません。')
        with pipeline_lock:
            if cid not in pipelines:
                if not (characters_root / cid / 'model.yaml').is_file():
                    raise HTTPException(404, 'キャラクターがありません。')
                config = pipeline.config.model_copy(deep=True)
                config.character.id = cid
                pipelines[cid] = TurnPipeline(config, load_character(cid, root=characters_root), pipeline.store, pipeline.client)
                if isinstance(pipeline, BootstrapHost) and pipeline.active is None:
                    pipeline.active = pipelines[cid]
            pipelines[cid].characters_root = characters_root
            return pipelines[cid]

    def selected(character_id: str | None = None):
        return get_pipeline(character_id or pipeline.config.character.id)

    @asynccontextmanager
    async def lifespan(app):
        stopped = asyncio.Event()
        app.state.tts_loop = asyncio.get_running_loop()
        app.state.prepare_stt()
        for current in list(pipelines.values()):
            app.state.start_tts(current)

        async def sweep():
            while not stopped.is_set():
                for current in list(pipelines.values()):
                    await asyncio.to_thread(current.maintenance)
                await asyncio.to_thread(app.state.tts.prune, pipeline.config.tts.cache_days)
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=300)
                except TimeoutError:
                    pass

        news_task = asyncio.create_task(app.state.news.run())
        telemetry_task = asyncio.create_task(app.state.telemetry.run())
        task = asyncio.create_task(sweep())
        async def monitor_stt():
            while not stopped.is_set():
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=5)
                except TimeoutError:
                    app.state.prepare_stt()
        stt_task = asyncio.create_task(monitor_stt())
        try:
            yield
        finally:
            stopped.set()
            news_task.cancel()
            telemetry_task.cancel()
            await asyncio.gather(telemetry_task, return_exceptions=True)
            await asyncio.gather(news_task, return_exceptions=True)
            app.state.import_jobs.close()
            app.state.bootstrap_import_jobs.close()
            await asyncio.to_thread(app.state.voice_download.close)
            await asyncio.to_thread(app.state.embedding_download.close)
            await asyncio.to_thread(app.state.stt_download.close)
            await asyncio.to_thread(app.state.stt_server.close)
            await stt_task
            try:
                await task
                await asyncio.gather(*list(app.state.bootstrap_tasks))
            finally:
                await asyncio.to_thread(app.state.tts.stop)
                if app.state.voice_candidates is not None:
                    app.state.voice_candidates.cleanup()
            for current in list(pipelines.values()):
                await asyncio.to_thread(current.maintenance, close=True)
                await asyncio.to_thread(current.wait_for_steward)
            await asyncio.to_thread(pipeline.store.costs.flush)
            await asyncio.to_thread(app.state.backup.close)

    app = FastAPI(title="Awalia", lifespan=lifespan)
    from cbs.web.resident import install_resident
    install_resident(app, resident_token, shutdown, selected)
    app.state.pipeline = pipeline
    app.state.settings_dir = Path(settings_dir or pipeline.store.path.parent)
    from cbs.first_run import trial_info
    app.state.first_run = trial_info(app.state.settings_dir) is not None
    from cbs.news import install_news
    news_config = pipeline.config.news.model_copy()
    if app.state.first_run:
        news_config.enabled = False
    install_news(app, news_config, data_dir)
    from cbs.telemetry import install_telemetry
    install_telemetry(app, pipeline, app.state.settings_dir)
    from cbs.share import install_share
    install_share(app, selected, app.state.settings_dir)
    from cbs.web.restore_api import install_restore
    install_restore(app, pipeline, app.state.settings_dir)
    from cbs.web.stt_api import install_stt
    install_stt(app, pipeline, selected, app.state.settings_dir)
    from cbs.web.voice_api import install_voice
    install_voice(app, selected, characters_root, settings_dir or pipeline.store.path.parent)
    from cbs.web.reading_api import install_reading
    install_reading(app, selected)
    from cbs.web.bootstrap_api import install_bootstrap
    install_bootstrap(app, pipeline, get_pipeline, characters_root, work_root)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def revalidate_static(request, call_next):
        # Modules import each other; a cached old file next to a new one stalls startup
        # after an update. Always revalidate (ETag makes unchanged files a cheap 304).
        response = await call_next(request)
        if request.url.path.startswith("/static/") or request.url.path in ("/", "/wizard"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.middleware("http")
    async def revalidate_static(request, call_next):
        if app.state.resident_draining and request.url.path != '/api/resident/shutdown':
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail': '終了の準備をしています。'}, status_code=503)
        # ES modules are otherwise cached heuristically, so an updated app would run stale screens.
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def index():
        return page('index.html')

    def page(name):
        if not app.state.first_run:
            return FileResponse(STATIC / name)
        from fastapi.responses import HTMLResponse
        text = (STATIC / name).read_text(encoding='utf-8')
        text = text.replace('</head>', '<link rel="stylesheet" href="/static/first-run.css"></head>', 1)
        text = text.replace('</title>', ' · お試し</title>', 1)
        text = text.replace('<body>', '<body><aside class="trial-banner" role="status">お試し — 記録は通常の環境と別に保存されます。</aside>', 1)
        return HTMLResponse(text, headers={'Cache-Control': 'no-store'})

    app.state.render_page = page

    @app.get("/dev")
    def inspector():
        return page('dev.html')

    @app.get("/api/partner")
    def partner(character_id: str | None = None):
        cid = character_id or pipeline.config.character.id
        if not cid:
            return {"partner": None}
        if not re.fullmatch(r'[a-z][a-z0-9_\-]{0,63}', cid):
            raise HTTPException(404, 'パートナーがありません。')
        if not (characters_root / cid / 'model.yaml').is_file():
            return {"partner": None}
        current = get_pipeline(cid)
        return {"partner": {"id": cid, "name": current.character.model.display_name}}

    @app.get("/api/sessions")
    def sessions(pipeline=Depends(selected)):
        return [s for s in pipeline.store.list_sessions() if s['character_id'] == pipeline.config.character.id]

    @app.post("/api/sessions", status_code=201)
    def create_session(body: SessionInput, pipeline=Depends(selected)):
        title = pipeline.client.sanitize_text(body.title).strip()
        if not title:
            raise HTTPException(422, "セッション名を入力してください。")
        with pipeline._lock:
            session = pipeline.store.create_session(pipeline.config.character.id, title)
            from cbs.memory import enqueue_pending
            enqueue_pending(pipeline)
        return session

    @app.get("/api/sessions/{session_id}/turns")
    def history(session_id: str, pipeline=Depends(selected)):
        try:
            if pipeline.store.get_session(session_id)['character_id'] != pipeline.config.character.id:
                raise KeyError(session_id)
            turns = pipeline.store.history(session_id)
            from cbs.photos import display_text
            with pipeline.store.connect() as db:
                notices = {r[0] for r in db.execute('SELECT source_turn FROM forming_notices WHERE character_id=?', (pipeline.config.character.id,))}
                photos = {}
                for row in db.execute('SELECT p.source_turn,p.id,p.description,p.width,p.height FROM photos p JOIN turns t ON t.id=p.source_turn WHERE t.session_id=? ORDER BY p.rowid', (session_id,)):
                    item = dict(row)
                    photos.setdefault(item.pop('source_turn'), []).append(item)
            turns = [{**t, 'photos': photos.get(t['id'], []),
                      'user_text': display_text(t['user_text'], photos.get(t['id'], []))} for t in turns]
            day_notices = pipeline.store.day_notices(pipeline.config.character.id)
            request_notices = pipeline.store.request_notices(pipeline.config.character.id)
            return [{**t, 'day_notices': day_notices.get(t['id'], []), 'request_notices': request_notices.get(t['id'], []), 'self_forming': pipeline.config.early.notify and t['id'] in notices} for t in turns]
        except KeyError:
            raise HTTPException(404, "セッションがありません。") from None

    @app.post("/api/sessions/{session_id}/messages")
    def send_message(session_id: str, body: MessageInput, background_tasks: BackgroundTasks, pipeline=Depends(selected)):
        try:
            return pipeline.run(session_id, body.content, background_tasks=background_tasks, photos=body.photos)
        except KeyError:
            raise HTTPException(404, "セッションがありません。") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except LLMError as error:
            from fastapi.responses import JSONResponse
            notice = failure_notice(error, pipeline.character.model.display_name)
            return JSONResponse({"detail": notice["message"], **notice}, status_code=502)

    @app.get("/api/llm/failures")
    def llm_failures(pipeline=Depends(selected)):
        from cbs.llm.failures import FailureState
        return {"notices": FailureState(pipeline.store).notices(pipeline.character.model.display_name)}

    @app.get("/api/turns/{turn_id}/trace")
    def trace(turn_id: int, spoilers: bool = False, pipeline=Depends(selected)):
        try:
            with pipeline.store.connect() as db:
                row = db.execute('SELECT s.character_id FROM turns t JOIN sessions s ON s.id=t.session_id WHERE t.id=?', (turn_id,)).fetchone()
            if row is None or row['character_id'] != pipeline.config.character.id:
                raise KeyError(turn_id)
            from cbs.reveal import inspector_trace
            return inspector_trace(pipeline.store.get_trace(turn_id), spoilers)
        except KeyError:
            raise HTTPException(404, "ターンがありません。") from None

    @app.get("/api/character")
    def character(pipeline=Depends(selected)):
        from cbs.interests import timed_interest
        from cbs.steward import episode_counts
        model = pipeline.store.current_model(pipeline.character)
        episodes = pipeline.store.list_episodes(pipeline.config.character.id)
        return {"model": model.model_dump(mode="json"),
                "initial_status": {d.id: d.status for d in pipeline.character.model.domains},
                "episode_counts": {d.id: episode_counts(episodes, d.id) for d in model.domains},
                "expression": pipeline.store.current_character(pipeline.character).expression.model_dump(mode="json", by_alias=True),
                "canon_facts": pipeline.store.list_canon(pipeline.config.character.id),
                "interests": [{**timed_interest(i), "progress": i["progress"]} for i in pipeline.store.list_interests(pipeline.config.character.id)],
                "now_summary": pipeline.store.now_summary(pipeline.config.character.id)}

    @app.get("/api/model/versions")
    def model_versions(pipeline=Depends(selected)):
        return pipeline.store.list_model_versions(pipeline.config.character.id)

    @app.get("/api/model/changes")
    def changes(pipeline=Depends(selected)):
        from cbs.model import model_changes
        return {"versions": pipeline.store.list_model_versions(pipeline.config.character.id),
                "interest_changes": pipeline.store.list_interest_changes(pipeline.config.character.id),
                "steward_runs": pipeline.store.list_steward_runs(pipeline.config.character.id),
                "changes": model_changes(pipeline.character.model, pipeline.store.current_model(pipeline.character))}

    @app.get("/api/episodes")
    def episodes(pipeline=Depends(selected)):
        return pipeline.store.list_episodes(pipeline.config.character.id)

    @app.get("/api/ledger")
    def ledger(pipeline=Depends(selected)):
        return pipeline.store.list_plans(pipeline.config.character.id)

    @app.get("/api/secrets")
    def secrets(spoilers: bool = False, pipeline=Depends(selected)):
        from cbs.reveal import secret_states
        return secret_states(pipeline.character, pipeline.store, spoilers=spoilers)

    @app.post("/api/proactive/opening")
    def opening(body: OpeningInput, request: Request, pipeline=Depends(selected)):
        if body.background:
            from cbs import clock
            from cbs.autonomy import quiet_hours
            from cbs.web.resident import is_resident
            if not is_resident(request):
                raise HTTPException(403, '常駐アプリからだけ使えます。')
            if quiet_hours(clock.now().hour, pipeline.config.autonomy):
                # Keep the startup greeting pending; do not record web_openings yet.
                return {'speak': False, 'deferred': True, 'reason': '静かな時間帯のため挨拶を保留しています。'}
        try:
            return pipeline.proactive(body.session_id, after_turn_id=body.after_turn_id)
        except KeyError:
            raise HTTPException(404, "セッションがありません。") from None

    @app.post("/api/proactive/idle")
    def idle(body: ProactiveInput, pipeline=Depends(selected)):
        try:
            return pipeline.proactive(body.session_id, "idle", after_turn_id=body.after_turn_id)
        except KeyError:
            raise HTTPException(404, "セッションがありません。") from None

    @app.post('/api/proactive/habit')
    def habit(body: ProactiveInput, pipeline=Depends(selected)):
        try:
            return pipeline.proactive(body.session_id, 'habit', after_turn_id=body.after_turn_id)
        except KeyError:
            raise HTTPException(404, 'セッションがありません。') from None

    @app.get("/api/activity")
    def activity(pipeline=Depends(selected)):
        from cbs.autonomy import inspector_activity
        return inspector_activity(pipeline)

    @app.get("/api/memory")
    def memory(pipeline=Depends(selected)):
        from cbs.memory import inspector_memory
        return inspector_memory(pipeline.store, pipeline.config.character.id)

    @app.get('/api/inner')
    def inner(pipeline=Depends(selected)):
        from cbs.inner import inspector_inner
        return inspector_inner(pipeline)

    from cbs.web.settings_api import install_settings
    from cbs.web.look_api import install_look
    from cbs.web.stream_api import install_stream
    from cbs.note import public_note
    from cbs.web.knowing_api import install_knowing
    from cbs.web.import_api import install_import
    install_import(app, selected)
    install_knowing(app, selected)
    install_settings(app, pipeline, pipelines, settings_dir or pipeline.store.path.parent, selected)
    install_look(app, selected, get_pipeline, characters_root)
    install_stream(app, selected, MessageInput)
    from cbs.web.photos_api import install_photos
    install_photos(app, selected)

    @app.get('/api/note')
    def note(pipeline=Depends(selected)):
        return {**public_note(pipeline), 'share_enabled': not app.state.first_run}

    @app.get('/api/note/activities')
    def note_activities(week: str, pipeline=Depends(selected)):
        from datetime import date, timedelta
        from cbs.note import activity_notes
        try:
            start = date.fromisoformat(week)
            if start.isoformat() != week or start.weekday() != 0:
                raise ValueError()
            until = (start + timedelta(days=7)).isoformat()
        except (ValueError, OverflowError):
            raise HTTPException(422, '週の開始日（月曜日）を確認してください。') from None
        return {'activities': activity_notes(pipeline.store, pipeline.config.character.id, since=week, until=until)}

    @app.put('/api/note/diaries/{day}/comment')
    def diary_comment(day: str, body: DiaryCommentInput, pipeline=Depends(selected)):
        from datetime import date
        from cbs import clock
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError()
        except ValueError:
            raise HTTPException(422, '日付を確認してください。')
        with pipeline.store.connect() as db:
            changed = db.execute('UPDATE diaries SET user_comment=?,comment_at=? WHERE character_id=? AND day=?',
                (body.comment.strip(),clock.iso(),pipeline.config.character.id,day)).rowcount
        if not changed:
            raise HTTPException(404, '日記が見つかりません。')
        return {'saved':True}

    return app
