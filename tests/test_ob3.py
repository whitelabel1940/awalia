from cbs.config import load_config
from shared_helpers import api
def test_finishing_look_pages_persist_to_saved_partner(pipeline, tmp_path):
    from fastapi.testclient import TestClient
    from test_web_bootstrap import app_for
    from cbs.look import normalize_look

    app, root = app_for(pipeline, tmp_path)
    payload = normalize_look({})
    with TestClient(app) as web:
        # Same cumulative payload as the draft and each onboarding Next action.
        for section, key, value in [
            ('style', 'base', '水彩'), ('profile', 'age', '大人'),
            ('face', 'eyes', '切れ長'), ('hair', 'color', '銀色'),
            ('outfit', 'mood', '普段着'),
        ]:
            payload['person'][section][key] = value
            response = web.put('/api/look/meta', json=payload)
            assert response.status_code == 200, response.text
            assert web.get('/api/look').json()['person'] == payload['person']
        assert (root / 'fixture' / 'look.yaml').is_file()
    assert not pipeline.client.calls


def test_simple_stt_choice_survives_reload_without_changing_conversation(pipeline, tmp_path):
    client = api(pipeline, tmp_path)
    assert client.put('/api/settings', json={'llm': {'mode': 'simple'}}).status_code == 200
    before = client.get('/api/settings').json()
    response = client.put('/api/settings', json={'simple': {'stt': 'local/haas-ja'},
                                               'stt': {'auto_send': False, 'device': 'auto'}})
    assert response.status_code == 200, response.text
    after = response.json()
    assert after['llm']['mode'] == 'simple'
    assert after['models']['stt'] == 'local/haas-ja'
    for role in ('actor', 'judge', 'steward', 'embedding', 'research', 'vision'):
        assert after['models'][role] == before['models'][role]
    saved = load_config(data_dir=tmp_path / 'settings')
    assert saved.models.stt == 'local/haas-ja'
    assert saved.llm.mode == 'simple'
    assert not pipeline.client.calls
