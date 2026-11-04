"""SP2 upgrade paths: archive/HTTP/CLI fakes only, no installed exe or models."""
import hashlib
import io
import zipfile
from types import SimpleNamespace

import httpx
import pytest

from cbs.config import load_config
from cbs.stt_download import HaasInstaller
from cbs.tts import ASSET, LEGACY_ASSET, RELEASE, TTSError, verified_archive
from cbs.tts_download import ARCHIVE_BYTES, SpeechInstaller

HTTPClient = httpx.Client


def package(content):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('speech.exe', content)
    return output.getvalue()


def test_pinned_release():
    assert RELEASE.endswith('/v0.9.4')
    assert ASSET == 'speech-0.9.4-windows-x64-vulkan.zip'


def test_legacy_playback_explicit_upgrade_reuses_models_and_preserves_old_install(tmp_path, monkeypatch):
    import cbs.tts_download as downloads
    old, new = package(b'old fake'), package(b'new fake')
    (tmp_path / 'speech.zip').write_bytes(old)
    old_sums = hashlib.sha256(old).hexdigest() + '  ' + LEGACY_ASSET
    (tmp_path / 'SHA256SUMS').write_text(old_sums)
    (tmp_path / 'speech.zip.part').write_bytes(b'legacy partial, incompatible with the new release')
    calls = []
    def handler(request):
        calls.append(request)
        assert 'Range' not in request.headers
        if request.url.path.endswith('SHA256SUMS'):
            return httpx.Response(200, text=hashlib.sha256(new).hexdigest() + '  ' + ASSET)
        assert request.url.path.endswith(ASSET)
        return httpx.Response(200, content=new)
    monkeypatch.setattr('cbs.tts.httpx.Client', lambda **kwargs: HTTPClient(transport=httpx.MockTransport(handler)))
    exe = verified_archive(tmp_path, allow_download=False)
    assert exe == tmp_path / 'bin' / 'speech.exe' and exe.read_bytes() == b'old fake'
    plan = SpeechInstaller(tmp_path).plan('auto')
    assert plan['engine_update'] and not plan['ready'] and plan['engine_bytes'] == ARCHIVE_BYTES
    assert '更新' in plan['reason'] and not calls
    local = HaasInstaller(tmp_path, load_config(data_dir=tmp_path))
    assert local.plan('haas-ja')['engine_update'] and not local.plan('haas-ja')['ready']
    # Cancellation or an incomplete new download leaves old playback usable.
    (tmp_path / 'v0.9.4').mkdir()
    (tmp_path / 'v0.9.4' / 'speech.zip.part').write_bytes(new[:10])
    assert verified_archive(tmp_path, allow_download=False) == exe
    (tmp_path / 'v0.9.4' / 'speech.zip.part').unlink()
    installed = verified_archive(tmp_path)
    assert installed == tmp_path / 'v0.9.4' / 'bin' / 'speech.exe'
    assert installed.read_bytes() == b'new fake' and exe.read_bytes() == b'old fake'
    assert (tmp_path / 'speech.zip').read_bytes() == old
    assert (tmp_path / 'SHA256SUMS').read_text() == old_sums
    assert verified_archive(tmp_path, allow_download=False) == installed
    # Both versions use the CLI's default model directory; never change or delete it.
    model = tmp_path / 'existing-irodori.gguf'
    model.write_bytes(b'existing model')
    monkeypatch.setattr(downloads, 'select_model', lambda *a: ('gpu', 'q8_0', 'GPU'))
    monkeypatch.setattr(downloads, 'catalog', lambda *a: dict(size=100, fetched=True, path=str(model)))
    plan = SpeechInstaller(tmp_path).plan('auto')
    assert plan['ready'] and plan['download_bytes'] == 0 and not plan['engine_update']
    assert model.read_bytes() == b'existing model'
    installed.write_bytes(b'tampered')
    assert verified_archive(tmp_path, allow_download=False).read_bytes() == b'new fake'
    (tmp_path / 'v0.9.4' / 'speech.zip').write_bytes(b'bad archive')
    with pytest.raises(TTSError, match='チェックサム'):
        verified_archive(tmp_path, allow_download=False)


def test_catalog_and_device_selection_no_real_process(tmp_path, monkeypatch):
    import json
    from cbs.tts_download import catalog, select_device
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=json.dumps({'models': [{'name': 'haas-ja-v1.0',
            'files': [{'type': 'q8_0', 'path': 'fake', 'size': 100, 'fetched': True}]}]}).encode()
            if command[1] == 'models' else b'CPU cpu Fake')
    monkeypatch.setattr('cbs.tts_download.subprocess.run', run)
    assert catalog('fake.exe', 'q8_0', 'haas-ja-v1.0')['size'] == 100
    assert select_device('fake.exe') == 'cpu'
    count = len(commands)
    assert select_device('fake.exe', 'gpu') == 'gpu'
    assert len(commands) == count
