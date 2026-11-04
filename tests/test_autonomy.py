from cbs.config import ROOT
import json
import pytest
from fastapi.testclient import TestClient

from cbs import clock, probe
from cbs.autonomy import catch_up, inner_life, quiet_hours
from cbs.llm import FakeLLMClient, LLMError, RoutingLLMClient
from cbs.pipeline import PreJudge, assemble_packet
from cbs.steward import submit
from cbs.web import create_app


def execute(pipeline, action='research', **fields):
    pipeline.client.inner_life_responses.append({'action': action, 'reason': '関心がある', 'topic': 'お香', 'task': '白檀の香り', **fields})
    submit(pipeline.store, inner_life, pipeline).result(timeout=5)


def use(ref):
    return {'ref': ref, 'relevant_now': 'yes', 'counterevidence': 'no', 'continuation': 'yes'}


@pytest.mark.parametrize('reality,enabled,allowed', [
    ('現実どおり', True, True), ('現実どおり', False, False), ('ファンタジー', True, False), ('少し不思議', True, False), ('SF', True, False)])
def test_world_and_research_switch_enforced(pipeline, reality, enabled, allowed):
    pipeline.character.world['reality'] = reality
    pipeline.config.autonomy.research = enabled
    with clock.at('2026-10-03 12:00'):
        execute(pipeline)
    calls = [c for c in pipeline.client.calls if c.get('grounded')]
    assert bool(calls) == allowed
    assert bool(pipeline.store.list_activities('fixture')) == allowed
    assert pipeline.store.list_inner_life('fixture')[0]['status'] == ('completed' if allowed else 'limited')


def test_budgets_and_day_reset(pipeline):
    with clock.at('2026-10-03 12:00'):
        for _ in range(4):
            execute(pipeline)
        assert len(pipeline.store.list_activities('fixture')) == 3
        pipeline.client.text_responses = ['考えをまとめた', '話す内容を整えた', 'さらに考えた']
        execute(pipeline, 'reflect')
        execute(pipeline, 'prepare_share')
        execute(pipeline, 'reflect')
        assert len(pipeline.store.list_activities('fixture')) == 6
        assert pipeline.store.autonomy_budget('fixture')['activities_remaining'] == 0
        assert pipeline.store.autonomy_budget('fixture')['research_remaining'] == 0
        assert all(not a['sources'] for a in pipeline.store.list_activities('fixture') if a['kind'] != 'research')
    with clock.at('2026-10-04 12:00'):
        execute(pipeline)
        assert len(pipeline.store.list_activities('fixture')) == 7


def test_different_world_cannot_search_even_if_realistic(pipeline):
    pipeline.character.world.update(reality='現実どおり', contact='C')
    execute(pipeline)
    assert not any(c.get('grounded') for c in pipeline.client.calls)
    assert pipeline.store.list_inner_life('fixture')[0]['status'] == 'limited'


def test_research_execution_and_activity_interest_evidence(pipeline):
    pipeline.client.activity_responses = [{'interests': [{'operation': 'new', 'topic': '白檀', 'kind': 'curiosity', 'detail': '落ち着く香りをもっと知りたい'}]}]
    with clock.at('2026-10-03 12:00'):
        execute(pipeline)
    a = pipeline.store.list_activities('fixture')[0]
    assert a['queries'] == ['白檀 香り'] and a['sources'][0]['uri'].startswith('https:')
    interest = next(i for i in pipeline.store.list_interests('fixture') if i['origin'] == 'activity')
    assert interest['status'] == 'tentative'
    assert interest['evidence'][0]['source_activity'] == a['id']
    assert interest['evidence'][0]['source_turn'] is None
    assert pipeline.store.list_threads('fixture')[0]['source_ref'] == a['ref']


def test_failed_search_consumes_budget_and_never_supports_claim(pipeline):
    pipeline.client.grounded_responses = [{'text': '調べたつもり', 'queries': [], 'sources': []}]
    with clock.at('2026-10-03 12:00'):
        execute(pipeline)
        assert pipeline.store.autonomy_budget('fixture')['research_remaining'] == 2
    assert pipeline.store.list_activities('fixture')[0]['status'] == 'failed'
    assert pipeline.store.list_activities('fixture', recent=True) == []
    assert pipeline.store.list_threads('fixture') == []


def test_thread_expiry_due_date_packet_limit_and_used(pipeline):
    store = pipeline.store
    with clock.at('2026-10-03 12:00'):
        store.add_thread('fixture', 'follow_up', '面接', '結果を聞きたい', 'fact:1', due_at='2026-10-06 00:00')
        for n in range(4):
            store.add_thread('fixture', 'share', str(n), '伝えたい', f'activity:{n}')
        candidates = store.list_threads('fixture')
        judge = PreJudge(memory_use=[use(t['ref']) for t in candidates])
        packet = assemble_packet(pipeline.character, judge, candidate_memories=candidates)
        assert len(packet['threads']) == 3
        assert all(t['kind'] != 'follow_up' for t in packet['threads'])
        # Even counterevidence must not expose a premature follow_up.
        judge = PreJudge(memory_use=[{**use('thread:1'), 'counterevidence': 'yes'}])
        packet = assemble_packet(pipeline.character, judge, candidate_memories=candidates)
        assert not packet['counterevidence_memories']
    with clock.at('2026-10-06 12:00'):
        assert any(t['kind'] == 'follow_up' for t in store.list_threads('fixture', eligible_only=True))
        store.expire_threads('fixture', 14)
    with clock.at('2026-10-18 12:00'):
        store.expire_threads('fixture', 14)
        states = {t['id']: t for t in store.list_threads('fixture', with_history=True)}
        assert states[1]['status'] == 'open'
        assert all(t['status'] == 'expired' for n, t in states.items() if n != 1)
    with clock.at('2026-10-21 12:00'):
        store.expire_threads('fixture', 14)
        assert all(t['status'] == 'expired' for t in store.list_threads('fixture'))
        assert states[2]['history'][0]['operation'] == 'new'


def test_extract_dated_event_and_thread_use(pipeline):
    with clock.at('2026-10-03 12:00'):
        sid = pipeline.store.create_session('fixture')['id']
        pipeline.client.extract_responses = [{'ledger_updates': [], 'new_canon': [], 'disclosures': [], 'used_memories': [],
            'new_threads': [{'topic': '健康診断', 'detail': '結果を尋ねたい', 'event_date': '2026-10-09'}]}]
        pipeline.run(sid, '金曜は健康診断')
        pipeline.wait_for_steward()
        assert pipeline.store.list_threads('fixture')[0]['due_at'].startswith('2026-10-10')
    with clock.at('2026-10-12 12:00'):
        pipeline.client = FakeLLMClient(json_responses=[{'memory_use': [use('thread:1')]}, {'problems': []}],
            extract_responses=[{'ledger_updates': [], 'new_canon': [], 'disclosures': [], 'used_memories': [], 'used_threads': ['thread:1', 'thread:999']}])
        pipeline.run(sid, 'ただいま')
        pipeline.wait_for_steward()
        assert pipeline.store.list_threads('fixture')[0]['status'] == 'used'


def test_plan_followup_cancellation_and_reschedule(pipeline):
    sid = pipeline.store.create_session('fixture')['id']
    plan = pipeline.store.create_plan(sid, 'カフェ', scheduled_date='2026-10-17')
    pipeline.store.sync_plan_threads('fixture')
    assert pipeline.store.list_threads('fixture')[0]['kind'] == 'plan_followup'
    pipeline.store.add_plan_event(sid, 'reschedule', plan_id=plan['plan_id'], scheduled_date='2026-10-18')
    pipeline.store.sync_plan_threads('fixture')
    assert pipeline.store.list_threads('fixture')[0]['due_at'] == '2026-10-18'
    pipeline.store.add_plan_event(sid, 'cancel', plan_id=plan['plan_id'])
    pipeline.store.sync_plan_threads('fixture')
    assert pipeline.store.list_threads('fixture')[0]['status'] == 'dropped'


@pytest.mark.parametrize('speak', [False, True])
def test_opening_once_including_none(pipeline, speak):
    with clock.at('2026-10-03 12:00'):
        sid = pipeline.store.create_session('fixture')['id']
        pipeline.client.proactive_responses = [{'speak': speak, 'reason': '自然な一言'}]
        pipeline.client.json_responses = [{'problems': []}]
        first = pipeline.proactive(sid)
        pipeline.wait_for_steward()
        second = pipeline.proactive(sid)
        assert first['speak'] == speak and not second['speak']
        assert len(pipeline.store.list_proactive('fixture')) == 1
        if speak:
            turn = pipeline.store.history(sid)[0]
            assert turn['user_text'] == '' and turn['kind'] == 'proactive'
        # A fresh session is still suppressed when the last conversation is recent.
        if speak:
            new_sid = pipeline.store.create_session('fixture')['id']
            assert not pipeline.proactive(new_sid)['speak']


def test_opening_thread_selection_and_no_unsupported_refs(pipeline):
    with clock.at('2026-10-03 12:00'):
        sid = pipeline.store.create_session('fixture')['id']
        pipeline.store.add_thread('fixture', 'follow_up', '面接', '結果', 'fact:1', due_at='2026-10-10')
        pipeline.client.proactive_responses = [{'speak': True, 'reason': '聞こう', 'thread_ref': 'thread:1'}]
        assert not pipeline.proactive(sid)['speak']
        assert not pipeline.store.history(sid)


def test_opening_uses_previous_session_context(pipeline):
    with clock.at('2026-10-03 21:00'):
        previous = pipeline.store.create_session('fixture')['id']
        pipeline.store.add_turn(previous, '眠いから寝るね', 'おやすみ', {})
    with clock.at('2026-10-04 08:30'):
        current = pipeline.store.create_session('fixture')['id']
        assert not pipeline.proactive(current)['speak']
    call = next(c for c in pipeline.client.calls if c.get('schema', {}).get('title') == 'Proactive')
    data = json.loads(call['messages'][0]['content'])
    assert data['recent_turns'][0]['user_text'] == '眠いから寝るね'


def test_opening_does_not_embed_empty_user_text(pipeline):
    with pipeline.store.connect() as db:
        db.execute('INSERT INTO user_facts (character_id, content, learned_at, last_confirmed_at, importance, pinned) VALUES (?, ?, ?, ?, ?, ?)',
                   ('fixture', '面接の予定', clock.iso(), clock.iso(), .7, False))
    pipeline.client = FakeLLMClient(proactive_responses=[{'speak': True, 'reason': '挨拶'}], json_responses=[{'problems': []}])
    result = pipeline.proactive(pipeline.store.create_session('fixture')['id'])
    pipeline.wait_for_steward()
    assert result['speak']
    assert all(text.strip() for call in pipeline.client.embedding_calls for text in call['texts'])
    assert not pipeline.store.get_trace(result['turn_id'])['Load'].get('warnings')


def test_episodizer_dated_threads_and_explicit_matching(pipeline):
    with clock.at('2026-10-03 12:00'):
        sid = pipeline.store.create_session('fixture')['id']
        result = pipeline.run(sid, '10月9日は健康診断と面接がある')
        pipeline.wait_for_steward()
        pipeline.store.add_thread('fixture', 'follow_up', '健康診断', '結果', 'turn:1:health', due_at='2026-10-10T00:00:00+09:00', source_turn=result['turn_id'])
        fact = {'operation': 'new', 'content': '健康診断', 'importance': .7, 'pinned': False,
                'event_date': '2026-10-09', 'learned_turn': result['turn_id'], 'follow_up': True, 'follow_up_thread': 1}
        pipeline.client.episodizer_responses = [{'episodes': [{'summary': '予定', 'details': 'ユーザーの予定', 'importance': .5, 'pinned': False}],
            'user_facts': [fact, {**fact, 'content': '面接', 'follow_up_thread': None},
                           {**fact, 'content': '共同予定', 'follow_up': False, 'follow_up_thread': None}]}]
        pipeline.close_conversation(sid)
        pipeline.wait_for_steward()
        threads = pipeline.store.list_threads('fixture')
        assert len(threads) == 2
        assert all(t['source_ref'].startswith('fact:') for t in threads)
        assert {t['topic'] for t in threads} == {'健康診断', '面接'}


def test_idle_default_off_quiet_hours_once_and_daily_limit(pipeline):
    cfg = pipeline.config.autonomy
    cfg.opening = 'reserved'  # 1日2回・90分（頻度ごとの上限は idle_pace）
    assert not cfg.idle_nudge
    for hour in [0, 3, 7]:
        assert quiet_hours(hour, cfg)
    assert not quiet_hours(8, cfg)
    cfg.quiet_start_hour, cfg.quiet_end_hour = 22, 8
    assert quiet_hours(23, cfg) and quiet_hours(1, cfg) and not quiet_hours(12, cfg)
    cfg.quiet_start_hour, cfg.quiet_end_hour = 0, 8
    with clock.at('2026-10-03 05:00'):
        sid = pipeline.store.create_session('fixture')['id']
        pipeline.store.add_turn(sid, 'おはよう', 'おはよう', {})
        pipeline.store.add_thread('fixture', 'share', 'お香', '分かったこと', 'activity:1', weight=.8)
        pipeline.proactive(sid)
    with clock.at('2026-10-03 07:00'):
        assert not pipeline.proactive(sid, 'idle')['speak']
        cfg.idle_nudge = True
        assert not pipeline.proactive(sid, 'idle')['speak']
        assert not pipeline.store.list_proactive('fixture')
    for hour in [10, 13, 16]:
        with clock.at(f'2026-10-03 {hour-2:02}:00'):
            pipeline.store.add_turn(sid, '話そう', 'うん', {})
        with clock.at(f'2026-10-03 {hour:02}:00'):
            pipeline.client = FakeLLMClient(json_responses=[{'problems': []}],
                proactive_responses=[{'speak': True, 'reason': '伝えたい', 'thread_ref': 'thread:1'}])
            result = pipeline.proactive(sid, 'idle')
            pipeline.wait_for_steward()
            assert result['speak'] == (hour < 16)
            assert not pipeline.proactive(sid, 'idle')['speak']
    with clock.at('2026-10-03 16:00'):
        assert pipeline.store.autonomy_budget('fixture')['idle_remaining'] == 0


def test_idle_pace_follows_frequency_setting(pipeline):
    cfg = pipeline.config.autonomy
    assert cfg.opening == 'normal' and cfg.idle_rule() == (30, 4)
    cfg.opening = 'many'
    assert cfg.idle_rule() == (20, 6)
    assert pipeline.store.autonomy_budget('fixture')['idle_daily_max'] == 6
    cfg.opening = 'off'
    assert cfg.idle_rule() is None
    assert pipeline.store.autonomy_budget('fixture')['idle_remaining'] == 0
    cfg.opening, cfg.idle_nudge = 'many', True
    with clock.at('2026-10-03 12:00'):
        sid = pipeline.store.create_session('fixture')['id']
        pipeline.store.add_thread('fixture', 'share', 'お香', '分かったこと', 'activity:1', weight=.8)
        pipeline.proactive(sid)
        pipeline.store.add_turn(sid, '話そう', 'うん', {})
    with clock.at('2026-10-03 12:15'):
        assert not pipeline.proactive(sid, 'idle')['speak']
    with clock.at('2026-10-03 12:21'):
        pipeline.client = FakeLLMClient(json_responses=[{'problems': []}],
            proactive_responses=[{'speak': True, 'reason': '伝えたい', 'thread_ref': 'thread:1'}])
        assert pipeline.proactive(sid, 'idle')['speak']


def test_idle_waits_after_opening_even_with_old_conversation(pipeline):
    pipeline.config.autonomy.idle_nudge = True
    with clock.at('2026-10-03 12:00'):
        sid = pipeline.store.create_session('fixture')['id']
        pipeline.store.add_turn(sid, '話そう', 'うん', {})
        pipeline.store.add_thread('fixture', 'share', 'お香', '伝えたい', 'activity:1', weight=.8)
    with clock.at('2026-10-04 12:00'):
        assert not pipeline.proactive(sid, 'idle')['speak']
        assert not pipeline.proactive(sid)['speak']
    with clock.at('2026-10-04 12:29'):  # ふつう: 画面を開いてから30分
        assert not pipeline.proactive(sid, 'idle')['speak']
        assert len(pipeline.store.list_proactive('fixture')) == 1


def test_check_flags_fake_research_then_rewrites_without_recording(pipeline):
    pipeline.client = FakeLLMClient(text_responses=['最近、東京の香彩堂で10月18日に新作が出ると調べたよ。', 'まだ調べてないけど、お香が気になる。'],
        json_responses=[{}, {'problems': [{'text': '調べたよ', 'verdict': 'forbidden', 'reason': '活動記録がない'}]}, {'problems': []}])
    result = pipeline.run(pipeline.store.create_session('fixture')['id'], '最近調べた？')
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(result['turn_id'])
    assert trace['Check']['response']['parsed']['problems'][0]['verdict'] == 'forbidden'
    assert 'まだ調べてない' in result['assistant_text']
    call = next(c for c in pipeline.client.calls if c.get('schema', {}).get('title') == 'Check')
    assert 'packet.activities' in call['system'] and '調べ物の捏造' in call['system']
    assert json.loads(call['messages'][0]['content'])['packet']['activities'] == []
    assert pipeline.store.list_activities('fixture') == []


def test_scheduler_catchup_max_three_no_replay_and_restart(pipeline):
    with pipeline.store.connect() as db:
        db.execute("UPDATE autonomy_schedule SET last_at='2026-10-01T00:00:00+09:00'")
    with clock.at('2026-10-03 12:00'):
        submit(pipeline.store, catch_up, pipeline).result(timeout=5)
        assert len(pipeline.store.list_inner_life('fixture')) == 3
        submit(pipeline.store, catch_up, pipeline).result(timeout=5)
        assert len(pipeline.store.list_inner_life('fixture')) == 3
    with clock.at('2026-10-03 16:00'):
        pipeline.maintenance()
        pipeline.wait_for_steward()
        assert len(pipeline.store.list_inner_life('fixture')) == 4


def test_activity_and_opening_endpoints_and_character_isolation(pipeline):
    with TestClient(create_app(pipeline)) as web:
        sid = web.post('/api/sessions', json={}).json()['id']
        response = web.post('/api/proactive/opening', json={'session_id': sid})
        assert response.status_code == 200 and not response.json()['speak']
        data = web.get('/api/activity').json()
        assert set(data) == {'threads', 'activities', 'decisions', 'budget', 'opening_decisions'}
        assert web.post('/api/proactive/opening', json={'session_id': 'missing'}).status_code == 404
        other = pipeline.store.create_session('other')['id']
        assert web.post('/api/proactive/opening', json={'session_id': other}).status_code == 404
        assert 'activity-panel' in web.get('/dev').text


def test_unsupported_grounding_provider_is_clear(pipeline):
    from cbs.config import ProviderConfig
    pipeline.config.llm.providers['local'] = ProviderConfig(api='chat', base_url='http://localhost:8000/v1')
    with pytest.raises(LLMError, match='not supported'):
        RoutingLLMClient(pipeline.config).generate_grounded('local/test', 'test', [])


def test_gemini_grounding_tool_metadata_and_redaction():
    from types import SimpleNamespace
    from google.genai import types
    from cbs.llm.gemini import GeminiLLMClient
    calls = []
    def generate_content(**kwargs):
        calls.append(kwargs)
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[types.Part(text='Result secret')]),
                grounding_metadata=types.GroundingMetadata(web_search_queries=['query secret'],
                    grounding_chunks=[types.GroundingChunk(web=types.GroundingChunkWeb(title='Source secret', uri='https://example.org'))]))])
    client = GeminiLLMClient.__new__(GeminiLLMClient)
    client._key, client._thinking = 'secret', {}
    client._client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    result = client.generate_grounded('test', 'rules', [{'role': 'user', 'content': 'task'}])
    assert calls[0]['config'].tools[0].google_search is not None
    assert result.queries == ['query [REDACTED]']
    assert result.sources == [{'title': 'Source [REDACTED]', 'uri': 'https://example.org'}]
    assert 'secret' not in json.dumps(result.trace())


def test_probe_empty_opening_and_inner_life_report(tmp_path, monkeypatch):
    path = tmp_path / 'suite.yaml'
    path.write_text('probes:\n- id: A\n  category: autonomy\n  expect: opening\n  sessions:\n  - at: "2026-10-03 12:00"\n    messages: []\n  - at: "2026-10-06 12:00"\n    inner_life: true\n    opening: true\n    messages: []\n', encoding='utf-8')
    fake = FakeLLMClient(inner_life_responses=[{'action': 'research', 'reason': '関心', 'topic': 'お香', 'task': '白檀'}])
    monkeypatch.setattr(probe, 'ROOT', tmp_path)
    monkeypatch.setattr(probe, 'create_client', lambda config: fake)
    report = probe.run_suite(path, 'M8_fake', character_id="fixture", characters_root=ROOT / "tests/fixtures/characters").read_text(encoding='utf-8')
    assert '白檀 香り' in report and 'https://example.org/incense' in report
    assert 'Opening の判断' in report and 'スレッドの変化' in report


def test_idle_can_check_in_without_a_topic(pipeline):
    cfg = pipeline.config.autonomy
    cfg.idle_nudge = True
    with clock.at('2026-10-03 12:00'):
        sid = pipeline.store.create_session('fixture')['id']
        pipeline.proactive(sid)
        pipeline.store.add_turn(sid, '話そう', 'うん', {})
    assert not pipeline.store.list_threads('fixture', eligible_only=True)
    with clock.at('2026-10-03 12:31'):
        pipeline.client = FakeLLMClient(json_responses=[{'problems': []}],
            proactive_responses=[{'speak': True, 'reason': 'なんとなく声をかけたい', 'thread_ref': None, 'why_now': 'ちょっと暇になったから'}])
        result = pipeline.proactive(sid, 'idle')
        assert result['speak'] and result['turn_id']
    with clock.at('2026-10-03 13:10'):
        # The previous line was the partner's own check-in: wait for the user first.
        assert not pipeline.proactive(sid, 'idle')['speak']
