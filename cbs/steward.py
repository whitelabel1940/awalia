"""Steward owns meaning; this module validates references and applies P4 rules."""
from cbs.llm.cost import for_step
from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import copy_context
import json
from threading import Lock
from time import perf_counter
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from cbs.character import Signature, Status
from cbs.runtime import read_prompt, character_prefix
from cbs.store import now
from cbs import clock


class StewardField(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Episode(StewardField):
    domain: str
    mode: Literal["enacted", "hypothetical"]
    situation: str = Field(min_length=1)
    reaction: str = Field(min_length=1)
    salient_conditions: list[str]
    # Meaning of repetition is decided by Steward, never by keyword comparison.
    repeat_of: str | None = None


class NewSignature(StewardField):
    id: str
    when: str = Field(min_length=1)
    tends_to: str = Field(min_length=1)


class Update(StewardField):
    target: str
    interpretation: Literal["first_observation", "reinforcement", "differentiation", "change", "unresolved"]
    proposed_status: Status
    proposed_known: str = Field(min_length=1)
    display_text: str = Field(default='', max_length=300)
    conditions: str = ""
    episode_refs: list[str]
    rationale: str
    opposing_episode_refs: list[str] = Field(default_factory=list)
    signature: NewSignature | None = None


class StewardOutput(StewardField):
    llm_role: ClassVar[str] = "steward"
    episodes: list[Episode] = Field(max_length=2)
    updates: list[Update]


_queues = {}
_queue_lock = Lock()


def submit(store, function, *args):
    key = str(store.path.resolve()).casefold()
    with _queue_lock:
        if key not in _queues:
            _queues[key] = (ThreadPoolExecutor(max_workers=1, thread_name_prefix="steward"), [])
        executor, futures = _queues[key]
        futures[:] = [future for future in futures if not future.done()]
        future = executor.submit(copy_context().run, function, *args)
        futures.append(future)
        return future


def enqueue(pipeline, turn_id, judge):
    submit(pipeline.store, run_steward, pipeline, turn_id, judge)


def wait_for_steward(store):
    key = str(store.path.resolve()).casefold()
    # A completed foreground/extract job can enqueue deferred startup work.
    # Drain descendants too; a single snapshot can return before they finish.
    while True:
        with _queue_lock:
            futures = [f for f in _queues.get(key, (None, []))[1] if not f.done()]
        if not futures:
            return
        wait(futures)


def episode_counts(episodes, domain=None):
    items = [e for e in episodes if domain is None or e["domain"] == domain]
    counts = {mode: len({(e.get("conversation_id") or e["session_id"], e.get("repeat_of") or e["id"]) for e in items if e["mode"] == mode})
              for mode in ("enacted", "hypothetical")}
    counts["enacted_sessions"] = len({e.get("conversation_id") or e["session_id"] for e in items if e["mode"] == "enacted" and e["session_id"]})
    counts["enacted_conversations"] = counts["enacted_sessions"]
    return counts


def apply_output(store, character, turn_id, output, input_context=None, llm_trace=None, *, notify=True):
    """Save episodes, proposals, versions, and final trace in one transaction."""
    model = store.current_model(character)
    character_id = model.character_id
    domains = {d.id: d.model_dump(mode="json") for d in model.domains}
    signatures = {s.id: s.model_dump(mode="json") for s in model.signatures}
    existing = {f"episode:{e['id']}": e for e in store.list_episodes(character_id)}
    application = {"episodes": [], "applied": [], "rejected": []}
    with store.connect() as connection:
        turn = connection.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        session_id = turn["session_id"]
        conversation_id = turn["conversation_id"]
        refs = dict(existing)
        saved_domains = set()
        for index, episode in enumerate(output.episodes):
            data = episode.model_dump(mode="json")
            repeated = existing.get(episode.repeat_of) if episode.repeat_of else None
            if episode.domain in saved_domains or episode.domain not in domains or (episode.repeat_of and
                    (not repeated or repeated.get("conversation_id") != conversation_id or repeated["domain"] != episode.domain or repeated["mode"] != episode.mode)):
                application["rejected"].append({"episode": data, "reason": "duplicate/unknown domain or invalid same-conversation repeat_of"})
                continue
            root = (repeated.get("repeat_of") or repeated["id"]) if repeated else None
            row = connection.execute("INSERT INTO episodes (character_id, domain, mode, session_id, situation, reaction, "
                                     "salient_conditions_json, source_turn, created_at, repeat_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                     (character_id, episode.domain, episode.mode, session_id, episode.situation, episode.reaction,
                                      json.dumps(episode.salient_conditions, ensure_ascii=False), turn_id, now(), root))
            saved = {**data, "id": row.lastrowid, "session_id": session_id, "source_turn": turn_id, "repeat_of": root}
            connection.execute("UPDATE episodes SET conversation_id=? WHERE id=?", (conversation_id, row.lastrowid))
            saved["conversation_id"] = conversation_id
            refs[f"new:{index}"] = saved
            refs[f"episode:{saved['id']}"] = saved
            application["episodes"].append(saved)
            saved_domains.add(episode.domain)
        for proposal in output.updates:
            data = proposal.model_dump(mode="json")
            reason = None
            collection = "domains" if proposal.target in domains else "signatures"
            target = domains.get(proposal.target) or signatures.get(proposal.target)
            new_signature = proposal.target == "new_signature"
            evidence = [refs[r] for r in dict.fromkeys(proposal.episode_refs) if r in refs]
            opposing = [refs[r] for r in dict.fromkeys(proposal.opposing_episode_refs) if r in refs]
            if len(evidence) != len(set(proposal.episode_refs)) or not evidence:
                reason = "episode_refs must resolve to saved episodes"
            elif target and collection == "domains" and any(e["domain"] != proposal.target for e in evidence + opposing):
                reason = "episode domain does not match target"
            counts = episode_counts(evidence)
            if new_signature:
                if not proposal.signature or not proposal.signature.id.startswith("sig_") or proposal.signature.id in signatures:
                    reason = "new signature requires a unique sig_ id, when and tends_to"
                elif proposal.proposed_status != "tentative" or len({e.get("conversation_id") or e["session_id"] for e in evidence if e["session_id"]}) < 2:
                    reason = "new_signature requires tentative and episodes from 2 conversations"
            elif target is None:
                reason = "unknown target"
            elif not reason:
                old, new = target["status"], proposal.proposed_status
                if new == "contested" and old != "open":
                    if (proposal.interpretation == "differentiation" or
                            len(opposing) != len(set(proposal.opposing_episode_refs)) or
                            not set(proposal.opposing_episode_refs) <= set(proposal.episode_refs) or
                            episode_counts(opposing)["enacted"] < 2):
                        reason = "contested requires 2 opposing enacted observations, not differentiation"
                elif old == new and proposal.interpretation == "reinforcement":
                    # Same tendency seen again: the episode itself is the record; do not grow `learned` with near-duplicates.
                    reason = "reinforcement keeps the model unchanged (episode recorded)"
                elif old == new:
                    pass
                elif old == "open" and new == "tentative":
                    pass
                elif old == "tentative" and new == "supported":
                    if counts["enacted"] < 2 or counts["enacted_conversations"] < 2:
                        reason = "supported requires 2 enacted observations in different conversations"
                elif old == "supported" and new == "well_supported":
                    if counts["enacted"] < 3 or counts["enacted_conversations"] < 2:
                        reason = "well_supported requires 3 enacted observations across 2 conversations"
                else:
                    reason = "transition is not permitted by P4"
            if reason:
                application["rejected"].append({"proposal": data, "reason": reason})
                continue
            ids = [f"episode:{e['id']}" for e in evidence]
            if new_signature:
                target_id = proposal.signature.id
                after = Signature(**proposal.signature.model_dump(), status="tentative", origin="acquired", evidence=ids,
                                  learned=[{"text": proposal.proposed_known, "conditions": proposal.conditions, "episode_refs": ids}]).model_dump(mode="json")
                before = None
            else:
                target_id, before = proposal.target, dict(target)
                after = {"status": proposal.proposed_status}
                author = any(ref.startswith("author:") for ref in target.get("evidence", []))
                if collection == "domains" and target["status"] in ("open", "tentative") and not author:
                    after["known"] = proposal.proposed_known
                else:
                    addition = {"text": proposal.proposed_known, "conditions": proposal.conditions, "episode_refs": ids}
                    after["learned"] = [*target.get("learned", []), addition]
            change = {"collection": collection, "target": target_id, "before": before, "after": after,
                      "interpretation": proposal.interpretation, "known": proposal.proposed_known,
                      "conditions": proposal.conditions}
            if proposal.display_text.strip():
                change['display_text'] = proposal.display_text.strip()
            row = connection.execute("INSERT INTO model_versions (character_id, base_version, diff_json, rationale, episode_refs_json, created_at) "
                                     "VALUES (?, ?, ?, ?, ?, ?)", (character_id, model.version,
                                     json.dumps({"changes": [change], "source_turn": turn_id, "session_id": session_id}, ensure_ascii=False),
                                     proposal.rationale, json.dumps(ids), now()))
            application["applied"].append({"version": row.lastrowid, "proposal": data, **change})
            (domains if collection == "domains" else signatures)[target_id] = {**(before or {}), **after}
        _save_run(connection, turn_id, input_context or {}, output.model_dump(mode="json"), application, llm_trace or {})
    return application


def _save_run(connection, turn_id, context, output, application, trace):
    connection.execute("INSERT INTO steward_runs (source_turn, input_json, output_json, application_json, created_at) VALUES (?, ?, ?, ?, ?)",
                       (turn_id, json.dumps(context, ensure_ascii=False), json.dumps(output, ensure_ascii=False), json.dumps(application, ensure_ascii=False), now()))
    turn_trace = json.loads(connection.execute("SELECT trace_json FROM turns WHERE id=?", (turn_id,)).fetchone()[0])
    turn_trace["Steward"] = {**trace, "state": "completed" if not application.get("error") else "failed", "output": application}
    if application.get("error"):
        turn_trace["Steward"]["warnings"] = [application["error"]]
    connection.execute("UPDATE turns SET trace_json=? WHERE id=?", (json.dumps(turn_trace, ensure_ascii=False), turn_id))


def run_steward(pipeline, turn_id, judge):
    started, context, trace = perf_counter(), {}, {}
    try:
        model = pipeline.store.current_model(pipeline.character)
        with pipeline.store.connect() as connection:
            turn = dict(connection.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone())
        episodes = pipeline.store.list_episodes(model.character_id)
        touched = {d.id for d in judge.domains_touched}
        context = {"user_text": turn["user_text"], "assistant_text": turn["assistant_text"], "session_id": turn["session_id"],
                   "conversation_id": turn["conversation_id"],
                   "summary": model.summary, "domains": [d.model_dump(mode="json") for d in model.domains if d.id in touched],
                   "signatures": [s.model_dump(mode="json") for s in model.signatures], "episodes": episodes,
                   "pre_judge": judge.model_dump(mode="json")}
        from cbs.memory import timed_turn
        context["turn"] = timed_turn({k: turn[k] for k in ("id", "created_at", "assistant_at", "user_text", "assistant_text")})
        messages = [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        trace = {"request": {"model": pipeline.config.models.steward, "system": (read_prompt("steward") + character_prefix(pipeline.character, actor=True, store=pipeline.store)), "messages": messages,
                             "schema": StewardOutput.model_json_schema()}}
        result = for_step(pipeline.client, 'steward', 'memory').generate_json(pipeline.config.models.steward, (read_prompt("steward") + character_prefix(pipeline.character, actor=True, store=pipeline.store)), messages, StewardOutput)
        trace = pipeline._safe_trace(result.trace())
        output = StewardOutput.model_validate_json(pipeline.client.sanitize_text(result.parsed.model_dump_json()))
        apply_output(pipeline.store, pipeline.character, turn_id, output, pipeline._safe_trace(context), trace,
                     notify=pipeline.config.early.notify)
    except Exception as error:
        trace = pipeline._safe_trace(getattr(error, "trace", None) or trace)
        trace.setdefault("latency_ms", round((perf_counter() - started) * 1000, 2))
        with pipeline.store.connect() as connection:
            _save_run(connection, turn_id, pipeline._safe_trace(context), {},
                      {"episodes": [], "applied": [], "rejected": [], "error": "Steward に失敗しました。会話と人物像は保持します。"}, trace)

    # Display failures must never turn a successfully applied Steward into a failure.
    from cbs.note import fill_forming_notes
    try:
        display_trace = fill_forming_notes(pipeline, source_turn=turn_id)
        with pipeline.store.connect() as connection:
            saved = json.loads(connection.execute('SELECT trace_json FROM turns WHERE id=?', (turn_id,)).fetchone()[0])
            saved['NoteScenes'] = {'output': display_trace}
            connection.execute('UPDATE turns SET trace_json=? WHERE id=?', (json.dumps(saved, ensure_ascii=False), turn_id))
    except Exception:
        pass  # Original records remain available for the next daily retry.
