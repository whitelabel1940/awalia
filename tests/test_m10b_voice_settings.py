"""M10b state transitions: all downloads, processes and speech are offline fakes."""
import hashlib
import io
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import wave
import zipfile

import pytest
from fastapi.testclient import TestClient

from cbs.config import TTSConfig
from cbs.tts import ASSET, TTSError, TTSService, VoiceConfig, read_voice, verified_archive, write_voice
from cbs.tts_download import DownloadJob, SpeechInstaller, installed_model, select_model
from test_m10_tts import FakeServer, FakeSpeech
from test_web_bootstrap import app_for, finish
from test_bootstrap import minimal_input, package_data
from cbs.llm import FakeLLMClient


def recording(seconds):
    out = io.BytesIO()
    with wave.open(out, 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\0\0' * int(8000 * seconds))
    return out.getvalue()


class SmallDownload:
    def __init__(self):
        self.done = 0
        self.fail = False

    def plan(self, choice):
        return dict(choice=choice, model='q8_0', total=100, completed=self.done,
                    download_bytes=100-self.done, reason='Fake GPU', ready=self.done == 100)

    def acquire(self, choice, cancel, progress):
        if self.fail:
            raise RuntimeError('private error')
        while self.done < 100:
            if cancel.wait(.01):
                raise InterruptedError()
            self.done += 5
            progress('model', self.done, 100)
        return 'q8_0', 'Fake GPU'


def test_download_confirmation_progress_cancel_resume_error_and_restart():
    backend = SmallDownload()
    job = DownloadJob(backend)
    assert job.status('auto')['status'] == 'missing'
    quote = job.quote('auto')
    with pytest.raises(TTSError):
        job.start(quote['token'], False)
    assert backend.done == 0
    job.start(quote['token'], True)
    with pytest.raises(TTSError):
        job.start(job.quote('auto')['token'], True)
    time.sleep(.06)
    state = job.status('auto')
    assert 0 < state['completed'] < state['total'] and 0 < state['percent'] < 100
    assert state['eta_seconds'] is not None
    job.cancel()
    job.thread.join(2)
    assert job.status('auto')['status'] == 'cancelled'
    offset = backend.done
    job = DownloadJob(backend)  # Process restart retains backend partials, no job metadata needed.
    assert job.quote('auto')['completed'] == offset
    job.start(job.quote('auto')['token'], True)
    job.thread.join(2)
    assert job.status('auto')['status'] == 'ready' and backend.done == 100
    backend.fail = True
    job.start(job.quote('auto')['token'], True)
    job.thread.join(2)
    assert job.status('auto')['status'] == 'error'
    assert 'private' not in str(job.status('auto'))


@pytest.mark.parametrize('gpu,choice,expected', [(True,'auto','q8_0'),(False,'auto','q4_k'),(True,'q4_k','q4_k'),(False,'q8_0','q8_0')])
def test_gpu_and_explicit_quantization(monkeypatch, gpu, choice, expected):
    monkeypatch.setattr('cbs.tts_download.subprocess.run', lambda *a, **k: SimpleNamespace(stdout=b'Vulkan0 gpu Fake\nCPU cpu Fake' if gpu else b'CPU cpu Fake'))
    device, quant, reason = select_model('fake', choice)
    assert quant == expected and device == ('gpu' if gpu else 'cpu') and reason


def test_installer_uses_resumable_pull_and_terminates_child(tmp_path, monkeypatch):
    import cbs.tts_download as module
    cancelled, commands, events = threading.Event(), [], []
    monkeypatch.setattr(module, 'verified_archive', lambda *a, **k: tmp_path / 'speech.exe')
    monkeypatch.setattr(module, 'select_model', lambda *a: ('gpu', 'q8_0', 'GPU'))
    monkeypatch.setattr(module, 'catalog', lambda *a: {'size':100,'partial':40,'fetched':False})
    class Process:
        code = None
        def poll(self): return self.code
        def terminate(self): self.code = 0
        def wait(self, **kwargs): return self.code
    process = Process()
    monkeypatch.setattr(module.subprocess, 'Popen', lambda command, **kwargs: (commands.append(command) or process))
    def progress(*args):
        events.append(args)
        cancelled.set()
    with pytest.raises(InterruptedError):
        SpeechInstaller(tmp_path).acquire('auto', cancelled, progress)
    assert commands[0][1:] == ['pull','irodori-tts:q8_0']
    assert process.code == 0 and events == [('model',40,100)]
    monkeypatch.setattr(module, 'catalog', lambda *a: {'fetched':False})
    with pytest.raises(TTSError):
        installed_model('fake', 'q8_0')


def test_archive_cancel_resume_and_ignored_range(tmp_path, monkeypatch):
    import cbs.tts as module
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('speech.exe', b'x' * 100)
    content = out.getvalue()
    (tmp_path / 'SHA256SUMS').write_text(hashlib.sha256(content).hexdigest() + '  ' + ASSET)
    cancelled, requests, events = threading.Event(), [], []
    ignore_range = False
    class HTTP:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stream(self, method, url, headers):
            requests.append(headers)
            self.offset = int(headers.get('Range', 'bytes=0-')[6:-1]) if not ignore_range else 0
            self.status_code = 206 if self.offset else 200
            self.headers = {'Content-Length':str(len(content)-self.offset)}
            if self.offset:
                self.headers['Content-Range'] = f'bytes {self.offset}-{len(content)-1}/{len(content)}'
            return self
        def raise_for_status(self): pass
        def iter_bytes(self):
            yield content[self.offset:self.offset+20]
            yield content[self.offset+20:]
    monkeypatch.setattr(module.httpx, 'Client', lambda **kwargs: HTTP())
    def progress(done, total):
        events.append((done,total))
        cancelled.set()
    with pytest.raises(InterruptedError):
        verified_archive(tmp_path, progress=progress, cancel=cancelled)
    assert (tmp_path / 'speech.zip.part').stat().st_size == 20
    cancelled.clear()
    assert verified_archive(tmp_path, cancel=cancelled).read_bytes() == b'x' * 100
    assert requests[-1] == {'Range':'bytes=20-'}
    (tmp_path / 'speech.zip').unlink()
    (tmp_path / 'speech.zip.part').write_bytes(content[:20])
    ignore_range = True
    assert verified_archive(tmp_path).read_bytes() == b'x' * 100


def test_reference_draft_list_apply_limit_remove_and_failure(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    service = app.state.tts
    service.transport, service.server = FakeSpeech(), FakeServer()
    calls = []
    def combine(config, directory, names):
        calls.append(names)
        target = 'combined-' + str(len(calls)) + '.gguf'
        (directory / 'voice' / target).write_bytes(b'voice')
        return target
    service.combine_references = combine
    with TestClient(app) as client:
        voice = client.get('/api/voice').json()['voice']
        one = client.put('/api/voice/reference', content=recording(12), headers={'X-Filename':'first.wav'}).json()
        two = client.put('/api/voice/reference', content=recording(18), headers={'X-Filename':'second.wav'}).json()
        assert [c['name'] for c in two['references']] == ['first.wav','second.wav']
        assert two['total_seconds'] == 30 and not two['reference_warning']
        assert read_voice(root/'fixture').reference == voice['reference']
        assert client.get('/api/voice/references/'+one['uploaded']).content == recording(12)
        assert client.get('/api/voice/references/missing.wav').status_code == 404
        voice.update(mode='clone',apply_references=True)
        response = client.put('/api/voice', json=voice)
        assert response.status_code == 200, response.text
        applied = response.json()['voice']['reference']
        assert calls == [[one['uploaded'],two['uploaded']]]
        client.delete('/api/voice/references/'+one['uploaded'])
        assert read_voice(root/'fixture').reference == applied
        assert client.get('/api/voice').json()['reference_warning']
        client.put('/api/voice/reference', content=recording(110))
        assert client.get('/api/voice').json()['total_seconds'] == 128
        assert client.put('/api/voice', json=voice).status_code == 422
        assert read_voice(root/'fixture').reference == applied
        assert client.delete('/api/voice/references').json()['references'] == []
        assert read_voice(root/'fixture').reference == applied
        assert client.put('/api/voice', json=voice).status_code == 422
        client.put('/api/voice/reference', content=recording(5))
        service.combine_references = lambda *a: (_ for _ in ()).throw(TTSError('failed'))
        assert client.put('/api/voice', json=voice).status_code == 503
        assert read_voice(root/'fixture').reference == applied
        assert client.put('/api/voice/reference', content=recording(121)).status_code == 422


def test_design_reference_generated_once_persisted_regenerated_and_failed_safely(pipeline, tmp_path):
    root = tmp_path/'characters'
    directory = root/'fixture'
    directory.mkdir(parents=True)
    write_voice(directory, VoiceConfig(description='柔らかな声'))
    pipeline.config.tts.enabled = True
    speech, server = FakeSpeech(), FakeServer()
    service = TTSService(tmp_path, root, transport=speech, server=server)
    service.synthesize(pipeline, 'これは一文目の少し長めの文です。これは二文目の少し長めの文です。')
    sample = read_voice(directory).sample_reference
    assert (directory/'voice'/sample).is_file()
    assert [p['voice'] for _,p in speech.calls] == ['none','partner','partner']
    assert all('instructions' not in p for _,p in speech.calls[1:])
    TTSService(tmp_path, root, transport=speech, server=server).synthesize(pipeline, 'これは三文目の少し長めの文です。これは四文目の少し長めの文です。')
    assert len(speech.calls) == 5 and all(p['voice']=='partner' for _,p in speech.calls[3:])
    assert read_voice(directory).sample_reference == sample
    voice = service.design_sample(pipeline, directory, regenerate=True)
    assert voice.sample_reference != sample
    speech.fail = True
    with pytest.raises(RuntimeError):
        service.design_sample(pipeline, directory, regenerate=True)
    assert read_voice(directory).sample_reference == voice.sample_reference


def test_download_api_confirmation_and_first_run_skip(pipeline, tmp_path, monkeypatch):
    app, _ = app_for(pipeline, tmp_path)
    app.state.voice_download.installer = SmallDownload()
    with TestClient(app) as client:
        assert client.get('/api/settings').json()['voice_setup_done'] is False
        assert client.post('/api/setup/voice-step',json={'skipped':True}).status_code == 409
        monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic-test-key')
        assert client.post('/api/settings/connection').status_code == 200
        assert client.post('/api/setup/voice-step',json={'skipped':True}).status_code == 200
        assert client.get('/api/settings').json()['voice_setup_done'] is True
        plan = client.post('/api/voice/download/plan',json={'choice':'auto'}).json()
        assert client.post('/api/voice/download',json={'token':plan['token']}).status_code == 409
        assert client.post('/api/voice/download',json={'token':plan['token'],'confirmed':True}).status_code == 200
        assert client.post('/api/voice/download/cancel').status_code == 200
        assert client.get('/api/voice/download?choice=invalid').status_code == 422
        assert client.get('/api/setup/voice').status_code == 200


def test_setup_voice_is_inherited_only_when_welcomed(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    app.state.tts.transport, app.state.tts.server = FakeSpeech(), FakeServer()
    pipeline.config.tts.enabled = True
    pipeline.client = FakeLLMClient(json_responses=[package_data(), {'problems':[]}, {'introduction':'紹介文','open_now':[]}])
    with TestClient(app) as client:
        configured = client.get('/api/setup/voice').json()['voice']
        configured['description'] = '初回設定で選んだ声'
        assert client.put('/api/setup/voice',json=configured).status_code == 200
        assert client.post('/api/setup/voice/preview',json={'text':'こんにちは。'}).status_code == 200
        job = finish(client, client.post('/api/bootstrap/create',json=minimal_input().model_dump(mode='json',by_alias=True)))
        assert job['status'] == 'done', job
        cid = job['result']['id']
        assert not (root/cid).exists()
        assert client.post(f"/api/bootstrap/jobs/{job['id']}/confirm").status_code == 200
        voice = read_voice(root/cid)
        assert voice.description == configured['description'] and voice.sample_reference
        assert (root/cid/'voice'/voice.sample_reference).is_file()
