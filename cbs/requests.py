"""Versioned partner adjustments. All semantic decisions belong to Extract/Check."""
import json
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from cbs import clock

FIELDS = {'first_person': '一人称', 'second_person': '呼び方', 'user_name': 'あなたの名前', 'register': '口調', 'length': '返事の長さ'}
SCHEMA = """
CREATE TABLE IF NOT EXISTS request_versions (
 id INTEGER PRIMARY KEY, character_id TEXT NOT NULL,
 state_json TEXT NOT NULL, before_json TEXT NOT NULL,
 source TEXT NOT NULL, source_turn INTEGER REFERENCES turns(id),
 notices_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_by_character ON request_versions(character_id,id);
CREATE TABLE IF NOT EXISTS request_imports (
 batch_id TEXT PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES request_versions(id)
);
"""


class RequestOperation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: str
    id: str | None = None
    field: str | None = None
    text: str = ''
    explicit_conflict: bool = False
    reason: str = ''


def base_state(character):
    v = character.expression.voice
    return {'fields': {'first_person': v.first_person, 'second_person': v.second_person.get('default', ''),
                       'user_name': '', 'register': v.speech_register, 'length': v.length.get('default', '')}, 'lines': []}


def effective_character(character, state):
    from cbs.character import Expression
    data = character.expression.model_dump(mode='json', by_alias=True)
    fields = state['fields']
    data['voice']['first_person'] = fields['first_person']
    data['voice']['second_person']['default'] = fields['second_person']
    # One editable calling field replaces all hidden calling alternatives.
    # Older snapshots have no flag; an exact stored-value change is sufficient.
    if state.get('second_person_overridden', fields['second_person'] != character.expression.voice.second_person.get('default', '')):
        data['voice']['second_person'] = {'default': fields['second_person'], 'when_needed': fields['second_person']}
    data['voice']['register'] = fields['register']
    data['voice']['length']['default'] = fields['length']
    return character.model_copy(update={'expression': Expression.model_validate(data)})


def prompt_context(state):
    from cbs.runtime import read_prompt
    return '\n\n' + read_prompt('requests_context') + '\n' + json.dumps({'partner_requests': state}, ensure_ascii=False)


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\n' in value or '\r' in value:
        raise ValueError('文字数・空欄・改行を確認してください。')
    return value.strip()


def _reverse_import_state(current, before, after):
    """Reverse unchanged imported values; preserve later independent edits."""
    restored = json.loads(json.dumps(current))
    for key in FIELDS:
        if before['fields'].get(key, '') != after['fields'].get(key, '') and current['fields'].get(key, '') == after['fields'].get(key, ''):
            restored['fields'][key] = before['fields'].get(key, '')
            if 'initial_import_fields' in restored and key not in before.get('initial_import_fields', []):
                restored['initial_import_fields'] = [k for k in restored['initial_import_fields'] if k != key]
    if current.get('second_person_overridden') == after.get('second_person_overridden') and restored['fields']['second_person'] == before['fields']['second_person']:
        if 'second_person_overridden' in before:
            restored['second_person_overridden'] = before['second_person_overridden']
        else:
            restored.pop('second_person_overridden', None)
    old = {line['id']: line for line in before['lines']}
    changed = {line['id']: line for line in after['lines'] if old.get(line['id']) != line}
    restored['lines'] = [old.get(line['id'], line) if line == changed.get(line['id']) else line for line in current['lines']
                         if line != changed.get(line['id']) or line['id'] in old]
    return restored


class RequestStore:
    def _request_state(self, db, character):
        row = db.execute('SELECT * FROM request_versions WHERE character_id=? ORDER BY id DESC LIMIT 1',
                         (character.model.character_id,)).fetchone()
        state = json.loads(row['state_json']) if row else base_state(character)
        state['fields'].setdefault('user_name', '')
        if 'initial_import_fields' in state:
            state['fields'] = {**base_state(character)['fields'],
                               **{k: state['fields'][k] for k in state['initial_import_fields']}}
        return state, row

    def current_requests(self, character):
        with self.connect() as db:
            state, row = self._request_state(db, character)
        return {**state, 'version': row['id'] if row else 0,
                'updated_at': row['created_at'] if row else None,
                'source': row['source'] if row else None, 'source_turn': row['source_turn'] if row else None,
                'can_undo': row is not None}

    def current_character(self, character):
        return effective_character(character, self.current_requests(character))

    def _request_version(self, db, character, before, state, source, turn=None, notices=()):
        return db.execute('INSERT INTO request_versions (character_id,state_json,before_json,source,source_turn,notices_json,created_at) VALUES (?,?,?,?,?,?,?)',
            (character.model.character_id, json.dumps(state, ensure_ascii=False), json.dumps(before, ensure_ascii=False),
             source, turn, json.dumps(list(notices), ensure_ascii=False), clock.iso())).lastrowid

    def save_requests(self, character, fields, lines, config, expected_version, *, undo=False):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            before, row = self._request_state(db, character)
            if expected_version != (row['id'] if row else 0):
                raise ValueError('内容が更新されました。読み直してから保存してください。')
            if undo:
                if row is None:
                    raise ValueError('戻せる版はありません。')
                state = json.loads(row['before_json'])
                state['fields'].setdefault('user_name', '')
            else:
                # Older clients omit the newly optional name; preserve its current value.
                fields = {'user_name': before['fields'].get('user_name', ''), **fields}
                if set(fields) != set(FIELDS) or len(lines) > config.max_lines:
                    raise ValueError('話し方の欄・お願いの件数を確認してください。')
                fields = {k: ('' if k == 'user_name' and isinstance(v, str) and not v.strip()
                              and len(v) <= config.max_field_chars and '\n' not in v and '\r' not in v
                              else _text(v, config.max_field_chars)) for k, v in fields.items()}
                old = {l['id']: l for l in before['lines']}
                seen, saved = set(), []
                for line in lines:
                    if set(line) - {'id', 'text'}:
                        raise ValueError('お願いの欄を確認してください。')
                    ref = line.get('id')
                    if ref is not None and (ref not in old or ref in seen):
                        raise ValueError('お願いのIDを確認してください。')
                    text = _text(line.get('text'), config.max_line_chars)
                    seen.add(ref)
                    saved.append(old[ref] if ref in old and old[ref]['text'] == text else
                                 {'id': ref or uuid4().hex, 'text': text, 'source': 'settings'})
                state = {'fields': fields, 'lines': saved}
                if 'initial_import_fields' in before:
                    state['initial_import_fields'] = list(dict.fromkeys(before['initial_import_fields'] +
                        [key for key in FIELDS if fields[key] != before['fields'][key]]))
                if (before.get('second_person_overridden') or
                        fields['second_person'] != before['fields']['second_person'] or
                        fields['second_person'] != base_state(character)['fields']['second_person']):
                    state['second_person_overridden'] = True
            if state != before:
                self._request_version(db, character, before, state, 'undo' if undo else 'settings')
        return self.current_requests(character)

    def apply_request_operations(self, db, character, turn_id, operations, config, expected_version,
                                 *, source='conversation', initial=False):
        before, row = self._request_state(db, character)
        result = {'applied': [], 'rejected': []}
        if not operations:
            return result
        if source == 'conversation':
            owner = db.execute('SELECT s.character_id FROM turns t JOIN sessions s ON s.id=t.session_id WHERE t.id=?',
                               (turn_id,)).fetchone()
            if owner is None or owner['character_id'] != character.model.character_id:
                result['rejected'].append({'reason': '会話ターンIDがこの人物のものではありません。'})
                return result
        elif source != 'import' or turn_id is not None:
            raise ValueError('お願いの変更元が不正です。')
        if expected_version != (row['id'] if row else 0):
            result['rejected'].append({'reason': '設定が更新されたため古いExtract操作を適用しません。'})
            return result
        if initial:
            before.setdefault('initial_import_fields', [])
        state = json.loads(json.dumps(before))
        notices = []
        for index, raw in enumerate(operations):
            try:
                op = RequestOperation.model_validate(raw)
                if op.operation == 'add':
                    if op.id is not None or op.field is not None:
                        raise ValueError('追加のID・欄名・件数が不正です。')
                    text = _text(op.text, config.max_line_chars)
                    if source == 'import' and any(line['text'] == text for line in state['lines']):
                        continue
                    if len(state['lines']) >= config.max_lines:
                        raise ValueError('追加のID・欄名・件数が不正です。')
                    state['lines'].append({'id': uuid4().hex, 'text': text, 'source': source, 'source_turn': turn_id})
                    notice = 'お願いとして覚えました'
                elif op.operation == 'replace':
                    target = next((l for l in state['lines'] if l['id'] == op.id), None)
                    if target is None or op.field is not None or not op.explicit_conflict or not op.reason.strip():
                        raise ValueError('既存IDと明示的な食い違いの判断が必要です。')
                    text = _text(op.text, config.max_line_chars)
                    if target['text'] == text:
                        continue
                    target.update(text=text, source=source, source_turn=turn_id)
                    notice = 'お願いを書き換えました'
                elif op.operation == 'set_field':
                    if op.field not in FIELDS or op.id is not None:
                        raise ValueError('話し方の欄名が不正です。')
                    text = _text(op.text, config.max_field_chars)
                    calling_override = op.field == 'second_person' and not state.get('second_person_overridden',
                        state['fields']['second_person'] != base_state(character)['fields']['second_person'])
                    if state['fields'][op.field] == text and not calling_override:
                        continue
                    state['fields'][op.field] = text
                    if 'initial_import_fields' in state and op.field not in state['initial_import_fields']:
                        state['initial_import_fields'].append(op.field)
                    if op.field == 'second_person':
                        state['second_person_overridden'] = True
                    notice = f'{FIELDS[op.field]}を「{text}」にしました'
                else:
                    raise ValueError('許可されていない操作です。削除は設定画面で行います。')
                result['applied'].append(index)
                notices.append(notice)
            except ValueError as error:
                result['rejected'].append({'index': index, 'reason': str(error)})
        if state != before:
            result['version'] = self._request_version(db, character, before, state, source, turn_id, notices)
        return result

    def undo_import_requests(self, db, character_id, batch_id):
        imported = db.execute('SELECT v.* FROM request_versions v JOIN request_imports i ON i.version_id=v.id WHERE i.batch_id=? AND v.character_id=?',
                              (batch_id, character_id)).fetchone()
        if not imported:
            return
        latest = db.execute('SELECT * FROM request_versions WHERE character_id=? ORDER BY id DESC LIMIT 1', (character_id,)).fetchone()
        before, after, current = (json.loads(r) for r in (imported['before_json'], imported['state_json'], latest['state_json']))
        # A later batch must not restore values from an already cancelled batch.
        # Rebase in memory, retaining every historical version unchanged.
        for older in db.execute("SELECT v.* FROM request_versions v JOIN request_imports i ON i.version_id=v.id JOIN import_batches b ON b.id=i.batch_id WHERE v.character_id=? AND v.id<? AND b.status='undone' ORDER BY v.id DESC",
                                (character_id, imported['id'])):
            before = _reverse_import_state(before, json.loads(older['before_json']), json.loads(older['state_json']))
        restored = _reverse_import_state(current, before, after)
        if restored != current:
            db.execute('INSERT INTO request_versions (character_id,state_json,before_json,source,notices_json,created_at) VALUES (?,?,?,?,?,?)',
                       (character_id, json.dumps(restored, ensure_ascii=False), json.dumps(current, ensure_ascii=False), 'undo', '[]', clock.iso()))

    def request_notices(self, character_id, turn_id=None):
        with self.connect() as db:
            rows = db.execute('SELECT source_turn,notices_json FROM request_versions WHERE character_id=? AND source_turn IS NOT NULL' +
                              (' AND source_turn=?' if turn_id is not None else '') + ' ORDER BY id',
                              (character_id, turn_id) if turn_id is not None else (character_id,)).fetchall()
        return {r['source_turn']: json.loads(r['notices_json']) for r in rows}
