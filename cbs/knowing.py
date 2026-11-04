"""M11: evidence-backed understanding. LLM interprets; code validates and counts."""
from cbs.llm.cost import for_step
from datetime import timedelta
import json
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from cbs import clock

Kind = Literal['interaction', 'interest', 'situation', 'shared']
LABELS = {'tentative': '〜みたい', 'probable': 'たぶん', 'known': '知っている'}


class KnowingField(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Observation(KnowingField):
    kind: Kind
    summary: str = Field(min_length=1, max_length=1200)
    response: Literal['positive', 'negative', 'neutral', 'unclear']
    source_turns: list[int] = Field(min_length=1, max_length=40)
    private: bool = False


class Understanding(KnowingField):
    previous_id: int | None = None
    relation: Literal['new', 'same', 'strengthened', 'weakened', 'replaced', 'retired']
    kind: Kind
    text: str = Field(min_length=1, max_length=1000)
    condition: str = Field(max_length=1000)
    support: list[int] = Field(max_length=100)
    counter: list[int] = Field(default_factory=list, max_length=100)
    private: bool = False


class Understandings(KnowingField):
    llm_role: ClassVar[str] = 'steward'
    understandings: list[Understanding] = Field(default_factory=list, max_length=30)


SCHEMA = """
CREATE TABLE IF NOT EXISTS user_observations (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, job_id INTEGER REFERENCES memory_jobs(id),
 kind TEXT NOT NULL, summary TEXT NOT NULL, response TEXT NOT NULL,
 source_turns_json TEXT NOT NULL, observed_at TEXT NOT NULL, created_at TEXT NOT NULL,
 suppressed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_understandings (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, kind TEXT NOT NULL,
 text TEXT NOT NULL, condition TEXT NOT NULL, support_json TEXT NOT NULL, counter_json TEXT NOT NULL,
 relation TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', confirmed_at TEXT,
 first_observed_at TEXT NOT NULL, last_observed_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowing_settings (
 character_id TEXT PRIMARY KEY, stopped INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS knowing_actions (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, understanding_id INTEGER,
 action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowing_questions (
 character_id TEXT NOT NULL, understanding_id INTEGER NOT NULL REFERENCES user_understandings(id),
 source_turn INTEGER NOT NULL REFERENCES turns(id), asked_at TEXT NOT NULL,
 PRIMARY KEY(understanding_id, source_turn)
);
CREATE TABLE IF NOT EXISTS knowing_runs (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, local_day TEXT NOT NULL,
 status TEXT NOT NULL, trace_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_character ON user_observations(character_id, id);
CREATE TABLE IF NOT EXISTS private_preferences (
 character_id TEXT NOT NULL, category TEXT NOT NULL, allow_mention INTEGER NOT NULL,
 PRIMARY KEY(character_id, category)
);
"""


def stage(item, observations, cfg, current=None):
    """Evidence dates, never LLM confidence. Recompute at read time to avoid stale stages."""
    now = clock.parse(current) if current else clock.now()
    support = [observations[i] for i in item['support'] if i in observations]
    counter = [observations[i] for i in item['counter'] if i in observations]
    days = sorted({clock.parse(o['observed_at']).date() for o in support})
    if not days:
        return {**item, 'stage': 'tentative', 'stage_label': LABELS['tentative'], 'expired': True}
    first, last = min(o['observed_at'] for o in support), max(o['observed_at'] for o in support)
    latest_counter = max((o['observed_at'] for o in counter), default=None)
    confirmed = item.get('confirmed_at')
    contested = latest_counter and clock.parse(latest_counter) >= clock.parse(last) and (not confirmed or clock.parse(latest_counter) > clock.parse(confirmed))
    value = 'tentative'
    if not contested:
        if confirmed or (len(days) >= cfg.known_min_days and (days[-1]-days[0]).days >= cfg.known_min_span_days):
            value = 'known'
        elif len(days) >= cfg.probable_min_days:
            value = 'probable'
    last_confirmation = max(clock.parse(last),clock.parse(confirmed)) if confirmed else clock.parse(last)
    age = max(0, (now-last_confirmation).total_seconds()/86400)
    expires = (last_confirmation+timedelta(days=cfg.situation_days)).isoformat() if item['kind'] == 'situation' else None
    if item['kind'] in ('interaction', 'interest') and age >= cfg.fade_days:
        value = {'known': 'probable', 'probable': 'tentative', 'tentative': 'tentative'}[value]
    return {**item, 'stage': value, 'stage_label': LABELS[value], 'days': len(days),
            'first_observed_at': first, 'last_observed_at': last, 'expires_at': expires,
            'expired': bool(expires and now >= clock.parse(expires)), 'contested': bool(contested)}


class KnowingStore:
    def private_catalog(self):
        from cbs.config import KnowingConfig
        return getattr(self, 'knowing_config', None) or KnowingConfig()

    def private_settings(self, character_id, db=None):
        if db is None:
            with self.connect() as connection:
                return self.private_settings(character_id, connection)
        overrides = {r['category']: bool(r['allow_mention']) for r in db.execute(
            'SELECT * FROM private_preferences WHERE character_id=?', (character_id,))}
        return {key: {'label': value.label, 'allow_mention': overrides.get(key, value.allow_mention)}
                for key, value in self.private_catalog().private_categories.items()}

    def fact_policy(self, fact, db=None):
        fact = dict(fact)
        if not fact.get('private'):
            return {**fact, 'allow_mention': True, 'may_initiate': False}
        settings = self.private_settings(fact['character_id'], db)
        category = fact.get('private_category')
        category = category if category in settings else 'other'
        override = fact.get('mention_override')
        allowed = bool(override) if override is not None else settings[category]['allow_mention']
        return {**fact, 'private_category': category, 'category_label': settings[category]['label'],
                'category_allow_mention': settings[category]['allow_mention'],
                'mention_override': None if override is None else bool(override),
                'allow_mention': allowed, 'may_initiate': False}

    def private_context(self, character_id):
        return [{k: f[k] for k in ('id','content','private_category','allow_mention','may_initiate')}
                for f in self.list_user_facts(character_id, current_only=True) if f['private']]

    def set_private_settings(self, character_id, values):
        if set(values) - self.private_catalog().private_categories.keys():
            raise ValueError('種類を確認してください。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for category, allowed in values.items():
                db.execute('INSERT INTO private_preferences VALUES (?,?,?) ON CONFLICT(character_id,category) DO UPDATE SET allow_mention=excluded.allow_mention',
                           (character_id, category, allowed))
            self._sync_private_threads(db, character_id)

    def _sync_private_threads(self, db, character_id):
        # Pause permission-blocked follow-ups; never reopen used/expired/dropped ones.
        for thread in db.execute("SELECT * FROM threads WHERE character_id=? AND status IN ('open','blocked') AND source_ref LIKE 'fact:%'", (character_id,)).fetchall():
            fact = db.execute("SELECT * FROM user_facts WHERE character_id=? AND id=? AND status='current'",
                              (character_id, thread['source_ref'][5:])).fetchone()
            if not fact:
                self._thread_status(db, thread, 'dropped')
            elif fact['private']:
                self._thread_status(db, thread, 'open' if self.fact_policy(fact, db)['allow_mention'] else 'blocked')
        # Enabling a category/fact also applies to a pending event learned while disabled.
        from cbs.memory import UserFact
        for fact in db.execute("SELECT * FROM user_facts WHERE character_id=? AND status='current' AND private=1 AND follow_up=1 AND event_date IS NOT NULL", (character_id,)).fetchall():
            if self.fact_policy(fact, db)['allow_mention']:
                self._fact_thread(db,character_id,fact['id'],UserFact(operation='confirm',content=fact['content'],private=True,
                    private_category=fact['private_category'],importance=fact['importance'],pinned=bool(fact['pinned']),event_date=fact['event_date']),None)

    def knowing_setting(self, character_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM knowing_settings WHERE character_id=?', (character_id,)).fetchone()
        return dict(row) if row else {'character_id': character_id, 'stopped': 0, 'revision': 0}

    def list_observations(self, character_id, *, include_suppressed=False):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM user_observations WHERE character_id=?' +
                ('' if include_suppressed else ' AND suppressed=0') + ' ORDER BY id', (character_id,)).fetchall()
        return [{**dict(r), 'source_turns': json.loads(r['source_turns_json'])} for r in rows]

    def save_observations(self, db, character_id, job, observations, turns, revision=0):
        setting = db.execute('SELECT stopped, revision FROM knowing_settings WHERE character_id=?', (character_id,)).fetchone()
        if setting and (setting['stopped'] or setting['revision'] != revision):
            return
        by_id = {t['id']: t for t in turns}
        for observation in observations:
            refs = list(dict.fromkeys(i for i in observation.source_turns if i in by_id))
            if observation.private or not refs:
                continue
            db.execute('INSERT INTO user_observations (character_id,job_id,kind,summary,response,source_turns_json,observed_at,created_at) VALUES (?,?,?,?,?,?,?,?)',
                (character_id, job['id'], observation.kind, observation.summary, observation.response,
                 json.dumps(refs), max(by_id[i]['created_at'] for i in refs), max(by_id[i]['created_at'] for i in refs)))

    def list_understandings(self, character_id, cfg, *, include_inactive=False):
        observations = {o['id']: o for o in self.list_observations(character_id)}
        with self.connect() as db:
            rows = db.execute('SELECT * FROM user_understandings WHERE character_id=?' +
                ('' if include_inactive else " AND status IN ('active','review')") + ' ORDER BY id', (character_id,)).fetchall()
        result = []
        for r in rows:
            item = {**dict(r), 'support': json.loads(r['support_json']), 'counter': json.loads(r['counter_json'])}
            item = stage(item, observations, cfg)
            item['evidence'] = [observations[i] for i in item['support'] if i in observations]
            item['counterevidence'] = [observations[i] for i in item['counter'] if i in observations]
            result.append(item)
        return result

    def apply_understandings(self, character_id, output, allowed, revision, *, batch_id=None):
        applied, warnings = [], []
        with self.connect() as db, self.import_journal(db,batch_id):
            db.execute('BEGIN IMMEDIATE')
            setting = db.execute('SELECT * FROM knowing_settings WHERE character_id=?', (character_id,)).fetchone()
            if setting and (setting['stopped'] or setting['revision'] != revision):
                return {'applied': [], 'warnings': ['user controls changed during rebuild; stale output dropped']}
            available = {r['id']: dict(r) for r in db.execute('SELECT * FROM user_observations WHERE character_id=? AND suppressed=0', (character_id,)) if r['id'] in allowed}
            seen = set()
            for u in output.understandings:
                old = db.execute("SELECT * FROM user_understandings WHERE id=? AND character_id=? AND status IN ('active','review')", (u.previous_id, character_id)).fetchone()
                if u.private or (u.previous_id is not None and (not old or u.previous_id in seen)):
                    warnings.append('private or invalid/duplicate previous_id dropped')
                    continue
                if (old is None and u.relation != 'new') or (old is not None and u.relation == 'new'):
                    warnings.append('invalid previous relation dropped')
                    continue
                seen.add(u.previous_id) if u.previous_id is not None else None
                if u.relation == 'retired' and old:
                    db.execute("UPDATE user_understandings SET status='retired', relation='retired', updated_at=? WHERE id=?", (clock.iso(), old['id']))
                    applied.append(old['id'])
                    continue
                support = list(dict.fromkeys(i for i in u.support if i in available and available[i]['kind'] == u.kind))
                counter = list(dict.fromkeys(i for i in u.counter if i in available and i not in support and available[i]['kind'] == u.kind))
                if set(u.support+u.counter) - set(support+counter):
                    warnings.append('invalid observation IDs dropped')
                if not support or (old and old['kind'] != u.kind):
                    continue
                first, last = min(available[i]['observed_at'] for i in support), max(available[i]['observed_at'] for i in support)
                # User disagreement remains counterevidence until explicitly addressed by a later review.
                if old:
                    counter = list(dict.fromkeys(counter + [i for i in json.loads(old['counter_json']) if i in available and i not in support]))
                    confirmed = old['confirmed_at'] if u.relation in ('same', 'strengthened') else None
                    db.execute("UPDATE user_understandings SET text=?, condition=?, support_json=?, counter_json=?, relation=?, status='active', confirmed_at=?, first_observed_at=?,last_observed_at=?,updated_at=? WHERE id=?",
                        (u.text, u.condition, json.dumps(support), json.dumps(counter), u.relation, confirmed, first, last, clock.iso(), old['id']))
                    applied.append(old['id'])
                else:
                    # Exact duplicate evidence/text is a storage duplicate, not a semantic match.
                    if db.execute("SELECT 1 FROM user_understandings WHERE character_id=? AND text=? AND support_json=? AND status='active'", (character_id, u.text, json.dumps(support))).fetchone():
                        continue
                    row = db.execute('INSERT INTO user_understandings (character_id,kind,text,condition,support_json,counter_json,relation,first_observed_at,last_observed_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (character_id,u.kind,u.text,u.condition,json.dumps(support),json.dumps(counter),u.relation,first,last,clock.iso()))
                    applied.append(row.lastrowid)
        return {'applied': applied, 'warnings': warnings}

    def understanding_action(self, character_id, understanding_id, action, detail=''):
        if action not in ('agree', 'disagree', 'forget'):
            raise ValueError('操作を確認してください。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            item = db.execute("SELECT * FROM user_understandings WHERE id=? AND character_id=? AND status IN ('active','review')", (understanding_id, character_id)).fetchone()
            if not item:
                raise KeyError(understanding_id)
            self._knowing_revision(db, character_id)
            db.execute('INSERT INTO knowing_actions (character_id,understanding_id,action,detail,created_at) VALUES (?,?,?,?,?)', (character_id, understanding_id, action, detail, clock.iso()))
            if action == 'agree':
                db.execute("UPDATE user_understandings SET confirmed_at=?,status='active',updated_at=? WHERE id=?", (clock.iso(),clock.iso(),understanding_id))
            elif action == 'disagree':
                counter = json.loads(item['counter_json'])
                setting = db.execute('SELECT stopped FROM knowing_settings WHERE character_id=?',(character_id,)).fetchone()
                if not setting['stopped']:
                    row = db.execute('INSERT INTO user_observations (character_id,kind,summary,response,source_turns_json,observed_at,created_at) VALUES (?,?,?,?,?,?,?)',
                        (character_id,item['kind'],'ユーザーはこの理解を「違う」と訂正した。', 'negative','[]',clock.iso(),clock.iso()))
                    counter.append(row.lastrowid)
                db.execute("UPDATE user_understandings SET status='review',confirmed_at=NULL,counter_json=?,updated_at=? WHERE id=?", (json.dumps(counter),clock.iso(),understanding_id))
            else:
                self._forget_understanding(db, item)

    def _forget_understanding(self, db, item):
        refs = json.loads(item['support_json'])+json.loads(item['counter_json'])
        for oid in refs:
            db.execute('UPDATE user_observations SET suppressed=1 WHERE id=? AND character_id=?', (oid,item['character_id']))
        db.execute("UPDATE user_understandings SET status='forgotten',text='',condition='',confirmed_at=NULL,updated_at=? WHERE id=?", (clock.iso(),item['id']))

    def _knowing_revision(self, db, character_id):
        db.execute('INSERT INTO knowing_settings (character_id,revision) VALUES (?,1) ON CONFLICT(character_id) DO UPDATE SET revision=revision+1', (character_id,))

    def set_knowing(self, character_id, stopped, clear=False):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._knowing_revision(db, character_id)
            db.execute('UPDATE knowing_settings SET stopped=? WHERE character_id=?', (stopped,character_id))
            if clear:
                for item in db.execute("SELECT * FROM user_understandings WHERE character_id=? AND status IN ('active','review')", (character_id,)).fetchall():
                    self._forget_understanding(db,item)
                db.execute('UPDATE user_observations SET suppressed=1 WHERE character_id=?', (character_id,))
            db.execute('INSERT INTO knowing_actions (character_id,action,detail,created_at) VALUES (?,?,?,?)', (character_id,'stop' if stopped else 'resume','clear' if clear else 'keep',clock.iso()))

    def knowing_question_allowed(self, character_id, understanding_id, cfg, current=None):
        now = clock.parse(current) if current else clock.now()
        with self.connect() as db:
            rows = db.execute('SELECT * FROM knowing_questions WHERE character_id=?', (character_id,)).fetchall()
        return (sum(clock.parse(r['asked_at']).date() == now.date() for r in rows) < cfg.questions_daily_max and
                not any(r['understanding_id'] == understanding_id and (now-clock.parse(r['asked_at'])).total_seconds() < cfg.question_gap_days*86400 for r in rows))

    def record_knowing_questions(self, db, character_id, turn_id, output, packet):
        cfg = self.knowing_config
        allowed = {i['ref']: i for i in packet.get('user_understandings', []) if i.get('may_ask')}
        at = db.execute('SELECT COALESCE(assistant_at,created_at) FROM turns WHERE id=?', (turn_id,)).fetchone()[0]
        for ref in dict.fromkeys(output.get('understanding_questions', [])):
            item = allowed.get(ref)
            exists = item and db.execute("SELECT 1 FROM user_understandings WHERE id=? AND character_id=? AND status='active'", (item['id'],character_id)).fetchone()
            if exists and self.knowing_question_allowed(character_id,item['id'],cfg,at):
                db.execute('INSERT OR IGNORE INTO knowing_questions VALUES (?,?,?,?)', (character_id,item['id'],turn_id,at))


def candidates(pipeline, query_vector=None):
    from cbs.memory import cosine
    from cbs.storage import unpack_vector
    store, cid, cfg = pipeline.store, pipeline.config.character.id, pipeline.config.knowing
    if store.knowing_setting(cid)['stopped']:
        return []
    items = [i for i in store.list_understandings(cid,cfg) if not i['expired'] and i['status'] == 'active']
    # Reuse source-turn memory embeddings. No new foreground API call or keyword heuristics.
    episodes = store.list_memory_episodes(cid)
    turns = {o['id']: set(o['source_turns']) for o in store.list_observations(cid)}
    with store.connect() as db:
        jobs = {r['id']: (r['first_turn'], r['last_turn']) for r in db.execute('SELECT * FROM memory_jobs')}
    def score(i):
        source = set().union(*(turns.get(oid,set()) for oid in i['support']))
        # Episode -> job gives exact source ranges, unlike broad conversation matching.
        vectors = [unpack_vector(e['embedding']) for e in episodes if e.get('embedding') and e['job_id'] in jobs and
                   any(jobs[e['job_id']][0] <= t <= jobs[e['job_id']][1] for t in source)]
        return max((cosine(query_vector,v) for v in vectors),default=0) if query_vector else 0
    scores = {i['id']:score(i) for i in items}
    items.sort(key=lambda i: (scores[i['id']],i['last_observed_at'],i['id']),reverse=True)
    result = []
    for i in items[:cfg.candidate_max]:
        result.append({k:i[k] for k in ('id','kind','text','condition','stage','stage_label','first_observed_at','last_observed_at','expires_at')})
        result[-1].update(ref=f"understanding:{i['id']}", similarity=scores[i['id']],
                          may_ask=i['stage']=='tentative' and store.knowing_question_allowed(cid,i['id'],cfg))
    return result


def rebuild(pipeline, *, force=False, batch_id=None):
    from cbs.runtime import read_prompt, character_prefix
    store, cid, cfg = pipeline.store, pipeline.config.character.id, pipeline.config.knowing
    setting = store.knowing_setting(cid)
    if setting['stopped']:
        return {'skipped': 'stopped'}
    observations = sorted(store.list_observations(cid),key=lambda o:(o['observed_at'],o['id']))[-cfg.observation_max:]
    if not observations:
        return {'skipped': 'no observations'}
    today = clock.now().date().isoformat()
    with store.connect() as db:
        if not force and db.execute("SELECT 1 FROM knowing_runs WHERE character_id=? AND local_day=?", (cid,today)).fetchone():
            return {'skipped': 'already maintained'}
    with store.connect() as db:
        feedback = [dict(r) for r in db.execute("SELECT understanding_id,action,detail,created_at FROM knowing_actions WHERE character_id=? AND action='disagree' ORDER BY id DESC LIMIT 30",(cid,))]
    context = {'observations': observations, 'previous_understandings': store.list_understandings(cid,cfg), 'user_corrections':feedback, 'current_datetime':clock.iso()}
    trace = {}
    status = 'failed'
    try:
        from cbs.display_text import display_call
        result = display_call(lambda: for_step(pipeline.client, 'knowing', 'memory').generate_json(pipeline.config.models.steward,read_prompt('knowing')+character_prefix(pipeline.character, actor=True, store=pipeline.store),
            [{'role':'user','content':json.dumps(context,ensure_ascii=False)}],Understandings),
            lambda r: [text for u in r.parsed.understandings for text in (u.text, u.condition)])
        trace = pipeline._safe_trace(result.trace())
        output = Understandings.model_validate_json(pipeline.client.sanitize_text(result.parsed.model_dump_json()))
        trace['application'] = store.apply_understandings(cid,output,{o['id'] for o in observations},setting['revision'],batch_id=batch_id)
        status = 'completed'
    except Exception as error:
        trace = pipeline._safe_trace(getattr(error,'trace',None) or trace)
        trace['error'] = '理解の見直しに失敗しました。観察と前の理解を保持し、次の日に再試行します。'
    with store.connect() as db, store.import_journal(db,batch_id):
        db.execute('INSERT INTO knowing_runs (character_id,local_day,status,trace_json,created_at) VALUES (?,?,?,?,?)', (cid,today,status,json.dumps(trace,ensure_ascii=False),clock.iso()))
    return {'status':status,**trace.get('application',{})}
