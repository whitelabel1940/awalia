"""AD1 persistence, validation, current readers and delivery; FakeLLMClient only."""
import json
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from cbs import clock
from cbs.config import RequestsConfig
from cbs.llm import FakeLLMClient
from cbs.pipeline import Check, Extract
from cbs.requests import base_state, effective_character, prompt_context
from cbs.runtime import character_prefix, read_prompt
from cbs.store import Store
from cbs.web import create_app
from test_fb2 import partner_files


def save(p, texts=(), fields=None):
    state = p.store.current_requests(p.character)
    return p.store.save_requests(p.character, {**state['fields'], **(fields or {})},
        [{'text': text} for text in texts], p.config.requests, state['version'])


def apply(p, operations, version=None):
    state = p.store.current_requests(p.character)
    sid = p.store.create_session(p.config.character.id)['id']
    tid = p.store.add_turn(sid, 'テストのお願い', 'わかった。', {})
    with p.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        result = p.store.apply_request_operations(db, p.character, tid, operations, p.config.requests,
                                                state['version'] if version is None else version)
    return result, tid


def test_versions_undo_restart_isolation_and_originals(pipeline, tmp_path):
    p = pipeline
    before = p.character.model_dump(mode='json')
    root = tmp_path / 'characters'
    directory = partner_files(root, p.character, 'fixture')
    originals = {f.name: f.read_bytes() for f in directory.iterdir()}
    initial = base_state(p.character)
    assert p.store.current_requests(p.character)['version'] == 0
    with clock.at('2026-10-20 12:00'):
        first = save(p, ['絵文字を使わない'], {'first_person': 'ぼく'})
    assert first['source'] == 'settings' and first['updated_at'].startswith('2026-10-20')
    assert Store(p.store.path).current_requests(p.character) == first
    other = p.character.model_copy(deep=True)
    other.model.character_id = 'other'
    assert not p.store.current_requests(other)['lines']
    second = save(p, ['先に結論から答える'])
    back = p.store.save_requests(p.character, {}, [], p.config.requests, second['version'], undo=True)
    assert back['fields'] == first['fields'] and back['lines'] == first['lines']
    assert back['version'] > second['version'] and back['source'] == 'undo'
    assert p.character.model_dump(mode='json') == before
    assert {f.name: f.read_bytes() for f in directory.iterdir()} == originals
    with p.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM request_versions').fetchone()[0] == 3
    assert initial['fields']['first_person'] != 'ぼく'


def test_first_undo_restores_creation_values(pipeline):
    first = save(pipeline, ['短く答える'])
    back = pipeline.store.save_requests(pipeline.character, {}, [], pipeline.config.requests, first['version'], undo=True)
    assert {k: back[k] for k in ('fields', 'lines')} == base_state(pipeline.character)


@pytest.mark.parametrize('fields,lines', [({'unknown': 'x'}, []), ({}, [{'text': ''}]),
    ({}, [{'text': 'a\nb'}]), ({}, [{'id': 'foreign', 'text': 'x'}]),
    ({}, [{'text': 'x' * 301}]), ({}, [{'text': 'x'}] * 41), ({'first_person': 'x' * 1001}, [])])
def test_settings_reject_invalid_without_version(pipeline, fields, lines):
    p = pipeline
    with pytest.raises(ValueError):
        p.store.save_requests(p.character, {**base_state(p.character)['fields'], **fields}, lines, p.config.requests, 0)
    assert p.store.current_requests(p.character)['version'] == 0


def test_extract_validates_each_operation_and_never_deletes_settings(pipeline):
    p = pipeline
    p.config.requests = RequestsConfig(max_lines=2, max_line_chars=30)
    first = save(p, ['絵文字を使わない'])
    ref = first['lines'][0]['id']
    result, tid = apply(p, [
        {'operation': 'delete', 'id': ref},
        {'operation': 'replace', 'id': 'missing', 'text': 'x', 'explicit_conflict': True, 'reason': '本人の変更'},
        {'operation': 'replace', 'id': ref, 'text': 'x'},
        {'operation': 'set_field', 'field': 'life_history', 'text': 'x'},
        {'operation': 'set_field', 'field': 'first_person', 'text': 'ぼく'},
        {'operation': 'add', 'text': '結論から答える'},
        {'operation': 'add', 'text': '上限超え'},
        {'operation': 'add', 'text': 'x' * 31},
    ])
    state = p.store.current_requests(p.character)
    assert result['applied'] == [4, 5] and len(result['rejected']) == 6
    assert state['lines'][0] == first['lines'][0]
    assert state['source_turn'] == tid and state['source'] == 'conversation'
    result, tid = apply(p, [{'operation': 'replace', 'id': ref, 'text': '絵文字は使ってよい',
                           'explicit_conflict': True, 'reason': '今回本人が前の禁止を変更した'}])
    state = p.store.current_requests(p.character)
    assert result['applied'] == [0] and state['lines'][0]['id'] == ref
    assert state['lines'][1]['text'] == '結論から答える'
    assert p.store.request_notices('fixture')[tid] == ['お願いを書き換えました']


def test_stale_extraction_cannot_overwrite_settings(pipeline):
    first = save(pipeline, ['設定で書いた行'])
    save(pipeline, ['新しく設定した行'])
    result, _ = apply(pipeline, [{'operation': 'add', 'text': '古い抽出'}], first['version'])
    assert result['rejected'] and not result['applied']
    assert [l['text'] for l in pipeline.store.current_requests(pipeline.character)['lines']] == ['新しく設定した行']


def test_concurrent_settings_preserves_one_version_and_rejects_foreign_turn(pipeline):
    p = pipeline
    fields = base_state(p.character)['fields']
    def write(text):
        try:
            return p.store.save_requests(p.character, fields, [{'text': text}], p.config.requests, 0)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ['先に結論から', '絵文字なし']))
    assert sum(r is not None for r in results) == 1
    sid = p.store.create_session('other')['id']
    tid = p.store.add_turn(sid, '別の人物への頼み', '了解。', {})
    with p.store.connect() as db:
        current = p.store.current_requests(p.character)
        result = p.store.apply_request_operations(db, p.character, tid,
            [{'operation': 'add', 'text': '混ざらない'}], p.config.requests, current['version'])
    assert result['rejected'] and not result['applied']
    assert p.store.current_requests(p.character) == current


def test_extract_persists_after_reply_no_extra_llm_and_next_turn_reads_it(pipeline):
    p = pipeline
    entered, release = Event(), Event()
    class BlockingFake(FakeLLMClient):
        def generate_json(self, model, system, messages, schema):
            if schema is Extract and not release.is_set():
                entered.set()
                assert release.wait(10)
            return super().generate_json(model, system, messages, schema)
    p.client = BlockingFake(json_responses=[{}, {'problems': []}, {}, {'problems': []}],
        text_responses=['ぼくで話すね。', 'ぼくもそう思う。'], extract_responses=[{
            'ledger_updates': [], 'new_canon': [], 'disclosures': [], 'used_memories': [],
            'request_operations': [{'operation': 'set_field', 'field': 'first_person', 'text': 'ぼく'},
                                   {'operation': 'add', 'text': '絵文字を使わない'}]}])
    events = []
    sid = p.store.create_session('fixture')['id']
    try:
        reply = p.run(sid, 'これから一人称はぼく、絵文字は使わないで', event_sink=lambda k, v: events.append((k, v)))
        assert entered.wait(5)
        assert any(k == 'reply' for k, _ in events)
        assert not p.store.current_requests(p.character)['lines']
    finally:
        release.set()
    p.wait_for_notice(reply['turn_id'])
    notices = [v for k, v in events if k == 'partner_requests']
    assert notices[0]['turn_id'] == reply['turn_id'] and len(notices[0]['notices']) == 2
    assert [k for k, _ in events].index('reply') < [k for k, _ in events].index('partner_requests')
    result = p.run(sid, 'おはよう')
    p.wait_for_steward()
    trace = p.store.get_trace(result['turn_id'])
    assert trace['Packet']['output']['expression']['voice']['first_person'] == 'ぼく'
    for step in ('Actor', 'Check', 'Extract'):
        system = trace[step]['request']['system']
        assert '絵文字を使わない' in system and 'ぼく' in system
        assert system.index('static_character') < system.index('{"partner_requests":')
    assert trace['Commit']['output']['requests']['applied'] == []
    assert len([c for c in p.client.calls if c.get('schema', {}).get('title') == 'Extract']) == 2


def test_check_request_violation_uses_existing_recovery(pipeline):
    p = pipeline
    save(p, ['絵文字を使わない'])
    p.client = FakeLLMClient(text_responses=['うん😊', 'うん。'], json_responses=[{},
        {'problems': [{'text': 'うん😊', 'verdict': 'forbidden', 'reason': 'お願いに反して絵文字がある'}]}, {'problems': []}])
    result = p.run(p.store.create_session('fixture')['id'], '元気？')
    p.wait_for_steward()
    assert result['assistant_text'] == 'うん。'
    trace = p.store.get_trace(result['turn_id'])
    assert trace['Safety']['accepted_check'] == 'Check-2'
    assert '絵文字を使わない' in trace['Rewrite']['request']['system']


@pytest.mark.parametrize('field,old,new,user,reply', [
    ('first_person', '私', 'ぼく', '一人称はぼくにして。', 'わかった、これからはぼくって言うね。'),
    ('register', '柔らかいタメ口', '敬語', 'これからは敬語で話して。', 'これからは敬語でお話ししますね。'),
    (None, '絵文字を使わない', '絵文字を使ってよい', 'これからは絵文字を使っていいよ。', 'わかった、これからは使うね😊'),
])
def test_current_request_change_reaches_check_and_is_accepted_without_rewrite(pipeline, field, old, new, user, reply):
    p = pipeline
    initial = save(p, [] if field else [old], {field: old} if field else {})
    operation = ({'operation': 'set_field', 'field': field, 'text': new} if field else
                 {'operation': 'replace', 'id': initial['lines'][0]['id'], 'text': new,
                  'explicit_conflict': True, 'reason': '本人が今回禁止を許可へ変更した'})
    checks = []
    class ChangeAcceptingFake(FakeLLMClient):
        def generate_json(self, model, system, messages, schema):
            if schema is Check:
                data = json.loads(messages[0]['content'])
                checks.append(data)
                # Check has the old snapshot AND the current change/acceptance.
                assert data['user_text'] == user and data['assistant_text'] == reply
                prior = data['packet']['partner_requests']
                assert (prior['fields'][field] if field else prior['lines'][0]['text']) == old
                assert p.store.current_requests(p.character)['version'] == initial['version']
                assert data['ai_question'] and 'secrets' in data
                assert '古い保存値との食い違いだけでは forbidden / contradicted にしない' in system
                assert '安全の決まり・Revealの開示段階・ai_questionが常に優先' in system
                # This fixture approves the requested change under that rule.
            return super().generate_json(model, system, messages, schema)
    p.client = ChangeAcceptingFake(text_responses=[reply], json_responses=[{}, {'problems': []}],
        extract_responses=[{'ledger_updates': [], 'new_canon': [], 'disclosures': [], 'used_memories': [],
                            'request_operations': [operation]}])
    result = p.run(p.store.create_session('fixture')['id'], user)
    p.wait_for_steward()
    trace = p.store.get_trace(result['turn_id'])
    assert len(checks) == 1 and result['assistant_text'] == reply
    assert not {'Rewrite', 'Rewrite-2', 'Deflection', 'Safety'} & trace.keys()
    current = p.store.current_requests(p.character)
    assert (current['fields'][field] if field else current['lines'][0]['text']) == new
    assert p.store.request_notices('fixture', result['turn_id'])[result['turn_id']]


@pytest.mark.parametrize('origin', ['settings', 'conversation'])
def test_calling_override_replaces_hidden_alternatives_and_undo_restores_them(pipeline, origin):
    p = pipeline
    original = {'default': '呼びかけない', 'when_needed': 'キミ', 'formal': 'あなた'}
    p.character.expression.voice.second_person = original.copy()
    before = p.character.expression.model_dump(mode='json', by_alias=True)
    # Changing another field must leave the original calling alternatives alone.
    save(p, fields={'first_person': 'ぼく'})
    assert p.store.current_character(p.character).expression.voice.second_person == original
    if origin == 'settings':
        state = save(p, fields={'second_person': 'アキと呼ぶ'})
    else:
        apply(p, [{'operation': 'set_field', 'field': 'second_person', 'text': 'アキと呼ぶ'}])
        state = p.store.current_requests(p.character)
    effective = p.store.current_character(p.character)
    assert effective.expression.voice.second_person == {'default': 'アキと呼ぶ', 'when_needed': 'アキと呼ぶ'}
    assert effective.expression.voice.length['expand_when'] == before['voice']['length']['expand_when']
    assert effective.expression.voice.sentence_endings == before['voice']['sentence_endings']
    assert p.character.expression.model_dump(mode='json', by_alias=True) == before
    # Existing AD1 snapshots also get corrected at read time, without a DB migration.
    legacy = {k: v for k, v in state.items() if k != 'second_person_overridden'}
    assert effective_character(p.character, legacy).expression.voice.second_person == effective.expression.voice.second_person
    # Explicitly returning the visible field to its old value must not revive キミ.
    returned = save(p, fields={'second_person': original['default']})
    assert p.store.current_character(p.character).expression.voice.second_person['when_needed'] == original['default']
    # Restore the immediate prior snapshot, then independently test original restoration.
    p.store.save_requests(p.character, {}, [], p.config.requests, returned['version'], undo=True)
    assert p.store.current_character(p.character).expression.voice.second_person['when_needed'] == 'アキと呼ぶ'


def test_calling_first_undo_and_same_default_explicit_request(pipeline):
    p = pipeline
    original = {'default': '呼びかけない', 'when_needed': 'キミ'}
    p.character.expression.voice.second_person = original.copy()
    first = save(p, fields={'second_person': 'アキと呼ぶ'})
    p.store.save_requests(p.character, {}, [], p.config.requests, first['version'], undo=True)
    assert p.store.current_character(p.character).expression.voice.second_person == original
    apply(p, [{'operation': 'set_field', 'field': 'second_person', 'text': original['default']}])
    assert p.store.current_character(p.character).expression.voice.second_person == {
        'default': '呼びかけない', 'when_needed': '呼びかけない'}


def test_proactive_and_other_context_readers_use_current_fields(pipeline):
    p = pipeline
    save(p, ['先に結論を話す'], {'first_person': 'ぼく', 'second_person': 'アキ', 'register': '丁寧', 'length': '短く'})
    p.config.autonomy.opening = 'normal'
    p.client = FakeLLMClient(proactive_responses=[{'speak': True, 'reason': '挨拶'}],
                             text_responses=['アキ、おはよう。'], json_responses=[{'problems': []}])
    with clock.at('2026-10-21 12:00'):
        result = p.proactive(p.store.create_session('fixture')['id'])
    p.wait_for_steward()
    assert result['speak']
    packet = p.store.get_trace(result['turn_id'])['Packet']['output']
    assert packet['expression']['voice']['first_person'] == 'ぼく'
    assert packet['expression']['voice']['second_person']['default'] == 'アキ'
    assert packet['length_hint'] == '短く'
    assert '先に結論を話す' in next(c for c in p.client.calls if c.get('schema', {}).get('title') == 'Proactive')['system']
    assert character_prefix(p.character, actor=True, store=p.store).startswith(character_prefix(p.character, actor=True))
    current = p.store.current_character(p.character)
    assert current.expression.voice.speech_register == '丁寧'
    assert current.model == p.character.model and current.reveal == p.character.reveal
    assert current.persona == p.character.persona


def test_api_save_undo_scope_stale_notice_history(pipeline, tmp_path):
    p = pipeline
    root = tmp_path / 'characters'
    partner_files(root, p.character, 'fixture')
    partner_files(root, p.character, 'other')
    app = create_app(p, characters_root=root, settings_dir=tmp_path / 'settings', work_root=tmp_path / 'work')
    with TestClient(app) as client:
        initial = client.get('/api/partner/requests').json()
        body = {'version': 0, 'fields': initial['fields'], 'lines': [{'text': '絵文字なし'}]}
        saved = client.put('/api/partner/requests', json=body).json()
        assert saved['version'] > 0 and saved['limits']['max_lines'] == 40
        assert client.put('/api/partner/requests', json=body).status_code == 422
        assert not client.get('/api/partner/requests?character_id=other').json()['lines']
        assert client.get('/api/partner/requests?character_id=missing').status_code == 404
        assert client.post('/api/partner/requests/undo', json={'version': saved['version']}).json()['lines'] == []
        p.client = FakeLLMClient(json_responses=[{}, {'problems': []}], extract_responses=[{
            'ledger_updates': [], 'new_canon': [], 'disclosures': [], 'used_memories': [],
            'request_operations': [{'operation': 'add', 'text': '結論から'}]}])
        sid = p.store.create_session('fixture')['id']
        response = client.post(f'/api/sessions/{sid}/messages/stream', json={'content': '結論から頼むね'})
        assert response.status_code == 200
        assert response.text.index('event: reply') < response.text.index('event: partner_requests')
        turns = client.get(f'/api/sessions/{sid}/turns').json()
        assert turns[0]['request_notices'] == ['お願いとして覚えました']


def test_invalid_extract_audit_and_no_daily_cap(pipeline):
    for i in range(2):
        pipeline.client = FakeLLMClient(json_responses=[{}, {'problems': []}], extract_responses=[{
            'ledger_updates': [], 'new_canon': [], 'disclosures': [], 'used_memories': [],
            'request_operations': [{'operation': 'add', 'text': str(i)}, {'operation': 'set_field', 'field': 'secrets', 'text': 'x'}]}])
        result = pipeline.run(pipeline.store.create_session('fixture')['id'], '頼むね')
        pipeline.wait_for_steward()
        audit = pipeline.store.get_trace(result['turn_id'])['Commit']['output']['requests']
        assert audit['applied'] == [0] and audit['rejected'][0]['index'] == 1
        assert pipeline.store.request_notices('fixture')[result['turn_id']]


def test_prompts_delegate_meaning_and_protect_precedence():
    assert 'その場限り' in read_prompt('extract')
    assert 'explicit_conflict' in read_prompt('extract')
    for prompt in ('episodizer', 'knowing'):
        assert 'interaction' in read_prompt(prompt) and '同じ' in read_prompt(prompt)
    context = prompt_context({'fields': {}, 'lines': []})
    assert all(word in context for word in ('Reveal', 'ai_question', '人物の芯', '安全'))
