"""Rebuild every saved embedding atomically with the configured model."""
from cbs.llm.cost import for_step
import argparse
import json
from pathlib import Path

from cbs.config import load_config, embedding_model
from cbs.llm import create_client
from cbs.storage import pack_vector
from cbs.store import Store


def reindex(store, client, config, progress=None):
    from cbs.costs import bind
    bind(client, store, config)
    model = embedding_model(config)
    # Keep the old complete index on provider failure. The DB queue/foreground lock
    # prevents new memories from being added between the snapshot and its commit.
    def snapshot(db):
        entries = []
        for table, fields in (("memory_episodes", "summary || char(10) || details"),
                              ("user_facts", "content"), ("monthly_digests", "summary || char(10) || details"),
                              ("interests", "topic || char(10) || detail")):
            entries.extend((table, r[0], r[1]) for r in db.execute(f"SELECT id, {fields} FROM {table}"))
        return entries
    with store.connect() as db:
        entries = snapshot(db)
    vectors = []
    if progress:
        progress(0, len(entries))
    for start in range(0, len(entries), 32):
        batch = entries[start:start + 32]
        output = for_step(client, 'embedding', 'memory').embed([e[2] for e in batch], config.models.embedding, role="document")
        if len(output) != len(batch):
            raise ValueError("invalid embedding count")
        vectors.extend(pack_vector(v) for v in output)
        if progress:
            progress(len(vectors), len(entries))
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if snapshot(db) != entries:
            raise ValueError("作り直し中に記憶が更新されました。再試行してください。")
        for (table, pk, _), vector in zip(entries, vectors):
            db.execute(f"UPDATE {table} SET embedding=? WHERE id=?", (vector, pk))
        # Reply sources are cache entries: invalidate and regenerate on use.
        db.execute("DELETE FROM reply_embeddings")
        db.execute("UPDATE embedding_index SET model=? WHERE id=1", (model,))
    store.embedding_model = model
    return {"model": model, "completed": len(entries), "total": len(entries)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--embedding", help="Override the embedding model for this rebuild")
    args = parser.parse_args()
    config = load_config()
    if args.embedding:
        config.models.embedding = args.embedding
    store = Store(args.db, embedding_model=embedding_model(config)) if args.db else Store(embedding_model=embedding_model(config))
    print(json.dumps(reindex(store, create_client(config), config), ensure_ascii=False))


if __name__ == "__main__":
    main()
