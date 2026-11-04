"""Public notices, independent of partner conversations and LLM calls."""
import asyncio
from datetime import date
from importlib.metadata import version, PackageNotFoundError
import json
import platform
from pathlib import Path
import re
from threading import Lock
import tomllib
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def https_url(value):
    parsed = urlsplit(value)
    parsed.port  # Reject malformed and out-of-range ports as part of validation.
    if parsed.scheme != 'https' or not parsed.hostname or any(c.isspace() for c in value):
        raise ValueError('https URL required')
    return value


HTTPS = Annotated[str, AfterValidator(https_url)]


class NewsModel(BaseModel):
    model_config = ConfigDict(extra='ignore')


class Link(NewsModel):
    label: str
    url: HTTPS


class Latest(NewsModel):
    version: str = Field(pattern=r'^\d+\.\d+\.\d+$')
    url: HTTPS
    notes: str


class Notice(NewsModel):
    id: str = Field(min_length=1)
    date: date
    title: str
    body: str
    links: list[Link] = Field(default_factory=list)
    until: date | None = None


class Feed(NewsModel):
    version: Literal[1]
    latest: Latest
    feedback_url: Annotated[str, AfterValidator(lambda v: https_url(v) if v else v)]
    notices: list[Notice]


def current_version():
    source = Path(__file__).resolve().parents[1] / 'pyproject.toml'
    if source.is_file():
        with source.open('rb') as stream:
            return tomllib.load(stream)['project']['version']
    try:
        return version('cbs-partner')
    except PackageNotFoundError:
        return '0.0.0'


async def fetch_feed(url):
    # The outer deadline also bounds the entire response, including slow trickles.
    async with httpx.AsyncClient(timeout=4, follow_redirects=False) as client:
        response = await client.get(https_url(url))
        response.raise_for_status()
        return response.text


class News:
    def __init__(self, config, data_dir):
        self.config = config
        self.root = Path(data_dir) / 'news'
        self.lock = Lock()
        self.current = current_version()
        self.feed = None
        self.read_ids = set()
        try:
            self.feed = Feed.model_validate_json((self.root / 'cache.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        try:
            state = json.loads((self.root / 'state.json').read_text(encoding='utf-8'))
            self.read_ids = {v for v in state['read_ids'] if isinstance(v, str)}
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def save(self, filename, value):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / filename
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)

    async def refresh(self):
        if not self.config.enabled:
            return
        try:
            raw = await asyncio.wait_for(fetch_feed(self.config.url), timeout=5)
            feed = Feed.model_validate_json(raw)
            with self.lock:
                self.save('cache.json', feed.model_dump(mode='json'))
                self.feed = feed
        except (OSError, ValueError, httpx.HTTPError, httpx.InvalidURL, TimeoutError):
            pass

    async def run(self):
        if not self.config.enabled:
            return
        while True:
            await self.refresh()
            await asyncio.sleep(86400)

    def snapshot(self, today=None):
        with self.lock:
            feed = self.feed if self.config.enabled else None
            notices = [] if feed is None else [
                {**n.model_dump(mode='json'), 'read': n.id in self.read_ids}
                for n in sorted(feed.notices, key=lambda n: n.date, reverse=True)
                if n.until is None or n.until >= (today or date.today())]
            latest = feed.latest if feed else None
            parts = lambda v: tuple(map(int, v.split('.'))) if re.fullmatch(r'\d+\.\d+\.\d+', v) else (0, 0, 0)
            newer = bool(latest and parts(latest.version) > parts(self.current))
            feedback = feed.feedback_url if feed else ''
            feedback = feedback.replace('{version}', quote(self.current, safe='')).replace('{os}', quote(platform.system(), safe=''))
            return {'current_version': self.current, 'has_update': newer,
                    'latest': latest.model_dump() if latest else None, 'notices': notices,
                    'unread_count': sum(not n['read'] for n in notices), 'feedback_url': feedback}

    def mark_read(self, ids):
        with self.lock:
            known = {n.id for n in self.feed.notices} if self.feed else set()
            updated = self.read_ids | (set(ids) & known)
            self.save('state.json', {'read_ids': sorted(updated)})
            self.read_ids = updated


class ReadInput(BaseModel):
    ids: list[str] = Field(max_length=10000)


def install_news(app, config, data_dir):
    news = News(config, data_dir)
    app.state.news = news

    @app.get('/api/news')
    def get_news():
        return news.snapshot()

    @app.post('/api/news/read')
    def read_news(body: ReadInput):
        news.mark_read(body.ids)
        return news.snapshot()
