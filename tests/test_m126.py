"""Resident reaction windows and persistent moods, using the existing Fake update."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cbs import clock
from cbs.expression import EXPRESSIONS
from cbs.inner import BASELINE, InnerUpdate, update_inner
from cbs.store import Store
from cbs.web import create_app


def mood(pipeline, expression, trigger='activity:1'):
    pipeline.client.inner_update_responses.append({
        'feeling': {'sentence': '静かに考えている。', 'levels': BASELINE},
        'wants': [], 'current_expression': expression})
    update_inner(pipeline, trigger, {'kind': 'activity', 'summary': '考えをまとめた。'})


def reply(pipeline, sid, expression, at):
    return pipeline.store.add_turn(sid, 'こんにちは', 'こんにちは。', {},
                                   expression=expression, created_at=at, assistant_at=at)


def test_reaction_window_mood_updates_unchanged_newest_reply_and_restart(pipeline):
    store = pipeline.store
    with clock.at('2026-10-21T12:00:00+09:00'):
        sid = store.create_session('fixture')['id']
        mood(pipeline, 'thinking')
        first = reply(pipeline, sid, 'smile', clock.iso())
        assert store.resident_expression('fixture') == {
            'expression': 'smile', 'revision': first,
            'fallbackExpression': 'thinking', 'replyRemainingMs': 600000}
    with clock.at('2026-10-21T12:05:00+09:00'):
        mood(pipeline, 'shy', 'activity:2')
        face = store.resident_expression('fixture')
        assert face['expression'] == 'smile' and face['fallbackExpression'] == 'shy'
        assert face['replyRemainingMs'] == 300000
    with clock.at('2026-10-21T12:09:59.999+09:00'):
        assert store.resident_expression('fixture')['expression'] == 'smile'
    with clock.at('2026-10-21T12:10:00+09:00'):
        assert store.resident_expression('fixture')['expression'] == 'shy'
        assert store.resident_expression('fixture')['replyRemainingMs'] is None
        newer = reply(pipeline, sid, 'surprised', clock.iso())
        # An imported/late older turn must not take over simply because its ID is larger.
        reply(pipeline, sid, 'sad', '2026-10-21T11:00:00+09:00')
        assert store.resident_expression('fixture')['revision'] == newer
    with clock.at('2026-10-22T12:00:00+09:00'):
        mood(pipeline, 'unchanged', 'activity:3')
        reopened = Store(store.path, embedding_model=store.embedding_model)
        assert reopened.resident_expression('fixture')['expression'] == 'shy'
        assert reopened.resident_expression('other')['expression'] == 'neutral'
        mood(pipeline, 'neutral', 'activity:4')
        assert store.resident_expression('fixture')['expression'] == 'neutral'


def test_configurable_reaction_falls_back_neutral_and_face_poll_never_calls_llm(pipeline, monkeypatch):
    monkeypatch.setattr(pipeline, 'maintenance', lambda **kwargs: None)
    pipeline.config.autonomy.resident_expression_neutral_minutes = 2
    token = 'm126-owner'
    with clock.at('2026-10-21T12:00:00+09:00'):
        sid = pipeline.store.create_session('fixture')['id']
        reply(pipeline, sid, 'pout', clock.iso())
    count = len(pipeline.client.calls)
    with TestClient(create_app(pipeline, resident_token=token), client=('127.0.0.1', 1)) as client:
        for at, expected, remaining in [('12:01:59', 'pout', 1000), ('12:02:00', 'neutral', None)]:
            with clock.at(f'2026-10-21T{at}+09:00'):
                face = client.get('/api/resident/face', headers={'X-Awalia-Desktop': token}).json()
                assert face['expression'] == expected and face['replyRemainingMs'] == remaining
                assert face['fallbackExpression'] == 'neutral'
    assert len(pipeline.client.calls) == count
    with pytest.raises(ValidationError):
        type(pipeline.config.autonomy)(resident_expression_neutral_minutes=0)


@pytest.mark.parametrize('expression', EXPRESSIONS)
def test_same_inner_update_selects_expression_and_checks_public_line(pipeline, expression):
    with clock.at('2026-10-21T12:00:00+09:00'):
        before = len(pipeline.client.calls)
        mood(pipeline, expression)
        calls = pipeline.client.calls[before:]
        assert [c['schema']['title'] for c in calls] == ['InnerUpdate', 'Monologue', 'VoiceCheck']
        assert [c['model'] for c in calls] == [pipeline.config.models.steward, pipeline.config.models.actor, pipeline.config.models.judge]
        assert 'current_expression' in calls[0]['schema']['properties']
        assert json.loads(calls[0]['messages'][0]['content'])['current_expression'] == 'neutral'
        assert pipeline.store.resident_expression('fixture')['expression'] == expression
        # Invalid structured output must preserve the previous mood and leave the turn usable.
        mood(pipeline, 'not-an-expression', 'activity:bad')
        assert pipeline.store.list_inner_updates('fixture')[-1]['status'] == 'failed'
        assert pipeline.store.resident_expression('fixture')['expression'] == expression


def test_legacy_inner_schema_migrates_without_losing_feelings(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript("""
            PRAGMA user_version=2;
            CREATE TABLE inner_states (id INTEGER PRIMARY KEY, character_id TEXT NOT NULL,
                created_at TEXT NOT NULL, sentence TEXT NOT NULL, levels_json TEXT NOT NULL,
                trigger_ref TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'llm');
        """)
        db.execute('INSERT INTO inner_states VALUES (1,?,?,?,?,?,?)',
                   ('fixture', clock.iso(), '今の気持ち', json.dumps(BASELINE), 'event:legacy', 'llm'))
    store = Store(path)
    with store.connect() as db:
        row = db.execute('SELECT * FROM inner_states').fetchone()
        assert row['sentence'] == '今の気持ち' and row['current_expression'] is None
        from cbs.backup import SCHEMA_VERSION
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    assert store.resident_expression('fixture')['expression'] == 'neutral'


def test_time_decay_does_not_clear_or_invent_an_inner_expression(pipeline):
    with clock.at('2026-10-21T12:00:00+09:00'):
        mood(pipeline, 'thinking')
        with pipeline.store.connect() as db:
            db.execute('UPDATE inner_states SET levels_json=?',
                       (json.dumps({'energy': '高い', 'social': '高い', 'concern': '強い'}),))
    with clock.at('2026-10-23T12:00:00+09:00'):
        assert pipeline.store.current_feeling('fixture')['origin'] == 'time'
        assert pipeline.store.resident_expression('fixture')['expression'] == 'thinking'


@pytest.mark.parametrize('source,expression', [
    ('reply_window', 'smile'), ('inner_mood', 'thinking'), ('neutral_fallback', 'neutral'),
])
def test_packet_actor_and_check_share_shown_face_snapshot(pipeline, monkeypatch, source, expression):
    monkeypatch.setattr(pipeline, 'maintenance', lambda **kwargs: None)
    with clock.at('2026-10-21T12:00:00+09:00'):
        sid = pipeline.store.create_session('fixture')['id']
        if source != 'neutral_fallback':
            mood(pipeline, 'thinking')
        if source == 'reply_window':
            reply(pipeline, sid, 'smile', clock.iso())
        pipeline.client.calls.clear()
        pipeline.client.text_responses.append('顔に出てた？ 少し考えていたんだ。')
        result = pipeline.run(sid, 'どうしたの？')
        pipeline.wait_for_steward()
        expected = {'expression': expression, 'source': source}
        trace = pipeline.store.get_trace(result['turn_id'])
        assert trace['Packet']['output']['shown_face'] == expected
        actor = next(c for c in pipeline.client.calls if 'schema' not in c and 'packet.shown_face' in c['system'])
        assert json.loads(actor['messages'][0]['content'])['packet']['shown_face'] == expected
        check = next(c for c in pipeline.client.calls if c.get('schema', {}).get('title') == 'Check')
        assert json.loads(check['messages'][0]['content'])['packet']['shown_face'] == expected
        assert 'packet.shown_face' in actor['system'] and 'packet.shown_face' in check['system']
        assert not any(c.get('schema', {}).get('title') == 'InnerUpdate' for c in pipeline.client.calls)
