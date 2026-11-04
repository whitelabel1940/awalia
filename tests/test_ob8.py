import pytest
from fastapi.testclient import TestClient
from cbs.bootstrap_schema import BootstrapPersona
from cbs.character import load_character
from cbs.config import PersonaConfig
from cbs.llm import FakeLLMClient
from cbs.pipeline import PreJudge, assemble_packet
from test_bootstrap import minimal_input, package_data, bootstrap, answered_input, answered_package, validate_package, CharacterPackage, ReaderOutput, load_questionnaire, clock
from test_web_bootstrap import app_for, finish

@pytest.mark.parametrize('style', ['hinted', 'free'])
@pytest.mark.parametrize('edit_confirmation', [False, True])
def test_open_style_draft_confirm_and_api(pipeline, tmp_path, style, edit_confirmation):
    app, root = app_for(pipeline, tmp_path)
    data = minimal_input().model_copy(update={'persona': BootstrapPersona(open_style=style)})
    pipeline.client = FakeLLMClient(json_responses=[package_data(), {'problems': []}, {'introduction': 'Intro', 'open_now': []}])
    with TestClient(app) as client:
        job = finish(client, client.post('/api/bootstrap/create', json=data.model_dump(mode='json', by_alias=True)))
        assert job['status'] == 'done', job
        draft = tmp_path / 'work' / 'drafts' / job['id'] / data.id
        assert load_character(data.id, root=draft.parent).persona.open_style == style
        assert not (root / data.id).exists()
        if edit_confirmation:
            style = 'free' if style == 'hinted' else 'hinted'
        assert client.post(f"/api/bootstrap/jobs/{job['id']}/confirm", json={'open_style': style}).status_code == 200
        url = f'/api/persona?character_id={data.id}'
        assert client.get(url).json()['open_style'] == style
        assert client.put(url, json={'ai_question': 'honest'}).json()['open_style'] == style
        other = 'hinted' if style == 'free' else 'free'
        assert client.put(url, json={'ai_question': 'honest', 'open_style': other}).json()['open_style'] == other
    assert load_character(data.id, root=root).persona.open_style == other
    # Further bootstrap work inherits this partner setting without specifying a new persona.
    engine = bootstrap(tmp_path, FakeLLMClient())
    engine.characters_root = root
    assert engine.persona(minimal_input()).open_style == other

@pytest.mark.parametrize('style', [None, 'hinted', 'free'])
def test_packet_open_style(character, style):
    character.persona = PersonaConfig(open_style=style) if style else None
    domain = next(d for d in character.model.domains if d.id == 'CBS09')
    domain.soft_prior = '慎重に状況を確かめそう。'
    domain.soft_prior_evidence = ['author:R1']
    packet = assemble_packet(character, PreJudge(domains_touched=[{'id': 'CBS09', 'strength': 'direct'}]))
    touched = packet['touched_domains'][0]
    assert ('soft_prior' in touched) == (style != 'free')
    assert ('soft_prior_evidence' in touched) == (style != 'free')
    assert domain.soft_prior and domain.soft_prior_evidence  # Stored hints survive toggling.

@pytest.mark.parametrize('answer', ['unknown', 'leave_open'])
def test_legacy_answers_and_grounded_open_hints(answer):
    data = answered_input().model_copy(update={'answers': {'R1': ['A'], 'R3': [answer]}})
    data = type(data).model_validate_json(data.model_dump_json())
    assert data.answers['R3'] == [answer]
    package = answered_package(data)
    domain = next(d for d in package['model']['domains'] if d['id'] == 'R03')
    domain.update(soft_prior='まず様子を確かめそう。', soft_prior_evidence=['author:R1'])
    assert not validate_package(CharacterPackage.model_validate(package), data, ReaderOutput(), load_questionnaire(), clock.now().date())
    domain['soft_prior_evidence'] = ['author:R3']
    assert any('invalid soft_prior evidence' in p for p in validate_package(CharacterPackage.model_validate(package), data, ReaderOutput(), load_questionnaire(), clock.now().date()))
