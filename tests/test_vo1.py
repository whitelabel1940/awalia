import base64
import hashlib
import io
import json
import threading
import wave
import zipfile

from fastapi.testclient import TestClient
import httpx
import pytest

from cbs.config import STTConfig, load_config
from cbs.llm import FakeLLMClient, RoutingLLMClient
from cbs.stt import HaasServer, decode_audio, stt_status, transcribe
from cbs.stt_download import HaasInstaller
from cbs.web import create_app
HTTPClient = httpx.Client


def audio(seconds=.1, rate=16000, channels=1):
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b'\0\0' * int(seconds * rate) * channels)
    return base64.b64encode(out.getvalue()).decode()


def test_presets_capability_keys_and_unknown_names(tmp_path, monkeypatch):
    cfg = load_config(data_dir=tmp_path)
    assert cfg.models.stt == 'openrouter/google/gemini-3.8-flash'
    assert not stt_status(cfg)['enabled']
    monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic')
    assert stt_status(cfg)['enabled']
    assert {p.id: p.models.stt.split('/')[0] for p in cfg.advanced.presets} == {
        'mixed': 'openrouter', 'gemini': 'gemini', 'claude': 'local', 'openai': 'openai', 'recommended_direct': 'gemini'}
    cfg.models.stt = 'openrouter/unknown-audio-name'
    assert not stt_status(cfg)['enabled']
    cfg.stt.candidates[cfg.models.stt] = True
    assert stt_status(cfg)['enabled']
    cfg.models.stt = 'anthropic/arbitrary'
    cfg.stt.candidates[cfg.models.stt] = True
    assert not stt_status(cfg)['enabled']
    cfg.models.stt = None
    assert '設定' in stt_status(cfg)['reason']


@pytest.mark.parametrize('bad', ['bad', audio(1.01), audio(rate=8000), audio(channels=2), audio(0)], ids=['invalid', 'duration', 'rate', 'channels', 'empty'])
def test_reject_length_format_and_capacity(bad):
    with pytest.raises(ValueError):
        decode_audio(bad, STTConfig(max_seconds=1))


def test_rebuild_wav_and_truncation():
    raw = base64.b64decode(audio(1))
    assert decode_audio(audio(1), STTConfig(max_seconds=1)) == raw
    with pytest.raises(ValueError):
        decode_audio(base64.b64encode(raw[:-5]).decode(), STTConfig())


@pytest.mark.parametrize('provider', ['openrouter', 'openai', 'gemini'])
def test_audio_routing_only_selected_role_without_constructing_provider(tmp_path, provider):
    client = RoutingLLMClient(load_config(data_dir=tmp_path))
    fake = FakeLLMClient(stt_responses=['聞こえた文。'])
    client._clients[provider] = fake
    assert client.transcribe_audio(provider + '/configured-model', 'prompt', b'fake-wav') == '聞こえた文。'
    assert fake.calls == [{'model': 'configured-model', 'role': 'stt', 'system': 'prompt', 'audio_bytes': 8}]


@pytest.mark.parametrize('auto_send', [False, True])
def test_transcription_is_not_a_turn_and_leaves_no_audio(pipeline, tmp_path, auto_send):
    app = create_app(pipeline, settings_dir=tmp_path / 'settings')
    pipeline.config.stt.auto_send = auto_send
    client = TestClient(app)
    from cbs.steward import wait_for_steward
    wait_for_steward(pipeline.store)
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob('*') if p.is_file()}
    response = client.post('/api/stt/transcribe', json={'audio': audio()})
    assert response.status_code == 200
    assert response.json() == {'text': '今日はいい天気ですね。', 'auto_send': auto_send}
    assert pipeline.client.calls[-1]['role'] == 'stt'
    assert 'audio' not in pipeline.client.calls[-1]
    # CO1 stores numeric usage asynchronously in the existing DB. Its transient
    # SQLite journal is not an audio file; compare after that transaction ends.
    wait_for_steward(pipeline.store)
    assert before == {p.relative_to(tmp_path) for p in tmp_path.rglob('*') if p.is_file()}
    with pipeline.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM turns').fetchone()[0] == 0
    assert client.post('/api/stt/notice').json() == {'seen': True}
    assert (tmp_path / 'settings' / 'stt-notice-seen').stat().st_size == 0
    other = create_app(pipeline, settings_dir=tmp_path / 'settings')
    assert other.state.stt_notice


def test_bounded_api_no_call_on_invalid_or_disabled(pipeline, tmp_path):
    client = TestClient(create_app(pipeline, settings_dir=tmp_path))
    pipeline.config.stt.max_seconds = 1
    for value in [audio(1.01), 'bad', 12]:
        assert client.post('/api/stt/transcribe', json={'audio': value}).status_code == 422
    assert client.post('/api/stt/transcribe', content=b'x' * 50000).status_code == 413
    assert client.post('/api/stt/transcribe', content='{').status_code == 422
    assert client.post('/api/stt/transcribe', json=[]).status_code == 422
    pipeline.config.models.stt = None
    assert client.post('/api/stt/transcribe', json={'audio': audio()}).status_code == 422
    assert not pipeline.client.calls


def test_settings_stt_and_local_missing_reason(pipeline, tmp_path):
    client = TestClient(create_app(pipeline, settings_dir=tmp_path))
    response = client.put('/api/settings', json={'llm': {'mode': 'advanced'}, 'models': {'stt': 'local/haas-ja'}, 'stt': {'auto_send': True, 'silence_seconds': 1., 'max_seconds': 15, 'device': 'cpu'}})
    assert response.status_code == 200
    status = response.json()['stt_status']
    assert not status['enabled'] and '取得' in status['reason']
    assert pipeline.config.models.stt == 'local/haas-ja'
    assert client.put('/api/settings', json={'stt': {'max_seconds': 121}}).status_code == 422
    assert client.put('/api/settings', json={'stt': {'candidates': {'arbitrary': True}}}).status_code == 422


def test_device_quote_is_separate_from_runtime_and_invalidated_on_save(pipeline, tmp_path, monkeypatch):
    monkeypatch.setattr(HaasInstaller, 'auto_device', lambda self: 'gpu')
    app = create_app(pipeline, settings_dir=tmp_path)
    client = TestClient(app)
    quote = client.post('/api/stt/download/plan', json={'choice': 'haas-ja', 'device': 'gpu'}).json()
    assert quote['engine'] == 'gpu'
    assert app.state.stt_installer.engine_name() == 'auto'
    assert pipeline.config.stt.device == 'auto'
    assert client.get('/api/stt/download?choice=haas-ja&device=cpu').json()['engine'] == 'cpu'
    assert client.get('/api/stt/download?choice=haas-ja&device=auto').json()['engine'] == 'gpu'
    assert client.put('/api/settings', json={'stt': {'device': 'cpu'}}).status_code == 200
    assert client.post('/api/stt/download', json={'choice': 'haas-ja', 'token': quote['token'], 'confirmed': True}).status_code == 409
    assert app.state.stt_installer.device == app.state.stt_download.installer.device == 'cpu'
    blocked = threading.Event()
    app.state.stt_download.thread = threading.Thread(target=lambda: blocked.wait(5))
    app.state.stt_download.thread.start()
    try:
        assert client.put('/api/settings', json={'stt': {'device': 'gpu'}}).status_code == 422
        assert pipeline.config.stt.device == 'cpu'
    finally:
        blocked.set()
        app.state.stt_download.thread.join()


def test_stt_changes_do_not_repeat_foreground_connection_checks(pipeline, tmp_path, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic')
    pipeline.config.llm.mode = 'advanced'
    app = create_app(pipeline, settings_dir=tmp_path)
    client = TestClient(app)
    assert client.post('/api/settings/connection').json()['cached'] is False
    pipeline.client.calls.clear()
    pipeline.client.embedding_calls.clear()
    assert client.put('/api/settings', json={'models': {'stt': 'local/haas-ja'}, 'stt': {'auto_send': True}}).status_code == 200
    assert app.state.connection_verified()
    assert client.post('/api/settings/connection').json()['cached'] is True
    assert not pipeline.client.calls and not pipeline.client.embedding_calls


@pytest.fixture
def speech_files(tmp_path, monkeypatch):
    """Only archive bytes, catalog responses and owned fake children, never a real CLI."""
    from types import SimpleNamespace
    from cbs.tts import ASSET
    from cbs.tts_download import ARCHIVE_BYTES
    root = tmp_path / 'tools' / 'speech'
    model = tmp_path / 'haas.gguf'
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        z.writestr('speech.exe', b'fake executable; never run')
    archive = output.getvalue()
    sums = hashlib.sha256(archive).hexdigest() + '  ' + ASSET
    requests, commands = [], []
    def http(req):
        requests.append(req)
        if req.url.path.endswith('SHA256SUMS'):
            return httpx.Response(200, text=sums)
        assert req.url.path.endswith(ASSET)
        return httpx.Response(200, content=archive)
    monkeypatch.setattr('cbs.tts.httpx.Client', lambda **kwargs: HTTPClient(transport=httpx.MockTransport(http)))
    def run(args, **kwargs):
        assert args[1:] in (['models', '--json'], ['devices'])
        return SimpleNamespace(stdout=json.dumps({'models': [{'name': 'haas-ja-v1.0', 'files': [
            {'type': 'q8_0', 'size': 100, 'partial': 40, 'fetched': model.is_file(), 'path': str(model)}]}]}).encode()
            if args[1] == 'models' else b'Vulkan0 gpu Fake\nCPU cpu Fake')
    monkeypatch.setattr('cbs.tts_download.subprocess.run', run)
    class Process:
        code = None
        def poll(self): return self.code
        def terminate(self): self.code = 0
        def wait(self, timeout): return self.code
    def spawn(args, **kwargs):
        commands.append(args)
        assert args[1:] == ['pull', 'haas-ja-v1.0']
        model.write_bytes(b'fake model')
        process = Process()
        process.code = 0
        return process
    monkeypatch.setattr('cbs.tts_download.subprocess.Popen', spawn)
    return SimpleNamespace(root=root, model=model, archive=archive, sums=sums,
                           requests=requests, commands=commands, Process=Process, allowance=ARCHIVE_BYTES)


def test_installer_plan_pull_and_existing_download_ui(pipeline, tmp_path, speech_files):
    from cbs.tts import verified_archive
    files = speech_files
    app = create_app(pipeline, settings_dir=tmp_path)
    client = TestClient(app)
    installer = app.state.stt_installer
    assert installer.directory == files.root == app.state.voice_download.installer.directory
    plan = installer.plan('haas-ja')
    assert not plan['ready'] and plan['engine_bytes'] == files.allowance
    assert plan['total'] == files.allowance + 660000000
    assert 'speech.cpp' in plan['reason'] and not files.requests
    verified_archive(files.root)  # Mock HTTP only.
    plan = installer.plan('haas-ja')
    assert not plan['ready'] and plan['engine_bytes'] == 0 and plan['model_completed'] == 40
    quote = client.post('/api/stt/download/plan', json={'choice': 'haas-ja'}).json()
    assert client.post('/api/stt/download', json={'choice': 'haas-ja', 'token': quote['token']}).status_code == 409
    assert client.post('/api/stt/download', json={'choice': 'haas-ja', 'token': 'wrong', 'confirmed': True}).status_code == 409
    assert client.post('/api/stt/download', json={'choice': 'haas-ja', 'token': quote['token'], 'confirmed': True}).status_code == 200
    app.state.stt_download.thread.join(5)
    assert client.get('/api/stt/download?choice=haas-ja').json()['status'] == 'ready'
    assert installer.plan('haas-ja')['ready']
    assert files.commands[0][1:] == ['pull', 'haas-ja-v1.0']
    assert installer.verify('haas-ja')[1:] == (str(files.model), 'gpu')
    assert client.post('/api/stt/download/cancel').json()['cancelled']
    assert not app.state.stt_server.process  # First transcription starts the recognizer.
    files.model.unlink()
    assert not installer.plan('haas-ja')['ready']


def test_pull_cancel_and_failure_terminate_owned_child(tmp_path, monkeypatch, speech_files):
    from cbs.tts_download import pull_model
    from cbs.tts import TTSError
    process = speech_files.Process()
    monkeypatch.setattr('cbs.tts_download.subprocess.Popen', lambda *a, **k: process)
    cancel = threading.Event()
    with pytest.raises(InterruptedError):
        pull_model('fake', 'haas-ja-v1.0', 'q8_0', cancel, lambda *a: cancel.set())
    assert process.code == 0
    process.code = 1
    with pytest.raises(TTSError):
        pull_model('fake', 'haas-ja-v1.0', 'q8_0', threading.Event(), lambda *a: None)


@pytest.mark.parametrize('device,actual', [('cpu', 'cpu'), ('gpu', 'gpu'), ('auto', 'gpu')])
def test_owned_local_server_loopback_restart_shutdown_and_no_audio_files(tmp_path, monkeypatch, speech_files, device, actual):
    cfg = load_config(data_dir=tmp_path)
    cfg.stt.device = device
    installer = HaasInstaller(speech_files.root, cfg)
    installer.acquire('haas-ja', threading.Event(), lambda *a: None)
    requests, processes = [], []
    def handler(req):
        requests.append(req)
        assert req.url.host == '127.0.0.1'
        if req.method == 'POST':
            assert req.url.path == '/v1/audio/transcriptions'
            body = req.content
            assert b'name="file"' in body and b'RIFF' in body
            assert b'name="response_format"\r\n\r\njson' in body
            assert 'name="words"\r\n\r\n陽菜'.encode() in body
            assert b'name="model"' not in body and b'words_alpha' not in body
        return httpx.Response(200, json={'status': 'ok'} if req.url.path == '/health' else {'text': '声の文。'})
    monkeypatch.setattr('cbs.stt.httpx.Client', lambda **kwargs: HTTPClient(transport=httpx.MockTransport(handler)))
    # Avoid archive HTTP sharing the mocked health client; binary verification has its own tests.
    monkeypatch.setattr(installer, 'verify', lambda choice: (speech_files.root / 'bin' / 'speech.exe', str(speech_files.model), actual))
    monkeypatch.setattr(installer, 'plan', lambda choice: {'ready': True})
    def spawn(args, **kwargs):
        assert args[1:3] == ['serve', str(speech_files.model)]
        assert args[args.index('--host') + 1] == '127.0.0.1'
        assert args[args.index('--device') + 1] == actual
        import subprocess
        assert kwargs['stdout'] == kwargs['stderr'] == subprocess.DEVNULL
        process = speech_files.Process()
        processes.append(process)
        return process
    monkeypatch.setattr('cbs.stt.subprocess.Popen', spawn)
    before = {p for p in tmp_path.rglob('*') if p.is_file()}
    server = HaasServer()
    assert server.transcribe(installer, 'haas-ja', base64.b64decode(audio()), '陽菜') == '声の文。'
    server.start(installer, 'haas-ja')
    assert len(processes) == 1
    processes[-1].code = 1
    server.start(installer, 'haas-ja')
    assert len(processes) == 2
    server.close()
    assert processes[-1].code == 0
    assert before == {p for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(Exception, match='終了'):
        server.start(installer, 'haas-ja')


def test_local_pipeline_passes_current_partner_name_and_cost_only(pipeline, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from cbs.llm import RoutingLLMClient
    calls = []
    pipeline.config.models.stt = 'local/haas-ja'
    pipeline.client = RoutingLLMClient(pipeline.config)
    server = SimpleNamespace(transcribe=lambda *args: calls.append(args) or '文字。', running=lambda *args: False)
    installer = SimpleNamespace(plan=lambda choice: {'ready': True})
    assert transcribe(pipeline, audio(), installer, server)['text'] == '文字。'
    assert calls[-1][-1] == pipeline.character.model.display_name
    pipeline.character.model.display_name = 'ハナ'
    transcribe(pipeline, audio(), installer, server)
    assert calls[-1][-1] == 'ハナ'
    # A running recognizer skips the acquisition plan (archive hash + catalog) on each request.
    server.running = lambda *args: True
    installer.plan = lambda choice: pytest.fail('plan on a running recognizer')
    assert transcribe(pipeline, audio(), installer, server)['text'] == '文字。'


@pytest.mark.parametrize('simple', [False, True])
def test_old_kotoba_settings_migrate_on_load(tmp_path, simple):
    (tmp_path / 'settings.toml').write_text(
        '[llm]\nmode = "' + ('simple' if simple else 'advanced') + '"\n'
        '[models]\nstt = "local/kotoba-whisper"\n'
        '[llm.advanced_models]\nstt = "local/kotoba-whisper"\n', encoding='utf-8')
    cfg = load_config(data_dir=tmp_path)
    assert cfg.models.stt == ('openrouter/google/gemini-3.8-flash' if simple else 'local/haas-ja')
    assert cfg.llm.advanced_models['stt'] == 'local/haas-ja'
    assert set(cfg.stt.local_models) == {'haas-ja'}
