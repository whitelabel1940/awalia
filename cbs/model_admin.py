import argparse
import json
from pathlib import Path

from cbs.character import load_character
from cbs.config import load_config
from cbs.paths import resolve_paths
from cbs.store import Store


def main():
    parser = argparse.ArgumentParser(description="人物像の変更履歴・無効化（履歴と経験は削除しない）")
    parser.add_argument("command", choices=["list", "revert"])
    parser.add_argument("version", type=int, nargs="?")
    parser.add_argument("--db", type=Path, default=resolve_paths()[0] / "cbs.sqlite3")
    args = parser.parse_args()
    character = load_character(load_config().character.id)
    store = Store(args.db)
    if args.command == "revert":
        if args.version is None:
            parser.error("revert には変更版の ID が必要です。")
        try:
            print(f"invalidation record: {store.revert_model(character, args.version)}")
        except ValueError as error:
            parser.error(str(error))
    else:
        print(json.dumps(store.list_model_versions(character.model.character_id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
