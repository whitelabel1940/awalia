"""Saved expressions and quiet-hour gates only; no LLM generation on face polling."""
import pytest
from fastapi.testclient import TestClient
from cbs import clock
from cbs.web import create_app

TOKEN = 'face-test-owner'
HEADERS = {'X-Awalia-Desktop': TOKEN}


def test_face_owner_only_and_saved_expression_order(pipeline, monkeypatch):
    monkeypatch.setattr(pipeline, 'maintenance', lambda **kwargs: None)
    sid = pipeline.store.create_session('fixture')['id']
    latest = pipeline.store.add_turn(sid, '', 'こんにちは。', {}, kind='proactive',
                                    created_at='2026-10-21T12:00:00+09:00', assistant_at='2026-10-21T12:00:00+09:00', expression='shy')
    pipeline.store.add_turn(sid, '古い会話', '返答', {}, created_at='2026-01-01', assistant_at='2026-01-01', expression='sad')
    pipeline.store.add_turn(sid, '表情待ち', '返答', {}, created_at='2026-10-22', expression=None)
    app = create_app(pipeline, resident_token=TOKEN)
    before = len(pipeline.client.calls)
    with TestClient(app, client=('127.0.0.1', 1)) as client:
        assert client.get('/api/resident/face').status_code == 403
        assert client.get('/api/resident/face', headers={'X-Awalia-Desktop': 'wrong'}).status_code == 403
        with clock.at('2026-10-21T12:00:00+09:00'):
            value = client.get('/api/resident/face', headers=HEADERS).json()
        assert value == {'characterId': 'fixture', 'name': pipeline.character.model.display_name,
                         'expression': 'shy', 'revision': latest, 'quiet': False, 'validForMs': 15000, 'fallbackExpression': 'neutral', 'replyRemainingMs': 600000}
        assert len(pipeline.client.calls) == before
    with TestClient(app, client=('192.0.2.1', 1)) as client:
        assert client.get('/api/resident/face', headers=HEADERS).status_code == 403


@pytest.mark.parametrize('start,end,at,quiet,valid', [
    (22, 8, '21:59:59.500', False, 500), (22, 8, '22:00:00', True, 15000),
    (22, 8, '07:59:59', True, 1000), (22, 8, '08:00:00', False, 15000),
    (10, 14, '12:00:00', True, 15000), (0, 0, '00:00:00', False, 15000),
])
def test_face_quiet_boundaries_and_empty_history(pipeline, start, end, at, quiet, valid):
    pipeline.config.autonomy.quiet_start_hour = start
    pipeline.config.autonomy.quiet_end_hour = end
    with TestClient(create_app(pipeline, resident_token=TOKEN), client=('127.0.0.1', 1)) as client:
        with clock.at(f'2026-10-21T{at}+09:00'):
            value = client.get('/api/resident/face', headers=HEADERS).json()
        assert value['expression'] == 'neutral' and value['revision'] == 0
        assert value['quiet'] is quiet and value['validForMs'] == valid
