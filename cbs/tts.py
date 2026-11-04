"""Local speech.cpp lifecycle and a replaceable OpenAI-compatible WAV transport."""
from cbs.llm.cost import for_step
import hashlib
import io
import json
from pathlib import Path
import re
import socket
import ssl
import subprocess
import threading
import time
from typing import Literal
import wave
import zipfile

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cbs.config import ROOT, VoiceStyle
from cbs.runtime import read_prompt

RELEASE = 'https://github.com/nyosegawa/speech.cpp/releases/download/v0.9.4'
ASSET = 'speech-0.9.4-windows-x64-vulkan.zip'
LEGACY_ASSET = 'speech-0.8.8-windows-x64-vulkan.zip'
CREATE_FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class TTSError(RuntimeError):
    """Only safe messages cross the HTTP boundary."""


class VoiceConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['design', 'clone'] = 'design'
    description: str = Field(default='', max_length=2000)
    seed: int = Field(default=0, ge=0, le=2147483647)
    reference: str = Field(default='', max_length=128)
    sample_reference: str = Field(default='', max_length=128)
    sample_description: str = Field(default='', max_length=64)
    external_voice: str = Field(default='alloy', max_length=128)
    steps: int | None = Field(default=None, ge=1, le=100)

    @field_validator('reference', 'sample_reference')
    @classmethod
    def valid_reference(cls, value):
        if value and (not value.lower().endswith(('.wav', '.gguf')) or value.startswith('.') or any(c in value for c in '/\\:\x00') or any(ord(c) < 32 for c in value)):
            raise ValueError('参照WAVのファイル名を確認してください。')
        return value


class VoiceDesign(BaseModel):
    model_config = ConfigDict(extra='forbid')
    description: str = Field(min_length=1, max_length=2000)


def read_voice(directory):
    path = Path(directory) / 'voice.yaml'
    if path.is_file():
        return VoiceConfig.model_validate(yaml.safe_load(path.read_text(encoding='utf-8')))
    seed = int.from_bytes(hashlib.sha256(Path(directory).name.encode()).digest()[:4], 'big') % 2147483648
    return VoiceConfig(seed=seed)


def write_voice(directory, voice):
    from cbs.web.settings_api import atomic_write
    atomic_write(Path(directory) / 'voice.yaml', yaml.safe_dump(voice.model_dump(), allow_unicode=True, sort_keys=False))


def design_voice(pipeline, directory, *, character=None, persist=True):
    character = character or pipeline.store.current_character(pipeline.character)
    context = {'summary': character.model.summary, 'world': character.world,
               'expression': character.expression.model_dump(mode='json', by_alias=True)}
    result = for_step(pipeline.client, 'voice_design', 'media').generate_json(pipeline.config.models.steward, read_prompt('voice_design'),
        [{'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}], VoiceDesign)
    description = pipeline.client.sanitize_text(VoiceDesign.model_validate(result.parsed).description).strip()
    voice = read_voice(directory).model_copy(update={'description': description})
    voice = VoiceConfig.model_validate(voice.model_dump())
    if not voice.description:
        raise ValueError('声の説明を生成できませんでした。')
    # Like other generation calls, keep a credential-sanitized trace alongside its artifact.
    from cbs.web.settings_api import atomic_write
    atomic_write(Path(directory) / 'voice_design_trace.json', pipeline.client.sanitize_text(json.dumps(result.trace(), ensure_ascii=False)))
    if persist:
        write_voice(directory, voice)
    return voice


def wav_info(data):
    try:
        with wave.open(io.BytesIO(data), 'rb') as stream:
            if stream.getcomptype() != 'NONE' or stream.getnchannels() not in (1, 2) or stream.getnframes() < 1:
                raise ValueError()
            if stream.getsampwidth() not in (1, 2, 3, 4) or not 8000 <= stream.getframerate() <= 192000:
                raise ValueError()
            if len(stream.readframes(stream.getnframes())) != stream.getnframes() * stream.getnchannels() * stream.getsampwidth():
                raise ValueError()
            return stream.getparams()
    except (wave.Error, EOFError, ValueError):
        raise ValueError('PCM形式のWAVを指定してください。') from None


def trim_silence(data, keep=0.15, floor_db=35):
    """Cut the long leading/trailing silence a reference-free sample carries (real recordings keep ~0.15 s)."""
    import io, wave, array, math
    try:
        with wave.open(io.BytesIO(data)) as w:
            params, frames = w.getparams(), w.readframes(w.getnframes())
        if params.sampwidth != 2:
            return data
        samples = array.array('h', frames)
        step = max(1, int(params.framerate * 0.02)) * params.nchannels
        levels = []
        for i in range(0, len(samples), step):
            block = samples[i:i + step]
            rms = math.sqrt(sum(v * v for v in block) / len(block)) / 32768 if block else 0
            levels.append(20 * math.log10(rms + 1e-9))
        if not levels:
            return data
        top = max(levels)
        voiced = [i for i, level in enumerate(levels) if level > top - floor_db]
        if not voiced:
            return data
        margin = int(params.framerate * keep) * params.nchannels
        start = max(0, voiced[0] * step - margin)
        end = min(len(samples), (voiced[-1] + 1) * step + margin)
        out = io.BytesIO()
        with wave.open(out, 'wb') as w:
            w.setparams(params)
            w.writeframes(samples[start:end].tobytes())
        return out.getvalue()
    except (wave.Error, EOFError, ValueError):
        return data


def merge_wavs(parts):
    output = io.BytesIO()
    with wave.open(output, 'wb') as target:
        expected = None
        for data in parts:
            params = wav_info(data)
            shape = (params.nchannels, params.sampwidth, params.framerate)
            if expected and expected != shape:
                raise TTSError('音声の形式が途中で変わりました。')
            if expected is None:
                target.setnchannels(params.nchannels)
                target.setsampwidth(params.sampwidth)
                target.setframerate(params.framerate)
            expected = shape
            with wave.open(io.BytesIO(data), 'rb') as source:
                target.writeframes(source.readframes(source.getnframes()))
    return output.getvalue()


def text_chunks(text, limit=220, min_chars=14):
    """Sentence chunks, with very short sentences joined to a neighbour (a lone 「こんにちは。」
    gives the model too little to go on and invites stray sounds). A short sentence joins the
    next one; a short last sentence joins the previous one. Formatting only."""
    chunks, pending = [], ''
    for chunk in _sentence_chunks(text, limit):
        joined = (pending + chunk) if pending else chunk
        if len(joined) < min_chars and len(joined.encode('utf-8')) < limit:
            pending = joined
            continue
        if pending and len(joined.encode('utf-8')) > limit:
            chunks.append(pending)
            pending = '' if len(chunk) >= min_chars else chunk
            if not pending:
                chunks.append(chunk)
            continue
        pending = ''
        chunks.append(joined)
    if pending:
        if chunks and len((chunks[-1] + pending).encode('utf-8')) <= limit:
            chunks[-1] += pending
        else:
            chunks.append(pending)
    yield from chunks


CLOSERS = '。、！？!?…‥」』）)】〕・ 　'


def _sentence_chunks(text, limit=220):
    """Split at sentence ends; a sentence over the byte limit is split at its last comma.
    Punctuation never starts a chunk (a request of only 「。」 makes the model hum), and a
    chunk without any letters is dropped. Formatting only."""
    def emit(piece):
        piece = piece.strip()
        if piece and any(c.isalnum() for c in piece):
            yield piece
    current = ''
    for char in text:
        if char in CLOSERS and not current.strip():
            continue  # stray punctuation left at a boundary belongs to the previous chunk
        if len((current + char).encode('utf-8')) > limit:
            cut = max(current.rfind('、'), current.rfind('，'), current.rfind(' '))
            if cut > 0:
                head, current = current[:cut + 1], current[cut + 1:]
                yield from emit(head)
            else:
                yield from emit(current)
                current = ''
            if char in CLOSERS and not current.strip():
                continue
        current += char
        if char in '。！？!?\n':
            yield from emit(current)
            current = ''
    yield from emit(current)


def speech_payload(text, expression, config, voice):
    style = config.styles.get(config.engine, VoiceStyle())
    value = style.values.get(expression, '')
    payload = {'input': text, 'voice': 'none' if config.engine == 'speech.cpp' else voice.external_voice,
               'response_format': 'wav'}
    instructions = voice.description if voice.mode == 'design' and not (config.engine == 'speech.cpp' and voice.sample_reference) else ''
    if style.mode == 'emoji' and value:
        # The prefix is confined to this fresh request dictionary.
        payload['input'] = value + ' ' + text
    elif style.mode == 'instructions' and value:
        instructions = '\n'.join(filter(None, (instructions, value)))
    if instructions:
        payload['instructions'] = instructions
    if config.engine == 'speech.cpp':
        payload['seed'] = voice.seed
        payload['duration_scale'] = config.duration_scale
        if voice.mode == 'clone' or voice.sample_reference:
            payload['voice'] = 'partner'
        if voice.steps:
            payload['steps'] = voice.steps
    elif config.model:
        payload['model'] = config.model
    return payload


class SpeechTransport:
    """Tests replace this one network boundary; never involve a real speech server."""
    def synthesize(self, base_url, payload):
        with httpx.Client(timeout=180, trust_env=False) as client:
            response = client.post(base_url.rstrip('/') + '/audio/speech', json=payload)
            response.raise_for_status()
            wav_info(response.content)
            return response.content


def release_directory(directory):
    """Keep the old installation usable (and its running Windows DLLs untouched)."""
    directory = Path(directory)
    try:
        names = {line.split()[-1].lstrip('*') for line in
                 (directory / 'SHA256SUMS').read_text().splitlines() if line.strip()}
    except OSError:
        names = set()
    return directory / 'v0.9.4' if LEGACY_ASSET in names and ASSET not in names else directory


def verified_archive(directory, *, allow_download=True, progress=None, cancel=None, tls_strict=False):
    """Verify the release archive before extracting or trusting installed binaries."""
    directory = Path(directory)
    current = release_directory(directory)
    asset = ASSET
    if not allow_download and current != directory and not (current / 'speech.zip').is_file():
        # Playback may continue on the verified 0.8.8 archive until explicit acquisition.
        asset = LEGACY_ASSET
    else:
        directory = current
    directory.mkdir(parents=True, exist_ok=True)
    sums = directory / 'SHA256SUMS'
    archive = directory / 'speech.zip'
    # Windows trust store like the LLM clients (security software may re-sign HTTPS);
    # with tls_strict off only the strict X.509 flag is relaxed, chain and hostname stay checked.
    context = ssl.create_default_context()
    if not tls_strict:
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, read=5), verify=context) as client:
        if not sums.is_file():
            if not allow_download:
                raise TTSError('設定の声から音声モデルを取得してください。')
            response = client.get(RELEASE + '/SHA256SUMS')
            response.raise_for_status()
            sums.write_bytes(response.content)
        entries = dict((name.lstrip('*'), digest) for digest, name in
                       (line.split(maxsplit=1) for line in sums.read_text().splitlines() if line.strip()))
        expected = entries.get(asset, '')
        if not expected and allow_download:
            response = client.get(RELEASE + '/SHA256SUMS')
            response.raise_for_status()
            sums.write_bytes(response.content)
            entries = dict((name.lstrip('*'), digest) for digest, name in
                           (line.split(maxsplit=1) for line in sums.read_text().splitlines() if line.strip()))
            expected = entries.get(asset, '')
            (directory / 'speech.zip.part').unlink(missing_ok=True)
        if not re.fullmatch('[0-9a-fA-F]{64}', expected):
            raise TTSError('SHA256SUMS に配布物がありません。')
        if archive.is_file() and allow_download:
            with archive.open('rb') as source:
                valid = hashlib.file_digest(source, 'sha256').hexdigest() == expected.lower()
            if not valid:
                archive.unlink()
        if not archive.is_file():
            if not allow_download:
                raise TTSError('設定の声から音声モデルを取得してください。')
            partial = directory / 'speech.zip.part'
            offset = partial.stat().st_size if partial.exists() else 0
            headers = {'Range': f'bytes={offset}-'} if offset else {}
            with client.stream('GET', RELEASE + '/' + ASSET, headers=headers) as response:
                # A fully downloaded partial may have survived a crash.
                complete = response.status_code == 416 and response.headers.get('Content-Range') == f'bytes */{offset}'
                if not complete:
                    response.raise_for_status()
                    append = offset > 0 and response.status_code == 206
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', '')) if append else None
                    if append and (not match or int(match[1]) != offset):
                        raise TTSError('配布物の再開位置が一致しません。')
                    done = offset if append else 0
                    total = int(match[3]) if append else int(response.headers.get('Content-Length', 0)) or None
                    with partial.open('ab' if append else 'wb') as output:
                        for chunk in response.iter_bytes():
                            if cancel and cancel.is_set():
                                raise InterruptedError()
                            output.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done, total)
                    if total is not None and done != total:
                        raise TTSError('配布物の取得が中断されました。再開してください。')
            with partial.open('rb') as source:
                valid = hashlib.file_digest(source, 'sha256').hexdigest() == expected.lower()
            if not valid:
                partial.unlink(missing_ok=True)
                raise TTSError('speech.cpp のチェックサムが一致しません。')
            partial.replace(archive)
    with archive.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != expected.lower():
            raise TTSError('speech.cpp のチェックサムが一致しません。配布物を再取得してください。')
    destination = directory / 'bin'
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            target = (destination / entry.filename).resolve()
            if not target.is_relative_to(destination.resolve()) or entry.is_dir():
                if entry.is_dir():
                    continue
                raise TTSError('配布物のパスが不正です。')
            data = package.read(entry)
            if not target.is_file() or target.read_bytes() != data:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
    exe = destination / 'speech.exe'
    if not exe.is_file():
        raise TTSError('speech.exe が配布物にありません。')
    return exe


class SpeechServer:
    def __init__(self, tools_dir):
        self.tools_dir = Path(tools_dir)
        self.process = None
        self.signature = None
        self.base_url = None

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=10)
            self.process = None
        self.signature = None
        self.base_url = None

    def start(self, config, voice, directory):
        from cbs.tts_download import installed_model, select_model
        reference = None
        name = voice.reference if voice.mode == 'clone' else voice.sample_reference
        if name:
            reference = Path(directory) / 'voice' / name
            if reference.suffix == '.wav':
                wav_info(reference.read_bytes())
            elif not reference.is_file():
                raise TTSError('声ファイルがありません。声を決め直してください。')
        elif voice.mode == 'clone':
            raise TTSError('参照WAVを選んでください。')
        signature = (config.device, config.local_model, str(reference), reference.stat().st_mtime_ns if reference else None)
        if self.process is not None and self.process.poll() is None and self.signature == signature:
            return self.base_url
        self.stop()
        exe = verified_archive(self.tools_dir, allow_download=False)
        device, quant, _ = select_model(exe, config.local_model, config.device)
        # Use the catalog's existing file path: serve/voice must never fetch implicitly.
        model = installed_model(exe, quant)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        command = [str(exe), 'serve', model, '--host', '127.0.0.1', '--port', str(port), '--device', device]
        if device == 'cpu':
            command += ['--steps', '16']
        if reference:
            command += ['--add-voice', 'partner=' + str(reference.resolve())]
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_FLAGS)
        self.base_url = f'http://127.0.0.1:{port}/v1'
        deadline = time.monotonic() + 180
        try:
            with httpx.Client(timeout=2, trust_env=False) as client:
                while time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        break
                    try:
                        if client.get(f'http://127.0.0.1:{port}/health').is_success:
                            self.signature = signature
                            return self.base_url
                    except httpx.HTTPError:
                        pass
                    time.sleep(.2)
            raise TTSError('音声サーバーを起動できませんでした。次の再生で再試行します。')
        except Exception:
            self.stop()
            raise

    def combine(self, config, directory, names):
        from uuid import uuid4
        from cbs.tts_download import installed_model, select_model
        exe = verified_archive(self.tools_dir, allow_download=False)
        _, quant, _ = select_model(exe, config.local_model, config.device)
        model = installed_model(exe, quant)
        destination = Path(directory) / 'voice'
        name = 'clone-' + uuid4().hex + '.gguf'
        target = destination / name
        try:
            subprocess.run([str(exe), 'voice', model, *(str(destination / n) for n in names), str(target)],
                           capture_output=True, timeout=180, check=True, creationflags=CREATE_FLAGS)
            if not target.is_file() or target.stat().st_size == 0:
                raise TTSError('声ファイルを作れませんでした。')
        except Exception:
            target.unlink(missing_ok=True)
            raise TTSError('参照音声をまとめられませんでした。取得の状態とWAVを確認してください。') from None
        return name


class TTSService:
    def __init__(self, data_dir, characters_root, *, transport=None, server=None):
        self.audio_dir = Path(data_dir) / 'audio'
        self.characters_root = Path(characters_root)
        self.server = server or SpeechServer(Path(data_dir) / 'tools' / 'speech')
        self.transport = transport or SpeechTransport()
        self.lock = threading.RLock()
        self.start_error = None
        self.closed = False

    def directory(self, pipeline):
        from cbs.look import character_path
        return character_path(self.characters_root, pipeline.config.character.id)

    def startup(self, pipeline):
        with self.lock:
            if self.closed:
                return
            try:
                self.prune(pipeline.config.tts.cache_days)
                if pipeline.config.tts.enabled and pipeline.config.tts.engine == 'speech.cpp':
                    directory = self.directory(pipeline)
                    self.server.start(pipeline.config.tts, read_voice(directory), directory)
            except Exception:
                self.start_error = '音声の準備に失敗しました。次の再生で再試行します。'

    def stop(self):
        with self.lock:
            self.closed = True
            self.server.stop()

    def reconfigure(self):
        with self.lock:
            self.server.stop()
            self.start_error = None

    def prune(self, days):
        with self.lock:
            cutoff = time.time() - days * 86400
            for path in (*self.audio_dir.glob('*.wav'), *self.audio_dir.glob('*.wav.tmp')):
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)

    def design_sample(self, pipeline, directory, *, regenerate=False):
        """Generate once without a reference, then pin every sentence to that WAV."""
        from uuid import uuid4
        directory = Path(directory)
        voice = read_voice(directory)
        fingerprint = hashlib.sha256(voice.description.encode()).hexdigest()
        if not regenerate and voice.sample_reference and voice.sample_description == fingerprint and (directory / 'voice' / voice.sample_reference).is_file():
            return voice
        config = pipeline.config.tts
        raw = voice.model_copy(update={'mode': 'design', 'sample_reference': ''})
        base_url = self.server.start(config, raw, directory)
        data = self.transport.synthesize(base_url, speech_payload(read_prompt('voice_sample').strip(), 'neutral', config, raw))
        wav_info(data)
        data = trim_silence(data)
        destination = directory / 'voice'
        destination.mkdir(parents=True, exist_ok=True)
        name = 'sample-' + uuid4().hex + '.wav'
        (destination / name).write_bytes(data)
        voice.sample_reference, voice.sample_description = name, fingerprint
        write_voice(directory, voice)
        return voice

    def combine_references(self, config, directory, names):
        # This boundary is replaceable in tests, just like server/transport.
        return self.server.combine(config, directory, names)

    def synthesize(self, pipeline, text, expression='neutral', *, turn_id=None, directory=None, on_audio=None):
        with self.lock:
            config = pipeline.config.tts.model_copy(deep=True)
            if self.closed or not config.enabled:
                raise TTSError('設定で音声をオンにしてください。')
            directory = Path(directory) if directory is not None else self.directory(pipeline)
            voice = read_voice(directory)
            self.prune(config.cache_days)
            # Per-turn cache intentionally survives edits to voice settings until expiry.
            path = self.audio_dir / f'{pipeline.config.character.id}-{turn_id}.wav' if turn_id is not None else None
            if path and path.is_file():
                data = path.read_bytes()
                if on_audio:
                    on_audio(data)
                return data
            try:
                if config.engine == 'speech.cpp' and voice.mode == 'design':
                    voice = self.design_sample(pipeline, directory)
                base_url = self.server.start(config, voice, directory) if config.engine == 'speech.cpp' else config.base_url
                parts = []
                for chunk in text_chunks(text):
                    part = self.transport.synthesize(base_url, speech_payload(chunk, expression, config, voice))
                    wav_info(part)
                    parts.append(part)
                    if on_audio:
                        on_audio(part)
                if not parts:
                    raise TTSError('読み上げる文がありません。')
                data = merge_wavs(parts)
                self.start_error = None
            except Exception as error:
                from cbs.telemetry import record_error
                record_error(pipeline.store, 'voice', error)
                if config.engine == 'speech.cpp':
                    self.server.stop()
                raise TTSError('音声を作れませんでした。設定を確認して、もう一度再生してください。') from None
            if path:
                self.audio_dir.mkdir(parents=True, exist_ok=True)
                partial = path.with_suffix('.wav.tmp')
                partial.write_bytes(data)
                partial.replace(path)
            return data
