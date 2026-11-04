"""Import-only context before publication, quoted Reader sources and pasted facts."""
from cbs.llm.cost import for_step
import json
import math
from types import SimpleNamespace
from typing import ClassVar
from uuid import uuid4

from pydantic import Field

from cbs import clock
from cbs.bootstrap_schema import Strict
from cbs.character import Character
from cbs.import_formats import Conversation
from cbs.runtime import read_prompt
from cbs.storage import pack_vector
from cbs.requests import FIELDS, RequestOperation


def import_pipeline(base, cid):
    """No provisional files, seed memories, or another partner's personality."""
    config = base.config.model_copy(deep=True)
    config.character.id = cid
    character = Character.model_validate({
        'model': {'version': 1, 'character_id': cid, 'display_name': 'パートナー',
                  'summary': [], 'signatures': [], 'domains': [], 'life_history': {}},
        'expression': {'voice': {'first_person': '', 'second_person': {}, 'register': '',
                       'length': {}, 'allow_disagreement_and_negative_emotion': True},
                       'demeanor': [], 'preferences': [], 'relationship_context': {},
                       'not_adopted_as_goal': [], 'existence': {}},
        'sources': {}, 'world': {}})
    return SimpleNamespace(config=config, character=character, store=base.store, client=base.client, provisional=True,
                           _safe_trace=lambda value: json.loads(base.client.sanitize_text(json.dumps(value, ensure_ascii=False))))


def partner_sources(store, cid, batch_ids):
    """Bounded deterministic sample of originals, not generated summaries."""
    messages = []
    with store.connect() as db:
        for bid in dict.fromkeys(batch_ids):
            batch = db.execute('SELECT * FROM import_batches WHERE id=? AND character_id=?', (bid, cid)).fetchone()
            if not batch or batch['status'] not in ('running', 'cancelling', 'cancelled', 'failed', 'completed'):
                raise ValueError('この人物の開始済みの取り込みを選んでください。')
            for row in db.execute("SELECT * FROM import_items WHERE batch_id=? AND status NOT IN ('duplicate','undone')", (bid,)):
                c = Conversation.load(json.loads(row['payload_json']))
                preceding_user = None
                for i, m in enumerate(c.messages):
                    if m.role == 'user':
                        preceding_user = m.text
                    if m.role == 'assistant':
                        messages.append({'id': f'{bid}:{row["id"]}:{i}', 'at': m.at, 'text': m.text,
                                         'context_user': preceding_user})
                        preceding_user = None
    messages.sort(key=lambda m: (clock.parse(m['at']), m['id']), reverse=True)
    # Half recent, half evenly distributed older messages; size and order only.
    if len(messages) > 120:
        rest = messages[60:]
        messages = messages[:60] + [rest[i * (len(rest)-1)//59] for i in range(60)]
    selected, size = [], 0
    for m in messages:
        chars = len(m['text']) + len(m['context_user'] or '')
        if size + chars <= 48000:
            selected.append(m)
            size += chars
    return selected


class PastedFact(Strict):
    content: str = Field(min_length=1, max_length=1200)
    quote: str = Field(min_length=1, max_length=4000)
    private: bool
    private_category: str | None
    duplicate_fact_ids: list[int] = Field(default_factory=list, max_length=64)


class PastedRequest(Strict):
    quote: str = Field(min_length=1, max_length=4000)
    request: RequestOperation


class PastedFacts(Strict):
    llm_role: ClassVar[str] = 'steward'
    facts: list[PastedFact] = Field(max_length=64)
    requests: list[PastedRequest] = Field(default_factory=list, max_length=64)


def review_about(pipeline, text):
    from cbs.bootstrap import quote_matches
    categories = pipeline.config.knowing.model_dump()['private_categories']
    existing = {f['id']: {k: f.get(k) for k in ('id', 'content', 'private', 'private_category', 'last_confirmed_at')}
                for f in pipeline.store.list_user_facts(pipeline.config.character.id, current_only=True)}
    requests = pipeline.store.current_requests(pipeline.character)
    result = for_step(pipeline.client, 'import', 'import').generate_json(pipeline.config.models.steward, read_prompt('import_about'),
        [{'role': 'user', 'content': json.dumps({'text': text, 'speaker': 'user', 'subject': 'user',
            'existing_facts': list(existing.values()), 'private_categories': categories,
            'partner_requests': requests, 'request_fields': FIELDS,
            'request_limits': pipeline.config.requests.model_dump()}, ensure_ascii=False)}], PastedFacts)
    output = PastedFacts.model_validate_json(pipeline.client.sanitize_text(result.parsed.model_dump_json()))
    for f in output.facts:
        if not quote_matches(f.quote, text) or (f.private and f.private_category not in categories) or (not f.private and f.private_category is not None):
            raise ValueError('原文の引用・種類を確認できませんでした。もう一度読み取ってください。')
        if set(f.duplicate_fact_ids) - existing.keys():
            raise ValueError('重なる事実を確認できませんでした。もう一度読み取ってください。')
    if len(output.facts) + len(output.requests) > 64:
        raise ValueError('候補は合計64件以内にしてください。')
    for item in output.requests:
        if not quote_matches(item.quote, text):
            raise ValueError('お願いの原文の引用を確認できませんでした。')
    # Exercise the same deterministic AD1 validation without persisting a version.
    if output.requests:
        with pipeline.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('SAVEPOINT review_requests')
            try:
                checked = pipeline.store.apply_request_operations(db, pipeline.character, None,
                    [item.request.model_dump() for item in output.requests], pipeline.config.requests, requests['version'],
                    source='import', initial=getattr(pipeline, 'provisional', False))
                if checked['rejected']:
                    raise ValueError('お願いのID・欄・上限または版を確認できませんでした。')
            finally:
                db.execute('ROLLBACK TO review_requests')
                db.execute('RELEASE review_requests')
    candidates = [{'id': str(i), **f.model_dump(), 'destination': 'fact', 'destination_label': 'あなたのこと',
                   'existing_facts': [existing[key] for key in dict.fromkeys(f.duplicate_fact_ids)],
                   'category_label': categories[f.private_category]['label'] if f.private else 'ふだんのこと'}
                  for i, f in enumerate(output.facts)]
    for item in output.requests:
        op = item.request
        candidates.append({'id': str(len(candidates)), 'content': op.text, 'quote': item.quote,
                           'destination': 'request', 'destination_label': FIELDS.get(op.field, 'お願い'),
                           'request': op.model_dump(), 'request_version': requests['version']})
    token = uuid4().hex
    with pipeline.store.connect() as db:
        db.execute('INSERT INTO import_reviews VALUES (?,?,?,?,NULL,?)',
            (token, pipeline.config.character.id, pipeline.client.sanitize_text(text), json.dumps(candidates, ensure_ascii=False), clock.iso()))
    return {'token': token, 'candidates': candidates}


def save_about(pipeline, token, keys, decisions=None):
    store, cid = pipeline.store, pipeline.config.character.id
    with store.connect() as db:
        row = db.execute('SELECT * FROM import_reviews WHERE id=? AND character_id=?', (token, cid)).fetchone()
    if not row:
        raise KeyError(token)
    candidates = json.loads(row['candidates_json'])
    keys = sorted(set(keys))
    decisions = decisions or {}
    if set(decisions) - set(keys):
        raise ValueError('選んだ候補だけをまとめてください。')
    selected = [f for f in candidates if f['id'] in keys]
    for f in selected:
        matches = f.get('existing_facts', [])
        if matches and f['id'] not in decisions:
            raise ValueError('まとめる・両方残すを選んでください。')
        if decisions.get(f['id']) is not None and decisions[f['id']] not in {m['id'] for m in matches}:
            raise ValueError('確認した既存の事実だけを選んでください。')
    selection = {'keys': keys, 'decisions': decisions} if decisions else keys
    if row['selected_json'] is not None and store.import_status(cid,token)['status']=='undone':
        raise ValueError('取り消し済みです。読み取り直してください。')
    if set(keys) - {f['id'] for f in candidates}:
        raise ValueError('確認した候補だけを選んでください。')
    if row['selected_json'] is not None:
        if json.loads(row['selected_json']) != selection:
            raise ValueError('保存済みです。変更する場合は読み取り直してください。')
        return {'saved': len(keys), 'token': token}
    facts = [f for f in selected if f.get('destination', 'fact') == 'fact']
    requests = [f for f in selected if f.get('destination') == 'request']
    vectors = for_step(pipeline.client, 'import', 'import').embed([f['content'] for f in facts], pipeline.config.models.embedding, role='document') if facts else []
    if len(vectors) != len(facts) or any(not v or not all(math.isfinite(n) for n in v) for v in vectors):
        raise ValueError('invalid fact embeddings')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT selected_json FROM import_reviews WHERE id=?', (token,)).fetchone()[0]
        if current is not None:
            if json.loads(current) != selection:
                raise ValueError('保存済みです。')
            return {'saved': len(keys), 'token': token}
        if facts:
            store.check_embedding_model(db, writing=True)
        if requests:
            checked = store.apply_request_operations(db, pipeline.character, None, [f['request'] for f in requests],
                pipeline.config.requests, requests[0]['request_version'], source='import', initial=getattr(pipeline, 'provisional', False))
            if checked['rejected']:
                raise ValueError('お願いが更新されたか、ID・欄・上限が不正です。もう一度読み取ってください。')
            if checked.get('version'):
                db.execute('INSERT INTO request_imports VALUES (?,?)', (token, checked['version']))
        now = clock.iso()
        merge_targets = {}
        for f in selected:
            target = decisions.get(f['id'])
            if target is not None:
                prior = next(m for m in f['existing_facts'] if m['id'] == target)
                current_fact = db.execute("SELECT * FROM user_facts WHERE id=? AND character_id=? AND status='current'", (target, cid)).fetchone()
                if not current_fact or any(current_fact[k] != prior[k] for k in prior):
                    raise ValueError('既存の事実が変わりました。もう一度確かめてください。')
                merge_targets[target] = dict(current_fact)
        db.execute("INSERT INTO import_batches VALUES (?,?,'completed','{}',?,?,NULL,0)", (token, cid, now, now))
        with store.import_journal(db, token):
            for f, v in zip(facts, vectors):
                target = decisions.get(f['id'])
                if target is not None:
                    current_fact = merge_targets[target]
                    # Equivalent content was judged by the LLM and selected by
                    # the user. Keep its identity, provenance and mention policy.
                    db.execute('UPDATE user_facts SET last_confirmed_at=?,private=?,private_category=? WHERE id=?',
                        (max(now, current_fact['last_confirmed_at']), int(bool(current_fact['private'] or f['private'])),
                         current_fact['private_category'] if current_fact['private'] else f['private_category'], target))
                    if f['private']:
                        current_fact.update(private=True, private_category=current_fact['private_category'] or f['private_category'])
                    continue
                db.execute('INSERT INTO user_facts (character_id,content,learned_at,last_confirmed_at,importance,pinned,embedding,private,private_category,follow_up) VALUES (?,?,?,?,.7,0,?,?,?,0)',
                    (cid, f['content'], now, now, pack_vector(v), int(f['private']), f['private_category']))
        db.execute('UPDATE import_reviews SET selected_json=? WHERE id=?', (json.dumps(selection), token))
    return {'saved': len(keys), 'token': token}
