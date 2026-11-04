"""RP1 offline integration: scripted meaning judgments, real storage/windows."""
import json

import pytest

from cbs import clock
from cbs.autonomy import inner_life
from cbs.inner import update_inner
from cbs.memory import daily_maintenance
from cbs.sharing import ShareChoice, consolidate_shares, nearest, share_candidates, spoken_context


def thread(store, source='activity:old', kind='share'):
    return store.add_thread('fixture', kind, 'ある作品', '以前伝えた要点', source)


def rows(store):
    return {t['id']: t for t in store.list_threads('fixture', with_history=True)}


def test_spoken_history_crosses_sessions_and_conversations_and_reaches_actor(pipeline):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-27 10:55'):
        sid = s.create_session('fixture')['id']
        old = s.add_turn(sid, '', '以前伝えた要点と例え。', {}, kind='proactive')
        tid = thread(s)
    with clock.at('2026-10-27 22:44'):
        conversation = s.add_turn(sid, 'その場面の話', '会話で伝えた別の点。', {})
    with clock.at('2026-10-28 09:28'):
        new_sid = s.create_session('fixture')['id']
        p.client.proactive_responses = [{'speak': True, 'reason': '新しい見方だけ', 'want_ref': f'thread:{tid}'}]
        p.client.json_responses = [{'problems': []}]
        result = p.proactive(new_sid)
        assert result['speak'], result
        trace = s.get_trace(result['turn_id'])
        packet = trace['Packet']['output']
        call = next(c for c in p.client.calls if c.get('schema', {}).get('title') == 'Proactive')
        ctx = json.loads(call['messages'][0]['content'])
        assert {t['id'] for t in ctx['spoken_context']} == {old, conversation}
        assert ctx['spoken_context'] == packet['proactive']['spoken_context']
        assert any(c.get('role') == 'query' for c in p.client.embedding_calls)
        with s.connect() as db:
            assert db.execute('SELECT COUNT(DISTINCT conversation_id) FROM turns').fetchone()[0] == 3


def test_spoken_window_status_character_and_future(pipeline):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-24 09:27'):
        sid = s.create_session('fixture')['id']
        s.add_turn(sid, '', '範囲外', {}, kind='proactive')
    with clock.at('2026-10-25 09:28'):
        boundary = s.add_turn(sid, '', '境界の発言', {}, kind='proactive')
        s.add_turn(sid, '失敗', '未公開', {}, status='failed')
        other = s.create_session('other')['id']
        s.add_turn(other, '', '別人物', {}, kind='proactive')
    with clock.at('2026-10-29 09:28'):
        s.add_turn(sid, '', '未来', {}, kind='proactive')
    with clock.at('2026-10-28 09:28'):
        assert [t['id'] for t in spoken_context(p, ['話題'])] == [boundary]
        p.config.autonomy.proactive_history_days = 1
        assert spoken_context(p, ['話題']) == []


def test_activity_topic_gets_history_even_when_share_was_skipped(pipeline):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-27 10:55'):
        sid = s.create_session('fixture')['id']
        prior = s.add_turn(sid, '', '作品について以前話したこと', {}, kind='proactive')
    with clock.at('2026-10-28 08:00'):
        p.client.inner_life_responses = [{'action': 'reflect', 'topic': 'ある作品', 'reason': '考える'}]
        p.client.text_responses = ['作品についてもう一度考えた。']
        p.client.activity_responses = [{'share': {'operation': 'skip'}}]
        inner_life(p, refresh=False)
        assert not s.list_threads('fixture')
    with clock.at('2026-10-28 09:28'):
        p.client.proactive_responses = [{'speak': True, 'reason': 'まだ話していない部分だけ'}]
        p.client.text_responses = ['新しい部分だけ。']
        p.client.json_responses = [{'problems': []}]
        result = p.proactive(s.create_session('fixture')['id'])
        assert result['speak'], result
        call = next(c for c in p.client.calls if c.get('schema', {}).get('title') == 'Proactive')
        ctx = json.loads(call['messages'][0]['content'])
        assert ctx['threads'] == [] and ctx['activities']
        assert [t['id'] for t in ctx['spoken_context']] == [prior]
        assert s.get_trace(result['turn_id'])['Packet']['output']['proactive']['spoken_context'] == ctx['spoken_context']


def test_embedding_candidates_are_ranked_without_meaning_rules(pipeline):
    items = [{'id': n, 'text': f'候補{n}'} for n in range(8)]
    pipeline.client.embedding_responses = [
        [[0., 1.]] * 7 + [[1., 0.]], [[1., 0.]]]
    assert nearest(pipeline, ['問い合わせ'], items, lambda x: x['text'], limit=1) == [items[-1]]
    # Content-addressed documents are reused, while a changed text is re-embedded.
    pipeline.client.embedding_calls.clear()
    nearest(pipeline, ['問い合わせ'], items, lambda x: x['text'], limit=1)
    assert len(pipeline.client.embedding_calls) == 1
    assert pipeline.client.embedding_calls[0]['role'] == 'query'


@pytest.mark.parametrize('operation,count', [('new', 2), ('append', 1), ('skip', 1)])
def test_activity_share_new_append_skip(pipeline, operation, count):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-28 08:00'):
        tid = thread(s)
        p.client.inner_life_responses = [{'action': 'reflect', 'reason': '考える', 'topic': 'ある作品'}]
        p.client.text_responses = ['以前伝えた要点。別の視点も見つかった。']
        p.client.activity_responses = [{'share': {'operation': operation,
            'thread_ref': f'thread:{tid}' if operation == 'append' else None, 'detail': '新しい視点'}}]
        inner_life(p, refresh=False)
        assert len(s.list_threads('fixture')) == count
        assert s.list_activities('fixture')[0]['status'] == 'completed'
        assert ('新しい視点' in rows(s)[tid]['detail']) == (operation == 'append')
        call = next(c for c in p.client.calls if c.get('schema', {}).get('title') == 'ActivityUpdate')
        assert json.loads(call['messages'][0]['content'])['share_candidates'][0]['id'] == tid


def test_share_validation_and_recent_used_new_point(pipeline):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-27 08:00'):
        tid = thread(s)
        with s.connect() as db:
            s._thread_status(db, db.execute('SELECT * FROM threads WHERE id=?', (tid,)).fetchone(), 'used')
    with clock.at('2026-10-28 08:00'):
        before = rows(s)[tid]
        candidates = share_candidates(p, 'ある作品')
        assert candidates[0]['status'] == 'used'
        for ref in ('thread:999', 'thread:other'):
            with s.connect() as db, pytest.raises(ValueError):
                s.apply_share_choice(db, 'fixture', ShareChoice(operation='append', thread_ref=ref, detail='新しい点'),
                                     candidates, '作品', '活動', 'activity:new')
        assert rows(s)[tid] == before
        with s.connect() as db:
            s.apply_share_choice(db, 'fixture', ShareChoice(operation='append', thread_ref=f'thread:{tid}', detail='未共有の点'),
                                 candidates, '作品', '活動', 'activity:new')
        updated = rows(s)[tid]
        assert updated['status'] == 'open' and updated['detail'].endswith('\n未共有の点')
        assert any(h['operation'] == 'used' for h in updated['history'])
        assert updated['history'][-1]['operation'] == 'append:activity:new'


@pytest.mark.parametrize('operation,count', [('new', 2), ('append', 1), ('skip', 1)])
def test_want_choices_share_the_existing_share(pipeline, operation, count):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-28 08:00'):
        tid = thread(s)
        p.client.inner_update_responses = [{'feeling': {'sentence': '考えを深めた。',
            'levels': {'energy': 'ふつう', 'social': 'ふつう', 'concern': 'なし'}},
            'wants': [{'operation': operation, 'thread_ref': f'thread:{tid}' if operation == 'append' else None,
                       'topic': 'ある作品', 'detail': '別の新しい点', 'direction': '伝えたい', 'motive': '伝えたくなった',
                       'weight': .8, 'trigger_ref': 'activity:new'}]}]
        update_inner(p, 'activity:new', {'kind': 'activity', 'summary': 'ある作品を考えた'})
        assert s.list_inner_updates('fixture')[-1]['status'] == 'completed'
        assert len(s.list_threads('fixture')) == count
        assert ('別の新しい点' in rows(s)[tid]['detail']) == (operation == 'append')


def test_daily_merge_skip_and_used_untouched_and_runs_once(pipeline):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-28 08:00'):
        target, duplicate, already = [thread(s, f'activity:{n}') for n in range(3)]
        with s.connect() as db:
            s._thread_status(db, db.execute('SELECT * FROM threads WHERE id=?', (already,)).fetchone(), 'used')
        used = rows(s)[already]
        # The fixture already maintained today's empty DB before these records.
        with s.connect() as db:
            db.execute("DELETE FROM memory_maintenance WHERE character_id='fixture'")
        p.client.share_review_responses = [
            {'operation': 'new'},
            {'operation': 'append', 'thread_ref': f'thread:{target}', 'detail': '重複していない点'}]
        daily_maintenance(p)
        first = rows(s)
        assert first[duplicate]['status'] == 'dropped'
        assert first[target]['detail'].endswith('\n重複していない点')
        assert first[already] == used
        calls = len(p.client.calls)
        daily_maintenance(p)
        assert len(p.client.calls) == calls
        p.client.share_review_responses = [{'operation': 'skip'}]
        consolidate_shares(p)
        assert rows(s)[target]['status'] == 'dropped'
        assert rows(s)[already] == used
    with clock.at('2026-10-29 08:00'):
        thread(s, 'activity:later')
        calls = sum(c.get('schema', {}).get('title') == 'ShareReview' for c in p.client.calls)
        daily_maintenance(p)
        assert sum(c.get('schema', {}).get('title') == 'ShareReview' for c in p.client.calls) == calls


def test_daily_never_appends_to_used_or_unknown_target(pipeline):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-28 08:00'):
        source, used = thread(s), thread(s, 'activity:used')
        with s.connect() as db:
            s._thread_status(db, db.execute('SELECT * FROM threads WHERE id=?', (used,)).fetchone(), 'used')
        before = rows(s)
        p.client.share_review_responses = [{'operation': 'append', 'thread_ref': f'thread:{used}', 'detail': '追加'}]
        # An invalid target keeps that share unchanged; the pass continues instead of stopping.
        reviewed = consolidate_shares(p)
        assert rows(s) == before
        assert any(r.get('kept') == 'invalid target' for r in reviewed)
