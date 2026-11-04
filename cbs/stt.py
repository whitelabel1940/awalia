"""Transient audio input. No audio, provider requests or transcription traces on disk."""
import base64
import binascii
import io
import socket
import subprocess
import threading
import time
import wave

import httpx

from cbs.config import key_env, key_set
from cbs.llm import FakeLLMClient, LLMError, RoutingLLMClient
from cbs.runtime import read_prompt
from cbs.tts import CREATE_FLAGS


def stt_status(config, *, installer=None, require_key=True):
    model = config.models.stt
    if not model:
        return {'enabled': False, 'reason': '文字起こしのモデルを設定してください。'}
    provider, bare = RoutingLLMClient(config)._split(model)
    full = provider + '/' + bare
    if provider == 'local':
        if bare not in config.stt.local_models:
            return {'enabled': False, 'reason': 'ローカルの文字起こしモデルを選んでください。'}
        if installer:
            plan = installer.plan(bare)
            if not plan['ready']:
                return {'enabled': False, 'reason': '設定から取得してください。' + plan['reason']}
    elif provider not in {'openai', 'openrouter', 'gemini'} or not config.stt.candidates.get(full, False):
        return {'enabled': False, 'reason': 'このモデルの音声入力対応は未確認です。文字起こしの設定を選び直してください。'}
    elif require_key and key_env(config, provider) and not key_set(config, provider):
        return {'enabled': False, 'reason': '文字起こしのサービスのキーを設定してください。'}
    return {'enabled': True, 'reason': '', 'max_seconds': config.stt.max_seconds,
            'silence_seconds': config.stt.silence_seconds, 'silence_rms': config.stt.silence_rms,
            'auto_send': config.stt.auto_send, 'local': provider == 'local'}


def decode_audio(encoded, config):
    maximum = config.max_seconds * 16000 * 2 + 44
    if len(encoded) > 4 * ((maximum + 2) // 3):
        raise ValueError('録音の容量が上限を超えています。')
    try:
        raw = base64.b64decode(encoded, validate=True)
        with wave.open(io.BytesIO(raw), 'rb') as w:
            frames = w.getnframes()
            if (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getcomptype()) != (1, 2, 16000, 'NONE'):
                raise ValueError()
            if not 0 < frames <= config.max_seconds * 16000 or len(raw) > maximum:
                raise ValueError()
            pcm = w.readframes(frames)
            if len(pcm) != frames * 2:
                raise ValueError()
        # Rebuild the header: client metadata and arbitrary RIFF chunks never go upstream.
        output = io.BytesIO()
        with wave.open(output, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        return output.getvalue()
    except (ValueError, binascii.Error, wave.Error, EOFError):
        raise ValueError('録音を読み取れませんでした。長さの上限内でもう一度話してください。') from None


class HaasServer:
    def __init__(self):
        self.lock = threading.RLock()
        self.process = None
        self.identity = None
        self.url = None
        self.closed = threading.Event()

    def _stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.process = None
        self.identity = None

    def stop(self):
        with self.lock:
            self._stop()

    def close(self):
        self.closed.set()
        self.stop()

    def running(self, installer, choice):
        with self.lock:
            return (self.identity == (str(installer.directory), installer.engine_name(), choice)
                    and self.process is not None and self.process.poll() is None)

    def start(self, installer, choice):
        with self.lock:
            if self.closed.is_set():
                raise LLMError('文字起こしを終了しています。')
            identity = (str(installer.directory), installer.engine_name(), choice)
            if self.identity == identity and self.process and self.process.poll() is None:
                return
            self._stop()
            if not installer.plan(choice)['ready']:
                raise LLMError('設定からローカルの文字起こしモデルを取得してください。')
            try:
                exe, model, device = installer.verify(choice)
            except Exception:
                raise LLMError('ローカルの文字起こしのファイルを確認できません。設定から再取得してください。') from None
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            self.url = f'http://127.0.0.1:{port}'
            # Use the fetched catalog path so serve never downloads implicitly.
            args = [str(exe.resolve()), 'serve', str(model), '--host', '127.0.0.1',
                    '--port', str(port), '--device', device]
            self.process = subprocess.Popen(args, cwd=exe.parent, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, creationflags=CREATE_FLAGS)
            self.identity = identity
            try:
                with httpx.Client(timeout=1, trust_env=False) as client:
                    for _ in range(240):
                        if self.closed.is_set() or self.process.poll() is not None:
                            break
                        try:
                            if client.get(self.url + '/health').is_success:
                                return
                        except httpx.HTTPError:
                            pass
                        time.sleep(.25)
                raise LLMError('ローカルの文字起こしを起動できませんでした。設定とGPUドライバーを確認してください。')
            except Exception:
                self._stop()
                raise

    def transcribe(self, installer, choice, raw, partner_name):
        with self.lock:
            self.start(installer, choice)
            try:
                # Loopback HTTP: skip loading a CA bundle on every request.
                with httpx.Client(timeout=120, trust_env=False, verify=False) as client:
                    response = client.post(self.url + '/v1/audio/transcriptions',
                                           files={'file': ('voice.wav', raw, 'audio/wav')},
                                           data={'response_format': 'json', 'words': partner_name})
                    response.raise_for_status()
                    return response.json()['text']
            except Exception:
                raise LLMError('ローカルの文字起こしに失敗しました。もう一度話してください。') from None


def transcribe(pipeline, encoded, installer, server):
    cfg = pipeline.config
    provider, choice = RoutingLLMClient(cfg)._split(cfg.models.stt)
    # A running server already passed the acquisition check; re-planning hashes the engine archive.
    checked = provider == 'local' and server.running(installer, choice)
    status = stt_status(cfg, installer=None if checked else installer, require_key=not isinstance(pipeline.client, FakeLLMClient))
    if not status['enabled']:
        raise ValueError(status['reason'])
    raw = decode_audio(encoded, cfg.stt)
    # Fake is also the only inference boundary used in local lifecycle/API tests.
    if provider == 'local' and not isinstance(pipeline.client, FakeLLMClient):
        try:
            text = server.transcribe(installer, choice, raw, pipeline.character.model.display_name)
        finally:
            pipeline.store.costs.record('local', pipeline.client.sanitize_text(cfg.models.stt), 'stt', 'media', {})
    else:
        from cbs.llm.cost import for_step
        text = for_step(pipeline.client, 'stt', 'media').transcribe_audio(cfg.models.stt, read_prompt('stt'), raw)
    text = pipeline.client.sanitize_text(text).strip()
    if len(text) > 20000:
        raise ValueError('文字起こしが長すぎます。短く分けて話してください。')
    return {'text': text, 'auto_send': cfg.stt.auto_send}
