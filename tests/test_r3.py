import asyncio
from datetime import date
import json
from urllib.parse import quote

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from cbs.config import NewsConfig
from cbs.news import Feed, News


def feed():
    return {'version': 1, 'latest': {'version': '9.0.0', 'url': 'https://example.com/download', 'notes': '<b>new</b>'},
            'feedback_url': 'https://example.com/?v={version}&os={os}',
            'notices': [{'id': 'a', 'date': '2026-11-01', 'title': 'title', 'body': '<b>text</b>',
                         'links': [{'label': 'link', 'url': 'https://example.com'}]},
                        {'id': 'expired', 'date': '2026-10-01', 'title': 'old', 'body': '', 'until': '2000-01-01'}]}


@pytest.mark.parametrize('change', [lambda f: f.update(version=2), lambda f: f.update(notices='bad'),
    lambda f: f['latest'].update(url='http://example.com'),
    lambda f: f.update(feedback_url='javascript:alert(1)'),
    lambda f: f['notices'][0]['links'][0].update(url='file:///tmp/x'),
    lambda f: f['notices'][0].update(until='bad'), lambda f: f['latest'].update(url='https://')])
def test_validation(change):
    value = feed()
    change(value)
    with pytest.raises(ValidationError):
        Feed.model_validate(value)


def test_cache_read_and_templates(tmp_path, monkeypatch):
    value = feed()
    value['unknown'] = True
    value['latest']['unknown'] = 3
    async def fetch(url):
        return json.dumps(value)
    monkeypatch.setattr('cbs.news.fetch_feed', fetch)
    monkeypatch.setattr('cbs.news.platform.system', lambda: 'Test OS / 日本')
    news = News(NewsConfig(), tmp_path)
    news.current = '0.1.0'
    asyncio.run(news.refresh())
    result = news.snapshot(date(2026, 11, 4))
    assert result['unread_count'] == 1 and result['has_update']
    assert result['feedback_url'].endswith('v=0.1.0&os=' + quote('Test OS / 日本', safe=''))
    assert [n['id'] for n in result['notices']] == ['a']
    news.mark_read(['a', 'unknown'])
    restored = News(NewsConfig(), tmp_path)
    assert restored.snapshot()['unread_count'] == 0
    assert restored.read_ids == {'a'}
    async def failure(url):
        raise OSError('offline')
    monkeypatch.setattr('cbs.news.fetch_feed', failure)
    asyncio.run(restored.refresh())
    assert restored.snapshot()['latest']['version'] == '9.0.0'
    async def malformed(url):
        return '{}'
    monkeypatch.setattr('cbs.news.fetch_feed', malformed)
    asyncio.run(restored.refresh())
    assert restored.snapshot()['latest']['version'] == '9.0.0'
    restored.current = '10.0.0'
    assert not restored.snapshot()['has_update']


def test_disabled(tmp_path, monkeypatch):
    async def forbidden(url):
        pytest.fail('must not fetch')
    monkeypatch.setattr('cbs.news.fetch_feed', forbidden)
    news = News(NewsConfig(enabled=False), tmp_path)
    asyncio.run(news.run())
    asyncio.run(news.refresh())
    assert not (tmp_path / 'news').exists()
    assert news.snapshot()['notices'] == []


def test_schedule(tmp_path, monkeypatch):
    calls = []
    async def fetch(url):
        calls.append(url)
        return json.dumps(feed())
    async def sleep(seconds):
        assert seconds == 86400
        raise asyncio.CancelledError()
    monkeypatch.setattr('cbs.news.fetch_feed', fetch)
    monkeypatch.setattr('cbs.news.asyncio.sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(News(NewsConfig(), tmp_path).run())
    assert len(calls) == 1


def test_api(pipeline, tmp_path):
    from cbs.web import create_app
    app = create_app(pipeline, settings_dir=tmp_path)
    app.state.news.feed = Feed.model_validate(feed())
    with TestClient(app) as client:
        assert client.get('/api/news').json()['unread_count'] == 1
        assert client.post('/api/news/read', json={'ids': ['a']}).json()['unread_count'] == 0
        assert client.post('/api/news/read', json={'ids': 'a'}).status_code == 422
    assert News(NewsConfig(), tmp_path).snapshot()['unread_count'] == 0


def test_seed():
    from cbs.config import ROOT
    from cbs.news import current_version
    seed = Feed.model_validate_json((ROOT / 'news/news.json').read_text())
    assert seed.latest.version == current_version()
    assert seed.notices == [] and seed.feedback_url == ''
