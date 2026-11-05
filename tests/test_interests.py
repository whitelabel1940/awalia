from cbs.config import ROOT
from response_helpers import problems, extraction
import json
import math

import pytest
from pydantic import ValidationError

from cbs import clock
from cbs.interests import salience, timed_interest
from cbs.llm import FakeLLMClient
from cbs.memory import HaruExpressed, daily_maintenance
from cbs.pipeline import NewCanon, PreJudge, assemble_packet
from cbs.store import Store

EMPTY = {'claims': [], 'ledger_updates': [], 'new_canon': []}


@pytest.fixture(autouse=True)
def two_day_rule(request):
    # Most rule tests below were written for "2 distinct days"; the default (3 days over 7 days) is tested separately.
    if 'pipeline' in request.fixturenames:
        request.getfixturevalue('pipeline').store.interest_rules = {'min_days': 2, 'min_span_days': 0}


def proposal(operation='new', interest_id=None, kind='curiosity', detail='お香が気になる', turns=None):
    return dict(operation=operation, interest_id=interest_id, topic='お香', kind=kind,
                detail=detail, importance=0.6, evidence_turns=turns or [1], source_episode=0)


def express(pipeline, when, operation='new', interest_id=None, **kwargs):
    with clock.at(when):
        sid = pipeline.store.create_session('fixture')['id']
        turn_id = pipeline.store.add_turn(sid, 'お香の話', 'お香が気になる', {})
        pipeline.client.episodizer_responses = [{
            'episodes': [{'summary': 'お香の話', 'details': 'テスト係はお香への関心を示した', 'importance': 0.5, 'pinned': False}],
            'user_facts': [], 'haru_expressed': [proposal(operation, interest_id, turns=[turn_id], **kwargs)]}]
        pipeline.close_conversation(sid)
        pipeline.wait_for_steward()
        return next(i for i in pipeline.store.list_interests('fixture') if i['origin'] == 'conversation')


def test_two_distinct_days_and_conversations_not_same_day(pipeline):
    first = express(pipeline, '2026-09-01 20:00')
    second = express(pipeline, '2026-09-01 22:00', 'reinforce', first['id'])
    assert second['status'] == 'tentative' and second['mention_count'] == 2
    third = express(pipeline, '2026-09-02 20:00', 'reinforce', first['id'])
    assert third['status'] == 'established'
    assert len(third['evidence']) == 3 and third['first_seen'] == first['first_seen']
    assert third['salience'] == pytest.approx(0.6 + 0.3 * math.log1p(3))


def test_single_conversation_crossing_midnight_not_established(pipeline):
    with clock.at('2026-09-01 23:50'):
        sid = pipeline.store.create_session('fixture')['id']
        first = pipeline.store.add_turn(sid, '話', '好き', {})
    with clock.at('2026-09-02 00:10'):
        second = pipeline.store.add_turn(sid, '話', '好き', {})
        pipeline.client.episodizer_responses = [{
            'episodes': [{'summary': '話', 'details': '好き', 'importance': .5, 'pinned': False}],
            'user_facts': [], 'haru_expressed': [proposal(turns=[first, second])]}]
        pipeline.close_conversation(sid)
        pipeline.wait_for_steward()
    assert next(i for i in pipeline.store.list_interests('fixture') if i['origin'] == 'conversation')['status'] == 'tentative'


def test_days_follow_partner_reply_not_user_input_at_midnight(pipeline):
    with clock.at('2026-09-01 23:59'):
        sid = pipeline.store.create_session('fixture')['id']
        tid = pipeline.store.add_turn(sid, '好き？', 'お香が気になる', {},
                                     assistant_at=clock.parse('2026-09-02 00:01').isoformat())
    with clock.at('2026-09-02 00:02'):
        pipeline.client.episodizer_responses = [{
            'episodes': [{'summary': 'お香', 'details': '関心', 'importance': .5, 'pinned': False}],
            'user_facts': [], 'haru_expressed': [proposal(turns=[tid])]}]
        pipeline.close_conversation(sid)
        pipeline.wait_for_steward()
        item = next(i for i in pipeline.store.list_interests('fixture') if i['origin'] == 'conversation')
        assert item['first_seen'].startswith('2026-09-02T00:01')
        assert item['evidence'][0]['local_day'] == '2026-09-02'
    same_day = express(pipeline, '2026-09-02 20:00', 'reinforce', item['id'])
    assert same_day['status'] == 'tentative'
    next_day = express(pipeline, '2026-09-03 20:00', 'reinforce', item['id'])
    assert next_day['status'] == 'established'


def test_change_keeps_history_and_new_position_requires_new_days(pipeline):
    first = express(pipeline, '2026-08-01 20:00', kind='dislike', detail='お香は苦手')
    express(pipeline, '2026-08-02 20:00', 'reinforce', first['id'])
    changed = express(pipeline, '2026-08-10 20:00', 'change', first['id'], kind='like', detail='軽いお香は好き')
    assert changed['status'] == 'tentative' and changed['kind'] == 'like'
    assert changed['history'][-1]['before']['kind'] == 'dislike'
    assert changed['history'][-1]['after']['detail'] == '軽いお香は好き'
    assert len(changed['evidence']) == 3
    revived = express(pipeline, '2026-08-11 20:00', 'reinforce', first['id'])
    assert revived['status'] == 'established'


def test_exact_fade_boundary_revival_and_author_protection(pipeline):
    first = express(pipeline, '2026-08-01 20:00')
    express(pipeline, '2026-08-02 20:00', 'reinforce', first['id'])
    with clock.at('2026-09-16 19:59:59'):
        pipeline.store.fade_interests('fixture', 45)
        assert next(i for i in pipeline.store.list_interests('fixture') if i['id'] == first['id'])['status'] == 'established'
    with clock.at('2026-09-16 20:00'):
        pipeline.store.fade_interests('fixture', 45)
        pipeline.store.fade_interests('fixture', 45)
        items = pipeline.store.list_interests('fixture')
        assert next(i for i in items if i['id'] == first['id'])['status'] == 'fading'
        assert all(i['status'] == 'established' for i in items if i['origin'] == 'author')
        assert sum(c['operation'] == 'fade' for c in pipeline.store.list_interest_changes('fixture')) == 1
    revived = express(pipeline, '2026-09-17 20:00', 'reinforce', first['id'])
    assert revived['status'] == 'established'
    with clock.at('2028-10-01 20:00'):
        pipeline.store.fade_interests('fixture', 45)
        assert all(i['status'] == 'established' for i in pipeline.store.list_interests('fixture') if i['origin'] == 'author')


def test_weaken_and_invalid_reference_do_not_mutate_author(pipeline):
    author = pipeline.store.list_interests('fixture')[0]
    with clock.at('2026-10-01 20:00'):
        sid = pipeline.store.create_session('fixture')['id']
        tid = pipeline.store.add_turn(sid, '話', '気持ち', {})
        cid = pipeline.store.history(sid)[0]['conversation_id']
        with pipeline.store.connect() as db:
            eid = db.execute("INSERT INTO memory_episodes (character_id, conversation_id, started_at, ended_at, summary, details, importance, pinned, created_at) VALUES ('fixture', ?, ?, ?, '話', '話', .5, 0, ?)",
                             (cid, clock.iso(), clock.iso(), clock.iso())).lastrowid
        warnings = []
        with pipeline.store.connect() as db:
            pipeline.store.apply_interests(db, 'fixture', [HaruExpressed(**proposal('change', author['id'], turns=[tid])),
                HaruExpressed(**proposal('reinforce', 999, turns=[tid])), HaruExpressed(**proposal(turns=[999]))],
                [[1, 0]] * 3, pipeline.store.history(sid), [eid], cid, warnings)
        assert len(warnings) == 3 and pipeline.store.list_interests('fixture')[0]['detail'] == author['detail']
        pipeline.close_conversation(sid)
        pipeline.wait_for_steward()
    first = express(pipeline, '2026-10-02 20:00')
    weak = express(pipeline, '2026-10-03 20:00', 'weaken', first['id'], detail='前ほどお香に興味がなくなった')
    assert weak['status'] == 'fading' and weak['importance'] == 0.3
    assert weak['detail'] == '前ほどお香に興味がなくなった'
    assert weak['history'][-1]['before']['detail'] != weak['history'][-1]['after']['detail']


def test_legacy_preference_migration_idempotent_existing_author_seed(store, character):
    with clock.at('2026-09-01 20:00'):
        sid = store.create_session('fixture')['id']
        tid = store.add_turn(sid, '好き？', '紅茶が好き', {})
        fact = store.add_canon('fixture', 'preference', '紅茶が好き', tid)
        store.add_canon('fixture', 'past_detail', '師匠の口癖', tid)
    reopened = Store(store.path, characters_root=ROOT / "tests/fixtures/characters")
    items = reopened.list_interests('fixture')
    migrated = next(i for i in items if i['migrated_canon'] == fact['id'])
    assert migrated['detail'] == '紅茶が好き' and migrated['origin'] == 'conversation'
    assert migrated['first_seen'] == fact['created_at'] and migrated['evidence'][0]['source_turn'] == tid
    assert all(f['kind'] != 'preference' for f in reopened.list_canon('fixture'))
    assert len([i for i in items if i['origin'] == 'author']) == len(character.expression.preferences)
    assert len(Store(store.path, characters_root=ROOT / "tests/fixtures/characters").list_interests('fixture')) == len(items)
    with pytest.raises(ValidationError):
        NewCanon(kind='preference', content='好み')


def test_salience_decay_and_author_never_decays():
    base = dict(importance=.6, mention_count=3, last_seen='2026-09-01 20:00', origin='conversation')
    assert salience(base, '2026-10-01 20:00') == pytest.approx((.6 + .3 * math.log1p(3)) / 2)
    assert salience({**base, 'origin': 'author'}, '2028-10-01 20:00') == pytest.approx(.6 + .3 * math.log1p(3))


def test_migration_does_not_reuse_legacy_canon_ids(store, character):
    old = store.add_canon('fixture', 'preference', '昔の好み')
    reopened = Store(store.path, characters_root=ROOT / "tests/fixtures/characters")
    assert {i['topic'] for i in reopened.list_interests('fixture') if i['origin'] == 'author'} == set(character.expression.preferences)
    new = reopened.add_canon('fixture', 'past_detail', '新しい過去の細部')
    assert new['id'] > old['id']
    another_legacy = reopened.add_canon('fixture', 'preference', '別の旧好み')
    migrated = Store(store.path, characters_root=ROOT / "tests/fixtures/characters")
    interests = [i for i in migrated.list_interests('fixture') if i['origin'] == 'conversation']
    assert {i['migrated_canon'] for i in interests} == {old['id'], another_legacy['id']}
    assert {i['detail'] for i in interests} == {'昔の好み', '別の旧好み'}


def test_packet_top_six_relevant_fading_history_and_relative_time(character):
    items = [dict(id=i, topic=str(i), kind='like', detail='関心', status='established', origin='conversation',
                  importance=i/10, mention_count=0, first_seen='2026-09-01 20:00', last_seen='2026-09-01 20:00', history=[])
             for i in range(1, 9)]
    items.append({**items[0], 'id': 9, 'status': 'fading', 'importance': 10})
    with clock.at('2026-09-10 20:00'):
        packet = assemble_packet(character, PreJudge(), interests=items)
        assert [i['id'] for i in packet['interests']] == [8, 7, 6, 5, 4, 3]
        judge = PreJudge(memory_use=[dict(ref='interest:9', relevant_now='yes', counterevidence='no', continuation='no')])
        packet = assemble_packet(character, judge, interests=items, candidate_memories=[timed_interest(items[-1])])
        assert packet['interests'][-1]['id'] == 9 and packet['interests'][-1]['time']['relative']


def test_summary_once_daily_failure_and_stale_summary_not_foreground(pipeline):
    with clock.at('2026-09-01 20:00'):
        pipeline.client.now_responses = [{'summary': '新しい関心に惹かれている。'}]
        daily_maintenance(pipeline)
        daily_maintenance(pipeline)
        assert pipeline.store.now_summary('fixture')['summary'] == '新しい関心に惹かれている。'
    new = express(pipeline, '2026-09-01 21:00')
    # A new interest or more evidence only adds to the summary: it stays visible.
    assert pipeline.store.now_summary('fixture')
    express(pipeline, '2026-09-01 21:30', 'reinforce', new['id'])
    assert pipeline.store.now_summary('fixture')
    # An interest first recorded after the summary cannot be in it, so changing it keeps the summary.
    express(pipeline, '2026-09-01 22:00', 'change', new['id'], detail='お香の香木の香りに惹かれている')
    assert pipeline.store.now_summary('fixture')
    with clock.at('2026-09-02 20:00'):
        pipeline.client.now_responses = ['bad JSON']
        daily_maintenance(pipeline)
        with pipeline.store.connect() as db:
            assert db.execute("SELECT status FROM memory_maintenance WHERE local_day='2026-09-02'").fetchone()[0] == 'failed'
    with clock.at('2026-09-03 20:00'):
        daily_maintenance(pipeline)
        assert pipeline.store.now_summary('fixture')
    # Now the summary may describe it, so a change may contradict it: hidden until the next daily summary.
    express(pipeline, '2026-09-03 21:00', 'change', new['id'], detail='白檀の香りに惹かれている')
    assert pipeline.store.now_summary('fixture') is None


def test_m5_already_maintained_day_still_initializes_m6_once(pipeline):
    with clock.at('2026-09-01 20:00'):
        with pipeline.store.connect() as db:
            db.execute('DELETE FROM now_summaries')
            db.execute('UPDATE interests SET embedding=NULL')
            db.execute("INSERT INTO memory_maintenance VALUES ('fixture', '2026-09-01', 'completed', ?)",
                       (json.dumps({'months': [{'month': '2026-06', 'already_done': True}]}),))
        daily_maintenance(pipeline)
        assert pipeline.store.now_summary('fixture')
        assert all(i['embedding'] for i in pipeline.store.list_interests('fixture'))
        with pipeline.store.connect() as db:
            tr = json.loads(db.execute("SELECT trace_json FROM memory_maintenance WHERE local_day='2026-09-01'").fetchone()[0])
        assert tr['months'] == [{'month': '2026-06', 'already_done': True}]
        assert tr['now_summary']
        before = len(pipeline.client.calls)
        daily_maintenance(pipeline)
        assert len(pipeline.client.calls) == before


@pytest.mark.parametrize('mode', ['sync', 'async'])
def test_slim_judges_preserve_packet_sources_and_active_plans(pipeline, mode):
    pipeline.config.pipeline.post_check = mode
    sid = pipeline.store.create_session('fixture')['id']
    for i in range(12):
        pipeline.store.add_canon('fixture', 'daily_life', f'日常{i}')
    pipeline.store.create_plan(sid, 'カフェ')
    pipeline.client = FakeLLMClient(json_responses=[{'domains_touched': [{'id': 'CBS09', 'strength': 'direct'}],
                'memory_use': [dict(ref='canon:12', relevant_now='yes', counterevidence='no', continuation='no')]}, problems(EMPTY), {'episodes': [], 'updates': []}], extract_responses=[extraction(EMPTY)])
    result = pipeline.run(sid, 'カフェの話')
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(result['turn_id'])
    pre = json.loads(trace['Pre-Judge']['request']['messages'][0]['content'])
    assert {'id','status'} <= set(pre['domains'][0]) <= {'id','label','status','description'}
    assert 'label' not in pre['domains'][0]  # Exact author copies now live only in the stable prefix.
    assert set(pre['signatures'][0]) == {'id', 'when'}
    assert set(pre['life_history_elements'][0]) == {'id', 'description'}
    post = json.loads(trace['Check']['request']['messages'][0]['content'])
    assert 'author_model' not in post and post['packet'] == trace['Packet']['output']
    assert 12 in {f['id'] for f in post['canon_facts']} and len(post['canon_facts']) <= 7
    assert post['plans'][0]['state'] == 'proposed'


def test_episodizer_interest_failure_never_blocks_turn_and_can_retry(pipeline):
    with clock.at('2026-10-01 20:00'):
        sid = pipeline.store.create_session('fixture')['id']
        pipeline.store.add_turn(sid, '関心', 'お香が好き', {})
        pipeline.client.episodizer_responses = ['invalid JSON']
        pipeline.close_conversation(sid)
        pipeline.wait_for_steward()
        assert pipeline.store.memory_jobs(retry=True)
        pipeline.maintenance()
        pipeline.wait_for_steward()
        assert not pipeline.store.memory_jobs()
        assert pipeline.run(sid, 'こんにちは')['assistant_text']


def test_retrieval_only_gates_interests_outside_foreground(pipeline):
    from cbs.memory import retrieve
    express(pipeline, '2026-08-01 20:00')
    with clock.at('2026-10-01 20:00'):
        pipeline.store.fade_interests('fixture', 45)
        candidates = retrieve(pipeline, '前のお香は？', [])
        interests = [i for i in candidates if i['ref'].startswith('interest:')]
        assert len(interests) == 1 and interests[0]['status'] == 'fading'
        assert 'embedding' not in interests[0] and 'evidence' not in interests[0]


@pytest.mark.parametrize('mode', ['sync', 'async'])
def test_reply_embedding_failure_keeps_packet_and_warns_in_both_modes(pipeline, mode):
    from cbs.llm import LLMError
    class FailedEmbedding(FakeLLMClient):
        def embed(self, texts, model=None, *, role="document"):
            raise LLMError('fake embedding failure')
    pipeline.config.pipeline.post_check = mode
    sid = pipeline.store.create_session('fixture')['id']
    for i in range(8):
        pipeline.store.add_canon('fixture', 'daily_life', f'日常{i}')
    pipeline.store.create_plan(sid, 'カフェ')
    pipeline.client = FailedEmbedding(json_responses=[{'memory_use': [dict(ref='canon:8', relevant_now='yes', counterevidence='no', continuation='no')]}, problems(EMPTY)], extract_responses=[extraction(EMPTY)])
    result = pipeline.run(sid, '日常の話')
    pipeline.wait_for_steward()
    tr = pipeline.store.get_trace(result['turn_id'])
    assert tr['Check']['warnings'] and not tr['Check'].get('error')
    post = json.loads(tr['Check']['request']['messages'][0]['content'])
    assert post['packet'] == tr['Packet']['output']
    assert post['canon_facts'][0]['id'] == 8 and post['plans'][0]['state'] == 'proposed'


def test_default_rule_needs_three_days_over_a_week(pipeline):
    pipeline.store.interest_rules = {'min_days': 3, 'min_span_days': 7}
    first = express(pipeline, '2026-09-01 20:00')
    assert express(pipeline, '2026-09-02 20:00', 'reinforce', first['id'])['status'] == 'tentative'
    assert express(pipeline, '2026-09-04 20:00', 'reinforce', first['id'])['status'] == 'tentative'  # 3 days, span 3
    assert express(pipeline, '2026-09-08 20:00', 'reinforce', first['id'])['status'] == 'established'  # 4 days, span 7
