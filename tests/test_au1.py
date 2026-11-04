import json

import pytest
from pydantic import ValidationError

from cbs import clock
from cbs.autonomy import ActivityUpdate, inner_life, inner_life_context
from cbs.pipeline import Extract
from cbs.store import Store


def activity(store, *, cid='fixture', interest_id=None, status='completed'):
    with store.connect() as db:
        run = db.execute("INSERT INTO inner_life_runs(character_id,created_at,action,reason,status,trace_json) VALUES (?,?,'reflect','test','completed','{}')",
                         (cid, clock.iso())).lastrowid
        return db.execute("INSERT INTO activities(character_id,run_id,kind,topic,summary,created_at,status,interest_id) VALUES (?,?,'reflect','topic','summary',?,?,?)",
                          (cid, run, clock.iso(), status, interest_id)).lastrowid


def curiosity(store, *, topic='問いの話題', detail='なぜそうなるのか？', status='tentative', origin='activity'):
    with store.connect() as db:
        return db.execute("INSERT INTO interests(character_id,topic,kind,detail,origin,status,first_seen,last_seen) VALUES ('fixture',?,'curiosity',?,?,?,?,?)",
                          (topic, detail, origin, status, clock.iso(), clock.iso())).lastrowid


def evidence(store, iid, aid=None, tid=None, revision=0):
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO interest_evidence(interest_id,source_activity,source_turn,local_day,revision) VALUES (?,?,?,?,?)',
                   (iid, aid, tid, clock.now().date().isoformat(), revision))


def questions(store, values, *, cid='fixture', source='activity:1'):
    with store.connect() as db:
        store.save_research_questions(db, cid, values, 'activity', source, '活動から')


def execute(pipeline, **fields):
    pipeline.client.inner_life_responses.append({'action': 'research', 'reason': 'この問いを追いたい', **fields})
    inner_life(pipeline)


def test_curiosity_requires_recent_real_evidence_and_status(pipeline):
    s = pipeline.store
    with clock.at('2026-10-11 09:00'):
        old = curiosity(s, detail='古い問い')
        evidence(s, old, activity(s))
    with clock.at('2026-10-26 09:00'):
        good = curiosity(s)
        evidence(s, good, activity(s))
        no_evidence = curiosity(s, detail='根拠なし')
        fading = curiosity(s, detail='薄れた問い', status='fading')
        evidence(s, fading, activity(s))
        failed = curiosity(s, detail='失敗の根拠')
        evidence(s, failed, activity(s, status='failed'))
        author = curiosity(s, detail='作者の問い', origin='author', status='established')
        evidence(s, author, activity(s))
        candidates = s.research_candidates('fixture')
        assert {c['interest_id'] for c in candidates} == {good, author}
        assert all(c['source_kind'] == 'interest' and c['source_ref'] == f"interest:{c['interest_id']}" for c in candidates)
        assert s.research_candidates('other') == []
        assert s.research_candidates('fixture') == candidates


def test_candidate_expiry_consumed_dedup_and_config_cap(pipeline):
    s, cfg = pipeline.store, pipeline.config.autonomy
    cfg.research_candidate_max = 2
    with clock.at('2026-10-12 09:00'):
        questions(s, ['問いA', '問いA'])
        questions(s, ['問いB', '問いC'], source='activity:2')
        candidates = s.research_candidates('fixture')
        assert {c['question'] for c in candidates} == {'問いA', '問いB'}
        questions(s, ['問いA'], cid='other')
        assert len(s.research_candidates('other')) == 1
        with s.connect() as db:
            db.execute("UPDATE research_candidates SET status='used' WHERE id=?", (candidates[0]['id'],))
        questions(s, [candidates[0]['question']], source='activity:3')
        assert len(s.research_candidates('fixture')) == 1
    with clock.at('2026-10-26 08:59'):
        assert len(s.research_candidates('fixture')) == 1
    with clock.at('2026-10-26 09:00'):
        assert s.research_candidates('fixture') == []
        cfg.research_candidate_expiry_days = 1
        questions(s, ['新しい問い', 'もう一つ'])
        cfg.research_candidate_max = 1
        assert len(s.research_candidates('fixture')) == 1
        cfg.research_candidate_max = 0
        assert s.research_candidates('fixture') == []
    with clock.at('2026-10-27 09:00'):
        assert s.research_candidates('other') == []


def test_selected_question_goes_to_search_and_normal_result_chain(pipeline):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-26 09:00'):
        iid = curiosity(s)
        evidence(s, iid, activity(s))
        selected = s.research_candidates('fixture')[0]
        p.client.activity_responses = [{'interests': [{'operation': 'reinforce', 'interest_id': iid,
            'topic': '問いの話題', 'kind': 'curiosity', 'detail': '次も知りたい'}], 'research_questions': ['次の問いは？']}]
        execute(p, research_candidate_id=selected['id'], task='この文で元の問いを置き換えない')
        call = next(c for c in p.client.calls if c.get('grounded'))
        payload = json.loads(call['messages'][0]['content'])
        assert payload['question'] == selected['question']
        assert payload['source'] == {k: selected[k] for k in ('source_kind', 'source_ref', 'source_note')}
        a = s.list_activities('fixture')[0]
        assert a['status'] == 'completed' and a['interest_id'] == iid
        assert a['sources'] and a['queries']
        assert any(e['source_activity'] == a['id'] for i in s.list_interests('fixture') if i['id'] == iid for e in i['evidence'])
        assert any(t['kind'] == 'share' and t['source_ref'] == a['ref'] for t in s.list_threads('fixture'))
        with s.connect() as db:
            assert db.execute('SELECT status,used_activity FROM research_candidates WHERE id=?', (selected['id'],)).fetchone()[:] == ('used', a['id'])
            assert db.execute("SELECT 1 FROM note_entries WHERE kind='activity' AND source_id=?", (a['id'],)).fetchone()
        assert any(c['question'] == '次の問いは？' and c['source_ref'] == a['ref'] for c in s.research_candidates('fixture'))


@pytest.mark.parametrize('invalid', ['unknown', 'other', 'expired', 'used'])
def test_unavailable_candidate_id_never_searches(pipeline, invalid):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-26 09:00'):
        questions(s, ['問い'], cid='other' if invalid == 'other' else 'fixture')
        with s.connect() as db:
            candidate_id = db.execute('SELECT id FROM research_candidates').fetchone()[0]
            if invalid in ('expired', 'used'):
                db.execute('UPDATE research_candidates SET status=?', (invalid,))
        execute(p, research_candidate_id=99999 if invalid == 'unknown' else candidate_id)
        assert not any(c.get('grounded') for c in p.client.calls)
        assert not s.list_activities('fixture')


def test_failed_or_budget_limited_search_leaves_question_open(pipeline):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-26 09:00'):
        questions(s, ['問い'])
        selected = s.research_candidates('fixture')[0]
        p.config.autonomy.research_daily_max = 0
        execute(p, research_candidate_id=selected['id'])
        assert s.list_inner_life('fixture')[0]['status'] == 'limited'
        assert len(s.research_candidates('fixture')) == 1
        p.config.autonomy.research_daily_max = 3
        p.client.grounded_responses = [{'text': '未確認', 'queries': [], 'sources': []}]
        execute(p, research_candidate_id=selected['id'])
        assert len(s.research_candidates('fixture')) == 1
        assert s.autonomy_budget('fixture')['research_remaining'] == 2
        assert not s.list_threads('fixture')


def test_conversation_extract_and_activity_questions(pipeline):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-26 09:00'):
        p.client.extract_responses = [{'ledger_updates': [], 'new_canon': [], 'disclosures': [], 'used_memories': [],
                                      'research_questions': ['会話から知りたくなったことは？']}]
        turn = p.run(s.create_session('fixture')['id'], 'こんにちは')
        p.wait_for_steward()
        c = next(c for c in s.research_candidates('fixture') if c['source_kind'] == 'conversation')
        assert c['source_ref'] == f"turn:{turn['turn_id']}"
        assert c['source_note'] == '会話の後に生まれた問い'
        p.client.activity_responses = [{'interests': [], 'research_questions': ['活動からの問いは？']}]
        p.client.text_responses = ['考えて、別のことも知りたくなった。']
        p.client.inner_life_responses = [{'action': 'reflect', 'reason': '考える', 'topic': 'topic'}]
        inner_life(p)
        c = next(c for c in s.research_candidates('fixture') if c['question'] == '活動からの問いは？')
        assert c['source_kind'] == 'activity'
        assert c['source_ref'] == s.list_activities('fixture')[0]['ref']


def test_coverage_unions_activity_and_evidence_counts_and_passes_all_interests(pipeline):
    p, s = pipeline, pipeline.store
    with clock.at('2026-10-18 09:00'):
        iid = curiosity(s)
        evidence(s, iid, activity(s, interest_id=iid))
    with clock.at('2026-10-19 09:00'):
        boundary = activity(s, interest_id=iid)
    with clock.at('2026-10-23 09:00'):
        aid = activity(s, interest_id=iid)
        evidence(s, iid, aid)
        evidence(s, iid, aid, revision=1)
    with clock.at('2026-10-24 09:00'):
        sid = s.create_session('fixture')['id']
        tid = s.add_turn(sid, '問い', '考えた', {})
        evidence(s, iid, tid=tid)
    with clock.at('2026-10-26 09:00'):
        activity(s, interest_id=iid, status='failed')
        empty = curiosity(s, detail='未着手')
        for n in range(8):
            curiosity(s, detail=f'ほか{n}')
        coverage = s.interest_activity_coverage('fixture')
        assert coverage[iid] == {'handled_last_7_days': 2, 'days_since_last_handled': 2}
        assert coverage[empty] == {'handled_last_7_days': 0, 'days_since_last_handled': None}
        context, interests = inner_life_context(p)
        assert len(context['interests']) == len(interests) > 6
        item = next(i for i in context['interests'] if i['id'] == iid)
        assert item['handled_last_7_days'] == 2 and item['days_since_last_handled'] == 2
        assert s.interest_activity_coverage('other') == {}


@pytest.mark.parametrize('values', [['a', 'b', 'c'], ['x' * 281], ['  ']])
def test_question_shape_bounds_both_outputs(values):
    with pytest.raises(ValidationError):
        ActivityUpdate(research_questions=values)
    with pytest.raises(ValidationError):
        Extract(research_questions=values, ledger_updates=[], new_canon=[], disclosures=[], used_memories=[])


def test_store_reopen_preserves_candidates_and_additive_migration(pipeline):
    s = pipeline.store
    with clock.at('2026-10-26 09:00'):
        questions(s, ['保存される問い'])
        before = s.research_candidates('fixture')
        # Simulate a v5 activities table without the new optional link.
        with s.connect() as db:
            db.execute('ALTER TABLE activities DROP COLUMN interest_id')
            db.execute('PRAGMA user_version=5')
        reopened = Store(s.path, characters_root=pipeline.characters_root, current_character_id='fixture')
        assert reopened.research_candidates('fixture') == before
        with reopened.connect() as db:
            assert 'interest_id' in {r[1] for r in db.execute('PRAGMA table_info(activities)')}


def test_curiosity_status_and_deleted_interest_do_not_leave_stale_candidates(pipeline):
    s = pipeline.store
    with clock.at('2026-10-26 09:00'):
        iid = curiosity(s)
        aid = activity(s, interest_id=iid)
        evidence(s, iid, aid)
        assert s.research_candidates('fixture')
        with s.connect() as db:
            db.execute("UPDATE interests SET status='fading' WHERE id=?", (iid,))
        assert not s.research_candidates('fixture')
        with s.connect() as db:
            db.execute('DELETE FROM interest_evidence WHERE interest_id=?', (iid,))
            db.execute('DELETE FROM interest_changes WHERE interest_id=?', (iid,))
            db.execute('DELETE FROM interests WHERE id=?', (iid,))
            assert db.execute('SELECT interest_id FROM activities WHERE id=?', (aid,)).fetchone()[0] is None
            assert not db.execute('SELECT 1 FROM research_candidates WHERE interest_id=?', (iid,)).fetchone()
