"""Durable import provenance and transactional, batch-scoped reversal."""
from contextlib import contextmanager
import json

from cbs import clock
from cbs.storage import compact_trace, dump_trace

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batches (
 id TEXT PRIMARY KEY, character_id TEXT NOT NULL, status TEXT NOT NULL,
 quote_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS import_items (
 id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(id),
 character_id TEXT NOT NULL, format TEXT NOT NULL, source_id TEXT NOT NULL,
 content_hash TEXT NOT NULL, payload_json TEXT NOT NULL, last_at TEXT,
 status TEXT NOT NULL DEFAULT 'pending', session_id TEXT REFERENCES sessions(id),
 UNIQUE(batch_id,format,source_id,content_hash)
);
CREATE INDEX IF NOT EXISTS import_identity ON import_items(character_id,format,source_id,content_hash);
CREATE TABLE IF NOT EXISTS import_steps (
 job_id INTEGER PRIMARY KEY REFERENCES memory_jobs(id), item_id INTEGER NOT NULL REFERENCES import_items(id),
 reflected INTEGER NOT NULL DEFAULT 0, trace_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS import_changes (
 id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(id),
 table_name TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_reviews (
 id TEXT PRIMARY KEY, character_id TEXT NOT NULL, text TEXT NOT NULL,
 candidates_json TEXT NOT NULL, selected_json TEXT, created_at TEXT NOT NULL
);
"""

# Only tables the import reflection / Episodizer / monthly maintenance writes.
JOURNALED = ('memory_episodes', 'user_facts', 'user_observations', 'interests', 'interest_evidence',
             'interest_changes', 'threads', 'thread_history', 'canon_facts', 'ledger_events',
             'reveal_events', 'secrets', 'episode_links', 'episode_link_runs',
             'monthly_digests', 'digest_episodes', 'user_understandings', 'knowing_runs')


class ImportStore:
    @contextmanager
    def import_journal(self, db, batch_id):
        """Connection-local triggers: changes and their undo records commit together.

        No full-table snapshots, and no triggers left on ordinary chat connections.
        """
        if not batch_id:
            yield
            return
        db.create_function('cbs_import_batch', 0, lambda: batch_id)
        names = []
        try:
            for table in JOURNALED:
                cols = list(db.execute(f'PRAGMA table_info({table})'))
                def row(alias):
                    return 'json_object(' + ','.join(f"'{c['name']}'," +
                        (f"CASE WHEN {alias}.{c['name']} IS NULL THEN NULL ELSE hex({alias}.{c['name']}) END"
                         if c['name'] == 'embedding' else (f'trace_text({alias}.{c["name"]})' if c['name'] == 'trace_json' else f'{alias}.{c["name"]}')) for c in cols) + ')'
                for op in ('INSERT', 'UPDATE'):
                    name = f'im_{table}_{op}'
                    names.append(name)
                    before = row('OLD') if op == 'UPDATE' else 'NULL'
                    db.execute(f'CREATE TEMP TRIGGER {name} AFTER {op} ON {table} BEGIN '
                        f'INSERT INTO import_changes(batch_id,table_name,before_json,after_json) '
                        f"VALUES(cbs_import_batch(),'{table}',{before},{row('NEW')}); END")
            yield
        finally:
            for name in names:
                db.execute(f'DROP TRIGGER IF EXISTS temp.{name}')

    def import_batch_for_job(self, db, job_id):
        row = db.execute('SELECT i.batch_id FROM import_steps s JOIN import_items i ON i.id=s.item_id WHERE s.job_id=?', (job_id,)).fetchone()
        return row[0] if row else None

    def imported_identity(self, character_id):
        with self.connect() as db:
            rows = db.execute("SELECT i.* FROM import_items i JOIN import_batches b ON b.id=i.batch_id WHERE i.character_id=? AND b.status NOT IN ('preview','quoted','undone')", (character_id,)).fetchall()
        rows = [r for r in rows if r['status'] not in ('duplicate','undone')]
        return {(r['format'], r['source_id'], r['content_hash']) for r in rows}, {r['content_hash'] for r in rows}

    def import_status(self, character_id, batch_id=None):
        with self.connect() as db:
            batch = db.execute('SELECT * FROM import_batches WHERE character_id=?' + (' AND id=?' if batch_id else '') +
                ' ORDER BY created_at DESC,id DESC LIMIT 1', (character_id,batch_id) if batch_id else (character_id,)).fetchone()
            if not batch:
                raise KeyError(batch_id)
            counts = dict(db.execute('SELECT status,COUNT(*) FROM import_items WHERE batch_id=? GROUP BY status', (batch['id'],)).fetchall())
            total = sum(counts.values())
            steps = dict(db.execute('SELECT j.status,COUNT(*) FROM memory_jobs j JOIN import_steps p ON p.job_id=j.id JOIN import_items i ON i.id=p.item_id WHERE i.batch_id=? GROUP BY j.status', (batch['id'],)).fetchall())
        quote = json.loads(batch['quote_json'])
        return {**dict(batch), 'quote': quote, 'counts': counts, 'total': total,
                'completed': counts.get('completed',0), 'steps': steps,
                'percent': round(100*(counts.get('completed',0)+counts.get('duplicate',0))/total,1) if total else 100}

    def undo_import(self, character_id, batch_id):
        """Reverse this batch's writes; preserve subsequent changes field by field.

        Later batch journals are rebased too, so undoing A then B cannot resurrect A.
        Derived links/digests/understandings using removed evidence are invalidated.
        """
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('PRAGMA defer_foreign_keys=ON')
            batch = db.execute('SELECT * FROM import_batches WHERE id=? AND character_id=?', (batch_id,character_id)).fetchone()
            if not batch:
                raise KeyError(batch_id)
            if batch['status'] in ('running','cancelling'):
                raise ValueError('進んでいる取り込みを止めてから取り消してください。')
            if batch['status'] == 'undone':
                return
            self.undo_import_requests(db, character_id, batch_id)
            removed_episodes, removed_observations, affected_interests = set(), set(), set()
            changes = db.execute('SELECT * FROM import_changes WHERE batch_id=? ORDER BY id DESC', (batch_id,)).fetchall()
            for change in changes:
                table = change['table_name']
                if table not in JOURNALED:
                    raise ValueError('Invalid import journal')
                before = json.loads(change['before_json']) if change['before_json'] else None
                after = json.loads(change['after_json'])
                if table == 'interests':
                    affected_interests.add(after['id'])
                elif table == 'interest_evidence':
                    affected_interests.add(after['interest_id'])
                primary = [c['name'] for c in db.execute(f'PRAGMA table_info({table})') if c['pk']]
                where = ' AND '.join(f'{k} IS ?' for k in primary)
                keys = [after[k] for k in primary]
                current = db.execute(f'SELECT * FROM {table} WHERE {where}', keys).fetchone()
                if before is None:
                    if table == 'memory_episodes':
                        removed_episodes.add(after['id'])
                    if table == 'user_observations':
                        removed_observations.add(after['id'])
                    if table == 'reveal_events':
                        db.execute("DELETE FROM note_entries WHERE character_id=? AND kind='past' AND source_id=?",(character_id,after['id']))
                    # If later evidence refers to an imported interest, retain the
                    # independently learned evidence and its node.
                    if table == 'interests' and db.execute('SELECT 1 FROM interest_evidence WHERE interest_id=? AND source_turn NOT IN (SELECT t.id FROM turns t JOIN import_items i ON i.session_id=t.session_id WHERE i.batch_id=?)', (after['id'],batch_id)).fetchone():
                        continue
                    db.execute(f'DELETE FROM {table} WHERE {where}', keys)
                elif current:
                    for col, value in before.items():
                        if col in primary or value == after[col]:
                            continue
                        actual = current[col].hex().upper() if isinstance(current[col],bytes) else current[col]
                        if col == 'trace_json':
                            actual = compact_trace(json.loads(actual))
                            expected = compact_trace(json.loads(after[col]))
                        else:
                            expected = after[col]
                        if actual == expected:
                            restored = bytes.fromhex(value) if col == 'embedding' and value is not None else value
                            if col == 'trace_json':
                                restored = dump_trace(json.loads(restored))
                            db.execute(f'UPDATE {table} SET {col}=? WHERE {where}', [restored,*keys])
                        for later in db.execute('SELECT * FROM import_changes WHERE id>? AND batch_id<>? AND table_name=? AND before_json IS NOT NULL', (change['id'],batch_id,table)).fetchall():
                            old = json.loads(later['before_json'])
                            if all(old[k] == after[k] for k in primary) and old.get(col) == after[col]:
                                old[col] = value
                                db.execute('UPDATE import_changes SET before_json=? WHERE id=?', (json.dumps(old,ensure_ascii=False),later['id']))
            # New derived records may have been produced by ordinary maintenance.
            for eid in removed_episodes:
                digests = [r[0] for r in db.execute('SELECT digest_id FROM digest_episodes WHERE episode_id=?',(eid,))]
                for did in digests:
                    db.execute("UPDATE memory_episodes SET status='active' WHERE id IN (SELECT episode_id FROM digest_episodes WHERE digest_id=?)",(did,))
                    db.execute('DELETE FROM digest_episodes WHERE digest_id=?',(did,))
                    db.execute('DELETE FROM monthly_digests WHERE id=?',(did,))
                db.execute('DELETE FROM episode_links WHERE source_episode=? OR target_episode=?',(eid,eid))
                db.execute('DELETE FROM episode_link_runs WHERE source_episode=?',(eid,))
            for row in db.execute('SELECT * FROM user_understandings WHERE character_id=?',(character_id,)).fetchall():
                if removed_observations.intersection(json.loads(row['support_json'])+json.loads(row['counter_json'])):
                    db.execute("UPDATE user_understandings SET status='forgotten',text='',condition='',support_json='[]',counter_json='[]' WHERE id=?",(row['id'],))
            # Evidence can also be referenced by notes created after import.
            for item in db.execute('SELECT * FROM import_items WHERE batch_id=?',(batch_id,)).fetchall():
                sid = item['session_id']
                if not sid:
                    continue
                tids = [r[0] for r in db.execute('SELECT id FROM turns WHERE session_id=?',(sid,))]
                for tid in tids:
                    db.execute('DELETE FROM pending_corrections WHERE source_turn=?',(tid,))
                    db.execute('UPDATE pending_corrections SET consumed_turn=NULL WHERE consumed_turn=?',(tid,))
                db.execute('DELETE FROM import_steps WHERE item_id=?',(item['id'],))
                db.execute('DELETE FROM memory_jobs WHERE conversation_id IN (SELECT id FROM conversations WHERE session_id=?)',(sid,))
                db.execute('DELETE FROM turns WHERE session_id=?',(sid,))
                db.execute('DELETE FROM conversations WHERE session_id=?',(sid,))
                db.execute("UPDATE import_items SET session_id=NULL,status='undone',payload_json='{}' WHERE id=?",(item['id'],))
                db.execute('DELETE FROM sessions WHERE id=?',(sid,))
            for iid in affected_interests:
                interest = db.execute('SELECT * FROM interests WHERE id=?',(iid,)).fetchone()
                if not interest:
                    continue
                evidence = db.execute('SELECT e.*,COALESCE(t.assistant_at,t.created_at,a.created_at,e.local_day) AS at FROM interest_evidence e LEFT JOIN turns t ON t.id=e.source_turn LEFT JOIN activities a ON a.id=e.source_activity WHERE e.interest_id=?',(iid,)).fetchall()
                if not evidence and interest['origin']!='author' and interest['migrated_canon'] is None:
                    db.execute('DELETE FROM interest_changes WHERE interest_id=?',(iid,))
                    db.execute('DELETE FROM interests WHERE id=?',(iid,))
                elif evidence:
                    count = len({(e['conversation_id'],e['source_activity'],e['local_day']) for e in evidence})
                    db.execute('UPDATE interests SET first_seen=?,last_seen=?,mention_count=? WHERE id=?',
                        (min(e['at'] for e in evidence),max(e['at'] for e in evidence),count,iid))
            db.execute("UPDATE import_batches SET status='undone',updated_at=? WHERE id=?",(clock.iso(),batch_id))
            db.execute("UPDATE import_reviews SET text='',candidates_json='[]' WHERE id=? AND character_id=?",(batch_id,character_id))
            db.execute('DELETE FROM import_changes WHERE batch_id=?',(batch_id,))
            self._knowing_revision(db,character_id)
