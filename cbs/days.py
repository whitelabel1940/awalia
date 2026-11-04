"""HB1 calendar storage and time rules. Meaning belongs to the prompt callers."""
import json
from datetime import date, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cbs import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS partner_days (
 character_id TEXT PRIMARY KEY, version INTEGER NOT NULL, state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_notices (
 character_id TEXT NOT NULL, source_turn INTEGER PRIMARY KEY REFERENCES turns(id), notices_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_mentions (
 character_id TEXT NOT NULL, day TEXT NOT NULL, ref TEXT NOT NULL, source_turn INTEGER REFERENCES turns(id),
 PRIMARY KEY(character_id,day,ref)
);
CREATE TABLE IF NOT EXISTS habit_runs (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL, day TEXT NOT NULL, ref TEXT NOT NULL,
 UNIQUE(character_id,day,ref)
);
CREATE TABLE IF NOT EXISTS birthday_initializations (
 character_id TEXT PRIMARY KEY, status TEXT NOT NULL
);
"""


class ImportantDay(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str | None = None
    kind: Literal['user_birthday', 'partner_birthday', 'anniversary']
    name: str = Field(min_length=1, max_length=100)
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    year: int | None = Field(default=None, ge=1, le=9999)

    @model_validator(mode='after')
    def calendar_date(self):
        date(self.year or 2000, self.month, self.day)
        if not self.name.strip() or '\n' in self.name or '\r' in self.name:
            raise ValueError('名前を一行で入力してください。')
        return self


class Habit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str | None = None
    name: str = Field(min_length=1, max_length=100)
    time: str = Field(pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')
    weekdays: list[int] = Field(min_length=1, max_length=7)
    allow_quiet: bool = False
    enabled: bool = True

    @model_validator(mode='after')
    def valid(self):
        if any(type(d) is not int or not 0 <= d <= 6 for d in self.weekdays) or len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError('曜日を確認してください。')
        if not self.name.strip() or '\n' in self.name or '\r' in self.name:
            raise ValueError('名前を一行で入力してください。')
        return self


def empty_state():
    return {'days': [], 'habits': []}


class DaysStore:
    def _days_state(self, db, cid):
        row = db.execute('SELECT * FROM partner_days WHERE character_id=?', (cid,)).fetchone()
        return (json.loads(row['state_json']), row['version']) if row else (empty_state(), 0)

    def days_state(self, cid):
        with self.connect() as db:
            state, version = self._days_state(db, cid)
        return {**state, 'version': version}

    def _write_days(self, db, cid, state, version):
        db.execute('INSERT INTO partner_days VALUES (?,?,?) ON CONFLICT(character_id) DO UPDATE SET version=excluded.version,state_json=excluded.state_json',
                   (cid, version + 1, json.dumps(state, ensure_ascii=False)))

    def save_days(self, cid, days, habits, expected_version):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            before, version = self._days_state(db, cid)
            if version != expected_version:
                raise ValueError('内容が更新されました。読み直してから保存してください。')
            state = {}
            for key, rows, schema in [('days', days, ImportantDay), ('habits', habits, Habit)]:
                old = {r['id'] for r in before[key]}
                seen, saved = set(), []
                for raw in rows:
                    item = schema.model_validate(raw).model_dump()
                    ref = item['id']
                    if ref is not None and (ref not in old or ref in seen):
                        raise ValueError('行のIDを確認してください。')
                    item['id'] = ref or uuid4().hex
                    seen.add(item['id'])
                    saved.append(item)
                if key == 'days':
                    for kind in ('user_birthday', 'partner_birthday'):
                        if sum(d['kind'] == kind for d in saved) > 1:
                            raise ValueError('誕生日はそれぞれ一つです。')
                state[key] = saved
            self._write_days(db, cid, state, version)
        return self.days_state(cid)

    def apply_day_operations(self, db, cid, turn_id, operations, expected_version):
        state, version = self._days_state(db, cid)
        result = {'applied': [], 'rejected': []}
        if version != expected_version:
            return {**result, 'rejected': ['設定が更新されたため古い抽出を適用しません。']}
        notices = []
        for i, raw in enumerate(operations):
            try:
                item = ImportantDay.model_validate(raw).model_dump()
                target = next((d for d in state['days'] if d['id'] == item['id']), None) if item['id'] else None
                if item['id'] and target is None:
                    raise ValueError('不正なID')
                if item['kind'] != 'anniversary':
                    birthday = next((d for d in state['days'] if d['kind'] == item['kind']), None)
                    if target and target is not birthday:
                        raise ValueError('種類の変更は設定で行います。')
                    target = birthday
                if target and target['kind'] != item['kind']:
                    raise ValueError('種類の変更は設定で行います。')
                # Once selected, the partner birthday is immutable to LLM operations.
                if target and item['kind'] == 'partner_birthday':
                    continue
                item['id'] = target['id'] if target else uuid4().hex
                if target == item:
                    continue
                if target:
                    state['days'][state['days'].index(target)] = item
                else:
                    state['days'].append(item)
                notices.append(f'「{item["name"]}」を大切な日として覚えました')
                result['applied'].append(i)
            except ValueError:
                result['rejected'].append(i)
        if notices:
            self._write_days(db, cid, state, version)
            db.execute('INSERT OR REPLACE INTO day_notices VALUES (?,?,?)', (cid, turn_id, json.dumps(notices, ensure_ascii=False)))
        return result

    def day_notices(self, cid, turn_id=None):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM day_notices WHERE character_id=?' + (' AND source_turn=?' if turn_id is not None else ''),
                              (cid, turn_id) if turn_id is not None else (cid,)).fetchall()
        return {r['source_turn']: json.loads(r['notices_json']) for r in rows}

    def mark_days(self, db, cid, refs, available, turn_id, local_day):
        for ref in set(refs) & set(available):
            db.execute('INSERT OR IGNORE INTO day_mentions VALUES (?,?,?,?)', (cid, local_day, ref, turn_id))

    def claim_habit(self, cid, habit):
        with self.connect() as db:
            cursor = db.execute('INSERT OR IGNORE INTO habit_runs (character_id,day,ref) VALUES (?,?,?)', (cid, habit['local_day'], habit['id']))
            return cursor.lastrowid if cursor.rowcount else None


def calendar_days(store, cid, config):
    today = clock.now().date()
    days = store.days_state(cid)['days']
    result = []
    for item in days:
        start = max(today.year, item['year'] or today.year)
        for year in range(start, min(start + 9, 10000)):
            try:
                occurs = date(year, item['month'], item['day'])
            except ValueError:
                continue  # Leap-day birthdays occur on the actual date only.
            if occurs >= today:
                result.append({**item, 'date': occurs.isoformat(), 'in_days': (occurs - today).days})
                break
    # Same completed, nonempty conversation dates as note_header; fetch only the
    # earliest row here because this path also supplies every reply's Packet.
    with store.connect() as db:
        first_row = db.execute("SELECT t.created_at FROM turns t JOIN sessions s ON s.id=t.session_id WHERE s.character_id=? AND t.status='completed' AND t.kind='conversation' AND trim(t.user_text)!='' ORDER BY julianday(t.created_at),t.id LIMIT 1", (cid,)).fetchone()
    if first_row:
        first = clock.parse(first_row[0]).date()
        if config.hundred_days:
            occurs = first + timedelta(days=100)
            if occurs >= today:
                result.append({'id': 'auto:100', 'kind': 'milestone', 'name': 'はじめて話して100日', 'date': occurs.isoformat(), 'in_days': (occurs-today).days})
        if config.yearly:
            for year in range(max(first.year + 1, today.year), min(today.year + 9, 10000)):
                try:
                    occurs = first.replace(year=year)
                except ValueError:
                    continue
                if occurs >= today:
                    result.append({'id': f'auto:year:{year-first.year}', 'kind': 'milestone', 'name': f'はじめて話して{year-first.year}年', 'date': occurs.isoformat(), 'in_days': (occurs-today).days})
                    break
    return sorted(result, key=lambda d: (d['date'], d['id']))


def days_context(store, cid, config, *, user_reply=False):
    today = clock.now().date().isoformat()
    start = clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with store.connect() as db:
        used = {r[0] for r in db.execute('SELECT ref FROM day_mentions WHERE character_id=? AND day=?', (cid, today))}
        user_turn = db.execute("SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id WHERE s.character_id=? AND t.status='completed' AND t.kind='conversation' AND trim(t.user_text)!='' AND julianday(t.created_at)>=julianday(?) AND julianday(t.created_at)<julianday(?) LIMIT 1", (cid, start.isoformat(), (start+timedelta(days=1)).isoformat())).fetchone()
    state = store.days_state(cid)
    return {**state, 'local_day': today,
            'today': [{**d, 'may_mention': d['id'] not in used and (not user_reply or (d['kind'] == 'user_birthday' and not user_turn)),
                       'first_user_reply': not bool(user_turn)} for d in calendar_days(store, cid, config) if d['in_days'] == 0]}


def due_habits(store, cid, config, autonomy):
    from cbs.autonomy import quiet_hours
    now = clock.now()
    with store.connect() as db:
        used = {(r['day'], r['ref']) for r in db.execute('SELECT day,ref FROM habit_runs WHERE character_id=?', (cid,))}
    result = []
    for habit in store.days_state(cid)['habits']:
        if not habit['enabled'] or (quiet_hours(now.hour, autonomy) and not habit['allow_quiet']):
            continue
        for offset in (0, -1, 1):
            scheduled_day = now.date() + timedelta(days=offset)
            scheduled = clock.parse(f'{scheduled_day.isoformat()}T{habit["time"]}:00')
            key = (scheduled_day.isoformat(), habit['id'])
            if scheduled_day.weekday() in habit['weekdays'] and key not in used and abs((now-scheduled).total_seconds()) <= config.habit_window_minutes * 60:
                result.append({**habit, 'local_day': key[0]})
    return result


class BirthdayInitial(BaseModel):
    birthday: ImportantDay | None = None
    source_ref: str | None = None
    quote: str = ''


def initialize_birthday(pipeline):
    """One background judgment, even when the explicit author birthday is absent."""
    p, cid = pipeline, pipeline.config.character.id
    with p.store.connect() as db:
        if not db.execute("INSERT OR IGNORE INTO birthday_initializations VALUES (?,'attempted')", (cid,)).rowcount:
            return
        _, version = p.store._days_state(db, cid)
    from cbs.runtime import read_prompt
    from cbs.llm.cost import for_step
    sources = {**p.character.sources, 'life_history': p.character.model.life_history.model_dump(mode='json'),
               'expression': p.character.expression.model_dump(mode='json', by_alias=True)}
    try:
        response = for_step(p.client, 'memory', 'memory').generate_json(p.config.models.judge, read_prompt('birthday_initial'),
            [{'role': 'user', 'content': json.dumps({'sources': sources}, ensure_ascii=False)}], BirthdayInitial)
        value = BirthdayInitial.model_validate_json(p.client.sanitize_text(response.parsed.model_dump_json()))
        with p.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            state, current = p.store._days_state(db, cid)
            if value.birthday and value.birthday.kind == 'partner_birthday' and value.source_ref in sources and value.quote and value.quote in (sources[value.source_ref] if isinstance(sources[value.source_ref], str) else json.dumps(sources[value.source_ref], ensure_ascii=False)):
                if current == version and not any(d['kind'] == 'partner_birthday' for d in state['days']):
                    state['days'].append({**value.birthday.model_dump(), 'id': uuid4().hex})
                    p.store._write_days(db, cid, state, current)
            db.execute("UPDATE birthday_initializations SET status='completed' WHERE character_id=?", (cid,))
    except Exception:
        with p.store.connect() as db:
            db.execute("UPDATE birthday_initializations SET status='failed' WHERE character_id=?", (cid,))
