"""Trace retention and lossless-dimension float32 storage."""
import json
import math
import struct
import sqlite3
import zlib
from contextlib import closing
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


def compact_trace(value, *, keep_packet=False):
    """Preserve decisions and the trace shapes read by Inspector/probes.

    Packet.input is the Reveal gate history. Never remove or reconstruct it.
    Retention compacts only finished jobs. Backup drops requests but preserves
    unfinished Packet.output verbatim for delayed Extract.
    """
    if isinstance(value, list):
        return [compact_trace(v, keep_packet=keep_packet) for v in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in ("request", "pre_rewrite_text", "raw_response"):
            continue
        if key == "response" and isinstance(item, dict):
            result[key] = {"parsed": compact_trace(item.get("parsed"), keep_packet=keep_packet)}
        elif key in ("Load", "Packet", "Commit") and isinstance(item, dict):
            result[key] = {k: compact_trace(v, keep_packet=keep_packet) for k, v in item.items()
                           if k not in ("input", "output") or (key == "Packet" and (k == "input" or keep_packet)) or
                           (key == "Commit" and (k == "output" or keep_packet))}
            if key == "Commit" and keep_packet and "input" in item:
                result[key]["input"] = item["input"]
            if key == "Packet":
                if "input" in item:
                    result[key]["input"] = item["input"]
                if keep_packet and "output" in item:
                    result[key]["output"] = item["output"]
            if key == "Packet" and not keep_packet and item.get("output", {}).get("secrets"):
                result[key]["output"] = {"secrets": [{k: v for k, v in secret.items() if k in
                    ("secret_id", "allowed_level", "disclosed_level")} for secret in item["output"]["secrets"]]}
        else:
            result[key] = compact_trace(item, keep_packet=keep_packet)
    return result


TRACE_MAGIC = b'CBSZ1\0'
TRACE_COLUMNS = frozenset(('trace_json', 'input_json', 'before_json', 'after_json'))


def trace_text(value):
    """Old plain JSON and versioned compressed JSON share the same read path."""
    if isinstance(value, bytes) and value.startswith(TRACE_MAGIC):
        return zlib.decompress(value[len(TRACE_MAGIC):]).decode('utf-8')
    return value


def pack_trace(text):
    if text is None:
        return None
    text = trace_text(text)
    raw = text.encode('utf-8')
    if len(raw) < 128:
        return text
    packed = TRACE_MAGIC + zlib.compress(raw)
    return packed if len(packed) < len(raw) else text


def dump_trace(value):
    return pack_trace(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def trace_row(cursor, values):
    # Keep sqlite.Row and the public *_json strings used by Inspector/probes.
    return sqlite3.Row(cursor, tuple(trace_text(v) if d[0] in TRACE_COLUMNS else v
                                    for d, v in zip(cursor.description, values)))


# table: (trace column, timestamp expression, joins, finished predicate).
# Explicit policies, tested against the schema, make new trace tables visible.
TRACE_POLICIES = {
    'turns': ('trace_json', 't.created_at', '', "t.status<>'running'"),
    'memory_jobs': ('trace_json', 'c.last_activity_at', 'JOIN conversations c ON c.id=t.conversation_id', "t.status IN ('completed','failed')"),
    'inner_life_runs': ('trace_json', 't.created_at', '', "t.status<>'running'"),
    'inner_update_runs': ('trace_json', 't.created_at', '', "t.status<>'running'"),
    'proactive_runs': ('trace_json', 't.created_at', '', '1'),
    'episode_link_runs': ('trace_json', 'e.created_at', 'JOIN memory_episodes e ON e.id=t.source_episode', "t.status='completed'"),
    'memory_maintenance': ('trace_json', 't.local_day', '', "t.status<>'running'"),
    'diaries': ('trace_json', 't.created_at', '', '1'),
    'steward_runs': ('input_json', 't.created_at', '', '1'),
    'knowing_runs': ('trace_json', 't.created_at', '', "t.status<>'running'"),
    'import_steps': ('trace_json', 'c.last_activity_at', 'JOIN memory_jobs j ON j.id=t.job_id JOIN conversations c ON c.id=j.conversation_id', 't.reflected=1'),
    # This input is author-supplied augmentation, not an LLM request. Never trim it.
    'bootstrap_augments': ('input_json', 't.created_at', '', '0'),
}


def pending_trace(value):
    return isinstance(value, dict) and (value.get('followups_pending') or
        any(isinstance(v, dict) and v.get('state') == 'pending' for v in value.values()))


def rewrite_traces(db, *, days=2, backup=False):
    """Compact eligible history, losslessly pack the rest. Works on old schemas.

    Backup removes requests even for unfinished work, retaining its Packet and
    parsed decisions. It never needs provider requests to resume local stages.
    """
    from cbs.shutdown import checkpoint
    cutoff = clock.now() - timedelta(days=days)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows, saved = 0, 0
    for table, (column, at, joins, finished) in TRACE_POLICIES.items():
        if table not in tables:
            continue
        checkpoint()
        # Read identifiers only; fetch the current value under the write lock so
        # late Extract/expression writes cannot be overwritten by an old snapshot.
        ids = [r[0] for r in db.execute(f'SELECT rowid FROM {table}')]
        for ident in ids:
            checkpoint()
            with db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute(f'SELECT t.{column}, {at}, {finished} FROM {table} t {joins} WHERE t.rowid=?', (ident,)).fetchone()
                if row is None:
                    continue
                original, timestamp, finished_row = row
                if table == 'bootstrap_augments':
                    encoded = pack_trace(original)  # Preserve the author's exact input bytes.
                else:
                    value = json.loads(trace_text(original))
                    ready = bool(finished_row) and not pending_trace(value)
                    if backup or (ready and clock.parse(timestamp) < cutoff):
                        value = {} if table == 'steward_runs' else compact_trace(value, keep_packet=not ready)
                    encoded = dump_trace(value)
                # Exports contain ordinary compact JSON, readable by old BK1 tools.
                if backup:
                    encoded = trace_text(encoded)
                if encoded != original:
                    db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?', (encoded, ident))
                    saved += len(original if isinstance(original, bytes) else original.encode('utf-8')) - len(encoded if isinstance(encoded, bytes) else encoded.encode('utf-8'))
                    rows += 1
                checkpoint()
    # Undo journals embed trace_json strings. Apply the same compact representation
    # to both snapshots so undo comparisons continue to match compacted rows.
    if 'import_changes' in tables:
        ids = [r[0] for r in db.execute('SELECT id FROM import_changes')]
        for ident in ids:
            checkpoint()
            with db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT before_json,after_json FROM import_changes WHERE id=?', (ident,)).fetchone()
                for column, original in zip(('before_json', 'after_json'), row):
                    if original is None:
                        continue
                    value = json.loads(trace_text(original))
                    if 'trace_json' in value:
                        value['trace_json'] = json.dumps(compact_trace(json.loads(trace_text(value['trace_json']))), ensure_ascii=False, separators=(',', ':'))
                    encoded = dump_trace(value)
                    if backup:
                        encoded = trace_text(encoded)
                    if encoded != original:
                        rows += 1
                        saved += len(original if isinstance(original, bytes) else original.encode('utf-8')) - len(encoded if isinstance(encoded, bytes) else encoded.encode('utf-8'))
                        db.execute(f'UPDATE import_changes SET {column}=? WHERE id=?', (encoded, ident))
                checkpoint()
    return {'rows': rows, 'bytes_removed': saved}


def vacuum_traces(db, stop=None):
    from cbs.shutdown import checkpoint, UpkeepStopped
    checkpoint()
    db.set_progress_handler(lambda: bool(stop and stop.is_set()), 1)
    try:
        db.execute('VACUUM')
    except sqlite3.OperationalError:
        if stop and stop.is_set():
            raise UpkeepStopped() from None
        raise
    finally:
        db.set_progress_handler(None, 0)
    checkpoint()


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

    def trim_traces(self, days=2, *, vacuum=True):
        # Each row commits separately. Shutdown never loses the completed prefix.
        with closing(sqlite3.connect(self.path, timeout=.2)) as db:
            result = rewrite_traces(db, days=days)
            if vacuum and result['bytes_removed']:
                vacuum_traces(db, self.upkeep_stop)
        return result

    def shrink_once(self, days=2):
        """Background R13 conversion. Mark done only after a successful VACUUM."""
        from cbs.shutdown import checkpoint
        checkpoint()
        with closing(sqlite3.connect(self.path, timeout=.2)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS storage_migrations (name TEXT PRIMARY KEY)')
            if db.execute("SELECT 1 FROM storage_migrations WHERE name='trace-zlib-v1'").fetchone():
                return
        self.trim_traces(days, vacuum=False)
        checkpoint()
        with closing(sqlite3.connect(self.path, timeout=.2)) as db:
            vacuum_traces(db, self.upkeep_stop)
        checkpoint()
        with closing(sqlite3.connect(self.path, timeout=.2)) as db, db:
            db.execute("INSERT OR IGNORE INTO storage_migrations VALUES ('trace-zlib-v1')")
            checkpoint()
        return True
