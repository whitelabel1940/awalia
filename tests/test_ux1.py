"""Presentation settings persist independently of the turn pipeline; no real providers."""
import pytest
from fastapi.testclient import TestClient
from cbs.config import DisplayConfig, load_config
from cbs.llm import FakeLLMClient
from cbs.web import create_app


def test_display_defaults_persistence_and_no_llm(pipeline, tmp_path):
    assert isinstance(pipeline.client, FakeLLMClient)
    assert pipeline.config.display == DisplayConfig()
    settings = tmp_path / 'settings'
    with TestClient(create_app(pipeline, settings_dir=settings, work_root=tmp_path/'work')) as client:
        values = client.get('/api/settings').json()['display']
        assert values == dict(split_replies=True, delay_user=False, read_seconds=1.2,
                              typing_seconds=2.5, user_delay_seconds=1.0, bubble_max_seconds=1.5,
                              split_min_chars=50, split_three_chars=140)
        calls = len(pipeline.client.calls)
        values.update(split_replies=False, delay_user=True, read_seconds=.4, bubble_max_seconds=.7)
        assert client.put('/api/settings', json={'display': values}).status_code == 200
        assert client.get('/api/settings').json()['display'] == values
        assert load_config(data_dir=settings).display.model_dump() == values
        assert len(pipeline.client.calls) == calls


@pytest.mark.parametrize('values', [dict(read_seconds=-1), dict(typing_seconds=31),
    dict(user_delay_seconds=6), dict(bubble_max_seconds=-.1), dict(split_replies='yes'),
    dict(delay_user=1), dict(unknown=True)])
def test_invalid_display_leaves_settings_unchanged(pipeline, tmp_path, values):
    with TestClient(create_app(pipeline, settings_dir=tmp_path/'settings', work_root=tmp_path/'work')) as client:
        before = client.get('/api/settings').json()['display']
        assert client.put('/api/settings', json={'display': values}).status_code == 422
        assert client.get('/api/settings').json()['display'] == before
