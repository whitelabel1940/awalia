"""RP1: embedding candidates and LLM-only decisions about repeated points."""
import hashlib
import math
from datetime import timedelta
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from cbs import clock


class ShareChoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Literal['new', 'append', 'skip'] = 'new'
    thread_ref: str | None = None
    detail: str = Field(default='', max_length=2400)


class ShareReview(ShareChoice):
    llm_role: ClassVar[str] = 'steward'


def nearest(pipeline, queries, items, text, *, limit=6):
    """Only narrow candidates; scores never decide whether meanings overlap."""
    if not items or not queries:
        return []
    from cbs.memory import _source_vectors, cosine
    from cbs.timing import embed
    pipeline.store.require_embedding_model()
    model = pipeline.config.models.embedding
    keyed = [('rp1', item, text(item), hashlib.sha256((model + text(item)).encode()).hexdigest())
             for item in items]
    cached = _source_vectors(pipeline, keyed)
    vectors = embed(pipeline, queries, role='query', purpose='autonomy')
    if len(vectors) != len(queries) or any(not v or not all(math.isfinite(n) for n in v) for v in vectors):
        raise ValueError('Invalid RP1 query embeddings')
    selected = set()
    for vector in vectors:
        ranked = sorted(enumerate(keyed), key=lambda pair: (-cosine(vector, cached[pair[1][3]]), pair[0]))
        selected.update(i for i, _ in ranked[:limit])
    return [item for i, item in enumerate(items) if i in selected]


def thread_text(thread):
    return thread['topic'] + '\n' + thread['detail']


def share_candidates(pipeline, query, *, kinds=('share', 'want'), exclude=None):
    store, cid = pipeline.store, pipeline.config.character.id
    since = clock.now() - timedelta(days=pipeline.config.autonomy.proactive_history_days)
    items = [t for t in store.list_threads(cid) if t['ref'] != exclude and t['kind'] in kinds
             and not t.get('private') and clock.parse(t['created_at']) <= clock.now()
             and (t['status'] == 'open' or (t['status'] == 'used'
                  and since <= clock.parse(t['updated_at']) <= clock.now()))]
    return [{k: v for k, v in t.items() if k != 'history'} for t in
            nearest(pipeline, [query], items, thread_text)]


def spoken_context(pipeline, queries):
    """Completed utterances across all sessions, split so conversations survive."""
    store, cid = pipeline.store, pipeline.config.character.id
    since = (clock.now() - timedelta(days=pipeline.config.autonomy.proactive_history_days)).isoformat()
    with store.connect() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT t.id,t.kind,t.user_text,t.assistant_text,COALESCE(t.assistant_at,t.created_at) AS spoken_at "
            "FROM turns t JOIN sessions s ON s.id=t.session_id WHERE s.character_id=? AND t.status='completed' "
            "AND t.assistant_text IS NOT NULL AND t.assistant_text!='' "
            "AND julianday(COALESCE(t.assistant_at,t.created_at))>=julianday(?) "
            "AND julianday(COALESCE(t.assistant_at,t.created_at))<=julianday(?) ORDER BY t.id",
            (cid, since, clock.iso()))]
    def content(t):
        return t['user_text'] + '\n' + t['assistant_text']
    result = []
    for kind in ('proactive', 'conversation'):
        result.extend(nearest(pipeline, queries, [t for t in rows if t['kind'] == kind], content))
    return result


def consolidate_shares(pipeline):
    """Daily, review each open share once; used rows are read-only evidence."""
    from cbs.autonomy import ask
    store, cid = pipeline.store, pipeline.config.character.id
    reviewed = []
    ids = [t['id'] for t in store.list_threads(cid) if t['kind'] == 'share' and t['status'] == 'open']
    for tid in ids:
        source = next(t for t in store.list_threads(cid) if t['id'] == tid)
        if source['status'] != 'open':
            continue
        candidates = share_candidates(pipeline, thread_text(source), kinds=('share',), exclude=source['ref'])
        if not candidates:
            continue
        try:
            choice, trace = ask(pipeline, 'share_review', ShareReview,
                                {'source': source, 'candidates': candidates, 'mode': 'daily'})
        except Exception as error:
            # One failed review leaves that share as it is; the rest are still reviewed.
            reviewed.append({'source_ref': source['ref'], 'error': type(error).__name__})
            continue
        with store.connect() as db:
            # A daily merge must never reopen or modify a used target: an invalid
            # target keeps the source unchanged instead of stopping the whole pass.
            if choice.operation == 'append':
                target = next((t for t in candidates if t['ref'] == choice.thread_ref and t['status'] == 'open'), None)
                if target is None:
                    reviewed.append({'source_ref': source['ref'], 'choice': choice.model_dump(), 'kept': 'invalid target'})
                    continue
                store.apply_share_choice(db, cid, choice, candidates, source['topic'], '',
                                         source['source_ref'], source['weight'], daily=True)
            elif choice.operation == 'new' and choice.thread_ref is not None:
                reviewed.append({'source_ref': source['ref'], 'choice': choice.model_dump(), 'kept': 'invalid keep reference'})
                continue
            if choice.operation in ('append', 'skip'):
                row = db.execute("SELECT * FROM threads WHERE id=? AND character_id=? AND status='open'", (tid, cid)).fetchone()
                if row:
                    store._thread_status(db, row, 'dropped')
        reviewed.append({'source_ref': source['ref'], 'choice': choice.model_dump(), 'trace': trace})
    return reviewed
