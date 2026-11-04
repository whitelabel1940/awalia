"""Autonomy state and limits; topic selection belongs to the LLM."""
import json
from datetime import timedelta

from cbs import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS inner_states (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, created_at TEXT NOT NULL,
 sentence TEXT NOT NULL, levels_json TEXT NOT NULL, trigger_ref TEXT NOT NULL,
 origin TEXT NOT NULL DEFAULT 'llm', current_expression TEXT, expression_at TEXT
);
CREATE TABLE IF NOT EXISTS monologues (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, created_at TEXT NOT NULL,
 text TEXT NOT NULL, trigger_ref TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inner_update_runs (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, created_at TEXT NOT NULL,
 trigger_ref TEXT NOT NULL, status TEXT NOT NULL, trace_json TEXT NOT NULL DEFAULT '{}',
 UNIQUE(character_id, trigger_ref)
);
CREATE TABLE IF NOT EXISTS threads (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, kind TEXT NOT NULL,
 topic TEXT NOT NULL, detail TEXT NOT NULL, source_ref TEXT NOT NULL,
 weight REAL NOT NULL, due_at TEXT, status TEXT NOT NULL DEFAULT 'open',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source_turn INTEGER,
 UNIQUE(character_id, kind, source_ref)
);
CREATE TABLE IF NOT EXISTS thread_history (
 id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL REFERENCES threads(id),
 operation TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS thread_history_used ON thread_history(thread_id, id) WHERE operation='used';
CREATE TABLE IF NOT EXISTS inner_life_runs (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, created_at TEXT NOT NULL,
 action TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, trace_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, run_id INTEGER NOT NULL REFERENCES inner_life_runs(id),
 kind TEXT NOT NULL, topic TEXT NOT NULL, summary TEXT NOT NULL,
 queries_json TEXT NOT NULL DEFAULT '[]', sources_json TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running'
);
CREATE INDEX IF NOT EXISTS activities_by_character_date ON activities(character_id, substr(created_at,1,10), id);
CREATE TABLE IF NOT EXISTS research_candidates (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, question TEXT NOT NULL,
 source_kind TEXT NOT NULL, source_ref TEXT NOT NULL, source_note TEXT NOT NULL,
 interest_id INTEGER REFERENCES interests(id) ON DELETE CASCADE, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open', used_activity INTEGER REFERENCES activities(id),
 UNIQUE(character_id, source_kind, source_ref, question)
);
CREATE TABLE IF NOT EXISTS autonomy_schedule (character_id TEXT PRIMARY KEY, last_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS world_facts (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, content TEXT NOT NULL,
 source_title TEXT NOT NULL, activity_id INTEGER NOT NULL REFERENCES activities(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS web_openings (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), opened_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proactive_runs (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id),
 conversation_id INTEGER NOT NULL REFERENCES conversations(id), mode TEXT NOT NULL,
 anchor INTEGER NOT NULL, created_at TEXT NOT NULL, speak INTEGER NOT NULL DEFAULT 0,
 reason TEXT NOT NULL DEFAULT '', thread_ref TEXT, source_turn INTEGER REFERENCES turns(id),
 trace_json TEXT NOT NULL DEFAULT '{}', UNIQUE(character_id, mode, anchor)
);
"""


def eligible(thread, when=None):
    return thread['status'] == 'open' and (thread['kind'] != 'follow_up' or not thread['due_at'] or
        clock.parse(thread['due_at']) <= (clock.parse(when) if when else clock.now()))


class AutonomyStore:
    def _research_rules(self):
        from cbs.config import AutonomyConfig
        return getattr(self, 'autonomy_config', AutonomyConfig())

    def _expire_research_candidates(self, db, character_id):
        db.execute("UPDATE research_candidates SET status='expired' WHERE character_id=? AND status='open' AND julianday(expires_at)<=julianday(?)",
                   (character_id, clock.iso()))
        db.execute("UPDATE research_candidates SET status='expired' WHERE character_id=? AND status='open' "
                   "AND interest_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM interests i WHERE i.id=interest_id "
                   "AND i.character_id=? AND i.kind='curiosity' AND i.status IN ('tentative','supported','well_supported','established'))",
                   (character_id, character_id))
        # Lowering the configured cap also bounds already saved candidates.
        db.execute("UPDATE research_candidates SET status='expired' WHERE id IN ("
                   "SELECT id FROM research_candidates WHERE character_id=? AND status='open' "
                   "ORDER BY julianday(created_at) DESC,id DESC LIMIT -1 OFFSET ?)",
                   (character_id, self._research_rules().research_candidate_max))

    def save_research_questions(self, db, character_id, questions, source_kind, source_ref,
                                source_note, *, interest_id=None, when=None):
        cfg = self._research_rules()
        self._expire_research_candidates(db, character_id)
        when = when or clock.iso()
        expires = clock.parse(when) + timedelta(days=cfg.research_candidate_expiry_days)
        if expires <= clock.now() or clock.parse(when) > clock.now():
            return
        for question in questions[:2]:
            question = question.strip()
            if not question or len(question) > 280:
                continue
            # Exact text only; retain used/expired identities so a poll cannot
            # resurrect the same question. No semantic similarity rules.
            if db.execute('SELECT 1 FROM research_candidates WHERE character_id=? AND question=?',
                          (character_id, question)).fetchone():
                continue
            count = db.execute("SELECT COUNT(*) FROM research_candidates WHERE character_id=? AND status='open'", (character_id,)).fetchone()[0]
            if count >= cfg.research_candidate_max:
                break
            db.execute('INSERT OR IGNORE INTO research_candidates '
                       '(character_id,question,source_kind,source_ref,source_note,interest_id,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?)',
                       (character_id, question, source_kind, source_ref, source_note[:180], interest_id, when, expires.isoformat()))

    def research_candidates(self, character_id):
        cfg = self._research_rules()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._expire_research_candidates(db, character_id)
            # Actual evidence time, rather than author initialization/last_seen.
            rows = db.execute("SELECT i.*,COALESCE(t.assistant_at,t.created_at,a.created_at,e.local_day) AS evidence_at,"
                "MAX(julianday(COALESCE(t.assistant_at,t.created_at,a.created_at,e.local_day))) AS evidence_day "
                "FROM interests i JOIN interest_evidence e ON e.interest_id=i.id "
                "LEFT JOIN turns t ON t.id=e.source_turn LEFT JOIN activities a ON a.id=e.source_activity "
                "WHERE i.character_id=? AND i.kind='curiosity' AND i.status IN ('tentative','supported','well_supported','established') "
                "AND (t.id IS NULL OR t.status='completed') AND (a.id IS NULL OR a.status='completed') "
                "AND julianday(COALESCE(t.assistant_at,t.created_at,a.created_at,e.local_day))<=julianday(?) "
                "GROUP BY i.id ORDER BY julianday(evidence_at) DESC,i.id", (character_id, clock.iso())).fetchall()
            for row in rows:
                if clock.parse(row['evidence_at']) + timedelta(days=cfg.research_candidate_expiry_days) <= clock.now():
                    continue
                self.save_research_questions(db, character_id, [row['detail'][:280]], 'interest', f"interest:{row['id']}",
                    row['topic'], interest_id=row['id'], when=row['evidence_at'])
            return [dict(r) for r in db.execute("SELECT * FROM research_candidates WHERE character_id=? AND status='open' "
                "AND julianday(created_at)<=julianday(?) ORDER BY julianday(created_at) DESC,id DESC LIMIT ?",
                (character_id, clock.iso(), cfg.research_candidate_max))]

    def interest_activity_coverage(self, character_id):
        """Union IDs so a chosen interest plus extracted evidence counts once.

        Each completed activity or evidenced conversation turn is one handling;
        duplicate revisions/evidence rows do not increase the count.
        """
        with self.connect() as db:
            rows = db.execute("SELECT interest_id,'activity:'||id AS ref,created_at AS at FROM activities "
                "WHERE character_id=? AND status='completed' AND interest_id IS NOT NULL UNION "
                "SELECT e.interest_id,'activity:'||a.id,a.created_at FROM interest_evidence e "
                "JOIN interests i ON i.id=e.interest_id JOIN activities a ON a.id=e.source_activity "
                "WHERE i.character_id=? AND a.character_id=? AND a.status='completed' UNION "
                "SELECT e.interest_id,'turn:'||t.id,COALESCE(t.assistant_at,t.created_at) FROM interest_evidence e "
                "JOIN interests i ON i.id=e.interest_id JOIN turns t ON t.id=e.source_turn JOIN sessions s ON s.id=t.session_id "
                "WHERE i.character_id=? AND s.character_id=? AND t.status='completed'",
                (character_id, character_id, character_id, character_id, character_id)).fetchall()
            ids = [r[0] for r in db.execute('SELECT id FROM interests WHERE character_id=?', (character_id,))]
        times = {i: [] for i in ids}
        for row in rows:
            at = clock.parse(row['at'])
            if row['interest_id'] in times and at <= clock.now():
                times[row['interest_id']].append(at)
        return {i: {'handled_last_7_days': sum(at > clock.now()-timedelta(days=7) for at in ats),
                    'days_since_last_handled': (clock.now()-max(ats)).days if ats else None}
                for i, ats in times.items()}

    def resident_expression(self, character_id, reply_minutes=10):
        """Reply reactions have a time window; inner expressions persist underneath."""
        from cbs.expression import EXPRESSIONS
        with self.connect() as db:
            inner = db.execute("SELECT id,current_expression FROM inner_states WHERE character_id=? "
                "AND origin='llm' AND current_expression IN (" + ','.join('?' for _ in EXPRESSIONS) +
                ") ORDER BY julianday(expression_at) DESC,id DESC LIMIT 1", (character_id, *EXPRESSIONS)).fetchone()
            reply = db.execute("SELECT t.id,t.expression,COALESCE(t.assistant_at,t.created_at) AS at "
                "FROM turns t JOIN sessions s ON s.id=t.session_id WHERE s.character_id=? "
                "AND t.status='completed' AND t.expression IN (" + ','.join('?' for _ in EXPRESSIONS) +
                ") ORDER BY julianday(COALESCE(t.assistant_at,t.created_at)) DESC,t.id DESC LIMIT 1",
                (character_id, *EXPRESSIONS)).fetchone()
        fallback = inner['current_expression'] if inner else 'neutral'
        remaining = ((clock.parse(reply['at']) + timedelta(minutes=reply_minutes) - clock.now()).total_seconds()
                     if reply else 0)
        active = remaining > 0
        return {'expression': reply['expression'] if active else fallback,
                'revision': reply['id'] if active else -inner['id'] if inner else 0,
                'fallbackExpression': fallback,
                'replyRemainingMs': max(0, int(remaining * 1000)) if active else None}

    def current_feeling(self, character_id):
        from cbs.inner import BASELINE, LEVELS
        from cbs.config import InnerConfig
        cfg = getattr(self, 'inner_config', InnerConfig())
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM inner_states WHERE character_id=? ORDER BY id DESC LIMIT 1', (character_id,)).fetchone()
            if not row:
                return None
            state = dict(row)
            levels = json.loads(state['levels_json'])
            at = clock.parse(state['created_at'])
            interval = timedelta(hours=cfg.baseline_hours)
            # Append only actual transitions, at their logical time. Repeated reads do not reset the clock.
            while at + interval <= clock.now() and levels != BASELINE:
                before = state['id']
                for key, value in levels.items():
                    slot, target = LEVELS[key].index(value), LEVELS[key].index(BASELINE[key])
                    levels[key] = LEVELS[key][slot + (1 if slot < target else -1 if slot > target else 0)]
                at += interval
                cursor = db.execute("INSERT INTO inner_states (character_id,created_at,sentence,levels_json,trigger_ref,origin) VALUES (?,?,?,?,?,'time')",
                    (character_id, at.isoformat(), state['sentence'], json.dumps(levels, ensure_ascii=False), f"inner:{before}"))
                state.update(id=cursor.lastrowid, created_at=at.isoformat(), trigger_ref=f'inner:{before}', origin='time',
                             levels_json=json.dumps(levels, ensure_ascii=False))
            return {**state, 'levels': levels, 'ref': f"inner:{state['id']}"}

    def list_inner_states(self, character_id):
        self.current_feeling(character_id)
        with self.connect() as db:
            return [{**dict(r), 'levels': json.loads(r['levels_json']), 'ref': f"inner:{r['id']}"}
                    for r in db.execute('SELECT * FROM inner_states WHERE character_id=? ORDER BY id', (character_id,))]

    def list_monologues(self, character_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM monologues WHERE character_id=? ORDER BY id', (character_id,))]

    def list_inner_updates(self, character_id):
        with self.connect() as db:
            return [{**dict(r), 'trace': json.loads(r['trace_json'])} for r in db.execute(
                'SELECT * FROM inner_update_runs WHERE character_id=? ORDER BY id', (character_id,))]

    def list_world_facts(self, character_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM world_facts WHERE character_id=? ORDER BY id', (character_id,))]

    def conversation_activities(self, character_id):
        # External adaptation materials remain in developer records only.
        result = self.list_activities(character_id, recent=True)
        for activity in result:
            if activity['kind'] == 'world_activity':
                activity['sources'] = [s for s in activity['sources'] if s.get('in_world')]
                activity['sources_json'] = json.dumps(activity['sources'], ensure_ascii=False)
                activity['queries'], activity['queries_json'] = [], '[]'
        return result

    def _thread_history(self, db, thread_id, operation, before=None):
        after = dict(db.execute('SELECT * FROM threads WHERE id=?', (thread_id,)).fetchone())
        db.execute('INSERT INTO thread_history (thread_id, operation, before_json, after_json, created_at) VALUES (?, ?, ?, ?, ?)',
                   (thread_id, operation, json.dumps(before, ensure_ascii=False) if before else None,
                    json.dumps(after, ensure_ascii=False), clock.iso()))

    def _thread(self, db, character_id, kind, topic, detail, source_ref, weight=0.5, due_at=None, source_turn=None):
        old = db.execute('SELECT * FROM threads WHERE character_id=? AND kind=? AND source_ref=?',
                         (character_id, kind, source_ref)).fetchone()
        if old:
            return old['id']
        cursor = db.execute('INSERT INTO threads (character_id, kind, topic, detail, source_ref, weight, due_at, created_at, updated_at, source_turn) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                            (character_id, kind, topic, detail, source_ref, weight, due_at, clock.iso(), clock.iso(), source_turn))
        self._thread_history(db, cursor.lastrowid, 'new')
        return cursor.lastrowid

    def add_thread(self, character_id, kind, topic, detail, source_ref, weight=0.5, due_at=None, source_turn=None):
        with self.connect() as db:
            return self._thread(db, character_id, kind, topic, detail, source_ref, weight, due_at, source_turn)

    def apply_share_choice(self, db, character_id, choice, candidates, topic, detail,
                           source_ref, weight=0.5, *, kind='share', daily=False):
        """Validate IDs/state; the LLM alone chooses new points and overlap."""
        if choice.operation == 'skip':
            return None
        if choice.operation == 'new':
            if choice.thread_ref is not None or daily:
                raise ValueError('Invalid new share reference')
            return self._thread(db, character_id, kind, topic, choice.detail.strip() or detail, source_ref, weight)
        candidate = next((t for t in candidates if t['ref'] == choice.thread_ref), None)
        if candidate is None:
            raise ValueError('Unknown share reference')
        row = db.execute("SELECT * FROM threads WHERE character_id=? AND id=? AND kind IN ('share','want') "
                         "AND status IN ('open','used')", (character_id, candidate['id'])).fetchone()
        if row is None or (daily and row['status'] != 'open') or (not daily and not choice.detail.strip()):
            raise ValueError('Invalid share append')
        if choice.detail.strip():
            db.execute("UPDATE threads SET detail=?,status='open',updated_at=? WHERE id=?",
                       (row['detail'] + '\n' + choice.detail.strip(), clock.iso(), row['id']))
            self._thread_history(db, row['id'], f'append:{source_ref}', dict(row))
        return row['id']

    def _fact_thread(self, db, character_id, fact_id, fact, source_turn):
        saved = db.execute('SELECT * FROM user_facts WHERE id=? AND character_id=?', (fact_id,character_id)).fetchone()
        policy = self.fact_policy(saved, db)
        if fact.private and not policy['allow_mention']:
            due = (clock.parse(fact.event_date) + timedelta(days=1)).isoformat() if fact.event_date else None
            for thread in db.execute("SELECT * FROM threads WHERE character_id=? AND status='open' AND (source_ref=? OR (id=? AND kind='follow_up' AND due_at=?))", (character_id,f'fact:{fact_id}',fact.follow_up_thread,due)).fetchall():
                self._thread_status(db,thread,'dropped')
            return
        if not fact.event_date or not fact.follow_up:
            if fact.private and not fact.follow_up:
                for thread in db.execute("SELECT * FROM threads WHERE character_id=? AND source_ref=? AND status='open'", (character_id,f'fact:{fact_id}')).fetchall():
                    self._thread_status(db,thread,'dropped')
            return
        due = (clock.parse(fact.event_date) + timedelta(days=1)).isoformat()
        # A past/current temporary event can be checked at the next conversation,
        # including later the same day. Future events retain the normal due date.
        if fact.private and policy['private_category'] == 'temporary_health' and fact.event_date <= clock.now().date():
            due = saved['learned_at']
        existing = db.execute("SELECT * FROM threads WHERE character_id=? AND kind='follow_up' AND id=? AND due_at=?",
                              (character_id, fact.follow_up_thread, due)).fetchone()
        if existing:
            # Resolve Extract's provisional utterance evidence to its persisted fact.
            if existing['source_ref'].startswith('turn:'):
                db.execute('UPDATE threads SET source_ref=?, updated_at=? WHERE id=?', (f'fact:{fact_id}', clock.iso(), existing['id']))
                self._thread_history(db, existing['id'], 'source', dict(existing))
            return
        self._thread(db, character_id, 'follow_up', fact.content, fact.content, f'fact:{fact_id}', fact.importance, due, source_turn)

    def _thread_status(self, db, thread, status):
        if thread['status'] == status:
            return
        db.execute('UPDATE threads SET status=?, updated_at=? WHERE id=?', (status, clock.iso(), thread['id']))
        self._thread_history(db, thread['id'], status, dict(thread))

    def expire_threads(self, character_id, days):
        with self.connect() as db:
            for t in db.execute("SELECT * FROM threads WHERE character_id=? AND status='open'", (character_id,)).fetchall():
                # A future dated event gets its full lifetime after it becomes due.
                base = max(clock.parse(t['created_at']), clock.parse(t['due_at'])) if t['due_at'] else clock.parse(t['created_at'])
                if clock.now() >= base + timedelta(days=days):
                    self._thread_status(db, t, 'expired')

    def list_threads(self, character_id, eligible_only=False, with_history=False):
        from cbs.config import InnerConfig
        decay = getattr(self, 'inner_config', InnerConfig()).want_decay_per_day
        with self.connect() as db:
            result = [dict(r) for r in db.execute('SELECT * FROM threads WHERE character_id=? ORDER BY weight DESC, id', (character_id,))]
            # Read only the latest use snapshot per thread, never its growing change log.
            told = {r['thread_id']: r for r in db.execute(
                "SELECT thread_id,created_at,after_json FROM thread_history WHERE id IN ("
                "SELECT MAX(h.id) FROM thread_history h JOIN threads t ON t.id=h.thread_id "
                "WHERE t.character_id=? AND h.operation='used' GROUP BY h.thread_id)", (character_id,))}
            for t in result:
                used = told.get(t['id'])
                told_detail = json.loads(used['after_json'])['detail'] if used else None
                t['told_at'] = clock.stamp(used['created_at']) if used else None
                t['told_detail'] = told_detail if used and told_detail != t['detail'] else None
                if t['source_ref'].startswith('fact:'):
                    fact = db.execute('SELECT * FROM user_facts WHERE id=? AND character_id=?', (t['source_ref'][5:],character_id)).fetchone()
                    if fact and fact['private']:
                        policy = self.fact_policy(fact, db)
                        t.update(private=True, private_category=policy['private_category'], allow_mention=policy['allow_mention'],
                                 may_initiate=policy['allow_mention'] and fact['status']=='current' and t['kind']=='follow_up' and eligible(t))
                days = max(0, (clock.now() - clock.parse(t['weight_at'] or t['created_at'])).total_seconds() / 86400)
                t['weight'] *= (1 - decay) ** days
                t['ref'] = f"thread:{t['id']}"
                # The change log grows with every update; only inspection screens need it, never model inputs.
                if with_history:
                    t['history'] = [{**dict(r), 'before': json.loads(r['before_json']) if r['before_json'] else None,
                                     'after': json.loads(r['after_json'])} for r in db.execute('SELECT * FROM thread_history WHERE thread_id=? ORDER BY id', (t['id'],))]
        result.sort(key=lambda t: (-t['weight'], t['id']))
        return [t for t in result if eligible(t) and (not t.get('private') or t['may_initiate'])] if eligible_only else result

    def sync_plan_threads(self, character_id):
        with self.connect() as db:
            for p in self.list_plans(character_id):
                ref = f"plan:{p['plan_id']}"
                old = db.execute("SELECT * FROM threads WHERE character_id=? AND kind='plan_followup' AND source_ref=?", (character_id, ref)).fetchone()
                if p['state'] in ('cancelled', 'reported_done'):
                    if old:
                        self._thread_status(db, old, 'dropped')
                    continue
                # New Ledger revisions may reopen a used followup, preserving its history.
                detail = p['content'] + ' / ' + (p['scheduled_date'] or p['scheduled_text'] or '日時未定')
                if old and (old['detail'] != detail or old['due_at'] != p['scheduled_date']):
                    db.execute("UPDATE threads SET detail=?, due_at=?, status='open', updated_at=? WHERE id=?",
                               (detail, p['scheduled_date'], clock.iso(), old['id']))
                    self._thread_history(db, old['id'], 'reschedule', dict(old))
                elif not old:
                    self._thread(db, character_id, 'plan_followup', p['content'], detail, ref, 0.8, p['scheduled_date'], p['source_turn'])

    def apply_threads(self, db, character_id, turn_id, output, packet):
        available = {t['ref']: t for t in packet.get('threads', [])}
        for ref in dict.fromkeys(output.get('used_threads', [])):
            if ref in available:
                row = db.execute("SELECT * FROM threads WHERE id=? AND character_id=? AND status='open'", (available[ref]['id'], character_id)).fetchone()
                if row and eligible(row):
                    self._thread_status(db, row, 'used')
        for t in output.get('new_threads', []):
            if t['event_date']:
                due = (clock.parse(t['event_date']) + timedelta(days=1)).isoformat()
                self._thread(db, character_id, 'follow_up', t['topic'], t['detail'],
                             f"turn:{turn_id}:{t['event_date']}:{t['topic']}", t['weight'], due, turn_id)

    def list_activities(self, character_id, recent=False):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM activities WHERE character_id=?' + (" AND status='completed'" if recent else '') +
                              ' ORDER BY id DESC' + (' LIMIT 3' if recent else ''), (character_id,)).fetchall()
        return [{**dict(r), 'ref': f"activity:{r['id']}", 'queries': json.loads(r['queries_json']), 'sources': json.loads(r['sources_json'])} for r in rows]

    def list_inner_life(self, character_id):
        with self.connect() as db:
            return [{**dict(r), 'trace': json.loads(r['trace_json'])} for r in db.execute('SELECT * FROM inner_life_runs WHERE character_id=? ORDER BY id DESC', (character_id,))]

    def list_proactive(self, character_id):
        with self.connect() as db:
            return [{**dict(r), 'trace': json.loads(r['trace_json'])} for r in db.execute('SELECT * FROM proactive_runs WHERE character_id=? ORDER BY id', (character_id,))]

    def autonomy_budget(self, character_id):
        today = clock.now().date().isoformat()
        with self.connect() as db:
            rows = db.execute("SELECT kind, COUNT(*) AS n FROM activities WHERE character_id=? AND substr(created_at,1,10)=? GROUP BY kind", (character_id, today)).fetchall()
            nested_searches = db.execute("SELECT COUNT(*) FROM activities WHERE character_id=? AND kind='world_activity' AND queries_json!='[]' AND substr(created_at,1,10)=?", (character_id, today)).fetchone()[0]
            nested_reads = db.execute("SELECT COUNT(*) FROM activities a JOIN reading_receipts r ON r.activity_id=a.id WHERE a.character_id=? AND a.kind='world_activity' AND substr(a.created_at,1,10)=?", (character_id, today)).fetchone()[0]
            # Every claimed judgment consumes the daily allowance, including silence/failure.
            idle = db.execute("SELECT COUNT(*) FROM proactive_runs WHERE character_id=? AND mode='idle' AND substr(created_at,1,10)=?", (character_id, today)).fetchone()[0]
        counts = {r['kind']: r['n'] for r in rows}
        from cbs.config import AutonomyConfig
        cfg = getattr(self, 'autonomy_config', AutonomyConfig())
        tools = {name: {'daily_max': (cfg.research_daily_max if name == 'research' and cfg.research_daily_max is not None else tool.daily_max),
                        'enabled': tool.enabled} for name, tool in ((n, getattr(cfg.tools, n)) for n in type(cfg.tools).model_fields)}
        for name, tool in tools.items():
            nested = nested_searches if name == 'research' else nested_reads if name == 'read_source' else 0
            tool['remaining'] = max(0, tool['daily_max'] - counts.get(name, 0) - nested)
        idle_max = (cfg.idle_rule() or (0, 0))[1]
        return {'day': today, 'tools': tools, 'activities_daily_max': cfg.activities_daily_max,
                'research_remaining': tools['research']['remaining'],
                'activities_remaining': max(0, cfg.activities_daily_max-sum(counts.values())),
                'idle_remaining': max(0, idle_max-idle), 'idle_daily_max': idle_max}
