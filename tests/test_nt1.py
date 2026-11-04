import pytest

from cbs import clock
from cbs.display_text import script_mix
from cbs.note import fill_activity_notes, activity_notes, fill_past_notes, fill_diaries, diaries
from cbs.inner import update_inner
from cbs.autonomy import inner_life
from cbs.note_scenes import fill_forming_notes, load_state
from test_diary import memory
from shared_helpers import record
@pytest.mark.parametrize('text', ['сочетание?', '한글', 'مرحبا', 'ไทย', 'देवनागरी', 'বাংলা', 'שלום', 'ελληνικά'])
def test_unexpected_scripts(text):
    assert script_mix('日本語の文。' + text)


@pytest.mark.parametrize('text', ['OpenAIとCafé、Beyoncé。', 'ありがとう 😊👩🏽‍💻❤️ 🇯🇵', '∞±×→① ＡＢＣ 123 𝑥', 'ひらがな・カタカナ、漢字𠮷。', 'Cafe\u0301、〆々〜ー！？'])
def test_allowed_scripts(text):
    assert not script_mix(text)


def activity(p):
    with p.store.connect() as db:
        rid = db.execute("INSERT INTO inner_life_runs(character_id,created_at,action,reason,status,trace_json) VALUES ('fixture',?,'reflect','考える','completed','{}')", (clock.iso(),)).lastrowid
        return db.execute("INSERT INTO activities(character_id,run_id,kind,topic,summary,created_at,status) VALUES ('fixture',?,'reflect','本','本について考えた',?,'completed')", (rid, clock.iso())).lastrowid


def calls(p, title):
    return [c for c in p.client.calls if c.get('schema', {}).get('title') == title]


@pytest.mark.parametrize('bad_field', ['title', 'summary', 'impression'])
@pytest.mark.parametrize('second_bad', [False, True])
def test_activity_retry_and_keep_original(pipeline, bad_field, second_bad):
    p = pipeline
    aid = activity(p)
    good = dict(title='Caféの本 📖', summary='本を読んで考えた。', impression='面白かったようだ。')
    bad = {**good, bad_field: '混ざった слово'}
    p.client.activity_note_responses = [bad, bad if second_bad else good, good]
    fill_activity_notes(p)
    assert len(calls(p, 'ActivityNote')) == 2
    assert bool(activity_notes(p.store, 'fixture')) != second_bad
    assert p.store.list_activities('fixture')[0]['id'] == aid
    assert p.store.list_activities('fixture')[0]['status'] == 'completed'
    assert p.store.list_activities('fixture')[0]['summary'] == '本について考えた'
    assert len(p.client.activity_note_responses) == 1  # No third call.


@pytest.mark.parametrize('second_bad', [False, True])
def test_diary_retry_atomic(pipeline, second_bad):
    p = pipeline
    memory(p.store, '2026-10-22')
    good = dict(summary='本の話。', partner_comment='また話そうね。')
    bad = {**good, 'partner_comment': 'слово'}
    p.client.diary_responses = [bad, bad if second_bad else good]
    with clock.at('2026-10-23T12:00:00+09:00'):
        fill_diaries(p)
    assert len(calls(p, 'DiaryEntry')) == 2
    assert bool(diaries(p.store, 'fixture')) != second_bad
    assert len(p.store.list_memory_episodes('fixture')) == 1


@pytest.mark.parametrize('second_bad', [False, True])
def test_ex2_retry_preserves_episode(pipeline, second_bad):
    p = pipeline
    record(p)
    good = {'operations': [{'action': 'add', 'scene_id': None, 'text': '理由を聞くみたい。'}], 'caption': '理由を聞いた。'}
    bad = {**good, 'caption': 'слово'}
    p.client.scene_responses = [bad, bad if second_bad else good]
    fill_forming_notes(p)
    assert len(calls(p, 'SceneEdits')) == 2
    assert bool(load_state(p.store, 'fixture')['scenes']) != second_bad
    assert len(p.store.list_episodes('fixture')) == 1


@pytest.mark.parametrize('second_bad', [False, True])
def test_today_line_retry(pipeline, second_bad):
    p = pipeline
    good = {'text': '少し考えたい。'}
    bad = {'text': '考えたい слово'}
    p.client.monologue_responses = [bad, bad if second_bad else good]
    update_inner(p, 'nt1:test', {'kind': 'gap'})
    assert len(calls(p, 'InnerUpdate')) == 1
    assert len(calls(p, 'Monologue')) == 2
    assert bool(p.store.list_monologues('fixture')) != second_bad


@pytest.mark.parametrize('second_bad', [False, True])
def test_activity_generated_summary_retry(pipeline, second_bad):
    p = pipeline
    p.client.inner_life_responses = [{'action': 'reflect', 'reason': '考えたい', 'topic': '本'}]
    p.client.text_responses = ['本 слово', '本 слово' if second_bad else '本について考えた。', '三度目は呼ばない']
    inner_life(p)
    assert len(p.client.text_responses) == 1
    a = p.store.list_activities('fixture')[0]
    assert a['status'] == ('failed' if second_bad else 'completed')
    assert not script_mix(a['summary'])
    assert not second_bad or a['summary'] == ''


def test_actor_diagnostic_only(pipeline):
    p = pipeline
    p.client.text_responses = ['今日は слово だね。']
    result = p.run(p.store.create_session('fixture')['id'], 'こんにちは')
    p.wait_for_steward()
    assert result['assistant_text'] == '今日は слово だね。'
    assert p.store.get_trace(result['turn_id'])['script_mix'] == [{'step': 'Actor', 'detected': True}]


@pytest.mark.parametrize('kind', ['summary', 'trait'])
@pytest.mark.parametrize('second_bad', [False, True])
def test_ex2_summary_and_trait_retry(pipeline, kind, second_bad):
    from shared_helpers import trait_version
    p = pipeline
    record(p)
    record(p, '2026-10-20')
    if kind == 'trait':
        trait_version(p, ['episode:1', 'episode:2'])
    good = {'text': '丁寧に向き合うみたい。', **({'scene_ids': [1]} if kind == 'trait' else {})}
    bad = {**good, 'text': 'слово'}
    setattr(p.client, f'scene_{kind}_responses', [bad, bad if second_bad else good])
    fill_forming_notes(p)
    assert len(calls(p, 'SceneSummary' if kind == 'summary' else 'SceneTrait')) == 2
    state = load_state(p.store, 'fixture')
    assert bool(state['summaries' if kind == 'summary' else 'traits']) != second_bad
    assert len(state['scenes']) == 2
    assert len(p.store.list_episodes('fixture')) == 2


@pytest.mark.parametrize('second_bad', [False, True])
def test_knowing_display_retry(pipeline, second_bad):
    from cbs.knowing import rebuild
    from test_knowing import observe, proposal
    p = pipeline
    observation = observe(p.store)
    good = {'understandings': [proposal([observation])]}
    bad = {'understandings': [{**good['understandings'][0], 'condition': 'слово'}]}
    p.client.knowing_responses = [bad, bad if second_bad else good]
    rebuild(p, force=True)
    assert len(calls(p, 'Understandings')) == 2
    assert bool(p.store.list_understandings('fixture', p.config.knowing)) != second_bad
    assert len(p.store.list_observations('fixture')) == 1


@pytest.mark.parametrize('schema_name', ['ReadingImpression', 'ShoppingImpression', 'WorldActivity'])
def test_private_activity_display_fields_and_original_quote(pipeline, schema_name):
    import cbs.autonomy as autonomy
    p = pipeline
    good = {'summary': '分かったこと。', 'impression': '面白かった。'}
    if schema_name == 'WorldActivity':
        good.update(source_title='村の記録', facts=['村で本を読んだ'])
        bad = {**good, 'facts': ['слово']}
    elif schema_name == 'ShoppingImpression':
        good.update(quote='слово', products=[{'title': 'Caféの本', 'uri': 'https://example.com/слово'}])
        bad = {**good, 'products': [{'title': 'слово', 'uri': 'https://example.com/слово'}]}
    else:
        good.update(quote='слово')  # Original quotes/URLs are not generated display prose.
        bad = {**good, 'summary': 'слово'}
    p.client.json_responses = [bad, good]
    result, _ = autonomy.private_ask(p, 'read_source', getattr(autonomy, schema_name), {})
    assert result.summary == good['summary']
    assert len(calls(p, schema_name)) == 2


@pytest.mark.parametrize('second_bad', [False, True])
def test_past_note_retry_keeps_reveal(pipeline, second_bad):
    p = pipeline
    sid = p.store.create_session('fixture')['id']
    tid = p.store.add_turn(sid, '前のことを聞いた', '少し話した', {})
    with p.store.connect() as db:
        db.execute('INSERT INTO reveal_events(character_id,secret_id,level,source_turn,created_at) VALUES (?,?,?,?,?)',
                   ('fixture', 'nt1', 1, tid, clock.iso()))
    p.client.note_responses = [{'text': 'слово'}, {'text': 'слово' if second_bad else '昔のことを話した。'}]
    fill_past_notes(p)
    assert len(calls(p, 'PastNote')) == 2
    with p.store.connect() as db:
        assert bool(db.execute("SELECT 1 FROM note_entries WHERE kind='past'").fetchone()) != second_bad
        assert db.execute('SELECT COUNT(*) FROM reveal_events').fetchone()[0] == 1


@pytest.mark.parametrize('second_bad', [False, True])
def test_now_summary_retry_keeps_previous(pipeline, second_bad):
    from cbs.memory import daily_maintenance
    p = pipeline
    with p.store.connect() as db:
        previous = db.execute('SELECT summary FROM now_summaries WHERE character_id=?', ('fixture',)).fetchone()[0]
    p.client.now_responses = [{'summary': 'слово'}, {'summary': 'слово' if second_bad else '新しい関心。'}]
    # A day after setup's own maintenance, whatever today's real date is.
    from datetime import timedelta
    with clock.at((clock.now() + timedelta(days=2)).replace(hour=12).isoformat()):
        daily_maintenance(p)
    assert len(calls(p, 'NowSummary')) == 2
    with p.store.connect() as db:
        assert db.execute('SELECT summary FROM now_summaries WHERE character_id=?', ('fixture',)).fetchone()[0] == (previous if second_bad else '新しい関心。')
