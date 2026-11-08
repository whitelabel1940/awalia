"""Voice settings and synthesis, outside the chat/persistence path."""
import asyncio
import base64
import json
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import unquote
from threading import Event
from uuid import uuid4

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from cbs.tts import TTSService, TTSError, VoiceConfig, design_voice, read_voice, write_voice, wav_info
from cbs.expression import EXPRESSIONS
from cbs.config import TTSConfig
from cbs.web.voice_text import VOICE_PREVIEW_TEXT


class PreviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(default=VOICE_PREVIEW_TEXT, min_length=1, max_length=300)
    expression: str = 'neutral'
    candidate_id: str = Field(default='', pattern=r'^(|[a-f0-9]{32})$')


class VoiceUpdate(VoiceConfig):
    apply_references: bool = False
    candidate_id: str = Field(default='', pattern=r'^(|[a-f0-9]{32})$')


class CandidateInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    voice: VoiceConfig
    tts: TTSConfig
    candidate_id: str = Field(default='', pattern=r'^(|[a-f0-9]{32})$')


class SampleInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidate_id: str = Field(default='', pattern=r'^(|[a-f0-9]{32})$')


class DownloadInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    choice: str = Field(default='auto', pattern=r'^(auto|q8_0|q4_k)$')
    token: str = ''
    confirmed: bool = False


def reference_list(directory):
    directory = Path(directory)
    path = directory / 'voice' / 'references.json'
    if path.is_file():
        return json.loads(path.read_text(encoding='utf-8'))
    # M10 migration: omit generated samples, retain uploaded WAVs and their order.
    return [{'id': p.name, 'name': p.name} for p in sorted((directory / 'voice').glob('*.wav')) if not p.name.startswith('sample-')]


def save_references(directory, references):
    from cbs.web.settings_api import atomic_write
    atomic_write(Path(directory) / 'voice' / 'references.json', json.dumps(references, ensure_ascii=False))


def install_voice(app, selected, characters_root, data_dir):
    service = app.state.tts = TTSService(data_dir, characters_root)
    app.state.tts_loop = None
    app.state.voice_candidates = None
    candidates = {}

    def start_tts(pipeline):
        loop = app.state.tts_loop
        if loop is None or loop.is_closed():
            return
        def schedule():
            task = asyncio.create_task(asyncio.to_thread(service.startup, pipeline))
            app.state.bootstrap_tasks.add(task)
            task.add_done_callback(app.state.bootstrap_tasks.discard)
        loop.call_soon_threadsafe(schedule)
    app.state.start_tts = start_tts

    from cbs.tts_download import DownloadJob, SpeechInstaller
    job = app.state.voice_download = DownloadJob(SpeechInstaller(Path(data_dir) / 'tools' / 'speech'))

    @app.get('/api/voice/download')
    def download_status(choice: str = 'auto'):
        if choice not in ('auto', 'q8_0', 'q4_k'):
            raise HTTPException(422, 'モデルを確認してください。')
        try:
            return job.status(choice)
        except Exception:
            raise HTTPException(503, '取得の状態を確認できません。再試行してください。') from None

    @app.post('/api/voice/download/plan')
    def download_plan(body: DownloadInput):
        try:
            return job.quote(body.choice)
        except Exception:
            raise HTTPException(503, 'モデルと GPU の状態を確認できません。') from None

    @app.post('/api/voice/download')
    def download_start(body: DownloadInput):
        try:
            return job.start(body.token, body.confirmed)
        except TTSError as error:
            raise HTTPException(409, str(error)) from None

    @app.post('/api/voice/download/cancel')
    def download_cancel():
        job.cancel()
        return {'status': 'cancelling'}

    def editor(prefix, provider, directory_for, *, setup=False):
        def candidate_for(token, pipeline):
            candidate = candidates.get(token)
            if candidate is None or candidate.owner != directory_for(pipeline):
                raise HTTPException(404, '声の候補がありません。もう一度試してください。')
            return candidate

        def candidate_pipeline(candidate, pipeline):
            return SimpleNamespace(config=pipeline.config.model_copy(update={'tts': candidate.tts}), client=pipeline.client)

        @app.post(prefix + '/candidate')
        def prepare_candidate(body: CandidateInput, pipeline=Depends(provider)):
            with service.lock:
                source = directory_for(pipeline)
                if body.candidate_id:
                    candidate = candidate_for(body.candidate_id, pipeline)
                    token = body.candidate_id
                else:
                    token = uuid4().hex
                    if app.state.voice_candidates is None:
                        Path(data_dir).mkdir(parents=True, exist_ok=True)
                        app.state.voice_candidates = TemporaryDirectory(prefix='voice-candidates-', dir=data_dir)
                    directory = Path(app.state.voice_candidates.name) / token
                    (directory / 'voice').mkdir(parents=True)
                    candidate = SimpleNamespace(owner=source, directory=directory, tts=body.tts, reference_key=None)
                    original = read_voice(source)
                    for name in {original.reference, original.sample_reference} - {''}:
                        if (source / 'voice' / name).is_file():
                            shutil.copy2(source / 'voice' / name, directory / 'voice' / name)
                    write_voice(directory, original)
                    candidates[token] = candidate
                previous = read_voice(candidate.directory)
                voice = body.voice.model_copy(update={
                    'seed': read_voice(source).seed,
                    'reference': previous.reference,
                    'sample_reference': previous.sample_reference,
                    'sample_description': previous.sample_description,
                    'description': pipeline.client.sanitize_text(body.voice.description),
                })
                if voice.mode == 'design' and not voice.description.strip():
                    raise HTTPException(422, '声の説明を入力してください。')
                if voice.mode == 'clone' and body.tts.engine == 'speech.cpp':
                    clips = public(pipeline)['references']
                    if not clips or sum(c['seconds'] for c in clips) > 120:
                        raise HTTPException(422, '参照音声を1本以上、合計120秒以内にしてください。')
                    key = ([c['id'] for c in clips], body.tts.local_model, voice.steps)
                    if candidate.reference_key != key:
                        for clip in clips:
                            shutil.copy2(source / 'voice' / clip['id'], candidate.directory / 'voice' / clip['id'])
                        try:
                            voice.reference = service.combine_references(body.tts, candidate.directory, key[0])
                        except TTSError as error:
                            raise HTTPException(503, str(error)) from None
                        candidate.reference_key = key
                    else:
                        voice.reference = previous.reference
                candidate.tts = body.tts
                write_voice(candidate.directory, voice)
                return {'candidate_id': token, 'voice': voice.model_dump()}

        def public(pipeline):
            directory = directory_for(pipeline)
            clips = []
            for item in reference_list(directory):
                params = wav_info((directory / 'voice' / item['id']).read_bytes())
                clips.append({**item, 'seconds': params.nframes / params.framerate})
            total = sum(c['seconds'] for c in clips)
            return {'voice': read_voice(directory).model_dump(), 'references': clips, 'total_seconds': total,
                    'preview_text': VOICE_PREVIEW_TEXT,
                    'reference_warning': '上限の120秒を超えています。音声を外してください。' if total > 120 else '合計30秒前後が目安です。短い音声を追加できます。' if total < 25 else '',
                    'warning': service.start_error}

        @app.get(prefix)
        def voice(pipeline=Depends(provider)):
            with service.lock:
                return public(pipeline)

        @app.put(prefix)
        def save_voice(body: VoiceUpdate, pipeline=Depends(provider)):
            with service.lock:
                directory = directory_for(pipeline)
                previous = read_voice(directory)
                if body.candidate_id:
                    candidate = candidate_for(body.candidate_id, pipeline)
                    if candidate.tts != pipeline.config.tts:
                        raise HTTPException(409, '音声の設定が変わりました。もう一度試してください。')
                    voice = read_voice(candidate.directory)
                    for name in {voice.reference, voice.sample_reference} - {''}:
                        path = candidate.directory / 'voice' / name
                        if path.is_file():
                            (directory / 'voice').mkdir(parents=True, exist_ok=True)
                            shutil.copy2(path, directory / 'voice' / name)
                    write_voice(directory, voice)
                    service.reconfigure()
                    return public(pipeline)
                voice = VoiceConfig.model_validate(body.model_dump(exclude={'apply_references', 'candidate_id'}))
                voice.seed = previous.seed
                voice.sample_reference, voice.sample_description = previous.sample_reference, previous.sample_description
                voice.description = pipeline.client.sanitize_text(voice.description)
                if voice.mode == 'clone' and pipeline.config.tts.engine == 'speech.cpp':
                    if body.apply_references:
                        clips = public(pipeline)['references']
                        if not clips or sum(c['seconds'] for c in clips) > 120:
                            raise HTTPException(422, '参照音声を1本以上、合計120秒以内にしてください。')
                        try:
                            voice.reference = service.combine_references(pipeline.config.tts, directory, [c['id'] for c in clips])
                        except TTSError as error:
                            raise HTTPException(503, str(error)) from None
                    else:
                        # Ordinary settings saves do not apply edits to the reference list.
                        voice.reference = previous.reference
                        if not voice.reference:
                            raise HTTPException(422, '参照音声を追加して「この声で決める」を押してください。')
                if voice.mode == 'design' and not voice.description.strip():
                    if setup:
                        voice.description = '落ち着いた自然な声。柔らかく、聞き取りやすい速さで話す。'
                    else:
                        try:
                            voice.description = design_voice(pipeline, directory, persist=False).description
                        except Exception:
                            raise HTTPException(502, '声の説明を作れませんでした。AIの接続設定を確認してください。') from None
                write_voice(directory, voice)
                service.reconfigure()
            return public(pipeline)

        @app.post(prefix + '/design')
        def design(pipeline=Depends(provider)):
            if setup:
                raise HTTPException(409, 'あなたのパートナーを迎えたあと、人物の設定から説明を作れます。')
            try:
                with service.lock:
                    voice = design_voice(pipeline, directory_for(pipeline), persist=False)
                    return {**public(pipeline), 'voice': voice.model_dump()}
            except Exception:
                raise HTTPException(502, '声の説明を作れませんでした。AIの接続設定を確認してください。') from None

        @app.put(prefix + '/reference')
        async def reference(request: Request, pipeline=Depends(provider)):
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 20 * 1024 * 1024:
                    raise HTTPException(413, 'WAVは20MB以下にしてください。')
            try:
                params = wav_info(bytes(data))
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            if params.nframes / params.framerate > 120:
                raise HTTPException(422, '1本の音声は120秒以内にしてください。')
            with service.lock:
                directory = directory_for(pipeline)
                clips = reference_list(directory)
                if len(clips) >= 100:
                    raise HTTPException(422, '参照音声は100本以内にしてください。')
                destination = directory / 'voice'
                destination.mkdir(parents=True, exist_ok=True)
                name = uuid4().hex + '.wav'
                label = unquote(request.headers.get('X-Filename', 'reference.wav')).replace('\\', '/').split('/')[-1]
                label = ''.join(c for c in label if ord(c) >= 32)[:200] or 'reference.wav'
                (destination / name).write_bytes(data)
                save_references(directory, [*clips, {'id': name, 'name': label}])
                return {**public(pipeline), 'uploaded': name}

        @app.delete(prefix + '/references')
        def clear_references(pipeline=Depends(provider)):
            with service.lock:
                # Applied voices are independent GGUFs; retain source files for in-flight auditions.
                save_references(directory_for(pipeline), [])
                return public(pipeline)

        @app.delete(prefix + '/references/{name}')
        def remove_reference(name: str, pipeline=Depends(provider)):
            with service.lock:
                directory = directory_for(pipeline)
                clips = reference_list(directory)
                if name not in {c['id'] for c in clips}:
                    raise HTTPException(404, '参照音声がありません。')
                save_references(directory, [c for c in clips if c['id'] != name])
                return public(pipeline)

        @app.get(prefix + '/references/{name}')
        def get_reference(name: str, pipeline=Depends(provider)):
            directory = directory_for(pipeline)
            if name not in {c['id'] for c in reference_list(directory)}:
                raise HTTPException(404, '参照音声がありません。')
            return FileResponse(directory / 'voice' / name, media_type='audio/wav', headers={'Cache-Control': 'no-store'})

        @app.post(prefix + '/sample')
        def regenerate_sample(body: SampleInput = SampleInput(), pipeline=Depends(provider)):
            with service.lock:
                directory = directory_for(pipeline)
                if body.candidate_id:
                    candidate = candidate_for(body.candidate_id, pipeline)
                    directory, pipeline = candidate.directory, candidate_pipeline(candidate, pipeline)
                if not pipeline.config.tts.enabled or pipeline.config.tts.engine != 'speech.cpp':
                    raise HTTPException(409, 'このPCの音声をオンにしてから作ってください。')
                try:
                    voice = service.design_sample(pipeline, directory, regenerate=True)
                    if body.candidate_id:
                        return {'voice': voice.model_dump(), 'candidate_id': body.candidate_id}
                    service.reconfigure()
                    return public(pipeline)
                except Exception:
                    raise HTTPException(503, '見本の音声を作れませんでした。取得の状態を確認してください。') from None

        @app.post(prefix + '/preview')
        def preview(body: PreviewInput, pipeline=Depends(provider)):
            if body.expression not in EXPRESSIONS:
                raise HTTPException(422, '表情を確認してください。')
            with service.lock:
                directory = directory_for(pipeline)
                if body.candidate_id:
                    candidate = candidate_for(body.candidate_id, pipeline)
                    directory, pipeline = candidate.directory, candidate_pipeline(candidate, pipeline)
                    # An explicit candidate preview works even with automatic audio disabled.
                    pipeline.config.tts = pipeline.config.tts.model_copy(update={'enabled': True})
                response = synthesize(pipeline, pipeline.client.sanitize_text(body.text), body.expression, directory=directory)
                if body.candidate_id and read_voice(directory).sample_reference:
                    response.headers['X-Voice-Sample'] = 'ready'
                return response

    editor('/api/voice', selected, service.directory)
    editor('/api/setup/voice', lambda: app.state.pipeline, lambda _: Path(data_dir) / 'setup_voice', setup=True)

    def synthesize(pipeline, text, expression, turn_id=None, directory=None):
        try:
            return Response(service.synthesize(pipeline, text, expression, turn_id=turn_id, directory=directory),
                            media_type='audio/wav', headers={'Cache-Control': 'no-store'})
        except TTSError as error:
            from cbs.telemetry import record_error
            record_error(pipeline.store, 'voice', error)
            raise HTTPException(503, str(error)) from None
        except (OSError, ValueError):
            raise HTTPException(422, '声の設定と参照WAVを確認してください。') from None

    def turn_audio(turn_id, pipeline):
        with pipeline.store.connect() as db:
            row = db.execute('SELECT t.* FROM turns t JOIN sessions s ON s.id=t.session_id '
                             'WHERE t.id=? AND s.character_id=? AND t.status=?',
                             (turn_id, pipeline.config.character.id, 'completed')).fetchone()
        if row is None or not row['assistant_text']:
            raise HTTPException(404, '返答がありません。')
        trace = pipeline.store.get_trace(turn_id)
        if any(trace.get(key, {}).get('state') == 'pending' for key in ('Expression', 'Expression-2')):
            raise HTTPException(409, '表情を確かめています。')
        return row['assistant_text'], row['expression'] or 'neutral'

    @app.post('/api/turns/{turn_id}/audio')
    def audio(turn_id: int, pipeline=Depends(selected)):
        text, expression = turn_audio(turn_id, pipeline)
        return synthesize(pipeline, text, expression, turn_id)

    @app.post('/api/turns/{turn_id}/audio/stream')
    async def stream_audio(turn_id: int, pipeline=Depends(selected)):
        text, expression = await asyncio.to_thread(turn_audio, turn_id, pipeline)
        if not pipeline.config.tts.enabled:
            raise HTTPException(503, '設定で音声をオンにしてください。')
        async def events():
            loop, queue, stopped = asyncio.get_running_loop(), asyncio.Queue(), Event()
            def emit(kind, data):
                loop.call_soon_threadsafe(queue.put_nowait, (kind, data))
            def on_audio(data):
                if stopped.is_set():
                    return
                emit('audio', {'wav': base64.b64encode(data).decode('ascii')})
            def work():
                try:
                    service.synthesize(pipeline, text, expression, turn_id=turn_id, on_audio=on_audio)
                except TTSError as error:
                    emit('error', {'message': str(error)})
                except Exception:
                    emit('error', {'message': '音声を作れませんでした。もう一度再生してください。'})
                finally:
                    emit('done', {})
            task = asyncio.create_task(asyncio.to_thread(work))
            app.state.bootstrap_tasks.add(task)
            task.add_done_callback(app.state.bootstrap_tasks.discard)
            try:
                while True:
                    kind, data = await queue.get()
                    yield 'event: ' + kind + '\ndata: ' + json.dumps(data) + '\n\n'
                    if kind == 'done':
                        break
            finally:
                stopped.set()
        return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
