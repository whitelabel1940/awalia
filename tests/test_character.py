def test_character_seed(character, store):
    model = store.current_model(character)
    assert len(model.domains) == 34  # CBS01-24 + R01-R10
    assert len(model.signatures) == 8
    assert [d.id for d in model.domains if d.status == "open" and d.id.startswith("CBS")] == [f"CBS{i:02}" for i in range(1, 25) if i != 11]
    assert all(d.status == "open" for d in model.domains if d.id.startswith("R"))
    assert set(character.sources) == {"behavioral_seed.md", "life_seed.md", "profile.md"}
    assert character.expression.voice.first_person == "私"
    assert character.expression.voice.speech_register == "簡潔なですます調"
    model.summary.clear()
    assert character.model.summary  # current model is an independent snapshot


def test_model_strings_route_to_configured_providers():
    from cbs.config import load_config
    from cbs.llm import RoutingLLMClient
    config = load_config()
    router = RoutingLLMClient(config)
    assert router._split("openai/gpt-6-luna") == ("openai", "gpt-6-luna")
    assert router._split("gemini-3.8-flash") == ("gemini", "gemini-3.8-flash")
    assert router._split("gemma3:12b") == ("gemini", "gemma3:12b")
    assert router._split("unknown/model") == ("gemini", "unknown/model")
