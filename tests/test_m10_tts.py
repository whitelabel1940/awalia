"""M10: all speech/HTTP/process boundaries are faked; no provider/network calls."""
import hashlib
import io
import json
import time
import wave
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cbs.config import TTSConfig, VoiceStyle
from cbs.expression import EXPRESSIONS
from cbs.tts import (ASSET, SpeechServer, TTSService, TTSError, VoiceConfig, design_voice,
                     read_voice, speech_payload, text_chunks, verified_archive, wav_info, write_voice)
from test_web_bootstrap import app_for


def wav():
    output = io.BytesIO()
    with wave.open(output, 'wb') as target:
        target.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        target.writeframes(b'\0\0' * 480)
    return output.getvalue()


class FakeSpeech:
    def __init__(self):
        self.calls = []
        self.fail = False

    def synthesize(self, url, payload):
        self.calls.append((url, payload))
        if self.fail:
            raise RuntimeError('private engine error')
        return wav()


class FakeServer:
    def __init__(self):
        self.starts = []
        self.stops = 0

    def start(self, config, voice, directory):
        self.starts.append((config, voice, directory))
        return 'http://127.0.0.1:49152/v1'

    def combine(self, config, directory, names):
        name = 'fake-clone.gguf'
        (directory / 'voice' / name).write_bytes(b'fake voice')
        return name

    def stop(self):
        self.stops += 1


@pytest.mark.parametrize('expression', EXPRESSIONS)
def test_style_is_only_in_request(expression):
    text = '今日は一緒に話せてうれしい。'
    cfg = TTSConfig()
    payload = speech_payload(text, expression, cfg, VoiceConfig(description='柔らかな声', seed=123))
    prefix = cfg.styles['speech.cpp'].values[expression]
    assert payload['input'] == (prefix + ' ' if prefix else '') + text
    assert text == '今日は一緒に話せてうれしい。'
    assert payload['seed'] == 123 and payload['voice'] == 'none'
    assert payload['instructions'] == '柔らかな声'
    cfg.engine = 'external'
    assert speech_payload(text, expression, cfg, VoiceConfig())['input'] == text
    cfg.styles['external'] = VoiceStyle(mode='instructions', values={expression: '静かに'})
    result = speech_payload(text, expression, cfg, VoiceConfig(description='柔らかな声'))
    assert result['input'] == text and result['instructions'] == '柔らかな声\n静かに'
    assert 'seed' not in result and 'model' not in result


def test_long_text_chunking_keeps_all_content_and_budget():
    text = 'あ' * 1000 + '。' + 'z' * 350 + '！'
    chunks = list(text_chunks(text))
    assert ''.join(chunks) == text
    assert all(len(('😳 ' + chunk).encode('utf-8')) < 256 for chunk in chunks)


def test_config_defaults_and_external_validation():
    assert not TTSConfig().enabled and TTSConfig().autoplay and not TTSConfig().autoplay_proactive
    assert TTSConfig().cache_days == 7
    for body in [{'base_url': 'http://name:secret@localhost/v1'}, {'base_url': 'file:///tmp'},
                 {'styles': {'external': {'mode': 'emoji'}}}, {'styles': {'speech.cpp': {'values': {'bad': 'x'}}}}]:
        with pytest.raises(ValueError):
            TTSConfig.model_validate(body)


def test_voice_design_uses_prompt_and_keeps_seed(pipeline, tmp_path):
    directory = tmp_path / 'fixture'
    directory.mkdir()
    seed = read_voice(directory).seed
    pipeline.client.voice_design_responses = [{'description': '落ち着いた柔らかな声。'}]
    generated = design_voice(pipeline, directory)
    assert generated.description == '落ち着いた柔らかな声。'
    assert read_voice(directory).seed == seed
    assert pipeline.client.calls[-1]['system'].startswith('人物の設定')
    assert json.loads((directory / 'voice_design_trace.json').read_text(encoding='utf-8'))['model']


def test_audio_cache_expiry_failure_and_character_scope(pipeline, tmp_path):
    root = tmp_path / 'characters'
    (root / 'fixture').mkdir(parents=True)
    pipeline.config.tts.enabled = True
    transport, server = FakeSpeech(), FakeServer()
    service = TTSService(tmp_path, root, transport=transport, server=server)
    first = service.synthesize(pipeline, 'こんにちは、今日も来てくれたんだね。今日はとてもいい日になりそうだね。', 'smile', turn_id=5)
    assert len(transport.calls) == 3 and wav_info(first).nframes == 960
    assert service.synthesize(pipeline, 'こんにちは。', 'sad', turn_id=5) == first
    assert len(transport.calls) == 3
    import os
    path = tmp_path / 'audio' / 'fixture-5.wav'
    os.utime(path, (time.time() - 8 * 86400,) * 2)
    service.prune(7)
    assert not path.exists()
    transport.fail = True
    with pytest.raises(TTSError, match='音声を作れませんでした'):
        service.synthesize(pipeline, 'こんにちは。', turn_id=6)
    assert server.stops == 1 and not (tmp_path / 'audio' / 'fixture-6.wav').exists()
    transport.fail = False
    service.synthesize(pipeline, 'こんにちは。', turn_id=6)
    assert len(server.starts) == 4
    service.stop()
    assert server.stops == 2
    with pytest.raises(TTSError):
        service.synthesize(pipeline, 'こんにちは。')


def test_api_upload_preview_turn_ownership_and_late_expression(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    transport, server = FakeSpeech(), FakeServer()
    app.state.tts.transport, app.state.tts.server = transport, server
    with TestClient(app) as client:
        assert not pipeline.config.tts.enabled
        assert not server.starts
        assert client.get('/api/voice').status_code == 200
        response = client.put('/api/settings', json={'tts': {'enabled': True, 'autoplay': False, 'autoplay_proactive': True}})
        assert response.status_code == 200 and response.json()['tts']['autoplay'] is False and response.json()['tts']['autoplay_proactive'] is True
        voice = client.post('/api/voice/design').json()['voice']
        seed = voice['seed']
        voice['seed'] = 2
        assert client.put('/api/voice', json=voice).json()['voice']['seed'] == seed
        assert client.put('/api/voice/reference', content=b'bad').status_code == 422
        uploaded = client.put('/api/voice/reference', content=wav()).json()['uploaded']
        assert (root / 'fixture' / 'voice' / uploaded).is_file()
        voice.update(mode='clone', reference=uploaded, apply_references=True)
        assert client.put('/api/voice', json=voice).status_code == 200
        assert client.put('/api/voice', json={**voice, 'reference': '../other.wav'}).status_code == 422
        response = client.post('/api/voice/preview', json={'text': 'こんにちは。', 'expression': 'sad'})
        assert response.status_code == 200 and response.content[:4] == b'RIFF'
        assert transport.calls[-1][1]['input'] == '😢 こんにちは。'
        assert transport.calls[-1][1]['voice'] == 'partner'
        session = pipeline.store.create_session('fixture')
        turn = pipeline.store.add_turn(session['id'], 'おはよう', 'こんにちは。', {'Expression': {'state': 'pending'}}, expression='thinking')
        assert client.post(f'/api/turns/{turn}/audio').status_code == 409
        pipeline.store.finish_expression(turn, 'smile', {'Expression': {'output': {'expression': 'smile'}}})
        assert client.post(f'/api/turns/{turn}/audio').status_code == 200
        assert transport.calls[-1][1]['input'] == '😊 こんにちは。'
        history = client.get(f"/api/sessions/{session['id']}/turns").json()
        assert history[-1]['assistant_text'] == 'こんにちは。' and history[-1]['user_text'] == 'おはよう'
        other = pipeline.store.create_session('someone_else')
        foreign = pipeline.store.add_turn(other['id'], '', 'secret', {})
        assert client.post(f'/api/turns/{foreign}/audio').status_code == 404
        assert client.post('/api/turns/999999/audio').status_code == 404
        assert client.post('/api/voice/preview', json={'expression': 'bad'}).status_code == 422
        assert client.put('/api/settings', json={'tts': {'enabled': False}}).status_code == 200
        assert client.post(f'/api/turns/{turn}/audio').status_code == 503
    assert server.stops >= 1


def test_release_checksum_and_extracted_files(tmp_path, monkeypatch):
    import cbs.tts as tts
    class Offline:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, *args, **kwargs):
            raise AssertionError('No network')
    monkeypatch.setattr(tts.httpx, 'Client', lambda **kwargs: Offline())
    archive = tmp_path / 'speech.zip'
    with zipfile.ZipFile(archive, 'w') as target:
        target.writestr('speech.exe', b'fake executable')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (tmp_path / 'SHA256SUMS').write_text(digest + '  ' + ASSET)
    exe = verified_archive(tmp_path)
    assert exe.read_bytes() == b'fake executable'
    exe.write_bytes(b'tampered')
    assert verified_archive(tmp_path).read_bytes() == b'fake executable'
    archive.write_bytes(b'bad')
    with pytest.raises(TTSError, match='チェックサム'):
        verified_archive(tmp_path, allow_download=False)


@pytest.mark.parametrize('gpu', [False, True])
def test_child_lifecycle_device_warmup_clone_and_restart(tmp_path, monkeypatch, gpu):
    import cbs.tts as tts
    commands, processes = [], []
    monkeypatch.setattr(tts, 'verified_archive', lambda root, **kwargs: root / 'speech.exe')
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=b'Vulkan0  gpu  Fake' if gpu else b'CPU cpu Fake')
    monkeypatch.setattr(tts.subprocess, 'run', run)
    monkeypatch.setattr('cbs.tts_download.installed_model', lambda exe, quant: 'existing-' + quant + '.gguf')
    class Process:
        code = None
        def poll(self): return self.code
        def terminate(self): self.code = 0
        def wait(self, **kwargs): return self.code
    def popen(command, **kwargs):
        commands.append(command)
        process = Process()
        processes.append(process)
        return process
    monkeypatch.setattr(tts.subprocess, 'Popen', popen)
    class Health:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url): return SimpleNamespace(is_success=True)
    monkeypatch.setattr(tts.httpx, 'Client', lambda **kwargs: Health())
    server = SpeechServer(tmp_path)
    root = tmp_path / 'fixture'
    (root / 'voice').mkdir(parents=True)
    (root / 'voice' / 'clone.wav').write_bytes(wav())
    cfg, voice = TTSConfig(enabled=True), VoiceConfig(mode='clone', reference='clone.wav')
    server.start(cfg, voice, root)
    command = commands[-1]
    assert command[1:3] == ['serve', 'existing-q8_0.gguf' if gpu else 'existing-q4_k.gguf']
    assert command[3:5] == ['--host', '127.0.0.1'] and '--no-warmup' not in command
    assert '--add-voice' in command and ('--steps' in command) is (not gpu)
    server.start(cfg, voice, root)
    assert len(processes) == 1
    processes[0].code = 1
    server.start(cfg, voice, root)
    assert len(processes) == 2
    server.stop()
    assert processes[1].code == 0


def test_external_transport_has_no_local_process(pipeline, tmp_path):
    root = tmp_path / 'characters'
    (root / 'fixture').mkdir(parents=True)
    pipeline.config.tts = TTSConfig(enabled=True, engine='external', base_url='http://fake.invalid/v1', model='fake-tts',
        styles={'external': VoiceStyle(mode='instructions', values={'sad': '悲しげに'})})
    transport, server = FakeSpeech(), FakeServer()
    service = TTSService(tmp_path, root, transport=transport, server=server)
    service.startup(pipeline)
    service.synthesize(pipeline, 'こんにちは。', 'sad')
    assert not server.starts
    url, body = transport.calls[0]
    assert url == 'http://fake.invalid/v1' and body['model'] == 'fake-tts'
    assert body['input'] == 'こんにちは。' and body['instructions'] == '悲しげに'


def test_welcome_voice_preview_stays_private_until_confirm(pipeline, tmp_path):
    from cbs.llm import FakeLLMClient
    from test_bootstrap import minimal_input, package_data
    from test_web_bootstrap import finish
    app, root = app_for(pipeline, tmp_path)
    app.state.tts.transport, app.state.tts.server = FakeSpeech(), FakeServer()
    pipeline.config.tts.enabled = True
    pipeline.client = FakeLLMClient(json_responses=[package_data(), {'problems': []},
        {'introduction': '紹介文', 'open_now': []}])
    with TestClient(app) as client:
        job = finish(client, client.post('/api/bootstrap/create', json=minimal_input().model_dump(mode='json', by_alias=True)))
        assert job['status'] == 'done', job
        cid = job['result']['id']
        assert not (root / cid).exists()
        assert job['result']['voice']['enabled'] and job['result']['voice']['description']
        preview = client.post(f"/api/bootstrap/jobs/{job['id']}/voice-preview")
        assert preview.status_code == 200 and preview.content[:4] == b'RIFF'
        assert not (root / cid).exists()
        assert client.post('/api/bootstrap/jobs/missing/voice-preview').status_code == 409
        assert client.post(f"/api/bootstrap/jobs/{job['id']}/confirm").status_code == 200
        assert read_voice(root / cid).description
        assert client.post(f"/api/bootstrap/jobs/{job['id']}/voice-preview").status_code == 409


def test_first_download_is_verified_before_extraction(tmp_path, monkeypatch):
    import cbs.tts as tts
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as package:
        package.writestr('speech.exe', b'executable')
    data = content.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    class Download:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_bytes(self): yield data[:5]; yield data[5:]
        def get(self, url):
            assert url == tts.RELEASE + '/SHA256SUMS'
            return SimpleNamespace(content=(digest + '  ' + ASSET).encode(), raise_for_status=lambda: None)
        status_code = 200
        headers = {}
        def stream(self, method, url, **kwargs):
            assert method == 'GET' and url == tts.RELEASE + '/' + ASSET
            return self
    monkeypatch.setattr(tts.httpx, 'Client', lambda **kwargs: Download())
    assert verified_archive(tmp_path).read_bytes() == b'executable'
    assert not (tmp_path / 'speech.zip.part').exists()
    # A mismatch must never install or retain the partial download.
    bad = tmp_path / 'bad'
    bad.mkdir()
    (bad / 'SHA256SUMS').write_text('0' * 64 + '  ' + ASSET)
    with pytest.raises(TTSError):
        verified_archive(bad)
    assert not (bad / 'bin').exists() and not (bad / 'speech.zip.part').exists()


def test_sentence_stream_cache_and_error_events(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    # app_for copies the user's character, which may already have a decided
    # voice. This test specifically exercises creation of a fresh design sample.
    write_voice(root / 'fixture', VoiceConfig())
    transport, server = FakeSpeech(), FakeServer()
    app.state.tts.transport, app.state.tts.server = transport, server
    pipeline.config.tts.enabled = True
    session = pipeline.store.create_session('fixture')
    turn = pipeline.store.add_turn(session['id'], '', 'こんにちは、今日も来てくれたんだね。今日はとてもいい日になりそうだね。', {}, expression='smile')
    with TestClient(app) as client:
        response = client.post(f'/api/turns/{turn}/audio/stream')
        assert response.status_code == 200 and 'text/event-stream' in response.headers['content-type']
        assert response.text.count('event: audio') == 2 and response.text.endswith('event: done\ndata: {}\n\n')
        assert len(transport.calls) == 3
        repeat = client.post(f'/api/turns/{turn}/audio/stream')
        assert repeat.text.count('event: audio') == 1 and len(transport.calls) == 3
        transport.fail = True
        failed = pipeline.store.add_turn(session['id'], '', 'こんにちは。', {})
        failure = client.post(f'/api/turns/{failed}/audio/stream')
        assert 'event: error' in failure.text and 'private engine error' not in failure.text
        assert not (app.state.tts.audio_dir / f'fixture-{failed}.wav').exists()
        assert client.post('/api/turns/999999/audio/stream').status_code == 404
        pipeline.config.tts.enabled = False
        assert client.post(f'/api/turns/{turn}/audio/stream').status_code == 503


def test_sentence_callback_delivers_before_remaining_synthesis(pipeline, tmp_path):
    root = tmp_path / 'characters'
    (root / 'fixture').mkdir(parents=True)
    pipeline.config.tts.enabled = True
    transport = FakeSpeech()
    service = TTSService(tmp_path, root, transport=transport, server=FakeServer())
    delivered = []
    data = service.synthesize(pipeline, 'こんにちは、今日も来てくれたんだね。今日はとてもいい日になりそうだね。', on_audio=lambda part: delivered.append(len(transport.calls)))
    assert delivered == [2, 3] and wav_info(data).nframes == 960


def test_very_short_sentences_are_joined_to_the_next():
    from cbs.tts import text_chunks
    assert list(text_chunks('こんにちは。今日も会えてうれしい。')) == ['こんにちは。今日も会えてうれしい。']
    assert list(text_chunks('こんにちは、今日も来てくれたんだね。今日はとてもいい日になりそうだね。')) == [
        'こんにちは、今日も来てくれたんだね。', '今日はとてもいい日になりそうだね。']


def test_design_sample_trims_long_silence():
    import io, wave, array
    from cbs.tts import trim_silence
    rate = 48000
    samples = array.array('h', [0] * rate + [8000, -8000] * (rate // 4) + [0] * rate)
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(samples.tobytes())
    with wave.open(io.BytesIO(trim_silence(out.getvalue()))) as w:
        assert 0.5 <= w.getnframes() / rate <= 0.85


def test_punctuation_never_starts_a_chunk_and_long_sentences_split_at_commas():
    from cbs.tts import text_chunks
    long = '焼いてじゅわっとしたしいたけに、ゴルゴンゾーラみたいなコクのあるチーズと黒こしょうを合わせるだけで、手軽なのにすごく贅沢なおつまみになるんだって。'
    chunks = list(text_chunks(long + 'そこに少し温めた純米酒を合わせると、たまらないらしい。', limit=200))
    assert all(c[0] not in '。、！？…' for c in chunks)
    assert all(len(c.encode('utf-8')) <= 200 for c in chunks)
    assert list(text_chunks('……。')) == []


def test_short_last_sentence_joins_the_previous_chunk():
    from cbs.tts import text_chunks
    assert list(text_chunks('おかえり、今日もおつかれさま。\nゆっくりできそう？')) == ['おかえり、今日もおつかれさま。ゆっくりできそう？']
    assert list(text_chunks('うん。')) == ['うん。']
