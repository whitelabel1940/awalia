"""A public note contains only material already spoken or established."""
from cbs.llm.cost import for_step
import json
from typing import ClassVar
from pydantic import BaseModel, ConfigDict, Field
from cbs import clock
from cbs.runtime import read_prompt
from cbs.display_text import display_call


class DiaryEntry(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    llm_role: ClassVar[str] = 'actor'
    summary: str = Field(min_length=1, max_length=1200)
    partner_comment: str = Field(min_length=1, max_length=300)


def diaries(store, cid, *, before=None, limit=None):
    with store.connect() as db:
        rows = db.execute('SELECT day,summary,partner_comment,user_comment,comment_at FROM diaries '
            'WHERE character_id=?' + (' AND day<?' if before else '') + ' ORDER BY day DESC' +
            (' LIMIT ?' if limit else ''), (cid, *((before,) if before else ()), *((limit,) if limit else ()))).fetchall()
    return [dict(r) for r in rows]


def latest_diary(store, cid, *, before=None):
    entry = next(iter(diaries(store, cid, before=before, limit=1)), None)
    if entry:
        # An older page can receive the newest reply. Keep its date attached.
        with store.connect() as db:
            comment = db.execute("SELECT day,user_comment,comment_at FROM diaries WHERE character_id=? AND user_comment!=''" +
                (' AND day<?' if before else '') + ' ORDER BY comment_at DESC,day DESC LIMIT 1',
                (cid,*((before,) if before else ()))).fetchone()
        entry['latest_user_comment'] = dict(comment) if comment else None
    return entry


def fill_diaries(pipeline, *, only_days=None):
    """One entry per finished local date; backfill only the preceding 14 days."""
    from datetime import timedelta
    from cbs.pipeline import INTERNAL_MARKERS
    from cbs.voice_text import voice_call
    store, cid = pipeline.store, pipeline.config.character.id
    today = clock.now().date()
    first = today - timedelta(days=14)
    window = (clock.parse(first).isoformat(), clock.parse(today).isoformat())
    with store.connect() as db:
        # Imported sessions and bootstrap seeds never become shared diaries.
        rows = db.execute('SELECT m.* FROM memory_episodes m JOIN conversations c ON c.id=m.conversation_id '
            'JOIN sessions s ON s.id=c.session_id WHERE m.character_id=? AND s.character_id=? '
            "AND m.status!='invalidated' AND julianday(m.ended_at)>=julianday(?) AND julianday(m.started_at)<julianday(?) "
            'AND NOT EXISTS (SELECT 1 FROM import_items i WHERE i.session_id=s.id)', (cid,cid,*window)).fetchall()
        completed = {r[0] for r in db.execute('SELECT day FROM diaries WHERE character_id=?', (cid,))}
        pending = [dict(r) for r in db.execute('SELECT c.id,c.started_at,c.last_activity_at FROM conversations c '
            'JOIN sessions s ON s.id=c.session_id WHERE s.character_id=? '
            'AND NOT EXISTS (SELECT 1 FROM import_items i WHERE i.session_id=s.id) '
            "AND (c.status='open' OR EXISTS (SELECT 1 FROM memory_jobs j WHERE j.conversation_id=c.id AND j.status!='completed'))", (cid,))]
        turns = [dict(r) for r in db.execute('SELECT t.* FROM turns t JOIN sessions s ON s.id=t.session_id '
            "WHERE s.character_id=? AND t.status='completed' AND t.conversation_id IS NOT NULL "
            'AND julianday(COALESCE(t.assistant_at,t.created_at))>=julianday(?) AND julianday(t.created_at)<julianday(?) '
            'AND NOT EXISTS (SELECT 1 FROM import_items i WHERE i.session_id=s.id)', (cid,*window))]
    from cbs.photos import model_turns
    turns = model_turns(store, turns)
    spoken = {}
    for turn in turns:
        for role, at, text in (('user',turn['created_at'],turn['user_text']),
                               ('assistant',turn['assistant_at'] or turn['created_at'],turn['assistant_text'])):
            if text:
                spoken.setdefault(turn['conversation_id'], []).append({'speaker':role,'at':at,'text':text})
    days = {}
    for row in rows:
        start, end = clock.parse(row['started_at']).date(), clock.parse(row['ended_at']).date()
        utterances = [u for u in spoken.get(row['conversation_id'], [])
                      if clock.parse(row['started_at']) <= clock.parse(u['at']) <= clock.parse(row['ended_at'])]
        for day in sorted({clock.parse(u['at']).date() for u in utterances}):
            if first <= day < today and day.isoformat() not in completed:
                entry = {k:row[k] for k in ('id','started_at','ended_at','summary','details')}
                if start != end:
                    # A 4am conversation boundary can cross midnight. Ground only
                    # this date's words without inventing diary dates in gaps.
                    entry['day_utterances'] = [u for u in utterances if clock.parse(u['at']).date()==day]
                days.setdefault(day.isoformat(), []).append(entry)
    trace = []
    for day, memories in sorted(days.items()):
        if only_days is not None and day not in only_days:
            continue
        # A failed/pending job must not produce a permanently partial diary.
        if any(clock.parse(c['started_at']).date().isoformat()<=day<=clock.parse(c['last_activity_at']).date().isoformat() for c in pending):
            continue
        call_trace = {}
        try:
            context = {'day':day, 'memories':memories, 'previous_diary':latest_diary(store,cid,before=day)}
            entry = voice_call(pipeline, 'note_diary', DiaryEntry, context,
                               lambda e: [e.summary, e.partner_comment], purpose='memory', trace=call_trace)
            if any(INTERNAL_MARKERS.search(v) for v in entry.model_dump().values()):
                raise ValueError('internal marker')
            safe_trace = pipeline._safe_trace(call_trace)
            with store.connect() as db:
                db.execute('INSERT OR IGNORE INTO diaries(character_id,day,summary,partner_comment,created_at,trace_json) VALUES (?,?,?,?,?,?)',
                    (cid,day,entry.summary,entry.partner_comment,clock.iso(),json.dumps(safe_trace,ensure_ascii=False)))
            trace.append({'day':day,'status':'completed'})
        except Exception as error:
            trace.append({'day':day,'status':'failed','trace':pipeline._safe_trace(call_trace or getattr(error,'trace',None) or {})})
            # Later entries should receive the preceding day's comment, in order.
            break
    return trace


class PastNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    llm_role: ClassVar[str] = "steward"
    text: str = Field(min_length=1)


def fill_past_notes(pipeline):
    from cbs.pipeline import INTERNAL_MARKERS
    store = pipeline.store
    with store.connect() as db:
        rows = db.execute("SELECT r.*, t.assistant_text FROM reveal_events r JOIN turns t ON t.id=r.source_turn "
            "WHERE r.character_id=? AND t.status='completed' AND NOT EXISTS "
            "(SELECT 1 FROM note_entries n WHERE n.character_id=r.character_id AND n.kind='past' AND n.source_id=r.id) ORDER BY r.id",
            (pipeline.config.character.id,)).fetchall()
    for row in rows:
        try:
            result = display_call(lambda: for_step(pipeline.client, 'note', 'memory').generate_json(pipeline.config.models.steward, read_prompt("note_past"),
                [{"role": "user", "content": json.dumps({"assistant_text": row["assistant_text"]}, ensure_ascii=False)}], PastNote), lambda r: [r.parsed.text])
            text = pipeline.client.sanitize_text(PastNote.model_validate(result.parsed).text)
            if INTERNAL_MARKERS.search(text):
                continue
            with store.connect() as db:
                db.execute("INSERT OR IGNORE INTO note_entries (character_id,kind,source_id,text,created_at) VALUES (?,'past',?,?,?)",
                    (row["character_id"], row["id"], text, clock.iso()))
        except Exception:
            # Retry on daily maintenance, never interrupt dialogue.
            continue


class ActivityNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    llm_role: ClassVar[str] = "steward"
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    impression: str = Field(min_length=1)


def reading_title(sources):
    source = next((s for s in sources if s.get('chunk_number')), None)
    return f"{source['title']}（{source['chunk_number']} / {source['chunk_count']}）" if source else None


def fill_activity_notes(pipeline):
    """Turn each completed activity into a notebook entry: title, summary, and how the partner seemed to feel."""
    from cbs.pipeline import INTERNAL_MARKERS
    store, cid = pipeline.store, pipeline.config.character.id
    with store.connect() as db:
        rows = db.execute("SELECT a.* FROM activities a WHERE a.character_id=? AND a.status='completed' AND NOT EXISTS "
            "(SELECT 1 FROM note_entries n WHERE n.character_id=a.character_id AND n.kind='activity' AND n.source_id=a.id) ORDER BY a.id",
            (cid,)).fetchall()
    for row in rows:
        try:
            context = {"name": pipeline.character.model.display_name, "kind": row["kind"], "topic": row["topic"],
                       "activity_summary": row["summary"],
                       "source_titles": [s.get("title", "") for s in json.loads(row["sources_json"]) if not s.get('internal')]}
            if row['kind'] == 'browse_shopping':
                context['observed_at'] = [s['observed_at'] for s in json.loads(row['sources_json'])]
            result = display_call(lambda: for_step(pipeline.client, 'note', 'memory').generate_json(pipeline.config.models.steward, read_prompt("note_activity"),
                [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}], ActivityNote), lambda r: r.parsed.model_dump().values())
            entry = ActivityNote.model_validate(result.parsed)
            entry = ActivityNote(**{k: pipeline.client.sanitize_text(v).strip() for k, v in entry.model_dump().items()})
            if row['kind'] == 'read_book':
                entry.title = reading_title(json.loads(row['sources_json'])) or entry.title
            if any(INTERNAL_MARKERS.search(v) for v in entry.model_dump().values()):
                continue
            with store.connect() as db:
                # note_entries keeps one text column; activity entries store their three parts as JSON.
                db.execute("INSERT OR IGNORE INTO note_entries (character_id,kind,source_id,text,created_at) VALUES (?,'activity',?,?,?)",
                    (cid, row["id"], json.dumps(entry.model_dump(), ensure_ascii=False), clock.iso()))
        except Exception:
            # Retry on daily maintenance, never interrupt Inner Life or dialogue.
            continue


def activity_weeks(store, cid):
    """Count saved, public note entries without loading activity bodies or JSON."""
    with store.connect() as db:
        rows = db.execute("SELECT date(substr(a.created_at,1,10), '-' || "
            "((CAST(strftime('%w',substr(a.created_at,1,10)) AS INTEGER)+6)%7) || ' days') AS week_start, COUNT(*) AS count "
            "FROM activities a JOIN note_entries n ON n.source_id=a.id AND n.character_id=a.character_id AND n.kind='activity' "
            "WHERE a.character_id=? AND a.status='completed' GROUP BY week_start ORDER BY week_start DESC", (cid,)).fetchall()
    return [{**dict(row), 'loaded': i < 4} for i, row in enumerate(rows)]


def activity_notes(store, cid, limit=None, *, since=None, until=None):
    where, params = '', [cid]
    if since is not None:
        where += ' AND substr(a.created_at,1,10)>=?'
        params.append(since)
    if until is not None:
        where += ' AND substr(a.created_at,1,10)<?'
        params.append(until)
    if limit is not None:
        params.append(limit)
    with store.connect() as db:
        rows = db.execute("SELECT n.text, a.created_at, a.sources_json FROM note_entries n JOIN activities a ON a.id=n.source_id "
            "WHERE n.character_id=? AND a.character_id=n.character_id AND n.kind='activity' AND a.status='completed'" + where +
            " ORDER BY substr(a.created_at,1,10) DESC, a.id DESC" + (' LIMIT ?' if limit is not None else ''), params).fetchall()
    def titles(sources_json):
        sources = json.loads(sources_json)
        if any(s.get('observed_at') for s in sources):
            return [{k: s[k] for k in ('title', 'uri', 'observed_at')} for s in sources if s.get('observed_at')][:5]
        # Grounding titles are often bare domains that repeat; show each once, a few at most.
        unique = list(dict.fromkeys(s.get("title", "") for s in json.loads(sources_json) if s.get("title") and not s.get('internal')))
        return [{"title": t} for t in unique[:5]]
    result = []
    for r in rows:
        entry = json.loads(r['text'])
        # Existing pre-M8b-2 notes also get distinct titles without rewriting their text.
        entry['title'] = reading_title(json.loads(r['sources_json'])) or entry['title']
        result.append({**entry, 'date': r['created_at'][:10],
                       'sources': titles(r['sources_json'])})
    return result


def self_forming(store, character):
    from cbs.note_scenes import read_note
    return read_note(store, character)[0]


def fill_forming_notes(pipeline, *, source_turn=None):
    from cbs.note_scenes import fill_forming_notes as fill
    return fill(pipeline, source_turn=source_turn)


def note_header(store, cid):
    """Reuse saved expression decisions and original local calendar dates."""
    from cbs.expression import EXPRESSIONS
    with store.connect() as db:
        rows = db.execute("SELECT t.created_at FROM turns t JOIN sessions s ON s.id=t.session_id "
            "WHERE s.character_id=? AND t.status='completed' AND t.kind='conversation' AND trim(t.user_text)!=''", (cid,)).fetchall()
        expression = db.execute("SELECT t.expression FROM turns t JOIN sessions s ON s.id=t.session_id "
            "WHERE s.character_id=? AND t.status='completed' AND t.expression IN (" + ','.join('?' for _ in EXPRESSIONS) +
            ") ORDER BY julianday(COALESCE(t.assistant_at,t.created_at)) DESC,t.id DESC LIMIT 1", (cid, *EXPRESSIONS)).fetchone()
    days = {clock.parse(r['created_at']).date() for r in rows}
    first = min(days) if days else None
    return {'current_expression': expression[0] if expression else 'neutral',
            'days': {'first_day': first.isoformat() if first else None,
                     'elapsed_days': (clock.now().date() - first).days if first else 0,
                     'talked_days': len(days)}}


NEW_DAYS = 7


def is_new(value):
    age = (clock.now() - clock.parse(value)).total_seconds()
    return 0 <= age < NEW_DAYS * 86400


def public_note(pipeline):
    from cbs.web.knowing_api import knowing_note
    store, cid = pipeline.store, pipeline.config.character.id
    with store.connect() as db:
        past = [r[0] for r in db.execute("SELECT text FROM note_entries WHERE character_id=? AND kind='past' ORDER BY id", (cid,))]
    states = {s["secret_id"]: s for s in store.list_secrets(cid)}
    monologues = store.list_monologues(cid) if pipeline.config.inner.show_monologue else []
    from cbs.days import calendar_days
    nearby = [d for d in calendar_days(store, cid, pipeline.config.days) if d['in_days'] <= pipeline.config.days.nearby_days]
    from cbs.note_scenes import read_note
    forming, traits = read_note(store, pipeline.character)
    weeks = activity_weeks(store, cid)
    return {'nearby_days': nearby, "name": pipeline.character.model.display_name, 'knowing': knowing_note(pipeline),
        **note_header(store, cid),
        'diaries': diaries(store, cid),
        'self_forming': forming, 'self_traits': traits,
        "today_haru": monologues[-1]['text'] if monologues else None,
        "now_summary": (store.now_summary(cid) or {}).get("summary"),
        "interests": [{"topic": i["topic"], "kind": i["kind"], "detail": i["detail"], "new": is_new(i["first_seen"])}
            for i in store.list_interests(cid) if i["status"] == "established"],
        "activity_weeks": weeks,
        "activities": activity_notes(store, cid, since=weeks[min(3, len(weeks)-1)]['week_start']) if weeks else [],
        "books": [{k: b[k] for k in ('title', 'author', 'read_count', 'chunk_count', 'finished_at')} for b in store.list_books(cid)],
        "past": past, "has_undisclosed_past": any(states.get(s.id, {}).get("disclosed_level", 0) < 3
            for s in pipeline.character.reveal.secrets)}
