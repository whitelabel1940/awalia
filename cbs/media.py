"""Bounded Wikipedia reads and YouTube metadata; all fetched content stays in memory."""
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit, quote

from cbs.fetch import FetchError


def youtube_url(url):
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return None
    if p.scheme not in ('https', 'http') or p.username or p.password or port not in (None, 80, 443):
        return None
    if p.hostname in ('www.youtube.com', 'youtube.com', 'm.youtube.com') and p.path == '/watch':
        vid = parse_qs(p.query).get('v', [''])[0]
    elif p.hostname == 'youtu.be':
        vid = p.path.strip('/')
    else:
        return None
    return 'https://www.youtube.com/watch?v=' + vid if re.fullmatch(r'[A-Za-z0-9_-]{11}', vid) else None


def video_model(config, *, fake=False):
    from cbs.config import key_set, research_provider, VideoToolConfig
    configured = config.autonomy.tools.watch_video.model
    gemini = fake or key_set(config, 'gemini')
    provider = config.llm.providers.get('openrouter')
    router = not config.llm.zdr_only and (fake or (key_set(config, 'openrouter') and provider is not None and provider.api == 'chat'))
    # Historical default means automatic selection, including existing config files.
    if configured != VideoToolConfig.model_fields['model'].default:
        if configured.startswith('gemini/') and gemini:
            return configured
        if configured.startswith('openrouter/') and router:
            return configured
        return None
    if research_provider(config) == 'gemini' and gemini:
        return 'gemini/gemini-3.8-flash'
    if router:
        return 'openrouter/google/gemini-3.8-flash'
    return None


def video_ready(pipeline):
    from cbs.llm import FakeLLMClient
    return video_model(pipeline.config, fake=isinstance(pipeline.client, FakeLLMClient)) is not None


def video_metadata(fetch, url, max_minutes):
    url = youtube_url(url)
    if not url:
        raise FetchError('YouTube の動画URLではありません。')
    page = fetch(url, max_bytes=4_000_000)
    if youtube_url(page.url) != url:
        raise FetchError('別の動画または動画以外へ転送されました。')
    # Read the public player metadata, never the media file or transcript.
    match = re.search(r'(?:var\s+)?ytInitialPlayerResponse\s*=\s*', page.text())
    if not match:
        raise FetchError('動画の長さを確認できません。')
    data, _ = json.JSONDecoder().raw_decode(page.text()[match.end():])
    details = data.get('videoDetails', {})
    seconds = int(details.get('lengthSeconds', 0))
    if data.get('playabilityStatus', {}).get('status') != 'OK' or details.get('isLiveContent') or not 0 < seconds <= max_minutes*60:
        raise FetchError('公開状態または動画の長さが条件外です。')
    return {'title': str(details['title'])[:500], 'uri': url, 'duration_seconds': seconds}


def video_candidates(store, cid):
    candidates = [{'title': i['title'], 'uri': i['url'], 'source_item_id': i['id']}
                  for i in store.source_candidates(cid) if youtube_url(i['url'])]
    watched = set()
    for activity in store.list_activities(cid):
        if activity['status'] != 'completed':
            continue
        if activity['kind'] == 'watch_video':
            watched.update(youtube_url(s.get('uri', '')) for s in activity['sources'])
        elif activity['kind'] == 'research':
            candidates.extend({'title': s.get('title', ''), 'uri': s['uri'], 'source_item_id': None}
                              for s in activity['sources'] if youtube_url(s.get('uri', '')))
    unique = {}
    for item in candidates:
        url = youtube_url(item['uri'])
        if url not in watched:
            unique.setdefault(url, {**item, 'uri': url})
    return list(unique.values())[:20]


def wiki_api(fetch, path):
    url = 'https://api.wikimedia.org/core/v1/wikipedia/ja/' + path
    page = fetch(url, max_bytes=2_000_000)
    if urlsplit(page.url).hostname != 'api.wikimedia.org':
        raise FetchError('Wikipedia 外へ転送されました。')
    data = json.loads(page.text())
    if 'error' in data:
        raise FetchError('Wikipedia API が取得を拒否しました。')
    return data


def wikipedia_search(fetch, query):
    if not query.strip():
        raise ValueError('検索語がありません。')
    return [{'page_id': int(p['id']), 'title': p['title'], 'key': p['key']}
            for p in wiki_api(fetch, 'search/page?' + urlencode({'q': query[:300], 'limit': 5})).get('pages', [])]


def wikipedia_read(fetch, candidate):
    from cbs.reading import html
    page = wiki_api(fetch, 'page/' + quote(candidate['key'], safe='') + '/with_html')
    if page.get('id') != candidate['page_id'] or not page.get('html'):
        raise FetchError('記事の本文がありません。')
    return {'title': page['title'], 'uri': 'https://ja.wikipedia.org/wiki/' + quote(page['key'], safe='')}, html(page['html']).text()
