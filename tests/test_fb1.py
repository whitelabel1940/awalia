"""FB1 contracts: isolated files, FakeLLMClient, and stubbed speech only."""
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cbs.bootstrap import decode_answers, load_questionnaire, questions_by_group
from cbs.bootstrap_schema import BootstrapInput, SettingConflict
from cbs.llm import FakeLLMClient
from cbs.web.voice_api import PreviewInput
from cbs.web.voice_text import VOICE_PREVIEW_TEXT
from test_bootstrap import bootstrap, minimal_input, package_data
from test_web_bootstrap import app_for, finish


def test_now_group_keeps_existing_answer_and_evidence_ids():
    questionnaire = load_questionnaire()
    section = next(s for s in questionnaire['sections'] if s['id'] == 'now_life')
    assert section['step'] == 'now' and section['group'] == 'now'
    groups = questions_by_group(questionnaire)
    assert set(groups['now']) == {f'L{i}now' for i in range(1, 5)}
    assert not set(groups['now']) & set(groups['answers'])
    data = minimal_input().model_copy(update={'now': minimal_input().now.model_copy(update={
        'life': {'L1now': ['A'], 'L2now': ['leave_open']}})})
    decoded = decode_answers(data, questionnaire)
    assert decoded['now:L1now']['answer'] == ['A']
    assert decoded['now:L2now']['open']


@pytest.mark.parametrize('answer', ['immersive', 'playful', 'honest'])
def test_reader_and_review_receive_explicit_setting_choices(tmp_path, answer):
    data = BootstrapInput(id='fb1_test', basics={'name': 'Test'},
        text='開発されたAI。猫耳がある。働かずに暮らす。',
        persona={'ai_question': answer},
        world={'description': '「猫耳がある」について: 見た目の表現（耳飾りなど）。'},
        now={'life': {'L1now': ['A']}},
        review_notes={'setting:chosen': '「開発されたAI」について: 正直に答える。'})
    conflict = {'topic': '仕事', 'quote': '働かずに暮らす', 'question_ref': 'now:L1now', 'answer_keys': ['A']}
    setting = {'setting': 'world', 'choice_template': 'appearance', 'topic': '見た目',
               'quote': '猫耳がある', 'detail': '現実の世界と耳の指定が食い違っています。'}
    fake = FakeLLMClient(json_responses=[{'conflicts': [conflict]}],
                         reader_review_responses=[{'conflicts': [conflict], 'setting_conflicts': [setting]}])
    plan = bootstrap(tmp_path, fake).plan(data)
    assert plan['reader']['setting_conflicts'] == [setting]
    assert plan['confirmation_required'][0]['question_ref'] == 'now:L1now'
    assert plan['confirmation_required'][0]['choices'] == ['会社などで働いている']
    for call in fake.calls:
        context = json.loads(call['messages'][0]['content'])
        assert context['ai_question'] == answer
        assert context['world'] == data.world.model_dump(exclude_none=True)
        assert context['review_notes'] == data.review_notes
    # Choices are existing persona/world inputs, surviving saved input round trips.
    assert BootstrapInput.model_validate_json(data.model_dump_json()) == data
    assert bootstrap(tmp_path, FakeLLMClient(json_responses=[{}])).read_text(None).setting_conflicts == []


def test_setting_templates_are_typed_without_free_form_questions():
    old = SettingConflict(setting='world', topic='世界', quote='引用', detail='説明')
    assert old.choice_template == 'world'
    with pytest.raises(ValidationError):
        SettingConflict(setting='world', choice_template='invented', topic='世界', quote='引用', detail='説明')


def test_draft_and_settings_voice_previews_share_file_text(pipeline, tmp_path, monkeypatch):
    app, _ = app_for(pipeline, tmp_path)
    spoken = []
    def synthesize(_pipeline, text, *args, **kwargs):
        spoken.append(text)
        return b'RIFF-fake-wave'
    monkeypatch.setattr(app.state.tts, 'synthesize', synthesize)
    pipeline.client = FakeLLMClient(json_responses=[package_data(), {'problems': []},
                                                                {'introduction': '紹介', 'open_now': []}])
    with TestClient(app) as client:
        created = finish(client, client.post('/api/bootstrap/create', json=minimal_input().model_dump(mode='json', by_alias=True)))
        assert created['status'] == 'done'
        response = client.post(f"/api/bootstrap/jobs/{created['id']}/voice-preview")
        assert response.status_code == 200 and response.content == b'RIFF-fake-wave'
        public = client.get('/api/voice').json()
        assert public['preview_text'] == VOICE_PREVIEW_TEXT == PreviewInput().text
        assert client.post('/api/voice/preview', json={}).status_code == 200
    assert spoken == [VOICE_PREVIEW_TEXT, VOICE_PREVIEW_TEXT]
    assert VOICE_PREVIEW_TEXT.startswith('おかえり、今日はどんな一日だった？')
