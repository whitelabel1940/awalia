import json

import pytest

from cbs.config import load_config
from cbs.decision import prejudge, questions_for
from cbs.llm import FakeLLMClient
from cbs.llm.decision import DecisionError, decode


def answers(questions, selected=None, **usage):
    selected = selected or {}
    out = {}
    for name, q in questions.items():
        if q['type'] == 'noul':
            out[name] = {'type': 'noul', 'noul': int(selected.get(name, False))}
        else:
            labels = list(q['criteria']) if q['type'] == 'choice' else list(map(str, range(len(q['criteria']))))
            value = selected.get(name, 'none' if 'none' in labels else 'no' if 'no' in labels else labels[0])
            out[name] = {'type': q['type'], 'probabilities': {k: int(k == value) for k in labels}}
            if q['type'] == 'choice':
                out[name]['choice'] = value
    return {'answers': out, 'usage': usage}


def context():
    return {'domains': [{'id': 'd', 'status': 'open'}], 'signatures': [{'id': 's', 'when': 'test'}],
            'secrets': [{'id': 'secret', 'about': 'test'}], 'life_history_elements': [{'id': 'life'}],
            'candidate_memories': [{'ref': 'fact:1', 'private': True}, {'ref': 'memory:2'}]}


@pytest.mark.parametrize('past', [False, True])
def test_assembly_and_conditional_past(pipeline, past):
    ctx = context()
    q = questions_for(ctx)
    assert q['domain_0']['instructions'] == 'domains_touched: d '
    assert 'memory_0_private_topic_current' in q and 'memory_1_private_topic_current' not in q
    client = FakeLLMClient(decision_responses=[answers(q, {
        'emotional_weight': '2', 'user_disclosure': True, 'asks_about_past': past,
        'domain_0': 'direct', 'signature_0': True, 'reveal_secret': 'secret', 'reveal_manner': 'sincere',
        'past_0': 'unknown_major', 'memory_0_relevant_now': 'yes', 'memory_0_private_topic_current': 'yes'})],
        past_reference_responses=[{'asks_about_past': True, 'targets': [
            {'life_ref': 'life', 'aspect': 'cause', 'known': True, 'major': False},
            {'life_ref': 'alien', 'aspect': 'cause', 'known': True, 'major': False}]}])
    metadata = {}
    result = prejudge(client, pipeline.config, 'system', [{'role': 'user', 'content': json.dumps(ctx)}], metadata)
    j = result.parsed
    assert j.emotional_weight == 'high' and j.user_disclosure == 'yes'
    assert j.domains_touched[0].id == 'd' and j.signatures_relevant == ['s']
    assert j.reveal_request.secret_id == 'secret' and j.reveal_request.manner == 'sincere'
    assert j.memory_use[0].private_topic_current == 'yes' and j.memory_use[1].private_topic_current == 'no'
    calls = [c for c in client.calls if c.get('schema', {}).get('title') == 'PastReference']
    assert len(calls) == int(past)
    if past:
        assert [t.model_dump() for t in j.past_reference.targets] == [
            {'life_ref': 'life', 'aspect': 'cause', 'known': False, 'major': True}]
    else:
        assert not j.past_reference.targets
    assert 'original_messages' not in json.dumps(metadata) and 'request' not in metadata


@pytest.mark.parametrize('failure', [TimeoutError(), ConnectionError(), DecisionError('http', status=503), {'answers': {}}])
def test_pipeline_fallback(pipeline, failure):
    pipeline.config.models.decision = 'openrouter/google/gemini-decision-1'
    pipeline.client = FakeLLMClient(decision_responses=[failure], json_responses=[{'user_disclosure': 'yes'}, {'problems': []}])
    result = pipeline.run(pipeline.store.create_session('fixture')['id'], 'hello')
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(result['turn_id'])
    assert trace['Pre-Judge']['used'] == 'judge'
    assert trace['Pre-Judge']['decision']['failure_kind'] in ('timeout', 'transport', 'http', 'answer_names')
    assert trace['Packet']['input']['user_disclosure'] == 'yes'
    assert trace['Pre-Judge']['decision']['request_count'] >= 1
    assert all('latency_ms' in b for b in trace['Pre-Judge']['decision']['batches'])


def test_zdr_skips_decision(pipeline):
    pipeline.config.models.decision = 'openrouter/google/gemini-decision-1'
    pipeline.config.llm.zdr_only = True
    pipeline.client = FakeLLMClient(decision_responses=[AssertionError('must not call')])
    result = pipeline.run(pipeline.store.create_session('fixture')['id'], 'hello')
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(result['turn_id'])['Pre-Judge']
    assert trace['used'] == 'judge' and 'zdr_only' in trace['decision']['skip_reason']
    assert len(pipeline.client.decision_responses) == 1


@pytest.mark.parametrize('model,count,expected', [('openrouter/google/gemini-decision-1', 65, [32,32,1]),
                                                 ('gemini/gemini-decision-1', 1001, [1000,1])])
def test_batches_and_accounting(pipeline, model, count, expected):
    from cbs.costs import bind
    from cbs.llm.cost import for_step
    q = {f'q{i}': {'type': 'noul'} for i in range(count)}
    items = list(q.items())
    responses = []
    index = 0
    for size in expected:
        responses.append(answers(dict(items[index:index+size]), input_tokens=100, total_input_tokens=100, cost=.02))
        index += size
    client = bind(FakeLLMClient(decision_responses=responses), pipeline.store, pipeline.config)
    result = for_step(client, 'pre_judge', 'chat').decide(model, '{}', q)
    assert [len(b['questions']) for b in result.batches] == expected
    assert len(result.values) == count
    client.costs.flush()
    with pipeline.store.connect() as db:
        row = db.execute('SELECT role,purpose,usd,input_tokens FROM cost_calls WHERE model=?', (model,)).fetchone()
    assert row['role'] == 'pre_judge' and row['purpose'] == 'chat'
    assert row['input_tokens'] == len(expected)*100
    assert row['usd'] == pytest.approx(.02*len(expected) if model.startswith('openrouter') else len(expected)*100*.1/1e6)


@pytest.mark.parametrize('answer', [None, {'type': 'noul', 'noul': True}, {'type': 'noul', 'noul': float('nan')},
                                   {'type': 'noul', 'noul': 1.01}, {'type': 'choice', 'probabilities': {}}])
def test_invalid_noul(answer):
    with pytest.raises(DecisionError):
        decode({'type': 'noul'}, answer)


@pytest.mark.parametrize('prob,choice', [({'a': .2}, 'a'), ({'a': .2,'b': .2}, 'a'),
                                      ({'a': True,'b': 0}, 'a'), ({'a': .2,'b': .8}, 'a'),
                                      ({'a': float('inf'),'b': 0}, 'a')])
def test_invalid_probabilities(prob, choice):
    with pytest.raises(DecisionError):
        decode({'type': 'choice', 'criteria': {'a': '', 'b': ''}},
               {'type': 'choice', 'choice': choice, 'probabilities': prob})


def test_tied_choice():
    assert decode({'type': 'choice', 'criteria': {'a': '', 'b': ''}},
                  {'type': 'choice', 'choice': 'b', 'probabilities': {'a': .5, 'b': .5}}) == 'b'


@pytest.mark.parametrize('preset', ['mixed', 'gemini', 'recommended_direct', 'claude', 'openai'])
def test_legacy_settings(tmp_path, preset):
    from cbs.web.settings_api import toml_text
    cfg = load_config(data_dir=tmp_path)
    models = next(p.models.model_dump(exclude={'decision'}) for p in cfg.advanced.presets if p.id == preset)
    (tmp_path/'settings.toml').write_text(toml_text({'models': models}), encoding='utf-8')
    reread = load_config(data_dir=tmp_path)
    expected = next(p.models.decision for p in cfg.advanced.presets if p.id == preset)
    assert reread.models.decision == expected
    models['actor'] += '-custom'
    (tmp_path/'settings.toml').write_text(toml_text({'models': models}), encoding='utf-8')
    assert load_config(data_dir=tmp_path).models.decision == ''
    models['decision'] = ''
    (tmp_path/'settings.toml').write_text(toml_text({'models': models}), encoding='utf-8')
    assert load_config(data_dir=tmp_path).models.decision == ''


@pytest.mark.parametrize('value', ['openai/gemini-decision-1','local/model','gemini/wrong','openrouter/gemini-decision-1'])
def test_invalid_config(pipeline, tmp_path, value):
    from cbs.web.settings_api import settings_candidate
    with pytest.raises(ValueError):
        settings_candidate(pipeline.config, {'models': {'decision': value}}, tmp_path)


def test_disabled_config_and_connection(pipeline, tmp_path):
    from cbs.web.settings_api import settings_candidate
    from shared_helpers import api
    for value in ('', None, 'gemini/gemini-decision-1', 'openrouter/google/gemini-decision-1'):
        new = settings_candidate(pipeline.config, {'llm': {'mode':'advanced'}, 'models': {'decision':value}}, tmp_path)
        assert new.models.decision == ('' if value is None else value)
    web = api(pipeline, tmp_path)
    disabled = web.put('/api/settings', json={'llm': {'mode':'advanced'}, 'models': {'decision':None}})
    assert disabled.status_code == 200
    assert load_config(data_dir=tmp_path/'settings').models.decision == ''
    result = web.post('/api/settings/connection', json={'settings': {'llm': {'mode':'advanced'}, 'models': {'decision':'gemini/gemini-decision-1'}}})
    assert result.status_code == 200
    assert len([c for c in pipeline.client.calls if c.get('role') == 'decision']) == 1
    pipeline.client.decision_responses = [TimeoutError('sensitive exception must not escape')]
    result = web.post('/api/settings/connection', json={'settings': {'models': {'decision':'gemini/gemini-decision-1'}}})
    assert result.status_code == 422 and 'sensitive' not in result.text


def test_legacy_custom_simple(tmp_path):
    # Simple mode always has the OpenRouter key, so a different conversation model still gets the Decision route.
    (tmp_path/'settings.toml').write_text('[llm]\nmode="simple"\n[simple]\nactor="openrouter/custom/model"\n', encoding='utf-8')
    assert load_config(data_dir=tmp_path).models.decision == 'openrouter/google/gemini-decision-1'


class AnsweringFake(FakeLLMClient):
    def decide(self, model, input_text, questions):
        # Generate finite scripted answers from the actual candidate names, not semantics.
        items = list(questions.items())
        self.decision_responses = [answers(dict(items[i:i+32]), user_disclosure=True) for i in range(0, len(items), 32)]
        return super().decide(model, input_text, questions)


def test_pipeline_uses_decision_and_omits_body(pipeline):
    pipeline.config.models.decision = 'openrouter/google/gemini-decision-1'
    pipeline.client = AnsweringFake(json_responses=[{'problems': []}])
    result = pipeline.run(pipeline.store.create_session('fixture')['id'], 'hello')
    pipeline.wait_for_steward()
    trace = pipeline.store.get_trace(result['turn_id'])['Pre-Judge']
    assert trace['used'] == 'decision' and trace['decision']['question_count'] > 5
    # The judge's own input stays (a later replay re-asks the judge); the Decision wrapper does not.
    assert trace['request']['system'] and trace['request']['messages']
    assert 'original_system' not in json.dumps(trace)
    assert not any(c.get('schema', {}).get('title') == 'PreJudge' for c in pipeline.client.calls)


def test_proactive_bypasses_decision(pipeline):
    from threading import Event
    from cbs.autonomy import Proactive
    pipeline.config.models.decision = 'openrouter/google/gemini-decision-1'
    pipeline.client = FakeLLMClient(decision_responses=[AssertionError('must not call')], json_responses=[{'problems': []}])
    result = pipeline.proactive_reply(pipeline.store.create_session('fixture')['id'],
                                      Proactive(speak=True, reason='test'), Event())
    pipeline.wait_for_steward()
    assert result and len(pipeline.client.decision_responses) == 1


@pytest.mark.parametrize('model', ['openrouter/google/gemini-decision-1', 'gemini/gemini-decision-1'])
def test_request_shapes_and_no_credentials_in_trace(pipeline, model, monkeypatch):
    from cbs.llm import RoutingLLMClient
    import httpx

    class HTTPFake(FakeLLMClient):
        def post(self, endpoint, *, headers, json):
            self.calls.append({'endpoint': endpoint, 'headers': headers, 'json': json})
            return httpx.Response(200, json=answers(json['questions'], total_input_tokens=12, input_tokens=12, cost=.01))

    fake = HTTPFake()
    captured = {}
    def http_client(**options):
        captured.update(options)
        return fake
    monkeypatch.setattr(httpx, 'Client', http_client)
    pipeline.config.llm.tls_strict = False
    route = RoutingLLMClient(pipeline.config, credentials={model.split('/')[0]: 'synthetic-memory-only'})
    q = {'test': {'type': 'noul', 'instructions': 'test'}}
    result = route.decide(model, '{"original_messages": []}', q)
    call = fake.calls[0]
    assert captured['verify'].check_hostname
    import ssl
    assert not captured['verify'].verify_flags & ssl.VERIFY_X509_STRICT
    assert captured['follow_redirects'] is False
    if model.startswith('gemini'):
        assert call['endpoint'] == 'https://generativelanguage.googleapis.com/v1beta/decisions'
        assert call['headers'] == {'x-goog-api-key': 'synthetic-memory-only'}
        assert call['json'] == {'model': 'gemini-decision-1', 'input': '{"original_messages": []}', 'questions': q}
    else:
        assert call['endpoint'].endswith('/api/v1/systemone')
        assert call['headers'] == {'Authorization': 'Bearer synthetic-memory-only'}
        assert call['json'] == {'model': 'google/gemini-decision-1', 'state': {'original_messages': []}, 'questions': q}
    assert 'synthetic-memory-only' not in json.dumps(result.trace())
    assert 'original_messages' not in json.dumps(result.trace())
