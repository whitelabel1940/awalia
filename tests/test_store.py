import sqlite3

import pytest

from cbs.store import SCHEMA, Store


def test_all_tables_and_reopen(store):
    tables = {"sessions", "turns", "ledger_events", "canon_facts", "episodes", "model_versions", "steward_runs"}
    with store.connect() as connection:
        assert tables <= {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    session = store.create_session('fixture')
    first = store.add_turn(session["id"], "こんにちは", "うん", {"Load": {"output": "日本語"}})
    second = store.add_turn(session["id"], "またね", "またね", {"日本語": True})
    reopened = Store(store.path)
    assert reopened.list_sessions() == [session]
    assert [t["id"] for t in reopened.history(session["id"])] == [first, second]
    assert reopened.history(session["id"], limit=1)[0]["user_text"] == "またね"
    assert reopened.get_trace(first) == {"Load": {"output": "日本語"}}


def test_isolation_and_missing_ids(store):
    a, b = store.create_session('fixture'), store.create_session('fixture')
    store.add_turn(a["id"], "a", "reply", {})
    assert store.history(b["id"]) == []
    with pytest.raises(KeyError):
        store.get_trace(1000)
    with pytest.raises(KeyError):
        store.history("missing")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_turn("missing", "x", "y", {})


def test_legacy_db_migrates_in_place(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("INSERT INTO sessions VALUES ('old', 'fixture', '昔の会話', '2026-10-01T00:00:00+00:00')")
        connection.execute("INSERT INTO turns VALUES (1, 'old', 'こんにちは', 'うん', 'completed', '{}', '2026-10-01')")
        connection.execute("INSERT INTO ledger_events VALUES (1, 'old', 'proposed', NULL, 'カフェ', NULL, 1, '2026-10-01')")
        connection.execute("INSERT INTO ledger_events VALUES (2, 'old', 'agreed', NULL, 'カフェ', 1, 1, '2026-10-01')")
    migrated = Store(path)
    reopened = Store(path)
    with migrated.connect() as connection:
        assert {"plan_id", "scheduled_text", "scheduled_date"} <= {row[1] for row in connection.execute("PRAGMA table_info(ledger_events)")}
    assert reopened.history("old")[0]["assistant_text"] == "うん"
    assert reopened.get_trace(1) == {}
    plan = reopened.list_plans('fixture')[0]
    assert plan["plan_id"] == 1 and plan["state"] == "agreed"
    assert len(plan["events"]) == 2 and plan["scheduled_date"] is None


def test_plan_events_latest_state_and_character_isolation(store):
    a, b = store.create_session('fixture'), store.create_session('fixture')
    plan = store.create_plan(a["id"], "カフェ", "今度の土曜", "2026-10-17")
    for operation, state in (("agree", "agreed"), ("reschedule", "rescheduled"), ("cancel", "cancelled"), ("report_done", "reported_done")):
        kwargs = {"scheduled_text": "翌週", "scheduled_date": "2026-10-24"} if operation == "reschedule" else {}
        store.add_plan_event(b["id"], operation, plan_id=plan["plan_id"], **kwargs)
        latest = store.list_plans('fixture')[0]
        assert latest["state"] == state
        assert bool(store.list_plans('fixture', active_only=True)) == (state in ("agreed", "rescheduled"))
    assert [event["kind"] for event in latest["events"]] == ["proposed", "agreed", "rescheduled", "cancelled", "reported_done"]
    assert latest["scheduled_date"] == "2026-10-24"
    assert latest["content"] == "カフェ"
    other = store.create_session("other")
    with pytest.raises(KeyError):
        store.add_plan_event(other["id"], "agree", plan_id=plan["plan_id"])
    with pytest.raises(KeyError):
        store.add_plan_event(b["id"], "agree", plan_id=999)
    assert store.list_plans("other") == []


def test_canon_dedup_and_previous_session(store):
    first = store.create_session('fixture')
    other = store.create_session("other")
    second = store.create_session('fixture')
    third = store.create_session('fixture')
    assert store.previous_session_datetime(first["id"]) is None
    assert store.previous_session_datetime(second["id"]) == first["created_at"]
    assert store.previous_session_datetime(third["id"]) == second["created_at"]
    assert store.previous_session_datetime(other["id"]) is None
    saved = store.add_canon("fixture", "past_detail", "師匠の口癖は『まず一息』", life_ref="rel_binder")
    assert saved["provenance"] == "ai_generated"
    assert store.add_canon("fixture", "past_detail", saved["content"]) is None
    assert store.add_canon("fixture", "preference", saved["content"]) is not None
    store.add_canon("other", "past_detail", saved["content"])
    assert len(store.list_canon('fixture')) == 2 and len(store.list_canon("other")) == 1
