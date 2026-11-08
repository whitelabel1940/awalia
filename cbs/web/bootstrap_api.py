"""Browser bootstrap jobs. Generation stays private until the author confirms."""
import asyncio
from pathlib import Path
from typing import Literal
from threading import Lock
from uuid import uuid4

import yaml
from fastapi import HTTPException, Body
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cbs.bootstrap import Bootstrapper, ReaderCache, domain_catalog, load_questionnaire, questions_by_group
from cbs.bootstrap_schema import AdaptiveQuestion, AnswerValue, BootstrapInput, BootstrapPersona
from cbs.character import load_character, effective_persona, write_persona
from cbs.days import ImportantDay
from cbs.web.voice_text import VOICE_PREVIEW_TEXT


class AugmentInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    answers: dict
    adaptive_questions: list[AdaptiveQuestion] = Field(default_factory=list, max_length=4)
    adaptive_answers: dict[str, AnswerValue] = Field(default_factory=dict)


class ReadInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-z][a-z0-9_\-]{0,63}$')
    text: str | None = None
    persona: BootstrapPersona | None = None
    import_batch_ids: list[str] = Field(default_factory=list, max_length=12)


class ConfirmInput(BaseModel):
    open_style: Literal["hinted", "free"] | None = None
    model_config = ConfigDict(extra='forbid')
    days: list[ImportantDay] = Field(default_factory=list, max_length=2)
    user_name: str = Field(default='', max_length=1000)
    rules: list[str] = Field(default_factory=list, max_length=64)
    about_token: str | None = None
    about_keys: list[str] = Field(default_factory=list, max_length=64)
    about_decisions: dict[str, int | None] = Field(default_factory=dict, max_length=64)


def save_welcome_details(pipeline, body, completed):
    """Checkpoint independent saves; never repeat successful stages on retry."""
    store, cid = pipeline.store, pipeline.config.character.id
    if 'days' not in completed:
        if body.days:
            state = store.days_state(cid)
            kinds = {d.kind for d in body.days}
            days = [d for d in state['days'] if d['kind'] not in kinds]
            days += [{**d.model_dump(), 'name': pipeline.client.sanitize_text(d.name)} for d in body.days]
            store.save_days(cid, days, state['habits'], state['version'])
        completed.add('days')
    if 'about' not in completed:
        if body.about_token:
            from cbs.import_profiles import save_about
            save_about(pipeline, body.about_token, body.about_keys, body.about_decisions)
        completed.add('about')

    if 'requests' not in completed:
        if body.user_name or body.rules:
            state = store.current_requests(pipeline.character)
            fields = dict(state['fields'])
            if body.user_name:
                fields['user_name'] = pipeline.client.sanitize_text(body.user_name)
            lines = [{k: line[k] for k in ('id', 'text')} for line in state['lines']]
            lines += [{'id': None, 'text': pipeline.client.sanitize_text(text)} for text in body.rules]
            store.save_requests(pipeline.character, fields, lines, pipeline.config.requests, state['version'])
        completed.add('requests')


def install_bootstrap(app, base, get_pipeline, characters_root, work_root):
    jobs, tasks = {}, set()
    publish_lock = Lock()
    questionnaire = load_questionnaire()
    reader_cache = ReaderCache()
    def importing(cid: str):
        import re
        from cbs.import_profiles import import_pipeline
        if not re.fullmatch(r'[a-z][a-z0-9_\-]{0,63}',cid) or (characters_root / cid).exists():
            raise HTTPException(409, '新しく迎える人物のIDを選んでください。')
        return import_pipeline(base,cid)
    from cbs.web.import_api import install_import
    install_import(app,importing,prefix='/api/bootstrap/import/{cid}',state_name='bootstrap_import_jobs')

    stages = {'reading': '読み取り', 'questions': '質問の生成', 'building': '人物像の生成', 'validation': '検証', 'review': '確認項目の整理', 'saving': '保存の準備'}

    def engine(jid, root=characters_root):
        def stage(value):
            note = '（いちばん時間がかかる処理です。ふつうは 30 秒〜1 分ほど）' if value == 'building' else ''
            jobs[jid].update(stage=value, progress=f'{stages[value]}を実行中です。{note}',
                             progress_percent=max(jobs[jid].get('progress_percent', 0),
                                                  {'reading': 10, 'questions': 35, 'building': 55, 'validation': 80, 'review': 90, 'saving': 95}[value]))
        return Bootstrapper(base.config, base.client, characters_root=root,
                            store=base.store, trace_root=work_root / 'traces', on_stage=stage, reader_cache=reader_cache)

    def result_screen(directory):
        def read(name):
            return yaml.safe_load((directory / f'{name}.yaml').read_text(encoding='utf-8'))
        model, expression = read('model'), read('expression')
        confirmation = read('confirmation')
        return {'id': model['character_id'], 'name': model['display_name'],
                'introduction': confirmation['introduction'],
                'open_domains': [{'description': d['description']} for d in model['domains'] if d['status'] == 'open'],
                'open_past': [{'about': s['about']} for s in read('reveal')['secrets']],
                'open_now': list(dict.fromkeys(confirmation['open_now'] + expression['existence']['open'])),
                'seed_memories': [{'content': m['content']} for m in read('seed_memories')],
                'author_review': confirmation['author_review'],
                'speaking_style': expression['voice'],
                'setting_conflicts': confirmation.get('setting_conflicts', []),
                'voice': {'enabled': base.config.tts.enabled, 'description': prepare_voice(directory).description}}

    def prepare_voice(directory):
        from cbs.tts import design_voice, read_voice, write_voice
        voice = read_voice(directory)
        setup = Path(app.state.tts.audio_dir).parent / 'setup_voice'
        if not (directory / 'voice.yaml').exists() and base.config.tts.enabled and (setup / 'voice.yaml').is_file():
            import shutil
            configured = read_voice(setup).model_copy(update={'seed': voice.seed})
            if (setup / 'voice').is_dir():
                shutil.copytree(setup / 'voice', directory / 'voice', dirs_exist_ok=True)
            write_voice(directory, configured)
            voice = configured
        return voice if voice.description else design_voice(base, directory, character=load_character(directory.name, root=directory.parent))

    async def submit(operation, work):
        jid = uuid4().hex
        initial = 'validation' if operation == 'augment' else 'reading'
        jobs[jid] = {'id': jid, 'operation': operation, 'status': 'running', 'stage': initial,
                     'progress': f'{stages[initial]}を実行中です。', 'progress_percent': 0}
        async def run():
            try:
                from cbs.shutdown import run_upkeep
                result = await asyncio.to_thread(run_upkeep, base.store, work, jid)
                if base.store.upkeep_stop.is_set():
                    jobs[jid].update(status='cancelled')
                    return
                jobs[jid].update(status='done', progress='完了', progress_percent=100, result=result)
            except Exception as error:
                from cbs.telemetry import record_error
                record_error(base.store, 'bootstrap', error, jobs[jid]['stage'])
                label = stages[jobs[jid]['stage']]
                jobs[jid].update(status='failed', progress='入力を修正して再試行してください。',
                                 error=f'{label}で止まりました。入力と使う AI の設定を確認し、もう一度試してください。')
        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return jobs[jid].copy()

    app.state.bootstrap_tasks = tasks

    @app.get('/wizard')
    def wizard():
        return app.state.render_page('wizard.html')

    @app.get('/api/bootstrap/questionnaire')
    def questions():
        return {'questionnaire': questionnaire, 'domains': domain_catalog()}

    @app.post('/api/bootstrap/plan', status_code=202)
    async def plan(body: BootstrapInput):
        return await submit('plan', lambda jid: engine(jid).plan(body))

    @app.post('/api/bootstrap/read', status_code=202)
    async def read(body: ReadInput):
        return await submit('read', lambda jid: engine(jid).profile(body.id, body.text, persona=body.persona, import_batch_ids=body.import_batch_ids))

    @app.post('/api/bootstrap/adaptive', status_code=202)
    async def adaptive(body: BootstrapInput):
        return await submit('adaptive', lambda jid: engine(jid).adaptive(body))

    @app.post('/api/bootstrap/create', status_code=202)
    async def create(body: BootstrapInput):
        if (characters_root / body.id).exists():
            raise HTTPException(409, 'このIDは使用済みです。別のIDで作成してください。')
        def work(jid):
            directory = engine(jid, work_root / 'drafts' / jid).create(body)
            return result_screen(directory)
        return await submit('create', work)

    @app.post('/api/bootstrap/jobs/{jid}/revise', status_code=202)
    async def revise(jid: str, body: BootstrapInput):
        previous = jobs.get(jid)
        if (not previous or previous['operation'] != 'create' or previous['status'] != 'done'
                or previous.get('confirmed') or previous['result']['id'] != body.id):
            raise HTTPException(409, '確認中の人物像だけ補足できます。')
        directory = work_root / 'drafts' / jid / body.id
        package = {p.stem: yaml.safe_load(p.read_text(encoding='utf-8')) for p in directory.glob('*.yaml')
                   if p.stem in ('model', 'expression', 'world', 'reveal', 'interests', 'seed_memories')}
        def work(new_jid):
            result = engine(new_jid, work_root / 'drafts' / new_jid).create(body, review_base=package)
            return result_screen(result)
        return await submit('create', work)

    @app.get('/api/bootstrap/jobs/{jid}')
    def job(jid: str):
        if jid not in jobs:
            raise HTTPException(404, 'このサーバーでの作業がありません。')
        return {k: v for k, v in jobs[jid].items() if not k.startswith('_')}

    @app.post('/api/bootstrap/jobs/{jid}/voice-preview')
    def preview_voice(jid: str):
        # V-2's optional partner preview; K2 onboarding/download steps are M10b.
        from types import SimpleNamespace
        from fastapi.responses import Response
        from cbs.tts import TTSError
        job = jobs.get(jid)
        if not job or job['operation'] != 'create' or job['status'] != 'done' or job.get('confirmed'):
            raise HTTPException(409, '確認中の人物像がありません。')
        cid = job['result']['id']
        config = base.config.model_copy(deep=True)
        config.character.id = cid
        try:
            data = app.state.tts.synthesize(SimpleNamespace(config=config), VOICE_PREVIEW_TEXT,
                                            directory=work_root / 'drafts' / jid / cid)
            return Response(data, media_type='audio/wav', headers={'Cache-Control': 'no-store'})
        except TTSError as error:
            raise HTTPException(503, str(error)) from None

    @app.post('/api/bootstrap/jobs/{jid}/look-draft')
    def look_draft(jid: str):
        from cbs.look import draft_look
        return look_suggestions(jid, draft_look)

    @app.post('/api/bootstrap/jobs/{jid}/look-expressions')
    def look_expressions(jid: str):
        from cbs.look import suggest_expressions
        return look_suggestions(jid, suggest_expressions)

    def look_suggestions(jid, generate):
        from cbs.look import draft_sources
        from cbs.llm import LLMError
        job = jobs.get(jid)
        if not job or job['operation'] != 'create' or job['status'] != 'done' or job.get('confirmed'):
            raise HTTPException(409, '確認中の人物像がありません。')
        root = work_root / 'drafts' / jid
        character = load_character(job['result']['id'], root=root)
        try:
            sources = draft_sources(character)
            sources['input.json'] = (root / character.model.character_id / 'source' / 'input.json').read_text(encoding='utf-8')
            checked, _ = generate(base.config, base.client, sources, purpose='welcome')
            return checked
        except LLMError:
            raise HTTPException(502, '下書きを作れませんでした。もう一度お試しください。') from None

    @app.post('/api/bootstrap/jobs/{jid}/confirm')
    def confirm(jid: str, body: ConfirmInput = Body(default=ConfirmInput())):
        with publish_lock:
            job = jobs.get(jid)
            if not job or job['operation'] != 'create' or job['status'] != 'done':
                raise HTTPException(409, '作成が完了していません。')
            cid = job['result']['id']
            if not job.get('confirmed'):
                if (characters_root / cid).exists():
                    raise HTTPException(409, 'このIDは使用済みです。別のIDで作成してください。')
                draft_root = work_root / 'drafts' / jid
                try:
                    prepare_voice(draft_root / cid)
                except Exception as error:
                    from cbs.telemetry import record_error
                    record_error(base.store, 'bootstrap', error, 'saving')
                    raise HTTPException(502, '声の説明を作れませんでした。もう一度お試しください。') from None
                if body.open_style is not None:
                    persona = effective_persona(load_character(cid, root=draft_root), base.config)
                    write_persona(draft_root / cid, persona.model_copy(update={'open_style': body.open_style}))
                characters_root.mkdir(parents=True, exist_ok=True)
                (draft_root / cid).rename(characters_root / cid)
                job['confirmed'] = True
            try:
                # These can fail after publication too; keep them retryable.
                from cbs.web.settings_api import remember_partner
                completed = job.setdefault('_welcome_saved', set())
                if 'selection' not in completed:
                    remember_partner(app.state.settings_dir, cid)
                    completed.add('selection')
                current = get_pipeline(cid)
                if 'tts' not in completed:
                    app.state.start_tts(current)
                    completed.add('tts')
                with current._lock:
                    save_welcome_details(current, body, completed)
            except Exception as error:
                from cbs.telemetry import record_error
                record_error(base.store, 'bootstrap', error, 'saving')
                raise HTTPException(502, 'パートナーは迎えましたが、追加の保存が途中です。同じボタンでもう一度保存してください。') from None
            return {'id': cid}

    @app.get('/api/characters')
    def characters():
        result = []
        for directory in sorted(characters_root.glob('*')):
            if directory.is_dir() and (directory / 'model.yaml').is_file():
                character = load_character(directory.name, root=characters_root)
                result.append({'id': directory.name, 'name': character.model.display_name})
        return result

    @app.post('/api/characters/{cid}/switch')
    def switch(cid: str):
        p = get_pipeline(cid)
        return {'id': cid, 'name': p.character.model.display_name}

    def unanswered(cid):
        p = get_pipeline(cid)
        model = p.store.current_model(p.character)
        prior = p.store.bootstrap_answers(cid)
        evidence = {ref for d in model.domains for ref in d.evidence}
        source = characters_root / cid / 'source' / 'input.json'
        original = BootstrapInput.model_validate_json(source.read_text(encoding='utf-8')).answers if source.is_file() else {}
        return [q for qid, q in questions_by_group(questionnaire)['answers'].items()
                if qid not in original and f'author:{qid}' not in prior and f'author:{qid}' not in evidence
                and not any(d.id == q['domain'] and any(ref.startswith(('author:', 'text:')) for ref in d.evidence) for d in model.domains)]

    @app.get('/api/characters/{cid}/augment')
    def augment_questions(cid: str):
        return {'questions': unanswered(cid)}

    def augment_data(cid, body):
        p = get_pipeline(cid)
        if set(body.answers) - {q['id'] for q in unanswered(cid)}:
            raise HTTPException(409, '未回答の質問だけ追加できます。')
        try:
            return p, BootstrapInput(id=cid, basics={'name': p.character.model.display_name}, answers=body.answers,
                                     persona=effective_persona(p.character, p.config).model_dump(),
                                     adaptive_questions=body.adaptive_questions, adaptive_answers=body.adaptive_answers)
        except ValidationError as error:
            raise HTTPException(422, error.errors(include_input=False, include_context=False)) from None

    @app.post('/api/characters/{cid}/adaptive', status_code=202)
    async def augment_adaptive(cid: str, body: AugmentInput):
        p, data = augment_data(cid, body)
        def work(jid):
            with p._lock:
                if p._post_future:
                    p._post_future.result()
                p.wait_for_steward()
                return engine(jid).adaptive(data, existing_id=cid)
        return await submit('adaptive', work)

    @app.post('/api/characters/{cid}/augment', status_code=202)
    async def augment(cid: str, body: AugmentInput):
        p, data = augment_data(cid, body)
        def work(jid):
            with p._lock:
                if p._post_future:
                    p._post_future.result()
                p.wait_for_steward()
                result = engine(jid).augment(cid, data.answers, adaptive_questions=data.adaptive_questions,
                                          adaptive_answers=data.adaptive_answers)
                p.character = p.character.model_copy(update={'sources': p.store.author_sources(p.character)})
                return result
        return await submit('augment', work)
