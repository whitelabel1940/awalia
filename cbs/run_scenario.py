import argparse
from cbs import clock
from datetime import timezone
import json
from pathlib import Path

from pydantic import BaseModel, Field
import yaml

from cbs.config import ROOT
from cbs.llm import LLMError
from cbs.runtime import create_pipeline


class Scenario(BaseModel):
    name: str
    messages: list[str] = Field(min_length=1)


def run_scenario(path, pipeline, output_dir=ROOT / "runs"):
    scenario = Scenario.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    session = pipeline.store.create_session(pipeline.config.character.id, pipeline.client.sanitize_text(scenario.name))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = clock.now().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = output_dir / f"{Path(path).stem}_{stamp}_{session['id'][:8]}.md"
    lines = [f"# {session['title']}", "", f"Session: `{session['id']}`", ""]
    try:
        for number, message in enumerate(scenario.messages, 1):
            result = pipeline.run(session["id"], message)
            pipeline.wait_for_steward()
            trace = pipeline.store.get_trace(result["turn_id"])
            lines += [f"## Turn {number} (DB: {result['turn_id']})", "", f"**ユーザー:** {pipeline.client.sanitize_text(message)}", "", f"**パートナー:** {result['assistant_text']}", "", "### Pre-Judge", "", "```json", json.dumps(trace["Pre-Judge"].get("response", {}).get("parsed"), ensure_ascii=False, indent=2), "```", "", "### Trace summary", ""]
            for step in ("Load", "Pre-Judge", "Packet", "Actor", "Check", "Rewrite", "Check-2", "Extract", "Commit", "Steward"):
                if step not in trace:
                    continue
                item = trace[step]
                lines.append(f"- {step}: {item.get('model', 'code')} / {item.get('latency_ms', 0)} ms / usage={json.dumps(item.get('usage', {}), ensure_ascii=False)}")
            lines += ["", "### Packet", "", "```json", json.dumps(trace["Packet"]["output"], ensure_ascii=False, indent=2), "```", ""]
    except LLMError:
        lines += ["## 実行失敗", "", "未完了ターンのトレースはDBに保存されています。"]
        raise
    finally:
        output.write_text("\n".join(lines), encoding="utf-8")
        print(f"report: {output}")
    return output


def main():
    parser = argparse.ArgumentParser(description="シナリオを新しいセッションで実行")
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        run_scenario(args.file, create_pipeline())
    except LLMError as error:
        print(error)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
