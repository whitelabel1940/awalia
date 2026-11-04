"""R1: neutral catalog snapshot and startup without a selected partner."""
import hashlib
import inspect
import json

import pytest
from fastapi.testclient import TestClient

from cbs.bootstrap import domain_catalog
from cbs.character import load_character
from cbs.config import CharacterConfig, ROOT, load_config
from cbs.llm import FakeLLMClient
from cbs.store import Store
from cbs.web import create_app
from cbs.web.bootstrap_host import BootstrapHost


def test_catalog_matches_pre_extraction_definitions(monkeypatch):
    # Snapshot of id/label/description (description falls back to label, as before).
    # No personality, status, evidence, or life history is part of this snapshot.
    def no_character(*args, **kwargs):
        raise AssertionError("Catalog must not load a character")
    monkeypatch.setattr("cbs.bootstrap.load_character", no_character)
    catalog = domain_catalog()
    assert len(catalog) == 34
    assert {d['id'] for d in catalog} == {f'CBS{i:02}' for i in range(1, 25)} | {f'R{i:02}' for i in range(1, 11)}
    assert all(set(d) == {'id', 'label', 'description'} for d in catalog)
    assert hashlib.sha256(json.dumps(catalog, ensure_ascii=False, sort_keys=True).encode()).hexdigest() == '54b5bf8f32ad8354cbe694c391782c8aa9956cb306d5b99384bb412ecc0b1b0a'
    fixture = load_character('fixture', root=ROOT / 'tests/fixtures/characters')
    assert catalog == [{'id': d.id, 'label': d.label, 'description': d.description} for d in fixture.model.domains]
    catalog[0]['label'] = 'changed'
    assert domain_catalog()[0]['label'] != 'changed'


@pytest.mark.parametrize('section', ['[character]\n', '[character]\nid = ""\n'])
def test_empty_character_config_starts_welcome(tmp_path, monkeypatch, section):
    config_file = tmp_path / 'config.toml'
    config_file.write_text((ROOT / 'config.toml').read_text(encoding='utf-8').replace('[character]\nid = ""', section.rstrip()), encoding='utf-8')
    cfg = load_config(config_file, data_dir=tmp_path / 'settings')
    assert cfg.character.id == CharacterConfig().id == ''
    fake = FakeLLMClient()
    monkeypatch.setattr('cbs.config.load_config', lambda **kwargs: cfg)
    monkeypatch.setattr('cbs.llm.create_client', lambda config: fake)
    app = create_app(characters_root=tmp_path / 'characters', settings_dir=tmp_path / 'settings', work_root=tmp_path / 'work')
    assert isinstance(app.state.pipeline, BootstrapHost)
    with TestClient(app) as client:
        assert client.get('/wizard').status_code == 200
        assert client.get('/api/characters').json() == []
        assert client.get('/api/partner').json()['partner'] is None
    assert not fake.calls


def test_character_and_store_ids_are_required():
    assert inspect.signature(load_character).parameters['character_id'].default is inspect.Parameter.empty
    for name in ('create_session', 'list_canon', 'list_memory_episodes', 'list_interests', 'current_feeling'):
        assert inspect.signature(getattr(Store, name)).parameters['character_id'].default is inspect.Parameter.empty
