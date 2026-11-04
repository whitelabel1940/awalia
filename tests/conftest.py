import pytest

from cbs.character import load_character
from cbs.config import ROOT, load_config
from cbs.llm import FakeLLMClient
from cbs.pipeline import TurnPipeline
from cbs.store import Store


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path_factory):
    # Tests never read the user's data/settings.toml, data/secrets.env or provider keys.
    monkeypatch.setenv("CBS_DATA_DIR", str(tmp_path_factory.mktemp("cbs_data")))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path_factory.mktemp("localappdata")))
    for name in ("GEMINI_API_KEY", "GPT_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    # Fails closed if any test accidentally constructs a real provider.
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must use FakeLLMClient")
    import google.genai
    import openai
    import cbs.llm.anthropic
    monkeypatch.setattr(google.genai, "Client", forbidden)
    monkeypatch.setattr(openai, "OpenAI", forbidden)
    monkeypatch.setattr(cbs.llm.anthropic, "AnthropicLLMClient", forbidden)


@pytest.fixture
def character():
    return load_character("fixture", root=ROOT / "tests/fixtures/characters")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "cbs.sqlite3")


@pytest.fixture
def pipeline(character, store, tmp_path):
    # UI-saved settings on this computer must not change the suite's default budgets/timing.
    config = load_config(data_dir=tmp_path / 'settings')
    config.character.id = character.model.character_id
    pipeline = TurnPipeline(config, character, store, FakeLLMClient())
    # BK1 integration tests only export isolated character copies, never live trees.
    import shutil
    from cbs.config import ROOT
    pipeline.characters_root = tmp_path / 'backup_fixture_characters'
    directory = pipeline.characters_root / character.model.character_id
    directory.mkdir(parents=True)
    for source in (ROOT / 'tests' / 'fixtures' / 'characters' / character.model.character_id).glob('*.yaml'):
        shutil.copy2(source, directory / source.name)
    shutil.copytree(ROOT / 'tests' / 'fixtures' / 'characters' / character.model.character_id / 'source', directory / 'source')
    pipeline.wait_for_steward()
    pipeline._embedding_warmer.wait()
    pipeline.client.calls.clear()
    pipeline.client.embedding_calls.clear()
    yield pipeline
    pipeline.wait_for_steward()
    pipeline._embedding_warmer.wait()


@pytest.fixture(autouse=True)
def offline_news(monkeypatch):
    async def unavailable(url):
        raise OSError('offline test')
    monkeypatch.setattr('cbs.news.fetch_feed', unavailable)


@pytest.fixture(autouse=True)
def offline_setup_helper(monkeypatch):
    async def unavailable(url):
        raise OSError('offline test')
    monkeypatch.setattr('cbs.web.setup_helper.fetch_guide', unavailable)


@pytest.fixture(autouse=True)
def offline_telemetry(monkeypatch):
    async def unavailable(host, key, batch):
        raise OSError('offline test')
    monkeypatch.setattr('cbs.telemetry.send_batch', unavailable)
