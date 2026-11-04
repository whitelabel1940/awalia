"""M8b-3: scripted LLM and in-memory fetches only."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from cbs import clock
from cbs.autonomy import available_tools, inner_life, reading_tools
from cbs.config import load_config
from cbs.fetch import FetchError, Page, PublicFetcher, in_domains
from cbs.llm import LLMError, RoutingLLMClient
from cbs.llm.gemini import GeminiLLMClient
from cbs.note import public_note
from cbs.reading import CATALOG_URL, ReadingTools, html, index_items
from cbs.steward import submit
from cbs.web import create_app
from tests.test_reading import catalog_zip
from shared_helpers import adapter
def run(pipeline, action='browse_shopping', **fields):
    pipeline.client.inner_life_responses.append({'action': action, 'reason': '関心から', 'topic': '香り', 'task': 'お香', **fields})
    submit(pipeline.store, inner_life, pipeline).result(timeout=10)


def shopping(pipeline, uri='https://www.amazon.co.jp/dp/test'):
    pipeline.client.grounded_responses.append({'text': '検索結果の公開説明。', 'queries': ['お香'], 'sources': [{'title': '検索出典', 'uri': uri}]})
    pipeline.client.json_responses = [{'summary': 'お香の品を見つけた。', 'impression': '香りが気になる。', 'quote': '', 'products': [{'title': '白檀のお香', 'uri': uri}]}]


def test_shopping_search_only_records_note_share_interest_and_budget(pipeline):
    def forbidden(*a, **kw):
        pytest.fail('Shopping must never fetch a page')
    pipeline.reading_tools = ReadingTools(pipeline.store, forbidden)
    shopping(pipeline)
    pipeline.client.activity_responses = [{'interests': [{'operation': 'new', 'topic': 'お香', 'kind': 'curiosity', 'detail': '香りが気になる'}]}]
    run(pipeline)
    activity = pipeline.store.list_activities('fixture')[0]
    assert activity['status'] == 'completed'
    assert activity['sources'][0]['title'] == '白檀のお香' and activity['sources'][0]['observed_at']
    assert '検索結果を見た日時' in activity['summary']
    call = next(c for c in pipeline.client.calls if c.get('grounded'))
    assert call['include_domains'] == pipeline.config.autonomy.tools.browse_shopping.domains
    assert call['messages'] == [{'role': 'user', 'content': 'お香'}]
    assert pipeline.store.list_threads('fixture')[0]['source_ref'] == activity['ref']
    assert any(i['origin'] == 'activity' for i in pipeline.store.list_interests('fixture'))
    assert public_note(pipeline)['activities'][0]['sources'][0]['uri'] == activity['sources'][0]['uri']
    assert pipeline.store.autonomy_budget('fixture')['tools']['browse_shopping']['remaining'] == 0
    run(pipeline)
    assert pipeline.store.list_inner_life('fixture')[0]['status'] == 'limited'
    assert len(pipeline.store.list_activities('fixture')) == 1
    with clock.at('2030-01-01 12:00'):
        assert pipeline.store.autonomy_budget('fixture')['tools']['browse_shopping']['remaining'] == 1


@pytest.mark.parametrize('failure', ['empty', 'outside', 'invented', 'schema'])
def test_shopping_invalid_results_fail_and_consume_budget(pipeline, failure):
    shopping(pipeline, 'https://amazon.co.jp.evil.example/product' if failure == 'outside' else 'https://makuake.com/project/test')
    if failure == 'empty':
        pipeline.client.grounded_responses[0]['queries'] = []
    if failure == 'invented':
        pipeline.client.json_responses[0]['products'][0]['uri'] = 'https://makuake.com/project/other'
    if failure == 'schema':
        pipeline.client.json_responses = [{'invalid': 'PRIVATE_SEARCH_INPUT'}]
    run(pipeline)
    assert pipeline.store.list_activities('fixture')[0]['status'] == 'failed'
    assert not pipeline.store.list_threads('fixture') and not public_note(pipeline)['activities']
    assert pipeline.store.autonomy_budget('fixture')['tools']['browse_shopping']['remaining'] == 0
    with pipeline.store.connect() as db:
        assert 'PRIVATE_SEARCH_INPUT' not in '\n'.join(db.iterdump())


def test_shopping_world_off_settings_total_budget_and_provider(pipeline):
    def allowed():
        return next(t['available'] for t in available_tools(pipeline, pipeline.store.autonomy_budget('fixture')) if t['name'] == 'browse_shopping')
    assert allowed()
    pipeline.character.world['reality'] = '別世界'
    assert not allowed()
    pipeline.character.world['reality'] = '現実どおり'
    pipeline.config.autonomy.tools.browse_shopping.enabled = False
    assert not allowed()
    pipeline.config.autonomy.tools.browse_shopping.enabled = True
    pipeline.config.autonomy.activities_daily_max = 0
    assert not allowed()
    pipeline.config.autonomy.activities_daily_max = 6
    pipeline.config.autonomy.tools.browse_shopping.domains = []
    assert not allowed()
    pipeline.config.autonomy.tools.browse_shopping.domains = ['shop.example']
    pipeline.client = RoutingLLMClient(pipeline.config)
    assert not allowed()  # no credential
    gemini = GeminiLLMClient.__new__(GeminiLLMClient)
    with pytest.raises(LLMError, match='ドメイン制限'):
        gemini.generate_grounded('test', '', [], include_domains=['shop.example'])


def test_domain_limits_reach_actual_adapter_and_router(pipeline):
    client = adapter()
    result = client.generate_grounded('test', 's', [], include_domains=['makuake.com'])
    assert client._client.options[-1]['extra_body']['tools'][0]['parameters']['allowed_domains'] == ['makuake.com']
    assert result.trace()['request']['tools'][0]['parameters']['allowed_domains'] == ['makuake.com']
    route = RoutingLLMClient(pipeline.config)
    route._clients['openrouter'] = pipeline.client
    route.generate_grounded('openrouter/test', '', [], include_domains=['makuake.com'])
    assert pipeline.client.calls[-1]['include_domains'] == ['makuake.com']
    assert in_domains('https://shop.rakuten.co.jp/product', ['rakuten.co.jp'])
    assert not in_domains('https://rakuten.co.jp.evil.example/product', ['rakuten.co.jp'])


def listing(n=5):
    return '<title>記事一覧</title><main>' + ''.join(f'<a href="/articles/{i}">長い記事の題名その{i}</a>' for i in range(n)) + '</main>'


def test_index_detection_refresh_diff_read_receipt_and_no_body_storage(pipeline):
    calls, count = [], [5]
    def fetch(url, **kw):
        calls.append(url)
        return Page(url, (listing(count[0]) if url.endswith('/list') else '<article>INDEX_ARTICLE_PRIVATE 本文を読めます。</article>').encode())
    reader = pipeline.reading_tools = ReadingTools(pipeline.store, fetch)
    with clock.at('2026-10-01 12:00'):
        source = reader.register('fixture', 'https://reader.example/list')
        assert source['kind'] == 'index'
        initial = {i['url'] for i in pipeline.store.source_candidates('fixture')}
        count[0] = 6
        assert reader.refresh('fixture', source['id'])['skipped']
        assert len(calls) == 1
    with clock.at('2026-10-02 12:00'):
        available_tools(pipeline, pipeline.store.autonomy_budget('fixture'), refresh=True)
        items = pipeline.store.source_candidates('fixture')
        new = [i for i in items if i['url'] not in initial]
        assert len(new) == 1 and new[0]['url'].endswith('/5')
        pipeline.client.json_responses = [{'summary': '新しい記事を読んだ。', 'impression': '面白かった。'}]
        run(pipeline, 'read_source', source_item_id=new[0]['id'])
        assert pipeline.store.list_activities('fixture')[0]['status'] == 'completed'
        assert len(calls) == 3  # register, next-day refresh, actual article
        assert len(pipeline.store.source_candidates('fixture')) == 5
    with clock.at('2026-10-03 12:00'):
        count[0] = 1
        reader.refresh('fixture', source['id'])
        assert pipeline.store.list_sources('fixture')[0]['kind'] == 'index'
        assert len(pipeline.store.source_candidates('fixture')) == 5
    with pipeline.store.connect() as db:
        assert 'INDEX_ARTICLE_PRIVATE' not in '\n'.join(db.iterdump())
        assert db.execute('SELECT COUNT(*) FROM reading_receipts').fetchone()[0] == 1


def test_index_shape_conservative_and_single_page_only_once(pipeline):
    base = 'https://reader.example/list'
    assert len(index_items(html(listing()), base)) == 5
    assert len(index_items(html(listing() + '<a href="/other/index_top.html">総合インデックスへ</a>'), base)) == 5
    for body in (listing(4), '<nav>'+listing()+'</nav>', listing().replace('/articles/', 'https://other.example/articles/'),
                 listing().replace('/articles/', '/'), listing().replace('/articles/', '/list#'), listing().replace('/articles/', '/list?q=')):
        assert len(index_items(html(body), base)) < 5
    calls = []
    reader = ReadingTools(pipeline.store, lambda url, **kw: (calls.append(url) or Page(url, listing(4).encode())))
    source = reader.register('fixture', base)
    assert source['kind'] == 'page'
    with clock.at('2030-01-01'):
        assert reader.refresh('fixture', source['id'])['skipped']
    assert len(calls) == 1


def test_meta_charset_decodes_legacy_index_and_article():
    page = Page('https://reader.example/list', ('<meta http-equiv="Content-Type" content="text/html; charset=Shift_JIS">' + listing()).encode('shift_jis'))
    assert '長い記事の題名' in page.text()
    assert len(index_items(html(page.text()), page.url)) == 5


def test_index_failed_refresh_keeps_candidates_and_waits(pipeline):
    reader = ReadingTools(pipeline.store, lambda url, **kw: Page(url, listing().encode()))
    with clock.at('2026-10-01'):
        source = reader.register('fixture', 'https://reader.example/list')
    calls = []
    def fail(*a, **kw):
        calls.append(a)
        raise FetchError('private-error')
    reader._fetch = fail
    with clock.at('2026-10-02'):
        assert not reader.refresh('fixture', source['id'])['ok']
        assert reader.refresh('fixture', source['id'])['skipped']
    assert len(calls) == 1 and len(pipeline.store.source_candidates('fixture')) == 5


def test_legacy_single_page_is_structurally_checked_once(pipeline):
    from cbs.store import Store
    with pipeline.store.connect() as db:
        # Recreate the old schema, then reopen to exercise the real migration.
        db.execute('ALTER TABLE sources DROP COLUMN structure_checked')
        db.execute("INSERT INTO sources (character_id,url,kind,title,checked_at) VALUES ('fixture','https://reader.example/list','page','旧ページ',?)", (clock.iso(),))
        sid = db.execute('SELECT id FROM sources').fetchone()[0]
        db.execute("INSERT INTO source_items (source_id,url,title) VALUES (?,'https://reader.example/list','旧ページ')", (sid,))
    migrated = Store(pipeline.store.path)
    reader = ReadingTools(migrated, lambda url, **kw: Page(url, listing().encode()))
    assert reader.refresh('fixture', sid)['kind'] == 'index'
    assert len(migrated.source_candidates('fixture')) == 5
    assert all(i['url'] != 'https://reader.example/list' for i in migrated.source_candidates('fixture'))


@pytest.mark.parametrize('body', [b'bad zip', b'', None])
def test_catalog_refresh_age_failure_preserves_bytes(pipeline, body):
    calls = []
    reader = ReadingTools(pipeline.store, lambda url, **kw: (calls.append(url) or Page(url, catalog_zip())))
    assert reader.catalog()
    path = reader.cache / 'catalog.csv'
    original = path.read_bytes()
    current = clock.now().timestamp()
    os.utime(path, (current - 29 * 86400, current - 29 * 86400))
    assert reader.catalog() and len(calls) == 1
    os.utime(path, (current - 30 * 86400, current - 30 * 86400))
    def fetch(url, **kw):
        calls.append(url)
        if body is None:
            raise FetchError('network unavailable')
        return Page(url, body)
    reader._fetch = fetch
    assert reader.catalog() and path.read_bytes() == original
    assert len(calls) == 2
    reader._fetch = lambda url, **kw: Page(url, catalog_zip())
    assert reader.catalog() and path.stat().st_mtime > current - 10


def test_shopping_fetch_blocked_before_dns_and_on_redirect(pipeline):
    from tests.test_reading import address
    calls = []
    def transport(url, *args):
        calls.append(url)
        return (200, {}, b'') if url.endswith('robots.txt') else (302, {'location': 'https://www.amazon.co.jp/dp/test'}, b'')
    fetch = PublicFetcher(resolver=address('8.8.8.8'), transport=transport, interval=0, blocked_domains=['amazon.co.jp'])
    with pytest.raises(FetchError, match='買い物'):
        fetch('https://www.amazon.co.jp/dp/test')
    assert calls == []
    with pytest.raises(FetchError, match='買い物'):
        fetch('https://reader.example/redirect')
    assert not any('amazon' in url for url in calls)
    pipeline.config.autonomy.tools.browse_shopping.domains = ['shop.example']
    reader = reading_tools(pipeline)
    assert reader.register('fixture', 'https://amazon.co.jp/dp/test')['error']
    assert reader.register('fixture', 'https://shop.example/product')['error']


def test_shopping_settings_validation_and_roundtrip(pipeline, tmp_path):
    with TestClient(create_app(pipeline, settings_dir=tmp_path)) as web:
        result = web.put('/api/settings', json={'autonomy': {'tools': {'browse_shopping': {'enabled': False, 'daily_max': 2, 'domains': ['shop.example']}}}})
        assert result.status_code == 200
        for domains in (['https://amazon.co.jp'], ['amazon.co.jp/path'], ['*.example.com'], ['localhost']):
            assert web.put('/api/settings', json={'autonomy': {'tools': {'browse_shopping': {'domains': domains}}}}).status_code == 422
    cfg = load_config(data_dir=tmp_path)
    assert cfg.autonomy.tools.browse_shopping.domains == ['shop.example']
    assert cfg.autonomy.tools.browse_shopping.daily_max == 2 and not cfg.autonomy.tools.browse_shopping.enabled


def test_shopping_sources_drop_affiliate_and_tracking_parameters():
    from cbs.autonomy import without_tracking
    assert without_tracking("https://www.amazon.co.jp/dp/B09JYVSLGX?tag=someone-22&th=1") == "https://www.amazon.co.jp/dp/B09JYVSLGX?th=1"
    assert without_tracking("https://item.rakuten.co.jp/shop/item/?utm_source=x&scid=af_pc") == "https://item.rakuten.co.jp/shop/item/"
