"""Trace retention and lossless-dimension float32 storage."""
import json
import math
import struct
from datetime import timedelta

from cbs import clock
from cbs.config import equivalent_embedding_model

VECTOR_TABLES = ("memory_episodes", "user_facts", "monthly_digests", "interests", "reply_embeddings")


class EmbeddingModelMismatch(ValueError):
    pass


def pack_vector(vector):
    if not vector or not all(math.isfinite(v) for v in vector):
        raise ValueError("invalid embedding")
    return struct.pack("<" + "f" * len(vector), *vector)


def unpack_vector(value):
    if isinstance(value, str):  # Legacy imports/tests; migrated on next startup.
        return json.loads(value)
    return list(struct.unpack("<" + "f" * (len(value) // 4), value))


def compact_trace(value):
    """Preserve decisions and the trace shapes read by Inspector/probes.

    Packet.input is the Reveal gate history. Never remove or reconstruct it.
    Only finished jobs/turns are compacted; delayed Extract still needs Packet.output.
    """
    if isinstance(value, list):
        return [compact_trace(v) for v in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in ("request", "pre_rewrite_text", "raw_response"):
            continue
        if key == "response" and isinstance(item, dict):
            result[key] = {"parsed": compact_trace(item.get("parsed"))}
        elif key in ("Load", "Packet", "Commit") and isinstance(item, dict):
            result[key] = {k: compact_trace(v) for k, v in item.items()
                           if k not in ("input", "output") or (key == "Packet" and k == "input") or
                           (key == "Commit" and k == "output")}
            if key == "Packet" and item.get("output", {}).get("secrets"):
                result[key]["output"] = {"secrets": [{k: v for k, v in secret.items() if k in
                    ("secret_id", "allowed_level", "disclosed_level")} for secret in item["output"]["secrets"]]}
        else:
            result[key] = compact_trace(item)
    return result


class StorageStore:
    def migrate_vectors(self, db, model):
        db.execute("CREATE TABLE IF NOT EXISTS embedding_index (id INTEGER PRIMARY KEY CHECK(id=1), model TEXT NOT NULL)")
        # Old Episodizer traces explicitly record the model used. A conflict cannot be silently mixed.
        models = {json.loads(r[0]).get("embedding", {}).get("model") for r in db.execute("SELECT trace_json FROM memory_jobs")}
        models.discard(None)
        models = {equivalent_embedding_model(m if "/" in m else model.split("/", 1)[0] + "/" + m) for m in models}
        recorded = next(iter(models)) if len(models) == 1 else ("mixed:reindex-required" if models else model)
        db.execute("INSERT OR IGNORE INTO embedding_index VALUES (1, ?)", (recorded,))
        for table in VECTOR_TABLES:
            for row in db.execute(f"SELECT rowid, embedding FROM {table} WHERE typeof(embedding)='text'").fetchall():
                if row[1]:
                    db.execute(f"UPDATE {table} SET embedding=? WHERE rowid=?", (pack_vector(unpack_vector(row[1])), row[0]))

    def require_embedding_model(self, model=None):
        wanted = model or self.embedding_model
        with self.connect() as db:
            self.check_embedding_model(db, wanted)

    def check_embedding_model(self, db, wanted=None, *, writing=False):
        if writing and not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        wanted = wanted or self.embedding_model
        recorded = db.execute("SELECT model FROM embedding_index WHERE id=1").fetchone()[0]
        if equivalent_embedding_model(recorded) != equivalent_embedding_model(wanted):
            raise EmbeddingModelMismatch("記憶の検索モデルが変更されています。python -m cbs.reindex で索引を作り直してください。")

    def trim_traces(self, days=14, *, vacuum=True):
        cutoff = clock.now() - timedelta(days=days)
        saved, changed = 0, 0
        with self.connect() as db:
            queries = {
                "turns": "SELECT id AS pk, created_at AS at, trace_json FROM turns WHERE status <> 'running'",
                "memory_jobs": "SELECT j.id AS pk, c.last_activity_at AS at, j.trace_json FROM memory_jobs j JOIN conversations c ON c.id=j.conversation_id WHERE j.status IN ('completed','failed')",
                "inner_life_runs": "SELECT id AS pk, created_at AS at, trace_json FROM inner_life_runs WHERE status<>'running'",
                "proactive_runs": "SELECT id AS pk, created_at AS at, trace_json FROM proactive_runs",
                "episode_link_runs": "SELECT r.source_episode AS pk, e.created_at AS at, r.trace_json FROM episode_link_runs r JOIN memory_episodes e ON e.id=r.source_episode WHERE r.status='completed'",
                "memory_maintenance": "SELECT rowid AS pk, local_day AS at, trace_json FROM memory_maintenance WHERE status<>'running'",
                "diaries": "SELECT rowid AS pk, created_at AS at, trace_json FROM diaries",
            }
            for table, query in queries.items():
                for row in db.execute(query).fetchall():
                    trace = json.loads(row["trace_json"])
                    if clock.parse(row["at"]) >= cutoff or any(isinstance(v, dict) and v.get("state") == "pending" for v in trace.values()):
                        continue
                    short = json.dumps(compact_trace(trace), ensure_ascii=False, separators=(",", ":"))
                    if len(short) < len(row["trace_json"]):
                        pk = "source_episode" if table == "episode_link_runs" else "rowid"
                        db.execute(f"UPDATE {table} SET trace_json=? WHERE {pk}=?", (short, row["pk"]))
                        saved += len(row["trace_json"].encode()) - len(short.encode())
                        changed += 1
            for row in db.execute("SELECT id, created_at, input_json FROM steward_runs").fetchall():
                if clock.parse(row["created_at"]) < cutoff and row["input_json"] != "{}":
                    saved += len(row["input_json"].encode()) - 2
                    db.execute("UPDATE steward_runs SET input_json='{}' WHERE id=?", (row["id"],))
                    changed += 1
        if vacuum and saved:
            with self.connect() as db:
                db.execute("VACUUM")
        return {"rows": changed, "bytes_removed": saved}
