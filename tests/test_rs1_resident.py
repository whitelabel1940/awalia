"""RS1 uses only the isolated pipeline fixture and FakeLLMClient."""
import pytest
from fastapi.testclient import TestClient

from cbs import clock
from cbs.config import AutonomyConfig
from cbs.llm import FakeLLMClient
from cbs.web import create_app


@pytest.mark.parametrize('seconds', [0, 45, 90, 3600])
def test_startup_delay_config(seconds):
    assert AutonomyConfig(resident_opening_delay_seconds=seconds).resident_opening_delay_seconds == seconds


@pytest.mark.parametrize('seconds', [-1, 3601, float('inf')])
def test_startup_delay_rejects_invalid(seconds):
    with pytest.raises(ValueError):
        AutonomyConfig(resident_opening_delay_seconds=seconds)


@pytest.mark.parametrize('start,end,quiet,awake', [
    (0, 8, '2026-10-25 07:59', '2026-10-25 08:00'),
    (22, 8, '2026-10-25 23:00', '2026-10-26 08:00'),
    (10, 12, '2026-10-25 11:00', '2026-10-25 12:00'),
])
def test_hidden_opening_defers_then_records_even_when_silent(pipeline, start, end, quiet, awake):
    pipeline.config.autonomy.quiet_start_hour = start
    pipeline.config.autonomy.quiet_end_hour = end
    pipeline.config.autonomy.idle_nudge = True
    app = create_app(pipeline, resident_token='test-owner', settings_dir=pipeline.store.path.parent)
    with TestClient(app, client=('127.0.0.1', 1)) as client:
        with clock.at(quiet):
            sid = pipeline.store.create_session('fixture')['id']
            body = {'session_id': sid, 'after_turn_id': 0, 'background': True}
            assert client.post('/api/proactive/opening', json=body).status_code == 403
            result = client.post('/api/proactive/opening', json=body, headers={'X-Awalia-Desktop': 'test-owner'}).json()
            assert result['deferred'] and not result['speak']
            with pipeline.store.connect() as db:
                assert db.execute('SELECT * FROM web_openings').fetchall() == []
            assert pipeline.client.calls == []
        with clock.at(awake):
            result = client.post('/api/proactive/opening', json=body, headers={'X-Awalia-Desktop': 'test-owner'}).json()
            assert not result['speak'] and not result.get('deferred')
            with pipeline.store.connect() as db:
                assert db.execute('SELECT opened_at FROM web_openings WHERE session_id=?', (sid,)).fetchone()[0] == clock.iso()
            pipeline.store.add_turn(sid, '少し話そう', 'うん。', {})
        with clock.at(clock.parse(awake).replace(minute=31)):
            pipeline.client = FakeLLMClient(proactive_responses=[{'speak': False, 'reason': '今は静かに過ごす'}])
            pipeline.proactive(sid, 'idle')
            assert any(c.get('schema', {}).get('title') == 'Proactive' for c in pipeline.client.calls)


@pytest.mark.parametrize('opening', ['normal', 'off'])
def test_explicit_opening_keeps_quiet_hours_behavior_and_records(pipeline, opening):
    pipeline.config.autonomy.opening = opening
    with clock.at('2026-10-25 01:00'):
        sid = pipeline.store.create_session('fixture')['id']
        with TestClient(create_app(pipeline, settings_dir=pipeline.store.path.parent)) as client:
            result = client.post('/api/proactive/opening', json={'session_id': sid}).json()
        assert not result.get('deferred')
        with pipeline.store.connect() as db:
            assert db.execute('SELECT opened_at FROM web_openings WHERE session_id=?', (sid,)).fetchone()[0] == clock.iso()
