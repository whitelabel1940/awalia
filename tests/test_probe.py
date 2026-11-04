from cbs.config import ROOT
from response_helpers import problems, extraction
from cbs.llm import FakeLLMClient
from cbs import probe


def test_probe_report_checks_and_final_database_state(tmp_path, monkeypatch):
    suite = tmp_path / "suite.yaml"
    suite.write_text('probes:\n  - id: P2\n    category: 過去\n    expect: 細部の一貫性\n    sessions:\n      - ["口癖は？"]\n      - ["口癖は？"]\n', encoding="utf-8")
    minor = {"claims": [{"text": "まず一息", "verdict": "new_minor_detail", "reason": "軽微な細部"}],
             "new_canon": [{"kind": "past_detail", "content": "師匠の口癖は『まず一息』", "life_ref": None}],
             "ledger_updates": [{"operation": "agree", "plan_id": None, "content": "カフェ", "scheduled_text": "土曜", "scheduled_date": "2026-10-17"}]}
    supported = {"claims": [{"text": "まず一息", "verdict": "supported", "reason": "canon:1"}], "new_canon": [], "ledger_updates": []}
    client = FakeLLMClient(text_responses=["まず一息", "まず一息"], json_responses=[{}, problems(minor), {}, problems(supported)], extract_responses=[extraction(minor), extraction(supported)])
    monkeypatch.setattr(probe, "create_client", lambda config: client)
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    report = probe.run_suite(suite, "test", character_id="fixture", characters_root=ROOT / "tests/fixtures/characters")
    text = report.read_text(encoding="utf-8")
    assert "| チェック |" in text and "抽出細部+1" in text
    assert "保存された Canon:" in text and "canon:1 | past_detail | 師匠の口癖は『まず一息』" in text
    assert "予定:" in text and "plan:1 | カフェ | agreed | 土曜 / 2026-10-17" in text
    assert "agreed (Turn 1)" in text
