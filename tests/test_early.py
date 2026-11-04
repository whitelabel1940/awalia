"""EX1: real state and delayed delivery, with FakeLLMClient only."""
import json
from threading import Event

import pytest
from fastapi.testclient import TestClient

from cbs import clock
from cbs.config import EarlyConfig
from cbs.llm import FakeLLMClient
from cbs.note import self_forming
from cbs.steward import StewardOutput, apply_output
from cbs.web import create_app
from test_steward import episode, update, DIRECT, EMPTY
from response_helpers import problems, extraction


def project(store, character, *, source_turn=None, notify=True, mode='enacted'):
    from types import SimpleNamespace
    from cbs.note import fill_forming_notes
    client = FakeLLMClient(scene_responses=[{'operations': [{'action': 'add', 'scene_id': None,
        'text': '好意には照れると言っていた' if mode == 'hypothetical' else '好意には照れるみたい'}]}])
    pipeline = SimpleNamespace(store=store, character=character, client=client, _safe_trace=lambda x: x,
        config=SimpleNamespace(character=SimpleNamespace(id=character.model.character_id),
                               models=SimpleNamespace(steward='fake'), early=SimpleNamespace(notify=notify)))
    return fill_forming_notes(pipeline, source_turn=source_turn)


def record(store, character, domain='CBS10', mode='enacted', interpretation='first_observation', notify=True):
    sid = store.create_session(character.model.character_id)['id']
    tid = store.add_turn(sid, '好き', 'ありがとう', {})
    out = StewardOutput(episodes=[episode(mode, domain)], updates=[update(target=domain, interpretation=interpretation)])
    apply_output(store, character, tid, out, notify=notify)
    project(store, character, source_turn=tid, notify=notify, mode=mode)
    return tid


def test_note_history_modes_changes_and_legacy(store, character):
    with clock.at('2026-10-20 12:00'):
        tid = record(store, character, mode='hypothetical')
    with clock.at('2026-10-21 12:00'):
        record(store, character, interpretation='differentiation')
        record(store, character, interpretation='reinforcement')
    note = self_forming(store, character)
    assert len(note) == 1 and note[0]['scenes'][0]['evidence'][0]['source_turn'] == tid
    assert note[0]['scenes'][0]['text'].endswith('と言っていた')
    assert note[0]['scenes'][0]['dates'] == ['2026-10-20'] and note[0]['scenes'][0]['evidence'][0]['summary']
    assert len(note[0]['scenes']) == 3 and note[0]['scenes'][1]['text'].endswith('みたい')
    # Legacy diffs still have their original proposal in steward_runs.
    with store.connect() as db:
        for r in db.execute('SELECT id,diff_json FROM model_versions').fetchall():
            diff = json.loads(r['diff_json'])
            for change in diff['changes']:
                for key in ('interpretation', 'known', 'conditions'):
                    change.pop(key, None)
            db.execute('UPDATE model_versions SET diff_json=? WHERE id=?', (json.dumps(diff), r['id']))
    assert self_forming(store, character) == note


def test_notice_daily_cap_off_restart_and_character_isolation(store, character):
    with clock.at('2026-10-20 12:00'):
        first = record(store, character)
        second = record(store, character, 'CBS14')
        assert store.forming_notice(first) and not store.forming_notice(second)
    with clock.at('2026-10-21 12:00'):
        reinforced = record(store, character, interpretation='change')
        assert not store.forming_notice(reinforced)
        off = record(store, character, 'CBS20', notify=False)
        assert not store.forming_notice(off)
        character.model.character_id = 'other'
        other = record(store, character)
        assert store.forming_notice(other)
    from cbs.store import Store
    assert Store(store.path).forming_notice(first)


def test_notice_uses_reply_day_even_when_steward_crosses_midnight(store, character):
    with clock.at('2026-10-20 23:59'):
        sid = store.create_session('fixture')['id']
        tid = store.add_turn(sid, '好き', 'ありがとう', {})
    with clock.at('2026-10-21 00:01'):
        apply_output(store, character, tid, StewardOutput(episodes=[episode()], updates=[update()]))
        project(store, character, source_turn=tid)
    with store.connect() as db:
        assert db.execute('SELECT day FROM forming_notices').fetchone()[0] == '2026-10-20'


def test_early_window_caps_and_open_only(store, character):
    cfg = EarlyConfig()
    with clock.at('2026-10-20 12:00'):
        assert store.early_candidates(character, cfg)
        record(store, character)
        assert 'CBS10' not in {d['id'] for d in store.early_candidates(character, cfg)}
    with clock.at('2026-10-22 23:59'):
        assert store.early_candidates(character, cfg)
    with clock.at('2026-10-23 00:00'):
        assert not store.early_candidates(character, cfg)
    sid = store.create_session('fixture')['id']
    conv = store.prepare_conversation(sid, clock.iso())
    with clock.at('2026-10-21 12:00'), store.connect() as db:
        for day in range(20, 23):
            db.execute('INSERT INTO proactive_runs (character_id,session_id,conversation_id,mode,anchor,created_at,speak,early_domain,source_turn) VALUES (?,?,?,?,?,?,?,?,?)',
                       ('fixture', sid, conv, 'opening', day, f'2026-10-{day}T12:00:00+09:00', 1, 'CBS14', 1))
    with clock.at('2026-10-21 13:00'):
        assert not store.early_candidates(character, cfg)
        assert store.early_candidates(character, cfg.model_copy(update={'daily_max': 2, 'total_max': 4}))
        assert not store.early_candidates(character, cfg.model_copy(update={'days': 0}))


def test_proactive_selection_and_caps_do_not_enter_ordinary_reply(pipeline):
    pipeline.config.autonomy.inner_life = False
    pipeline.client = FakeLLMClient(proactive_responses=[{'speak': True, 'reason': '軽い自分の話', 'early_domain': 'CBS14'}],
        text_responses=['今日は風が気持ちよかった。', 'よかったね。'], json_responses=[problems(EMPTY), {}, problems(EMPTY)])
    with clock.at('2026-10-20 12:00'):
        sid = pipeline.store.create_session('fixture')['id']
        result = pipeline.proactive(sid)
        pipeline.wait_for_steward()
        assert result['speak'] and result['early_domain'] == 'CBS14'
        assert not pipeline.store.early_candidates(pipeline.character, pipeline.config.early)
        reply = pipeline.run(sid, '今日はコーヒーがおいしかった。')
        pipeline.wait_for_steward()
    packet = pipeline.store.get_trace(reply['turn_id'])['Packet']['output']
    assert 'proactive' not in packet and 'early_candidates' not in packet


@pytest.mark.parametrize('choice', [
    {'early_domain': 'not-a-candidate'},
    {'early_domain': 'CBS14', 'want_ref': 'missing'},
    {'early_domain': 'CBS14', 'private_topic': True},
])
def test_proactive_rejects_invalid_selection_without_consuming_cap(pipeline, choice):
    pipeline.client = FakeLLMClient(proactive_responses=[{'speak': True, 'reason': 'candidate', **choice}])
    with clock.at('2026-10-20 12:00'):
        result = pipeline.proactive(pipeline.store.create_session('fixture')['id'])
        assert not result['speak']
        assert pipeline.store.early_candidates(pipeline.character, pipeline.config.early)


def test_idle_early_candidate_can_compete_without_share(pipeline):
    pipeline.config.autonomy.idle_nudge = True
    with clock.at('2026-10-20 10:00'):
        sid = pipeline.store.create_session('fixture')['id']
        pipeline.proactive(sid)  # Records the time the screen opened; Fake chooses silence.
        pipeline.store.add_turn(sid, 'のんびりしているよ。', 'そうなんだ。', {})
    pipeline.client = FakeLLMClient(proactive_responses=[{'speak': True, 'reason': 'light topic', 'early_domain': 'CBS14'}],
        text_responses=['川沿いを歩いてみたいな。'], json_responses=[problems(EMPTY)])
    with clock.at('2026-10-20 12:00'):
        result = pipeline.proactive(sid, 'idle')
        pipeline.wait_for_steward()
        assert result['speak'] and result['early_domain'] == 'CBS14'
        assert not pipeline.store.early_candidates(pipeline.character, pipeline.config.early)


def test_idle_silence_keeps_llm_reason(pipeline):
    pipeline.config.autonomy.idle_nudge = True
    with clock.at('2026-10-20 10:00'):
        sid = pipeline.store.create_session('fixture')['id']
        pipeline.proactive(sid)
        pipeline.store.add_turn(sid, '今は休みたい。', 'ゆっくり休んでね。', {})
    pipeline.client = FakeLLMClient(proactive_responses=[{'speak': False, 'reason': '相手が休む時間を大切にする'}])
    with clock.at('2026-10-20 12:00'):
        result = pipeline.proactive(sid, 'idle')
    assert not result['speak'] and result['reason'] == '相手が休む時間を大切にする'


def test_delayed_notice_attaches_to_original_reply(pipeline):
    entered, release = Event(), Event()
    class BlockingFake(FakeLLMClient):
        def generate_json(self, model, system, messages, schema):
            if schema is StewardOutput:
                entered.set()
                assert release.wait(10)
            return super().generate_json(model, system, messages, schema)
    pipeline.client = BlockingFake(json_responses=[DIRECT, problems(EMPTY),
        {'episodes': [episode()], 'updates': [update()]}], extract_responses=[extraction(EMPTY)])
    events = []
    with clock.at('2026-10-20 12:00'):
        result = pipeline.run(pipeline.store.create_session('fixture')['id'], '好き', event_sink=lambda kind, data: events.append((kind, data)))
        try:
            assert entered.wait(5)
            assert any(kind == 'reply' for kind, _ in events)
            assert not any(kind == 'self_forming' for kind, _ in events)
        finally:
            release.set()
        pipeline.wait_for_expression(result['turn_id'])
        pipeline.wait_for_notice(result['turn_id'])
    assert [data for kind, data in events if kind == 'self_forming'] == [{'turn_id': result['turn_id']}]
    assert [kind for kind, _ in events].index('reply') < [kind for kind, _ in events].index('self_forming')


def test_sse_history_and_settings(pipeline, tmp_path):
    pipeline.client = FakeLLMClient(json_responses=[DIRECT, problems(EMPTY),
        {'episodes': [episode()], 'updates': [update()]}], extract_responses=[extraction(EMPTY)])
    app = create_app(pipeline=pipeline, settings_dir=tmp_path)
    with TestClient(app) as client:
        sid = pipeline.store.create_session('fixture')['id']
        r = client.post(f'/api/sessions/{sid}/messages/stream', json={'content': '好き'})
        assert r.status_code == 200 and r.text.index('event: reply') < r.text.index('event: self_forming')
        history = client.get(f'/api/sessions/{sid}/turns').json()
        assert history[0]['self_forming']
        assert client.get('/api/note').json()['self_forming']
        assert client.put('/api/settings', json={'early': {'notify': False}}).status_code == 200
        assert not client.get(f'/api/sessions/{sid}/turns').json()[0]['self_forming']
        assert client.get('/api/settings').json()['early']['notify'] is False
        assert 'false' in (tmp_path / 'settings.toml').read_text(encoding='utf-8')
