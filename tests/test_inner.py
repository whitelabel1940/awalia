import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from cbs import clock
from cbs.autonomy import inner_life
from cbs.inner import BASELINE, InnerUpdate, update_inner, update_long_gap
from cbs.llm import FakeLLMClient
from cbs.note import public_note
from cbs.steward import submit
from cbs.store import Store
from cbs.web import create_app


def update(trigger='event:1', ref=None, **levels):
    return {'feeling': {'sentence': 'あの日の話がまだ気になってる。',
                       'levels': {'energy': '高い', 'social': '高い', 'concern': '強い', **levels}},
            'wants': [{'thread_ref': ref, 'direction': '気にかける', 'motive': '緊張していたから、どうなったか聞きたい。',
                       'topic': '面接', 'detail': '落ち着いたら結果を聞きたい', 'weight': .8, 'trigger_ref': trigger}]}


def apply(p, trigger='event:1', response=None):
    p.client.inner_update_responses.append(response or update(trigger))
    submit(p.store, update_inner, p, trigger, {'kind': 'conversation', 'summary': '面接の前に緊張していた。'}).result(timeout=10)


def test_baseline_decay_restart_and_legacy_migration(pipeline, tmp_path):
    s = pipeline.store
    with clock.at('2026-10-01 12:00'):
        apply(pipeline)
        assert s.list_threads('fixture')[0]['weight'] == .8
    with clock.at('2026-10-01 23:59'):
        assert s.current_feeling('fixture')['levels']['concern'] == '強い'
    with clock.at('2026-10-02 00:00'):
        state = s.current_feeling('fixture')
        assert state['levels'] == {'energy': 'ふつう', 'social': 'ふつう', 'concern': '少し'}
        assert state['origin'] == 'time' and state['trigger_ref'] == 'inner:1'
        assert len(s.list_inner_states('fixture')) == 2
    with clock.at('2026-10-03 12:00'):
        reopened = Store(s.path, embedding_model=s.embedding_model)
        assert reopened.current_feeling('fixture')['levels'] == BASELINE
        assert len(reopened.list_inner_states('fixture')) == 3
        assert reopened.list_threads('fixture')[0]['weight'] == pytest.approx(.8 * .9 ** 2)
        # Reading repeatedly, reopening, and expiring do not compound the decay.
        reopened.expire_threads('fixture', 14)
        assert reopened.list_threads('fixture')[0]['weight'] == pytest.approx(.648)
    legacy = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(legacy) as db:
        db.execute('CREATE TABLE threads (id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, kind TEXT NOT NULL, topic TEXT NOT NULL, detail TEXT NOT NULL, source_ref TEXT NOT NULL, weight REAL NOT NULL, due_at TEXT, status TEXT NOT NULL DEFAULT \'open\', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source_turn INTEGER, UNIQUE(character_id,kind,source_ref))')
        db.execute("INSERT INTO threads VALUES (1,'fixture','share','本','読んだ','activity:1',.8,NULL,'open','2026-10-01','2026-10-01',NULL)")
    with clock.at('2026-10-02 00:00'):
        t = Store(legacy, embedding_model=s.embedding_model).list_threads('fixture')[0]
        assert (t['direction'], t['motive'], t['trigger_ref']) == ('', '', '')
        assert t['weight'] == pytest.approx(.72)


def test_configurable_time_rules_and_character_isolation(pipeline):
    pipeline.config.inner.baseline_hours = 6
    pipeline.config.inner.want_decay_per_day = .2
    with clock.at('2026-10-01 12:00'):
        apply(pipeline, response=update(energy='低い', social='低い'))
    with clock.at('2026-10-02 12:00'):
        assert pipeline.store.current_feeling('fixture')['levels'] == BASELINE
        assert pipeline.store.list_threads('fixture')[0]['weight'] == pytest.approx(.64)
        assert pipeline.store.current_feeling('other') is None
        assert pipeline.store.list_monologues('other') == []


@pytest.mark.parametrize('field,value', [('energy', '最大'), ('social', 3), ('concern', 'ふつう')])
def test_slots_reject_unrecognized_values(field, value):
    with pytest.raises(ValueError):
        InnerUpdate.model_validate(update(**{field: value}))
    bad = update()
    bad['wants'][0]['direction'] = '呼び戻す'
    with pytest.raises(ValueError):
        InnerUpdate.model_validate(bad)


def test_update_references_history_and_no_reopening(pipeline):
    with clock.at('2026-10-01 12:00'):
        apply(pipeline)
        submit(pipeline.store, update_inner, pipeline, 'event:1', {}).result(timeout=10)
        assert len(pipeline.store.list_monologues('fixture')) == 1
        apply(pipeline, 'event:2', update('event:2', 'thread:1'))
        t = pipeline.store.list_threads('fixture', with_history=True)[0]
        assert len(pipeline.store.list_threads('fixture')) == 1
        assert t['direction'] == '気にかける' and t['trigger_ref'] == 'event:2'
        assert t['history'][-1]['before']['trigger_ref'] == 'event:1'
        bad = update('unknown', 'thread:999')
        apply(pipeline, 'event:3', bad)
        assert len(pipeline.store.list_threads('fixture')) == 1
        assert pipeline.store.list_inner_updates('fixture')[-1]['trace']['warnings']
        with pipeline.store.connect() as db:
            db.execute("UPDATE threads SET status='used' WHERE id=1")
        apply(pipeline, 'event:4', update('event:4', 'thread:1'))
        assert pipeline.store.list_threads('fixture')[0]['status'] == 'used'


def test_conversation_activity_gap_and_failure_do_not_stop_work(pipeline):
    with clock.at('2026-10-01 12:00'):
        sid = pipeline.store.create_session('fixture')['id']
        pipeline.run(sid, '面接がある')
        pipeline.wait_for_steward()
        pipeline.client.inner_update_responses.append('invalid json')
        pipeline.close_conversation(sid)
        pipeline.wait_for_steward()
        assert pipeline.store.list_inner_updates('fixture')[0]['status'] == 'failed'
        assert pipeline.store.list_memory_episodes('fixture')
        with pipeline.store.connect() as db:
            assert db.execute('SELECT status FROM memory_jobs').fetchone()[0] == 'completed'
    with clock.at('2026-10-02 11:59'):
        submit(pipeline.store, update_long_gap, pipeline).result(timeout=10)
        assert len(pipeline.store.list_inner_updates('fixture')) == 1
    with clock.at('2026-10-03 12:00'):
        pipeline.client.inner_life_responses.append({'action': 'reflect', 'reason': '考えたい', 'topic': '面接'})
        pipeline.client.text_responses.append('緊張がほぐれているといいなと考えた。')
        pipeline.client.inner_update_responses.extend([update('gap-unused'), 'invalid json'])
        submit(pipeline.store, inner_life, pipeline).result(timeout=10)
        updates = pipeline.store.list_inner_updates('fixture')
        assert updates[1]['trigger_ref'].startswith('gap:') and updates[1]['status'] == 'completed'
        assert updates[2]['trigger_ref'] == 'activity:1' and updates[2]['status'] == 'failed'
        assert pipeline.store.list_activities('fixture')[0]['status'] == 'completed'
        assert pipeline.store.list_inner_life('fixture')[0]['status'] == 'completed'
        submit(pipeline.store, update_long_gap, pipeline).result(timeout=10)
        assert len(pipeline.store.list_inner_updates('fixture')) == 3
    call = next(c for c in pipeline.client.calls if c.get('schema', {}).get('title') == 'InnerUpdate')
    context = json.loads(call['messages'][0]['content'])
    assert context['character_summary'] and context['signatures'] and context['demeanor']
    assert call['model'] == pipeline.config.models.steward


@pytest.mark.parametrize('speak', [True, False])
def test_proactive_chain_and_packet_expression(pipeline, speak):
    with clock.at('2026-10-01 12:00'):
        apply(pipeline)
        pipeline.client.proactive_responses.append({'speak': speak, 'reason': '気遣い', 'want_ref': 'thread:1',
            'why_now': '落ち着いて話せそうだから、少し聞いてみたい。' if speak else '休んでいそうだから、今はそっとしておきたい。'})
        sid = pipeline.store.create_session('fixture')['id']
        pipeline.client.json_responses = [{'problems': []}]
        result = pipeline.proactive(sid)
        pipeline.wait_for_steward()
        d = pipeline.store.list_proactive('fixture')[0]
        assert result['speak'] == speak and d['want_ref'] == 'thread:1' and d['why_now']
        ctx = json.loads(d['trace']['request']['messages'][0]['content'])
        assert ctx['feeling']['levels']['social'] == '高い'
        assert ctx['threads'][0]['motive']
        if speak:
            packet = pipeline.store.get_trace(result['turn_id'])['Packet']['output']
            assert packet['proactive']['want']['motive'] == update()['wants'][0]['motive']
            assert packet['feeling']['sentence']
            face = next(c for c in pipeline.client.calls if c.get('schema', {}).get('title') == 'Expression')
            assert json.loads(face['messages'][0]['content'])['feeling']['levels']['concern'] == '強い'


def test_normal_conversation_packet_off_and_eligibility(pipeline):
    with clock.at('2026-10-01 12:00'):
        apply(pipeline)
        sid = pipeline.store.create_session('fixture')['id']
        pipeline.config.autonomy.opening = 'off'
        assert not pipeline.proactive(sid)['speak']
        assert not any(c.get('schema', {}).get('title') == 'Proactive' for c in pipeline.client.calls)
        pipeline.config.autonomy.opening = 'normal'
        with pipeline.store.connect() as db:
            db.execute("UPDATE threads SET kind='follow_up',due_at='2026-10-05' WHERE id=1")
        pipeline.client.proactive_responses.append({'speak': True, 'want_ref': 'thread:1', 'reason': '聞く', 'why_now': '今聞きたい'})
        assert not pipeline.proactive(sid)['speak']
        result = pipeline.run(sid, 'こんにちは')
        pipeline.wait_for_steward()
        assert pipeline.store.get_trace(result['turn_id'])['Packet']['output']['feeling']['sentence']


def test_inner_public_note_and_setting(pipeline, tmp_path):
    with clock.at('2026-10-01 12:00'):
        pipeline.client.monologue_responses = [{'text': '少しほっとできているといいな。\nゆっくり待とう。'}]
        apply(pipeline)
        note = public_note(pipeline)
        assert note['today_haru'] == '少しほっとできているといいな。 ゆっくり待とう。'
        assert 'levels' not in json.dumps(note)
        with TestClient(create_app(pipeline, settings_dir=tmp_path / 'settings')) as web:
            dev = web.get('/api/inner').json()
            assert dev['feelings'][0]['trigger_ref'] == 'event:1'
            assert dev['wants'][0]['direction'] == '気にかける'
            assert 'inner-panel' in web.get('/dev').text
            assert not web.get('/api/inner?character_id=missing').status_code == 200
            response = web.put('/api/settings', json={'inner': {'show_monologue': False}})
            assert response.status_code == 200
            assert web.get('/api/note').json()['today_haru'] is None
            assert pipeline.store.list_monologues('fixture')  # visibility does not erase state
            assert web.put('/api/settings', json={'inner': {'baseline_hours': 0}}).status_code == 422
