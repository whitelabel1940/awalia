"""M9c quotation, profile, compatibility and safe job failures (Fake only)."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from cbs.bootstrap import decode_answers, load_questionnaire, quote_matches
from cbs.bootstrap_schema import Answer, ReaderOutput
from cbs.llm import FakeLLMClient
from test_bootstrap import bootstrap, minimal_input, package_data, answered_input
from test_web_bootstrap import app_for, finish


def item(quote, **changes):
    return {'id': 'text:1', 'category': 'now', 'quote': quote, 'content': '静かな暮らし', 'covers': [], **changes}


@pytest.mark.parametrize('text,quote', [
    ('- 朝はお茶を飲む\n- 夜は本を読む', '朝はお茶を飲む 夜は本を読む'),
    ('  ・ 朝はお茶を飲む\r\n * 夜は本を読む', '朝はお茶を飲む\n夜は本を読む'),
    ('1. 朝はお茶を飲む\n2) 夜は本を読む', '朝はお茶を飲む夜は本を読む'),
    ('（1） 朝はお茶を飲む\n(2) 夜は本を読む', '朝はお茶を飲む 夜は本を読む'),
])
def test_quotes_normalize_formatting_only(text, quote, tmp_path):
    fake = FakeLLMClient(json_responses=[{'items': [item(quote)], 'mentions': [
        {'domain': 'R01', 'kind': 'trait_only', 'quotes': [quote]}]}])
    result = bootstrap(tmp_path, fake).plan(minimal_input().model_copy(update={'text': text}))
    assert len(result['reader']['items']) == len(result['reader']['mentions']) == 1
    assert result['warnings'] == [] and len(fake.calls) == 2
    assert not quote_matches('朝はコーヒーを飲む', text)
    assert not quote_matches('朝はお茶を飲む。夜は本を読む', text)
    assert not quote_matches(' - \n * ', text)
    assert not quote_matches('朝はお茶を飲む夜は本を読む', '朝はお茶を飲む。散歩する。夜は本を読む')


def test_reader_retries_with_problems_and_full_previous_output(tmp_path):
    bad = {'items': [item('作者が書いていない')], 'conflicts': []}
    good = {'items': [item('本を読む')]}
    fake = FakeLLMClient(json_responses=[bad, good])
    result = bootstrap(tmp_path, fake).plan(minimal_input().model_copy(update={'text': '夜は本を読む'}))
    assert result['warnings'] == [] and result['reader']['items'][0]['quote'] == '本を読む'
    context = json.loads(fake.calls[1]['messages'][0]['content'])
    assert context['validation_problems'] and context['previous_reader']['items'] == bad['items']
    assert len(fake.calls) == 3


def test_normalized_mention_still_links_builder_evidence(tmp_path):
    data = minimal_input().model_copy(update={'text': '- 相手が落ち込んだ時\n- ゆっくり話を聞く'})
    reader = {'items': [item(data.text)], 'mentions': [
        {'domain': 'R01', 'kind': 'text', 'quotes': ['相手が落ち込んだ時 ゆっくり話を聞く']}]}
    package = package_data(data)
    domain = next(d for d in package['model']['domains'] if d['id'] == 'R01')
    domain.update(status='supported', known='話を聞く', evidence=['text:1'])
    package['expression']['items'] = [{'ref': 'text:1', 'content': '静かな暮らし', 'elaborate': 'may_elaborate'}]
    fake = FakeLLMClient(json_responses=[reader, package, {'problems': []},
                                       {'introduction': '紹介', 'open_now': []}])
    assert bootstrap(tmp_path, fake).create(data).is_dir()
    assert len(fake.calls) == 5


def test_reader_drops_invalid_entries_without_failing_plan_or_create(tmp_path):
    bad = {'items': [item('本を読む'), item('ねつ造', id='text:2'), item('本を読む'),
                     item('本を読む', id='text:3', covers=['life:Q99'])],
           'mentions': [{'domain': 'R01', 'kind': 'text', 'quotes': ['ねつ造']}],
           'profile': {'name': {'value': '架空', 'quote': 'ねつ造'}, 'world': {
               'era': {'value': '現代', 'quote': '現代'}}}}
    data = minimal_input().model_copy(update={'text': '現代。夜は本を読む'})
    fake = FakeLLMClient(json_responses=[bad, bad])
    engine = bootstrap(tmp_path, fake)
    result = engine.plan(data)
    assert len(result['reader']['items']) == 1 and not result['reader']['mentions']
    assert result['coverage']['R01'] == 'open'
    assert result['reader']['profile']['name'] is None
    assert result['reader']['profile']['world']['era']['value'] == '現代'
    assert len(result['warnings']) == 5 and len(fake.calls) == 3
    trace = json.loads(next((tmp_path / 'traces').glob('plan_*.json')).read_text(encoding='utf-8'))
    assert next(t['warnings'] for t in trace if 'warnings' in t) == result['warnings']
    # Every invalid item can be discarded and the normal Builder still succeeds.
    fake = FakeLLMClient(json_responses=[{'items': [item('ねつ造')]}, {'items': [item('ねつ造')]},
        package_data(data), {'problems': []}, {'introduction': '紹介', 'open_now': []}])
    assert bootstrap(tmp_path, fake).create(data).is_dir()
    assert len(fake.calls) == 6


def test_profile_read_before_name_has_quoted_fields_and_no_guessed_defaults(pipeline, tmp_path):
    app, _ = app_for(pipeline, tmp_path)
    reader = {'profile': {'name': {'value': 'ユキ', 'quote': '名前はユキ'}, 'world': {
        'region': {'value': '北の国', 'quote': '北の国で暮らす'}}}}
    pipeline.client = FakeLLMClient(json_responses=[reader])
    with TestClient(app) as client:
        job = finish(client, client.post('/api/bootstrap/read', json={
            'id': 'new_partner', 'text': '名前はユキ。北の国で暮らす。'}))
        assert job['status'] == 'done'
        profile = job['result']['reader']['profile']
        assert profile['name']['value'] == 'ユキ'
        assert profile['world']['region']['value'] == '北の国'
        assert all(profile['world'][axis] is None for axis in ('era', 'being', 'reality', 'contact'))
        empty = finish(client, client.post('/api/bootstrap/read', json={'id': 'new_partner'}))
        assert empty['result']['reader']['profile'] is None
        assert client.post('/api/bootstrap/read', json={'id': '../bad', 'text': 'x'}).status_code == 422
    assert ReaderOutput().profile is None and len(pipeline.client.calls) == 2


def test_life_choices_unique_and_every_legacy_letter_decodes():
    questionnaire = load_questionnaire()
    life = next(section for section in questionnaire['sections'] if section['id'] == 'life')
    for q in life['questions']:
        labels = list(q['choices'].values())
        assert len(labels) == len(set(labels))
        for old, new in q['legacy_choices'].items():
            assert old not in q['choices']
            raw = Answer(answer=[old], text='補足')
            data = minimal_input().model_copy(update={'life': {q['id']: raw}})
            decoded = decode_answers(data, questionnaire)[f'life:{q["id"]}']
            assert decoded['answer'] == [new] and decoded['open']
            assert decoded['choices'] == [questionnaire['common_choices'][new].replace('{name}', data.basics.name)]
            assert raw.answer == [old]  # Never rewrite source/input objects.


@pytest.mark.parametrize('operation,stage,responses', [
    ('plan', 'reading', ['{} INVALID', '{} INVALID']),
    ('adaptive', 'questions', ['{} INVALID']),
    ('create', 'building', ['{} INVALID', '{} INVALID']),
    ('create', 'validation', [package_data(), '{} INVALID']),
])
def test_failed_jobs_identify_stage_without_raw_exception(pipeline, tmp_path, operation, stage, responses):
    app, _ = app_for(pipeline, tmp_path)
    pipeline.client = FakeLLMClient(json_responses=deepcopy(responses))
    data = answered_input() if operation == 'adaptive' else minimal_input()
    if operation == 'plan':
        data = data.model_copy(update={'text': '本文'})
    with TestClient(app) as client:
        job = finish(client, client.post('/api/bootstrap/' + operation, json=data.model_dump(mode='json', by_alias=True)))
    assert job['status'] == 'failed' and job['stage'] == stage
    assert 'で止まりました' in job['error']
    assert 'JSON' not in job['error'] and 'INVALID' not in json.dumps(job)


def test_welcome_leave_open_questions_never_count_as_unanswered(pipeline, tmp_path):
    app, root = app_for(pipeline, tmp_path)
    data = minimal_input()
    questionnaire = load_questionnaire()
    data.answers = {q['id']: ['leave_open'] for section in questionnaire['sections']
                    if section['id'] in ('relationship', 'depth') for q in section['questions']}
    pipeline.client = FakeLLMClient(json_responses=[package_data(data), {'problems': []},
                                                   {'introduction': '紹介', 'open_now': []}])
    with TestClient(app) as client:
        draft = finish(client, client.post('/api/bootstrap/create', json=data.model_dump(mode='json', by_alias=True)))
        assert draft['status'] == 'done'
        assert client.post(f'/api/bootstrap/jobs/{draft["id"]}/confirm', json={}).status_code == 200
        assert client.get(f'/api/characters/{data.id}/augment').json()['questions'] == []
    assert (root / data.id / 'source' / 'input.json').is_file()


def test_feature_update_preserves_legacy_accent(pipeline, tmp_path):
    app, _ = app_for(pipeline, tmp_path)
    with TestClient(app) as client:
        assert client.put('/api/look/meta', json={'accent': 'akane', 'features': '短い髪'}).status_code == 200
        updated = client.put('/api/look/meta', json={'features': '長い髪'}).json()
        assert updated['accent'] == 'madder' and updated['features'] == '長い髪'


def test_activity_note_entries_title_summary_impression(pipeline):
    from cbs import clock
    from cbs.note import fill_activity_notes, public_note
    cid = pipeline.config.character.id
    with pipeline.store.connect() as db:
        run = db.execute("INSERT INTO inner_life_runs (character_id, created_at, action, reason, status, trace_json) VALUES (?, ?, 'research', 'r', 'completed', '{}')",
                         (cid, clock.iso())).lastrowid
        db.execute("INSERT INTO activities (character_id, run_id, kind, topic, summary, sources_json, created_at, status) VALUES (?, ?, 'research', '和洋折衷', '**分かったこと** 着物にブーツを合わせる人が増えている。', ?, '2026-10-14T10:00:00+09:00', 'completed')",
                   (cid, run, '[{"title": "着物の記事", "uri": "https://example.com"}]'))
    pipeline.client.activity_note_responses = [{"title": "秋の和洋折衷", "summary": "着物にブーツを合わせる着こなしが広がっている。", "impression": "自分でも試してみたいと感じたようだ。"}]
    fill_activity_notes(pipeline)
    fill_activity_notes(pipeline)
    entries = public_note(pipeline)["activities"]
    assert entries == [{"title": "秋の和洋折衷", "summary": "着物にブーツを合わせる着こなしが広がっている。",
                        "impression": "自分でも試してみたいと感じたようだ。", "date": "2026-10-14", "sources": [{"title": "着物の記事"}]}]
    calls = [c for c in pipeline.client.calls if c.get("schema", {}).get("title") == "ActivityNote"]
    assert len(calls) == 1
