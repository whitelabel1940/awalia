from cbs.llm.cost import for_step
import re
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextvars import copy_context
from datetime import date, datetime
from threading import Lock, Event
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cbs.llm import LLMError
from cbs.character import effective_persona
from cbs.autonomy import FollowUp, ResearchQuestion
from cbs.runtime import read_prompt, character_prefix
from cbs.requests import RequestOperation, effective_character, prompt_context
from cbs.days import ImportantDay
from cbs import clock
from cbs.memory import timed_memory, timed_turn
from cbs.storage import EmbeddingModelMismatch
from cbs.timing import Timeline, current as current_timeline, span


class JudgeField(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DomainTouched(JudgeField):
    id: str
    strength: Literal["direct", "indirect"]


class PastTarget(JudgeField):
    life_ref: str
    aspect: str
    known: bool
    major: bool


class PastReference(JudgeField):
    asks_about_past: bool = False
    targets: list[PastTarget] = Field(default_factory=list)


Feature = Literal["yes", "no", "unclear"]


class MemoryUse(JudgeField):
    ref: str
    relevant_now: Feature
    counterevidence: Feature
    continuation: Feature
    private_topic_current: Feature = 'no'


class PreJudge(JudgeField):
    domains_touched: list[DomainTouched] = Field(default_factory=list)
    signatures_relevant: list[str] = Field(default_factory=list)
    past_reference: PastReference = Field(default_factory=PastReference)
    memory_use: list[MemoryUse] = Field(default_factory=list)
    emotional_weight: Literal["low", "medium", "high"] = "low"
    reveal_request: "RevealRequest | None" = None
    user_disclosure: Literal["yes", "no"] = "no"


class RevealRequest(JudgeField):
    secret_id: str
    manner: Literal["casual", "sincere", "pushy"]


class Disclosure(JudgeField):
    secret_id: str
    level: int = Field(ge=1, le=3)


class Claim(JudgeField):
    text: str
    verdict: Literal["supported", "new_minor_detail", "contradicted", "forbidden"]
    reason: str


class LedgerUpdate(JudgeField):
    operation: Literal["propose", "agree", "cancel", "reschedule", "report_done"]
    plan_id: int | None = Field(default=None, gt=0)
    content: str = Field(min_length=1)
    scheduled_text: str | None = None
    scheduled_date: date | None = None


class NewCanon(JudgeField):
    kind: Literal["past_detail", "daily_life", "shared_event"]
    content: str = Field(min_length=1)
    life_ref: str | None = None
    event_date: date | None = None


class PostCheck(JudgeField):
    claims: list[Claim]
    ledger_updates: list[LedgerUpdate]
    new_canon: list[NewCanon]
    disclosures: list[Disclosure] = Field(default_factory=list)
    used_memories: list[str] = Field(default_factory=list)


class Problem(JudgeField):
    text: str
    verdict: Literal["contradicted", "forbidden"]
    reason: str


class Check(JudgeField):
    problems: list[Problem]


class Extract(JudgeField):
    research_questions: list[ResearchQuestion] = Field(default_factory=list, max_length=2)
    # Typed so the strict JSON schema the providers require stays valid.
    day_operations: list[ImportantDay] = Field(default_factory=list)
    used_days: list[str] = Field(default_factory=list)
    request_operations: list[RequestOperation] = Field(default_factory=list)
    understanding_questions: list[str] = Field(default_factory=list, max_length=1)
    used_threads: list[str] = Field(default_factory=list)
    new_threads: list[FollowUp] = Field(default_factory=list)
    ledger_updates: list[LedgerUpdate]
    new_canon: list[NewCanon]
    disclosures: list[Disclosure]
    used_memories: list[str]


def assemble_packet(character, judge: PreJudge, *, current_datetime=None, previous_session_datetime=None,
                    candidate_memories=(), active_plans=(), model=None, episodes=(), reveal_states=None,
                    current_utterance=None, episode_links=(), memory_episodes=(), pending_correction=(),
                    interests=(), now_summary=None, activities=(), feeling=None):
    """Select by Judge IDs/status, never infer meaning from utterance text."""
    model = model or character.model.model_copy(deep=True)
    from cbs.steward import episode_counts
    from cbs.reveal import reveal_packet
    relevant = set(judge.signatures_relevant)
    signatures = {s.id: s.model_dump(mode="json") for s in model.signatures}
    domains = {d.id: d for d in model.domains}
    life = {element["id"]: element for element in model.life_history.elements()}
    # Unknown IDs from the Judge are dropped (and reported) rather than failing the turn.
    warnings = sorted((set(d.id for d in judge.domains_touched) - domains.keys()) | (relevant - signatures.keys())
                      | (set(t.life_ref for t in judge.past_reference.targets) - life.keys()))
    current = current_datetime["iso"] if current_datetime else None
    from cbs.autonomy_store import eligible
    candidate_memories = [m for m in candidate_memories if not m["ref"].startswith("thread:") or eligible(m, current)]
    private_allowed = {u.ref for u in judge.memory_use if u.private_topic_current == 'yes' and u.relevant_now == 'yes'}
    memories = {memory["ref"]: timed_memory(memory, current) for memory in candidate_memories
                if not memory.get('private') or memory.get('may_initiate') or memory['ref'] in private_allowed
                if not memory["ref"].startswith("fact:") or memory.get("status") == "current"}
    warnings += sorted({use.ref for use in judge.memory_use} - memories.keys())
    selected_canon = [memories[use.ref] for use in judge.memory_use
                      if use.ref in memories and use.relevant_now == "yes" and use.ref.startswith("canon:")]
    counterevidence = [memories[use.ref] for use in judge.memory_use if use.ref in memories and use.counterevidence == "yes"
                       and (not use.ref.startswith('interest:') or use.relevant_now == 'yes')]
    # updates edges point from the newer episode to its previous state.
    all_memory = {f"memory:{e['id']}": timed_memory({**e, "ref": f"memory:{e['id']}"}, current) for e in memory_episodes}
    all_memory.update({ref: m for ref, m in memories.items() if ref.startswith("memory:")})
    def memory_order(ref):
        item = all_memory[ref]
        return (item.get('ended_at') or item.get('created_at') or item.get('started_at') or '', item['id'])
    replacements = {}
    for link in episode_links:
        if link["relation"] == "updates":
            old, new = f"memory:{link['target_episode']}", f"memory:{link['source_episode']}"
            if new in all_memory and old in all_memory and memory_order(new) > memory_order(old):
                if old not in replacements or memory_order(new) > memory_order(replacements[old]):
                    replacements[old] = new

    def with_updates(items):
        selected = {}
        for item in items:
            ref = item["ref"]
            selected.setdefault(ref, item)
            while ref in replacements:
                newer = replacements[ref]
                selected[ref] = {**all_memory[ref], "memory_state": "previous", "superseded_by": newer}
                selected[newer] = {**all_memory[newer], "memory_state": "current"}
                ref = newer
        return list(selected.values())

    counterevidence = with_updates(counterevidence)
    episode_refs = list(dict.fromkeys(use.ref for use in judge.memory_use
                                    if use.ref in memories and use.relevant_now == "yes" and use.ref.startswith("episode:")))[:5]
    judge = judge.model_copy(update={
        "domains_touched": [d for d in judge.domains_touched if d.id in domains],
        "past_reference": judge.past_reference.model_copy(update={
            "targets": [t for t in judge.past_reference.targets if t.life_ref in life]}),
    })
    relevant &= signatures.keys()
    from cbs.interests import timed_interest
    foreground = sorted((timed_interest(i, current) for i in interests if i['status'] != 'fading'),
                        key=lambda i: (-i['salience'], i['id']))[:6]
    selected_interests = {i['ref']: i for i in foreground}
    selected_interests.update({u.ref: memories[u.ref] for u in judge.memory_use
                              if u.ref in memories and u.ref.startswith('interest:') and u.relevant_now == 'yes'})
    touched = []
    for hit in judge.domains_touched:
        domain = domains[hit.id]
        domain_data = domain.model_dump(mode="json")
        if domain.status != "open" or not domain_data.get("soft_prior") or (character.persona and character.persona.open_style == "free"):
            domain_data.pop("soft_prior", None)
            domain_data.pop("soft_prior_evidence", None)
        touched.append({**domain_data, "strength": hit.strength,
                        "learned": getattr(domain, "learned", []),
                        "episode_counts": episode_counts(episodes, domain.id),
                        "related_signatures": [signatures[s] for s in domain.related_signatures if s in signatures],
                        "judge_relevant_signatures": [signatures[s.id] for s in model.signatures if s.id in relevant],
                        "guidance": read_prompt("open_domain") if domain.status == "open" else None})
    selected_life = {t.life_ref for t in judge.past_reference.targets}
    selected_life.update(fact["life_ref"] for fact in selected_canon + counterevidence if fact.get("life_ref") in life)
    # Explicit structural edges only (from/to/linked_to), not semantic guessing.
    for ref in list(selected_life):
        selected_life.update(life[ref][key] for key in ("from", "to", "linked_to") if life[ref].get(key) in life)
    selected_threads = {u.ref: memories[u.ref] for u in judge.memory_use if u.ref in memories and u.ref.startswith('thread:') and u.relevant_now == 'yes'}
    # Include the existing one-shot record when the judge selected its fact instead.
    selected_facts = {u.ref for u in judge.memory_use if u.ref in memories and u.relevant_now == 'yes' and u.ref.startswith('fact:') and memories[u.ref].get('private') and memories[u.ref].get('may_initiate')}
    for ref, memory in memories.items():
        if ref.startswith('thread:') and memory.get('may_initiate') and memory.get('source_ref') in selected_facts:
            selected_threads[ref] = memory
    return {
        "character_id": model.character_id, "display_name": model.display_name, "model_version": model.version,
        "summary": model.summary, "world": character.world, "expression": character.expression.model_dump(mode="json", by_alias=True),
        "feeling": feeling,
        "signatures": list(signatures.values()), "tensions": model.tensions,
        "touched_domains": touched,
        "life_history": [element for element in model.life_history.elements() if element["id"] in selected_life],
        "past_reference": judge.past_reference.model_dump(mode="json"),
        "past_guidance": read_prompt("unknown_past"),
        "current_datetime": current_datetime, "previous_session_datetime": previous_session_datetime,
        "current_utterance": current_utterance,
        "elapsed_since_last_session_seconds": (
            (datetime.fromisoformat(current_datetime["iso"]) - datetime.fromisoformat(previous_session_datetime)).total_seconds()
            if current_datetime and previous_session_datetime else None),
        "episodes": [memories[ref] for ref in episode_refs], "canon_facts": selected_canon, "counterevidence_memories": counterevidence,
        "memory_episodes": with_updates([memories[u.ref] for u in judge.memory_use if u.ref in memories and u.relevant_now == "yes" and u.ref.startswith("memory:")]),
        "monthly_digests": [memories[u.ref] for u in judge.memory_use if u.ref in memories and u.relevant_now == "yes" and u.ref.startswith("digest:")],
        "pending_correction": natural_corrections(pending_correction),
        "interests": list(selected_interests.values()), "now_summary": now_summary,
        "threads": list(selected_threads.values())[:3],
        "activities": [timed_memory(a, current) for a in list(activities)[:3]],
        "user_facts": [memories[u.ref] for u in judge.memory_use if u.ref in memories and u.relevant_now == "yes" and u.ref.startswith("fact:")],
        "user_understandings": list({u.ref: memories[u.ref] for u in judge.memory_use if u.ref in memories and u.relevant_now == 'yes' and u.ref.startswith('understanding:')}.values()),
        "unfinished_ledger": [timed_memory(p, current) for p in active_plans],
        "emotional_weight": judge.emotional_weight,
        "length_hint": character.expression.voice.length["expand_when" if judge.emotional_weight == "high" else "default"],
        "judge_warnings": [f"unknown id dropped: {w}" for w in warnings],
        "secrets": reveal_packet(character, judge, reveal_states)
                   if reveal_states is not None else [],
    }


# Our own internal identifiers. If the Actor writes any of them, it is leaking its working notes (hard rule, not semantics).
INTERNAL_MARKERS = re.compile(r"allowed_level|disclosed_level|pending_correction|major_unknowns|life_ref|Packet|"
                              r"secret_[a-z]|transition_\d|rel_(mentor|companion)|event_world_expansion|"
                              r"(?<![A-Za-z0-9_])(?:CBS|R)\d{2}(?!\d)|(?<![A-Za-z0-9_])sig_[a-z]|(?<![A-Za-z0-9_])(understanding|memory|fact|interest|canon|plan|episode|digest|thread|activity):\d")

# History lines carry "[time] text" labels for the Actor; some models copy the label into the reply.
TIME_LABEL = re.compile(r"^\s*\[\d{4}/\d{1,2}/\d{1,2}[^\]\n]{0,60}\]\s*")


def strip_time_label(text):
    return TIME_LABEL.sub("", text, count=1)


_CORRECTION_NOTES = {
    "contradicted": "前に言ったことや記録と食い違っていたかもしれない",
    "forbidden": "まだ話していない（話す段階ではない）ことや、実際にはしていないことを言ってしまったかもしれない",
}


def natural_corrections(pending):
    """Plain-language notes for the Actor: no judge reasons or internal identifiers."""
    result = []
    for item in pending or ():
        notes = []
        for claim in item.get("claims", []):
            note = {"note": _CORRECTION_NOTES.get(claim.get("verdict"), "前の返答に気になる点があったかもしれない"),
                    "how": "必要なら自然に言い直してよい。触れずに今の話を続けてもよい"}
            if claim.get("text") and not INTERNAL_MARKERS.search(claim["text"]):
                note["said"] = claim["text"]
            notes.append(note)
        # id is kept only so the store can mark the correction as consumed.
        result.append({"id": item.get("id"), "source_turn": item.get("source_turn"), "notes": notes})
    return result


def quoted_problems(problems, user_text, reply):
    """Drop Check findings that quote the user's words rather than the reply (formatting, not meaning)."""
    norm = lambda text: re.sub(r"\s+", "", text or "")
    said, answered = norm(user_text), norm(reply)
    return [p for p in problems if not (norm(p.text) and norm(p.text) in said and norm(p.text) not in answered)]


class TurnPipeline:
    def __init__(self, config, character, store, client):
        self.config, self.character, self.store, self.client = config, character, store, client
        from cbs.costs import bind
        bind(client, store, config)
        self.store.interest_rules = {"min_days": config.memory.interest_established_min_days,
                                     "min_span_days": config.memory.interest_established_min_span_days}
        self._lock = Lock()
        self._opening_lock = Lock()
        self._opening_cancel = None
        self._startup_pending = True
        self._post_future = None
        from cbs.candidates import EmbeddingWarmer
        self._embedding_warmer = EmbeddingWarmer(self)
        self._extract_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extract")
        self._faces = []
        self._expression_deliveries = {}
        self._notice_deliveries = {}
        self.store.memory_config = config.memory
        from cbs.config import embedding_model
        self.store.embedding_model = embedding_model(config)
        self.store.autonomy_config = config.autonomy
        self.store.inner_config = config.inner
        self.store.knowing_config = config.knowing
        self.store.initialize_interests(character)
        self.store.initialize_seed_memories(character)
        self.character = character.model_copy(update={'sources': self.store.author_sources(character)})

    def release_startup(self):
        """Reusable by web/resident/CLI hosts after their first foreground work.

        Explicit wait_for_steward also releases it for batch/probe callers.
        Required Extract persistence remains on the separate extraction future.
        """
        with self._opening_lock:
            pending = self._startup_pending
            self._startup_pending = False
        if pending:
            # May run from the DB worker or from a host already holding the
            # turn lock. Scheduling optional work must never reacquire it.
            self._schedule_maintenance()

    def maintenance(self, *, session_id=None, close=False, retry=True, inner_life=True):
        with self._lock:
            if self._startup_pending and not close:
                return
            self._schedule_maintenance(session_id=session_id,close=close,retry=retry,inner_life=inner_life)

    def _schedule_maintenance(self, *, session_id=None, close=False, retry=True, inner_life=True):
        self.store.costs.schedule()
        from cbs.memory import daily_maintenance, enqueue_pending
        from cbs.steward import submit
        self.store.sweep_conversations(session_id=session_id, force=close)
        enqueue_pending(self, retry=retry)
        submit(self.store, daily_maintenance, self)
        submit(self.store, self._warm_reply_embeddings)
        if inner_life:
            from cbs.autonomy import catch_up
            submit(self.store, catch_up, self)

    def proactive(self, session_id, mode="opening", *, after_turn_id=None):
        from cbs.autonomy import proactive
        cancel = Event()
        with self._opening_lock:
            if self._opening_cancel is not None:
                self._opening_cancel.set()
            self._opening_cancel = cancel
        with self._lock:
            if after_turn_id is not None:
                with self.store.connect() as db:
                    latest = db.execute('SELECT MAX(t.id) FROM turns t JOIN sessions s ON s.id=t.session_id WHERE s.character_id=?',
                                        (self.config.character.id,)).fetchone()[0] or 0
                if latest != after_turn_id:
                    cancel.set()
                    return {'speak':False, 'reason':'会話が進んだため話しかけを取りやめ'}
            if self._post_future:
                try:
                    self._post_future.result(timeout=self.config.pipeline.background_wait_seconds)
                except FutureTimeout:
                    raise LLMError('前の返答の記録がまだ保存中です。少し待って再送してください。') from None
        try:
            # Decision I/O never holds the foreground lock or waits for upkeep.
            return proactive(self, session_id, mode, cancel=cancel)
        finally:
            if not cancel.is_set() and self._post_future is None:
                self.release_startup()

    def cancel_opening(self):
        with self._opening_lock:
            if self._opening_cancel is not None:
                self._opening_cancel.set()

    def proactive_reply(self, session_id, decision, cancel):
        with self._lock:
            if cancel.is_set():
                return None
            if self._post_future:
                self._post_future.result(timeout=self.config.pipeline.background_wait_seconds)
            if decision.habit:
                habit = decision.habit
                saved = {k: v for k, v in habit.items() if k != 'local_day'}
                scheduled = clock.parse(f'{habit["local_day"]}T{habit["time"]}:00')
                if (saved not in self.store.days_state(self.config.character.id)['habits'] or
                        abs((clock.now() - scheduled).total_seconds()) > self.config.days.habit_window_minutes * 60):
                    return None
            return self._run(session_id, '', proactive_decision=decision, proactive_cancel=cancel)

    def close_conversation(self, session_id):
        self.maintenance(session_id=session_id, close=True, retry=False, inner_life=False)

    def run(self, session_id, user_text, *, background_tasks=None, event_sink=None, photos=None):
        # Local app runs one worker; serialize turns so a double-send cannot read stale history.
        self.cancel_opening()
        timeline = Timeline()
        token = current_timeline.set(timeline)
        acquired = False
        result = None
        warning = None
        try:
            with span("lock_wait"):
                self._lock.acquire()
                acquired = True
            with span("previous_extract_wait"):
                if self._post_future:
                    try:
                        self._post_future.result(timeout=self.config.pipeline.background_wait_seconds)
                    except FutureTimeout:
                        raise LLMError('前の返答の記録がまだ保存中です。少し待って再送してください。') from None
            # One extra foreground call only when photos are attached. No upkeep wait.
            from cbs.photos import describe, photo_block
            if photos and self.store.get_session(session_id)['character_id'] != self.config.character.id:
                raise ValueError('Session character mismatch')
            photo_items, vision_trace = describe(self, user_text, photos, event_sink)
            enriched_text = user_text + ''.join(photo_block(p) for p in photo_items)
            result = self._run(session_id, enriched_text, background_tasks,
                               photos=photo_items, vision_trace=vision_trace,
                               background_warning=warning, request_started=timeline.started, event_sink=event_sink)
            return result
        finally:
            timeline.mark("processing_end")
            if acquired:
                self._lock.release()
            try:
                if result:
                    self.store.merge_timeline(result['turn_id'], timeline.snapshot())
            finally:
                current_timeline.reset(token)

    def wait_for_expression(self, turn_id):
        future = self._expression_deliveries.pop(turn_id, None)
        if future:
            future.result()

    def wait_for_notice(self, turn_id):
        future = self._notice_deliveries.pop(turn_id, None)
        if future:
            maintenance = future.result()
            if maintenance:
                maintenance.result()

    def wait_for_steward(self):
        from cbs.steward import wait_for_steward
        if self._post_future:
            self._post_future.result()
        self.release_startup()
        for future in list(self._faces):
            future.result()
        wait_for_steward(self.store)

    def _run(self, session_id, user_text, background_tasks=None, background_warning=None, request_started=None, proactive_decision=None, event_sink=None, proactive_cancel=None, photos=None, vision_trace=None):
        if event_sink:
            event_sink("stage", {"stage": "thinking"})
        user_text = self.client.sanitize_text(user_text).strip()
        if not user_text and proactive_decision is None:
            raise ValueError("メッセージを入力してください。")
        started = perf_counter()
        request_started = request_started if request_started is not None else started
        session = self.store.get_session(session_id)
        if session["character_id"] != self.config.character.id:
            raise ValueError("セッションのキャラクターが設定と一致しません。")
        turn_at = clock.iso()
        face = self.store.resident_expression(self.config.character.id,
                                             self.config.autonomy.resident_expression_neutral_minutes)
        # Snapshot the face before this reply; negative revisions identify saved inner moods.
        shown_face = {'expression': face['expression'],
                      'source': 'reply_window' if face['replyRemainingMs'] is not None else
                                'inner_mood' if face['revision'] < 0 else 'neutral_fallback'}
        conversation_id = self.store.prepare_conversation(session_id, turn_at)
        self.store.fade_interests(self.config.character.id, self.config.memory.interest_fade_days)
        from cbs.memory import enqueue_pending, retrieve
        if not self._startup_pending:
            enqueue_pending(self)
        recent = [{k: v for k, v in timed_turn(t).items() if k in
                   ('id', 'user_text', 'assistant_text', 'user_time', 'assistant_time')}
                  for t in self.store.history(session_id) if t['status'] == 'completed'][-12:]
        from cbs.photos import model_turns
        recent = model_turns(self.store, recent)
        model = self.store.current_model(self.character)
        local_now = clock.now()
        current_datetime = {**clock.stamp(turn_at), "weekday": clock.WEEKDAYS[local_now.weekday()]}
        current_utterance = {"time": clock.stamp(turn_at), "now": turn_at,
                             "gap": clock.gap(self.store.previous_utterance(self.config.character.id), turn_at),
                             "gap_casual": clock.casual_gap(self.store.previous_utterance(self.config.character.id), turn_at),
                             "date_lookup": clock.calendar(turn_at)}
        previous_datetime = self.store.previous_session_datetime(session_id)
        if previous_datetime:
            previous_datetime = datetime.fromisoformat(previous_datetime).astimezone().isoformat()
        # Copy the probe clock into the retrieval worker; SQLite connections remain thread-local.
        with ThreadPoolExecutor(max_workers=2) as pool:
            # Opening has already been judged against threads; its empty user text is not an embedding query.
            retrieval = (pool.submit(lambda: []) if proactive_decision is not None else
                         pool.submit(copy_context().run, retrieve, self, user_text, recent))
            def load():
                with span('memory_read'):
                    return self._load_memories()
            loaded = pool.submit(copy_context().run, load)
            plans, episodes, canon, links, memory_episodes, interests, now_summary, corrections = loaded.result()
            retrieval_warning = None
            try:
                retrieved = retrieval.result()
            except EmbeddingModelMismatch as error:
                retrieved = []
                retrieval_warning = str(error)
            except Exception:
                retrieved = []
                retrieval_warning = '記憶検索に失敗したため、保存済みの会話・Canon・Ledger で続行しました。'
        active_plans = [plan for plan in plans if plan["state"] in ("proposed", "agreed", "rescheduled")]
        candidate_memories = [{"ref": f"canon:{fact['id']}", **fact} for fact in canon]
        # Include every active plan and recent closed plans; keep full history for Post-Check.
        memory_plans = {plan["plan_id"]: plan for plan in plans[-20:] + active_plans}
        candidate_memories += [{"ref": f"plan:{plan['plan_id']}", **plan} for plan in memory_plans.values()]
        candidate_memories += [{"ref": f"episode:{episode['id']}", **episode} for episode in episodes]
        self.store.expire_threads(self.config.character.id, self.config.autonomy.thread_expiry_days)
        self.store.sync_plan_threads(self.config.character.id)
        candidate_memories += self.store.list_threads(self.config.character.id, eligible_only=True)
        candidate_memories += retrieved
        from cbs.candidates import cap_candidates, candidate_counts
        candidate_pool_counts = candidate_counts(candidate_memories)
        if proactive_decision is None:
            with span('candidate_cap'):
                candidate_memories = cap_candidates(self, candidate_memories, user_text, recent, retrieved)
        candidate_memories = [timed_memory(m) for m in candidate_memories]
        request_state = {**self.store.current_requests(self.character), 'limits': self.config.requests.model_dump()}
        context = {"character_summary": model.summary,
                   "domains": [{"id": d.id, "label": d.label, "status": d.status,
                                "description": " ".join((d.known if d.status != "open" and d.known else getattr(d, "description", "") or "").split())[:160]} for d in model.domains],
                   "signatures": [{"id": s.id, "when": s.when} for s in model.signatures],
                   "life_history_elements": [{"id": e['id'], "description": ' / '.join(str(e[k]) for k in
                       ('kind', 'function', 'known', 'tone_now', 'meaning_then', 'meaning_now') if e.get(k))[:160]} for e in model.life_history.elements()],
                   "user_text": user_text, "recent_turns": recent,
                   "current_datetime": current_datetime, "previous_session_datetime": previous_datetime,
                   "current_utterance": current_utterance,
                   "unfinished_ledger": active_plans, "candidate_memories": candidate_memories}
        context["secrets"] = [{"id": s.id, "about": s.about} for s in self.character.reveal.secrets]
        context['private_facts_policy'] = self.store.private_context(self.config.character.id)
        context['partner_requests'] = request_state
        timeline = current_timeline.get() or Timeline()
        timeline.mark("load", started)
        trace = {"Timeline": timeline.snapshot(), "milestone": "M8", "Load": {"input": {"session_id": session_id, "conversation_id": conversation_id, "recent_turn_limit": 12},
                 "output": context, "latency_ms": round((perf_counter() - started) * 1000, 2)}}
        if vision_trace:
            trace['Vision'] = vision_trace
        if retrieval_warning:
            trace["Load"]["warnings"] = [retrieval_warning]
        if background_warning:
            trace["Load"].setdefault("warnings", []).append(background_warning)
        trace['Load']['candidate_counts'] = {'before':candidate_pool_counts, 'after':candidate_counts(candidate_memories)}
        step = "Pre-Judge"
        step_started = perf_counter()
        try:
            from cbs.judge_input import prejudge_input
            judge_system, judge_context = prejudge_input(self.character, context)
            judge_system += prompt_context(request_state)
            trace[step] = {"request": {"model": self.config.models.judge, "system": judge_system,
                           "messages": [{"role": "user", "content": json.dumps(judge_context, ensure_ascii=False)}], "schema": PreJudge.model_json_schema()}}
            judge_request = trace[step]['request']
            degraded = False
            for attempt in range(2):
                attempt_step = 'Pre-Judge' if attempt == 0 else 'Pre-Judge-Retry'
                trace[attempt_step] = {'request': judge_request}
                try:
                    if proactive_decision is not None:
                        from cbs.llm import LLMResult
                        forced = PreJudge(memory_use=[MemoryUse(ref=proactive_decision.thread_ref, relevant_now="yes", counterevidence="no", continuation="yes")] if proactive_decision.thread_ref else [])
                        judgment = LLMResult(forced.model_dump_json(), forced, {}, 0, self.config.models.judge, {})
                    else:
                        judgment = for_step(self.client, 'pre_judge', 'chat').generate_json(self.config.models.judge, judge_system,
                                    judge_request['messages'], PreJudge)
                    trace[attempt_step] = judgment.trace()
                    judge = PreJudge.model_validate(judgment.parsed)
                    break
                except Exception as judge_error:
                    trace[attempt_step] = getattr(judge_error, "trace", None) or trace[attempt_step]
                    trace[attempt_step]["error"] = "返答前の判断に失敗しました。"
                    trace[attempt_step].setdefault("latency_ms", round((perf_counter() - step_started) * 1000, 2))
            else:
                degraded = True
                judge = PreJudge(emotional_weight='low', reveal_request=None, domains_touched=[], memory_use=[])
                trace['Pre-Judge-Retry']['fallback'] = 'conservative: L0, no Open, recent turns only'
            timeline.mark("Pre-Judge", step_started)
            from cbs.reveal import generate_truth, secret_states
            if judge.reveal_request:
                if judge.reveal_request.secret_id not in {s.id for s in self.character.reveal.secrets}:
                    trace["Pre-Judge"].setdefault("warnings", []).append("unknown reveal secret_id dropped: " + judge.reveal_request.secret_id)
                    judge = judge.model_copy(update={"reveal_request": None})
                elif not any(s["secret_id"] == judge.reveal_request.secret_id for s in self.store.list_secrets(self.config.character.id)):
                    with span("Reveal Generator"):
                        trace["Reveal Generator"] = generate_truth(self.character, self.store, self.client, self.config, judge.reveal_request.secret_id)
            states = secret_states(self.character, self.store, spoilers=True,
                                   requested_id=judge.reveal_request.secret_id if judge.reveal_request else None,
                                   manner=judge.reveal_request.manner if judge.reveal_request else None)
            if degraded:
                states = {**states, 'secrets': [{**s, 'allowed_level': 0} for s in states['secrets']]}
            step = "Packet"
            step_started = perf_counter()
            started = perf_counter()
            trace[step] = {"input": judge.model_dump(mode="json")}
            packet = assemble_packet(effective_character(self.character, request_state), judge, current_datetime=current_datetime,
                                     previous_session_datetime=previous_datetime, candidate_memories=candidate_memories,
                                     active_plans=active_plans, model=model, episodes=episodes, reveal_states=states,
                                     episode_links=links, memory_episodes=memory_episodes,
                                     pending_correction=corrections, interests=interests, now_summary=now_summary,
                                     activities=self.store.conversation_activities(self.config.character.id))
            from cbs.look import look_meta, author_look
            from cbs.note import latest_diary
            if photos:
                packet['shared_photos'] = [{'id': p['id'], 'description': p['description']} for p in photos]
            from cbs.days import days_context
            packet['important_days'] = days_context(self.store, self.config.character.id, self.config.days, user_reply=proactive_decision is None)
            packet['partner_requests'] = request_state
            packet['shared_diary'] = latest_diary(self.store, self.config.character.id)
            packet['private_facts_policy'] = context['private_facts_policy']
            packet['reading_progress'] = self.store.list_books(self.config.character.id)
            packet['feeling'] = self.store.current_feeling(self.config.character.id)
            packet['shown_face'] = shown_face
            packet['world_facts'] = self.store.list_world_facts(self.config.character.id)
            packet["author_look"] = author_look(look_meta(getattr(self, 'characters_root', None), self.config.character.id))
            packet["current_utterance"] = current_utterance
            if degraded:
                packet['judgment_degraded'] = True
                for key in ('secrets', 'touched_domains', 'life_history', 'episodes', 'canon_facts',
                            'counterevidence_memories', 'memory_episodes', 'monthly_digests',
                            'user_facts', 'user_understandings', 'threads', 'interests', 'activities',
                            'unfinished_ledger', 'reading_progress', 'world_facts',
                            'private_facts_policy', 'pending_correction'):
                    packet[key] = []
                packet['now_summary'] = None
            if proactive_decision is not None:
                packet["proactive"] = proactive_decision.model_dump(mode="json")
                packet['proactive']['want'] = next((t for t in candidate_memories if t['ref'] == proactive_decision.want_ref), None)
            trace[step].update(output=packet, latency_ms=round((perf_counter() - started) * 1000, 2))
            timeline.mark("Packet", started)
            step = "Actor"
            step_started = perf_counter()
            messages = [{"role": "user", "content": json.dumps({"packet": packet}, ensure_ascii=False)}]
            for turn in recent:
                messages.extend([{"role": "user", "content": f"[{turn['user_time']['absolute']}] {turn['user_text']}"},
                                 {"role": "assistant", "content": f"[{turn['assistant_time']['absolute']}] {turn['assistant_text']}"}])
            messages.append({"role": "user", "content": f"[{current_datetime['absolute']}; {current_utterance['gap'] or '最初の発話'}] {user_text}"})
            ai_question = effective_persona(self.character, self.config).ai_question
            system = read_prompt("actor") + "\n\n" + read_prompt(f"ai_question_{ai_question}") + character_prefix(self.character, actor=True) + prompt_context(request_state)
            trace[step] = {"request": {"model": self.config.models.actor, "system": system, "messages": messages}}
            response = self._actor_text(system, messages, trace, step, event_sink)
            timeline.mark("Actor", step_started)
            trace[step] = {**response.trace(), **({"leak_retry": True} if trace.get(step, {}).get("leak_retry") else {})}
            if not response.text.strip():
                raise LLMError("Actor が空の返答を返しました。")
            reply = strip_time_label(self.client.sanitize_text(response.text))
            assistant_at = clock.iso()
            post_context = {"user_text": user_text, "recent_turns": recent, "packet": packet,
                            "current_datetime": current_datetime,
                            "current_utterance": current_utterance, "assistant_time": clock.stamp(assistant_at),
                            "ai_question": ai_question,
                            "private_facts_policy": context['private_facts_policy'],
                            "canon_facts": [], "plans": [],
                            "secrets": [s for s in states['secrets'] if degraded or s['secret_id'] in
                                        {s['secret_id'] for s in packet['secrets']}]}
            # Public text must pass Check, including legacy async configurations.
            if event_sink:
                event_sink("stage", {"stage": "checking"})
            from cbs.expression import start_expression, finish_expression
            face_future = start_expression(self, user_text, reply)
            face_step = "Expression"
            original_face = None
            draft = reply
            step, step_started = 'Check', perf_counter()
            reply, check = self._checked_reply(trace, post_context, reply, system, messages)
            if reply != draft:
                assistant_at = clock.iso()
                post_context['assistant_time'] = clock.stamp(assistant_at)
                original_face = face_future
                face_future = start_expression(self, user_text, reply)
                face_step = 'Expression-2'
        except Exception as error:
            if proactive_cancel is not None and proactive_cancel.is_set():
                return None
            from cbs.telemetry import record_error
            record_error(self.store, 'autonomy' if proactive_decision is not None else 'reply', error)
            if trace.get('Safety', {}).get('blocked'):
                step = 'Safety'
            timeline.mark(step + '.failed', step_started, state='failed')
            if isinstance(error, LLMError) and error.trace is not None:
                previous = trace[step]
                trace[step] = {**previous, **error.trace}
                if "pre_rewrite_text" in previous:
                    trace[step]["pre_rewrite_text"] = previous["pre_rewrite_text"]
            if isinstance(error, LLMError):
                trace[step].update(error.metadata())
            trace[step]["error"] = "このステップに失敗しました。"
            trace[step].setdefault("latency_ms", round((perf_counter() - step_started) * 1000, 2))
            trace["Commit"] = {"input": {"session_id": session_id, "status": "failed"}}
            trace['Timeline'] = timeline.snapshot()
            save_started = perf_counter()
            failed_turn = self.store.add_turn(session_id, user_text, None, self._safe_trace(trace), status="failed",
                                             conversation_id=conversation_id, created_at=turn_at, photos=photos)
            timeline.mark('save', save_started)
            timeline.mark('processing_end')
            self.store.merge_timeline(failed_turn, timeline.snapshot())
            details = error.metadata() if isinstance(error, LLMError) else {}
            raise LLMError("返事を用意できませんでした。もう一度送ってください。",
                           kind=details.get("error_kind", "unknown"), provider=details.get("provider"),
                           status_code=details.get("status_code")) from None
        if proactive_cancel is not None and proactive_cancel.is_set():
            return None
        updates = None
        # SSE can publish the checked text before waiting for its face. The
        # synchronous JSON API continues to return the completed expression.
        face_pending = bool(event_sink and not face_future.done())
        expression = "thinking" if face_pending else finish_expression(face_future, trace, face_step)
        if face_pending:
            trace[face_step] = {"state": "pending"}
        if original_face is not None:
            if not event_sink or original_face.done():
                finish_expression(original_face, trace)
            else:
                trace["Expression"] = {"state": "superseded"}
        trace["Extract"] = {"state": "pending"}
        trace["Commit"] = {"input": {"session_id": session_id, "user_text": user_text, "assistant_text": reply,
                                     "status": "completed", "post_check": updates}}
        trigger = any(hit.strength == "direct" for hit in judge.domains_touched) or judge.emotional_weight == "high"
        if trigger:
            trace["Steward"] = {"state": "pending"}
        trace["Actor"]["reply_latency_ms"] = round((perf_counter() - request_started) * 1000, 2)
        trace["Timeline"] = timeline.snapshot()
        safe_trace = self._safe_trace(trace)
        save_started = perf_counter()
        with self._opening_lock:
            if proactive_cancel is not None and proactive_cancel.is_set():
                return None
            turn_id = self.store.add_turn(session_id, user_text, reply, safe_trace,
                                          post_check=safe_trace["Commit"]["input"]["post_check"],
                                          conversation_id=conversation_id, created_at=turn_at, assistant_at=assistant_at,
                                          kind="proactive" if proactive_decision is not None else "conversation", expression=expression, photos=photos)
        from cbs.photos import public_photos
        response_body = {"turn_id": turn_id, "session_id": session_id, "assistant_text": reply,
                         "expression": expression, "expression_pending": face_pending, "assistant_at": assistant_at}
        if photos:
            response_body['photos'] = public_photos(self.store, turn_id)
        timeline.mark("save", save_started)
        if event_sink:
            event_sink("reply", response_body.copy())
        timeline.mark("reply_sent")
        if face_pending:
            # A callback runs on the expression worker, outside the turn lock
            # and the serial memory-maintenance queue.
            def face_done(future):
                details = {}
                value = finish_expression(future, details, face_step, timeout=None)
                timeline.mark("expression_delivered")
                self.store.finish_expression(turn_id, value, self._safe_trace(details))
                self.store.merge_timeline(turn_id, timeline.snapshot())
                event_sink("expression", {"turn_id": turn_id, "expression": value})
            from concurrent.futures import Future
            delivery = Future()
            self._faces = [f for f in self._faces if not f.done()] + [delivery]
            def deliver(future):
                try:
                    face_done(future)
                    delivery.set_result(None)
                except Exception as error:
                    delivery.set_exception(error)
            self._expression_deliveries[turn_id] = delivery
            face_future.add_done_callback(deliver)
        from cbs.steward import submit
        from threading import Event
        gate = Event()
        if background_tasks is not None:
            background_tasks.add_task(gate.set)
        else:
            gate.set()
        self._post_future = self._extract_pool.submit(copy_context().run, self._finish_reply, turn_id, judge, post_context,
                                   reply, trigger, gate, event_sink)
        if event_sink:
            self._notice_deliveries[turn_id] = self._post_future
        if not self._startup_pending:
            enqueue_pending(self)
        self.store.merge_timeline(turn_id, timeline.snapshot())
        return response_body

    def _finish_reply(self, turn_id, judge, context, reply, trigger, gate, event_sink=None):
        with span("response_gate_wait"):
            gate.wait(timeout=self.config.pipeline.background_wait_seconds)
        timeline = current_timeline.get()
        if timeline:
            timeline.mark("extract_start")
        trace = self.store.get_trace(turn_id)
        final_check = trace.get(trace.get('Safety', {}).get('accepted_check', 'Check-2'), trace.get("Check", {}))
        check_result = (final_check.get("response") or {}).get("parsed") if not final_check.get("error") else None
        extract = self._judge_reply(trace, "Extract", {**context, "check_result": check_result}, reply, Extract)
        self.store.finish_post_check(turn_id, self._safe_trace(trace),
                                     self._safe_trace(extract.model_dump(mode="json")) if extract else None,
                                     [], request_character=self.character, request_config=self.config.requests,
                                     request_version=context['packet']['partner_requests']['version'],
                                     days_context=context['packet'].get('important_days'))
        notices = self.store.request_notices(self.config.character.id, turn_id).get(turn_id, [])
        if notices and event_sink:
            event_sink('partner_requests', {'turn_id': turn_id, 'notices': notices})
        day_notices = self.store.day_notices(self.config.character.id, turn_id).get(turn_id, [])
        if day_notices and event_sink:
            event_sink('important_days', {'turn_id': turn_id, 'notices': day_notices})
        if timeline:
            timeline.mark("extract_saved")
            self.store.merge_timeline(turn_id, timeline.snapshot())
        from cbs.steward import submit
        return submit(self.store, self._after_extract, turn_id, judge, trigger, event_sink)

    def _after_extract(self, turn_id, judge, trigger, event_sink=None):
        self.release_startup()
        self._warm_reply_embeddings()
        from cbs.note import fill_past_notes
        fill_past_notes(self)
        self.store.sync_plan_threads(self.config.character.id)
        if trigger:
            from cbs.steward import run_steward
            run_steward(self, turn_id, judge)
        if event_sink and self.config.early.notify and self.store.forming_notice(turn_id):
            event_sink('self_forming', {'turn_id': turn_id})
        if timeline := current_timeline.get():
            timeline.mark("maintenance_end")
            self.store.merge_timeline(turn_id, timeline.snapshot())

    def _warm_reply_embeddings(self):
        """Schedule optional indexing without occupying the DB/Extract worker."""
        self._embedding_warmer.request(refresh=True)

    def _checked_reply(self, trace, context, reply, system, messages):
        """Bounded recovery; no candidate is published without a successful Check."""
        if 'partner_requests' not in context['packet']:
            context['packet']['partner_requests'] = self.store.current_requests(self.character)
        check = self._judge_reply(trace, 'Check', context, reply, Check)
        if reply.strip() and check is not None and not check.problems:
            return reply, check
        safety = trace['Safety'] = {'path': [], 'rewrite_count': 0}

        def accept(text, result, name):
            ok = bool(text.strip()) and result is not None and not result.problems
            safety['path'].append({'step': name, 'accepted': ok})
            if ok:
                safety['accepted_check'] = name
            return ok

        def actor(name, instruction, findings):
            # Check may contain undisclosed truth. Never forward its reasons or
            # quotes when any secret has a truth; only a generic L0-safe rule.
            corrections = ([{'verdict': p.verdict, 'reason': read_prompt('rewrite_reveal')} for p in findings]
                           if any(s.get('truth') for s in context['secrets']) else
                           [p.model_dump(mode='json') for p in findings])
            current = messages + [{'role': 'assistant', 'content': reply},
                {'role': 'user', 'content': instruction + '\n\n' + json.dumps(corrections, ensure_ascii=False)}]
            trace[name] = {'pre_rewrite_text': reply, 'request': {'model': self.config.models.actor,
                            'system': system, 'messages': current}}
            started = perf_counter()
            try:
                result = self._actor_text(system, current, trace, name)
                trace[name] = {**result.trace(), 'pre_rewrite_text': reply}
                text = strip_time_label(self.client.sanitize_text(result.text)).strip()
                if not text:
                    raise LLMError('空の返答です。')
                return text
            except Exception as error:
                trace[name] = {**(getattr(error, 'trace', None) or trace[name]),
                               'pre_rewrite_text': reply, 'error': '返答の再生成に失敗しました。'}
                return None
            finally:
                trace[name].setdefault('latency_ms', round((perf_counter()-started)*1000, 2))
                if timeline := current_timeline.get():
                    timeline.mark(name, started)

        findings = list(check.problems) if check is not None else []
        for number in (1, 2):
            name, check_name = ('Rewrite', 'Check-2') if number == 1 else ('Rewrite-2', 'Check-3')
            safety['rewrite_count'] = number
            candidate = actor(name, read_prompt('rewrite'), findings)
            safety['path'].append({'step': name, 'generated': candidate is not None})
            if candidate is not None:
                reply = candidate
                check = self._judge_reply(trace, check_name, context, reply, Check)
                if accept(reply, check, check_name):
                    return reply, check
                findings.extend(check.problems if check is not None else [])
            if number == 1:
                # Formatting only: no sentence splitting, normalization, or
                # inference about what survives. The next Check decides that.
                trimmed = reply
                removed = []
                for text in sorted({p.text for p in findings if p.text}, key=len, reverse=True):
                    if text in trimmed:
                        trimmed = trimmed.replace(text, '')
                        removed.append(text)
                trimmed = trimmed.strip()
                trace['Remove-Quoted'] = {'removed': removed, 'text': trimmed}
                if removed and trimmed:
                    result = self._judge_reply(trace, 'Check-Trimmed', context, trimmed, Check)
                    if accept(trimmed, result, 'Check-Trimmed'):
                        return trimmed, result
                    findings.extend(result.problems if result is not None else [])
                else:
                    safety['path'].append({'step': 'Check-Trimmed', 'accepted': False, 'skipped': 'empty or no exact quote'})
        candidate = actor('Deflection', read_prompt('deflection'), [])
        if candidate is not None:
            result = self._judge_reply(trace, 'Check-Deflection', context, candidate, Check)
            if accept(candidate, result, 'Check-Deflection'):
                return candidate, result
        safety['blocked'] = True
        # Persist a failed turn with no assistant text. Never expose any draft.
        recovery_steps = {'Check', 'Check-2', 'Check-3', 'Check-Trimmed', 'Check-Deflection',
                          'Rewrite', 'Rewrite-2', 'Deflection'}
        failed = next((item for name, item in reversed(list(trace.items()))
                       if name in recovery_steps and item.get('error_kind') is not None), {})
        raise LLMError('返答の確認を完了できませんでした。もう一度送ってください。',
                       kind=failed.get('error_kind', 'unknown'), provider=failed.get('provider'),
                       status_code=failed.get('status_code'))

    def _judge_reply(self, trace, step, context, reply, schema):
        started = perf_counter()
        from cbs.memory import reply_candidates
        retrieval_warning = None
        candidate_reuse = False
        try:
            cached = context.get('_reply_candidates')
            if cached is not None and cached[0] == reply:
                candidate_reuse = True
                facts, plans = cached[1:]
            else:
                with span(step + ".retrieval"):
                    facts, plans = reply_candidates(self, reply, context['packet'])
                context['_reply_candidates'] = (reply, facts, plans)
            context = {**context, 'canon_facts': facts, 'plans': plans}
        except EmbeddingModelMismatch as error:
            context = {**context, 'canon_facts': context['packet']['canon_facts'],
                       'plans': context['packet']['unfinished_ledger']}
            retrieval_warning = str(error)
        except Exception:
            context = {**context, 'canon_facts': context['packet']['canon_facts'],
                       'plans': context['packet']['unfinished_ledger']}
            retrieval_warning = '返答の埋め込み検索に失敗。Packet の根拠で検査します。'
        # Stable instructions/world/expression precede all turn-specific data.
        system = (read_prompt("check" if schema is Check else "extract") + character_prefix(self.character)
                  + prompt_context(context['packet']['partner_requests']))
        retrieval_ms = (perf_counter() - started)*1000
        context = {"ai_question": context["ai_question"], **{k: v for k, v in context.items() if k != "_reply_candidates"}}
        messages = [{"role": "user", "content": json.dumps({**context, "assistant_text": reply}, ensure_ascii=False)}]
        trace[step] = {"request": {"model": self.config.models.judge, "system": system,
                                  "messages": messages, "schema": schema.model_json_schema()}}
        try:
            with span(step + ".llm"):
                result = for_step(self.client, step, 'chat' if schema is Check else 'memory').generate_json(self.config.models.judge, system, messages, schema)
            trace[step] = result.trace()
            output = schema.model_validate(result.parsed)
            if schema is Check:
                kept = quoted_problems(output.problems, context['user_text'], reply)
                if len(kept) != len(output.problems):
                    trace[step]['ignored'] = 'ユーザーの発言だけを引用した指摘を除外しました。'
                output = output.model_copy(update={'problems': kept})
            trace[step]["response"]["parsed"] = output.model_dump(mode="json")
            if retrieval_warning:
                trace[step].setdefault('warnings', []).append(retrieval_warning)
            trace[step]["retrieval_latency_ms"] = round(retrieval_ms, 2)
            trace[step]['candidate_reuse'] = candidate_reuse
            trace[step]["state"] = "completed"
            trace[step]["total_latency_ms"] = round((perf_counter() - started) * 1000, 2)
            return output
        except Exception as error:
            trace[step] = getattr(error, "trace", None) or trace[step]
            trace[step]["error"] = ("Check に失敗しました。この文は公開せず復旧経路へ進みます。" if schema is Check else
                                    "Extract に失敗しました。会話を続け、今回の記録用抽出の保存をスキップします。")
            trace[step]["warnings"] = [trace[step]["error"]]
            trace[step]["state"] = "failed"
            if retrieval_warning:
                trace[step]['warnings'].append(retrieval_warning)
            trace[step].setdefault("latency_ms", round((perf_counter() - started) * 1000, 2))
            return None

    def _actor_text(self, system, messages, trace, step, event_sink=None):
        """Only the initial Actor may retry a leak; recovery calls are single attempts."""
        def generate(current):
            # Streaming unverified drafts would bypass the publication gate.
            response = for_step(self.client, step, 'chat').generate_text(self.config.models.actor, system, current)
            from cbs.display_text import script_mix
            if script_mix(response.text):
                # Diagnostic only. Actor publication/recovery remains Check's job.
                trace.setdefault('script_mix', []).append({'step': step, 'detected': True})
            return response
        response = generate(messages)
        if not INTERNAL_MARKERS.search(response.text or ""):
            return response
        if step != 'Actor':
            raise LLMError('返答に内部の情報が混ざったため、表示を取りやめました。もう一度送ってください。', trace=response.trace())
        trace.setdefault(step, {})["leak_retry"] = True
        retry = messages + [{"role": "assistant", "content": response.text},
                            {"role": "user", "content": "（今の出力には内部のメモが混ざっていた。メモや設定の説明は一切書かず、パートナーとしての返答本文だけをもう一度書いて）"}]
        response = generate(retry)
        if INTERNAL_MARKERS.search(response.text or ""):
            raise LLMError("Actor の返答に内部情報が含まれていました。", trace={**response.trace(), "leak_retry": True, "error": "internal notes leaked twice"})
        return response

    def _safe_trace(self, trace):
        return json.loads(self.client.sanitize_text(json.dumps(trace, ensure_ascii=False)))

    def _load_memories(self):
        store, cid = self.store, self.config.character.id
        return (store.list_plans(cid), store.list_episodes(cid), store.list_canon(cid),
                store.list_episode_links(cid), store.list_memory_episodes(cid), store.list_interests(cid),
                store.now_summary(cid), store.pending_corrections(cid))
