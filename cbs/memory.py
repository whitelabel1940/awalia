"""Episodizer queue, embeddings retrieval and strength calculations."""
from cbs.llm.cost import for_step
from cbs.timing import embed as timed_embed, span
from datetime import date, timedelta
from cbs.storage import pack_vector, unpack_vector, EmbeddingModelMismatch
import json
import math
from array import array
import sys
from time import perf_counter
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from cbs import clock
from cbs.interests import timed_interest
from cbs.runtime import read_prompt, character_prefix
from cbs.steward import submit
from cbs.knowing import Observation


class MemoryField(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationEpisode(MemoryField):
    summary: str = Field(min_length=1)
    details: str = Field(min_length=1)
    importance: float = Field(ge=0, le=1)
    pinned: bool
    first_turn: int | None = None
    last_turn: int | None = None


class UserFact(MemoryField):
    private: bool = False
    private_category: str | None = None
    operation: Literal["new", "confirm", "update"]
    fact_id: int | None = None
    content: str = Field(min_length=1)
    source_episode: int = Field(default=0, ge=0)
    importance: float = Field(ge=0, le=1)
    pinned: bool
    event_date: date | None = None
    learned_turn: int | None = None
    follow_up: bool = True
    follow_up_thread: int | None = None


class EpisodizerOutput(MemoryField):
    llm_role: ClassVar[str] = "steward"
    episodes: list[ConversationEpisode] = Field(min_length=1, max_length=5)
    user_facts: list[UserFact]
    observations: list[Observation] = Field(default_factory=list, max_length=12)
    haru_expressed: list["HaruExpressed"] = Field(default_factory=list)


class ImportFactsCheck(MemoryField):
    llm_role: ClassVar[str] = 'steward'
    user_facts: list[UserFact]


class HaruExpressed(MemoryField):
    operation: Literal["new", "reinforce", "change", "weaken"]
    interest_id: int | None = None
    topic: str = Field(min_length=1)
    kind: Literal["like", "dislike", "curiosity", "into", "opinion"]
    detail: str = Field(min_length=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    evidence_turns: list[int] = Field(min_length=1)
    source_episode: int = Field(default=0, ge=0)


class NowSummary(MemoryField):
    llm_role: ClassVar[str] = "steward"
    summary: str = Field(min_length=1)


class EpisodeLink(MemoryField):
    target_episode: int
    relation: Literal["continues", "updates", "contradicts", "same_topic", "none"]
    reason: str


class EpisodeLinks(MemoryField):
    links: list[EpisodeLink]


class MonthlyDigest(MemoryField):
    llm_role: ClassVar[str] = "steward"
    summary: str = Field(min_length=1)
    details: str = Field(min_length=1)


def strength(memory, current=None):
    base = memory["importance"] + 0.3 * math.log1p(memory.get("recall_count", 0))
    elapsed = max(0, ((clock.parse(current) if current else clock.now()) -
                     clock.parse(memory.get("last_recalled_at") or memory.get("created_at") or memory["learned_at"])).total_seconds() / 86400)
    return base if memory["pinned"] else base * 0.5 ** (elapsed / 30)


def cosine(a, b):
    if len(a) != len(b) or not a:
        return 0.0
    # Python 3.14's C loop avoids several Python passes and temporary lists for
    # every record. Invalid/zero vectors still cannot enter the ranking.
    dot = math.sumprod(a,b)
    denominator = math.sqrt(math.sumprod(a,a) * math.sumprod(b,b))
    return dot / denominator if denominator and math.isfinite(denominator) and math.isfinite(dot) else 0.0


def vector_scores(pipeline, query, values):
    """Reuse decoded float32 documents and norms; content keys invalidate edits/reindex.

    Only this pipeline's current corpus is retained. No persistent index or new
    dependency; sumprod and the float32 source values preserve cosine ordering.
    """
    cache = getattr(pipeline, '_memory_vectors', {})
    current = {}
    qnorm = math.sumprod(query, query)
    scores = []
    for value in values:
        entry = cache.get(value) or current.get(value)
        if entry is None:
            if isinstance(value, bytes):
                vector = array('f')
                vector.frombytes(value)
                if sys.byteorder != 'little': vector.byteswap()
            else:
                vector = unpack_vector(value)
            entry = (vector, math.sumprod(vector, vector))
        current[value] = entry
        vector, norm = entry
        denominator = math.sqrt(qnorm * norm)
        dot = math.sumprod(query, vector) if len(query) == len(vector) else 0.0
        scores.append(dot / denominator if denominator and math.isfinite(denominator) and math.isfinite(dot) else 0.0)
    pipeline._memory_vectors = current
    return scores


class Retrieved(list):
    def __init__(self, items=(), query_vector=None):
        super().__init__(items)
        self.query_vector = query_vector


def timed_memory(memory, current=None):
    result = {k: v for k, v in memory.items() if k != "embedding"}
    value = memory.get("started_at") or memory.get("learned_at") or memory.get("created_at")
    if value:
        result["time"] = clock.stamp(value, current)
    if memory.get("month"):
        result["time"] = {"month": memory["month"], "absolute": memory["month"].replace("-", "年") + "月",
                          "relative": clock.relative(memory["month"] + "-01", current)}
    if memory.get("event_date"):
        result["event_time"] = clock.stamp(memory["event_date"], current)
    if memory.get("scheduled_date"):
        result["scheduled_time"] = clock.stamp(memory["scheduled_date"], current)
    if memory.get("due_at"):
        result["due_time"] = clock.stamp(memory["due_at"], current)
    return result


def timed_turn(turn):
    return {**turn, "user_time": clock.stamp(turn["created_at"]),
            "assistant_time": clock.stamp(turn.get("assistant_at") or turn["created_at"]),
            "utterances": [{'speaker': role, 'at': turn.get(at) or turn['created_at'], 'text': turn.get(text) or ''}
                           for role, at, text in (('user', 'created_at', 'user_text'), ('assistant', 'assistant_at', 'assistant_text'))],
            "date_lookup": clock.calendar(turn["created_at"])}


@span('user_retrieval')
def retrieve(pipeline, text, recent):
    from cbs.config import archived_margin
    pipeline.store.require_embedding_model()
    from cbs.interests import salience, timed_interest
    interests = pipeline.store.list_interests(pipeline.config.character.id)
    foreground = {i['id'] for i in sorted((i for i in interests if i['status'] != 'fading'),
                                        key=lambda i: (-salience(i), i['id']))[:6]}
    groups = (("memory", pipeline.store.list_memory_episodes(pipeline.config.character.id)),
              ("fact", pipeline.store.list_user_facts(pipeline.config.character.id, current_only=True)),
              ("digest", pipeline.store.list_monthly_digests(pipeline.config.character.id)),
              ("interest", [i for i in interests if i['id'] not in foreground]))
    from cbs.knowing import candidates as understanding_candidates
    if not any(items for _, items in groups):
        pipeline._memory_vectors = {}
        return Retrieved(understanding_candidates(pipeline))
    query = "\n".join([*[f"ユーザー: {t['user_text']}\nパートナー: {t['assistant_text']}" for t in recent[-3:]], text])
    vector = timed_embed(pipeline, [query], role="query", purpose="user_retrieval")[0]
    documents = [(prefix, memory) for prefix, memories in groups for memory in memories if memory.get('embedding')]
    scored = [(prefix, memory, similarity) for (prefix, memory), similarity in zip(
        documents, vector_scores(pipeline, vector, [m['embedding'] for _, m in documents]))]
    # Archived originals come back only on a cue clearly stronger than this query's background similarity.
    # Relative to the mean so it does not depend on one embedding model's absolute scale.
    memory_scores = [s for prefix, _, s in scored if prefix != "interest"]
    baseline = sum(memory_scores) / len(memory_scores) if memory_scores else 0.0
    margin = archived_margin(pipeline.config)
    candidates = []
    for prefix, memories in groups:
        ranked = []
        for _, memory, similarity in (item for item in scored if item[0] == prefix):
            if memory.get("status") == "archived" and similarity < baseline + margin:
                continue
            power = salience(memory) if prefix == "interest" else strength(memory)
            ranked.append({**memory, "ref": f"{prefix}:{memory['id']}",
                           "strength": power, "similarity": similarity, "rank": similarity * (0.5 + power)})
        selected = sorted(ranked, key=lambda m: (-m["rank"], m["id"]))[:8]
        if prefix == 'memory':
            # A clear semantic cue already passed the archived gate above.
            # Do not let age decay and many recent records crowd it out again.
            cue = sorted((m for m in ranked if m.get('status')=='archived'),key=lambda m:(-m['similarity'],m['id']))[:2]
            selected = cue + [m for m in selected if m['id'] not in {c['id'] for c in cue}][:8-len(cue)]
        candidates.extend({**timed_interest(m), **{k:m[k] for k in ('strength','similarity','rank')}}
                          if prefix == 'interest' else timed_memory(m) for m in selected)
        # Manual corrections have no vector yet. Keep a bounded set immediately usable.
        if prefix == 'fact':
            candidates.extend({**timed_memory(m), 'ref': f"fact:{m['id']}"} for m in memories[-8:] if not m.get('embedding'))
    # Facts are extracted from an exact source episode. A relevant fact can
    # reconnect that original even when similar facts/digests inflated the
    # archive gate's background mean. Private facts do not expand evidence.
    source_ids = [m.get('source_episode') for m in sorted((m for m in candidates if m['ref'].startswith('fact:') and not m.get('private')),
        key=lambda m:-m.get('similarity',0))[:2]]
    existing_refs = {m['ref'] for m in candidates}
    originals = []
    for prefix,memory,similarity in scored:
        if prefix=='memory' and memory['id'] in source_ids and memory.get('status')=='archived' and f"memory:{memory['id']}" not in existing_refs:
            power = strength(memory)
            originals.append({**timed_memory(memory),'ref':f"memory:{memory['id']}",'strength':power,'similarity':similarity,'rank':similarity*(.5+power),'retrieval_reason':'fact_source'})
    if originals:
        keep = [m for m in candidates if m['ref'].startswith('memory:')][:8-len(originals)]
        candidates = [m for m in candidates if not m['ref'].startswith('memory:')] + originals + keep
    pipeline.store.require_embedding_model()
    candidates.extend(understanding_candidates(pipeline, vector))
    return Retrieved(candidates, vector)


def enqueue_pending(pipeline, retry=False):
    for job in pipeline.store.memory_jobs(retry=retry, character_id=pipeline.config.character.id):
        submit(pipeline.store, episodize, pipeline, job)


def _reply_sources(pipeline):
    import hashlib
    store, cid = pipeline.store, pipeline.config.character.id
    groups = [('canon', store.list_canon(cid)), ('plan', store.list_plans(cid))]
    keyed = []
    for prefix, items in groups:
        for item in items:
            text = json.dumps(item, ensure_ascii=False, sort_keys=True)
            key = hashlib.sha256((pipeline.config.models.embedding + text).encode()).hexdigest()
            keyed.append((prefix, item, text, key))
    return groups, keyed


def warm_reply_embeddings(pipeline):
    """Optional document indexing after persistence; retrieval retains lazy fallback."""
    pipeline.store.require_embedding_model()
    _, keyed = _reply_sources(pipeline)
    _source_vectors(pipeline, keyed)


def warm_user_fact_embeddings(pipeline):
    """Index notebook corrections after the response, never in the edit/reply path."""
    store, cid = pipeline.store, pipeline.config.character.id
    from cbs.config import embedding_model
    wanted = embedding_model(pipeline.config)
    missing = [f for f in store.list_user_facts(cid,current_only=True) if not f['embedding']][:64]
    if not missing:
        return
    store.require_embedding_model()
    from cbs.candidates import source_documents
    documents = source_documents(pipeline,[{**f,'ref':f"fact:{f['id']}"} for f in missing])
    vectors = _source_vectors(pipeline,documents)
    with store.connect() as db:
        store.check_embedding_model(db,wanted,writing=True)
        for fact,document in zip(missing,documents):
            vector = vectors[document[3]]
            db.execute("UPDATE user_facts SET embedding=? WHERE id=? AND character_id=? AND status='current' AND content=? AND embedding IS NULL", (pack_vector(vector),fact['id'],cid,fact['content']))


def cached_source_vectors(pipeline, keyed):
    """Read only the requested source vectors; never generate missing documents."""
    keys = list(dict.fromkeys(k[3] for k in keyed))
    cached = {}
    with pipeline.store.connect() as db:
        for offset in range(0, len(keys), 500):
            batch = keys[offset:offset+500]
            rows = db.execute('SELECT * FROM reply_embeddings WHERE cache_key IN (' +
                              ','.join('?' for _ in batch) + ')', batch)
            cached.update((r['cache_key'], unpack_vector(r['embedding'])) for r in rows)
        # A fact's cap document is exactly the text used by its retrieval index.
        # Reuse any already indexed copy, without calling the provider again.
        for kind,item,content,key in keyed:
            if kind == 'fact' and key not in cached:
                row = db.execute('SELECT embedding FROM user_facts WHERE character_id=? AND content=? '
                                 'AND embedding IS NOT NULL LIMIT 1',
                                 (pipeline.config.character.id,content)).fetchone()
                if row:
                    cached[key] = unpack_vector(row['embedding'])
    return cached


def _source_vectors(pipeline, keyed):
    from hashlib import sha256
    store = pipeline.store
    model = pipeline.config.models.embedding
    wanted = model if '/' in model else pipeline.config.llm.provider + '/' + model
    if any(key != sha256((model + content).encode()).hexdigest() for _,_,content,key in keyed):
        raise EmbeddingModelMismatch('queued source embedding model changed')
    cached = cached_source_vectors(pipeline, keyed)
    missing = list({k[3]:k for k in keyed if k[3] not in cached}.values())
    if missing:
        vectors = timed_embed(pipeline, [k[2] for k in missing], role="document", purpose="reply_sources", model=model)
        if len(vectors) != len(missing) or any(not v or not all(math.isfinite(n) for n in v) for v in vectors):
            raise ValueError('invalid source embeddings')
        with store.connect() as db:
            store.check_embedding_model(db, wanted, writing=True)
            for entry, vector in zip(missing, vectors):
                cached[entry[3]] = vector
                db.execute('INSERT OR IGNORE INTO reply_embeddings VALUES (?, ?)', (entry[3], pack_vector(vector)))
    return cached


def reply_candidates(pipeline, reply, packet):
    """Nearest canon/plans plus every Packet source and active plan; cache source vectors."""
    pipeline.store.require_embedding_model()
    groups, keyed = _reply_sources(pipeline)
    # Counts only: no semantic filtering, and every small-group record survives.
    large = {prefix for prefix, items in groups if len(items) > 6}
    if large:
        cached = _source_vectors(pipeline, [k for k in keyed if k[0] in large])
        vectors = timed_embed(pipeline, [reply], role="query", purpose="reply_retrieval")
        if len(vectors) != 1 or not vectors[0] or not all(math.isfinite(n) for n in vectors[0]):
            raise ValueError('invalid query embedding')
        query = vectors[0]
    selected = []
    for prefix, _ in groups:
        entries_for_group = [k for k in keyed if k[0] == prefix]
        nearest = (sorted(entries_for_group, key=lambda k: -cosine(query, cached[k[3]]))[:6]
                   if prefix in large else entries_for_group)
        entries = {k[1]['id' if prefix == 'canon' else 'plan_id']: timed_memory(k[1]) for k in nearest}
        keys = ('canon_facts', 'counterevidence_memories') if prefix == 'canon' else ('unfinished_ledger', 'counterevidence_memories')
        for key in keys:
            for item in packet.get(key, []):
                if key == 'counterevidence_memories' and not item['ref'].startswith(prefix + ':'):
                    continue
                entries[item['id' if prefix == 'canon' else 'plan_id']] = item
        selected.append(list(entries.values()))
    pipeline.store.require_embedding_model()
    return selected


def episodize(pipeline, job, *, followups=True):
    store = pipeline.store
    with store.connect() as connection:
        if not connection.execute("UPDATE memory_jobs SET status='running' WHERE id=? AND status='pending' AND conversation_id IN "
                "(SELECT c.id FROM conversations c JOIN sessions s ON s.id=c.session_id WHERE s.character_id=?)", (job["id"],pipeline.config.character.id)).rowcount:
            return
        turns = [dict(r) for r in connection.execute("SELECT * FROM turns WHERE conversation_id=? AND status='completed' AND id BETWEEN ? AND ? ORDER BY id",
                 (job["conversation_id"], job["first_turn"], job["last_turn"]))]
    from cbs.photos import model_turns
    turns = model_turns(store, turns)
    trace, started = {}, perf_counter()
    try:
        context = {"conversation_id": job["conversation_id"], "turns": [timed_turn({k: v for k, v in t.items() if k != "trace_json"}) for t in turns],
                   "current_threads": store.list_threads(pipeline.config.character.id),
                   "private_categories": pipeline.config.knowing.model_dump()['private_categories'],
                   "current_user_facts": [timed_memory(f) for f in store.list_user_facts(pipeline.config.character.id, current_only=True)],
                   "current_interests": [timed_interest(i) for i in store.list_interests(pipeline.config.character.id)]}
        if not followups:
            context['import_context'] = read_prompt('import_context')
            facts = store.list_user_facts(pipeline.config.character.id,current_only=True)
            interests = store.list_interests(pipeline.config.character.id)
            if len(facts)>64 or len(interests)>32:
                query = for_step(pipeline.client, 'memory', 'memory').embed(['\n'.join(t['user_text']+'\n'+(t['assistant_text'] or '') for t in turns)],pipeline.config.models.embedding,role='query')[0]
                def nearest(items,limit):
                    return sorted(items,key=lambda i:(cosine(query,unpack_vector(i['embedding'])) if i.get('embedding') else 0,i['id']),reverse=True)[:limit]
                context['current_user_facts'] = [timed_memory(f) for f in nearest(facts,64)]
                context['current_interests'] = [timed_interest(i) for i in nearest(interests,32)]
                context['existing_context_is_subset'] = True
        knowing_setting = store.knowing_setting(pipeline.config.character.id)
        context['inference_stopped'] = bool(knowing_setting['stopped'])
        messages = [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        trace = {"request": {"model": pipeline.config.models.steward, "system": (read_prompt("episodizer") + character_prefix(pipeline.character, actor=True, store=pipeline.store)), "messages": messages,
                             "schema": EpisodizerOutput.model_json_schema()}}
        result = for_step(pipeline.client, 'memory', 'memory').generate_json(pipeline.config.models.steward, (read_prompt("episodizer") + character_prefix(pipeline.character, actor=True, store=pipeline.store)), messages, EpisodizerOutput)
        trace = pipeline._safe_trace(result.trace())
        output = EpisodizerOutput.model_validate_json(pipeline.client.sanitize_text(result.parsed.model_dump_json()))
        if not followups and output.user_facts:
            # Import runs newest first. A dedicated semantic check must not
            # combine an older report with a later existing event or swap the
            # speaker and addressee. Code provides chronology, not meaning.
            review_context = {**context, 'episodes': [e.model_dump(mode='json') for e in output.episodes],
                'proposed_facts': [f.model_dump(mode='json') for f in output.user_facts]}
            review = for_step(pipeline.client, 'memory', 'memory').generate_json(pipeline.config.models.steward, read_prompt('import_facts_check'),
                [{'role': 'user', 'content': json.dumps(review_context, ensure_ascii=False)}], ImportFactsCheck)
            checked = ImportFactsCheck.model_validate_json(pipeline.client.sanitize_text(review.parsed.model_dump_json()))
            output = output.model_copy(update={'user_facts': checked.user_facts})
            trace['facts_check'] = pipeline._safe_trace(review.trace())
        texts = ([e.summary + "\n" + e.details for e in output.episodes] + [f.content for f in output.user_facts]
                 + [i.topic + "\n" + i.detail for i in output.haru_expressed])
        embedding_started = perf_counter()
        store.require_embedding_model()
        vectors = for_step(pipeline.client, 'memory', 'memory').embed(texts, pipeline.config.models.embedding, role="document")
        if len(vectors) != len(texts) or any(not v or not all(math.isfinite(n) for n in v) for v in vectors):
            raise ValueError("invalid embeddings")
        trace["embedding"] = {"model": pipeline.config.models.embedding, "count": len(texts),
                              "latency_ms": round((perf_counter() - embedding_started) * 1000, 2)}
        trace['knowing_revision'] = knowing_setting['revision']
        ids = store.save_memories(job, pipeline.config.character.id, output, vectors, turns, trace)
    except Exception as error:
        trace = pipeline._safe_trace(getattr(error, "trace", None) or trace)
        trace["error"] = "Episodizer に失敗しました。会話を保持し、次回 maintenance で再試行します。"
        trace.setdefault("latency_ms", round((perf_counter() - started) * 1000, 2))
        with store.connect() as connection:
            connection.execute("UPDATE memory_jobs SET status='failed', trace_json=? WHERE id=?", (json.dumps(trace, ensure_ascii=False), job["id"]))
        return
    if not followups:
        return ids
    from cbs.inner import update_inner
    update_inner(pipeline, f"memory_job:{job['id']}", {'kind': 'conversation',
        'conversation_id': job['conversation_id'], 'summary': output.model_dump(mode='json')})
    for episode_id in ids or []:
        link_episode(pipeline, episode_id)
    catch_up_diaries(pipeline, job)


def catch_up_diaries(pipeline, job):
    """Today's pass skips a day whose conversation was still open; write it once that conversation is saved."""
    store, cid = pipeline.store, pipeline.config.character.id
    today = clock.now().date().isoformat()
    with store.connect() as db:
        # Before today's pass the pass itself will write the day; never race it.
        if not db.execute("SELECT 1 FROM memory_maintenance WHERE character_id=? AND local_day=? AND status IN ('completed','failed')",
                          (cid, today)).fetchone():
            return None
        span = db.execute('SELECT started_at,last_activity_at FROM conversations WHERE id=?', (job['conversation_id'],)).fetchone()
    if not span:
        return None
    first, last = clock.parse(span['started_at']).date(), clock.parse(span['last_activity_at']).date()
    days = {(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)}
    days = {d for d in days if d < today}
    if not days:
        return None
    from cbs.note import fill_diaries
    return fill_diaries(pipeline, only_days=days)


def link_episode(pipeline, episode_id):
    store, trace = pipeline.store, {}
    try:
        episodes = store.list_memory_episodes(pipeline.config.character.id)
        new = next(e for e in episodes if e["id"] == episode_id)
        store.require_embedding_model()
        older = [e for e in episodes if e["id"] < episode_id and e.get("embedding")]
        if not older:
            store.save_episode_links(episode_id, [], "completed", {})
            return
        # The new episode searches older documents. Keep its saved document
        # vector unchanged; asymmetric local models need a query encoding here.
        vector = for_step(pipeline.client, 'memory', 'memory').embed([new["summary"] + "\n" + new["details"]],
                                       pipeline.config.models.embedding, role="query")[0]
        neighbours = sorted(older,
                            key=lambda e: (-cosine(vector, unpack_vector(e["embedding"])), e["id"]))[:5]
        context = {"new_episode": timed_memory(new), "neighbours": [timed_memory(e) for e in neighbours]}
        messages = [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        trace = {"request": {"model": pipeline.config.models.judge, "messages": messages,
                             "system": (read_prompt("episode_linker") + character_prefix(pipeline.character, actor=True, store=pipeline.store)), "schema": EpisodeLinks.model_json_schema()}}
        result = for_step(pipeline.client, 'memory', 'memory').generate_json(pipeline.config.models.judge, (read_prompt("episode_linker") + character_prefix(pipeline.character, actor=True, store=pipeline.store)), messages, EpisodeLinks)
        trace = pipeline._safe_trace(result.trace())
        output = EpisodeLinks.model_validate_json(pipeline.client.sanitize_text(result.parsed.model_dump_json()))
        allowed = {e["id"] for e in neighbours}
        links = {link.target_episode: link for link in output.links if link.target_episode in allowed}
        trace["warnings"] = ["unknown neighbour dropped"] if any(l.target_episode not in allowed for l in output.links) else []
        store.save_episode_links(episode_id, links.values(), "completed", trace)
    except Exception as error:
        trace = pipeline._safe_trace(getattr(error, "trace", None) or trace)
        trace["error"] = "リンク判定に失敗しました。エピソードと会話は保持します。"
        store.save_episode_links(episode_id, [], "failed", trace)


def digest_candidates(episodes, settings):
    return [e for e in episodes if e["status"] == "active" and not e["pinned"]
            and (clock.now() - clock.parse(e["created_at"])).total_seconds() >= settings.digest_age_days * 86400
            and strength(e) < settings.digest_strength_threshold]


def daily_maintenance(pipeline):
    try:
        return _daily_maintenance(pipeline)
    finally:
        # Scheduling only: never add disk/crypto work to the serialized DB queue.
        worker = getattr(pipeline.store, 'backup_worker', None)
        if worker is not None and not worker.closed:
            worker.request_due()


def _daily_maintenance(pipeline):
    """Serialized after pending Episodizers. One attempt per local calendar day."""
    store, character_id = pipeline.store, pipeline.config.character.id
    from cbs.days import initialize_birthday
    initialize_birthday(pipeline)
    today = clock.now().date().isoformat()
    legacy_trace, legacy_status = None, None
    with store.connect() as connection:
        if not connection.execute("INSERT OR IGNORE INTO memory_maintenance (character_id, local_day, status) VALUES (?, ?, 'running')",
                                  (character_id, today)).rowcount:
            previous = connection.execute('SELECT * FROM memory_maintenance WHERE character_id=? AND local_day=?',
                                          (character_id, today)).fetchone()
            legacy_trace = json.loads(previous['trace_json'])
            if all(key in legacy_trace for key in ('now_summary', 'storage', 'knowing', 'diaries', 'forming_notes', 'ex2', 'rp1_shares')):
                return
            # M5 may already have maintained this day. Add the new M6 work once,
            # without repeating its monthly digest attempt.
            legacy_status = previous['status']
            connection.execute("UPDATE memory_maintenance SET status='running' WHERE character_id=? AND local_day=?", (character_id, today))
    trace, failed = legacy_trace if legacy_trace is not None else {"months": []}, legacy_status == 'failed'
    if 'rp1_shares' not in trace:
        try:
            from cbs.sharing import consolidate_shares
            with store.connect() as connection:
                done = connection.execute("SELECT 1 FROM memory_maintenance WHERE character_id=? AND local_day<=? "
                    "AND json_extract(trace_json,'$.rp1_shares.completed')=1 LIMIT 1", (character_id, today)).fetchone()
            trace['rp1_shares'] = {'completed': True, 'already_completed': bool(done),
                                   'reviews': [] if done else consolidate_shares(pipeline)}
        except Exception:
            failed = True
            trace['rp1_shares'] = {'error': '話したいことの整理に失敗しました。翌日の手入れで再試行します。'}
    from cbs.note import fill_past_notes, fill_activity_notes, fill_diaries, fill_forming_notes
    fill_past_notes(pipeline)
    fill_activity_notes(pipeline)
    trace['diaries'] = fill_diaries(pipeline)
    failed |= any(d['status']=='failed' for d in trace['diaries'])
    trace['forming_notes'] = fill_forming_notes(pipeline)
    trace['ex2'] = True
    failed |= any(n['status']=='failed' for n in trace['forming_notes'])
    trace['storage'] = store.trim_traces(pipeline.config.storage.full_trace_days)
    from cbs.knowing import rebuild
    trace['knowing'] = rebuild(pipeline)
    try:
        from cbs.interests import timed_interest
        store.fade_interests(character_id, pipeline.config.memory.interest_fade_days)
        interests = store.list_interests(character_id)
        missing = [i for i in interests if not i['embedding']]
        if missing:
            store.require_embedding_model()
            vectors = for_step(pipeline.client, 'memory', 'memory').embed([i['topic'] + '\n' + i['detail'] for i in missing], pipeline.config.models.embedding, role="document")
            if len(vectors) != len(missing) or any(not v or not all(math.isfinite(n) for n in v) for v in vectors):
                raise ValueError('invalid interest embeddings')
            with store.connect() as connection:
                store.check_embedding_model(connection, writing=True)
                for item, vector in zip(missing, vectors):
                    connection.execute('UPDATE interests SET embedding=? WHERE id=?', (pack_vector(vector), item['id']))
        # Fading interests must not return to the foreground through the summary.
        current = [timed_interest(i) for i in interests if i['status'] != 'fading']
        messages = [{'role': 'user', 'content': json.dumps({'interests': current}, ensure_ascii=False)}]
        from cbs.display_text import display_call
        result = display_call(lambda: for_step(pipeline.client, 'memory', 'memory').generate_json(pipeline.config.models.steward, (read_prompt('now_summary') + character_prefix(pipeline.character, actor=True, store=pipeline.store)), messages, NowSummary), lambda r: [r.parsed.summary])
        trace['now_summary'] = pipeline._safe_trace(result.trace())
        summary = pipeline.client.sanitize_text(result.parsed.summary)
        with store.connect() as connection:
            last_change = connection.execute('SELECT MAX(c.id) FROM interest_changes c JOIN interests i ON i.id=c.interest_id WHERE i.character_id=?', (character_id,)).fetchone()[0] or 0
            connection.execute('INSERT INTO now_summaries VALUES (?, ?, ?, ?) ON CONFLICT(character_id) DO UPDATE SET summary=excluded.summary, local_day=excluded.local_day, source_change=excluded.source_change',
                               (character_id, summary, today, last_change))
    except Exception as error:
        failed = True
        trace['now_summary'] = {**(error.metadata() if hasattr(error, 'metadata') else {}), 'error': '今の要約に失敗しました。前の要約を保持し、翌日の手入れで再試行します。'}
    episodes = store.list_memory_episodes(character_id)
    candidates = digest_candidates(episodes, pipeline.config.memory)
    months = [] if legacy_trace is not None else sorted({clock.parse(e["started_at"]).strftime("%Y-%m") for e in candidates})
    existing = {d["month"]: d for d in store.list_monthly_digests(character_id)}
    for month in months:
        item = {"month": month}
        try:
            ids = {e["id"] for e in candidates if clock.parse(e["started_at"]).strftime("%Y-%m") == month}
            ids.update(existing.get(month, {}).get("episode_ids", []))
            originals = [e for e in episodes if e["id"] in ids]
            context = {"month": month, "episodes": [timed_memory(e) for e in originals],
                       "user_facts": [timed_memory(f) for f in store.list_user_facts(character_id) if f["source_episode"] in ids]}
            messages = [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
            item["request"] = {"model": pipeline.config.models.steward, "system": (read_prompt("monthly_digest") + character_prefix(pipeline.character, actor=True, store=pipeline.store)),
                               "messages": messages, "schema": MonthlyDigest.model_json_schema()}
            result = for_step(pipeline.client, 'memory', 'memory').generate_json(pipeline.config.models.steward, (read_prompt("monthly_digest") + character_prefix(pipeline.character, actor=True, store=pipeline.store)), messages, MonthlyDigest)
            item.update(pipeline._safe_trace(result.trace()))
            output = MonthlyDigest.model_validate_json(pipeline.client.sanitize_text(result.parsed.model_dump_json()))
            store.require_embedding_model()
            vectors = for_step(pipeline.client, 'memory', 'memory').embed([output.summary + "\n" + output.details], pipeline.config.models.embedding, role="document")
            if len(vectors) != 1 or not vectors[0] or not all(math.isfinite(v) for v in vectors[0]):
                raise ValueError("invalid digest embedding")
            store.save_monthly_digest(character_id, month, output, vectors[0], sorted(ids))
            item["archived_episode_ids"] = sorted(ids)
        except Exception as error:
            failed = True
            item.update(pipeline._safe_trace(getattr(error, "trace", None) or {}))
            item["error"] = "月の要約に失敗しました。元のエピソードは保持し、次の日の手入れで再試行します。"
        trace["months"].append(item)
    with store.connect() as connection:
        connection.execute("UPDATE memory_maintenance SET status=?, trace_json=? WHERE character_id=? AND local_day=?",
                           ("failed" if failed else "completed", json.dumps(pipeline._safe_trace(trace), ensure_ascii=False), character_id, today))


def inspector_memory(store, character_id):
    return {"episodes": [{**timed_memory(m), "strength": strength(m)} for m in store.list_memory_episodes(character_id)],
            "user_facts": [{**timed_memory(m), "strength": strength(m)} for m in store.list_user_facts(character_id)],
            "links": store.list_episode_links(character_id),
            "digests": [{**timed_memory(m), "strength": strength(m)} for m in store.list_monthly_digests(character_id)],
            "conversations": store.list_conversations()}
