"""App-owned Pre-Judge questions and assembly; meaning is selected by the LLMs."""
from collections import defaultdict
from copy import deepcopy
import json
from time import perf_counter

from cbs.llm import LLMResult
from cbs.llm.cost import for_step
from cbs.runtime import read_prompt


def questions_for(ctx):
    templates = json.loads(read_prompt('decision_questions'))
    def template(name, **values):
        item = deepcopy(templates[name])
        item['instructions'] = item['instructions'].format_map(defaultdict(str, values))
        return item
    q = {key: template(key) for key in ('emotional_weight', 'asks_about_past', 'user_disclosure', 'reveal_secret', 'reveal_manner')}
    q['reveal_secret']['criteria'].update({s['id']: s['about'] for s in ctx['secrets']})
    for i, d in enumerate(ctx['domains']):
        q[f'domain_{i}'] = template('domain', **d)
    for i, s in enumerate(ctx['signatures']):
        q[f'signature_{i}'] = template('signature', **s)
    for i, e in enumerate(ctx['life_history_elements']):
        q[f'past_{i}'] = template('past_target', **e)
    for i, m in enumerate(ctx['candidate_memories']):
        fields = ['relevant_now', 'counterevidence', 'continuation']
        if m.get('private'):
            fields.append('private_topic_current')
        for field in fields:
            q[f'memory_{i}_{field}'] = template('memory', ref=m['ref'], field=field)
    return q


def skip_reason(config):
    if config.models.decision and config.models.decision.startswith('openrouter/') and config.llm.zdr_only:
        return 'zdr_only: systemone provider retention policy unverified'
    return None


def prejudge(client, config, system, messages, metadata, *, capture=None, purpose='chat', questions=None,
             choices=None, past_text=None):
    from cbs.pipeline import PreJudge, PastReference
    started = perf_counter()
    ctx = json.loads(messages[-1]['content'])
    questions = questions if questions is not None else questions_for(ctx)
    if capture is not None:
        capture['questions'] = questions
    metadata.update(model=config.models.decision, question_count=len(questions), request_count=0, batches=[])
    state = {'instructions': choices if choices is not None else read_prompt('decision_choices'), 'original_system': system, 'original_messages': messages}
    try:
        result = for_step(client, 'pre_judge', purpose).decide(config.models.decision, json.dumps(state, ensure_ascii=False), questions)
        if capture is not None:
            capture['raw'] = result.raw
        metadata.update(result.trace())
        v = result.values
        data = {'emotional_weight': ['low', 'medium', 'high'][int(v['emotional_weight'])],
                'user_disclosure': 'yes' if v['user_disclosure'] else 'no',
                'past_reference': {'asks_about_past': v['asks_about_past'], 'targets': []},
                'domains_touched': [{'id': d['id'], 'strength': v[f'domain_{i}']} for i, d in enumerate(ctx['domains']) if v[f'domain_{i}'] != 'none'],
                'signatures_relevant': [s['id'] for i, s in enumerate(ctx['signatures']) if v[f'signature_{i}']],
                'memory_use': [{'ref': m['ref'], **{field: v[f'memory_{i}_{field}'] for field in
                    ('relevant_now', 'counterevidence', 'continuation', 'private_topic_current') if f'memory_{i}_{field}' in v}}
                    for i, m in enumerate(ctx['candidate_memories'])]}
        if v['reveal_secret'] != 'none':
            data['reveal_request'] = {'secret_id': v['reveal_secret'], 'manner': v['reveal_manner']}
        if v['asks_about_past']:
            from cbs.llm.decision import request_guard
            if guard := request_guard.get():
                guard(config.models.judge)
            text = for_step(client, 'pre_judge', purpose).generate_json(config.models.judge,
                (past_text if past_text is not None else read_prompt('decision_past_text')) + '\n' + system, messages, PastReference)
            metadata['past_reference'] = {'model': text.model, 'usage': text.usage, 'latency_ms': text.latency_ms}
            for target in text.parsed.targets:
                i = next((i for i, e in enumerate(ctx['life_history_elements']) if e['id'] == target.life_ref), None)
                if i is not None and v[f'past_{i}'] != 'none':
                    selected = v[f'past_{i}']
                    data['past_reference']['targets'].append(target.model_copy(update={
                        'known': selected.startswith('known_'), 'major': selected.endswith('_major')}).model_dump())
        parsed = PreJudge.model_validate(data)
        # Keep the judge's exact input in the trace, so a later replay can re-ask the judge.
        return LLMResult(parsed.model_dump_json(), parsed, result.usage,
                         round((perf_counter()-started)*1000, 2), config.models.decision,
                         {'model': config.models.decision, 'system': system, 'messages': messages,
                          'schema': PreJudge.model_json_schema()})
    except Exception as error:
        from cbs.llm.decision import DecisionError
        if isinstance(error, DecisionError):
            metadata.update(error.trace)
            if capture is not None:
                capture['raw'] = getattr(error, 'raw', {})
        else:
            metadata['failure_kind'] = 'timeout' if isinstance(error, TimeoutError) else 'assembly'
        raise
