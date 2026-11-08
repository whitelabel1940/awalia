"""Conversation boundaries and memory persistence; no semantic decisions."""
from cbs.storage import dump_trace
from cbs.storage import pack_vector, unpack_vector
import json

from cbs import clock
from cbs.config import MemoryConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
 id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 started_at TEXT NOT NULL, last_activity_at TEXT NOT NULL, closed_at TEXT,
 status TEXT NOT NULL DEFAULT 'open', queued_through INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memory_jobs (
 id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversations(id),
 first_turn INTEGER NOT NULL, last_turn INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 trace_json TEXT NOT NULL DEFAULT '{}', UNIQUE(conversation_id, first_turn, last_turn)
);
CREATE TABLE IF NOT EXISTS memory_episodes (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, conversation_id INTEGER NOT NULL REFERENCES conversations(id),
 job_id INTEGER REFERENCES memory_jobs(id), started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
 summary TEXT NOT NULL, details TEXT NOT NULL, importance REAL NOT NULL, pinned INTEGER NOT NULL,
 embedding BLOB, recall_count INTEGER NOT NULL DEFAULT 0, last_recalled_at TEXT,
 status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_facts (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'current',
 learned_at TEXT NOT NULL, last_confirmed_at TEXT NOT NULL, superseded_by INTEGER REFERENCES user_facts(id),
 source_episode INTEGER REFERENCES memory_episodes(id), importance REAL NOT NULL, pinned INTEGER NOT NULL,
 embedding BLOB, recall_count INTEGER NOT NULL DEFAULT 0, last_recalled_at TEXT, event_date TEXT
);
CREATE TABLE IF NOT EXISTS episode_links (
 id INTEGER PRIMARY KEY, source_episode INTEGER NOT NULL REFERENCES memory_episodes(id),
 target_episode INTEGER NOT NULL REFERENCES memory_episodes(id),
 relation TEXT NOT NULL CHECK(relation IN ('continues','updates','contradicts','same_topic','none')),
 reason TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(source_episode, target_episode)
);
CREATE TABLE IF NOT EXISTS episode_link_runs (
 source_episode INTEGER PRIMARY KEY REFERENCES memory_episodes(id), status TEXT NOT NULL, trace_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monthly_digests (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, month TEXT NOT NULL,
 summary TEXT NOT NULL, details TEXT NOT NULL, embedding BLOB NOT NULL,
 importance REAL NOT NULL DEFAULT 0.5, pinned INTEGER NOT NULL DEFAULT 0,
 recall_count INTEGER NOT NULL DEFAULT 0, last_recalled_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(character_id, month)
);
CREATE TABLE IF NOT EXISTS digest_episodes (
 digest_id INTEGER NOT NULL REFERENCES monthly_digests(id), episode_id INTEGER NOT NULL REFERENCES memory_episodes(id),
 PRIMARY KEY(digest_id, episode_id)
);
CREATE TABLE IF NOT EXISTS memory_maintenance (
 character_id TEXT NOT NULL, local_day TEXT NOT NULL, status TEXT NOT NULL, trace_json TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(character_id, local_day)
);
CREATE TABLE IF NOT EXISTS pending_corrections (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, source_turn INTEGER NOT NULL REFERENCES turns(id),
 correction_json TEXT NOT NULL, consumed_turn INTEGER REFERENCES turns(id), created_at TEXT NOT NULL,
 UNIQUE(source_turn)
);
"""


class MemoryStore:
    memory_config = None

    @property
    def memory_settings(self):
        return self.memory_config or MemoryConfig()

    def _conversation(self, connection, session_id, when):
        row = connection.execute("SELECT * FROM conversations WHERE session_id=? AND status='open' ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        if row and not connection.execute("SELECT 1 FROM turns WHERE conversation_id=? LIMIT 1", (row["id"],)).fetchone():
            connection.execute("UPDATE conversations SET started_at=?, last_activity_at=? WHERE id=?", (when, when, row["id"]))
            return row["id"]
        if row and self._expired(row, when):
            self._close(connection, row, when)
            row = None
        if row is None:
            cursor = connection.execute("INSERT INTO conversations (session_id, started_at, last_activity_at) VALUES (?, ?, ?)",
                                        (session_id, when, when))
            row = connection.execute("SELECT * FROM conversations WHERE id=?", (cursor.lastrowid,)).fetchone()
        return row["id"]

    def _expired(self, row, when):
        cfg = self.memory_settings
        return ((clock.parse(when) - clock.parse(row["last_activity_at"])).total_seconds() >= cfg.gap_minutes * 60 or
                clock.day(when, cfg.day_boundary_hour) != clock.day(row["started_at"], cfg.day_boundary_hour))

    def _chunk(self, connection, row, force=False, max_size=None):
        turns = connection.execute("SELECT id FROM turns WHERE conversation_id=? AND status='completed' AND id>? ORDER BY id",
                                   (row["id"], row["queued_through"])).fetchall()
        size = min(self.memory_settings.max_exchanges,max_size) if max_size else self.memory_settings.max_exchanges
        while len(turns) >= size or (force and turns):
            chunk, turns = turns[:size], turns[size:]
            connection.execute("INSERT OR IGNORE INTO memory_jobs (conversation_id, first_turn, last_turn) VALUES (?, ?, ?)",
                               (row["id"], chunk[0]["id"], chunk[-1]["id"]))
            connection.execute("UPDATE conversations SET queued_through=? WHERE id=?", (chunk[-1]["id"], row["id"]))

    def _close(self, connection, row, when):
        self._chunk(connection, row, force=True)
        connection.execute("UPDATE conversations SET status='closed', closed_at=? WHERE id=?", (when, row["id"]))

    def prepare_conversation(self, session_id, when=None):
        self.get_session(session_id)
        with self.connect() as connection:
            return self._conversation(connection, session_id, when or clock.iso())

    def sweep_conversations(self, *, session_id=None, force=False):
        when = clock.iso()
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM conversations WHERE status='open'" +
                                      (" AND session_id=?" if session_id else ""), (session_id,) if session_id else ()).fetchall()
            for row in rows:
                if force or self._expired(row, when):
                    self._close(connection, row, when)
                else:
                    self._chunk(connection, row)
            # Empty conversations need no LLM work.
            connection.execute("UPDATE conversations SET status='episodized' WHERE status='closed' AND NOT EXISTS "
                               "(SELECT 1 FROM memory_jobs j WHERE j.conversation_id=conversations.id AND j.status<>'completed')")

    def list_conversations(self):
        with self.connect() as connection:
            return [dict(r) for r in connection.execute("SELECT * FROM conversations ORDER BY id")]

    def memory_jobs(self, retry=False, *, character_id=None):
        scope = ' AND id NOT IN (SELECT job_id FROM import_steps)'
        scope += (' AND conversation_id IN (SELECT c.id FROM conversations c JOIN sessions s ON s.id=c.session_id WHERE s.character_id=?)' if character_id else '')
        args = (character_id,) if character_id else ()
        with self.connect() as connection:
            if retry:
                connection.execute("UPDATE memory_jobs SET status='pending' WHERE status='failed'"+scope,args)
            return [dict(r) for r in connection.execute("SELECT * FROM memory_jobs WHERE status='pending'"+scope+" ORDER BY id",args)]

    def previous_utterance(self, character_id):
        with self.connect() as connection:
            row = connection.execute("SELECT t.assistant_at, t.created_at FROM turns t JOIN sessions s ON s.id=t.session_id "
                                     "WHERE s.character_id=? AND t.status='completed' ORDER BY COALESCE(t.assistant_at,t.created_at) DESC,t.id DESC LIMIT 1", (character_id,)).fetchone()
        return (row["assistant_at"] or row["created_at"]) if row else None

    def list_memory_episodes(self, character_id):
        with self.connect() as connection:
            return [dict(r) for r in connection.execute("SELECT * FROM memory_episodes WHERE character_id=? ORDER BY id", (character_id,))]

    def list_user_facts(self, character_id, current_only=False):
        with self.connect() as connection:
            facts = [self.fact_policy(r, connection) for r in connection.execute("SELECT * FROM user_facts WHERE character_id=?" +
                    (" AND status='current'" if current_only else "") + " ORDER BY id", (character_id,))]
            from cbs.autonomy_store import eligible
            for fact in facts:
                threads = connection.execute("SELECT * FROM threads WHERE character_id=? AND kind='follow_up' AND source_ref=?", (character_id, f"fact:{fact['id']}")).fetchall()
                fact['may_initiate'] = fact['status'] == 'current' and fact['allow_mention'] and any(eligible(t) for t in threads)
            return facts

    def save_memories(self, job, character_id, output, vectors, turns, trace):
        """Commit all episode/fact changes and the job marker atomically."""
        with self.connect() as connection, self.import_journal(connection, self.import_batch_for_job(connection, job['id'])):
            connection.execute('BEGIN IMMEDIATE')
            self.check_embedding_model(connection, writing=True)
            if connection.execute("SELECT status FROM memory_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "completed":
                return
            ids, warnings = [], []
            by_id = {t["id"]: t for t in turns}
            for index, episode in enumerate(output.episodes):
                first, last = by_id.get(episode.first_turn, turns[0]), by_id.get(episode.last_turn, turns[-1])
                if first["id"] > last["id"]:
                    first, last = turns[0], turns[-1]
                row = connection.execute("INSERT INTO memory_episodes (character_id, conversation_id, job_id, started_at, ended_at, "
                    "summary, details, importance, pinned, embedding, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (character_id, job["conversation_id"], job["id"], first["created_at"], last["assistant_at"] or last["created_at"],
                     episode.summary, episode.details, episode.importance, episode.pinned, pack_vector(vectors[index]), turns[-1]["created_at"]))
                ids.append(row.lastrowid)
            for index, fact in enumerate(output.user_facts):
                learned = by_id.get(fact.learned_turn, turns[-1])["created_at"]
                learned_turn = by_id.get(fact.learned_turn, turns[-1])['id']
                old = connection.execute("SELECT * FROM user_facts WHERE id=? AND character_id=? AND status='current'",
                                         (fact.fact_id, character_id)).fetchone() if fact.fact_id else None
                if fact.operation != "new" and not old:
                    warnings.append(f"unknown/current fact required: {fact.fact_id}")
                    continue
                private = bool(fact.private or (old and old['private']))
                category = fact.private_category or (old['private_category'] if old else None)
                category = category if category in self.private_catalog().private_categories else 'other'
                override = old['mention_override'] if old else None
                effective = fact.model_copy(update={'private': private, 'private_category': category})
                if fact.operation == "confirm":
                    if old and clock.parse(learned) < clock.parse(old['last_confirmed_at']):
                        continue
                    connection.execute("UPDATE user_facts SET last_confirmed_at=?, private=?,private_category=?,event_date=COALESCE(?,event_date),follow_up=? WHERE id=?",
                        (learned,private,category,fact.event_date.isoformat() if fact.event_date else None,fact.follow_up,fact.fact_id))
                    self._fact_thread(connection, character_id, fact.fact_id, effective, learned_turn)
                    continue
                if not ids or fact.source_episode >= len(ids):
                    warnings.append("invalid source_episode")
                    continue
                row = connection.execute("INSERT INTO user_facts (character_id, content, learned_at, last_confirmed_at, source_episode, "
                    "importance, pinned, embedding, event_date, private, private_category, mention_override, follow_up) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (character_id, fact.content, learned, learned, ids[fact.source_episode], fact.importance, fact.pinned,
                     pack_vector(vectors[len(ids) + index]), fact.event_date.isoformat() if fact.event_date else None, private, category, override, fact.follow_up))
                historical = old and clock.parse(learned) < clock.parse(old['learned_at'])
                if historical:
                    connection.execute("UPDATE user_facts SET status='superseded',superseded_by=? WHERE id=?", (old['id'],row.lastrowid))
                elif old:
                    connection.execute("UPDATE user_facts SET status='superseded', superseded_by=? WHERE id=?", (row.lastrowid, old["id"]))
                    if private:
                        # Carry the one-shot record, including used status, to the revised fact.
                        for thread in connection.execute('SELECT * FROM threads WHERE character_id=? AND source_ref=?', (character_id,f"fact:{old['id']}")).fetchall():
                            connection.execute('UPDATE threads SET source_ref=?,topic=?,detail=?,updated_at=? WHERE id=?',
                                (f'fact:{row.lastrowid}',fact.content,fact.content,clock.iso(),thread['id']))
                            self._thread_history(connection,thread['id'],'fact_revision',dict(thread))
                if not historical:
                    self._fact_thread(connection, character_id, row.lastrowid, effective, learned_turn)
            trace["interest_updates"] = self.apply_interests(connection, character_id, output.haru_expressed,
                vectors[len(output.episodes) + len(output.user_facts):], turns, ids, job['conversation_id'], warnings)
            trace["warnings"] = warnings
            self.save_observations(connection, character_id, job, output.observations, turns, trace.get('knowing_revision', 0))
            from cbs.photos import save_job_sources
            save_job_sources(connection, job, turns, trace)
            connection.execute("UPDATE memory_jobs SET status='completed', trace_json=? WHERE id=?", (dump_trace(trace), job["id"]))
            connection.execute("UPDATE conversations SET status='episodized' WHERE id=? AND status='closed' AND NOT EXISTS "
                               "(SELECT 1 FROM memory_jobs WHERE conversation_id=? AND status<>'completed')", (job["conversation_id"], job["conversation_id"]))
            return ids

    def list_episode_links(self, character_id):
        with self.connect() as connection:
            return [dict(r) for r in connection.execute(
                "SELECT l.* FROM episode_links l JOIN memory_episodes e ON e.id=l.source_episode WHERE e.character_id=? ORDER BY l.id", (character_id,))]

    def save_episode_links(self, episode_id, links, status, trace):
        with self.connect() as connection:
            job_id = connection.execute('SELECT job_id FROM memory_episodes WHERE id=?',(episode_id,)).fetchone()[0]
            with self.import_journal(connection, self.import_batch_for_job(connection,job_id)):
                self._save_episode_links(connection, episode_id, links, status, trace)

    def _save_episode_links(self, connection, episode_id, links, status, trace):
        for link in links:
            source,target = episode_id,link.target_episode
            if link.relation == 'updates':
                dates = {r['id']:(r['ended_at'],r['id']) for r in connection.execute('SELECT id,ended_at FROM memory_episodes WHERE id IN (?,?)',(source,target))}
                if dates[source] < dates[target]:
                    source,target = target,source
            connection.execute("INSERT OR IGNORE INTO episode_links (source_episode, target_episode, relation, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                               (source,target,link.relation,link.reason,clock.iso()))
        connection.execute("INSERT OR REPLACE INTO episode_link_runs VALUES (?, ?, ?)",
                           (episode_id,status,dump_trace(trace)))

    def list_monthly_digests(self, character_id):
        with self.connect() as connection:
            result = [dict(r) for r in connection.execute("SELECT * FROM monthly_digests WHERE character_id=? ORDER BY month", (character_id,))]
            for digest in result:
                digest["episode_ids"] = [r[0] for r in connection.execute("SELECT episode_id FROM digest_episodes WHERE digest_id=? ORDER BY episode_id", (digest["id"],))]
            return result

    def save_monthly_digest(self, character_id, month, output, vector, episode_ids, *, batch_id=None, original_at=None):
        with self.connect() as connection, self.import_journal(connection,batch_id):
            self.check_embedding_model(connection, writing=True)
            connection.execute("INSERT INTO monthly_digests (character_id, month, summary, details, embedding, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                               "ON CONFLICT(character_id, month) DO UPDATE SET summary=excluded.summary, details=excluded.details, embedding=excluded.embedding, updated_at=excluded.updated_at",
                               (character_id, month, output.summary, output.details, pack_vector(vector), original_at or clock.iso(), original_at or clock.iso()))
            digest_id = connection.execute("SELECT id FROM monthly_digests WHERE character_id=? AND month=?", (character_id, month)).fetchone()[0]
            for episode_id in episode_ids:
                connection.execute("INSERT OR IGNORE INTO digest_episodes VALUES (?, ?)", (digest_id, episode_id))
                connection.execute("UPDATE memory_episodes SET status='archived' WHERE id=? AND character_id=?", (episode_id, character_id))

    def pending_corrections(self, character_id):
        with self.connect() as connection:
            return [{"id": r["id"], "source_turn": r["source_turn"], "claims": json.loads(r["correction_json"])} for r in connection.execute(
                "SELECT * FROM pending_corrections WHERE character_id=? AND consumed_turn IS NULL ORDER BY id", (character_id,))]

    def _recall(self, connection, character_id, post_check, trace):
        packet = trace.get("Packet", {}).get("output", {})
        available = {m["ref"] for key in ("memory_episodes", "user_facts", "monthly_digests", "counterevidence_memories") for m in packet.get(key, [])}
        counted = []
        for ref in dict.fromkeys(post_check.get("used_memories", [])):
            if ref not in available:
                continue
            prefix, _, number = ref.partition(":")
            table = {"memory": "memory_episodes", "fact": "user_facts", "digest": "monthly_digests"}.get(prefix)
            if not table or not number.isdigit():
                continue
            cursor = connection.execute(f"UPDATE {table} SET recall_count=recall_count+1, last_recalled_at=? WHERE id=? AND character_id=?" +
                                        (" AND status='current'" if prefix == "fact" else ""), (clock.iso(), int(number), character_id))
            if cursor.rowcount:
                counted.append(ref)
        return counted
