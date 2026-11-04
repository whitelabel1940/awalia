"""Current-self persistence and time rules. Topic identity is decided by the LLM."""
from cbs.storage import pack_vector, unpack_vector
import json
import math

from cbs import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS reply_embeddings (cache_key TEXT PRIMARY KEY, embedding BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS interests (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, topic TEXT NOT NULL,
 kind TEXT NOT NULL, detail TEXT NOT NULL, origin TEXT NOT NULL,
 status TEXT NOT NULL, salience REAL NOT NULL DEFAULT 0.5,
 importance REAL NOT NULL DEFAULT 0.5, mention_count INTEGER NOT NULL DEFAULT 0,
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, embedding BLOB,
 revision INTEGER NOT NULL DEFAULT 0, author_key TEXT, migrated_canon INTEGER,
 UNIQUE(character_id, author_key), UNIQUE(migrated_canon)
);
CREATE TABLE IF NOT EXISTS interest_evidence (
 id INTEGER PRIMARY KEY, interest_id INTEGER NOT NULL REFERENCES interests(id),
 conversation_id INTEGER REFERENCES conversations(id), source_episode INTEGER REFERENCES memory_episodes(id),
 source_turn INTEGER REFERENCES turns(id), local_day TEXT NOT NULL, revision INTEGER NOT NULL,
 UNIQUE(interest_id, source_turn, revision)
);
CREATE TABLE IF NOT EXISTS interest_changes (
 id INTEGER PRIMARY KEY, interest_id INTEGER NOT NULL REFERENCES interests(id),
 operation TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL,
 source_turn INTEGER REFERENCES turns(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS now_summaries (
 character_id TEXT PRIMARY KEY, summary TEXT NOT NULL, local_day TEXT NOT NULL,
 source_change INTEGER NOT NULL DEFAULT 0
);
"""


def salience(item, current=None):
    base = item['importance'] + 0.3 * math.log1p(item['mention_count'])
    days = max(0, ((clock.parse(current) if current else clock.now()) - clock.parse(item['last_seen'])).total_seconds() / 86400)
    return base if item['origin'] == 'author' else base * 0.5 ** (days / 30)


def timed_interest(item, current=None):
    history = [{'operation': h['operation'], 'before': {k: v for k, v in (h['before'] or {}).items()
                if k in ('topic', 'kind', 'detail', 'status')},
                'after': {k: v for k, v in h['after'].items() if k in ('topic', 'kind', 'detail', 'status')},
                'created_at': h['created_at'], 'time': clock.stamp(h['created_at'], current)} for h in item.get('history', [])]
    return {**{k: v for k, v in item.items() if k in
               ('id', 'topic', 'kind', 'detail', 'origin', 'status', 'first_seen', 'last_seen', 'elaborate')},
            'ref': f"interest:{item['id']}", 'salience': salience(item, current),
            'history': history,
            'time': clock.stamp(item['first_seen'], current),
            'last_seen_time': clock.stamp(item['last_seen'], current)}


class InterestStore:
    def _interest_change(self, db, item_id, operation, before, source_turn=None, when=None):
        after = dict(db.execute('SELECT * FROM interests WHERE id=?', (item_id,)).fetchone())
        # Vectors are storage, not change-log content.
        before = {k: v for k, v in before.items() if k != 'embedding'} if before else None
        after.pop('embedding', None)
        db.execute('INSERT INTO interest_changes (interest_id, operation, before_json, after_json, source_turn, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                   (item_id, operation, json.dumps(before, ensure_ascii=False) if before else None,
                    json.dumps(after, ensure_ascii=False), source_turn, when or clock.iso()))

    def initialize_interests(self, character):
        cid = character.model.character_id
        items = character.interests or [{'topic': topic, 'detail': topic, 'kind': 'like',
                 'elaborate': character.expression.elaborate_default} for topic in character.expression.preferences]
        with self.connect() as db:
            for item in items:
                when = character.bootstrap_meta.get('created_at', clock.iso())
                row = db.execute("INSERT OR IGNORE INTO interests (character_id, topic, kind, detail, origin, status, first_seen, last_seen, author_key, elaborate) VALUES (?, ?, ?, ?, 'author', 'established', ?, ?, ?, ?)",
                                 (cid, item['topic'], item['kind'], item['detail'], when, when, item['topic'], item.get('elaborate', 'may_elaborate')))
                if row.rowcount:
                    self._interest_change(db, row.lastrowid, 'author', None)

    def migrate_preferences(self, db):
        for fact in db.execute("SELECT * FROM canon_facts WHERE kind='preference' ORDER BY id").fetchall():
            row = db.execute("INSERT OR IGNORE INTO interests (character_id, topic, kind, detail, origin, status, first_seen, last_seen, migrated_canon) VALUES (?, ?, 'opinion', ?, 'conversation', 'tentative', ?, ?, ?)",
                             (fact['character_id'], fact['content'], fact['content'], fact['created_at'], fact['created_at'], fact['id']))
            if row.rowcount:
                if fact['source_turn']:
                    turn = db.execute('SELECT * FROM turns WHERE id=?', (fact['source_turn'],)).fetchone()
                    db.execute('INSERT INTO interest_evidence (interest_id, conversation_id, source_turn, local_day, revision) VALUES (?, ?, ?, ?, 0)',
                               (row.lastrowid, turn['conversation_id'], turn['id'], clock.parse(turn['assistant_at'] or turn['created_at']).date().isoformat()))
                    db.execute('UPDATE interests SET mention_count=1 WHERE id=?', (row.lastrowid,))
                self._interest_change(db, row.lastrowid, 'migration', None, fact['source_turn'], fact['created_at'])
            db.execute('DELETE FROM canon_facts WHERE id=?', (fact['id'],))

    def list_interests(self, character_id):
        with self.connect() as db:
            result = [dict(r) for r in db.execute('SELECT * FROM interests WHERE character_id=? ORDER BY id', (character_id,))]
            for item in result:
                item['evidence'] = [dict(r) for r in db.execute('SELECT * FROM interest_evidence WHERE interest_id=? ORDER BY id', (item['id'],))]
                # Progress toward "established" for the current revision: distinct days and span in days.
                days = sorted({e['local_day'] for e in item['evidence'] if e['revision'] == item['revision']})
                item['progress'] = {'days': len(days), 'span_days': (clock.parse(days[-1]) - clock.parse(days[0])).days if days else 0}
                item['history'] = [{'operation': r['operation'], 'before': json.loads(r['before_json']) if r['before_json'] else None,
                                    'after': json.loads(r['after_json']), 'created_at': r['created_at']}
                                   for r in db.execute("SELECT * FROM interest_changes WHERE interest_id=? AND operation IN ('change', 'weaken', 'fade') ORDER BY id", (item['id'],))]
            return result

    def list_interest_changes(self, character_id):
        with self.connect() as db:
            return [{**dict(r), 'before': json.loads(r['before_json']) if r['before_json'] else None,
                     'after': json.loads(r['after_json'])} for r in db.execute(
                         'SELECT c.* FROM interest_changes c JOIN interests i ON i.id=c.interest_id WHERE i.character_id=? ORDER BY c.id', (character_id,))]

    def now_summary(self, character_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM now_summaries WHERE character_id=?', (character_id,)).fetchone()
            # Hide it only when an interest it may describe has since changed, weakened or faded.
            # New interests and more evidence (almost every activity) only add to it.
            stale = row and db.execute("SELECT 1 FROM interest_changes c JOIN interests i ON i.id=c.interest_id "
                "WHERE i.character_id=? AND c.id>? AND c.operation IN ('change','weaken','fade') LIMIT 1", (character_id, row['source_change'])).fetchone()
            return dict(row) if row and not stale else None

    def apply_interests(self, db, character_id, proposals, vectors, turns, episode_ids, conversation_id, warnings, activity_id=None):
        if proposals:
            self.check_embedding_model(db, writing=True)
        by_id = {t['id']: t for t in turns}
        changed = []
        for proposal, vector in zip(proposals, vectors):
            evidence = [by_id[t] for t in dict.fromkeys(proposal.evidence_turns) if t in by_id and by_id[t]['assistant_text']]
            if not evidence or proposal.source_episode >= len(episode_ids):
                warnings.append('interest requires valid evidence turns/source episode')
                continue
            when = max(t.get('assistant_at') or t['created_at'] for t in evidence)
            old = db.execute('SELECT * FROM interests WHERE id=? AND character_id=?', (proposal.interest_id, character_id)).fetchone()
            if proposal.operation != 'new' and not old:
                warnings.append(f'unknown interest: {proposal.interest_id}')
                continue
            if proposal.operation == 'new':
                if proposal.interest_id is not None:
                    warnings.append('new interest must have null interest_id')
                    continue
                row = db.execute("INSERT INTO interests (character_id, topic, kind, detail, origin, status, importance, first_seen, last_seen, embedding) VALUES (?, ?, ?, ?, ?, 'tentative', ?, ?, ?, ?)",
                                 (character_id, proposal.topic, proposal.kind, proposal.detail, 'activity' if activity_id else 'conversation', proposal.importance,
                                  min(t.get('assistant_at') or t['created_at'] for t in evidence), when, pack_vector(vector)))
                item_id, revision = row.lastrowid, 0
            else:
                old = dict(old)
                item_id, revision = old['id'], old['revision']
                if clock.parse(when) < clock.parse(old['last_seen']) and proposal.operation in ('change', 'weaken'):
                    # A historical statement is retained in its episode; it
                    # cannot replace a more recent position.
                    warnings.append('historical interest change retained in episode')
                    continue
                if old['origin'] == 'author' and proposal.operation in ('change', 'weaken'):
                    warnings.append('author interest protected')
                    continue
                if proposal.operation == 'change':
                    revision += 1
                    db.execute("UPDATE interests SET topic=?, kind=?, detail=?, importance=?, embedding=?, revision=?, status='tentative' WHERE id=?",
                               (proposal.topic, proposal.kind, proposal.detail, proposal.importance, pack_vector(vector), revision, item_id))
                elif proposal.operation == 'weaken':
                    db.execute("UPDATE interests SET topic=?, kind=?, detail=?, embedding=?, importance=importance*0.5, status='fading' WHERE id=?",
                               (proposal.topic, proposal.kind, proposal.detail, pack_vector(vector), item_id))
            for turn in evidence:
                db.execute('INSERT OR IGNORE INTO interest_evidence (interest_id, conversation_id, source_episode, source_turn, local_day, revision, source_activity) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (item_id, conversation_id, episode_ids[proposal.source_episode], turn.get('source_turn', turn['id']), clock.parse(turn.get('assistant_at') or turn['created_at']).date().isoformat(), revision, activity_id))
            days = db.execute('SELECT COUNT(DISTINCT local_day) FROM interest_evidence WHERE interest_id=? AND revision=?', (item_id, revision)).fetchone()[0]
            conversations = db.execute('SELECT COUNT(DISTINCT COALESCE(conversation_id, -source_activity)) FROM interest_evidence WHERE interest_id=? AND revision=?', (item_id, revision)).fetchone()[0]
            count = db.execute('SELECT COUNT(*) FROM (SELECT DISTINCT COALESCE(conversation_id, -source_activity), local_day FROM interest_evidence WHERE interest_id=?)', (item_id,)).fetchone()[0]
            first_day, last_day = db.execute('SELECT MIN(local_day), MAX(local_day) FROM interest_evidence WHERE interest_id=? AND revision=?',
                                             (item_id, revision)).fetchone()
            span = (clock.parse(last_day) - clock.parse(first_day)).days if first_day and last_day else 0
            rules = getattr(self, 'interest_rules', {'min_days': 3, 'min_span_days': 7})
            # Established = mentioned on enough distinct days, spread over enough time (config [memory]).
            status = 'established' if (old and old['origin'] == 'author') or (
                days >= rules['min_days'] and conversations >= 2 and span >= rules['min_span_days']) else 'tentative'
            if proposal.operation == 'weaken':
                status = 'fading'
            first_seen = min(t.get('assistant_at') or t['created_at'] for t in evidence)
            db.execute('UPDATE interests SET first_seen=MIN(first_seen,?),last_seen=MAX(last_seen, ?), mention_count=?, status=? WHERE id=?', (first_seen,when,count,status,item_id))
            item = dict(db.execute('SELECT * FROM interests WHERE id=?', (item_id,)).fetchone())
            db.execute('UPDATE interests SET salience=? WHERE id=?', (salience(item, when), item_id))
            self._interest_change(db, item_id, proposal.operation, old, evidence[-1].get('source_turn', evidence[-1]['id']), when)
            changed.append(item_id)
        return changed

    def fade_interests(self, character_id, days):
        with self.connect() as db:
            for row in db.execute('SELECT * FROM interests WHERE character_id=?', (character_id,)).fetchall():
                item = dict(row)
                db.execute('UPDATE interests SET salience=? WHERE id=?', (salience(item), item['id']))
                if item['origin'] == 'conversation' and item['status'] != 'fading' and (clock.now() - clock.parse(item['last_seen'])).total_seconds() >= days * 86400:
                    db.execute("UPDATE interests SET status='fading' WHERE id=?", (item['id'],))
                    self._interest_change(db, item['id'], 'fade', item)
