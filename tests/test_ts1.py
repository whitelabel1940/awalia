"""TS1: use snapshots distinguish private notes from already shared points."""
import json

import pytest

from cbs import clock
from cbs.autonomy import inspector_activity
from cbs.sharing import ShareChoice, consolidate_shares, share_candidates


def mark_used(store, tid):
    with store.connect() as db:
        row = db.execute('SELECT * FROM threads WHERE id=?', (tid,)).fetchone()
        store._thread_status(db, row, 'used')


def append(store, tid, detail):
    candidates = store.list_threads('fixture')
    with store.connect() as db:
        store.apply_share_choice(db, 'fixture',
            ShareChoice(operation='append', thread_ref=f'thread:{tid}', detail=detail),
            candidates, 'ある作品', '', f'activity:{detail}')


@pytest.mark.parametrize('kind', ['share', 'want'])
def test_latest_used_snapshot_and_inspector_history(store, kind):
    with clock.at('2026-10-27 08:00'):
        tid = store.add_thread('fixture', kind, 'ある作品', '最初の要点', 'activity:1')
        never = store.list_threads('fixture')[0]
        assert never['told_at'] is None and never['told_detail'] is None
        assert 'history' not in never
        mark_used(store, tid)
        first_at = clock.iso()
        unchanged = store.list_threads('fixture')[0]
        assert unchanged['told_at'] == clock.stamp(first_at)
        assert unchanged['told_detail'] is None
    with clock.at('2026-10-28 08:00'):
        append(store, tid, '新しい要点')
        reopened = store.list_threads('fixture', eligible_only=True)[0]
        assert reopened['told_at'] == clock.stamp(first_at)
        assert reopened['told_at']['relative'] == '昨日'
        assert reopened['told_detail'] == '最初の要点'
        assert reopened['detail'] == '最初の要点\n新しい要点'
        mark_used(store, tid)
        second_at = clock.iso()
        assert store.list_threads('fixture')[0]['told_detail'] is None
        append(store, tid, 'さらに別の要点')
        inspected = store.list_threads('fixture', with_history=True)[0]
        assert inspected['told_at'] == clock.stamp(second_at)
        assert inspected['told_detail'] == reopened['detail']
        assert [h['operation'] for h in inspected['history']].count('used') == 2
        assert inspected['history'][-1]['after']['detail'] == inspected['detail']
        # A non-use event must not replace the last shared snapshot.
        with store.connect() as db:
            row = db.execute('SELECT * FROM threads WHERE id=?', (tid,)).fetchone()
            store._thread_status(db, row, 'dropped')
        assert store.list_threads('fixture')[0]['told_at'] == clock.stamp(second_at)
        other = store.add_thread('other', kind, '別人物の話', '別の要点', 'activity:1')
        mark_used(store, other)
        assert [t['id'] for t in store.list_threads('fixture')] == [tid]


@pytest.mark.parametrize('shared', [False, True])
def test_fields_reach_proactive_judge_actor_and_check(pipeline, shared):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-27 08:00'):
        tid = s.add_thread('fixture', 'want', 'ある作品', '最初の要点', 'activity:1')
        with s.connect() as db:
            db.execute('UPDATE threads SET direction=?,motive=? WHERE id=?', ('伝えたい', '考えを伝えたい', tid))
        if shared:
            mark_used(s, tid)
            append(s, tid, '新しい要点')
    with clock.at('2026-10-28 08:00'):
        expected = s.list_threads('fixture')[0]
        p.client.proactive_responses = [{'speak': True, 'reason': '今の考えを伝える',
                                        'want_ref': expected['ref']}]
        p.client.json_responses = [{'problems': []}]
        result = p.proactive(s.create_session('fixture')['id'])
        assert result['speak'], result
        call = next(c for c in p.client.calls if c.get('schema', {}).get('title') == 'Proactive')
        ctx = json.loads(call['messages'][0]['content'])
        packet = s.get_trace(result['turn_id'])['Packet']['output']
        check = next(c for c in p.client.calls if c.get('schema', {}).get('title') == 'Check')
        check_packet = json.loads(check['messages'][0]['content'])['packet']
        for item in (ctx['threads'][0], ctx['wants'][0], packet['proactive']['want'],
                     check_packet['proactive']['want']):
            assert item['told_at'] == expected['told_at']
            assert item['told_detail'] == expected['told_detail']
            assert 'history' not in item
            assert 'after_json' not in item and 'before_json' not in item
        assert inspector_activity(p)['threads'][0]['history']


@pytest.mark.parametrize('state', ['never', 'appended'])
def test_fields_reach_normal_turn_packet(pipeline, state):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-28 08:00'):
        tid = s.add_thread('fixture', 'share', 'ある作品', '最初の要点', 'activity:1')
        if state != 'never':
            mark_used(s, tid)
            append(s, tid, '新しい要点')
        expected = s.list_threads('fixture')[0]
        p.client.json_responses = [{'memory_use': [{'ref': expected['ref'], 'relevant_now': 'yes',
            'counterevidence': 'no', 'continuation': 'no'}]}, {'problems': []}]
        result = p.run(s.create_session('fixture')['id'], 'その作品について聞きたい')
        packet = s.get_trace(result['turn_id'])['Packet']['output']
        actual = packet['threads'][0]
        assert actual['told_at'] == expected['told_at']
        assert actual['told_detail'] == expected['told_detail']
        assert 'history' not in actual


def test_fields_reach_share_candidates_and_review(pipeline):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-28 08:00'):
        tid = s.add_thread('fixture', 'share', 'ある作品', '最初の要点', 'activity:1')
        mark_used(s, tid)
        append(s, tid, '新しい要点')
        s.add_thread('fixture', 'share', 'ある作品', '別の要点', 'activity:2')
        candidates = share_candidates(p, 'ある作品')
        assert candidates[0]['told_at'] == clock.stamp(clock.iso())
        assert candidates[0]['told_detail'] == '最初の要点'
        p.client.share_review_responses = [{'operation': 'new'}, {'operation': 'new'}]
        consolidate_shares(p)
        calls = [c for c in p.client.calls if c.get('schema', {}).get('title') == 'ShareReview']
        assert len(calls) == 2
        for call in calls:
            ctx = json.loads(call['messages'][0]['content'])
            for item in [ctx['source'], *ctx['candidates']]:
                assert 'told_at' in item and 'told_detail' in item
                assert 'history' not in item
                if item['id'] == tid:
                    assert item['told_detail'] == '最初の要点'
