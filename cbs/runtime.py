from pathlib import Path

from cbs.character import load_character
from cbs.config import load_config
from cbs.llm import create_client
from cbs.store import Store


def read_prompt(name):
    return (Path(__file__).parent / "prompts" / f"{name}.md").read_text(encoding="utf-8")


def create_pipeline(character_id=None, *, db_path=None):
    from cbs.pipeline import TurnPipeline
    from cbs.config import ROOT
    from cbs.paths import resolve_paths
    from cbs.restore import apply_staged
    data_dir, characters_root = resolve_paths(Path(db_path).parent if db_path else None)
    apply_staged(data_dir, characters_root, db_path or data_dir / 'cbs.sqlite3', config_path=ROOT / 'config.toml')
    config = load_config(data_dir=data_dir)
    if character_id:
        config.character.id = character_id
    store = Store(db_path or config._data_dir / 'cbs.sqlite3', memory_config=config.memory,
                  characters_root=characters_root, current_character_id=config.character.id, config_path=ROOT / 'config.toml')
    pipeline = TurnPipeline(config, load_character(config.character.id, root=characters_root), store, create_client(config))
    from cbs.backup import BackupWorker
    store.backup_worker = BackupWorker(store, characters_root, store.path.parent, lambda: pipeline.config,
                                       config_path=ROOT / 'config.toml')
    pipeline.characters_root = characters_root
    return pipeline


def character_prefix(character, *, actor=False, catalog=False, store=None):
    """Stable author prefix, optionally followed by the current DB adjustments."""
    import json
    context = {"character_id": character.model.character_id, "display_name": character.model.display_name,
               "world": character.world, "expression": character.expression.model_dump(mode="json", by_alias=True),
               "initial_interests": character.interests}
    if actor:
        context = {"character_summary": character.model.summary,
                   "signatures": [s.model_dump(mode="json") for s in character.model.signatures], **context}
    if catalog:
        context = {"character_summary": character.model.summary,
                   "domain_catalog": [{"id": d.id, "label": d.label,
                                       "description": " ".join((d.known or getattr(d, "description", "") or "").split())[:160]}
                                      for d in character.model.domains], **context}
    from cbs.requests import prompt_context
    adjustment = prompt_context(store.current_requests(character)) if store is not None else ""
    return "\n\n" + read_prompt('character_context') + "\n\n" + json.dumps({"static_character": context}, ensure_ascii=False) + adjustment
