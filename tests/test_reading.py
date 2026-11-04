import csv
import io
import json
import socket
import zipfile

import pytest
from fastapi.testclient import TestClient

from cbs import clock
from cbs.autonomy import BookProposal, available_tools, inner_life
from cbs.autonomy import ReadingImpression
from cbs.fetch import FetchError, Page, PublicFetcher, public_url, USER_AGENT
from cbs.llm import FakeLLMClient
from cbs.note import public_note
from cbs.reading import CATALOG_URL, ReadingTools, chunk_text, feed_items, html
from cbs.steward import submit
from cbs.web import create_app


def address(ip):
    return lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]


@pytest.mark.parametrize('ip', ['127.0.0.1', '10.1.0.1', '172.16.1.1', '192.168.0.1', '169.254.1.1', '::1', 'fe80::1', 'fc00::1', '0.0.0.0', '::ffff:127.0.0.1'])
def test_rejects_nonpublic_dns(ip):
    with pytest.raises(FetchError):
        public_url('https://reader.example/x', address(ip))


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://example.org', 'https://user:password@example.org'])
def test_rejects_nonhttp_or_credentials(url):
    with pytest.raises(FetchError):
        public_url(url, address('8.8.8.8'))


def test_fetch_robots_redirect_dns_pinning_limits_and_interval(monkeypatch):
    calls = []
    def transport(url, ip, port, timeout, limit):
        calls.append((url, ip, timeout, limit))
        if url.endswith('robots.txt'):
            return 200, {}, b'User-agent: *\nDisallow: /private\n'
        if url.endswith('/redirect'):
            return 302, {'Location': 'http://127.0.0.1/secret'}, b''
        return 200, {}, b'hello'
    resolve = lambda h, p, **kw: address('127.0.0.1' if h == '127.0.0.1' else '8.8.8.8')(h, p)
    waits = []
    monkeypatch.setattr('cbs.fetch.time.sleep', waits.append)
    fetch = PublicFetcher(resolver=resolve, transport=transport, interval=.1)
    with pytest.raises(FetchError, match='robots'):
        fetch('https://reader.example/private')
    assert len(calls) == 1
    assert fetch('https://reader.example/public').body == b'hello'
    assert calls[-1][1] == '8.8.8.8' and any(w > 0 for w in waits)
    with pytest.raises(FetchError):
        fetch('https://reader.example/redirect')
    assert not any('127.0.0.1' in c[0] for c in calls)
    with pytest.raises(FetchError, match='上限'):
        fetch('https://reader.example/public', max_bytes=3)
    assert 'CBSPartner' in USER_AGENT


def test_robots_error_fails_closed_and_total_timeout():
    fetch = PublicFetcher(resolver=address('8.8.8.8'), transport=lambda *a: (403, {}, b''), interval=0)
    with pytest.raises(FetchError):
        fetch('https://reader.example/')
    fetch = PublicFetcher(resolver=address('8.8.8.8'), transport=lambda *a: (200, {}, b''), interval=10, timeout=.01)
    with pytest.raises(FetchError, match='時間'):
        fetch('https://reader.example/')


RSS = '<rss><channel><title>ニュース</title><item><title>香りの記事</title><link>https://reader.example/article</link><pubDate>today</pubDate></item></channel></rss>'
ARTICLE = '<html><title>記事</title><nav>広告</nav><main><article><p>ARTICLE_BODY_NEVER_PERSISTED 独自の長い本文。</p><p>読んだ部分の文章。</p></article><aside>関連広告</aside></main><footer>フッター</footer></html>'


def install_reader(pipeline, *, failing=False):
    calls = []
    def fetch(url, **kw):
        calls.append(url)
        if failing:
            raise FetchError('do not retain raw exception')
        if url.endswith('/account'):
            return Page(url, b'<title>account</title><link rel="alternate" type="application/rss+xml" href="/rss">')
        if url.endswith('/rss'):
            return Page(url, RSS.encode())
        return Page(url, ARTICLE.encode())
    pipeline.reading_tools = ReadingTools(pipeline.store, fetch)
    return pipeline.reading_tools, calls


def run(pipeline, action, **fields):
    pipeline.client.inner_life_responses.append({'action': action, 'reason': '読みたい', 'topic': '読書', **fields})
    submit(pipeline.store, inner_life, pipeline).result(timeout=10)


def test_discovery_rss_atom_page_extraction_and_errors(pipeline):
    reader, calls = install_reader(pipeline)
    source = reader.register('fixture', 'https://reader.example/account')
    assert source['kind'] == 'feed' and source['title'] == 'ニュース'
    assert source['url'].endswith('/account') and len(calls) == 2
    assert reader.register('fixture', 'https://reader.example/account')['id'] == source['id']
    assert len(pipeline.store.list_sources('fixture')) == 1
    assert len(pipeline.store.source_candidates('fixture')) == 1
    assert pipeline.store.source_candidates('other') == []
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><title>A</title><entry><title>B</title><link href="/b"/><updated>now</updated></entry></feed>'
    assert feed_items(atom, 'https://reader.example/')[1][0]['url'].endswith('/b')
    parser = html(ARTICLE)
    assert '独自' in parser.text() and all(s not in parser.text() for s in ('広告', 'フッター'))
    assert len(parser.text(limit=5)) == 5
    page = reader.register('fixture', 'https://reader.example/page')
    assert page['kind'] == 'page'
    reader.fetch = lambda *a, **kw: (_ for _ in ()).throw(FetchError('secret-body'))
    assert not reader.refresh('fixture', source['id'])['ok']
    source = pipeline.store.list_sources('fixture')[0]
    assert source['error'] and source['checked_at'] and 'secret-body' not in source['error']


def test_source_body_never_saved_and_result_goes_to_note_share_interests(pipeline):
    reader, _ = install_reader(pipeline)
    reader.register('fixture', 'https://reader.example/rss')
    item = pipeline.store.source_candidates('fixture')[0]
    pipeline.client.json_responses = [{'summary': '香りについての記事を読んだ。', 'impression': '静けさが気になる。', 'quote': '読んだ部分の文章。'}]
    pipeline.client.activity_responses = [{'interests': [{'operation': 'new', 'topic': '静けさ', 'kind': 'curiosity', 'detail': '静けさを知りたい'}]}]
    run(pipeline, 'read_source', source_item_id=item['id'])
    activity = pipeline.store.list_activities('fixture')[0]
    assert activity['status'] == 'completed' and activity['sources'][0]['quote'] == '読んだ部分の文章。'
    assert not pipeline.store.source_candidates('fixture')
    assert pipeline.store.list_threads('fixture')[0]['source_ref'] == activity['ref']
    assert any(i['origin'] == 'activity' for i in pipeline.store.list_interests('fixture'))
    assert public_note(pipeline)['activities']
    with pipeline.store.connect() as db:
        dump = '\n'.join(db.iterdump())
    assert 'ARTICLE_BODY_NEVER_PERSISTED' not in dump
    assert not list(pipeline.store.path.parent.glob('aozora/*'))
    trace = pipeline.store.list_inner_life('fixture')[0]['trace']
    assert trace['tools'] and trace['budget_before']['tools']['read_source']['remaining'] == 3
    assert trace['budget_after']['tools']['read_source']['remaining'] == 2


def test_tool_conditions_targets_failure_budgets_day_reset(pipeline):
    reader, calls = install_reader(pipeline)
    source = reader.register('fixture', 'https://reader.example/rss')
    calls.clear()
    with clock.at('2026-10-03 12:00'):
        pipeline.config.autonomy.tools.read_source.enabled = False
        run(pipeline, 'read_source', source_item_id=1)
        assert not calls and not pipeline.store.list_activities('fixture')
        pipeline.config.autonomy.tools.read_source.enabled = True
        pipeline.character.world['reality'] = 'ファンタジー'
        run(pipeline, 'read_source', source_item_id=1)
        # M8b-2 can refresh feed candidates for adaptation, but cannot read them directly.
        assert calls == ['https://reader.example/rss']
        assert not pipeline.store.list_activities('fixture')
        assert pipeline.store.list_inner_life('fixture')[0]['status'] == 'limited'
        pipeline.character.world['reality'] = '現実どおり'
        pipeline.config.autonomy.tools.read_source.daily_max = 1
        run(pipeline, 'read_source', source_item_id=999)
        assert pipeline.store.list_activities('fixture')[0]['status'] == 'failed'
        assert pipeline.store.autonomy_budget('fixture')['tools']['read_source']['remaining'] == 0
        calls.clear()
        run(pipeline, 'read_source', source_item_id=1)
        assert not calls and len(pipeline.store.list_activities('fixture')) == 1
    with clock.at('2026-10-04 12:00'):
        assert pipeline.store.autonomy_budget('fixture')['tools']['read_source']['remaining'] == 1
    with pipeline.store.connect() as db:
        db.execute('UPDATE sources SET enabled=0 WHERE id=?', (source['id'],))
    assert not next(t for t in available_tools(pipeline, pipeline.store.autonomy_budget('fixture')) if t['name'] == 'read_source')['available']


@pytest.mark.parametrize('body', ['COPY_GUARD_BODY' + '他人の文章です。'*80, 'COPY_GUARD_BODY 短い記事。'])
def test_quote_length_and_copy_guard_are_code_rules(pipeline, body):
    assert ReadingImpression(summary='短いまとめ', impression='感想', quote='あ'*200).quote == 'あ'*140
    reader, _ = install_reader(pipeline)
    reader.register('fixture', 'https://reader.example/rss')
    original = reader.fetch
    reader.fetch = lambda url, **kw: Page(url, ('<article>'+body+'</article>').encode()) if url.endswith('/article') else original(url, **kw)
    pipeline.client.json_responses = [{'summary': body, 'impression': '感想'}]
    run(pipeline, 'read_source', source_item_id=pipeline.store.source_candidates('fixture')[0]['id'])
    assert pipeline.store.list_activities('fixture')[0]['status'] == 'failed'
    assert pipeline.store.source_candidates('fixture')[0]['read_at'] is None
    assert not public_note(pipeline)['activities']
    with pipeline.store.connect() as db:
        assert 'COPY_GUARD_BODY' not in '\n'.join(db.iterdump())


def test_read_failure_trace_never_persists_article_input(pipeline):
    reader, _ = install_reader(pipeline)
    reader.register('fixture', 'https://reader.example/rss')
    # Invalid structured output causes FakeLLMClient to attach its whole request to an error.
    pipeline.client.json_responses = [{'not_a_summary': 'invalid'}]
    run(pipeline, 'read_source', source_item_id=pipeline.store.source_candidates('fixture')[0]['id'])
    trace = pipeline.store.list_inner_life('fixture')[0]['trace']
    assert trace['error_type'] == 'LLMError'
    with pipeline.store.connect() as db:
        assert 'ARTICLE_BODY_NEVER_PERSISTED' not in '\n'.join(db.iterdump())


def catalog_zip():
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=['作品ID', '作品名', '作品著作権フラグ', 'XHTML/HTMLファイルURL', '姓', '名', '役割フラグ'])
    writer.writeheader()
    for wid, title, copyright in [('000001', '本物の本', 'なし'), ('000002', '著作権あり', 'あり')]:
        writer.writerow({'作品ID': wid, '作品名': title, '作品著作権フラグ': copyright,
            'XHTML/HTMLファイルURL': f'https://www.aozora.gr.jp/cards/{wid}.html', '姓': '著者', '名': '名', '役割フラグ': '著者'})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        z.writestr('catalog.csv', text.getvalue().encode('utf-8-sig'))
    return buffer.getvalue()


def book_reader(pipeline):
    calls = []
    def fetch(url, **kw):
        calls.append(url)
        if url == CATALOG_URL:
            return Page(url, catalog_zip())
        body = '<div class="main_text">' + ''.join('<p>' + str(n) + 'あ'*999 + '</p>' for n in range(12)) + '<ruby>漢字<rt>かんじ</rt><rp>（</rp></ruby><span class="notes">入力者注</span>［＃注記］</div>'
        return Page(url, body.encode('shift_jis'))
    pipeline.reading_tools = ReadingTools(pipeline.store, fetch)
    return pipeline.reading_tools, calls


def test_catalog_verified_first_match_cache_chunks_progress_and_check(pipeline):
    reader, calls = book_reader(pipeline)
    with pytest.raises(ValueError):
        reader.match_book([BookProposal(title='存在しない本')])
    with pytest.raises(ValueError):
        reader.match_book([BookProposal(title='著作権あり')])
    proposals = [{'title': '存在しない本'}, {'title': '本物の本', 'author': '著者 名'}]
    pipeline.client.json_responses = [{'summary': '始めの範囲を読んだ。', 'impression': '先が気になる。', 'quote': ''}]
    run(pipeline, 'read_book', book_proposals=proposals)
    book = pipeline.store.list_books('fixture')[0]
    assert book['read_count'] == 1 and book['chunk_count'] == 3 and not book['finished_at']
    chunks = json.loads((reader.cache / '000001.json').read_text(encoding='utf-8'))
    assert all(4000 <= len(c) <= 6000 for c in chunks[:-1])
    assert not any(s in ''.join(chunks) for s in ('かんじ', '入力者注', '注記'))
    assert '漢字' in ''.join(chunks)
    assert calls.count(CATALOG_URL) == 1
    for n in [2, 3]:
        pipeline.config.autonomy.tools.read_book.daily_max = 3
        pipeline.client.json_responses = [{'summary': f'{n}番目を読んだ。', 'impression': '感想。'}]
        run(pipeline, 'read_book', book_id=book['id'])
    book = pipeline.store.list_books('fixture')[0]
    assert book['read_count'] == book['chunk_count'] and book['finished_at']
    assert len(calls) == 2
    assert public_note(pipeline)['books'][0]['read_count'] == 3
    assert len(public_note(pipeline)['activities']) == 3
    assert [n['title'] for n in public_note(pipeline)['activities']] == ['本物の本（3 / 3）', '本物の本（2 / 3）', '本物の本（1 / 3）']
    with pipeline.store.connect() as db:
        db.execute("UPDATE note_entries SET text=? WHERE kind='activity'", (json.dumps({'title': '古い読書記録', 'summary': 'まとめ', 'impression': '感想'}),))
    assert [n['title'] for n in public_note(pipeline)['activities']] == ['本物の本（3 / 3）', '本物の本（2 / 3）', '本物の本（1 / 3）']
    # A semantic judgment belongs to Check; code passes progress and obeys its verdict.
    pipeline.client = FakeLLMClient(text_responses=['まだ読んでいない結末を知ってる。', '今読んだところが気になる。'],
        json_responses=[{}, {'problems': [{'text': '結末を知ってる', 'verdict': 'forbidden', 'reason': '未読範囲'}]}, {'problems': []}])
    with pipeline.store.connect() as db:
        db.execute('UPDATE books SET read_count=1,finished_at=NULL WHERE id=?', (book['id'],))
    result = pipeline.run(pipeline.store.create_session('fixture')['id'], '本のことを教えて')
    pipeline.wait_for_steward()
    assert result['assistant_text'] == '今読んだところが気になる。'
    call = next(c for c in pipeline.client.calls if c.get('schema', {}).get('title') == 'Check')
    assert json.loads(call['messages'][0]['content'])['packet']['reading_progress'][0]['read_count'] == 1
    assert '未読' in call['system']


def test_chunking_short_tail_and_long_paragraph():
    chunks = chunk_text(('あ'*1000+'\n\n')*15)
    assert all(4000 <= len(c) <= 6000 for c in chunks)
    assert ''.join(chunk_text('あ'*15000)) == 'あ'*15000
    assert chunk_text('') == []


def test_source_api_and_tool_settings_are_scoped_and_validated(pipeline, tmp_path):
    install_reader(pipeline)
    with TestClient(create_app(pipeline, settings_dir=tmp_path)) as web:
        source = web.post('/api/sources', json={'url': 'https://reader.example/account'}).json()
        assert source['kind'] == 'feed'
        assert web.post('/api/sources', json={'url': 'file:///x'}).status_code == 422
        assert web.post(f"/api/sources/{source['id']}/test", json={}).json()['ok']
        assert web.patch(f"/api/sources/{source['id']}", json={'enabled': False}).status_code == 200
        cfg = web.put('/api/settings', json={'autonomy': {'tools': {'read_book': {'enabled': False, 'daily_max': 8}}}})
        assert cfg.status_code == 200 and not cfg.json()['autonomy']['tools']['read_book']['enabled']
        assert web.put('/api/settings', json={'autonomy': {'tools': {'invented': {'daily_max': 1}}}}).status_code == 422
        assert web.put('/api/settings', json={'autonomy': {'tools': {'read_book': {'daily_max': -1}}}}).status_code == 422
        assert web.put('/api/settings', json={'autonomy': {'research_daily_max': None}}).status_code == 422
        assert web.put('/api/settings', json={'autonomy': {'tools': {'research': {'daily_max': 7}}}}).status_code == 200
        from cbs.config import load_config
        saved = load_config(data_dir=tmp_path)
        assert saved.autonomy.tools.research.daily_max == saved.autonomy.research_daily_max == 7
        assert web.get('/api/sources?character_id=missing').status_code == 404
        assert web.delete(f"/api/sources/{source['id']}").status_code == 200
        assert web.get('/api/sources').json() == []
        assert web.post(f"/api/sources/{source['id']}/test", json={}).status_code == 404


def test_proposed_author_matches_catalog_spelling_variants(pipeline, monkeypatch):
    from cbs.reading import same_text
    assert same_text('芥川龍之介', '芥川竜之介', part=True) and same_text('森 鷗外', '森鴎外', part=True)
    assert not same_text('芥川龍之介', '樋口一葉', part=True)
    reader, _ = book_reader(pipeline)
    monkeypatch.setattr(reader, 'catalog', lambda: [
        {'work_id': '000128', 'title': '羅生門', 'author': '芥川竜之介', 'url': 'https://www.aozora.gr.jp/x.html'},
        {'work_id': '056041', 'title': 'たけくらべ', 'author': '樋口一葉', 'url': 'https://www.aozora.gr.jp/y.html'}])
    # The first proposal wins even when the catalog spells the author differently.
    work = reader.match_book([BookProposal(title='羅生門', author='芥川龍之介'), BookProposal(title='たけくらべ', author='樋口一葉')])
    assert work['work_id'] == '000128'
