"""Fetch the copyable setup guide on demand; never connect to an AI."""
import asyncio

import httpx

from cbs.config import ROOT
from cbs.news import https_url

MAX_BYTES = 64 * 1024


def guide_text(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError('setup guide too large')
    text = raw.decode('utf-8')
    if not text.split('\n', 1)[0].startswith('<!-- awalia-setup-helper '):
        raise ValueError('invalid setup guide header')
    return text


async def fetch_guide(url):
    async with httpx.AsyncClient(timeout=3, follow_redirects=False) as client:
        async with client.stream('GET', https_url(url)) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise ValueError('setup guide too large')
            return bytes(raw)


async def setup_helper(config, data_dir, *, first_run=False):
    cache = data_dir / 'setup_helper' / 'cache.md'
    if config.enabled and not first_run:
        try:
            raw = await asyncio.wait_for(fetch_guide(config.url), timeout=3)
            text = guide_text(raw)
        except (OSError, ValueError, httpx.HTTPError, httpx.InvalidURL, TimeoutError):
            try:
                return {'text': guide_text(cache.read_bytes()), 'source': 'cache'}
            except (OSError, ValueError):
                pass
        else:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache.with_suffix('.md.tmp')
                temporary.write_bytes(raw)
                temporary.replace(cache)
            except OSError:
                pass  # A read-only data directory must not discard a valid latest guide.
            return {'text': text, 'source': 'latest'}
    return {'text': (ROOT / 'onboarding' / 'setup_helper.md').read_text(encoding='utf-8'),
            'source': 'bundled'}
