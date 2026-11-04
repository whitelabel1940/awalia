"""python -m cbs.reveal_admin list|show|approve|regenerate|set"""
import argparse
import json
from pathlib import Path

import yaml

from cbs.character import load_character
from cbs.config import load_config
from cbs.paths import resolve_paths
from cbs.llm import create_client
from cbs.reveal import RevealTruth, generate_truth, secret_states
from cbs.store import Store


def main():
    parser = argparse.ArgumentParser(description="隠れた真相の管理（差し替え前の内容と開示記録を保存）")
    parser.add_argument("command", choices=["list", "show", "approve", "regenerate", "set"])
    parser.add_argument("id", nargs="?")
    parser.add_argument("yaml", type=Path, nargs="?")
    parser.add_argument("--db", type=Path, default=resolve_paths()[0] / "cbs.sqlite3")
    args = parser.parse_args()
    config = load_config()
    character, store = load_character(config.character.id), Store(args.db)
    if args.command != "list" and args.id not in {s.id for s in character.reveal.secrets}:
        parser.error("定義にある secret の ID を指定してください。")
    try:
        if args.command == "approve":
            store.approve_secret(config.character.id, args.id)
        elif args.command == "regenerate":
            result = generate_truth(character, store, create_client(config), config, args.id, replace=True, purpose='other')
            if result.get("error"):
                parser.error(result["error"])
        elif args.command == "set":
            if args.yaml is None:
                parser.error("set には L1 / L2 / L3 を含む YAML ファイルが必要です。")
            # Provider is constructed only for API operations. Read the credential solely to redact it.
            import os
            content = args.yaml.read_text(encoding="utf-8")
            key = os.environ.get(config.llm.api_key_env, "")
            if key:
                content = content.replace(key, "[REDACTED]")
            truth = RevealTruth.model_validate(yaml.safe_load(content))
            store.save_secret(config.character.id, args.id, truth.model_dump(), status="approved",
                              provenance="author", replace=True, action="set")
        states = secret_states(character, store, spoilers=args.command == "show")
        if args.command == "show":
            states["secrets"] = [s for s in states["secrets"] if s["secret_id"] == args.id]
            states["events"] = [e for e in states["events"] if e["secret_id"] == args.id]
        print(json.dumps(states, ensure_ascii=False, indent=2))
    except (ValueError, OSError) as error:
        parser.error("操作に失敗しました。ID と YAML の L1/L2/L3 を確認してください。")


if __name__ == "__main__":
    main()
