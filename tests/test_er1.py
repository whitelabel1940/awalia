"""ER1 offline contracts: SDK exceptions are hand-built, clients are fake."""
import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from google.genai.errors import ClientError, ServerError

from cbs.config import load_config
from cbs.costs import bind
from cbs.llm import FakeLLMClient, LLMError, RoutingLLMClient
from cbs.llm.anthropic import AnthropicLLMClient
from cbs.llm.gemini import GeminiLLMClient
from cbs.llm.openai_client import OpenAILLMClient
from cbs.llm.errors import failure_notice, provider_error
from cbs.llm.failures import FailureState
from cbs.llm.failures import observed
from cbs.llm.cost import for_step, metered
from cbs.pipeline import Check
from cbs.store import Store
from cbs.web import create_app

PRIVATE = 'PRIVATE_BODY https://private.invalid/?key_id=PRIVATE_ID'


def sdk_error(provider, status, detail=None):
    detail = {'message': PRIVATE, **(detail or {})}
    body = {'error': detail}
    request = httpx.Request('POST', 'https://private.invalid/?key_id=PRIVATE_ID')
    response = httpx.Response(status, json=body, request=request)
    if provider in ('openai', 'openrouter', 'local'):
        return openai.APIStatusError(PRIVATE, response=response, body=body)
    if provider == 'anthropic':
        return httpx.HTTPStatusError(PRIVATE, request=request, response=response)
    return (ClientError if status < 500 else ServerError)(status, body)


CASES = [
    ('openrouter', 402, {}, 'balance'),
    ('openrouter', 403, {'metadata': {'scope': 'key_limit'}}, 'limit'),
    ('openrouter', 403, {'metadata': {'limit_source': 'openrouter_key_limit'}}, 'limit'),
    ('openrouter', 403, {}, 'unknown'),
    ('openrouter', 401, {}, 'auth'),
    *[('openrouter', code, {}, 'busy') for code in (408, 429, 502, 503, 504, 529)],
    ('openrouter', 500, {}, 'unknown'),
    ('openai', 429, {'code': 'insufficient_quota'}, 'balance'),
    ('openai', 429, {}, 'busy'),
    *[('openai', code, {}, 'auth') for code in (401, 403)],
    *[('openai', code, {}, 'busy') for code in (500, 503, 529)],
    ('anthropic', 400, {'message': 'credit balance is too low ' + PRIVATE}, 'balance'),
    ('anthropic', 400, {'message': 'usage limits ' + PRIVATE}, 'limit'),
    *[('anthropic', code, {}, 'auth') for code in (401, 403)],
    *[('anthropic', code, {}, 'busy') for code in (429, 500, 529)],
    ('gemini', 429, {'message': 'prepayment credits are depleted ' + PRIVATE}, 'balance'),
    ('gemini', 429, {'message': 'billing ' + PRIVATE}, 'balance'),
    ('gemini', 429, {'status': 'RESOURCE_EXHAUSTED'}, 'busy'),
    ('gemini', 400, {'details': [{'reason': 'API_KEY_INVALID'}]}, 'auth'),
    *[('gemini', code, {}, 'auth') for code in (401, 403)],
    *[('gemini', code, {}, 'busy') for code in (500, 503)],
    *[(provider, 404, {}, 'unknown') for provider in ('openrouter', 'openai', 'anthropic', 'gemini', 'local')],
]


def adapter(provider, error):
    def fail(*args, **kwargs):
        raise error
    method = SimpleNamespace(create=fail, parse=fail)
    if provider in ('openrouter', 'openai', 'local'):
        client = object.__new__(OpenAILLMClient)
        client.name, client._key, client._api, client._zdr, client._reasoning = provider, '', 'chat', False, {}
        client._client = SimpleNamespace(chat=SimpleNamespace(completions=method), responses=method,
            embeddings=method, audio=SimpleNamespace(transcriptions=method))
    elif provider == 'anthropic':
        client = object.__new__(AnthropicLLMClient)
        client._key = 'synthetic-secret'
        client._client = SimpleNamespace(post=fail, stream=fail)
    else:
        client = object.__new__(GeminiLLMClient)
        client._key, client._thinking = 'synthetic-secret', {}
        client._client = SimpleNamespace(models=SimpleNamespace(generate_content=fail,
            generate_content_stream=fail, embed_content=fail))
    return client


@pytest.mark.parametrize('provider,status,detail,kind', CASES)
def test_provider_classification_and_safe_trace(provider, status, detail, kind):
    client = adapter(provider, sdk_error(provider, status, detail))
    with pytest.raises(LLMError) as caught:
        client.generate_text('test', '', [])
    error = caught.value
    assert (error.kind, error.provider, error.status_code) == (kind, provider, status)
    assert error.trace['error_kind'] == kind
    public = str(error) + json.dumps(error.trace) + json.dumps(failure_notice(error, 'ソラ'))
    assert 'PRIVATE_' not in public and 'private.invalid' not in public


@pytest.mark.parametrize('provider', ['openrouter', 'openai', 'anthropic', 'gemini', 'local'])
@pytest.mark.parametrize('error', [httpx.ConnectError(PRIVATE), httpx.ReadTimeout(PRIVATE),
    openai.APIConnectionError(request=httpx.Request('POST', 'https://private.invalid')),
    openai.APITimeoutError(request=httpx.Request('POST', 'https://private.invalid'))])
def test_network(provider, error):
    result = provider_error(error, provider)
    assert result.kind == 'network' and result.status_code is None
    notice = failure_notice(result)
    assert ('起動' in notice['message']) == (provider == 'local')
    assert 'PRIVATE_' not in json.dumps(result.trace)


@pytest.mark.parametrize('provider,operation', [
    (p, op) for p in ('openrouter', 'openai', 'anthropic', 'gemini')
    for op in ('json', 'stream', 'embed', 'audio', 'video')
    if not (p == 'anthropic' and op in ('embed', 'audio', 'video') or p != 'openrouter' and op == 'video')])
def test_other_provider_boundaries(provider, operation):
    error = sdk_error(provider, 429, {'message': 'billing ' + PRIVATE})
    client = adapter(provider, error)
    with pytest.raises(LLMError) as caught:
        if operation == 'json':
            client.generate_json('test', '', [], Check)
        elif operation == 'stream':
            client.generate_text_stream('test', '', [], lambda _: None)
        elif operation == 'embed':
            client.embed(['test'], 'test')
        elif operation == 'audio':
            client.transcribe_audio('test', '', b'fake')
        else:
            client.generate_video_json('google/gemini-3.8-flash', '', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', Check)
    assert caught.value.kind == ('balance' if provider == 'gemini' else 'busy')
    assert caught.value.provider == provider
    assert 'PRIVATE_' not in str(caught.value) + json.dumps(caught.value.trace)


@pytest.mark.parametrize('kind', ['balance', 'limit', 'auth', 'busy', 'network', 'unknown'])
def test_persisted_banner_rules_and_provider_specific_recovery(store, tmp_path, kind):
    cfg = load_config(data_dir=tmp_path)
    client = bind(FakeLLMClient(error_responses=[LLMError('safe', kind=kind, provider='openrouter', status_code=403)],
        text_responses=['fail', 'other provider', 'success']), store, cfg)
    with pytest.raises(LLMError):
        for_step(client, 'maintenance', 'memory').generate_text('openrouter/test', '', [])
    state = FailureState(Store(store.path))  # Reopening the DB/browser retains state.
    assert bool(state.notices('ソラ')) == (kind in ('balance', 'limit', 'auth'))
    client.generate_text('openai/test', '', [])
    assert bool(state.notices('ソラ')) == (kind in ('balance', 'limit', 'auth'))
    client.generate_text('openrouter/test', '', [])
    assert state.notices('ソラ') == []


def test_transient_failure_does_not_clear_other_banner(store):
    state = FailureState(store)
    for provider in ('openai', 'openrouter'):
        state.record(provider, LLMError('', provider=provider, kind='balance'))
    state.record('openrouter', LLMError('', kind='network'))
    assert len(state.notices('ソラ')) == 2
    state.record('openai')
    assert state.notices('ソラ')[0]['provider'] == 'openrouter'


@pytest.mark.parametrize('stream', [False, True])
def test_chat_error_response_trace_and_text_are_preserved(pipeline, tmp_path, stream):
    failure = provider_error(sdk_error('openrouter', 403, {'metadata': {'scope': 'key_limit'}}), 'openrouter')
    # Pre-Judge succeeds, Actor fails. The scripted Fake and router share the production boundary.
    fake = FakeLLMClient(error_responses=[None, failure])
    pipeline.client = bind(fake, pipeline.store, pipeline.config)
    session = pipeline.store.create_session('fixture')
    app = create_app(pipeline, settings_dir=tmp_path / 'settings', work_root=tmp_path / 'work')
    with TestClient(app) as web:
        path = f"/api/sessions/{session['id']}/messages" + ('/stream' if stream else '')
        result = web.post(path, json={'content': '失われない文'})
        if stream:
            frames = [json.loads(frame.split('data: ', 1)[1]) for frame in result.text.split('\n\n') if frame.startswith('event: error')]
            notice = frames[0]
        else:
            assert result.status_code == 502
            notice = result.json()
        assert notice['kind'] == 'limit' and '上限' in notice['message']
        assert notice['link']['url'] == 'https://openrouter.ai/settings/keys'
        assert web.get('/api/llm/failures').json()['notices'][0]['kind'] == 'limit'
        turn = pipeline.store.history(session['id'])[0]
        assert turn['status'] == 'failed' and turn['user_text'] == '失われない文'
        trace = pipeline.store.get_trace(turn['id'])['Actor']
        assert {k: trace[k] for k in ('error_kind', 'status_code', 'provider')} == {
            'error_kind': 'limit', 'status_code': 403, 'provider': 'openrouter'}
        assert 'PRIVATE_' not in json.dumps(trace) + result.text


def test_background_daily_failure_sets_banner_and_keeps_kind(pipeline):
    from cbs.memory import daily_maintenance
    failure = LLMError('safe', kind='balance', provider='openrouter', status_code=402)
    with pipeline.store.connect() as db:
        db.execute('DELETE FROM memory_maintenance')
    pipeline.client = bind(FakeLLMClient(error_responses=[failure]), pipeline.store, pipeline.config)
    daily_maintenance(pipeline)
    assert FailureState(pipeline.store).notices('ソラ')[0]['kind'] == 'balance'
    with pipeline.store.connect() as db:
        trace = json.loads(db.execute('SELECT trace_json FROM memory_maintenance ORDER BY rowid DESC LIMIT 1').fetchone()[0])
    assert trace['now_summary']['error_kind'] == 'balance'


def test_setup_uses_same_notice(pipeline, tmp_path, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic-secret')
    failure = LLMError('safe', kind='balance', provider='openrouter', status_code=402)
    pipeline.client = bind(FakeLLMClient(error_responses=[failure]), pipeline.store, pipeline.config)
    app = create_app(pipeline, settings_dir=tmp_path / 'settings', work_root=tmp_path / 'work')
    with TestClient(app) as web:
        response = web.post('/api/settings/connection')
        assert response.status_code == 422
        assert response.json()['kind'] == 'balance'
        assert response.json()['message'] == failure_notice(failure, pipeline.character.model.display_name)['message']
        assert 'synthetic-secret' not in response.text


def test_setup_failure_before_a_partner_exists(store, tmp_path):
    from fastapi import FastAPI
    from cbs.web.bootstrap_host import BootstrapHost
    from cbs.web.setup_api import install_setup
    cfg = load_config(data_dir=tmp_path)
    host = BootstrapHost(cfg, store, FakeLLMClient(error_responses=[
        LLMError('safe', kind='balance', provider='openrouter', status_code=402)]))
    app = FastAPI()
    install_setup(app, host, tmp_path, lambda: {'ai_ready': True}, lambda *args: None)
    response = TestClient(app).post('/api/settings/connection')
    assert response.status_code == 422 and response.json()['kind'] == 'balance'
    assert 'パートナー' in response.json()['message']


def test_key_update_keeps_failure_observer_bound(pipeline, tmp_path):
    pipeline.client = bind(RoutingLLMClient(pipeline.config), pipeline.store, pipeline.config)
    pipeline.client.failures.record('openrouter', LLMError('', kind='auth', provider='openrouter'))
    app = create_app(pipeline, settings_dir=tmp_path / 'settings', work_root=tmp_path / 'work')
    web = TestClient(app)
    # No key is written: an empty override exercises the same router replacement as reconnecting.
    assert web.put('/api/settings/keys', json={'provider': 'openrouter', 'key': ''}).status_code == 200
    assert pipeline.client.failures is pipeline.store.llm_failures
    pipeline.client._clients['openrouter'] = FakeLLMClient()
    pipeline.client.generate_text('openrouter/test', '', [])
    assert web.get('/api/llm/failures').json()['notices'] == []


def test_banner_hides_unused_provider_and_new_key_clears_it(pipeline, tmp_path):
    # A restored PC without keys fails on Gemini, then is connected to OpenRouter only.
    from cbs.llm.failures import providers_in_use
    state = FailureState(pipeline.store)
    state.record('gemini', LLMError('', kind='auth', provider='gemini'))
    used = providers_in_use(pipeline.config)
    shown = [n['provider'] for n in state.notices('ソラ', pipeline.config)]
    assert ('gemini' in shown) == ('gemini' in used)
    unused = pipeline.config.model_copy(deep=True)
    unused.models = unused.models.model_copy(update={role: 'openrouter/test' for role in unused.models.model_dump()})
    assert state.notices('ソラ', unused) == [] and state.notices('ソラ')
    app = create_app(pipeline, settings_dir=tmp_path / 'settings', work_root=tmp_path / 'work')
    with TestClient(app) as web:
        assert web.put('/api/settings/keys', json={'provider': 'gemini', 'key': ''}).status_code == 200
    assert FailureState(pipeline.store).notices('ソラ') == []


def test_notice_display_name_and_fixed_links():
    for provider in ('openrouter', 'openai', 'anthropic', 'gemini'):
        notice = failure_notice(LLMError('', kind='balance', provider=provider), 'ソラ')
        assert 'ソラ' in notice['message'] and notice['link']['url'].startswith('https://')
    assert 'トレース' not in failure_notice(LLMError(''))['message']


class SelectiveFailure(FakeLLMClient):
    fail_schemas = ()
    failure = LLMError('safe', kind='auth', provider='openai', status_code=401)

    @observed
    @metered
    def generate_json(self, model, system, messages, schema):
        if schema.__name__ in self.fail_schemas:
            raise self.failure
        return super().generate_json(model, system, messages, schema)


def selective(pipeline, *schemas):
    fake = SelectiveFailure(text_responses=['最初の文', '復旧1', '復旧2', '最後の復旧'])
    fake.fail_schemas = schemas
    pipeline.config.models.judge = 'openai/test'
    pipeline.config.models.steward = 'openai/test'
    pipeline.client = bind(fake, pipeline.store, pipeline.config)
    return fake


@pytest.mark.parametrize('kind,status', [('auth', 401), ('unknown', 404)])
def test_check_recovery_preserves_failure_classification(pipeline, kind, status):
    fake = selective(pipeline, 'Check', 'Expression')
    fake.failure = LLMError('safe', kind=kind, provider='openai', status_code=status)
    sid = pipeline.store.create_session('fixture')['id']
    with pytest.raises(LLMError) as caught:
        pipeline.run(sid, '確認も失敗する')
    assert caught.value.kind == kind and caught.value.status_code == status
    assert caught.value.provider == 'openai'
    turn = pipeline.store.history(sid)[0]
    trace = pipeline.store.get_trace(turn['id'])
    assert trace['Check']['error_kind'] == kind and trace['Safety']['error_kind'] == kind
    assert trace['Safety']['blocked'] and trace['Safety']['rewrite_count'] == 2
    assert bool(FailureState(pipeline.store).notices('ソラ')) == (kind == 'auth')


def test_recovered_prejudge_error_is_not_reported_as_final_safety_error(pipeline):
    fake = selective(pipeline, 'PreJudge')
    problem = {'problems': [{'text': '文', 'verdict': 'forbidden', 'reason': '根拠がない'}]}
    fake.json_responses = [problem] * 4
    sid = pipeline.store.create_session('fixture')['id']
    with pytest.raises(LLMError) as caught:
        pipeline.run(sid, '以前のエラーを理由にしない')
    assert caught.value.kind == 'unknown' and caught.value.status_code is None


def test_background_extract_retains_metadata(pipeline):
    selective(pipeline, 'Extract', 'Expression')
    sid = pipeline.store.create_session('fixture')['id']
    result = pipeline.run(sid, '抽出は背景で失敗する')
    pipeline._post_future.result(timeout=5)
    trace = pipeline.store.get_trace(result['turn_id'])['Extract']
    assert trace['error_kind'] == 'auth' and trace['status_code'] == 401
    assert result['assistant_text'] == '最初の文'


def test_background_proactive_retains_metadata_and_banner(pipeline):
    from cbs.autonomy import proactive
    selective(pipeline, 'Proactive')
    sid = pipeline.store.create_session('fixture')['id']
    result = proactive(pipeline, sid)
    assert not result['speak']
    trace = pipeline.store.list_proactive('fixture')[0]['trace']
    assert trace['error_type'] == 'LLMError' and trace['error_kind'] == 'auth'
    assert FailureState(pipeline.store).notices('ソラ')[0]['kind'] == 'auth'


@pytest.mark.parametrize('provider', ['openrouter', 'openai', 'gemini'])
def test_stream_receive_failure_preserves_kind(provider):
    client = adapter(provider, sdk_error(provider, 429))
    error = sdk_error(provider, 429)
    class BrokenStream:
        def __iter__(self):
            return self
        def __next__(self):
            raise error
        def close(self):
            pass
    if provider == 'gemini':
        client._client.models.generate_content_stream = lambda **kw: BrokenStream()
    else:
        client._client.chat.completions.create = lambda **kw: BrokenStream()
    with pytest.raises(LLMError) as caught:
        client.generate_text_stream('test', '', [], lambda _: None)
    assert caught.value.kind == 'busy' and caught.value.status_code == 429
    assert 'PRIVATE_' not in json.dumps(caught.value.trace)


@pytest.mark.parametrize('status,message,kind', [
    (400, 'credit balance is too low', 'balance'), (400, 'usage limits', 'limit'),
    (401, '', 'auth'), (429, '', 'busy'), (404, '', 'unknown'), (503, '', 'busy')])
def test_anthropic_stream_http_error_body_is_read_before_close(status, message, kind):
    response = httpx.Response(status, request=httpx.Request('POST', 'https://private.invalid'),
        stream=httpx.ByteStream(json.dumps({'error': {'message': message + PRIVATE}}).encode()))
    class ResponseContext:
        def __enter__(self):
            return response
        def __exit__(self, *args):
            response.close()
    client = adapter('anthropic', RuntimeError())
    client._client.stream = lambda *args, **kw: ResponseContext()
    with pytest.raises(LLMError) as caught:
        client.generate_text_stream('test', '', [], lambda _: None)
    assert caught.value.kind == kind and caught.value.status_code == status
    assert response.is_closed and 'PRIVATE_' not in json.dumps(caught.value.trace)
