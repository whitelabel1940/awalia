"""Sources and public-domain books. Bodies from sources remain in memory only."""
import csv
from datetime import timedelta
import io
import json
from html.parser import HTMLParser
from pathlib import Path
import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

from cbs import clock
from cbs.fetch import FetchError, PublicFetcher, in_domains

CATALOG_URL = 'https://www.aozora.gr.jp/index_pages/list_person_all_extended_utf8.zip'

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, url TEXT NOT NULL,
 kind TEXT NOT NULL, title TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 checked_at TEXT, error TEXT, structure_checked INTEGER NOT NULL DEFAULT 0, UNIQUE(character_id, url)
);
CREATE TABLE IF NOT EXISTS source_items (
 id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 url TEXT NOT NULL, title TEXT NOT NULL, published_at TEXT, read_at TEXT,
 UNIQUE(source_id, url)
);
CREATE TABLE IF NOT EXISTS books (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, work_id TEXT NOT NULL,
 title TEXT NOT NULL, author TEXT NOT NULL, url TEXT NOT NULL, chunk_count INTEGER NOT NULL,
 read_count INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, finished_at TEXT,
 UNIQUE(character_id, work_id)
);
CREATE TABLE IF NOT EXISTS reading_receipts (
 activity_id INTEGER PRIMARY KEY REFERENCES activities(id), book_id INTEGER REFERENCES books(id),
 chunk_number INTEGER, source_item_id INTEGER REFERENCES source_items(id) ON DELETE SET NULL
);
"""


# The catalog writes some names with the common form (芥川竜之介, 森鴎外); a proposal may use the
# traditional one. Only character variants are folded, never meaning.
VARIANTS = str.maketrans('龍鷗澤齋齊邊邉濱國學櫻廣眞髙﨑德惠藏', '竜鴎沢斎斉辺辺浜国学桜広真高崎徳恵蔵')


def same_text(proposed, catalog, *, part=False):
    fold = lambda t: unicodedata.normalize('NFKC', t).replace(' ', '').replace('　', '').translate(VARIANTS)
    return fold(proposed) in fold(catalog) if part else fold(proposed) == fold(catalog)


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.all, self.preferred, self.links, self.title_parts = [], [], [], [], []
        self.anchors, self.anchor = [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parent_skip = self.stack[-1][1] if self.stack else False
        skip = parent_skip or tag in ('script', 'style', 'nav', 'header', 'footer', 'aside', 'rt', 'rp') or any(
            c in attrs.get('class', '').split() for c in ('notes', 'notation_notes', 'bibliographical_information'))
        preferred = (self.stack[-1][2] if self.stack else False) or tag in ('article', 'main') or 'main_text' in attrs.get('class', '').split()
        if tag == 'a':
            self.anchor = [attrs.get('href', ''), [], skip]
        if tag == 'link' and 'alternate' in attrs.get('rel', '').lower().split() and attrs.get('type', '').lower() in ('application/rss+xml', 'application/atom+xml'):
            self.links.append(attrs.get('href', ''))
        if tag in ('p', 'div', 'br', 'section', 'h1', 'h2', 'li') and not skip:
            self.all.append('\n\n')
            if preferred:
                self.preferred.append('\n\n')
        if tag not in ('br', 'img', 'meta', 'link', 'hr', 'input', 'wbr', 'source'):
            self.stack.append((tag, skip, preferred))

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor:
            self.anchors.append(self.anchor)
            self.anchor = None
        for n in range(len(self.stack)-1, -1, -1):
            if self.stack[n][0] == tag:
                del self.stack[n:]
                break

    def handle_data(self, data):
        if self.anchor and not (self.stack and self.stack[-1][1]):
            self.anchor[1].append(data)
        if any(t[0] == 'title' for t in self.stack):
            self.title_parts.append(data)
        if self.stack and self.stack[-1][1]:
            return
        self.all.append(data)
        if self.stack and self.stack[-1][2]:
            self.preferred.append(data)

    def text(self, limit=24000):
        text = ''.join(self.preferred or self.all)
        text = re.sub(r'［＃[^］]*］', '', text)
        return re.sub(r'\n[ \t\r\n]*\n', '\n\n', text).strip()[:limit]


def html(text):
    parser = TextHTML()
    parser.feed(text)
    return parser


def web_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise FetchError('http / https のURLを指定してください。')
    return url


def index_items(parsed, base):
    """Conservative shape rule: >=5 distinct, labelled, same-host deep links.

    Navigation/chrome, queries, fragments, assets and shallow category links do
    not provide enough evidence. No language or topic classification is used.
    """
    root = urlsplit(base)
    items = {}
    for href, parts, skipped in parsed.anchors:
        title = ' '.join(''.join(parts).split())
        try:
            url = web_url(urljoin(base, href))
            p = urlsplit(url)
        except ValueError:
            continue
        segments = [s for s in p.path.split('/') if s]
        suffix = Path(p.path).suffix.lower()
        if (skipped or len(title) < 8 or p.hostname != root.hostname or p.port != root.port
                or p.query or p.fragment or p.path.rstrip('/') == root.path.rstrip('/')
                or (len(segments) < 2 and suffix not in ('.html', '.htm'))
                or (suffix and suffix not in ('.html', '.htm'))):
            continue
        items[url] = {'url': url, 'title': title[:500], 'published_at': None}
    # A repeated path family is stronger structural evidence than scattered
    # navigation destinations. Isolated "back to index" links do not qualify.
    groups = {}
    for item in items.values():
        family = urlsplit(item['url']).path.strip('/').split('/')[0]
        groups.setdefault(family, []).append(item)
    return [item for group in groups.values() if len(group) >= 5 for item in group][:100]


def feed_items(text, base):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    local = lambda t: t.split('}')[-1]
    if local(root.tag) not in ('rss', 'feed', 'RDF'):
        return None
    title = next((e.text for e in root.iter() if local(e.tag) == 'title' and e.text), base)
    items = []
    for entry in root.iter():
        if local(entry.tag) not in ('item', 'entry'):
            continue
        fields = {local(e.tag): e.text or '' for e in entry}
        link = next((e.get('href') or e.text for e in entry if local(e.tag) == 'link' and e.get('rel', 'alternate') == 'alternate'), None)
        if not link:
            continue
        try:
            link = web_url(urljoin(base, link.strip()))
        except ValueError:
            continue
        items.append({'url': link, 'title': fields.get('title', link)[:500],
                      'published_at': fields.get('pubDate') or fields.get('published') or fields.get('updated')})
    return (title[:500], items[:100])


class ReadingStore:
    def list_sources(self, cid):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM sources WHERE character_id=? ORDER BY id', (cid,))]

    def source_candidates(self, cid):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT i.*, s.title AS source_title FROM source_items i JOIN sources s ON s.id=i.source_id '
                'WHERE s.character_id=? AND s.enabled=1 AND i.read_at IS NULL ORDER BY i.id DESC LIMIT 100', (cid,))]

    def list_books(self, cid):
        with self.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM books WHERE character_id=? ORDER BY id DESC', (cid,))]
        # Only impressions from already read chunks, never cached unread text, enter the packet.
        with self.connect() as db:
            for row in rows:
                row['read_impressions'] = [r[0] for r in db.execute("SELECT a.summary FROM activities a JOIN reading_receipts r ON r.activity_id=a.id "
                    "WHERE r.book_id=? AND r.chunk_number<=? AND a.status='completed' ORDER BY r.chunk_number", (row['id'], row['read_count']))]
        return rows


class ReadingTools:
    def __init__(self, store, fetcher=None, *, blocked_domains=None):
        from cbs.config import ShoppingToolConfig
        self.store = store
        self.blocked_domains = list(dict.fromkeys(ShoppingToolConfig().domains + (blocked_domains or [])))
        self._fetch = fetcher or PublicFetcher(blocked_domains=self.blocked_domains)
        self.interval_hours = 24
        self.cache = store.path.parent / 'aozora'

    def fetch(self, url, **kwargs):
        if in_domains(url, self.blocked_domains):
            raise FetchError('買い物サイトは検索結果だけを読みます。')
        return self._fetch(url, **kwargs)

    def refresh(self, cid, source_id):
        source = next((s for s in self.store.list_sources(cid) if s['id'] == source_id), None)
        if not source:
            raise KeyError(source_id)
        if source['checked_at'] and (source['kind'] == 'index' or source['error']):
            if clock.now() - clock.parse(source['checked_at']) < timedelta(hours=self.interval_hours):
                return {'ok': not bool(source['error']), 'skipped': True}
        if source['checked_at'] and source['kind'] == 'page' and source['structure_checked'] and not source['error']:
            return {'ok': True, 'skipped': True}
        try:
            page = self.fetch(source['url'], max_bytes=2_000_000)
            feed = feed_items(page.text(), page.url)
            if feed is None:
                parsed = html(page.text())
                for link in parsed.links[:5]:
                    try:
                        linked = self.fetch(web_url(urljoin(page.url, link)), max_bytes=2_000_000)
                        feed = feed_items(linked.text(), linked.url)
                    except FetchError:
                        continue
                    if feed is not None:
                        page = linked
                        break
            if feed is not None:
                title, items = feed
                kind = 'feed'
            else:
                title = ''.join(parsed.title_parts).strip()[:500] or page.url
                items = index_items(parsed, page.url)
                # Once registered as an index, a temporarily shorter list must
                # never turn the index itself into an unread article.
                if source['kind'] == 'index' or len(items) >= 5:
                    kind = 'index'
                else:
                    items, kind = [{'url': page.url, 'title': title, 'published_at': None}], 'page'
            with self.store.connect() as db:
                # Keep the user-registered URL so adding the same account stays idempotent.
                db.execute('UPDATE sources SET kind=?, title=?, checked_at=?, structure_checked=1, error=NULL WHERE id=?',
                           (kind, title, clock.iso(), source_id))
                if kind == 'index' and source['kind'] == 'page':
                    db.execute('DELETE FROM source_items WHERE source_id=? AND read_at IS NULL AND url IN (?,?)',
                               (source_id, source['url'], page.url))
                for item in items:
                    db.execute('INSERT INTO source_items (source_id,url,title,published_at) VALUES (?,?,?,?) '
                        'ON CONFLICT(source_id,url) DO UPDATE SET title=excluded.title,published_at=excluded.published_at',
                        (source_id, item['url'], item['title'], item['published_at']))
            return {'ok': True, 'items': len(items), 'kind': kind}
        except Exception:
            message = '取得できませんでした。公開URL・robots.txt・接続を確認してください。'
            with self.store.connect() as db:
                db.execute('UPDATE sources SET checked_at=?, error=? WHERE id=?', (clock.iso(), message, source_id))
            return {'ok': False, 'error': message}

    def register(self, cid, url):
        url = web_url(url.strip())
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO sources (character_id,url,kind,title) VALUES (?,?,'page',?)", (cid, url, url))
            sid = db.execute('SELECT id FROM sources WHERE character_id=? AND url=?', (cid, url)).fetchone()[0]
        self.refresh(cid, sid)
        return next(s for s in self.store.list_sources(cid) if s['id'] == sid)

    def catalog(self):
        self.cache.mkdir(parents=True, exist_ok=True)
        path = self.cache / 'catalog.csv'
        stale = path.exists() and clock.now().timestamp() - path.stat().st_mtime >= 30 * 86400
        if not path.exists() or stale:
            try:
                self._refresh_catalog(path)
            except Exception:
                if not path.exists():
                    raise
                # The old catalog remains usable on HTTP, archive or validation errors.
        works = {}
        for r in csv.DictReader(io.StringIO(path.read_text(encoding='utf-8-sig'))):
            if r.get('作品著作権フラグ') != 'なし' or not r.get('XHTML/HTMLファイルURL'):
                continue
            wid = r['作品ID']
            if wid not in works:
                works[wid] = {'work_id': wid, 'title': r['作品名'], 'author': r.get('姓', '') + r.get('名', ''), 'url': r['XHTML/HTMLファイルURL']}
            elif r.get('役割フラグ') == '著者':
                works[wid]['author'] += ' / ' + r.get('姓', '') + r.get('名', '')
        return list(works.values())

    def _refresh_catalog(self, path):
        page = self.fetch(CATALOG_URL, max_bytes=12_000_000)
        with zipfile.ZipFile(io.BytesIO(page.body)) as archive:
            info = next(i for i in archive.infolist() if i.filename.endswith('.csv'))
            if info.file_size > 40_000_000:
                raise FetchError('作品一覧が大きすぎます。')
            body = archive.read(info)
        # Validate all rows before replacement; archive names never become paths.
        reader = csv.DictReader(io.StringIO(body.decode('utf-8-sig')), strict=True)
        rows = list(reader)
        if (not {'作品ID', '作品名', '作品著作権フラグ', 'XHTML/HTMLファイルURL'}.issubset(reader.fieldnames or [])
                or not rows or any(None in r or not r.get('作品ID') or not r.get('作品名')
                                   or r.get('作品著作権フラグ') not in ('あり', 'なし')
                                   or r.get('XHTML/HTMLファイルURL') is None for r in rows)):
            raise ValueError('青空文庫の作品一覧の形式が違います。')
        temp = path.with_suffix('.tmp')
        temp.write_bytes(body)
        temp.replace(path)

    def match_book(self, proposals):
        catalog = self.catalog()
        for proposal in proposals[:5]:
            for work in catalog:
                if same_text(work['title'], proposal.title) and (not proposal.author or same_text(proposal.author, work['author'], part=True)):
                    return work
        raise ValueError('候補が青空文庫の公開作品一覧にありません。')

    def prepare_book(self, cid, work):
        p = urlsplit(work['url'])
        if p.scheme != 'https' and p.scheme != 'http' or p.hostname != 'www.aozora.gr.jp':
            raise FetchError('青空文庫の本文URLではありません。')
        wid = work['work_id']
        if not re.fullmatch(r'\d+', wid):
            raise ValueError('Invalid catalog ID')
        path = self.cache / f'{wid}.json'
        if not path.exists():
            page = self.fetch(work['url'], max_bytes=8_000_000)
            if urlsplit(page.url).hostname != 'www.aozora.gr.jp':
                raise FetchError('青空文庫外の本文です。')
            chunks = chunk_text(html(page.text('shift_jis')).text(limit=4_000_000))
            if not chunks:
                raise ValueError('本文がありません。')
            self.cache.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(chunks, ensure_ascii=False), encoding='utf-8')
            temp.replace(path)
        chunks = json.loads(path.read_text(encoding='utf-8'))
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO books (character_id,work_id,title,author,url,chunk_count,started_at) VALUES (?,?,?,?,?,?,?)',
                (cid, wid, work['title'], work['author'], work['url'], len(chunks), clock.iso()))
        return next(b for b in self.store.list_books(cid) if b['work_id'] == wid)

    def book_chunk(self, book):
        chunks = json.loads((self.cache / (book['work_id'] + '.json')).read_text(encoding='utf-8'))
        return chunks[book['read_count']]


def chunk_text(text):
    """Balance paragraph groups at 4–6k; split only paragraphs exceeding 6k."""
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    chunks, current = [], ''
    for para in paragraphs:
        while len(para) > 6000:
            if current:
                chunks.append(current)
                current = ''
            chunks.append(para[:5000])
            para = para[5000:]
        combined = current + ('\n\n' if current else '') + para
        if len(combined) > 6000:
            chunks.append(current)
            current = para
        else:
            current = combined
    if current:
        chunks.append(current)
    # Avoid a short final section when paragraph boundaries permit balancing.
    if len(chunks) > 1 and len(chunks[-1]) < 4000:
        previous = chunks[-2].split('\n\n')
        while len(previous) > 1 and len(chunks[-1]) < 4000:
            left = '\n\n'.join(previous[:-1])
            right = previous[-1] + '\n\n' + chunks[-1]
            if len(left) < 4000 or len(right) > 6000:
                break
            chunks[-2], chunks[-1] = left, right
            previous.pop()
    return chunks
