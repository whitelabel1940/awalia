"""D3 permission plumbing. Semantic judgments are supplied by FakeLLMClient."""
import json

import pytest
from fastapi.testclient import TestClient

from cbs import clock
from cbs.inner import BASELINE, update_inner
from cbs.knowing import rebuild
from cbs.memory import UserFact
from cbs.note import DiaryEntry, fill_diaries
from cbs.pipeline import PreJudge, assemble_packet
from cbs.runtime import read_prompt
from cbs.store import Store
from cbs.voice_text import voice_call
from cbs.web import create_app
from test_m11c import fact
from test_diary import memory


def context(p, title):
    call = next(c for c in reversed(p.client.calls) if c.get('schema', {}).get('title') == title)
    return json.loads(call['messages'][0]['content']), call


def test_migration_once_preserves_no_and_notices_until_save(pipeline):
    p = pipeline
    yes = fact(p.store, 'ongoing_health', True)
    no = fact(p.store, override=False)
    fact(p.store, 'money', True, cid='other')
    p.store.set_private_settings('fixture', {'ongoing_health': True, 'money': False})
    p.store.set_private_settings('other', {'money': True})
    with p.store.connect() as db:
        db.execute("DELETE FROM storage_migrations WHERE name='private-daily-mention-v1'")
    migrated = Store(p.store.path)
    with migrated.connect() as db:
        assert not db.execute('SELECT * FROM private_preferences').fetchall()
        assert db.execute('SELECT mention_override FROM user_facts WHERE id=?', (yes,)).fetchone()[0] is None
        assert db.execute('SELECT mention_override FROM user_facts WHERE id=?', (no,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM storage_migrations WHERE name='private-daily-mention-v1'").fetchone()[0] == 1
    assert all(not t['may_initiate'] for t in migrated.list_threads('fixture'))
    assert migrated.private_mention_notice('fixture') and migrated.private_mention_notice('other')
    p.store = migrated
    web = TestClient(create_app(p))
    assert web.get('/api/note/private-settings').json()['mention_notice']
    assert web.put('/api/note/private-settings', json={'categories': {'unknown': True}}).status_code == 422
    assert web.get('/api/note/private-settings').json()['mention_notice']
    saved = web.put('/api/note/private-settings', json={'categories': {'ongoing_health': True}}).json()
    assert not saved['mention_notice']
    assert saved['categories']['ongoing_health']['allow_mention']
    reopened = Store(migrated.path)
    assert reopened.private_settings('fixture')['ongoing_health']['allow_mention']
    assert not reopened.private_mention_notice('fixture') and reopened.private_mention_notice('other')
    # Fact permissions selected under the new meaning survive subsequent starts too.
    with reopened.connect() as db:
        db.execute('UPDATE user_facts SET mention_override=1 WHERE id=?', (yes,))
    assert next(f for f in Store(migrated.path).list_user_facts('fixture') if f['id'] == yes)['mention_override'] is True


@pytest.mark.parametrize('category', ['temporary_health', 'ongoing_health', 'religion', 'politics', 'sexual', 'money', 'other'])
def test_daily_permission_without_followup_and_per_fact_no(pipeline, category):
    p = pipeline
    assert not p.store.private_mention_notice('fixture')
    fid = fact(p.store, category, follow=False)
    def packet():
        f = p.store.list_user_facts('fixture', current_only=True)[0]
        f['ref'] = f'fact:{fid}'
        judge = PreJudge(memory_use=[dict(ref=f['ref'], relevant_now='yes', counterevidence='no', continuation='yes')])
        return f, assemble_packet(p.character, judge, candidate_memories=[f])
    f, result = packet()
    assert not f['allow_mention'] and not f['may_initiate'] and not result['user_facts']
    p.store.set_private_settings('fixture', {category: True})
    f, result = packet()
    assert f['allow_mention'] and f['may_initiate'] and result['user_facts']
    assert not p.store.list_threads('fixture')
    with p.store.connect() as db:
        db.execute('UPDATE user_facts SET mention_override=0 WHERE id=?', (fid,))
    f, result = packet()
    assert not f['allow_mention'] and not f['may_initiate'] and not result['user_facts']
    p.store.set_private_settings('fixture', {category: False})
    with p.store.connect() as db:
        db.execute('UPDATE user_facts SET mention_override=1 WHERE id=?', (fid,))
    assert packet()[1]['user_facts']


def test_private_want_eligibility_and_permission_pause(pipeline):
    p = pipeline
    fid = fact(p.store, 'money', True, follow=False)
    tid = p.store.add_thread('fixture', 'want', 'お金の話', '本人が話した使い方', f'fact:{fid}')
    with p.store.connect() as db:
        p.store._fact_thread(db, 'fixture', fid, UserFact(operation='confirm', content='本人が話した使い方',
            private=True, private_category='money', importance=.5, pinned=False, follow_up=False), None)
    assert p.store.list_threads('fixture', eligible_only=True)[0]['id'] == tid
    with p.store.connect() as db:
        db.execute('UPDATE user_facts SET mention_override=0 WHERE id=?', (fid,))
        p.store._sync_private_threads(db, 'fixture')
    assert not p.store.list_threads('fixture', eligible_only=True)
    assert p.store.list_threads('fixture')[0]['status'] == 'blocked'
    with p.store.connect() as db:
        db.execute('UPDATE user_facts SET mention_override=1 WHERE id=?', (fid,))
        p.store._sync_private_threads(db, 'fixture')
    assert p.store.list_threads('fixture', eligible_only=True)[0]['id'] == tid
    with p.store.connect() as db:
        t = db.execute('SELECT * FROM threads WHERE id=?', (tid,)).fetchone()
        p.store._thread_status(db, t, 'used')
        p.store._sync_private_threads(db, 'fixture')
    assert not p.store.list_threads('fixture', eligible_only=True)
    assert p.store.list_user_facts('fixture')[0]['may_initiate']  # daily permission survives a use


def test_reported_outcome_while_blocked_cannot_reopen(pipeline):
    p = pipeline
    fid = fact(p.store)
    t = p.store.list_threads('fixture')[0]
    with p.store.connect() as db:
        db.execute('UPDATE user_facts SET mention_override=0 WHERE id=?', (fid,))
        p.store._sync_private_threads(db, 'fixture')
        db.execute('UPDATE user_facts SET follow_up=0 WHERE id=?', (fid,))
        p.store._fact_thread(db, 'fixture', fid, UserFact(operation='confirm', content='もう回復した',
            private=True, private_category='temporary_health', importance=.5, pinned=False,
            follow_up=False, follow_up_thread=t['id']), None)
        db.execute('UPDATE user_facts SET mention_override=NULL WHERE id=?', (fid,))
        p.store._sync_private_threads(db, 'fixture')
    assert p.store.list_threads('fixture')[0]['status'] == 'used'
    assert not p.store.list_threads('fixture', eligible_only=True)
    assert not p.store.private_context('fixture')[0]['may_initiate']


@pytest.mark.parametrize('allowed', [False, True])
def test_proactive_direct_private_fact_without_followup(pipeline, allowed):
    p = pipeline
    with clock.at('2026-10-19 12:00'):
        sid = p.store.create_session('fixture')['id']
        p.store.add_turn(sid, '話した', '聞いた', {})
        fid = fact(p.store, 'money', allowed, follow=False)
    with clock.at('2026-10-20 12:00'):
        p.client.proactive_responses = [dict(speak=True, reason='前の使い方の話の続き', fact_ref=f'fact:{fid}', private_topic=True)]
        p.client.text_responses = ['経験に使いたいって話、印象に残ってるよ。']
        p.client.extract_responses = [{'ledger_updates': [], 'new_canon': [], 'disclosures': [], 'used_memories': [], 'used_threads': []}]
        p.client.json_responses = [{'problems': []}]
        result = p.proactive(sid)
        p.wait_for_steward()
    assert result['speak'] is allowed
    if allowed:
        proactive_context, _ = context(p, 'Proactive')
        assert proactive_context['spoken_context']  # RP1 also searches for direct fact choices, without a thread.
        trace = p.store.get_trace(result['turn_id'])
        assert trace['Packet']['output']['user_facts'][0]['id'] == fid
        assert trace['Packet']['output']['private_facts_policy'][0]['allow_mention']
        assert p.store.list_user_facts('fixture')[0]['may_initiate']


def test_inner_creates_daily_private_want_and_append_uses_rp1(pipeline):
    p = pipeline
    fid = fact(p.store, 'money', True, follow=False)
    want = dict(private=True, fact_ref=f'fact:{fid}', direction='気にかける', motive='本人の話を覚えている',
                topic='経験に使いたい', detail='本人の考えの続き', weight=.6, trigger_ref='d3:new')
    p.client.inner_update_responses = [dict(feeling=dict(sentence='また話したい。', levels=BASELINE), wants=[want])]
    update_inner(p, 'd3:new', {'kind': 'conversation'})
    threads = p.store.list_threads('fixture', eligible_only=True)
    assert len(threads) == 1 and threads[0]['private'] and threads[0]['kind'] == 'want'
    t = threads[0]
    with p.store.connect() as db:
        row = db.execute('SELECT * FROM threads WHERE id=?', (t['id'],)).fetchone()
        p.store._thread_status(db, row, 'used')
    p.client.inner_update_responses = [dict(feeling=dict(sentence='新しい話を聞いた。', levels=BASELINE),
        wants=[{**want, 'operation': 'append', 'thread_ref': t['ref'], 'detail': '本人が新しく話した点', 'trigger_ref': 'd3:append'}])]
    update_inner(p, 'd3:append', {'kind': 'conversation'})
    t = p.store.list_threads('fixture', eligible_only=True)[0]
    assert '本人が新しく話した点' in t['detail'] and t['told_at']


def test_episodizer_and_knowing_receive_effective_permissions(pipeline):
    p = pipeline
    p.store.set_private_settings('fixture', {'politics': True})
    fid = fact(p.store, 'politics', False, follow=False)
    sid = p.store.create_session('fixture')['id']
    tid = p.store.add_turn(sid, '選挙には毎回行くと話した', '聞いた', {})
    p.client.episodizer_responses = [{'episodes': [dict(summary='大切にしていることを話した。', details='本人が話した。', importance=.5, pinned=False)], 'user_facts': [], 'observations': [dict(kind='interest',
        summary='本人は選挙には毎回行くと話した。', response='unclear', source_turns=[tid], private=False)]}]
    p.close_conversation(sid)
    p.wait_for_steward()
    ctx, call = context(p, 'EpisodizerOutput')
    assert ctx['private_categories']['politics']['allow_mention']
    assert next(f for f in ctx['private_facts_policy'] if f['id'] == fid)['allow_mention'] is False
    assert '根拠の発言' in call['system'] and '性的指向の推測' in call['system']
    rebuild(p, force=True)
    ctx, call = context(p, 'Understandings')
    assert ctx['private_categories']['politics']['allow_mention']
    assert ctx['private_facts_policy'][0]['allow_mention'] is False
    assert '体調については気づかい方の好みだけ' in call['system']


@pytest.mark.parametrize('allowed', [False, True])
def test_diary_summary_stays_soft_and_check_gets_separate_fields(pipeline, allowed):
    p = pipeline
    fact(p.store, 'ongoing_health', allowed, follow=False)
    entry = dict(summary='体調の話をした。', partner_comment='話してくれたこと、覚えてるよ。')
    p.client.diary_responses = [entry]
    p.client.voice_check_responses = [{'problems': []}]
    output = voice_call(p, 'note_diary', DiaryEntry, {'day': '2026-10-19', 'memories': []},
                        lambda e: [e.summary, e.partner_comment], purpose='memory', trace={})
    assert output.summary == entry['summary']
    ctx, writer = context(p, 'DiaryEntry')
    assert ctx['private_facts_policy'][0]['allow_mention'] is allowed
    ctx, check = context(p, 'VoiceCheck')
    assert ctx['voice_output'] == entry
    assert ctx['private_facts_policy'][0]['allow_mention'] is allowed
    assert 'チェックありでも' in writer['system'] and 'summary' in writer['system']
    assert 'voice_outputのsummary' in check['system'] and 'partner_commentと独り言' in check['system']
    assert 'allow_mention=true' in read_prompt('note_monologue')


@pytest.mark.parametrize('where', ['diary', 'monologue'])
def test_voice_followup_consumed_atomically_once(pipeline, where):
    p = pipeline
    with clock.at('2026-10-19 12:00'):
        fid = fact(p.store)
        memory(p.store, '2026-10-19')
    with clock.at('2026-10-20 12:00'):
        p.client.voice_check_responses = [{'problems': [], 'used_private_followups': [f'fact:{fid}', 'fact:99999', 'thread:1']}]
        if where == 'diary':
            p.client.diary_responses = [dict(summary='体調の話をした。', partner_comment='その後、具合はどう？')]
            assert fill_diaries(p)[0]['status'] == 'completed'
            assert fill_diaries(p) == []
        else:
            p.client.monologue_responses = [dict(text='その後、具合はどう？')]
            update_inner(p, 'd3:voice', {'kind': 'conversation'})
        assert p.store.list_threads('fixture')[0]['status'] == 'used'
        assert not p.store.private_context('fixture')[0]['may_initiate']
        p.store.set_private_settings('fixture', {'temporary_health': False})
        assert not p.store.list_threads('fixture', eligible_only=True)
