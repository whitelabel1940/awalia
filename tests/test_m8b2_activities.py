import json
import socket

import pytest
from fastapi.testclient import TestClient

from cbs import clock
from cbs.autonomy import available_tools, inner_life
from cbs.fetch import FetchError, Page, PublicFetcher
from cbs.llm import FakeLLMClient
from cbs.media import video_metadata, youtube_url, video_candidates, wikipedia_search
from cbs.note import public_note
from cbs.reading import ReadingTools
from cbs.steward import submit
from cbs.web import create_app


VIDEO = 'https://www.youtube.com/watch?v=jNQXAC9IVRw'
BODY = 'PRIVATE_MATERIAL_BODY 独自の記事本文を入力だけに使います。'


def install(pipeline, seconds=19, status='OK', live=False):
    calls = []
    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if 'youtube.com/watch' in url:
            data = {'videoDetails': {'title': '動物園の動画', 'lengthSeconds': str(seconds), 'isLiveContent': live},
                    'playabilityStatus': {'status': status}}
            return Page(url, ('<script>var ytInitialPlayerResponse = ' + json.dumps(data) + ';</script>VIDEO_BODY_NOT_STORED').encode())
        if 'search/page?' in url:
            return Page(url, json.dumps({'pages': [{'id': 12, 'key': '白檀', 'title': '白檀'}]}).encode())
        if '/with_html' in url:
            return Page(url, json.dumps({'id': 12, 'key': '白檀', 'title': '白檀', 'html': '<main>'+BODY+'</main>'}).encode())
        if 'feeds/videos.xml' in url:
            return Page(url, f'<feed><title>チャンネル</title><entry><title>動画</title><link href="{VIDEO}"/></entry></feed>'.encode())
        return Page(url, ('<title>現実の記事</title><main>'+BODY+'</main>').encode())
    pipeline.reading_tools = ReadingTools(pipeline.store, fetch)
    return pipeline.reading_tools, calls


def run(pipeline, action, **fields):
    pipeline.client.inner_life_responses.append({'action': action, 'reason': '関心に沿う', 'topic': '新しい見聞', **fields})
    submit(pipeline.store, inner_life, pipeline).result(timeout=10)


def tools(pipeline):
    return {t['name']: t for t in available_tools(pipeline, pipeline.store.autonomy_budget('fixture'))}


def dump(pipeline):
    with pipeline.store.connect() as db:
        return '\n'.join(db.iterdump())


@pytest.mark.parametrize('url', ['https://youtube.com.evil.org/watch?v=jNQXAC9IVRw',
    'https://user:pass@youtube.com/watch?v=jNQXAC9IVRw', 'file:///video',
    'https://youtube.com/watch?v=bad', 'https://youtu.be/jNQXAC9IVRw/extra',
    'https://127.0.0.1/watch?v=jNQXAC9IVRw', 'https://youtube.com:999/watch?v=jNQXAC9IVRw',
    'https://youtube.com:bad/watch?v=jNQXAC9IVRw', 'https://[broken/watch?v=jNQXAC9IVRw'])
def test_video_url_rejects_untrusted_targets(url):
    assert youtube_url(url) is None


def test_video_discovery_feed_search_duration_budget_and_destinations(pipeline):
    reader, calls = install(pipeline)
    reader.register('fixture', 'https://www.youtube.com/feeds/videos.xml?channel_id=abc')
    assert tools(pipeline)['watch_video']['available']
    assert not tools(pipeline)['read_source']['available']
    pipeline.client.json_responses = [{'understood': True, 'summary': '象の前で話す短い動画。', 'impression': '鼻の長さに注目するところが楽しい。'}]
    pipeline.client.activity_responses = [{'interests': [{'operation': 'new', 'topic': '象', 'kind': 'curiosity', 'detail': '象の鼻の使い方を知りたい'}]}]
    with clock.at('2026-10-16 12:00'):
        run(pipeline, 'watch_video', video_url=VIDEO)
        a = pipeline.store.list_activities('fixture')[0]
        assert a['status'] == 'completed' and a['sources'] == [{'title': '動物園の動画', 'uri': VIDEO}]
        assert not tools(pipeline)['watch_video']['available']
        assert pipeline.store.autonomy_budget('fixture')['tools']['watch_video']['remaining'] == 0
        assert not video_candidates(pipeline.store, 'fixture')
        assert not pipeline.store.source_candidates('fixture')
        assert public_note(pipeline)['activities'] and pipeline.store.list_threads('fixture')[0]['kind'] == 'share'
        assert any(i['origin'] == 'activity' for i in pipeline.store.list_interests('fixture'))
        assert 'VIDEO_BODY_NOT_STORED' not in dump(pipeline)
    with clock.at('2026-10-17 12:00'):
        assert pipeline.store.autonomy_budget('fixture')['tools']['watch_video']['remaining'] == 1
    assert any('/watch?' in c[0] and c[1]['max_bytes'] == 4_000_000 for c in calls)


@pytest.mark.parametrize('seconds,status,live', [(1201, 'OK', False), (0, 'OK', False), (19, 'ERROR', False), (19, 'OK', True)])
def test_video_rejects_long_unknown_unavailable_and_live(pipeline, seconds, status, live):
    reader, _ = install(pipeline, seconds, status, live)
    reader.register('fixture', 'https://www.youtube.com/feeds/videos.xml?channel_id=abc')
    run(pipeline, 'watch_video', video_url=VIDEO)
    assert pipeline.store.list_activities('fixture')[0]['status'] == 'failed'
    assert not any('jNQXAC9IVRw' == c.get('messages', [{}])[0].get('content') for c in pipeline.client.calls)
    assert not public_note(pipeline)['activities'] and pipeline.store.source_candidates('fixture')
    assert pipeline.store.autonomy_budget('fixture')['tools']['watch_video']['remaining'] == 0


def test_video_missing_metadata_and_input_method_and_unselected_url(pipeline):
    reader, _ = install(pipeline)
    reader.register('fixture', 'https://www.youtube.com/feeds/videos.xml?channel_id=abc')
    pipeline.config.autonomy.tools.watch_video.model = 'local/no-video'
    assert 'watch_video' not in tools(pipeline)
    run(pipeline, 'watch_video', video_url=VIDEO)
    assert not pipeline.store.list_activities('fixture') and pipeline.store.list_inner_life('fixture')[0]['status'] == 'limited'
    pipeline.config.autonomy.tools.watch_video.model = 'openrouter/google/gemini-3.8-flash'
    run(pipeline, 'watch_video', video_url='https://www.youtube.com/watch?v=abcdefghijk')
    assert pipeline.store.list_activities('fixture')[0]['status'] == 'failed'
    reader.fetch = lambda url, **kw: Page(url, b'<main>No metadata</main>')
    with pytest.raises(FetchError):
        video_metadata(reader.fetch, VIDEO, 20)


def test_video_research_candidates_and_no_method_without_key(pipeline):
    install(pipeline)
    pipeline.client.grounded_responses = [{'text': '動画を見つけた。', 'queries': ['動画'], 'sources': [{'title': '元動画', 'uri': VIDEO}]}]
    run(pipeline, 'research', task='元動画を探す')
    assert tools(pipeline)['watch_video']['candidates'][0]['uri'] == VIDEO
    from cbs.llm import RoutingLLMClient
    pipeline.client = RoutingLLMClient(pipeline.config)
    assert 'watch_video' not in tools(pipeline)


def test_video_provider_request_and_private_failure_trace():
    from types import SimpleNamespace
    from cbs.autonomy import VideoImpression
    from cbs.llm import LLMError
    from cbs.llm.openai_client import OpenAILLMClient

    class VideoTransport(FakeLLMClient):
        def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(model='google/gemini-3.8-flash', provider='Google AI Studio', usage=None,
                choices=[SimpleNamespace(message=SimpleNamespace(content=self.text_responses.pop(0)))])

    client = OpenAILLMClient.__new__(OpenAILLMClient)
    client._zdr = False
    client.name, client._api, client._key = 'openrouter', 'chat', 'synthetic-secret'
    transport = VideoTransport(text_responses=[json.dumps({'understood': True, 'summary': '象を観た。',
        'impression': 'synthetic-secret 楽しい。'}), 'INVALID_VIDEO_BODY'])
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=transport))
    result = client.generate_video_json('google/gemini-3.8-flash', '指示', VIDEO, VideoImpression)
    request = transport.calls[0]
    assert request['messages'][1]['content'] == [{'type': 'video_url', 'video_url': {'url': VIDEO}}]
    assert request['extra_body']['provider'] == {'only': ['Google AI Studio'], 'allow_fallbacks': False}
    assert request['response_format']['json_schema']['strict']
    assert 'synthetic-secret' not in result.text and VIDEO not in json.dumps(result.trace())
    with pytest.raises(LLMError) as invalid:
        client.generate_video_json('google/gemini-3.8-flash', '指示', VIDEO, VideoImpression)
    assert 'INVALID_VIDEO_BODY' not in str(invalid.value)
    # ER1 retains only safe failure metadata, never the video input/body.
    assert invalid.value.trace == {'error_kind': 'unknown', 'status_code': None, 'provider': 'openrouter'}


@pytest.mark.parametrize('response', [{'understood': False, 'summary': '参照不可', 'impression': '感想なし'}, {'wrong': 'schema'}])
def test_video_failed_understanding_does_not_claim_watch(pipeline, response):
    reader, _ = install(pipeline)
    reader.register('fixture', 'https://www.youtube.com/feeds/videos.xml?channel_id=abc')
    pipeline.client.json_responses = [response]
    run(pipeline, 'watch_video', video_url=VIDEO)
    assert pipeline.store.list_activities('fixture')[0]['status'] == 'failed'
    assert not public_note(pipeline)['activities'] and not pipeline.store.list_threads('fixture')
    assert 'VIDEO_BODY_NOT_STORED' not in dump(pipeline)


def world_reply(content='港の灯台は青い魔石で灯る。'):
    return {'summary': '港の灯台について読んだ。', 'impression': '灯りが道を示すのが好き。', 'source_title': '港の便り', 'facts': [content]}


def test_world_adaptation_generation_persistence_packet_check_and_private_sources(pipeline):
    reader, _ = install(pipeline)
    reader.register('fixture', 'https://real.example/article')
    item = pipeline.store.source_candidates('fixture')[0]
    assert not tools(pipeline)['world_activity']['available']
    pipeline.character.world['reality'] = '魔法の世界'
    assert tools(pipeline)['world_activity']['available']
    assert not tools(pipeline)['read_source']['available']
    assert not tools(pipeline)['read_wikipedia']['available']
    pipeline.client.json_responses = [
        {'mode': 'adaptation', 'reason': '現実に近い港町だから', 'task': '便りを読む', 'source_item_id': item['id']}, world_reply(),
        {'mode': 'generation', 'reason': '魔法の世界の噂を作る', 'task': '噂を聞く'}, world_reply('市場では光る貝の噂がある。')]
    run(pipeline, 'world_activity')
    run(pipeline, 'world_activity')
    facts = pipeline.store.list_world_facts('fixture')
    assert len(facts) == 2 and facts[0]['activity_id'] != facts[1]['activity_id'] and facts[0]['created_at']
    assert not pipeline.store.list_world_facts('other')
    assert len(public_note(pipeline)['activities']) == 2
    assert all(n['sources'] == [{'title': '港の便り'}] for n in public_note(pipeline)['activities'])
    activities = pipeline.store.list_activities('fixture')
    assert all(a['topic'] == '港の便り' for a in activities)
    assert any(s.get('internal') and s['uri'] == item['url'] for a in activities for s in a['sources'])
    assert BODY not in dump(pipeline)
    compose = [json.loads(c['messages'][0]['content']) for c in pipeline.client.calls if c.get('schema', {}).get('title') == 'WorldActivity']
    assert compose[1]['world_facts'][0]['content'] == facts[0]['content']
    pipeline.client = FakeLLMClient(text_responses=['灯台は赤い炎だよ。', '青い魔石の灯りが好き。'], json_responses=[{},
        {'problems': [{'text': '赤い炎', 'verdict': 'contradicted', 'reason': 'world_factsと矛盾'}]}, {'problems': []}])
    result = pipeline.run(pipeline.store.create_session('fixture')['id'], '港のことを教えて')
    pipeline.wait_for_steward()
    assert result['assistant_text'] == '青い魔石の灯りが好き。'
    for call in pipeline.client.calls:
        if call.get('schema', {}).get('title') == 'Check':
            packet = json.loads(call['messages'][0]['content'])['packet']
            assert packet['world_facts'] == facts
            assert 'https://real.example' not in json.dumps(packet)
            assert 'world_facts' in call['system']


@pytest.mark.parametrize('bad', ['missing_material', 'raw_url', 'copy', 'invalid'])
def test_world_invalid_material_or_output_fails_without_fact_or_body_trace(pipeline, bad):
    reader, _ = install(pipeline)
    reader.register('fixture', 'https://real.example/article')
    pipeline.character.world['reality'] = '幻想世界'
    item = pipeline.store.source_candidates('fixture')[0]
    reply = world_reply('https://real.example/raw' if bad == 'raw_url' else BODY if bad == 'copy' else '灯台は青い。')
    if bad == 'invalid':
        reply = {'wrong': 'schema'}
    pipeline.client.json_responses = [{'mode': 'adaptation', 'reason': '翻案', 'task': '読む',
        'source_item_id': 999 if bad == 'missing_material' else item['id']}, reply]
    run(pipeline, 'world_activity')
    assert pipeline.store.list_activities('fixture')[0]['status'] == 'failed'
    assert not pipeline.store.list_world_facts('fixture') and not public_note(pipeline)['activities']
    assert pipeline.store.source_candidates('fixture') and BODY not in dump(pipeline)


def test_wikipedia_conditions_keyless_fetch_and_recording(pipeline):
    reader, calls = install(pipeline)
    assert not tools(pipeline)['read_wikipedia']['available']
    pipeline.config.autonomy.tools.research.daily_max = 0
    assert tools(pipeline)['read_wikipedia']['available']
    pipeline.client.json_responses = [{'page_id': 12}, {'summary': '香木について読んだ。', 'impression': '甘い香りに関心がある。'}]
    run(pipeline, 'read_wikipedia', task='白檀')
    activity = pipeline.store.list_activities('fixture')[0]
    assert activity['status'] == 'completed'
    assert activity['sources'][0]['title'] == '白檀' and activity['sources'][0]['uri'].startswith('https://ja.wikipedia.org/wiki/')
    assert len(calls) == 2 and all(c[1]['max_bytes'] == 2_000_000 for c in calls)
    assert not pipeline.client.grounded_responses and BODY not in dump(pipeline)
    assert public_note(pipeline)['activities'] and pipeline.store.list_threads('fixture')
    # An actual keyless configuration can offer Wikipedia when the steward runs locally.
    from cbs.llm import RoutingLLMClient
    pipeline.client = RoutingLLMClient(pipeline.config)
    pipeline.config.models.steward = 'local/test'
    assert tools(pipeline)['read_wikipedia']['available']


@pytest.mark.parametrize('failure', ['choice', 'copy', 'schema', 'robots', 'empty'])
def test_wikipedia_failures_consume_budget_but_never_persist_body(pipeline, failure):
    reader, _ = install(pipeline)
    pipeline.config.autonomy.research = False
    if failure == 'robots':
        reader.fetch = PublicFetcher(resolver=lambda h, p, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', p))],
            transport=lambda *a: (200, {}, b'User-agent: *\nDisallow: /'), interval=0)
    if failure == 'empty':
        reader.fetch = lambda url, **kw: Page(url, b'{"pages":[]}')
    pipeline.client.json_responses = [{'page_id': 999 if failure == 'choice' else 12},
        {'invalid': True} if failure == 'schema' else {'summary': BODY if failure == 'copy' else 'まとめ', 'impression': '感想'}]
    run(pipeline, 'read_wikipedia', task='白檀')
    assert pipeline.store.list_activities('fixture')[0]['status'] == 'failed'
    assert not public_note(pipeline)['activities'] and BODY not in dump(pipeline)
    assert pipeline.store.autonomy_budget('fixture')['tools']['read_wikipedia']['remaining'] == 2


def test_new_tool_settings_validation_and_reload(pipeline, tmp_path):
    with TestClient(create_app(pipeline, settings_dir=tmp_path)) as web:
        body = {'autonomy': {'tools': {'watch_video': {'enabled': False, 'daily_max': 2, 'max_minutes': 10},
                                      'world_activity': {'daily_max': 4}, 'read_wikipedia': {'daily_max': 5}}}}
        assert web.put('/api/settings', json=body).status_code == 200
        assert web.put('/api/settings', json={'autonomy': {'tools': {'watch_video': {'max_minutes': 0}}}}).status_code == 422
    from cbs.config import load_config
    cfg = load_config(data_dir=tmp_path)
    assert not cfg.autonomy.tools.watch_video.enabled and cfg.autonomy.tools.watch_video.max_minutes == 10
    assert cfg.autonomy.tools.world_activity.daily_max == 4 and cfg.autonomy.tools.read_wikipedia.daily_max == 5


@pytest.mark.parametrize('grounded', [True, False])
def test_world_research_material_reserves_search_budget_and_stays_private(pipeline, grounded):
    install(pipeline)
    pipeline.character.world['reality'] = '現代魔法の世界'
    pipeline.config.autonomy.tools.research.daily_max = 1
    pipeline.client.json_responses = [{'mode': 'adaptation', 'reason': '近い世界なので公開情報を翻案',
        'task': '灯台の仕組み', 'material_kind': 'research'}, world_reply()]
    pipeline.client.grounded_responses = [{'text': BODY, 'queries': ['実際の検索語'],
        'sources': [{'title': '現実の研究資料', 'uri': 'https://real.example/research'}] if grounded else []}]
    before = pipeline.store.autonomy_budget('fixture')['activities_remaining']
    run(pipeline, 'world_activity')
    a = pipeline.store.list_activities('fixture')[0]
    assert a['status'] == ('completed' if grounded else 'failed')
    assert pipeline.store.autonomy_budget('fixture')['tools']['research']['remaining'] == 0
    assert pipeline.store.autonomy_budget('fixture')['activities_remaining'] == before-1
    assert not tools(pipeline)['world_activity']['research_available']
    assert BODY not in dump(pipeline)
    assert 'https://real.example' not in json.dumps(pipeline.store.conversation_activities('fixture'))
    if grounded:
        assert a['queries'] == ['実際の検索語'] and a['sources'][1]['internal']
        assert pipeline.store.list_world_facts('fixture')


def test_world_search_cannot_bypass_disabled_budget(pipeline):
    install(pipeline)
    pipeline.character.world['reality'] = '魔法の世界'
    pipeline.config.autonomy.tools.research.enabled = False
    pipeline.client.json_responses = [{'mode': 'adaptation', 'reason': '試す', 'task': '灯台', 'material_kind': 'research'}]
    run(pipeline, 'world_activity')
    assert pipeline.store.list_activities('fixture')[0]['status'] == 'failed'
    assert not any(c.get('grounded') for c in pipeline.client.calls)


@pytest.mark.parametrize('failed_fetch', [False, True])
def test_world_refresh_and_nested_source_read_budget(pipeline, failed_fetch):
    reader, _ = install(pipeline)
    reader.register('fixture', 'https://real.example/article')
    item = pipeline.store.source_candidates('fixture')[0]
    pipeline.character.world['reality'] = '魔法の港町'
    pipeline.config.autonomy.tools.read_source.daily_max = 1
    refreshed = []
    reader.refresh = lambda cid, sid: refreshed.append((cid, sid))
    available_tools(pipeline, pipeline.store.autonomy_budget('fixture'), refresh=True)
    assert refreshed == [('fixture', item['source_id'])]
    if failed_fetch:
        def fail(url, **kwargs):
            raise FetchError('取得失敗')
        reader.fetch = fail
    pipeline.client.json_responses = [
        {'mode': 'adaptation', 'reason': '翻案', 'task': '便りを読む', 'source_item_id': item['id']}, world_reply()]
    before = pipeline.store.autonomy_budget('fixture')['activities_remaining']
    run(pipeline, 'world_activity')
    assert pipeline.store.list_activities('fixture')[0]['status'] == ('failed' if failed_fetch else 'completed')
    assert pipeline.store.autonomy_budget('fixture')['tools']['read_source']['remaining'] == 0
    assert pipeline.store.autonomy_budget('fixture')['activities_remaining'] == before-1
    assert tools(pipeline)['world_activity']['available']
    assert tools(pipeline)['world_activity']['candidates'] == []
    refreshed.clear()
    available_tools(pipeline, pipeline.store.autonomy_budget('fixture'), refresh=True)
    assert not refreshed


def test_world_and_wikipedia_tool_and_total_limits(pipeline):
    install(pipeline)
    pipeline.config.autonomy.tools.read_wikipedia.enabled = False
    pipeline.config.autonomy.research = False
    run(pipeline, 'read_wikipedia', task='白檀')
    assert pipeline.store.list_inner_life('fixture')[0]['status'] == 'limited'
    pipeline.character.world['reality'] = '魔法の世界'
    pipeline.config.autonomy.tools.world_activity.daily_max = 0
    run(pipeline, 'world_activity')
    assert pipeline.store.list_inner_life('fixture')[0]['status'] == 'limited'
    pipeline.config.autonomy.tools.world_activity.daily_max = 2
    pipeline.config.autonomy.activities_daily_max = 0
    run(pipeline, 'world_activity')
    assert pipeline.store.list_inner_life('fixture')[0]['status'] == 'limited' and not pipeline.store.list_activities('fixture')
