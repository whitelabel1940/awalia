import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError
import pytest

from cbs.config import ROOT, SetupHelperConfig, load_config
from cbs.web.onboarding_api import install_onboarding
from cbs.web.settings_api import atomic_write
from cbs.web.setup_helper import MAX_BYTES, fetch_guide as real_fetch_guide, guide_text, setup_helper

GUIDE = '<!-- awalia-setup-helper 1 -->\n手引き <script>literal</script>\n'


def run(config, directory, **kwargs):
    return asyncio.run(setup_helper(config, directory, **kwargs))


def test_latest_on_demand_and_api_route(tmp_path, monkeypatch):
    calls = []
    async def fetch(url):
        calls.append(url)
        return GUIDE.encode('utf-8')
    monkeypatch.setattr('cbs.web.setup_helper.fetch_guide', fetch)
    config = load_config(data_dir=tmp_path).setup_helper
    assert config == SetupHelperConfig()
    app = FastAPI()
    app.state.first_run = False
    install_onboarding(app, tmp_path, atomic_write, config)
    with TestClient(app) as client:
        assert calls == []
        assert client.get('/api/onboarding/ai').status_code == 200
        assert calls == []
        assert client.get('/api/onboarding/setup-helper').json() == {'text': GUIDE, 'source': 'latest'}
        assert calls == [config.url]
        app.state.first_run = True
        assert client.get('/api/onboarding/setup-helper').json()['source'] == 'bundled'
        assert calls == [config.url]
    assert (tmp_path / 'setup_helper/cache.md').read_bytes() == GUIDE.encode('utf-8')
    assert not (tmp_path / 'setup_helper/cache.md.tmp').exists()


@pytest.mark.parametrize('failure', [OSError('offline'), TimeoutError(),
    httpx.HTTPStatusError('redirect', request=httpx.Request('GET', 'https://example.org'),
                          response=httpx.Response(302)),
    b'no header', b'\n<!-- awalia-setup-helper 1 -->', b'<!-- awalia-setup-helper 1 -->\n\xff',
    b'<!-- awalia-setup-helper 1 -->\n' + b'x' * MAX_BYTES],
    ids=['offline', 'timeout', 'redirect', 'header', 'leading-line', 'utf8', 'oversize'])
def test_fallback_validates_latest_and_cache(tmp_path, monkeypatch, failure):
    async def fetch(url):
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr('cbs.web.setup_helper.fetch_guide', fetch)
    bundled = (ROOT / 'onboarding/setup_helper.md').read_text(encoding='utf-8')
    assert run(SetupHelperConfig(), tmp_path) == {'text': bundled, 'source': 'bundled'}
    cache = tmp_path / 'setup_helper/cache.md'
    cache.parent.mkdir(parents=True)
    cache.write_bytes(GUIDE.encode('utf-8'))
    assert run(SetupHelperConfig(), tmp_path) == {'text': GUIDE, 'source': 'cache'}
    assert cache.read_text(encoding='utf-8') == GUIDE
    cache.write_bytes(b'corrupted')
    assert run(SetupHelperConfig(), tmp_path)['source'] == 'bundled'


def test_size_is_utf8_bytes_and_header_is_first_line():
    header = b'<!-- awalia-setup-helper 2 -->\n'
    assert len(guide_text(header + b'x' * (MAX_BYTES - len(header))).encode('utf-8')) == MAX_BYTES
    for raw in [header + b'x' * (MAX_BYTES - len(header) + 1),
                header + ('あ' * (MAX_BYTES // 3)).encode('utf-8'),
                b'\xef\xbb\xbf' + header, b'<!-- wrong -->\n' + header]:
        with pytest.raises(ValueError):
            guide_text(raw)


@pytest.mark.parametrize('url', ['http://example.org', 'file:///guide', 'javascript:alert(1)', 'https://', 'https://bad host/'])
def test_https_only(url):
    with pytest.raises(ValidationError):
        SetupHelperConfig(url=url)


@pytest.mark.parametrize('enabled,trial', [(False, False), (True, True), (False, True)])
def test_disabled_and_trial_ignore_cache_and_never_fetch(tmp_path, monkeypatch, enabled, trial):
    async def forbidden(url):
        pytest.fail('must not fetch')
    monkeypatch.setattr('cbs.web.setup_helper.fetch_guide', forbidden)
    atomic_write(tmp_path / 'setup_helper/cache.md', GUIDE)
    result = run(SetupHelperConfig(enabled=enabled), tmp_path, first_run=trial)
    assert result == {'text': (ROOT / 'onboarding/setup_helper.md').read_text(encoding='utf-8'), 'source': 'bundled'}


def test_total_deadline(tmp_path, monkeypatch):
    async def fetch(url):
        await asyncio.sleep(60)
    wait_for = asyncio.wait_for
    async def deadline(awaitable, *, timeout):
        assert timeout == 3
        return await wait_for(awaitable, timeout=.01)
    monkeypatch.setattr('cbs.web.setup_helper.fetch_guide', fetch)
    monkeypatch.setattr('cbs.web.setup_helper.asyncio.wait_for', deadline)
    assert run(SetupHelperConfig(), tmp_path)['source'] == 'bundled'


@pytest.mark.parametrize('status,raw', [(200, GUIDE.encode('utf-8')), (302, b''),
                                       (200, b'x' * (MAX_BYTES + 1))], ids=['ok', 'redirect', 'oversize'])
def test_fetch_stream_bounds_and_no_redirects(monkeypatch, status, raw):
    # Exercise the actual fetch implementation with an in-memory HTTP transport.
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, content=raw, headers={'Location': 'https://example.org/other'})
    client_class = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs == {'timeout': 3, 'follow_redirects': False}
        return client_class(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr('cbs.web.setup_helper.httpx.AsyncClient', client)
    if status == 302:
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(real_fetch_guide('https://example.org/guide'))
    elif len(raw) > MAX_BYTES:
        with pytest.raises(ValueError):
            asyncio.run(real_fetch_guide('https://example.org/guide'))
    else:
        assert asyncio.run(real_fetch_guide('https://example.org/guide')) == raw
    assert len(requests) == 1
