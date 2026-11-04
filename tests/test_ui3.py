"""UI3 settings compatibility and week-bounded public notes, using isolated Fake clients."""
import json
import tomllib
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cbs.config import AutonomyConfig, load_config
from cbs.web import create_app
from cbs.web.settings_api import atomic_write


@pytest.mark.parametrize('winerror', [5, 32, 33])
def test_settings_save_recovers_from_temporary_windows_file_lock(tmp_path, monkeypatch, winerror):
    target = tmp_path / 'settings.toml'
    target.write_text('previous', encoding='utf-8')
    replace, attempts, waits = Path.replace, [], []

    def temporarily_locked(source, destination):
        attempts.append(source)
        if len(attempts) <= 2:
            assert target.read_text(encoding='utf-8') == 'previous'
            error = PermissionError('temporary Windows lock')
            error.winerror = winerror
            raise error
        return replace(source, destination)

    monkeypatch.setattr(Path, 'replace', temporarily_locked)
    monkeypatch.setattr('cbs.web.settings_api.time.sleep', waits.append)
    atomic_write(target, 'saved')
    assert target.read_text(encoding='utf-8') == 'saved'
    assert not target.with_suffix('.toml.tmp').exists()
    assert len(attempts) == 3 and 0 < sum(waits) < 1


@pytest.mark.parametrize('winerror', [5, None])
def test_settings_save_keeps_previous_file_on_persistent_failure(tmp_path, monkeypatch, winerror):
    target = tmp_path / 'settings.toml'
    target.write_text('previous', encoding='utf-8')
    attempts, waits = [], []

    def locked(source, destination):
        attempts.append(source)
        error = PermissionError('file remains locked')
        error.winerror = winerror
        raise error

    monkeypatch.setattr(Path, 'replace', locked)
    monkeypatch.setattr('cbs.web.settings_api.time.sleep', waits.append)
    with pytest.raises(PermissionError):
        atomic_write(target, 'unsaved')
    assert target.read_text(encoding='utf-8') == 'previous'
    assert len(attempts) <= 5 and sum(waits) < 1
    if winerror is None:
        assert len(attempts) == 1 and not waits


@pytest.mark.parametrize('opening,idle', [('many', False), ('normal', False), ('reserved', True), ('off', True), ('off', False)])
def test_shared_talk_switch_legacy_and_saved_settings(pipeline, opening, idle):
    p = pipeline
    p.config.autonomy = AutonomyConfig(opening=opening, idle_nudge=idle)
    with TestClient(create_app(p)) as client:
        legacy = client.get('/api/settings').json()['autonomy']
        assert legacy['opening'] == opening
        # The existing opening preference determines the unified checkbox.
        enabled = legacy['opening'] != 'off'
        saved = client.put('/api/settings', json={'autonomy': {'opening': opening, 'idle_nudge': enabled}})
        assert saved.status_code == 200
        assert p.config.autonomy.idle_nudge is enabled
        assert p.config.autonomy.idle_rule() == ({'many': (20, 6), 'normal': (30, 4), 'reserved': (90, 2)}.get(opening))
        path = p.store.path.parent / 'settings.toml'
        assert tomllib.loads(path.read_text(encoding='utf-8'))['autonomy'] == {'opening': opening, 'idle_nudge': enabled}
        restored = load_config(data_dir=p.store.path.parent).autonomy
        assert restored.opening == opening and restored.idle_nudge is enabled
        for preference, enabled in [('off', False), ('reserved', True)]:
            # Legacy opening-only requests also save both sides of the switch.
            assert client.put('/api/settings', json={'autonomy': {'opening': preference}}).status_code == 200
            assert p.config.autonomy.idle_nudge is enabled
        for invalid in [{'opening': 'off', 'idle_nudge': True}, {'opening': 'normal', 'idle_nudge': False},
                        {'opening': 'normal', 'idle_nudge': 1}, {'opening': 'invalid'}]:
            assert client.put('/api/settings', json={'autonomy': invalid}).status_code == 422
        assert p.config.autonomy.opening == 'reserved' and p.config.autonomy.idle_nudge
    assert not p.client.calls


def add_activity(db, cid, day, title, *, status='completed', note=True):
    run = db.execute("INSERT INTO inner_life_runs (character_id,created_at,action,reason,status,trace_json) "
                     "VALUES (?,?,'research','','completed','{}')", (cid, day)).lastrowid
    aid = db.execute("INSERT INTO activities (character_id,run_id,kind,topic,summary,created_at,status) "
                     "VALUES (?,?,'research','','',?,?)", (cid, run, day + 'T00:05:00+09:00', status)).lastrowid
    if note:
        text = json.dumps({'title': title, 'summary': '読んだこと', 'impression': '気になったようだ。'})
        db.execute("INSERT INTO note_entries (character_id,kind,source_id,text,created_at) VALUES (?,'activity',?,?,?)",
                   (cid, aid, text, day))
    return aid


def test_activity_archive_counts_and_week_fetch(pipeline):
    p, monday = pipeline, date(2026, 10, 19)
    cid = p.config.character.id
    with p.store.connect() as db:
        for w in range(60):
            start = monday - timedelta(weeks=w)
            for d in (0, 6):
                add_activity(db, cid, (start + timedelta(days=d)).isoformat(), f'週{w}日{d}')
        for i in range(34):
            add_activity(db, cid, monday.isoformat(), f'追加{i}')
        add_activity(db, 'other', monday.isoformat(), '別の人物')
        add_activity(db, cid, monday.isoformat(), '未完了', status='running')
        add_activity(db, cid, monday.isoformat(), 'ノート作成前', note=False)
        # An old body must not be deserialized by the initial public-note request.
        old = add_activity(db, cid, '2024-12-30', '古い本文')
        db.execute("UPDATE note_entries SET text='not JSON' WHERE kind='activity' AND source_id=?", (old,))
    with TestClient(create_app(p)) as client:
        note = client.get('/api/note').json()
        weeks = note['activity_weeks']
        assert len(weeks) == 61
        assert weeks[0] == {'week_start': '2026-10-19', 'count': 36, 'loaded': True}
        assert [w['loaded'] for w in weeks] == [True] * 4 + [False] * 57
        assert len(note['activities']) == 42  # Whole weeks; no old 30-row truncation.
        assert min(a['date'] for a in note['activities']) == '2026-09-28'
        assert not any(a['title'] in ('別の人物', '未完了', 'ノート作成前') for a in note['activities'])
        older = client.get('/api/note/activities?week=2026-09-21').json()['activities']
        assert [a['date'] for a in older] == ['2026-09-27', '2026-09-21']
        assert [a['title'] for a in older] == ['週4日6', '週4日0']
        assert len(client.get('/api/note/activities?week=2026-10-19').json()['activities']) == 36
        assert client.get('/api/note/activities?week=2020-01-06').json() == {'activities': []}
        for week in ('2026-10-20', '2026-02-30', '20261019', 'not-a-date', '9999-12-27'):
            assert client.get('/api/note/activities', params={'week': week}).status_code == 422
        assert client.get('/api/note/activities').status_code == 422
    assert not p.client.calls


def test_activity_weeks_calendar_boundaries_and_empty(pipeline):
    p = pipeline
    with TestClient(create_app(p)) as client:
        empty = client.get('/api/note').json()
        assert empty['activity_weeks'] == [] and empty['activities'] == []
        with p.store.connect() as db:
            for day in ('2025-12-28', '2025-12-29', '2026-01-01', '2026-01-04', '2026-01-05'):
                add_activity(db, p.config.character.id, day, day)
        note = client.get('/api/note').json()
        assert [(w['week_start'], w['count']) for w in note['activity_weeks']] == [
            ('2026-01-05', 1), ('2025-12-29', 3), ('2025-12-22', 1)]
        rows = client.get('/api/note/activities?week=2025-12-29').json()['activities']
        assert [a['date'] for a in rows] == ['2026-01-04', '2026-01-01', '2025-12-29']
