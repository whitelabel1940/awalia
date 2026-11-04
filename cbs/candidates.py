"""Bound the combined Pre-Judge pool by shape and embedding rank only."""
from collections import Counter
from contextvars import copy_context
import hashlib
import json
from threading import Condition, Thread
from time import sleep

from cbs.memory import _source_vectors, cached_source_vectors, cosine
from cbs.timing import embed


def source_documents(pipeline, items):
    documents=[]
    for m in items:
        source = m
        kind = m['ref'].split(':')[0]
        if kind == 'understanding':
            # The retrieval payload is a projection of the stored understanding.
            source = {k:m[k] for k in ('id','kind','text','condition','stage','stage_label',
                                      'first_observed_at','last_observed_at','expires_at')}
        elif kind == 'interest':
            # Match timed_interest's payload without its changing time/salience labels.
            source = {k:v for k,v in m.items() if k in
                      ('id','topic','kind','detail','origin','status','first_seen','last_seen','elaborate')}
            source['history'] = [{
                'operation': h['operation'], 'created_at': h['created_at'],
                **{side:{k:v for k,v in (h[side] or {}).items() if k in ('topic','kind','detail','status')}
                   for side in ('before','after')},
            } for h in m.get('history', [])]
        elif kind == 'thread':
            # list_threads applies continuous weight decay at read time.
            source = {k:v for k,v in m.items() if k != 'weight'}
        content = json.dumps({k:v for k,v in source.items() if k not in
                              ('ref','embedding','time','event_time','due_time','scheduled_time',
                               'strength','rank','similarity','retrieval_reason','may_initiate')},
                             ensure_ascii=False, sort_keys=True)
        key = hashlib.sha256((pipeline.config.models.embedding + content).encode()).hexdigest()
        if kind == 'fact':
            # Fact retrieval embeds this exact text already; share that vector.
            content = m['content']
            key = hashlib.sha256((pipeline.config.models.embedding + content).encode()).hexdigest()
        documents.append((kind, m, content, key))
    return documents


def warm_candidate_embeddings(pipeline):
    """Worker-only scan of sources that can reach the cap without a similarity."""
    store,cid=pipeline.store,pipeline.config.character.id
    items=[{'ref':f"canon:{m['id']}",**m} for m in store.list_canon(cid)]
    items += [{'ref':f"plan:{m['plan_id']}",**m} for m in store.list_plans(cid)]
    items += [{'ref':f"episode:{m['id']}",**m} for m in store.list_episodes(cid)]
    items += store.list_threads(cid,eligible_only=True)
    # retrieve supplies similarities for memory/digest/interest/understanding
    # and indexed facts. CBS episode:N above is a different, unranked source.
    items += [{'ref':f"fact:{m['id']}",**m} for m in store.list_user_facts(cid,current_only=True)
              if not m.get('embedding')]
    warm_source_documents(pipeline, source_documents(pipeline,items))


def warm_source_documents(pipeline, documents):
    pipeline.store.require_embedding_model()
    for offset in range(0,len(documents),64):
        _source_vectors(pipeline,documents[offset:offset+64])
        sleep(.01)  # Release CPU/DB between small chunks, including local providers.


class EmbeddingWarmer:
    """Optional, coalesced work; never a member of the steward/Extract queue.

    No foreground path waits on this daemon. wait() is only for explicit offline
    checks/test cleanup. DB connections are closed before every provider call.
    """
    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.condition = Condition()
        self.pending = {}
        self.refresh = False
        self.thread = None
        self.last_error = None

    def request(self, documents=(), *, refresh=False):
        with self.condition:
            self.pending.update((entry[3],entry) for entry in documents)
            self.refresh |= refresh
            if self.thread is None and (self.pending or self.refresh):
                self.last_error = None
                self.thread = Thread(target=copy_context().run, args=(self._run,),
                                     name='embedding-warm', daemon=True)
                self.thread.start()

    def _run(self):
        from cbs.memory import warm_user_fact_embeddings
        while True:
            with self.condition:
                refresh, self.refresh = self.refresh, False
                keys = list(self.pending)[:64]
                documents = [self.pending.pop(key) for key in keys]
                if not refresh and not documents:
                    self.thread = None
                    self.condition.notify_all()
                    return
            try:
                if refresh:
                    warm_user_fact_embeddings(self.pipeline)
                    warm_candidate_embeddings(self.pipeline)
                if documents:
                    warm_source_documents(self.pipeline,documents)
            except Exception as error:
                # Optional indexing may fail; a later request retries cache misses.
                self.last_error = type(error).__name__

    def wait(self, timeout=30):
        with self.condition:
            if not self.condition.wait_for(lambda: self.thread is None, timeout):
                raise TimeoutError('background embedding warm is still running')


def cap_candidates(pipeline, candidates, text, recent, retrieved):
    cfg = pipeline.config.memory
    # Preserve the original order and payload for small pools.
    unique = {m['ref']: m for m in candidates}
    items = list(unique.values())
    # Check shares this durable cache. Only the background worker may fill it.
    documents = source_documents(pipeline,[m for m in items if 'similarity' not in m])
    vectors = cached_source_vectors(pipeline, documents)
    missing = [entry for entry in documents if entry[3] not in vectors]
    if missing:
        pipeline._embedding_warmer.request(missing)
    if len(items) <= cfg.candidate_max:
        return items
    vector = getattr(retrieved, 'query_vector', None)
    if vector is None:
        query = '\n'.join([*[f"ユーザー: {t['user_text']}\nパートナー: {t['assistant_text']}" for t in recent[-3:]], text])
        vector = embed(pipeline, [query], role='query', purpose='candidate_cap')[0]
    # The same durable document cache used by Check. Dates/rank are supplied
    # after selection, so elapsed labels never cause document re-embedding.
    similarity = {m['ref']: cosine(vector, vectors[key]) for _,m,_,key in documents if key in vectors}
    similarity.update({m['ref']:m['similarity'] for m in items if 'similarity' in m})
    uncached = {m['ref'] for _,m,_,_ in missing}
    def rank(m):
        # New records get their kind's allotment first, then shared spare slots.
        return (m['ref'] not in uncached, -similarity.get(m['ref'], 0), m['ref'])
    selected = []
    for kind, minimum in cfg.candidate_min_per_kind.items():
        group = [m for m in items if m['ref'].split(':')[0] == kind]
        # M4's archive gate already selected strong cues. Retain its up-to-two
        # originals inside the memory allotment, including exact fact sources.
        if kind == 'memory':
            group.sort(key=lambda m:(m.get('status') != 'archived', *rank(m)))
        else:
            group.sort(key=rank)
        selected.extend(group[:minimum])
    chosen = {m['ref'] for m in selected}
    selected.extend(sorted((m for m in items if m['ref'] not in chosen), key=rank)[:cfg.candidate_max-len(selected)])
    return selected


def candidate_counts(items):
    return dict(Counter(m['ref'].split(':')[0] for m in items))
