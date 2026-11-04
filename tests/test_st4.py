from unittest.mock import Mock

import pytest
from cbs.autonomy import activity_availability, autonomy_status, available_tools


def test_local_status_and_shared_builder(pipeline, monkeypatch):
    import cbs.autonomy as autonomy
    pipeline.character.world.update(reality='現実どおり', contact='A')
    pipeline.client.calls.clear()
    monkeypatch.setattr(autonomy, 'reading_tools', Mock(side_effect=AssertionError('no fetch')))
    from types import SimpleNamespace
    assert autonomy_status(SimpleNamespace()) == {'tools': {}, 'real_world': False, 'world_activity': False}
    status = autonomy_status(pipeline)
    assert status['tools']['watch_video']['state'] == 'waiting'
    assert status['tools']['read_source']['state'] == 'waiting'
    assert status['tools']['read_wikipedia']['state'] == 'ok'
    assert '選ばれません' in status['tools']['read_wikipedia']['reason']
    spy = Mock(wraps=activity_availability)
    monkeypatch.setattr(autonomy, 'activity_availability', spy)
    available_tools(pipeline, pipeline.store.autonomy_budget('fixture'))
    autonomy_status(pipeline)
    assert spy.call_count == 2
    assert not pipeline.client.calls
    budget = pipeline.store.autonomy_budget('fixture')
    budget['activities_remaining'] = 0
    budget['tools']['watch_video']['remaining'] = 0
    assert not any(t['available'] for t in available_tools(pipeline, budget) if t['name'] != 'none')
    assert autonomy_status(pipeline) == status
    pipeline.config.autonomy.research = False
    pipeline.config.autonomy.tools.watch_video.enabled = False
    after = autonomy_status(pipeline)
    # With research off, Wikipedia is offered (available_tools), so it no longer explains why it steps aside.
    assert after['tools']['read_wikipedia'] == {'state': 'ok', 'reason': '', 'fix': None}
    assert {k: v for k, v in after['tools'].items() if k != 'read_wikipedia'} == {k: v for k, v in status['tools'].items() if k != 'read_wikipedia'}


@pytest.mark.parametrize('reality,contact,real,world', [('現実どおり','A',True,False),('現実どおり','C',False,False),('異世界','A',False,True),('異世界','C',False,True)])
def test_world(pipeline, reality, contact, real, world):
    pipeline.character.world.update(reality=reality, contact=contact)
    status = autonomy_status(pipeline)
    assert status['real_world'] == real
    assert status['world_activity'] == world
    for name in ('research','read_source','read_book','watch_video','read_wikipedia','browse_shopping'):
        if not real:
            assert status['tools'][name]['state'] == 'unavailable'
    assert (status['tools']['world_activity']['state'] == 'ok') == world


@pytest.mark.parametrize('zdr,model', [(True,False),(False,False),(False,True),(True,True)])
def test_video_reasons(pipeline, zdr, model):
    pipeline.character.world.update(reality='現実どおり', contact='A')
    pipeline.config.llm.zdr_only = zdr
    if model:
        pipeline.config.autonomy.tools.watch_video.model = 'other/model'
    tool = autonomy_status(pipeline)['tools']['watch_video']
    assert tool['state'] == ('unavailable' if zdr or model else 'waiting')
    if zdr or model:
        assert tool['fix'] == '/wizard?ai=1'
        assert ('保存しない' if zdr and not model else 'OpenRouter') in tool['reason']


def test_connection_reasons(pipeline, monkeypatch):
    import cbs.autonomy as autonomy
    pipeline.character.world.update(reality='現実どおり', contact='A')
    monkeypatch.setattr(autonomy, 'research_connection', lambda p: False)
    monkeypatch.setattr(autonomy, 'shopping_ready', lambda p: False)
    status = autonomy_status(pipeline)['tools']
    for name in ('research','browse_shopping'):
        assert status[name]['state'] == 'unavailable'
        assert status[name]['fix'] == '/wizard?ai=1'
    assert status['read_wikipedia']['reason'] == ''

    # Stored candidates are enough: no feed or page request is needed.
    monkeypatch.setattr(pipeline.store, 'source_candidates', lambda cid: [{'id': 1, 'title': 'article', 'url': 'https://example.org/article'}, {'id': 2, 'title': 'video', 'url': 'https://youtu.be/jNQXAC9IVRw'}])
    status = autonomy_status(pipeline)['tools']
    assert status['watch_video']['state'] == 'ok'
    assert status['read_source']['state'] == 'ok'
    pipeline.config.autonomy.tools.browse_shopping.domains = []
    assert status['browse_shopping']['state'] == 'unavailable'
    from fastapi.testclient import TestClient
    from cbs.web import create_app
    monkeypatch.setattr(autonomy, 'reading_tools', Mock(side_effect=AssertionError('no fetch')))
    with TestClient(create_app(pipeline)) as client:
        response = client.get('/api/settings?character_id=fixture')
        assert response.status_code == 200
        assert response.json()['autonomy_status'] == autonomy_status(pipeline)
