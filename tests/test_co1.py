import json
import sqlite3
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from cbs import clock
from cbs.config import CostPrice, load_config
from cbs.costs import CostRecorder, bind, estimate
from cbs.llm import FakeLLMClient, LLMError, RoutingLLMClient
from cbs.llm.cost import for_step
from cbs.steward import wait_for_steward
from cbs.web import create_app


class Empty(BaseModel):
    pass


def test_prices_usage_shapes_and_unknown(tmp_path):
    cfg = load_config(data_dir=tmp_path).costs
    cfg.prices['openai/test'] = CostPrice(input_usd=2, output_usd=10, cache_read_usd=.2, cache_write_usd=2.5)
    data = {'input_tokens':100, 'output_tokens':20, 'input_tokens_details':{'cached_tokens':40}}
    counts, amount = estimate('openai', 'openai/test', data, cfg)
    assert counts == (100,20,40,0)
    assert amount == pytest.approx((60*2+20*10+40*.2)/1e6)
    anthropic = {'input_tokens':60,'cache_read_input_tokens':30,'cache_creation_input_tokens':10,'output_tokens':20}
    assert estimate('anthropic','openai/test',anthropic,cfg)[0] == (100,20,30,10)
    gemini = {'prompt_token_count':100,'candidates_token_count':20,'thoughts_token_count':30,'cached_content_token_count':10}
    assert estimate('gemini','openai/test',gemini,cfg)[0] == (100,50,10,0)
    assert estimate('openrouter','openai/test',{'cost':.03},cfg)[1] == .03
    assert estimate('openrouter','openai/test',{'cost':0},cfg)[1] == 0
    assert estimate('openrouter','openai/test',data,cfg)[1] is None
    assert estimate('local','local/test',{},cfg)[1] == 0
    for data in ({},{'cost':float('nan')},{'input_tokens':1}):
        assert estimate('openai','openai/unknown',data,cfg)[1] is None
    assert estimate('openai','openai/test',{'input_tokens':None},cfg)[1] is None
    cfg.prices['openai/test'].cache_read_usd = None
    assert estimate('openai','openai/test',{'input_tokens':10,'input_tokens_details':{'cached_tokens':2}},cfg)[1] is None


@pytest.mark.parametrize('provider',['openrouter','openai','gemini','local'])
def test_all_llm_boundaries_fake_only(store,tmp_path,provider):
    cfg = load_config(data_dir=tmp_path)
    cfg.costs.prices[f'{provider}/test'] = CostPrice(input_usd=1,output_usd=2)
    router = bind(RoutingLLMClient(cfg),store,cfg)
    fake = FakeLLMClient(text_responses=['a','b'],json_responses=[{},{}],stream_chunks=[['s']])
    fake.usage_responses = [{'prompt_tokens':10,'completion_tokens':2,'cost':.001} for _ in range(6)]
    router._clients[provider] = fake
    router._local_embedding = fake
    client = for_step(router,'explicit_step','media')
    model = provider+'/test'
    client.generate_text(model,'PRIVATE_SENTINEL',[{'role':'user','content':'PRIVATE_SENTINEL'}])
    client.generate_json(model,'',[],Empty)
    client.generate_images_json(model,'','photo',[b'PRIVATE_SENTINEL'],Empty)
    client.generate_text_stream(model,'',[],lambda _:None)
    client.embed(['PRIVATE_SENTINEL'],model)
    # local STT bypasses the router in the application; its lifecycle is separately tested.
    if provider != 'local':
        client.transcribe_audio(model,'',b'PRIVATE_SENTINEL')
    else:
        store.costs.record('local',model,'stt','media',{})
    wait_for_steward(store)
    store.costs.flush()
    with store.connect() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM cost_calls')]
    assert len(rows)==6
    assert all(r['purpose']=='media' and r['model']==model for r in rows)
    assert all(r['usd'] is not None for r in rows)
    assert 'PRIVATE_SENTINEL' not in json.dumps(rows)


def test_failed_json_and_nested_fake_are_counted_once(store,tmp_path):
    cfg = load_config(data_dir=tmp_path)
    fake = bind(FakeLLMClient(json_responses=['broken'],usage_responses=[{'cost':.02}]),store,cfg)
    with pytest.raises(LLMError):
        for_step(fake,'vision','media').generate_images_json('openrouter/test','','',[b'image'],Empty)
    assert store.costs.summary()['month']['calls']==1
    assert store.costs.summary()['month']['usd']==.02


def test_standalone_factory_records_without_pipeline(tmp_path):
    from cbs.llm import create_client
    cfg=load_config(data_dir=tmp_path)
    client=create_client(cfg)
    client._clients['openrouter']=FakeLLMClient(usage_responses=[{'cost':.005}])
    client.generate_text('openrouter/test','',[])
    wait_for_steward(client.costs.store)
    assert client.costs.summary()['month']['usd']==.005


def test_openai_transcription_usage_reaches_recorder(store,tmp_path):
    from types import SimpleNamespace
    from cbs.llm.openai_client import OpenAILLMClient
    class Usage(BaseModel):
        input_tokens: int
        output_tokens: int
    class AudioFake(FakeLLMClient):
        @property
        def audio(self):
            return SimpleNamespace(transcriptions=SimpleNamespace(create=self.create))
        def create(self,model,file,prompt,language):
            return SimpleNamespace(text=self.transcribe_audio(model,prompt,file[1]),
                                   usage=Usage(input_tokens=120,output_tokens=5))
    cfg=load_config(data_dir=tmp_path)
    cfg.costs.prices['openai/test']=CostPrice(input_usd=1,output_usd=2)
    router=bind(RoutingLLMClient(cfg),store,cfg)
    adapter=object.__new__(OpenAILLMClient)
    adapter.name,adapter._key,adapter._client='openai','',AudioFake()
    router._clients['openai']=adapter
    for_step(router,'stt','media').transcribe_audio('openai/test','',b'PRIVATE_AUDIO_SENTINEL')
    wait_for_steward(store)
    store.costs.flush()
    with store.connect() as db:
        row=dict(db.execute('SELECT * FROM cost_calls').fetchone())
    assert (row['input_tokens'],row['output_tokens'],row['purpose'])==(120,5,'media')
    assert row['usd']==pytest.approx(.00013)
    assert 'PRIVATE_AUDIO_SENTINEL' not in json.dumps(row)


def test_actual_turn_steps_are_classified(pipeline):
    sid=pipeline.store.create_session(pipeline.config.character.id)['id']
    result=pipeline.run(sid,'こんにちは')
    pipeline.wait_for_expression(result['turn_id'])
    pipeline.wait_for_steward()
    pipeline._embedding_warmer.wait()
    pipeline.store.costs.flush()
    with pipeline.store.connect() as db:
        steps={r[0]:r[1] for r in db.execute('SELECT role,purpose FROM cost_calls')}
    assert steps['pre_judge']=='chat'
    assert steps['Actor']=='chat'
    assert steps['Check']=='chat'
    assert steps['Extract']=='memory'
    assert steps['expression']=='chat'


def test_month_rollup_retains_totals_and_unknown(store,tmp_path):
    cfg = load_config(data_dir=tmp_path)
    recorder = bind(FakeLLMClient(),store,cfg).costs
    with clock.at('2026-06-01T12:00:00+09:00'):
        recorder.record('openrouter','openrouter/test','extract','memory',{'cost':.1})
        recorder.record('openrouter','openrouter/test','actor','chat',{})
    wait_for_steward(store)
    with clock.at('2026-10-21T12:00:00+09:00'):
        for at,amount in [('2026-09-30T23:59:00+09:00',.2),('2026-10-01T00:00:00+09:00',.3),('2026-10-21T12:00:00+09:00',.4)]:
            with clock.at(at):
                recorder.record('openrouter','openrouter/test','actor','chat',{'cost':amount})
        wait_for_steward(store)
        recorder.flush()
        first=recorder.summary()
        recorder.flush()
        assert recorder.summary()==first
        assert first['today']['usd']==.4
        assert first['month']['usd']==pytest.approx(.7)
        assert first['previous']['usd']==.2
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM cost_calls WHERE at<'2026-07'").fetchone()[0]==0
        rolled=db.execute("SELECT sum(calls),sum(unknown),sum(usd) FROM cost_months WHERE month='2026-06'").fetchone()
        assert tuple(rolled)==(2,1,.1)
    with clock.at('2026-07-15T00:00:00+09:00'):
        assert recorder.summary()['previous']['unknown']==1
    reopened=CostRecorder(store,cfg.costs)
    assert reopened.month_totals['2026-06']==.1


def test_recording_never_waits_for_db_or_existing_background(store,tmp_path,monkeypatch):
    cfg = load_config(data_dir=tmp_path)
    client=bind(FakeLLMClient(),store,cfg)
    # Prove completion while persistence is held behind an unrelated worker.
    from cbs.steward import submit
    blocked,release,finished=Event(),Event(),Event()
    def block():
        blocked.set();release.wait(5)
    submit(store,block)
    assert blocked.wait(2)
    def forbidden():
        raise AssertionError('foreground recording opened the DB')
    monkeypatch.setattr(store,'connect',forbidden)
    def call():
        for_step(client,'actor','chat').generate_text('openrouter/test','',[])
        finished.set()
    thread=Thread(target=call)
    thread.start()
    try:
        assert finished.wait(1)
        assert len(store.costs.pending)==1
    finally:
        release.set();thread.join()
        wait_for_steward(store)


def test_busy_db_retries_without_loss(store,tmp_path):
    cfg=load_config(data_dir=tmp_path)
    recorder=bind(FakeLLMClient(),store,cfg).costs
    with sqlite3.connect(store.path) as db:
        db.execute('BEGIN IMMEDIATE')
        recorder.record('openrouter','openrouter/test','actor','chat',{'cost':.02})
        wait_for_steward(store)
        assert len(recorder.pending)==1
    recorder.flush()
    assert recorder.summary()['month']['calls']==1


def test_guide_once_restart_month_and_autonomy_only(pipeline):
    from cbs.autonomy import inner_life,proactive
    from cbs.inner import update_inner
    recorder=pipeline.store.costs
    recorder.config.monthly_guide_usd=.01
    recorder.config.pause_autonomy=True
    with clock.at('2026-10-21T12:00:00+09:00'):
        recorder.record('openrouter','openrouter/test','actor','chat',{'cost':.02})
        assert recorder.paused()
        assert recorder.notice(consume=True)=={'month':'2026-10','paused':True}
        assert recorder.notice(consume=True) is None
        before=len(pipeline.client.calls)
        inner_life(pipeline)
        update_inner(pipeline,'turn:1',{})
        assert proactive(pipeline,'unused')['speak'] is False
        assert len(pipeline.client.calls)==before
        with pytest.raises(LLMError):
            for_step(pipeline.client,'research','autonomy').generate_text('openrouter/test','',[])
        # The same boundary keeps conversation and memory work enabled.
        pipeline.client.text_responses=['conversation','memory']
        for purpose in ('chat','memory'):
            for_step(pipeline.client,'actor',purpose).generate_text('openrouter/test','',[])
        recorder.flush()
        assert CostRecorder(pipeline.store,recorder.config).notice(consume=True) is None
        recorder.config.pause_autonomy=False
        assert not recorder.paused()
    with clock.at('2026-11-01T00:00:00+09:00'):
        recorder.config.pause_autonomy=True
        assert not recorder.paused()
        recorder.record('openrouter','openrouter/test','actor','chat',{'cost':.02})
        assert recorder.notice(consume=True)['month']=='2026-11'


def test_settings_guide_clear_validation_and_backup(pipeline,tmp_path):
    from cbs.backup import snapshot_db
    app=create_app(pipeline,settings_dir=tmp_path/'settings',work_root=tmp_path/'work')
    client=TestClient(app)
    saved=client.put('/api/settings',json={'costs':{'monthly_guide_usd':.01,'pause_autonomy':True}})
    assert saved.status_code==200
    assert pipeline.client.costs is pipeline.store.costs
    pipeline.store.costs.record('openrouter','openrouter/test','actor','chat',{'cost':.02})
    values=client.get('/api/settings').json()
    assert values['cost_summary']['month']['usd']==.02
    assert values['cost_summary']['paused']
    assert client.post('/api/costs/notice').json()['notice']
    assert client.post('/api/costs/notice').json()['notice'] is None
    snapshot=tmp_path/'snapshot.sqlite3'
    pipeline.store.costs.flush()
    snapshot_db(pipeline.store.path,snapshot)
    with sqlite3.connect(snapshot) as db:
        assert db.execute('SELECT sum(usd) FROM cost_calls').fetchone()[0]==.02
        assert db.execute('SELECT seen FROM cost_notices').fetchone()[0]==1
    for value in (-1,0,'bad'):
        assert client.put('/api/settings',json={'costs':{'monthly_guide_usd':value}}).status_code==422
    assert client.put('/api/settings',json={'costs':{'monthly_guide_usd':None,'pause_autonomy':False}}).status_code==200
    assert load_config(data_dir=tmp_path/'settings').costs.monthly_guide_usd is None
