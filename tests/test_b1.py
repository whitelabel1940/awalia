"""B1 state/record contracts. FakeLLMClient and in-memory HTTP responses only."""
import io
import json
import math
import threading

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from cbs import clock
from cbs.bootstrap_schema import BootstrapInput
from cbs.character import load_character
from cbs.config import MemoryConfig, archived_margin, load_config
from cbs.embedding_download import EmbeddingInstaller
from cbs.llm import FakeLLMClient
from cbs.llm.local import MODELS, model_files
from cbs.memory import retrieve
from cbs.storage import pack_vector
from cbs.web import create_app
from test_bootstrap import bootstrap, package_data


def test_review_recovers_voice_and_projects_it_to_runtime(tmp_path):
    text = '一人称は私。普段は呼ばず必要時はあなた。柔らかな敬語。普段は二文、感動した時は長く。'
    data = BootstrapInput(id='voice_test', basics={'name': 'Test'}, text=text, now={'likes': [{'topic': 'Tea', 'elaborate': 'keep'}]})
    quoted = lambda value, quote: {'value': value, 'quote': quote}
    review = {'voice': {
        'first_person': quoted('私', '一人称は私'),
        'second_person_default': quoted('普段は呼ばない', '普段は呼ばず'),
        'second_person_when_needed': quoted('あなた', '必要時はあなた'),
        'register': quoted('柔らかな敬語', '柔らかな敬語'),
        'length_default': quoted('二文', '普段は二文'),
        'length_expand_when': quoted('感動した時は長く', '感動した時は長く')},
        'missing_items': [{'id': 'arbitrary', 'category': 'expression', 'content': '話し方', 'quote': text}],
        'setting_conflicts': []}
    package = package_data(data)
    package['expression']['items'] = [{'ref': 'text:review:1', 'content': '話し方', 'elaborate': 'may_elaborate'}]
    fake = FakeLLMClient(json_responses=[{}, package, {'problems': []}, {'introduction': '紹介', 'open_now': []}],
                         reader_review_responses=[review])
    directory = bootstrap(tmp_path, fake).create(data)
    voice = load_character(data.id, root=directory.parent).expression.voice
    assert voice.first_person == '私' and voice.second_person == {'default': '普段は呼ばない', 'when_needed': 'あなた'}
    assert voice.speech_register == '柔らかな敬語'
    assert voice.length == {'default': '二文', 'expand_when': '感動した時は長く'}
    audit_context = json.loads(fake.calls[1]['messages'][0]['content'])
    assert audit_context['ai_question'] == 'immersive' and audit_context['world']['contact'] == 'A'



def test_typed_speech_fields_win_over_reader_voice():
    from cbs.bootstrap import apply_reader_voice
    from cbs.bootstrap_schema import CharacterPackage, ReaderOutput
    data = BootstrapInput(id='voice_test', basics={'name': 'Test', 'first_person': 'あたし', 'register': 'くだけた口調'},
                          text='一人称は私。柔らかな敬語。', now={'likes': [{'topic': 'Tea', 'elaborate': 'keep'}]})
    reader = ReaderOutput.model_validate({'voice': {'first_person': {'value': '私', 'quote': '一人称は私'},
                                                    'register': {'value': '柔らかな敬語', 'quote': '柔らかな敬語'},
                                                    'length_default': {'value': '二文', 'quote': '二文'}}})
    package = apply_reader_voice(CharacterPackage.model_validate(package_data(data)), reader, data)
    voice = package.expression.voice
    assert voice.first_person == 'あたし' and voice.speech_register == 'くだけた口調'
    assert voice.length.default == '二文'


def test_review_rejects_unverifiable_voice_after_one_retry(tmp_path):
    fake = FakeLLMClient(json_responses=[{}], reader_review_responses=[
        {'voice': {'first_person': {'value': '僕', 'quote': '原文にない'}}}] * 2)
    runner = bootstrap(tmp_path, fake)
    with pytest.raises(ValueError, match='Reader review'):
        runner.read_text('お茶が好き')
    assert len(fake.calls) == 3 and not (tmp_path / 'characters').exists()


def test_first_opening_delivers_relationship_context_to_judge(pipeline):
    pipeline.character.expression.relationship_context = {'stance': '以前から知っている相手', 'not_fixed': []}
    pipeline.character.world['contact'] = 'B2'
    with clock.at('2026-10-17 14:00'):
        sid = pipeline.store.create_session('fixture')['id']
        result = pipeline.proactive(sid)
    assert not result['speak']  # The Fake's scripted judgment remains authoritative.
    call = next(c for c in pipeline.client.calls if c.get('schema', {}).get('title') == 'Proactive')
    context = json.loads(call['messages'][0]['content'])
    assert {d['id'] for d in context['relationship_domains']} == {f'R{i:02}' for i in range(1, 11)}
    assert context['gap_seconds'] is None and not context['recent_turns']
    assert '以前から知っている相手' in call['system'] and 'B2' in call['system']


def test_model_margin_precedence_and_validation(tmp_path):
    cfg = load_config(data_dir=tmp_path)
    cfg.models.embedding = 'local/ruri-v3-130m'
    assert archived_margin(cfg) == .06
    cfg.memory.archived_margins[cfg.models.embedding] = .07
    assert archived_margin(cfg) == .07
    cfg.models.embedding = 'openrouter/google/gemini-embedding-2'
    assert archived_margin(cfg) == .08
    for value in [-.1, 1.1, float('nan')]:
        with pytest.raises(ValidationError):
            MemoryConfig(archived_margins={'local/ruri-v3-130m': value})


def test_archived_retrieval_uses_local_margin_and_override(pipeline):
    pipeline.config.models.embedding = 'local/ruri-v3-130m'
    pipeline.store.embedding_model = 'local/ruri-v3-130m'
    with pipeline.store.connect() as db:
        db.execute('UPDATE embedding_index SET model=? WHERE id=1', ('local/ruri-v3-130m',))
    with clock.at('2026-10-17 14:00'):
        sid = pipeline.store.create_session('fixture')['id']
        cid = pipeline.store.prepare_conversation(sid)
        with pipeline.store.connect() as db:
            for i, score in enumerate([.64, .5]):
                db.execute("INSERT INTO memory_episodes (character_id,conversation_id,started_at,ended_at,summary,details,importance,pinned,embedding,created_at,status) VALUES ('fixture',?,'2026-01-01','2026-01-01',?,'d',.5,0,?,'2026-01-01','archived')",
                           (cid, f'episode {i}', pack_vector([score, math.sqrt(1-score**2)])))
        assert any(c['ref'] == 'memory:1' for c in retrieve(pipeline, 'query', []))
        pipeline.config.memory.archived_margins['local/ruri-v3-130m'] = .08
        assert not any(c['ref'] == 'memory:1' for c in retrieve(pipeline, 'query', []))


def test_embedding_cancel_keeps_partial_and_resumes_aggregate_progress(monkeypatch, tmp_path):
    choice = 'ruri-v3-130m'
    files = model_files(MODELS[choice])
    size = 2 * 1024 * 1024 + 3
    monkeypatch.setitem(__import__('cbs.embedding_download', fromlist=['FILE_SIZES']).FILE_SIZES, choice, [3, 3, 3, size])
    cancel = threading.Event()
    ranges, progress = [], []
    class Response(io.BytesIO):
        def __init__(self, offset):
            super().__init__(b'x' * (size-offset))
            self.status = 206 if offset else 200
            self.headers = {'Content-Length': str(size-offset)}
            if offset:
                self.headers['Content-Range'] = f'bytes {offset}-{size-1}/{size}'
    def open_url(request, **kwargs):
        offset = int((request.get_header('Range') or 'bytes=0-')[6:-1])
        ranges.append(offset)
        return Response(offset)
    monkeypatch.setattr('urllib.request.urlopen', open_url)
    installer = EmbeddingInstaller(tmp_path)
    directory = tmp_path / MODELS[choice].repo / MODELS[choice].revision
    directory.mkdir(parents=True)
    for f in files[:-1]:
        (directory / f).write_bytes(b'abc')
    def update(stage, done, total):
        progress.append((done, total))
        if done > 9:
            cancel.set()
    with pytest.raises(InterruptedError):
        installer.acquire(choice, cancel, update)
    partial = (directory / files[-1]).with_suffix('.onnx.partial')
    assert partial.stat().st_size == 1024*1024
    assert installer.plan(choice)['completed'] == 9 + 1024*1024
    cancel.clear()
    installer.acquire(choice, cancel, lambda stage, done, total: progress.append((done, total)))
    assert ranges == [0, 1024*1024] and installer.plan(choice)['ready']
    assert progress[-1] == (9+size, 9+size)


def test_embedding_api_requires_matching_capacity_token(pipeline, tmp_path):
    app = create_app(pipeline, settings_dir=tmp_path / 'settings', work_root=tmp_path / 'work')
    with TestClient(app) as client:
        assert client.get('/api/embedding/download?choice=../bad').status_code == 422
        body = {'choice': 'ruri-v3-130m'}
        assert client.post('/api/embedding/download', json=body).status_code == 409
        plan = client.post('/api/embedding/download/plan', json=body).json()
        assert plan['total'] == 140032944 and not plan['ready']
        assert not (tmp_path / 'settings/models').exists()  # Planning never acquires files.
        assert client.post('/api/embedding/download', json={**body, 'token': plan['token']}).status_code == 409
        assert client.post('/api/embedding/download', json={'choice': 'ruri-v3-30m', 'token': plan['token'], 'confirmed': True}).status_code == 409
