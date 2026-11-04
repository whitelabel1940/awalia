"""UI1: versioned user name, legacy snapshots and Fake-only prompt delivery."""
import json

import pytest
from fastapi.testclient import TestClient

from cbs.llm import FakeLLMClient
from cbs.web import create_app


def test_name_save_undo_packet_and_judgment(pipeline):
    p = pipeline
    p.client = FakeLLMClient(json_responses=[{}, {'problems': []}], text_responses=['わかった。'])
    with TestClient(create_app(p)) as client:
        before = client.get('/api/partner/requests').json()
        assert before['fields']['user_name'] == ''
        fields = {**before['fields'], 'user_name': '葵', 'second_person': 'キミ'}
        saved = client.put('/api/partner/requests', json={'version': before['version'], 'fields': fields, 'lines': []}).json()
        assert saved['fields']['user_name'] == '葵' and saved['version'] > before['version']
        sid = p.store.create_session('fixture')['id']
        result = p.run(sid, '私の名前を覚えてる？')
        p.wait_for_steward()
        trace = p.store.get_trace(result['turn_id'])
        assert trace['Packet']['output']['partner_requests']['fields']['user_name'] == '葵'
        assert trace['Packet']['output']['expression']['voice']['second_person']['default'] == 'キミ'
        for step in ('Pre-Judge', 'Actor', 'Check'):
            request = trace[step]['request']
            assert 'user_name' in request['system'] and 'second_person' in request['system']
            assert '葵' in json.dumps(request, ensure_ascii=False)
        undone = client.post('/api/partner/requests/undo', json={'version': saved['version']}).json()
        assert undone['fields'] == before['fields'] and undone['version'] > saved['version']


def test_name_legacy_snapshots_and_optional_clear(pipeline):
    p = pipeline
    initial = p.store.current_requests(p.character)
    old_fields = {k: v for k, v in initial['fields'].items() if k != 'user_name'}
    old = {'fields': old_fields, 'lines': []}
    with p.store.connect() as db:
        p.store._request_version(db, p.character, old, old, 'settings')
    state = p.store.current_requests(p.character)
    assert state['fields']['user_name'] == ''
    saved = p.store.save_requests(p.character, {**state['fields'], 'user_name': '葵'}, [], p.config.requests, state['version'])
    legacy_saved = p.store.save_requests(p.character, {**old_fields, 'register': '丁寧'}, [], p.config.requests, saved['version'])
    assert legacy_saved['fields']['user_name'] == '葵'
    cleared = p.store.save_requests(p.character, {**legacy_saved['fields'], 'user_name': ''}, [], p.config.requests, legacy_saved['version'])
    assert cleared['fields']['user_name'] == ''
    restored = p.store.save_requests(p.character, {}, [], p.config.requests, cleared['version'], undo=True)
    assert restored['fields']['user_name'] == '葵'


@pytest.mark.parametrize('name', ['a\nb', 'a\rb', 'x' * 1001])
def test_name_validation(pipeline, name):
    p = pipeline
    state = p.store.current_requests(p.character)
    with pytest.raises(ValueError):
        p.store.save_requests(p.character, {**state['fields'], 'user_name': name}, [], p.config.requests, state['version'])
    assert p.store.current_requests(p.character)['version'] == state['version']
