"""OB4 external opening is entirely offline, including webbrowser."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cbs.web.resident import install_resident


@pytest.fixture
def external(monkeypatch):
    app = FastAPI()
    install_resident(app, token='desktop-test-token')
    opened = []
    monkeypatch.setattr('cbs.web.resident.webbrowser.open', lambda url, **kw: opened.append((url, kw)) or True)
    return TestClient(app, base_url='http://127.0.0.1:9987', client=('127.0.0.1', 1234)), opened


@pytest.mark.parametrize('url', ['https://example.org/path?q=1#x', 'http://127.0.0.1:9987/oauth/openrouter/start'])
def test_external_accepts_http_only_from_desktop(external, url):
    client, opened = external
    response = client.post('/api/external/open', json={'url': url}, headers={'X-Awalia-Desktop': 'desktop-test-token', 'Origin': 'http://127.0.0.1:9987'})
    assert response.json() == {'opened': True}
    assert opened == [(url, {'new': 2})]


@pytest.mark.parametrize('url', ['file:///C:/secret', 'javascript:alert(1)', 'data:text/html,x', '/relative', 'https://', 'https://host:bad', 'https://user:pass@host', 'https://host/\nnext', 'https://host\\evil', 'https://[invalid'])
def test_external_rejects_invalid_urls(external, url):
    client, opened = external
    assert client.post('/api/external/open', json={'url': url}, headers={'X-Awalia-Desktop': 'desktop-test-token'}).status_code == 422
    assert not opened


def test_external_cross_site_and_form_requests_cannot_open(external):
    client, opened = external
    for headers in [{}, {'X-Awalia-Desktop': 'wrong'}, {'X-Awalia-Desktop': 'desktop-test-token', 'Origin': 'https://other.example'}]:
        assert client.post('/api/external/open', json={'url': 'https://example.org'}, headers=headers).status_code == 403
    assert client.post('/api/external/open', data={'url': 'https://example.org'}, headers={'X-Awalia-Desktop': 'desktop-test-token'}).status_code == 422
    assert client.get('/api/external/open?url=https://example.org').status_code == 405
    assert not opened


def test_external_open_failure(external, monkeypatch):
    client, opened = external
    monkeypatch.setattr('cbs.web.resident.webbrowser.open', lambda *a, **kw: False)
    assert client.post('/api/external/open', json={'url': 'https://example.org'}, headers={'X-Awalia-Desktop': 'desktop-test-token'}).status_code == 503
    assert not opened
