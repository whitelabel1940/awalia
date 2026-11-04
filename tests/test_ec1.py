"""EC1 projections, separate wide assets and display-only maintenance; no network."""
import json
from io import BytesIO

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from cbs import clock
from cbs.look import image_prompt, author_look, draft_look
from cbs.note import note_header, self_forming, fill_forming_notes
from cbs.memory import daily_maintenance
from cbs.steward import StewardOutput, apply_output
from cbs.web import create_app
from test_steward import episode, update


def png(size, mode='RGB'):
    stream = BytesIO()
    Image.new(mode, size, 'purple').save(stream, format='PNG')
    return stream.getvalue()


def test_cover_prompt_scene_reference_and_actor_separation():
    look = {'person': {'hair': {'color': '銀色'}, 'expressions': {'smile': '歯を見せる'}},
            'image': {'use': 'cover', 'scene': '窓辺で読みかけの本を開く', 'from_reference': True}}
    prompt = image_prompt(look)
    assert all(s in prompt for s in ('銀色', '窓辺で読みかけの本を開く', '16:9', '添付した画像の人物', '左下'))
    assert '歯を見せる' not in prompt and '{{' not in prompt
    assert '窓辺' not in author_look(look)
    for use in ('single', 'sheet'):
        look['image']['use'] = use
        assert '窓辺' not in image_prompt(look)


def test_cover_draft_uses_verified_scene_quote(pipeline):
    pipeline.client.json_responses = [{'assignments': [
        {'path': 'image.scene', 'value': '窓辺で本を読む', 'source': 'current_interests', 'quote': '窓辺で本を読む'},
        {'path': 'image.background', 'value': '海辺', 'source': 'world', 'quote': 'ない引用'}], 'suggestions': []}]
    result, _ = draft_look(pipeline.config, pipeline.client, {'current_interests': '窓辺で本を読む', 'world': '現実'})
    assert [i['path'] for i in result['assignments']] == ['image.scene'] and result['dropped'] == 1


def test_cover_crud_wide_dimensions_no_expression_slot(pipeline, tmp_path):
    root = tmp_path / 'characters'
    (root / 'fixture').mkdir(parents=True)
    client = TestClient(create_app(pipeline, characters_root=root, settings_dir=tmp_path / 'settings'))
    assert client.get('/api/look').json()['cover'] is False
    assert client.get('/looks/fixture/cover').status_code == 404
    assert client.put('/api/look/neutral', content=png((80, 80)), headers={'Content-Type': 'image/png'}).status_code == 200
    neutral = client.get('/looks/fixture/neutral').content
    assert client.get('/looks/fixture/cover').status_code == 404  # Never substitute a face for a cover.
    r = client.put('/api/look/cover', content=png((2400, 1350)), headers={'Content-Type': 'image/png'})
    assert r.status_code == 200 and r.json()['cover'] and r.json()['expressions'] == ['neutral']
    saved = client.get('/looks/fixture/cover')
    assert saved.headers['content-type'] == 'image/png'
    assert Image.open(BytesIO(saved.content)).size == (1600, 900)
    assert client.get('/looks/fixture/neutral').content == neutral
    for content in (png((200, 300)), png((200, 200)), b'not an image'):
        assert client.put('/api/look/cover', content=content, headers={'Content-Type': 'image/png'}).status_code == 422
    assert client.get('/looks/fixture/cover').content == saved.content
    assert client.put('/api/look/cover', content=b'bad', headers={'Content-Type': 'text/plain'}).status_code == 415
    assert client.put('/api/look/cover', content=b'x'*(5*1024*1024+1), headers={'Content-Type': 'image/png'}).status_code == 413
    assert client.put('/api/look/meta', json={'image': {'use': 'cover', 'scene': '窓辺'}}).status_code == 200
    assert client.get('/api/look').json()['image']['scene'] == '窓辺'
    assert client.put('/api/look/not-a-slot', content=png((20, 10)), headers={'Content-Type': 'image/png'}).status_code == 422
    assert client.put('/api/look/cover?character_id=../fixture', content=png((20, 10)), headers={'Content-Type': 'image/png'}).status_code == 404
    assert client.delete('/api/look/cover').json()['cover'] is False
    assert client.get('/looks/fixture/cover').status_code == 404
    assert client.get('/looks/fixture/neutral').content == neutral


def test_header_counts_original_calendar_dates_and_latest_decision(pipeline):
    store = pipeline.store
    assert note_header(store, 'fixture') == {'current_expression': 'neutral', 'days': {'first_day': None, 'elapsed_days': 0, 'talked_days': 0}}
    sid = store.create_session('fixture')['id']
    # Insert out of order, as with imported conversations; 4am conversation boundaries do not shift dates.
    for at, expression in [('2026-10-19T00:10:00+09:00', 'shy'), ('2026-06-01T23:00:00+09:00', 'sad'), ('2026-10-19T02:00:00+09:00', None)]:
        store.add_turn(sid, '話', '返事', {}, created_at=at, assistant_at=at, expression=expression)
    store.add_turn(sid, '', '自分から', {}, kind='proactive', created_at='2026-10-20T10:00:00+09:00', expression='smile')
    store.add_turn(store.create_session('other')['id'], '別の人', '返事', {}, created_at='2025-01-01', expression='pout')
    failed = store.add_turn(sid, '失敗', '', {}, created_at='2025-01-01', expression='pout')
    with store.connect() as db:
        db.execute("UPDATE turns SET status='failed' WHERE id=?", (failed,))
    calls = len(pipeline.client.calls)
    with clock.at('2026-10-20T12:00:00+09:00'):
        result = note_header(store, 'fixture')
    assert result == {'current_expression': 'smile', 'days': {'first_day': '2026-06-01', 'elapsed_days': 141, 'talked_days': 2}}
    assert len(pipeline.client.calls) == calls


def forming(pipeline, *, display_text='', mode='enacted'):
    sid = pipeline.store.create_session('fixture')['id']
    tid = pipeline.store.add_turn(sid, '好き', 'ありがとう', {})
    output = StewardOutput(episodes=[episode(mode)], updates=[update(
        interpretation='first_observation', proposed_known='今の1回の観測では、相手の好意に照れる', display_text=display_text)])
    apply_output(pipeline.store, pipeline.character, tid, output)


@pytest.mark.parametrize('mode', ['enacted', 'hypothetical'])
def test_old_display_rewrite_separate_idempotent_and_original_dates(pipeline, mode):
    with clock.at('2026-10-19T12:00:00+09:00'):
        forming(pipeline, mode=mode)
    before_versions = pipeline.store.list_model_versions('fixture')
    before_episodes = pipeline.store.list_episodes('fixture')
    assert self_forming(pipeline.store, pipeline.character) == []
    text = '好意を伝えられると、照れるみたい。' if mode == 'enacted' else '好意を伝えられたら、照れると言っていた。'
    pipeline.client.scene_responses = [{'operations': [{'action': 'add', 'scene_id': None, 'text': text}]}]
    result = fill_forming_notes(pipeline)
    assert result[0]['status'] == 'completed' and 'trace' in result[0]
    after = self_forming(pipeline.store, pipeline.character)[0]['scenes'][0]
    assert after['text'] == text and after['dates'] == ['2026-10-19']
    assert pipeline.store.list_model_versions('fixture') == before_versions
    assert pipeline.store.list_episodes('fixture') == before_episodes
    assert fill_forming_notes(pipeline) == []
    context = json.loads(pipeline.client.calls[-1]['messages'][0]['content'])
    assert context['episode']['mode'] == mode and context['episode']['situation']


def test_display_present_skipped_and_invalid_output_retried(pipeline):
    forming(pipeline, display_text='好意を伝えられると、照れるみたい。')
    # EX2 regenerates scene-limited prose even if EC1 had a display string.
    assert self_forming(pipeline.store, pipeline.character) == []
    pipeline.client.scene_responses = [{'operations': [{'action': 'add', 'scene_id': None, 'text': '  '}]},
        'invalid JSON', {'operations': [{'action': 'add', 'scene_id': None, 'text': '好意に照れるみたい。'}]}]
    assert fill_forming_notes(pipeline)[0]['status'] == 'failed'
    assert fill_forming_notes(pipeline)[0]['status'] == 'failed'
    assert fill_forming_notes(pipeline)[0]['status'] == 'completed'


def test_daily_upgrade_once_and_public_api(pipeline, tmp_path):
    forming(pipeline)
    with clock.at('2026-10-20T12:00:00+09:00'):
        with pipeline.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO memory_maintenance VALUES ('fixture','2026-10-20','completed',?)",
                       (json.dumps({k: {} for k in ('now_summary', 'storage', 'knowing', 'diaries')}),))
        daily_maintenance(pipeline)
        count = len(pipeline.client.calls)
        daily_maintenance(pipeline)
        assert len(pipeline.client.calls) == count
    root = tmp_path / 'characters'; (root / 'fixture').mkdir(parents=True)
    client = TestClient(create_app(pipeline, characters_root=root, settings_dir=tmp_path / 'settings'))
    count = len(pipeline.client.calls)
    note = client.get('/api/note').json()
    assert note['current_expression'] == 'neutral' and note['days']['talked_days'] == 1
    assert note['self_forming'][0]['scenes'][0]['text'] == '好意を伝えられると、照れながら理由を聞くみたい。'
    assert not any(k.startswith('_') for k in note['self_forming'][0])
    assert len(pipeline.client.calls) == count


def test_old_internal_wording_can_be_rewritten_without_becoming_public(pipeline):
    forming(pipeline)
    with pipeline.store.connect() as db:
        row = db.execute('SELECT id,diff_json FROM model_versions').fetchone()
        diff = json.loads(row['diff_json']); diff['changes'][0]['known'] = 'CBS10の1回の観測では、相手の好意に照れる'
        db.execute('UPDATE model_versions SET diff_json=? WHERE id=?', (json.dumps(diff), row['id']))
    assert self_forming(pipeline.store, pipeline.character) == []
    pipeline.client.scene_responses = [{'operations': [{'action': 'add', 'scene_id': None, 'text': '好意に照れるみたい。'}]}]
    assert fill_forming_notes(pipeline)[0]['status'] == 'completed'
    assert self_forming(pipeline.store, pipeline.character)[0]['scenes'][0]['text'] == '好意に照れるみたい。'


def test_daily_failure_retries_next_day_and_other_character_is_untouched(pipeline):
    forming(pipeline)
    with pipeline.store.connect() as db:
        db.execute("DELETE FROM memory_maintenance WHERE character_id='fixture'")
    pipeline.client.scene_responses = ['invalid JSON', {'operations': [{'action': 'add', 'scene_id': None, 'text': '好意に照れるみたい。'}]}]
    with clock.at('2026-10-20T12:00:00+09:00'):
        daily_maintenance(pipeline)
        assert self_forming(pipeline.store, pipeline.character) == []
        count = len(pipeline.client.calls)
        daily_maintenance(pipeline)
        assert len(pipeline.client.calls) == count
    with clock.at('2026-10-21T12:00:00+09:00'):
        daily_maintenance(pipeline)
        assert self_forming(pipeline.store, pipeline.character)[0]['scenes'][0]['text'] == '好意に照れるみたい。'
    pipeline.character.model.character_id = 'other'
    assert fill_forming_notes(pipeline) == []
