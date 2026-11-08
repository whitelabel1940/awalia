from cbs.storage import dump_trace
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from time import perf_counter
from uuid import uuid4

from cbs.paths import resolve_paths
from cbs.storage import StorageStore
from cbs import clock
from cbs.clock import iso as now
from cbs.memory_store import MemoryStore, SCHEMA as MEMORY_SCHEMA
from cbs.interests import InterestStore, SCHEMA as INTEREST_SCHEMA
from cbs.autonomy_store import AutonomyStore, SCHEMA as AUTONOMY_SCHEMA
from cbs.reading import ReadingStore, SCHEMA as READING_SCHEMA
from cbs.knowing import KnowingStore, SCHEMA as KNOWING_SCHEMA
from cbs.import_store import ImportStore, SCHEMA as IMPORT_SCHEMA
from cbs.requests import RequestStore, SCHEMA as REQUEST_SCHEMA
from cbs.days import DaysStore, SCHEMA as DAYS_SCHEMA

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_failures (
 provider TEXT PRIMARY KEY, kind TEXT NOT NULL, status_code INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, character_id TEXT NOT NULL, title TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
 id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 user_text TEXT NOT NULL, assistant_text TEXT, status TEXT NOT NULL,
 trace_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_events (
 id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 kind TEXT NOT NULL, target TEXT, content TEXT NOT NULL,
 parent_event INTEGER REFERENCES ledger_events(id), source_turn INTEGER REFERENCES turns(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canon_facts (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, kind TEXT NOT NULL,
 content TEXT NOT NULL, provenance TEXT NOT NULL DEFAULT 'ai_generated',
 source_turn INTEGER REFERENCES turns(id), life_ref TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, domain TEXT NOT NULL,
 situation TEXT NOT NULL, reaction TEXT NOT NULL, salient_conditions_json TEXT NOT NULL,
 source_turn INTEGER REFERENCES turns(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_versions (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, base_version INTEGER NOT NULL,
 diff_json TEXT NOT NULL, rationale TEXT NOT NULL, episode_refs_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steward_runs (
 id INTEGER PRIMARY KEY, source_turn INTEGER REFERENCES turns(id),
 input_json TEXT NOT NULL, output_json TEXT NOT NULL, application_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forming_notices (
 character_id TEXT NOT NULL, day TEXT NOT NULL, source_turn INTEGER NOT NULL REFERENCES turns(id),
 created_at TEXT NOT NULL, PRIMARY KEY(character_id, day), UNIQUE(source_turn)
);
CREATE INDEX IF NOT EXISTS turns_by_session ON turns(session_id, id);
CREATE TABLE IF NOT EXISTS diaries (
 character_id TEXT NOT NULL, day TEXT NOT NULL, summary TEXT NOT NULL,
 partner_comment TEXT NOT NULL, user_comment TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, comment_at TEXT, trace_json TEXT NOT NULL,
 PRIMARY KEY(character_id, day)
);
CREATE TABLE IF NOT EXISTS secrets (
 character_id TEXT NOT NULL, secret_id TEXT NOT NULL, status TEXT NOT NULL,
 truth_json TEXT NOT NULL, provenance TEXT NOT NULL,
 generated_turn INTEGER REFERENCES turns(id), disclosed_level INTEGER NOT NULL DEFAULT 0,
 history_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY (character_id, secret_id)
);
CREATE TABLE IF NOT EXISTS reveal_events (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, secret_id TEXT NOT NULL,
 level INTEGER NOT NULL, source_turn INTEGER REFERENCES turns(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bootstrap_augments (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, input_json TEXT NOT NULL,
 answers_json TEXT NOT NULL, readable_md TEXT NOT NULL,
 model_version_id INTEGER NOT NULL REFERENCES model_versions(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bootstrap_seeds (
 character_id TEXT NOT NULL, seed_id TEXT NOT NULL, canon_id INTEGER NOT NULL REFERENCES canon_facts(id),
 PRIMARY KEY(character_id, seed_id)
);
"""


class Store(MemoryStore, InterestStore, AutonomyStore, StorageStore, ReadingStore, KnowingStore, ImportStore, RequestStore, DaysStore):
    def __init__(self, path: Path | None = None, memory_config=None, embedding_model=None, *, characters_root=None, current_character_id=None, config_path=None):
        from threading import Event
        self.upkeep_stop = Event()
        self.memory_config = memory_config
        from cbs.config import load_config, embedding_model as model_name
        self.embedding_model = embedding_model or model_name(load_config())
        default_data, default_characters = resolve_paths()
        self.path = Path(path) if path is not None else default_data / "cbs.sqlite3"
        characters_root = Path(characters_root) if characters_root is not None else (
            default_characters if self.path.resolve() == (default_data / "cbs.sqlite3").resolve()
            else self.path.parent / "characters")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        from cbs.backup import SCHEMA_VERSION, pre_migration, readonly, ERROR, error_type
        from contextlib import closing
        if self.path.exists() and self.path.stat().st_size:
            with closing(readonly(self.path)) as source:
                version = source.execute('PRAGMA user_version').fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise ValueError('この記録には新しい版のアプリが必要です。')
                tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                from cbs.storage import VECTOR_TABLES
                needs_migration = version < SCHEMA_VERSION or any(
                    source.execute(f"SELECT 1 FROM {t} WHERE typeof(embedding)='text' LIMIT 1").fetchone()
                    for t in VECTOR_TABLES if t in tables)
            if needs_migration:
                root = characters_root
                try:
                    pre_migration(self.path, root, self.path.parent, current_character_id,
                                  config_path=config_path)
                except Exception as error:
                    # Never copy exception messages (paths/provider values can contain secrets).
                    status = self.path.parent / 'backup' / 'status.json'
                    try:
                        status.parent.mkdir(parents=True, exist_ok=True)
                        previous = json.loads(status.read_text(encoding='utf-8')) if status.is_file() else {}
                        previous = {k: clock.parse(v).isoformat() for k, v in previous.items()
                                    if k in ('auto_success', 'scheduled_success')}
                        previous['migration_error'] = ERROR
                        previous['migration_error_type'] = error_type(error)
                        status.write_text(json.dumps(previous), encoding='utf-8')
                    except Exception:
                        pass
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            from cbs.costs import SCHEMA as COST_SCHEMA
            connection.executescript(COST_SCHEMA)
            from cbs.photos import SCHEMA as PHOTO_SCHEMA
            connection.executescript(PHOTO_SCHEMA)
            connection.executescript(MEMORY_SCHEMA)
            connection.executescript(INTEREST_SCHEMA)
            connection.executescript(AUTONOMY_SCHEMA)
            connection.executescript(READING_SCHEMA)
            connection.executescript(KNOWING_SCHEMA)
            connection.executescript(IMPORT_SCHEMA)
            connection.executescript(REQUEST_SCHEMA)
            connection.executescript(DAYS_SCHEMA)
            connection.execute("UPDATE import_batches SET status='cancelled',cancel_requested=0 WHERE status IN ('running','cancelling')")
            connection.execute("CREATE TABLE IF NOT EXISTS note_entries (id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, kind TEXT NOT NULL, source_id INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(character_id, kind, source_id))")
            self.migrate_vectors(connection, self.embedding_model)
            # A previous process may have stopped mid-job; transactions keep retries atomic.
            connection.execute("UPDATE memory_jobs SET status='pending' WHERE status='running'")
            for table, additions in {
                "inner_states": [("current_expression", "TEXT"), ("expression_at", "TEXT")],
                "threads": [("direction", "TEXT NOT NULL DEFAULT ''"), ("motive", "TEXT NOT NULL DEFAULT ''"),
                            ("trigger_ref", "TEXT NOT NULL DEFAULT ''"), ("weight_at", "TEXT NOT NULL DEFAULT ''")],
                "proactive_runs": [("want_ref", "TEXT"), ("why_now", "TEXT NOT NULL DEFAULT ''"), ("early_domain", "TEXT")],
                "sources": [("structure_checked", "INTEGER NOT NULL DEFAULT 0")],
                "turns": [("expression", "TEXT"), ("conversation_id", "INTEGER REFERENCES conversations(id)"), ("assistant_at", "TEXT"), ("kind", "TEXT NOT NULL DEFAULT 'conversation'")],
                "interest_evidence": [("source_activity", "INTEGER REFERENCES activities(id)")],
                "activities": [("interest_id", "INTEGER REFERENCES interests(id) ON DELETE SET NULL")],
                "episodes": [("conversation_id", "INTEGER REFERENCES conversations(id)")],
                "canon_facts": [("event_date", "TEXT")],
                "user_facts": [("private", "INTEGER NOT NULL DEFAULT 0"), ("private_category", "TEXT"),
                               ("mention_override", "INTEGER"), ("follow_up", "INTEGER NOT NULL DEFAULT 0")],
                "interests": [("elaborate", "TEXT NOT NULL DEFAULT 'may_elaborate'")],
                "now_summaries": [("source_change", "INTEGER NOT NULL DEFAULT 0")],
            }.items():
                columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                for name, sql_type in additions:
                    if name not in columns:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS interest_activity_evidence ON interest_evidence(interest_id, source_activity, revision)")
            # Backfill legacy turns without altering their content or traces.
            for turn in connection.execute("SELECT * FROM turns WHERE conversation_id IS NULL ORDER BY id").fetchall():
                cid = self._conversation(connection, turn["session_id"], turn["created_at"])
                connection.execute("UPDATE turns SET conversation_id=?, assistant_at=created_at WHERE id=?", (cid, turn["id"]))
                connection.execute("UPDATE conversations SET last_activity_at=? WHERE id=?", (turn["created_at"], cid))
            connection.execute("UPDATE episodes SET conversation_id=(SELECT conversation_id FROM turns WHERE id=source_turn) WHERE conversation_id IS NULL")
            self.migrate_preferences(connection)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(ledger_events)")}
            for name, sql_type in (("plan_id", "INTEGER"), ("scheduled_text", "TEXT"), ("scheduled_date", "TEXT")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE ledger_events ADD COLUMN {name} {sql_type}")
            # Preserve legacy events, using their parent chain as the plan identity.
            for row in connection.execute("SELECT id, parent_event FROM ledger_events WHERE plan_id IS NULL ORDER BY id").fetchall():
                parent = connection.execute("SELECT plan_id FROM ledger_events WHERE id=?", (row["parent_event"],)).fetchone()
                plan_id = parent[0] if parent and parent[0] else row["id"]
                connection.execute("UPDATE ledger_events SET plan_id=? WHERE id=?", (plan_id, row["id"]))
            columns = {row[1] for row in connection.execute("PRAGMA table_info(episodes)")}
            for name, sql_type in (("mode", "TEXT NOT NULL DEFAULT 'hypothetical'"), ("session_id", "TEXT"),
                                   ("repeat_of", "INTEGER REFERENCES episodes(id)")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE episodes ADD COLUMN {name} {sql_type}")
            connection.execute("UPDATE episodes SET session_id=(SELECT session_id FROM turns WHERE id=source_turn) WHERE session_id IS NULL")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(secrets)")}
            for name, sql_type in (("disclosed_level", "INTEGER NOT NULL DEFAULT 0"),
                                   ("history_json", "TEXT NOT NULL DEFAULT '[]'"),
                                   ("generated_turn", "INTEGER REFERENCES turns(id)")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE secrets ADD COLUMN {name} {sql_type}")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            existing_characters = [r[0] for r in connection.execute(
                'SELECT character_id FROM sessions UNION SELECT character_id FROM interests UNION SELECT character_id FROM canon_facts')]
        from cbs.character import load_character
        for character_id in existing_characters:
            if (characters_root / character_id / 'expression.yaml').is_file():
                self.initialize_interests(load_character(character_id, root=characters_root))

    @contextmanager
    def connect(self):
        from cbs.shutdown import checkpoint
        checkpoint()
        connection = sqlite3.connect(self.path, timeout=30)
        from cbs.storage import trace_row, trace_text, pack_trace
        connection.row_factory = trace_row
        connection.create_function("trace_text", 1, trace_text, deterministic=True)
        connection.create_function("trace_pack", 1, pack_trace, deterministic=True)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
                checkpoint()  # A stopped upkeep unit rolls back as a whole.
        finally:
            connection.close()

    def create_session(self, character_id, title="新しい会話"):
        session = {"id": uuid4().hex, "character_id": character_id, "title": title, "created_at": now()}
        with self.connect() as connection:
            for row in connection.execute("SELECT c.* FROM conversations c JOIN sessions s ON s.id=c.session_id WHERE s.character_id=? AND c.status='open'", (character_id,)).fetchall():
                self._close(connection, row, now())
            connection.execute("INSERT INTO sessions VALUES (:id, :character_id, :title, :created_at)", session)
            self._conversation(connection, session["id"], session["created_at"])
        return session

    def list_sessions(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM sessions ORDER BY created_at DESC, id")]

    def get_session(self, session_id):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        return dict(row)

    def add_turn(self, session_id, user_text, assistant_text, trace, status="completed", post_check=None,
                 conversation_id=None, created_at=None, assistant_at=None, kind="conversation", expression=None, photos=None):
        started = perf_counter()
        with self.connect() as connection:
            created_at = created_at or now()
            conversation_id = conversation_id or self._conversation(connection, session_id, created_at)
            row = connection.execute(
                "INSERT INTO turns (session_id, user_text, assistant_text, status, trace_json, created_at, conversation_id, assistant_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, user_text, assistant_text, status, dump_trace(trace), created_at, conversation_id, assistant_at or now()),
            )
            turn_id = row.lastrowid
            connection.execute("UPDATE turns SET kind=?, expression=? WHERE id=?", (kind, expression, turn_id))
            connection.execute("UPDATE conversations SET last_activity_at=? WHERE id=?", (assistant_at or now(), conversation_id))
            self._chunk(connection, connection.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone())
            updates = {"ledger_events": [], "canon_facts": [], "warnings": []}
            character_id = connection.execute("SELECT character_id FROM sessions WHERE id=?", (session_id,)).fetchone()[0]
            generation = trace.get("Reveal Generator", {})
            if generation.get("generated"):
                connection.execute("UPDATE secrets SET generated_turn=? WHERE character_id=? AND secret_id=? AND generated_turn IS NULL",
                                   (turn_id, character_id, generation["secret_id"]))
            if status == "completed" and post_check is not None:
                updates = self._apply_post_check(connection, session_id, turn_id, character_id, post_check, trace)
            if status == "completed":
                for correction in trace.get("Packet", {}).get("output", {}).get("pending_correction", []):
                    connection.execute("UPDATE pending_corrections SET consumed_turn=? WHERE id=? AND character_id=? AND consumed_turn IS NULL",
                                       (turn_id, correction["id"], character_id))
            if "Commit" in trace:
                trace["Commit"].update(output={"turn_id": turn_id, "status": status, **updates}, latency_ms=round((perf_counter() - started) * 1000, 2))
                connection.execute("UPDATE turns SET trace_json=? WHERE id=?", (dump_trace(trace), turn_id))
            from cbs.photos import save_photos, save_turn_sources
            save_photos(self, connection, character_id, turn_id, photos or [])
            save_turn_sources(connection, turn_id, trace, photos or [])
            return turn_id

    def _apply_post_check(self, connection, session_id, turn_id, character_id, post_check, trace):
        updates = {"ledger_events": [], "canon_facts": [], "warnings": []}
        self.apply_threads(connection, character_id, turn_id, post_check, trace.get("Packet", {}).get("output", {}))
        turn = connection.execute('SELECT * FROM turns WHERE id=?', (turn_id,)).fetchone()
        self.save_research_questions(connection, character_id, post_check.get('research_questions', []),
                                     'conversation', f'turn:{turn_id}', '会話の後に生まれた問い',
                                     when=turn['assistant_at'] or turn['created_at'])
        if post_check.get('understanding_questions'):
            self.record_knowing_questions(connection, character_id, turn_id, post_check, trace.get('Packet', {}).get('output', {}))
        updates["used_memories"] = self._recall(connection, character_id, post_check, trace)
        for update in post_check["ledger_updates"]:
            plan_id = update["plan_id"]
            operation = update["operation"]
            if plan_id is None and operation not in ("propose", "agree"):
                updates["warnings"].append("plan_id required: " + operation)
                continue
            try:
                event = self._plan_event(connection, session_id, operation, update["content"], plan_id,
                                         update["scheduled_text"], update["scheduled_date"], turn_id)
                if event is not None:
                    updates["ledger_events"].append(event)
            except KeyError:
                updates["warnings"].append(f"unknown plan_id ignored: {plan_id}")
        for fact in post_check["new_canon"]:
            saved = self._canon(connection, character_id, fact["kind"], fact["content"], turn_id, fact["life_ref"], fact.get("event_date"))
            if saved is not None:
                updates["canon_facts"].append(saved)
        disclosures = post_check.get("disclosures", [])
        if disclosures:
            updates["reveal_events"] = []
        packet_secrets = {s["secret_id"]: s for s in trace.get("Packet", {}).get("output", {}).get("secrets", [])}
        for disclosure in disclosures:
            secret_id, level = disclosure["secret_id"], disclosure["level"]
            packet_secret = packet_secrets.get(secret_id)
            secret = connection.execute("SELECT * FROM secrets WHERE character_id=? AND secret_id=?", (character_id, secret_id)).fetchone()
            if not secret:
                updates["warnings"].append(f"invalid disclosure ignored: {secret_id} L{level}")
                continue
            if secret["status"] == "pending" or not packet_secret or level > packet_secret["allowed_level"]:
                # Even after the one rewrite, the final reply can still
                # contain a violation. Record what was actually told.
                updates["warnings"].append(f"disclosure beyond allowed: {secret_id} L{level}")
            connection.execute("UPDATE secrets SET disclosed_level=MAX(disclosed_level, ?), updated_at=? WHERE character_id=? AND secret_id=?",
                               (level, now(), character_id, secret_id))
            event = connection.execute("INSERT INTO reveal_events (character_id, secret_id, level, source_turn, created_at) VALUES (?, ?, ?, ?, ?)",
                                       (character_id, secret_id, level, turn_id, now()))
            updates["reveal_events"].append(dict(connection.execute("SELECT * FROM reveal_events WHERE id=?", (event.lastrowid,)).fetchone()))
        return updates

    def merge_timeline(self, turn_id, timeline):
        """Merge worker snapshots without replacing a newer trace or losing events."""
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            stored = json.loads(connection.execute('SELECT trace_json FROM turns WHERE id=?', (turn_id,)).fetchone()[0])
            events = stored.get('Timeline', {}).get('events', []) + timeline['events']
            unique = {json.dumps(e, sort_keys=True): e for e in events}
            stored['Timeline'] = {**timeline, 'events': sorted(unique.values(), key=lambda e: e['at_ms'])}
            connection.execute('UPDATE turns SET trace_json=? WHERE id=?', (dump_trace(stored), turn_id))

    def finish_expression(self, turn_id, expression, trace):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("SELECT trace_json FROM turns WHERE id=?", (turn_id,)).fetchone()
            stored = json.loads(row["trace_json"])
            stored.update(trace)
            connection.execute("UPDATE turns SET expression=?, trace_json=? WHERE id=?",
                               (expression, dump_trace(stored), turn_id))

    def finish_post_check(self, turn_id, trace, post_check, corrections, *, request_character=None, request_config=None, request_version=0, days_context=None):
        """Persist delayed extraction, correction and trace together, without another turn."""
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            turn = connection.execute("SELECT t.*, s.character_id FROM turns t JOIN sessions s ON s.id=t.session_id WHERE t.id=?", (turn_id,)).fetchone()
            stored = json.loads(turn["trace_json"])
            for step in ("Check", "Check-2", "Extract"):
                if step in trace:
                    stored[step] = trace[step]
            updates = self._apply_post_check(connection, turn["session_id"], turn_id, turn["character_id"], post_check, stored) if post_check else {"ledger_events": [], "canon_facts": [], "warnings": []}
            if post_check and request_character is not None:
                updates['requests'] = self.apply_request_operations(connection, request_character, turn_id,
                    post_check.get('request_operations', []), request_config, request_version)
            if post_check and days_context is not None:
                updates['days'] = self.apply_day_operations(connection, turn['character_id'], turn_id,
                    post_check.get('day_operations', []), days_context['version'])
                self.mark_days(connection, turn['character_id'], post_check.get('used_days', []),
                    [d['id'] for d in days_context['today'] if d['may_mention']], turn_id, days_context['local_day'])
            stored["Extract"]["state"] = "completed" if post_check else "failed"
            stored["Commit"]["input"]["post_check"] = post_check
            stored["Commit"]["output"].update(updates)
            if corrections:
                connection.execute("INSERT OR IGNORE INTO pending_corrections (character_id, source_turn, correction_json, created_at) VALUES (?, ?, ?, ?)",
                                   (turn["character_id"], turn_id, json.dumps(corrections, ensure_ascii=False), now()))
            connection.execute("UPDATE turns SET trace_json=? WHERE id=?", (dump_trace(stored), turn_id))

    def previous_session_datetime(self, session_id):
        session = self.get_session(session_id)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT created_at FROM sessions WHERE character_id=? AND id<>? AND rowid < "
                "(SELECT rowid FROM sessions WHERE id=?) ORDER BY rowid DESC LIMIT 1",
                (session["character_id"], session_id, session_id),
            ).fetchone()
        return row[0] if row else None

    def _plan_event(self, connection, session_id, operation, content, plan_id, scheduled_text, scheduled_date, source_turn):
        kind = {"propose": "proposed", "agree": "agreed", "cancel": "cancelled",
                "reschedule": "rescheduled", "report_done": "reported_done"}[operation]
        parent = None
        if plan_id is not None:
            parent = connection.execute(
                "SELECT e.* FROM ledger_events e JOIN sessions s ON s.id=e.session_id "
                "WHERE e.plan_id=? AND s.character_id=(SELECT character_id FROM sessions WHERE id=?) "
                "ORDER BY e.created_at DESC,e.id DESC LIMIT 1", (plan_id, session_id),
            ).fetchone()
            if parent is None:
                raise KeyError(plan_id)
            content = content or parent["content"]
            if operation != "reschedule" and scheduled_text is None and scheduled_date is None:
                scheduled_text, scheduled_date = parent["scheduled_text"], parent["scheduled_date"]
            # A second reschedule can genuinely move the date again. Other
            # same-state operations merely reconfirm the current plan state.
            same_schedule = (scheduled_date == parent["scheduled_date"] and
                             (scheduled_date is not None or scheduled_text == parent["scheduled_text"]))
            if kind == parent["kind"] and (kind != "rescheduled" or same_schedule):
                return None
        row = connection.execute(
            "INSERT INTO ledger_events (session_id, kind, content, parent_event, source_turn, created_at, plan_id, scheduled_text, scheduled_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, kind, content, parent["id"] if parent else None, source_turn, now(), plan_id, scheduled_text, scheduled_date),
        )
        if plan_id is None:
            connection.execute("UPDATE ledger_events SET plan_id=? WHERE id=?", (row.lastrowid, row.lastrowid))
        return dict(connection.execute("SELECT * FROM ledger_events WHERE id=?", (row.lastrowid,)).fetchone())

    def create_plan(self, session_id, content, scheduled_text=None, scheduled_date=None, source_turn=None):
        return self.add_plan_event(session_id, "propose", content, scheduled_text=scheduled_text,
                                   scheduled_date=scheduled_date, source_turn=source_turn)

    def add_plan_event(self, session_id, operation, content="", plan_id=None, scheduled_text=None, scheduled_date=None, source_turn=None):
        if plan_id is None and operation not in ("propose", "agree"):
            raise ValueError("plan_id required")
        with self.connect() as connection:
            return self._plan_event(connection, session_id, operation, content, plan_id, scheduled_text, scheduled_date, source_turn)

    def list_plans(self, character_id, active_only=False):
        with self.connect() as connection:
            events = [dict(row) for row in connection.execute(
                "SELECT e.* FROM ledger_events e JOIN sessions s ON s.id=e.session_id WHERE s.character_id=? ORDER BY e.id",
                (character_id,),
            )]
        grouped = {}
        for event in events:
            grouped.setdefault(event["plan_id"], []).append(event)
        histories = [sorted(history,key=lambda e:(e['created_at'],e['id'])) for history in grouped.values()]
        plans = [{**history[-1], "state": history[-1]["kind"], "events": history} for history in histories]
        return [plan for plan in plans if not active_only or plan["state"] in ("proposed", "agreed", "rescheduled")]

    def _canon(self, connection, character_id, kind, content, source_turn, life_ref, event_date=None):
        existing = connection.execute("SELECT id,created_at FROM canon_facts WHERE character_id=? AND kind=? AND content=?",
                              (character_id, kind, content)).fetchone()
        if existing:
            if source_turn and clock.parse(now()) < clock.parse(existing['created_at']):
                connection.execute('UPDATE canon_facts SET created_at=?,source_turn=? WHERE id=?',(now(),source_turn,existing['id']))
            return None
        row = connection.execute(
            # Migrated preference IDs still identify facts in legacy traces; never reuse them.
            "INSERT INTO canon_facts (id, character_id, kind, content, source_turn, life_ref, created_at, event_date) "
            "VALUES ((SELECT COALESCE(MAX(id), 0)+1 FROM (SELECT id FROM canon_facts UNION ALL "
            "SELECT migrated_canon FROM interests)), ?, ?, ?, ?, ?, ?, ?)",
            (character_id, kind, content, source_turn, life_ref, now(), event_date),
        )
        return dict(connection.execute("SELECT * FROM canon_facts WHERE id=?", (row.lastrowid,)).fetchone())

    def add_canon(self, character_id, kind, content, source_turn=None, life_ref=None):
        with self.connect() as connection:
            return self._canon(connection, character_id, kind, content, source_turn, life_ref)

    def list_canon(self, character_id):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM canon_facts WHERE character_id=? ORDER BY id", (character_id,))]

    def history(self, session_id, limit=None):
        self.get_session(session_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, session_id, conversation_id, user_text, assistant_text, status, created_at, assistant_at, kind, expression FROM turns WHERE session_id=? ORDER BY id", (session_id,)
            ).fetchall()
        result = [dict(row) for row in rows]
        return result[-limit:] if limit else result

    def get_trace(self, turn_id):
        with self.connect() as connection:
            row = connection.execute("SELECT trace_json FROM turns WHERE id=?", (turn_id,)).fetchone()
        if row is None:
            raise KeyError(turn_id)
        return json.loads(row[0])

    def list_episodes(self, character_id):
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM episodes WHERE character_id=? ORDER BY id", (character_id,)).fetchall()
        return [{**dict(row), "salient_conditions": json.loads(row["salient_conditions_json"])} for row in rows]

    def forming_notice(self, turn_id):
        with self.connect() as db:
            row = db.execute('SELECT source_turn FROM forming_notices WHERE source_turn=?', (turn_id,)).fetchone()
        return bool(row)

    def early_candidates(self, character, config):
        """Only window/cap/state rules; the proactive judge chooses meaning and tone."""
        cid = character.model.character_id
        with self.connect() as db:
            first = db.execute("SELECT MIN(t.created_at) FROM turns t JOIN sessions s ON s.id=t.session_id "
                               "WHERE s.character_id=? AND t.status='completed'", (cid,)).fetchone()[0]
            used = db.execute("SELECT created_at FROM proactive_runs WHERE character_id=? AND early_domain IS NOT NULL "
                              "AND source_turn IS NOT NULL", (cid,)).fetchall()
        today = clock.now().date()
        age = (today - clock.parse(first).date()).days if first else 0
        if not 0 <= age < config.days or len(used) >= config.total_max:
            return []
        if sum(clock.parse(r[0]).date() == today for r in used) >= config.daily_max:
            return []
        return [{"id": d.id, "label": getattr(d, 'description', None) or d.label}
                for d in self.current_model(character).domains if d.status == 'open']

    def list_model_versions(self, character_id):
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM model_versions WHERE character_id=? ORDER BY id", (character_id,)).fetchall()
        result = [{**dict(row), "diff": json.loads(row["diff_json"]),
                   "episode_refs": json.loads(row["episode_refs_json"])} for row in rows]
        invalid = {i for v in result for i in v["diff"].get("invalidate", [])}
        for version in result:
            version["invalidated"] = version["id"] in invalid
        return result

    def list_steward_runs(self, character_id):
        with self.connect() as connection:
            rows = connection.execute("SELECT r.*, t.session_id, t.user_text FROM steward_runs r JOIN turns t ON t.id=r.source_turn "
                                      "JOIN sessions s ON s.id=t.session_id WHERE s.character_id=? ORDER BY r.id", (character_id,)).fetchall()
        return [{**dict(row), "input": json.loads(row["input_json"]), "output": json.loads(row["output_json"]),
                 "application": json.loads(row["application_json"])} for row in rows]

    def current_model(self, character):
        from cbs.model import current_model
        return current_model(character, self.list_model_versions(character.model.character_id))

    def bootstrap_answers(self, character_id):
        result = {}
        with self.connect() as db:
            for row in db.execute('SELECT answers_json FROM bootstrap_augments WHERE character_id=? ORDER BY id', (character_id,)):
                result.update(json.loads(row['answers_json']))
        return result

    def author_sources(self, character):
        sources = dict(character.sources)
        with self.connect() as db:
            for row in db.execute('SELECT id, readable_md FROM bootstrap_augments WHERE character_id=? ORDER BY id', (character.model.character_id,)):
                sources[f'augment_{row["id"]}.md'] = row['readable_md']
        return sources

    def initialize_seed_memories(self, character):
        cid = character.model.character_id
        with self.connect() as db:
            for seed in character.seed_memories:
                if db.execute('SELECT 1 FROM bootstrap_seeds WHERE character_id=? AND seed_id=?', (cid, seed['id'])).fetchone():
                    continue
                fact = self._canon(db, cid, seed['kind'], seed['content'], None, seed.get('life_ref'), seed['event_date'])
                if fact is None:
                    fact = db.execute('SELECT id FROM canon_facts WHERE character_id=? AND kind=? AND content=?', (cid, seed['kind'], seed['content'])).fetchone()
                db.execute('INSERT INTO bootstrap_seeds VALUES (?, ?, ?)', (cid, seed['id'], fact['id']))
                db.execute('UPDATE canon_facts SET created_at=? WHERE id=?', (character.bootstrap_meta.get('created_at', now()), fact['id']))

    def relationship_metrics(self, character_id):
        with self.connect() as connection:
            rows = connection.execute("SELECT t.session_id, t.created_at, t.trace_json FROM turns t JOIN sessions s ON s.id=t.session_id "
                                      "WHERE s.character_id=? AND t.status='completed'", (character_id,)).fetchall()
        judgments = [json.loads(row["trace_json"]).get("Packet", {}).get("input", {}) for row in rows]
        return {"sessions": len({row["session_id"] for row in rows}),
                "days": len({clock.day(row["created_at"], self.memory_settings.day_boundary_hour) for row in rows}),
                "deep_moments": sum(j.get("emotional_weight") == "high" for j in judgments),
                "user_disclosures": sum(j.get("user_disclosure") == "yes" for j in judgments)}

    def list_secrets(self, character_id):
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM secrets WHERE character_id=? ORDER BY secret_id", (character_id,)).fetchall()
        return [{**dict(row), "truth": json.loads(row["truth_json"]), "history": json.loads(row["history_json"])} for row in rows]

    def save_secret(self, character_id, secret_id, truth, *, status="hidden", provenance="ai_generated",
                    replace=False, action="generate"):
        from cbs.reveal import RevealTruth
        truth = RevealTruth.model_validate(truth).model_dump()
        if status not in ("hidden", "pending", "approved"):
            raise ValueError("invalid secret status")
        with self.connect() as connection:
            old = connection.execute("SELECT * FROM secrets WHERE character_id=? AND secret_id=?", (character_id, secret_id)).fetchone()
            if old and not replace:
                return False
            history = json.loads(old["history_json"]) if old else []
            if old:
                history.append({"action": action, "replaced_at": now(), "truth": json.loads(old["truth_json"]),
                                "status": old["status"], "provenance": old["provenance"],
                                "generated_turn": old["generated_turn"], "disclosed_level": old["disclosed_level"]})
            connection.execute("INSERT INTO secrets (character_id, secret_id, status, truth_json, provenance, history_json, created_at, updated_at) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(character_id, secret_id) DO UPDATE SET "
                               "status=excluded.status, truth_json=excluded.truth_json, provenance=excluded.provenance, "
                               "history_json=excluded.history_json, generated_turn=NULL, updated_at=excluded.updated_at",
                               (character_id, secret_id, status, json.dumps(truth, ensure_ascii=False), provenance,
                                json.dumps(history, ensure_ascii=False), now(), now()))
        return True

    def approve_secret(self, character_id, secret_id):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM secrets WHERE character_id=? AND secret_id=?", (character_id, secret_id)).fetchone()
            if row is None:
                raise ValueError("真相がまだ生成されていません。")
            if row["status"] != "pending":
                return
            history = json.loads(row["history_json"])
            history.append({"action": "approve", "previous_status": row["status"], "at": now()})
            connection.execute("UPDATE secrets SET status='approved', history_json=?, updated_at=? WHERE character_id=? AND secret_id=?",
                               (json.dumps(history, ensure_ascii=False), now(), character_id, secret_id))

    def list_reveal_events(self, character_id):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM reveal_events WHERE character_id=? ORDER BY id", (character_id,))]

    def revert_model(self, character, version_id):
        versions = self.list_model_versions(character.model.character_id)
        if not any(v["id"] == version_id and "changes" in v["diff"] for v in versions):
            raise ValueError("変更版がありません。")
        invalid = [v["id"] for v in versions if v["id"] >= version_id and "changes" in v["diff"] and not v["invalidated"]]
        with self.connect() as connection:
            row = connection.execute("INSERT INTO model_versions (character_id, base_version, diff_json, rationale, episode_refs_json, created_at) "
                                     "VALUES (?, ?, ?, ?, '[]', ?)", (character.model.character_id, character.model.version,
                                     json.dumps({"invalidate": invalid}), f"revert {version_id}", now()))
        return row.lastrowid
