from fastapi.testclient import TestClient

from cbs.run_scenario import run_scenario
from cbs.web import create_app


def test_web_endpoints(pipeline):
    with TestClient(create_app(pipeline)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        session = client.post("/api/sessions", json={"title": "テスト"}).json()
        assert client.get("/api/sessions").json()[0]["id"] == session["id"]
        response = client.post(f"/api/sessions/{session['id']}/messages", json={"content": "こんにちは"})
        assert response.status_code == 200
        result = response.json()
        assert client.get(f"/api/turns/{result['turn_id']}/trace").json()["Actor"]["model"] == pipeline.config.models.actor
        assert len(client.get(f"/api/sessions/{session['id']}/turns").json()) == 1
        assert len(client.get("/api/character").json()["model"]["domains"]) == 34
        assert client.get("/api/character").json()["canon_facts"] == []
        assert {i['topic'] for i in client.get('/api/character').json()['interests']} == set(pipeline.character.expression.preferences)
        assert client.get('/api/model/changes').json()['interest_changes'][0]['operation'] == 'author'
        assert client.get("/api/ledger").json() == []
        plan = pipeline.store.create_plan(session["id"], "カフェ", "土曜日", "2026-10-17", result["turn_id"])
        pipeline.store.add_plan_event(session["id"], "agree", plan_id=plan["plan_id"], source_turn=result["turn_id"])
        fact = pipeline.store.add_canon("fixture", "daily_life", "紅茶を飲んだ", result["turn_id"])
        ledger = client.get("/api/ledger").json()[0]
        assert ledger["state"] == "agreed" and ledger["scheduled_date"] == "2026-10-17"
        assert [event["kind"] for event in ledger["events"]] == ["proposed", "agreed"]
        assert client.get("/api/character").json()["canon_facts"] == [fact]
        assert client.get("/api/turns/1000/trace").status_code == 404
        assert client.post("/api/sessions/missing/messages", json={"content": "x"}).status_code == 404
        assert client.post(f"/api/sessions/{session['id']}/messages", json={"content": " "}).status_code == 422


def test_scenario_markdown(pipeline, tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text('name: テスト\nmessages: ["こんにちは"]\n', encoding="utf-8")
    report = run_scenario(path, pipeline, tmp_path / "runs")
    content = report.read_text(encoding="utf-8")
    assert "## Turn 1" in content
    assert "### Pre-Judge" in content
    assert '"emotional_weight": "low"' in content
    assert "### Packet" in content
