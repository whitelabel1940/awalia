import base64
from io import BytesIO
import json
import zipfile

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from cbs.config import PhotoConfig, load_config
from cbs.photos import normalize_photo, vision_status, photo_path, delete_photo, PhotoDescriptions
from cbs.web import create_app


def picture(size=(200, 100), fmt='PNG', exif=None):
    image = Image.new('RGB', size, 'red')
    output = BytesIO()
    image.save(output, format=fmt, **({'exif': exif} if exif else {}))
    return base64.b64encode(output.getvalue()).decode()


def test_pixels_metadata_and_orientation():
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = 'private author'
    exif[270] = 'location should disappear'
    photo = normalize_photo(picture((2000, 1000), 'JPEG', exif), PhotoConfig(max_edge=1600))
    with Image.open(BytesIO(photo['bytes'])) as image:
        assert image.size == (800, 1600)
        assert not image.getexif()
        assert not any(k in image.info for k in ('exif', 'icc_profile', 'comment', 'xmp'))
    assert b'private author' not in photo['bytes']


@pytest.mark.parametrize('encoded,cfg', [
    ('bad', PhotoConfig()), (picture(fmt='GIF'), PhotoConfig()),
    (picture(), PhotoConfig(max_pixels=100)),
    (base64.b64encode(b'x'*1025).decode(), PhotoConfig(max_bytes=1024)),
], ids=['malformed', 'format', 'pixels', 'bytes'])
def test_reject_input(encoded, cfg):
    with pytest.raises(ValueError):
        normalize_photo(encoded, cfg)


def test_config_vision_defaults_and_no_name_heuristics(tmp_path, monkeypatch):
    config = load_config(data_dir=tmp_path)
    monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic')
    assert config.models.vision == 'openrouter/google/gemini-3.8-flash'
    assert vision_status(config)['enabled']
    config.models.vision = 'openrouter/any-new-model'
    assert not vision_status(config)['enabled']
    config.photos.candidates[config.models.vision] = True
    assert vision_status(config)['enabled']
    config.models.vision = None
    assert not vision_status(config)['enabled']
    assert all(p.models.vision for p in config.advanced.presets)


def test_one_vision_text_only_all_stages_and_export(pipeline, tmp_path):
    pipeline.config.models.actor = 'local/text-only'
    sid = pipeline.store.create_session('fixture')['id']
    result = pipeline.run(sid, '今日のごはん', photos=[picture(), picture()])
    pipeline.wait_for_steward()
    calls = pipeline.client.calls
    visions = [c for c in calls if c.get('schema', {}).get('title') == 'PhotoDescriptions']
    assert len(visions) == 1
    assert len(visions[0]['messages'][0]['content']) == 3
    trace = pipeline.store.get_trace(result['turn_id'])
    for step in ('Pre-Judge', 'Packet', 'Actor', 'Check', 'Extract'):
        assert '白い皿' in json.dumps(trace[step], ensure_ascii=False), step
        assert 'data:image' not in json.dumps(trace[step]), step
    assert 'data:image' not in json.dumps(trace)
    assert len(result['photos']) == 2
    for photo in result['photos']:
        assert photo_path(pipeline.store, photo['id']).is_file()
    pipeline.close_conversation(sid)
    pipeline.wait_for_steward()
    episodizer = next(c for c in calls if c.get('schema', {}).get('title') == 'EpisodizerOutput')
    assert '白い皿' in episodizer['messages'][-1]['content']
    assert 'private_categories' in episodizer['messages'][-1]['content']
    from cbs.backup import build_export
    target = tmp_path / 'export.zip'
    build_export(pipeline.store.path, pipeline.characters_root, pipeline.store.path.parent, 'fixture', target)
    with zipfile.ZipFile(target) as archive:
        assert len([n for n in archive.namelist() if n.startswith('data/photos/')]) == 2


def test_limits_before_call_and_missing_vision(pipeline):
    sid = pipeline.store.create_session('fixture')['id']
    pipeline.config.photos.max_count = 1
    with pytest.raises(ValueError):
        pipeline.run(sid, '写真', photos=[picture(), picture()])
    assert not pipeline.client.calls
    pipeline.config.models.vision = None
    with pytest.raises(ValueError):
        pipeline.run(sid, '', photos=[picture()])
    assert not pipeline.client.calls


def test_api_notice_history_delete_and_other_partner(pipeline, tmp_path, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic')
    app = create_app(pipeline, settings_dir=tmp_path)
    sid = pipeline.store.create_session('fixture')['id']
    with TestClient(app) as client:
        assert client.get('/api/settings').json()['photos']['enabled']
        assert not client.get('/api/settings').json()['photos']['notice_seen']
        assert client.post('/api/photos/notice').status_code == 200
        assert client.get('/api/settings').json()['photos']['notice_seen']
        response = client.post(f'/api/sessions/{sid}/messages', json={'photos': [picture(), picture()]})
        assert response.status_code == 200, response.text
        data = response.json()
        first, second = data['photos']
        pipeline.wait_for_steward()
        pipeline.close_conversation(sid)
        pipeline.wait_for_steward()
        history = client.get(f'/api/sessions/{sid}/turns').json()
        assert history[0]['user_text'] == ''
        assert len(history[0]['photos']) == 2
        assert client.get('/api/photos/' + first['id']).headers['content-type'] == 'image/jpeg'
        assert client.delete('/api/photos/' + first['id']).status_code == 200
        assert not photo_path(pipeline.store, first['id']).exists()
        assert photo_path(pipeline.store, second['id']).exists()
        history = client.get(f'/api/sessions/{sid}/turns').json()
        assert [p['id'] for p in history[0]['photos']] == [second['id']]
        assert client.get('/api/photos/' + first['id']).status_code == 404
        with pipeline.store.connect() as db:
            assert db.execute('SELECT count(*) FROM memory_episodes').fetchone()[0] == 0
            assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_no_photos_no_added_call(pipeline):
    sid = pipeline.store.create_session('fixture')['id']
    pipeline.run(sid, 'こんにちは')
    pipeline.wait_for_steward()
    assert not any(c.get('schema', {}).get('title') == 'PhotoDescriptions' for c in pipeline.client.calls)


def test_provider_image_shapes_and_trace_redaction():
    from cbs.llm import FakeLLMClient, image_messages
    from cbs.llm.openai_client import responses_messages
    from cbs.llm.anthropic import anthropic_content
    from cbs.llm.gemini import gemini_parts
    jpeg = normalize_photo(picture(), PhotoConfig())['bytes']
    messages = image_messages('photo context', [jpeg])
    assert messages[0]['content'][1]['type'] == 'image_url'
    assert responses_messages(messages)[0]['content'][1]['type'] == 'input_image'
    assert base64.b64decode(anthropic_content(messages[0]['content'])[1]['source']['data']) == jpeg
    assert gemini_parts(messages[0]['content'])[1].inline_data.data == jpeg
    result = FakeLLMClient().generate_images_json('arbitrary', 'static vision prompt', '', [jpeg], PhotoDescriptions)
    assert 'data:image' not in json.dumps(result.trace())
    assert 'photo bytes omitted' in json.dumps(result.trace())


def test_vision_failure_is_not_retried_or_sent_to_actor(pipeline):
    from cbs.llm import LLMError
    pipeline.client.vision_responses = [{'descriptions': ['one']}]
    sid = pipeline.store.create_session('fixture')['id']
    with pytest.raises(LLMError):
        pipeline.run(sid, '', photos=[picture(), picture()])
    assert len(pipeline.client.calls) == 1
    assert not pipeline.store.history(sid)
    assert not (pipeline.store.path.parent / 'photos').exists()


def test_deletion_forgets_derived_facts_observations_and_later_turns(pipeline):
    from cbs import clock
    from cbs.knowing import Understandings
    from cbs.storage import compact_trace
    from cbs.photos import model_turns
    pipeline.client.text_responses = ['白い皿だね。', '前に写真を見せてくれたね。']
    pipeline.client.json_responses = [{}, {'problems': []}] * 2
    sid = pipeline.store.create_session('fixture')['id']
    result = pipeline.run(sid, 'これは好きな昼ごはん', photos=[picture()])
    pipeline.wait_for_steward()
    pipeline.client.episodizer_responses = [{
        'episodes': [{'summary': '写真を見せた', 'details': '皿の写真', 'importance': .7, 'pinned': False}],
        'user_facts': [{'operation': 'new', 'content': 'この昼ごはんが好き', 'importance': .7, 'pinned': False}],
        'observations': [{'kind': 'shared', 'summary': '昼ごはんの写真を見せた', 'response': 'positive', 'source_turns': [result['turn_id']]}],
    }]
    pipeline.close_conversation(sid)
    pipeline.wait_for_steward()
    observations = pipeline.store.list_observations('fixture')
    assert observations
    pipeline.store.apply_understandings('fixture', Understandings(understandings=[{
        'relation': 'new', 'kind': 'shared', 'text': '写真を見せてくれる', 'condition': '',
        'support': [observations[0]['id']], 'counter': [],
    }]), {o['id'] for o in observations}, pipeline.store.knowing_setting('fixture')['revision'])
    assert pipeline.store.list_user_facts('fixture')
    second_sid = pipeline.store.create_session('fixture')['id']
    later = pipeline.run(second_sid, '前の昼ごはんを覚えてる？')
    pipeline.wait_for_steward()
    with pipeline.store.connect() as db:
        assert db.execute('SELECT 1 FROM photo_turn_sources WHERE photo_id=? AND turn_id=?',
            (result['photos'][0]['id'], later['turn_id'])).fetchone()
        # Old request bodies may already have been dropped by retention.
        for row in db.execute('SELECT id,trace_json FROM turns').fetchall():
            db.execute('UPDATE turns SET trace_json=? WHERE id=?', (json.dumps(compact_trace(json.loads(row['trace_json']))), row['id']))
        db.execute("INSERT INTO note_entries(character_id,kind,source_id,text,created_at) VALUES ('fixture','ex2',0,?,?)",
                   (json.dumps({'scenes': [{'text': '写真から派生した表示'}]}), clock.iso()))
        # AU1: a question born from the photo turn must go with it.
        pipeline.store.save_research_questions(db, 'fixture', ['白い皿の産地が気になる'], 'conversation', f"turn:{result['turn_id']}", '会話の後に生まれた問い')
        assert db.execute("SELECT 1 FROM research_candidates WHERE character_id='fixture' AND source_ref=?", (f"turn:{result['turn_id']}",)).fetchone()
    delete_photo(pipeline, result['photos'][0]['id'])
    with pipeline.store.connect() as db:
        assert not db.execute("SELECT 1 FROM research_candidates WHERE character_id='fixture' AND source_ref=?", (f"turn:{result['turn_id']}",)).fetchone()
    assert not pipeline.store.list_user_facts('fixture')
    assert not pipeline.store.list_observations('fixture')
    assert not pipeline.store.list_understandings('fixture', pipeline.config.knowing)
    assert '対象外' in model_turns(pipeline.store, pipeline.store.history(second_sid))[0]['assistant_text']
    assert '白い皿' not in pipeline.store.history(sid)[0]['user_text']
    with pipeline.store.connect() as db:
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        assert not db.execute("SELECT 1 FROM note_entries WHERE character_id='fixture' AND kind='ex2'").fetchone()
    # Rebuilding the mixed job cannot restore the deleted photo or its old reply.
    pipeline.maintenance()
    pipeline.wait_for_steward()
    calls = [c for c in pipeline.client.calls if c.get('schema', {}).get('title') == 'EpisodizerOutput']
    assert 'photo:' not in calls[-1]['messages'][-1]['content']
