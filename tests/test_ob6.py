import pytest

from cbs.bootstrap import decode_answers, gap_questions, load_questionnaire, questionnaire_named
from cbs.bootstrap_schema import ReaderOutput


@pytest.mark.parametrize('name,expected', [('テスト係', 'テスト係'), ('', 'あなたのパートナー')])
def test_questionnaire_name(name, expected):
    from test_bootstrap import minimal_input
    data = minimal_input()
    data.basics.name = name
    data.answers = {'R3': ['leave_open']}
    catalog = load_questionnaire()
    named = questionnaire_named(catalog, name)
    assert named['common_choices']['leave_open'] == '決めないでおく（話していくうちに分かる）'
    decoded = decode_answers(data, catalog)
    assert decoded['author:R3']['choices'][0] == '決めないでおく（話していくうちに分かる）'
    assert '{name}' not in str(decoded)
    questions = gap_questions(data, ReaderOutput(), catalog)
    assert '{name}' not in str(questionnaire_named(questions, name))
    assert 'テスト係' in str(questionnaire_named(questions, 'テスト係'))
    assert 'ユキ' in str(questionnaire_named(questions, 'ユキ'))
    assert catalog['common_choices']['leave_open'] == named['common_choices']['leave_open']


@pytest.mark.parametrize('name,expected', [('テスト係', 'テスト係'), ('', 'あなたのパートナー')])
def test_questionnaire_name_in_llm_request(name, expected, tmp_path):
    from cbs.bootstrap import Bootstrapper
    from cbs.config import load_config
    from cbs.llm import FakeLLMClient
    client = FakeLLMClient(json_responses=[{}])
    engine = Bootstrapper(load_config(data_dir=tmp_path), client)
    engine.call('bootstrap_reader', {'input': {'basics': {'name': name}},
                'questionnaire': load_questionnaire()}, ReaderOutput)
    content = str(client.calls)
    assert '{name}' not in content
    assert expected in content
