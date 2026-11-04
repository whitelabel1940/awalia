import json
from fastapi.testclient import TestClient

from cbs import clock
from cbs.note import fill_diaries, diaries, latest_diary
from cbs.web import create_app


def memory(store, day, *, cid='fixture', imported=False, pending=False):
    with clock.at(day+'T12:00:00+09:00'):
        sid=store.create_session(cid)['id']
        conv=store.prepare_conversation(sid,clock.iso())
        store.add_turn(sid,'その日の話','その日の返事',{},conversation_id=conv)
        with store.connect() as db:
            db.execute("UPDATE conversations SET status=? WHERE id=?", ('open' if pending else 'episodized',conv))
            db.execute('INSERT INTO memory_episodes(character_id,conversation_id,started_at,ended_at,summary,details,importance,pinned,created_at) VALUES (?,?,?,?,?,?,0.5,0,?)',
                (cid,conv,clock.iso(),clock.iso(),'その日の話','会話の記憶',clock.iso()))
            if imported:
                db.execute("INSERT INTO import_batches VALUES ('diary-test',?,'completed','{}',?,?,NULL,0)",(cid,clock.iso(),clock.iso()))
                db.execute("INSERT INTO import_items(batch_id,character_id,format,source_id,content_hash,payload_json,session_id) VALUES ('diary-test',?,'test','test','hash','{}',?)",(cid,sid))
        return conv


def test_window_import_isolation_order_idempotency_and_previous_comment(pipeline):
    s=pipeline.store
    for day in ('2026-10-05','2026-10-06','2026-10-18','2026-10-20'):
        memory(s,day)
    memory(s,'2026-10-19',imported=True)
    memory(s,'2026-10-17',cid='other')
    pipeline.client.diary_responses=[{'summary':'本の話をした。','partner_comment':'また読もうね。'}, {'summary':'お茶の話をした。','partner_comment':'返事、うれしかった。'}]
    with clock.at('2026-10-20T12:00:00+09:00'):
        # Generate the first day, then fail the next one; retry keeps first intact.
        pipeline.client.diary_responses.insert(1,'invalid JSON')
        result=fill_diaries(pipeline)
        assert [r['status'] for r in result]==['completed','failed']
        with s.connect() as db:
            db.execute("UPDATE diaries SET user_comment='私も楽しかった' WHERE day='2026-10-06'")
        fill_diaries(pipeline)
        count=len(pipeline.client.calls)
        assert fill_diaries(pipeline)==[]
    assert len(pipeline.client.calls)==count
    assert [d['day'] for d in diaries(s,'fixture')]==['2026-10-18','2026-10-06']
    assert not diaries(s,'other')
    context=json.loads(pipeline.client.calls[-1]['messages'][0]['content'])
    assert context['previous_diary']['user_comment']=='私も楽しかった'
    assert latest_diary(s,'fixture')['partner_comment']=='返事、うれしかった。'


def test_pending_and_empty_output_retry(pipeline):
    conv=memory(pipeline.store,'2026-10-19',pending=True)
    with clock.at('2026-10-20T12:00:00+09:00'):
        assert fill_diaries(pipeline)==[]
        with pipeline.store.connect() as db:
            db.execute("UPDATE conversations SET status='episodized' WHERE id=?",(conv,))
        pipeline.client.diary_responses=[{'summary':'  ','partner_comment':'一言'}]
        assert fill_diaries(pipeline)[0]['status']=='failed'
        assert not diaries(pipeline.store,'fixture')
        assert fill_diaries(pipeline)[0]['status']=='completed'


def test_midnight_has_only_actual_speech_dates_and_grounding(pipeline):
    s=pipeline.store
    with clock.at('2026-10-17T23:50:00+09:00'):
        sid=s.create_session('fixture')['id'];conv=s.prepare_conversation(sid,clock.iso())
        s.add_turn(sid,'古本の話','本の話の返事',{},conversation_id=conv)
    with clock.at('2026-10-19T00:10:00+09:00'):
        s.add_turn(sid,'雨の話','雨の話の返事',{},conversation_id=conv)
        with s.connect() as db:
            db.execute("UPDATE conversations SET status='episodized' WHERE id=?",(conv,))
            db.execute('INSERT INTO memory_episodes(character_id,conversation_id,started_at,ended_at,summary,details,importance,pinned,created_at) VALUES (?,?,?,?,?,?,0.5,0,?)',
                ('fixture',conv,'2026-10-17T23:50:00+09:00',clock.iso(),'本と雨','二つの話題',clock.iso()))
    with clock.at('2026-10-20T12:00:00+09:00'):
        assert len(fill_diaries(pipeline))==2
    assert [d['day'] for d in diaries(s,'fixture')]==['2026-10-19','2026-10-17']
    for call in pipeline.client.calls:
        if call.get('schema',{}).get('title')=='DiaryEntry':
            context=json.loads(call['messages'][0]['content'])
            assert all(clock.parse(u['at']).date().isoformat()==context['day'] for m in context['memories'] for u in m['day_utterances'])


def test_recent_comment_on_older_page_reaches_next_generation(pipeline):
    s=pipeline.store
    for day in ('2026-10-17','2026-10-18','2026-10-19'):
        memory(s,day)
    with clock.at('2026-10-19T12:00:00+09:00'):
        fill_diaries(pipeline)
        with s.connect() as db:
            db.execute("UPDATE diaries SET user_comment='古いページへの返事',comment_at=? WHERE day='2026-10-17'",(clock.iso(),))
        assert latest_diary(s,'fixture')['latest_user_comment']['day']=='2026-10-17'
    with clock.at('2026-10-20T12:00:00+09:00'):
        fill_diaries(pipeline)
    context=json.loads(pipeline.client.calls[-1]['messages'][0]['content'])
    assert context['previous_diary']['day']=='2026-10-18'
    assert context['previous_diary']['latest_user_comment']['user_comment']=='古いページへの返事'


def test_daily_maintenance_upgrades_old_marker_once(pipeline):
    from cbs.memory import daily_maintenance
    memory(pipeline.store,'2026-10-19')
    with clock.at('2026-10-20T12:00:00+09:00'):
        with pipeline.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO memory_maintenance VALUES ('fixture','2026-10-20','completed',?)",
                (json.dumps({'now_summary':{},'storage':{},'knowing':{},'months':[]}),))
        daily_maintenance(pipeline)
        count=len(pipeline.client.calls)
        daily_maintenance(pipeline)
        assert len(pipeline.client.calls)==count
        assert len(diaries(pipeline.store,'fixture'))==1
    with clock.at('2026-11-04T12:00:00+09:00'):
        pipeline.store.trim_traces(14,vacuum=False)
        with pipeline.store.connect() as db:
            trace=json.loads(db.execute('SELECT trace_json FROM diaries').fetchone()[0])
        assert 'request' not in trace and 'usage' in trace
        assert latest_diary(pipeline.store,'fixture')['summary']=='日々のことを話した。'


def test_comment_api_validation_and_packet(pipeline,tmp_path):
    memory(pipeline.store,'2026-10-19')
    with clock.at('2026-10-20T12:00:00+09:00'):
        fill_diaries(pipeline)
        root=tmp_path/'characters';(root/'fixture').mkdir(parents=True)
        client=TestClient(create_app(pipeline,characters_root=root,settings_dir=tmp_path/'settings'))
        url='/api/note/diaries/2026-10-19/comment'
        assert client.put(url,json={'comment':'<script>ユーザーの言葉</script>'}).status_code==200
        assert client.get('/api/note').json()['diaries'][0]['user_comment']=='<script>ユーザーの言葉</script>'
        assert client.put(url,json={'comment':'a'*2001}).status_code==422
        assert client.put('/api/note/diaries/bad/comment',json={'comment':''}).status_code==422
        assert client.put('/api/note/diaries/2026-10-18/comment',json={'comment':''}).status_code==404
        sid=pipeline.store.create_session('fixture')['id']
        pipeline.client.text_responses=['またお話ししよう。']
        result=pipeline.run(sid,'こんにちは')
        pipeline.wait_for_steward()
        packet=pipeline.store.get_trace(result['turn_id'])['Packet']['output']
        assert packet['shared_diary']['user_comment']=='<script>ユーザーの言葉</script>'
        assert client.put(url,json={'comment':''}).status_code==200
        assert latest_diary(pipeline.store,'fixture')['user_comment']==''


def test_day_held_by_an_open_conversation_is_written_once_it_is_saved(pipeline):
    from cbs.memory import catch_up_diaries
    from cbs.note import fill_diaries, diaries
    s = pipeline.store
    conv = memory(s, '2026-10-25', pending=True)
    with s.connect() as db:
        db.execute("DELETE FROM memory_maintenance WHERE local_day='2026-10-26'")  # setup may record a pass for the real date
    pipeline.client.diary_responses = [dict(summary='夜の話。', partner_comment='遅くまでありがとう。')]
    with clock.at('2026-10-26T00:05:00+09:00'):
        fill_diaries(pipeline)                       # today's pass: the open conversation holds 10/25
        assert not diaries(s, 'fixture')
        assert catch_up_diaries(pipeline, {'conversation_id': conv}) is None   # pass not recorded yet: leave it to the pass
        with s.connect() as db:
            db.execute("INSERT INTO memory_maintenance(character_id,local_day,status,trace_json) VALUES ('fixture','2026-10-26','completed','{}')")
            db.execute("UPDATE conversations SET status='episodized' WHERE id=?", (conv,))
        catch_up_diaries(pipeline, {'conversation_id': conv})
    assert [d['day'] for d in diaries(s, 'fixture')] == ['2026-10-25']
