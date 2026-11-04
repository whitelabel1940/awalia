"""Actor-written public note lines, checked before the caller's atomic save."""
import json
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cbs import clock
from cbs.character import effective_persona
from cbs.display_text import display_call
from cbs.llm.cost import for_step
from cbs.runtime import character_prefix, read_prompt
from cbs.pipeline import Check


class VoiceCheck(Check):
    llm_role: ClassVar[str] = 'judge'


class Monologue(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    llm_role: ClassVar[str] = 'actor'
    text: str = Field(min_length=1, max_length=300)

    @field_validator('text', mode='before')
    @classmethod
    def one_line(cls, value):
        return ' '.join(value.split()) if isinstance(value, str) else value


def voice_call(pipeline, prompt, schema, context, texts, *, purpose, trace):
    """Share NT1's two-candidate limit for script checks and semantic Check.

    Check reasons and hidden truth never enter the writer's retry context.
    The caller owns persistence, including keeping prior records on failure.
    """
    from cbs.pipeline import INTERNAL_MARKERS
    from cbs.reveal import secret_states

    store, character = pipeline.store, pipeline.character
    cid = pipeline.config.character.id
    persona = effective_persona(character, pipeline.config).ai_question
    requests = store.current_requests(character)
    packet = {'world': character.world, 'expression': store.current_character(character).expression.model_dump(mode='json', by_alias=True),
              'partner_requests': requests, 'feeling': context.get('feeling'),
              'current_life': character.expression.current_life,
              'interests': [{k: v for k, v in i.items() if k != 'embedding'} for i in store.list_interests(cid)]}
    # Truth stays in Check only; a note cannot disclose a new secret.
    secrets = secret_states(character, store, spoilers=True)['secrets']
    secrets = [{**s, 'allowed_level': s['disclosed_level']} for s in secrets]
    check_context = {**context, 'ai_question': persona, 'packet': packet,
                     'user_text': '', 'recent_turns': [], 'current_datetime': clock.stamp(clock.iso()),
                     'private_facts_policy': store.private_context(cid),
                     'canon_facts': store.list_canon(cid), 'plans': store.list_plans(cid), 'secrets': secrets}
    system = (read_prompt(prompt) + '\n\n' + read_prompt(f'ai_question_{persona}')
              + character_prefix(character, actor=True, store=store))
    check_system = read_prompt('check') + '\n\n' + read_prompt('voice_check') + character_prefix(character, store=store)
    attempts = trace.setdefault('voice_attempts', [])

    def generate():
        messages = [{'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}]
        if attempts:
            previous = (attempts[-1].get('response') or {}).get('parsed')
            if previous is not None:
                messages.append({'role': 'assistant', 'content': json.dumps(previous, ensure_ascii=False)})
            messages.append({'role': 'user', 'content': read_prompt('voice_retry')})
        attempt = {'request': {'model': pipeline.config.models.actor, 'system': system,
                              'messages': messages, 'schema': schema.model_json_schema()}}
        attempts.append(attempt)
        try:
            result = for_step(pipeline.client, 'actor', purpose).generate_json(
                pipeline.config.models.actor, system, messages, schema)
        except Exception as error:
            attempt.update(getattr(error, 'trace', None) or {})
            attempt['error'] = 'voice generation failed'
            raise
        attempt.update(result.trace())
        # Validate exactly the sanitized text that will be saved.
        result.parsed = schema.model_validate_json(pipeline.client.sanitize_text(result.parsed.model_dump_json()))
        attempt['response']['parsed'] = result.parsed.model_dump(mode='json')
        return result

    def validate(result):
        candidate = '\n'.join(texts(result.parsed))
        if INTERNAL_MARKERS.search(candidate):
            attempts[-1]['rejected'] = 'internal marker'
            return False
        messages = [{'role': 'user', 'content': json.dumps({**check_context, 'assistant_text': candidate}, ensure_ascii=False)}]
        attempts[-1]['check'] = {'request': {'model': pipeline.config.models.judge, 'system': check_system,
                                           'messages': messages, 'schema': VoiceCheck.model_json_schema()}}
        try:
            result = for_step(pipeline.client, 'Check', purpose).generate_json(
                pipeline.config.models.judge, check_system, messages, VoiceCheck)
        except Exception as error:
            attempts[-1]['check'].update(getattr(error, 'trace', None) or {})
            attempts[-1]['check']['error'] = 'voice check failed'
            raise
        attempts[-1]['check'] = result.trace()
        return not VoiceCheck.model_validate(result.parsed).problems

    result = display_call(generate, lambda r: r.parsed.model_dump().values(), validate=validate)
    # Retain the existing top-level usage metadata used by trace retention.
    trace.update(result.trace())
    return result.parsed
