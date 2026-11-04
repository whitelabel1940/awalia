"""Reveal: LLM generation, deterministic gates and bounded Actor context."""
from cbs.llm.cost import for_step
import json
from copy import deepcopy
from time import perf_counter
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from cbs.runtime import read_prompt, character_prefix


class RevealTruth(BaseModel):
    llm_role: ClassVar[str] = "steward"
    model_config = ConfigDict(extra="forbid")
    L1: str = Field(min_length=1)
    L2: str = Field(min_length=1)
    L3: str = Field(min_length=1)


def allowed_level(definition, metrics, *, manner=None, secret=None):
    if not secret or not secret.get("truth") or secret["status"] == "pending":
        return 0
    # Once told, a fact remains available even on a casual/pushy follow-up.
    level = max(secret["disclosed_level"], 1 if manner == "sincere" else 0)
    for value in (2, 3):
        gate = definition.gates.get(f"L{value}")
        if gate is not None and all(metrics.get(key, 0) >= count for key, count in gate.model_dump().items()):
            level = max(level, value)
    return level


def secret_states(character, store, *, manner=None, requested_id=None, spoilers=False):
    metrics = store.relationship_metrics(character.model.character_id)
    saved = {s["secret_id"]: s for s in store.list_secrets(character.model.character_id)}
    states = []
    for definition in character.reveal.secrets:
        secret = saved.get(definition.id)
        state = {**definition.model_dump(), "secret_id": definition.id,
                 "status": secret["status"] if secret else "ungenerated",
                 "disclosed_level": secret["disclosed_level"] if secret else 0,
                 "allowed_level": allowed_level(character.reveal, metrics, secret=secret,
                                                manner=manner if definition.id == requested_id else None),
                 "generated_turn": secret["generated_turn"] if secret else None,
                 "provenance": secret["provenance"] if secret else None}
        if spoilers:
            state["truth"] = secret["truth"] if secret else None
            state["history"] = secret["history"] if secret else []
        states.append(state)
    return {"metrics": metrics, "secrets": states,
            "events": store.list_reveal_events(character.model.character_id)}


def reveal_packet(character, judge, states):
    request = judge.reveal_request
    selected = []
    for state in states["secrets"]:
        requested = request is not None and request.secret_id == state["secret_id"]
        spontaneous = judge.emotional_weight == "high" and state["allowed_level"] > state["disclosed_level"]
        # Disclosed facts also remain in context outside an explicit request.
        if not (requested or spontaneous or state["disclosed_level"]):
            continue
        level = state["allowed_level"]
        selected.append({"secret_id": state["secret_id"], "about": state["about"], "life_ref": state["life_ref"],
                         "allowed_level": level, "disclosed_level": state["disclosed_level"],
                         "manner": request.manner if requested else None,
                         "truth": {f"L{i}": state["truth"][f"L{i}"] for i in range(1, level + 1)},
                         "spontaneous": spontaneous and not requested})
    return selected


def generate_truth(character, store, client, config, secret_id, *, replace=False, purpose='chat'):
    from cbs.costs import bind
    bind(client, store, config)
    definition = next(s for s in character.reveal.secrets if s.id == secret_id)
    existing = store.list_secrets(character.model.character_id)
    if not replace and any(s["secret_id"] == secret_id for s in existing):
        return {"secret_id": secret_id, "generated": False}
    context = {"author_sources": store.author_sources(character), "world": character.world, "secret": definition.model_dump(),
               "life_history": store.current_model(character).life_history.model_dump(mode="json"),
               "interests": [{k: v for k, v in i.items() if k != "embedding"} for i in store.list_interests(character.model.character_id)],
               "canon_facts": store.list_canon(character.model.character_id),
               "other_secrets": [{"secret_id": s["secret_id"], "truth": s["truth"]} for s in existing
                                 if s["secret_id"] != secret_id]}
    messages = [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
    started = perf_counter()
    trace = {"secret_id": secret_id, "generated": False,
             "request": {"model": config.models.steward, "system": read_prompt("reveal_generator") + character_prefix(character),
                         "messages": messages, "schema": RevealTruth.model_json_schema()}}
    try:
        result = for_step(client, 'reveal', purpose).generate_json(config.models.steward, read_prompt("reveal_generator") + character_prefix(character), messages, RevealTruth)
        truth = RevealTruth.model_validate(result.parsed).model_dump()
        truth = json.loads(client.sanitize_text(json.dumps(truth, ensure_ascii=False)))
        status = "pending" if character.reveal.generation == "author_approval" else "hidden"
        store.save_secret(character.model.character_id, secret_id, truth, status=status,
                          provenance="ai_generated", replace=replace, action="regenerate" if replace else "generate")
        trace.update(result.trace(), generated=True, status=status)
    except Exception as error:
        trace.update(getattr(error, "trace", None) or {})
        trace.update(error="真相の生成に失敗しました。保存済みの内容または L0 で続行します。",
                     warnings=["Reveal Generator 失敗・会話は継続"])
    trace.setdefault("latency_ms", round((perf_counter() - started) * 1000, 2))
    return trace


def inspector_trace(trace, spoilers=False):
    if spoilers:
        return trace
    trace = deepcopy(trace)
    has_reveal = bool(trace.get("Packet", {}).get("output", {}).get("secrets") or
                      trace.get('Packet', {}).get('output', {}).get('judgment_degraded'))
    for secret in trace.get("Packet", {}).get("output", {}).get("secrets", []):
        secret.pop("truth", None)
    if has_reveal:
        for step in ("Actor", "Rewrite", "Rewrite-2", "Deflection"):
            trace.get(step, {}).get("request", {}).pop("system", None)
            for message in trace.get(step, {}).get("request", {}).get("messages", []):
                # The per-turn Packet is now an input message, outside the fixed System.
                if message["content"].startswith('{"packet":'):
                    context = json.loads(message["content"])
                    for secret in context.get("packet", {}).get("secrets", []):
                        secret.pop("truth", None)
                    message["content"] = json.dumps(context, ensure_ascii=False)
    generation = trace.get("Reveal Generator")
    if generation:
        for key in ("request", "response"):
            generation.pop(key, None)
    for step in [key for key in trace if key.startswith(('Check', 'Post-Check')) or key == 'Extract']:
        item = trace.get(step, {})
        for message in item.get("request", {}).get("messages", []):
            context = json.loads(message["content"])
            for secret in context.get("secrets", []):
                secret.pop("truth", None)
                secret.pop("history", None)
            for secret in context.get("packet", {}).get("secrets", []):
                secret.pop("truth", None)
            message["content"] = json.dumps(context, ensure_ascii=False)
        # Check explanations can quote full hidden truth, including after failures.
        response = item.get("response")
        if response and has_reveal:
            parsed = response.get("parsed")
            if parsed:
                for claim in parsed.get("problems", parsed.get("claims", [])):
                    claim["reason"] = "検査理由を表示するにはネタバレを表示してください。"
            response["text"] = "検査の生出力はネタバレ表示時に確認できます。"
    if has_reveal:
        for claim in (trace.get("Commit", {}).get("input", {}).get("post_check") or {}).get("claims", []):
            claim["reason"] = "検査理由を表示するにはネタバレを表示してください。"
    return trace
