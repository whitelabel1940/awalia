"""M11 storage/rules and complete background integration. FakeLLMClient only."""
import json
from datetime import timedelta
from threading import Event

from fastapi.testclient import TestClient

from cbs import clock
from cbs.config import KnowingConfig
from cbs.knowing import Observation, Understandings, candidates, rebuild, stage
from cbs.memory import episodize
from cbs.pipeline import PreJudge, assemble_packet
from cbs.web import create_app


def observe(store, cid='fixture', at='2026-10-01 12:00', kind='interaction', summary='短い返事がよいと言った'):
    with clock.at(at), store.connect() as db:
        return db.execute('INSERT INTO user_observations (character_id,kind,summary,response,source_turns_json,observed_at,created_at) VALUES (?,?,?,?,?,?,?)',
            (cid,kind,summary,'neutral','[]',clock.iso(),clock.iso())).lastrowid


def proposal(support, previous_id=None, **kw):
    return dict(previous_id=previous_id, relation='same' if previous_id else 'new',kind='interaction',text='短い返事が好みらしい',condition='普段の会話',support=support,counter=[],**kw)


def apply(store, rows, cid='fixture', revision=None):
    return store.apply_understandings(cid,Understandings(understandings=rows),{o['id'] for o in store.list_observations(cid)},
        store.knowing_setting(cid)['revision'] if revision is None else revision)


def test_dates_stage_decay_expiry_and_confirmation(store):
    a=observe(store);b=observe(store,at='2026-10-02 12:00');c=observe(store,at='2026-10-15 12:00')
    obs={o['id']:o for o in store.list_observations('fixture')};cfg=KnowingConfig()
    item=proposal([a]);item['confirmed_at']=None
    assert stage(item,obs,cfg,'2026-10-16')['stage']=='tentative'
    assert stage({**item,'support':[a,b]},obs,cfg,'2026-10-16')['stage']=='probable'
    assert stage({**item,'support':[a,b,c]},obs,cfg,'2026-10-16')['stage']=='known'
    assert stage({**item,'support':[a,b,c]},obs,cfg,'2027-02-01')['stage']=='probable'
    assert stage({**item,'kind':'shared','support':[a,b,c]},obs,cfg,'2027-02-01')['stage']=='known'
    assert stage({**item,'confirmed_at':'2026-10-16'},obs,cfg,'2026-10-16')['stage']=='known'
    assert stage({**item,'confirmed_at':'2027-02-01'},obs,cfg,'2027-02-01')['stage']=='known'
    assert not stage({**item,'kind':'situation','confirmed_at':'2027-02-01'},obs,cfg,'2027-02-01')['expired']
    assert stage({**item,'kind':'situation'},obs,cfg,'2026-10-11 12:00')['expired']
    # More observations on the same day cannot advance a stage.
    d=observe(store,at='2026-10-01 13:00');obs[d]=store.list_observations('fixture')[-1]
    assert stage({**item,'support':[a,d]},obs,cfg,'2026-10-16')['stage']=='tentative'


def test_ids_private_and_character_validation(store):
    a=observe(store);other=observe(store,cid='other');wrong_kind=observe(store,kind='interest')
    result=apply(store,[proposal([a,999,other,wrong_kind])])
    item=store.list_understandings('fixture',KnowingConfig())[0]
    assert item['support']==[a] and result['warnings']
    assert not apply(store,[proposal([other])])['applied']
    private=proposal([a]);private['private']=True
    assert not apply(store,[private])['applied']
    assert not apply(store,[proposal([a],previous_id=999)])['applied']


def test_forget_stop_and_stale_background_results(store):
    a=observe(store);uid=apply(store,[proposal([a])])['applied'][0]
    revision=store.knowing_setting('fixture')['revision']
    store.understanding_action('fixture',uid,'forget')
    assert not store.list_understandings('fixture',KnowingConfig())
    assert not store.list_observations('fixture')
    assert not apply(store,[proposal([a])],revision=revision)['applied']
    assert not apply(store,[proposal([a])])['applied']
    b=observe(store,at='2026-10-18 12:00');uid=apply(store,[proposal([b])])['applied'][0]
    store.set_knowing('fixture',True,False)
    assert not apply(store,[proposal([b],uid)])['applied']
    assert store.list_understandings('fixture',KnowingConfig())
    store.set_knowing('fixture',True,True)
    assert not store.list_observations('fixture')
    store.set_knowing('fixture',False)
    assert not store.list_understandings('fixture',KnowingConfig())


def test_disagreement_then_agreement(store):
    a=observe(store);uid=apply(store,[proposal([a])])['applied'][0]
    with clock.at('2026-10-18 12:00'):
        store.understanding_action('fixture',uid,'disagree','仕事の相談では詳しく聞きたい')
        item=store.list_understandings('fixture',KnowingConfig())[0]
        assert item['status']=='review' and item['counter']
        assert item['stage']=='tentative'
        assert '仕事の相談では' not in item['counterevidence'][0]['summary']
        with store.connect() as db:
            assert db.execute("SELECT detail FROM knowing_actions WHERE action='disagree'").fetchone()[0]=='仕事の相談では詳しく聞きたい'
        store.understanding_action('fixture',uid,'agree')
        assert store.list_understandings('fixture',KnowingConfig())[0]['stage']=='known'
    with clock.at('2026-10-19 12:00'):
        store.understanding_action('fixture',uid,'agree')
        assert store.list_understandings('fixture',KnowingConfig())[0]['stage']=='known'


def test_background_episodizer_and_rebuild(pipeline):
    p=pipeline;p.config.autonomy.inner_life=False
    with clock.at('2026-10-01 12:00'):
        sid=p.store.create_session('fixture')['id'];tid=p.store.add_turn(sid,'短い返事がいい','わかった',{})
        unrelated=p.store.add_thread('fixture','curiosity','関心','続き','interest:999')
        p.store.sweep_conversations(force=True);job=p.store.memory_jobs()[0]
        p.client.episodizer_responses=[{'episodes':[{'summary':'返事の長さ','details':'短いほうがいいと伝えた','importance':.5,'pinned':False}],
            'user_facts':[{'operation':'new','content':'治療中と話した','importance':.5,'pinned':False,'private':True,'follow_up_thread':unrelated,'event_date':'2026-10-02'}],
            'observations':[{'kind':'interaction','summary':'短いほうがいいと伝えた','response':'neutral','source_turns':[tid,999]},
                            {'kind':'situation','summary':'治療の推測','response':'neutral','source_turns':[tid],'private':True}]}]
        episodize(p,job)
        assert len(p.store.list_observations('fixture'))==1
        assert p.store.list_observations('fixture')[0]['source_turns']==[tid]
        assert p.store.list_user_facts('fixture')[0]['private']
        assert next(t for t in p.store.list_threads('fixture') if t['id']==unrelated)['status']=='open'
        oid=p.store.list_observations('fixture')[0]['id'];p.client.knowing_responses=[{'understandings':[proposal([oid])]}]
        assert rebuild(p)['status']=='completed'
        assert rebuild(p)['skipped']=='already maintained'
        assert p.store.list_understandings('fixture',p.config.knowing)[0]['stage']=='tentative'
        episodize(p,job)
        assert len(p.store.list_observations('fixture'))==1


def test_candidates_bounded_and_stopped(pipeline):
    for n in range(10):
        a=observe(pipeline.store,summary=f'観察{n}');q=proposal([a]);q['text']=f'理解{n}';apply(pipeline.store,[q])
    assert len(candidates(pipeline))==pipeline.config.knowing.candidate_max
    assert all(i['may_ask'] for i in candidates(pipeline))
    pipeline.store.set_knowing('fixture',True)
    assert candidates(pipeline)==[]


def test_packet_private_gate_and_unknown_ids(character):
    memories=[{'ref':'fact:1','id':1,'content':'治療中','status':'current','private':True},
              {'ref':'understanding:2','id':2,'text':'短い返事','stage':'tentative','may_ask':False}]
    uses=[{'ref':r,'relevant_now':'yes','counterevidence':'yes','continuation':'no'} for r in ('fact:1','understanding:2','understanding:999')]
    p=assemble_packet(character,PreJudge(memory_use=uses),candidate_memories=memories)
    assert not p['user_facts']
    assert not any(m.get('private') for m in p['counterevidence_memories'])
    assert len(p['user_understandings'])==1
    uses[0]['private_topic_current']='yes'
    assert assemble_packet(character,PreJudge(memory_use=uses),candidate_memories=memories)['user_facts']


def test_question_quota_recorded_only_from_final_extract(pipeline):
    p=pipeline;a=observe(p.store);uid=apply(p.store,[proposal([a])])['applied'][0]
    packet={'user_understandings':[{'id':uid,'ref':f'understanding:{uid}','may_ask':True}]}
    with clock.at('2026-10-19 12:00'):
        sid=p.store.create_session('fixture')['id'];tid=p.store.add_turn(sid,'u','a',{})
        with p.store.connect() as db:
            p.store.record_knowing_questions(db,'fixture',tid,{'understanding_questions':['understanding:999']},packet)
        assert p.store.knowing_question_allowed('fixture',uid,p.config.knowing)
        with p.store.connect() as db:
            p.store.record_knowing_questions(db,'fixture',tid,{'understanding_questions':[f'understanding:{uid}']},packet)
        assert not p.store.knowing_question_allowed('fixture',uid,p.config.knowing)
        assert not p.store.knowing_question_allowed('fixture',uid+1,p.config.knowing)
    with clock.at('2026-10-20 12:00'):
        assert not p.store.knowing_question_allowed('fixture',uid,p.config.knowing)
        assert p.store.knowing_question_allowed('fixture',uid+1,p.config.knowing)
    with clock.at('2026-10-26 12:00'):
        assert p.store.knowing_question_allowed('fixture',uid,p.config.knowing)
        late=p.store.add_turn(sid,'u','a',{},created_at='2026-10-26T23:59:00+09:00',assistant_at='2026-10-27T00:01:00+09:00')
        with p.store.connect() as db:
            p.store.record_knowing_questions(db,'fixture',late,{'understanding_questions':[f'understanding:{uid}']},packet)
        assert not p.store.knowing_question_allowed('fixture',uid+1,p.config.knowing,'2026-10-27 12:00')
        assert p.store.knowing_question_allowed('fixture',uid+1,p.config.knowing,'2026-10-26 12:00')


def test_note_controls_and_private_lazy_endpoint(pipeline):
    p=pipeline;a=observe(p.store);uid=apply(p.store,[proposal([a])])['applied'][0]
    with p.store.connect() as db:
        for cid,private in [('fixture',0),('fixture',1),('other',0)]:
            db.execute('INSERT INTO user_facts (character_id,content,learned_at,last_confirmed_at,importance,pinned,private) VALUES (?,?,?,?,?,?,?)',(cid,'秘密' if private else '明言',clock.iso(),clock.iso(),.5,0,private))
    client=TestClient(create_app(p))
    note=client.get('/api/note').json()
    assert '秘密' not in json.dumps(note,ensure_ascii=False)
    assert client.get('/api/note/private').json()['facts'][0]['content']=='秘密'
    assert client.put(f'/api/note/understandings/{uid}',json={'action':'agree'}).status_code==200
    assert client.put('/api/note/understandings/999',json={'action':'forget'}).status_code==404
    assert client.put('/api/note/facts/3',json={'content':'侵入'}).status_code==404
    assert client.put('/api/note/facts/1',json={'content':'訂正'}).status_code==200
    facts=client.get('/api/note').json()['knowing']['facts'];assert [f['content'] for f in facts]==['訂正']
    p.wait_for_steward()
    p._embedding_warmer.wait()
    assert next(f for f in p.store.list_user_facts('fixture', current_only=True) if f['content']=='訂正')['embedding']
    assert client.delete('/api/note/facts/2').status_code==200
    assert client.get('/api/note/private').json()['facts']==[]
    assert client.put('/api/note/inference',json={'stopped':True,'clear':True}).status_code==200
    assert client.get('/api/note').json()['knowing']['understandings']==[]


def test_stopped_observation_and_stale_revision(store):
    with clock.at('2026-10-19 12:00'):
        sid=store.create_session('fixture')['id'];tid=store.add_turn(sid,'u','a',{})
        observation=Observation(kind='interaction',summary='観察',response='neutral',source_turns=[tid])
        store.set_knowing('fixture',True);store.set_knowing('fixture',False)
        with store.connect() as db:
            store.save_observations(db,'fixture',{'id':None},[observation],[{'id':tid,'created_at':clock.iso()}],revision=0)
        assert store.list_observations('fixture')==[]


def test_pending_memory_jobs_are_partner_scoped(pipeline):
    p=pipeline
    sid=p.store.create_session('other')['id'];p.store.add_turn(sid,'他の人の話','返答',{})
    p.store.sweep_conversations(force=True)
    jobs=p.store.memory_jobs(character_id='other');assert len(jobs)==1
    assert not p.store.memory_jobs(character_id='fixture')
    episodize(p,jobs[0])
    assert p.store.memory_jobs(character_id='other')==jobs
    assert not p.store.list_observations('fixture')


def test_observation_and_rebuild_do_not_block_reply(pipeline):
    p=pipeline;p.client.json_responses=[{}, {'problems':[]}]
    sid=p.store.create_session('fixture')['id']
    result=p.run(sid,'短い返事で話してね。');p.wait_for_steward()
    names=[c.get('schema',{}).get('title') for c in p.client.calls]
    assert result['assistant_text']
    assert 'Understandings' not in names and 'EpisodizerOutput' not in names


def test_private_inner_wants_are_dropped_not_scheduled(pipeline):
    from cbs.inner import update_inner, BASELINE
    p=pipeline
    tid=p.store.add_thread('fixture','want','元の話題','詳細','manual:old')
    want={'thread_ref':f'thread:{tid}','private':True,'direction':'気にかける','motive':'聞きたい','topic':'私的な話','detail':'本人から話すまで待つ','weight':.5,'trigger_ref':'private-control'}
    p.client.inner_update_responses=[{'feeling':{'sentence':'落ち着いている。','levels':BASELINE},
        'wants':[want,{**want,'thread_ref':None}]}]
    update_inner(p,'private-control',{'kind':'conversation'})
    assert len(p.store.list_threads('fixture'))==1
    assert p.store.list_threads('fixture')[0]['status']=='dropped'


def test_private_proactive_decision_never_reaches_actor(pipeline):
    p=pipeline
    with clock.at('2026-10-20 12:00'):
        sid=p.store.create_session('fixture')['id']
        p.store.add_turn(sid,'u','a',{},created_at='2026-10-18T12:00:00+09:00',assistant_at='2026-10-18T12:00:00+09:00')
        tid=p.store.add_thread('fixture','want','私的な話','詳細','manual:private')
        p.client.proactive_responses=[{'speak':True,'reason':'この話題を選んだ','thread_ref':f'thread:{tid}', 'want_ref':f'thread:{tid}','private_topic':True}]
        before=sum('schema' not in c for c in p.client.calls)
        result=p.proactive(sid)
        assert result['speak'] is False
        assert sum('schema' not in c for c in p.client.calls)==before
