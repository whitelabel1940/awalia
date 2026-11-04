"""Storage selection and a packaged welcome/conversation, entirely offline."""
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from cbs import paths
from cbs.config import Config, load_config
from cbs.llm import FakeLLMClient
from cbs.web import create_app
from test_bootstrap import minimal_input, package_data
from test_web_bootstrap import finish


@pytest.mark.parametrize('git_kind', ['directory', 'file', 'absent'])
def test_default_locations(tmp_path, monkeypatch, git_kind):
    monkeypatch.delenv('CBS_DATA_DIR', raising=False)
    root = tmp_path / 'application'
    root.mkdir()
    if git_kind == 'directory':
        (root / '.git').mkdir()
    elif git_kind == 'file':
        (root / '.git').write_text('gitdir: elsewhere')
    monkeypatch.setattr(paths, 'ROOT', root)
    local = tmp_path / 'local'
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    expected = local / 'Awalia' if git_kind == 'absent' else root / 'data'
    characters = expected / 'characters' if git_kind == 'absent' else root / 'characters'
    assert paths.resolve_paths() == (expected, characters)
    config = load_config()
    assert Config.model_validate(config.model_dump())._data_dir == config._data_dir == expected
    assert list(root.iterdir()) == ([] if git_kind == 'absent' else [root / '.git'])


def test_explicit_storage_and_home_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'ROOT', tmp_path)
    (tmp_path / '.git').mkdir()
    env = tmp_path / 'environment'
    cli = tmp_path / 'argument'
    monkeypatch.setenv('CBS_DATA_DIR', str(env))
    assert paths.resolve_paths() == (env, env / 'characters')
    assert paths.resolve_paths(cli) == (cli, cli / 'characters')
    assert paths.resolve_paths(cli, tmp_path / 'partners') == (cli, tmp_path / 'partners')
    monkeypatch.delenv('CBS_DATA_DIR')
    monkeypatch.delenv('LOCALAPPDATA')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'home'))
    data = tmp_path / 'home/AppData/Local/Awalia'
    assert paths.resolve_paths(root=tmp_path / 'packaged') == (data, data / 'characters')


def test_packaged_welcome_and_conversation_do_not_write_application(tmp_path, monkeypatch):
    # Replace only the storage root: bundled config/prompts/questionnaires stay readable.
    root = tmp_path / 'installed'
    root.mkdir()
    (root / 'sentinel').write_bytes(b'installed resource')
    monkeypatch.setattr(paths, 'ROOT', root)
    monkeypatch.delenv('CBS_DATA_DIR', raising=False)
    data = paths.resolve_paths()[0]
    assert data == Path(os.environ['LOCALAPPDATA']) / 'Awalia'
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    fake = FakeLLMClient(json_responses=[package_data(), {'problems': []},
                                        {'introduction': 'Welcome', 'open_now': []}])
    monkeypatch.setattr('cbs.llm.create_client', lambda config: fake)
    app = create_app()
    with TestClient(app) as client:
        assert app.state.settings_dir == data
        assert client.get('/api/partner').json() == {'partner': None}
        body = minimal_input().model_dump(mode='json', by_alias=True)
        job = finish(client, client.post('/api/bootstrap/create', json=body))
        assert job['status'] == 'done', job
        assert client.post(f"/api/bootstrap/jobs/{job['id']}/confirm").status_code == 200
        assert (data / 'characters' / body['id'] / 'model.yaml').is_file()
        fake.json_responses = [{}, {'problems': []}]
        session = client.post(f"/api/sessions?character_id={body['id']}", json={}).json()
        response = client.post(f"/api/sessions/{session['id']}/messages?character_id={body['id']}",
                               json={'content': 'Hello'})
        assert response.status_code == 200, response.text
        assert client.get(f"/api/sessions/{session['id']}/turns?character_id={body['id']}").json()
    assert (data / 'cbs.sqlite3').is_file()
    assert (data / 'settings.toml').is_file()
    assert list((data / 'web_bootstrap').rglob('*.json'))
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_explicit_data_is_used_by_web_runtime_and_defaults(tmp_path, monkeypatch):
    from cbs.bootstrap import Bootstrapper
    from cbs.character import load_character
    from cbs.look import character_path
    from cbs.runtime import create_pipeline
    from cbs.store import Store
    import shutil
    data = tmp_path / 'explicit'
    characters = data / 'characters'
    shutil.copytree(paths.ROOT / 'tests/fixtures/characters/fixture', characters / 'fixture')
    monkeypatch.setenv('CBS_DATA_DIR', str(data))
    monkeypatch.setattr('cbs.llm.create_client', lambda config: FakeLLMClient())
    monkeypatch.setattr('cbs.runtime.create_client', lambda config: FakeLLMClient())
    assert load_character('fixture').model.character_id == 'fixture'
    assert character_path(None, 'fixture') == (characters / 'fixture').resolve()
    assert Store().path == data / 'cbs.sqlite3'
    bootstrap = Bootstrapper(load_config(), FakeLLMClient())
    assert bootstrap.characters_root == characters
    assert bootstrap.trace_root == data / 'bootstrap'
    app = create_app(settings_dir=data)
    with TestClient(app) as client:
        assert client.get('/api/partner').json()['partner'] is not None
        assert app.state.settings_dir == data
    pipeline = create_pipeline('fixture')
    try:
        assert pipeline.characters_root == characters
        assert pipeline.store.path == data / 'cbs.sqlite3'
    finally:
        pipeline.wait_for_steward()
        pipeline._embedding_warmer.wait()


@pytest.mark.parametrize('option', ['--data-dir', '--db'])
def test_web_cli_isolates_partners_without_characters_argument(tmp_path, monkeypatch, option):
    from cbs.web import __main__ as main
    data = tmp_path / 'cli'
    target = data if option == '--data-dir' else data / 'check.sqlite3'
    monkeypatch.setattr(sys, 'argv', ['cbs.web', option, str(target)])
    monkeypatch.setattr('cbs.llm.create_client', lambda config: FakeLLMClient())

    class Server:
        def __init__(self, config):
            self.config = config

        def run(self):
            app = self.config.app
            assert app.state.settings_dir == data
            assert app.state.pipeline.characters_root == data / 'characters'
            assert app.state.pipeline.store.path == (target if option == '--db' else data / 'cbs.sqlite3')
            with TestClient(app) as client:
                assert client.get('/api/partner').json() == {'partner': None}

    monkeypatch.setattr(main.uvicorn, 'Server', Server)
    main.main()
