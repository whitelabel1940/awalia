"""M11c policy and one-shot follow-ups. All LLM work uses the Fake fixture."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from cbs import clock
from cbs.inner import BASELINE, update_inner
from cbs.memory import UserFact
from cbs.pipeline import PreJudge, assemble_packet
from cbs.web import create_app


def fact(store, category='temporary_health', override=None, cid='fixture', follow=True):
    with store.connect() as db:
        fid = db.execute('INSERT INTO user_facts (character_id,content,learned_at,last_confirmed_at,importance,pinned,private,private_category,mention_override,event_date,follow_up) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (cid,'本人が話した体調の出来事',clock.iso(),clock.iso(),.7,0,1,category,override,clock.now().date().isoformat(),follow)).lastrowid
        store._fact_thread(db,cid,fid,UserFact(operation='new',content='本人が話した体調の出来事',private=True,private_category=category,
            importance=.7,pinned=False,event_date=clock.now().date(),follow_up=follow),None)
    return fid


def test_defaults_legacy_unknown_and_override_resolution(pipeline):
    p=pipeline
    settings=p.store.private_settings('fixture')
    assert {k for k,v in settings.items() if v['allow_mention']} == {'temporary_health'}
    ids=[fact(p.store,c) for c in settings]
    for category in [None,'not-in-config']:
        fid=fact(p.store,category)
        row=next(f for f in p.store.list_user_facts('fixture') if f['id']==fid)
        assert row['private_category']=='other' and not row['allow_mention']
    p.store.set_private_settings('fixture',{'temporary_health':False,'ongoing_health':True})
    rows={f['id']:f for f in p.store.list_user_facts('fixture')}
    assert not rows[ids[0]]['allow_mention'] and rows[ids[1]]['allow_mention']
    assert p.store.private_settings('other')['temporary_health']['allow_mention']
    fid=fact(p.store,'ongoing_health',False)
    assert not next(f for f in p.store.list_user_facts('fixture') if f['id']==fid)['allow_mention']
    fid=fact(p.store,'other',True)
    assert next(f for f in p.store.list_user_facts('fixture') if f['id']==fid)['allow_mention']
    with pytest.raises(ValueError):p.store.set_private_settings('fixture',{'unknown':True})


def test_same_day_and_future_due_one_shot_and_fact_selection(pipeline):
    p=pipeline
    with clock.at('2026-10-19 12:00'):
        fid=fact(p.store)
        f=p.store.list_user_facts('fixture')[0];t=p.store.list_threads('fixture', eligible_only=True)[0]
        assert f['may_initiate'] and t['may_initiate']
        f['ref']=f'fact:{fid}'
        judge=PreJudge(memory_use=[dict(ref=f['ref'],relevant_now='yes',counterevidence='no',continuation='yes')])
        packet=assemble_packet(p.character,judge,candidate_memories=[f,t])
        assert packet['threads'][0]['id']==t['id']
        sid=p.store.create_session('fixture')['id'];tid=p.store.add_turn(sid,'こんにちは','その後どう？',{})
        with p.store.connect() as db:p.store.apply_threads(db,'fixture',tid,{'used_threads':[t['ref']]},packet)
        assert not p.store.list_threads('fixture', eligible_only=True)
        assert not p.store.list_user_facts('fixture')[0]['may_initiate']
        # Reconfirmation of the same event cannot reopen a used follow-up.
        with p.store.connect() as db:
            p.store._fact_thread(db,'fixture',fid,UserFact(operation='confirm',content='同じ出来事',private=True,private_category='temporary_health',importance=.7,pinned=False,event_date=clock.now().date()),tid)
        assert len(p.store.list_threads('fixture'))==1 and p.store.list_threads('fixture')[0]['status']=='used'
        p.store.set_private_settings('fixture',{'temporary_health':False})
        p.store.set_private_settings('fixture',{'temporary_health':True})
        assert not p.store.list_threads('fixture', eligible_only=True)
        # A future event still waits until after that date.
        future=fact(p.store)
        with p.store.connect() as db:
            db.execute('DELETE FROM thread_history WHERE thread_id IN (SELECT id FROM threads WHERE source_ref=?)',(f'fact:{future}',))
            db.execute('DELETE FROM threads WHERE source_ref=?',(f'fact:{future}',))
            db.execute("UPDATE user_facts SET event_date='2026-10-21' WHERE id=?",(future,))
            p.store._fact_thread(db,'fixture',future,UserFact(operation='new',content='検査予定',private=True,private_category='temporary_health',importance=.7,pinned=False,event_date='2026-10-21'),tid)
        assert not p.store.list_threads('fixture', eligible_only=True)


def test_api_overrides_scope_reset_edit_delete_and_permission_pause(pipeline):
    p=pipeline;fid=fact(p.store);other=fact(p.store,cid='other');web=TestClient(create_app(p))
    assert web.put(f'/api/note/facts/{other}/mention',json={'mention_override':False}).status_code==404
    assert web.put('/api/note/private-settings',json={'categories':{'unknown':True}}).status_code==422
    assert web.put(f'/api/note/facts/{fid}/mention',json={'mention_override':False}).status_code==200
    assert not p.store.list_threads('fixture', eligible_only=True)
    assert web.put('/api/note/private-settings',json={'categories':{'temporary_health':True}}).status_code==200
    assert not p.store.list_threads('fixture', eligible_only=True)  # Per-fact no wins.
    assert web.put(f'/api/note/facts/{fid}/mention',json={'mention_override':None}).status_code==200
    assert len(p.store.list_threads('fixture', eligible_only=True))==1
    private=web.get('/api/note/private').json()['facts'][0]
    assert private['private_category']=='temporary_health' and private['mention_override'] is None
    assert web.put(f'/api/note/facts/{fid}',json={'content':'訂正した出来事'}).status_code==200
    p.wait_for_steward()
    f=p.store.list_user_facts('fixture', current_only=True)[0]
    assert f['private_category']=='temporary_health' and f['event_date'] and f['may_initiate']
    assert p.store.list_threads('fixture', eligible_only=True)[0]['detail']=='訂正した出来事'
    assert web.delete(f"/api/note/facts/{f['id']}").status_code==200
    assert not p.store.list_threads('fixture', eligible_only=True)


def test_ongoing_permission_enable_and_explicit_current_topic_gate(pipeline):
    p=pipeline;fid=fact(p.store,'ongoing_health');f=p.store.list_user_facts('fixture')[0];f['ref']=f'fact:{fid}'
    use=dict(ref=f['ref'],relevant_now='yes',counterevidence='yes',continuation='yes')
    packet=assemble_packet(p.character,PreJudge(memory_use=[use]),candidate_memories=[f])
    assert not packet['user_facts'] and not packet['counterevidence_memories']
    use['private_topic_current']='yes'
    assert assemble_packet(p.character,PreJudge(memory_use=[use]),candidate_memories=[f])['user_facts']
    p.store.set_private_settings('fixture',{'ongoing_health':True})
    assert p.store.list_user_facts('fixture')[0]['allow_mention']
    assert p.store.list_threads('fixture')[0]['status']=='open'
    with clock.at((clock.now()+timedelta(days=1)).date().isoformat()+' 12:00'):
        assert p.store.list_user_facts('fixture')[0]['may_initiate']


def test_inner_updates_allowed_thread_without_creating_private_want(pipeline):
    p=pipeline;fact(p.store);thread=p.store.list_threads('fixture', eligible_only=True)[0]
    want=dict(thread_ref=thread['ref'],private=True,direction='気にかける',motive='一度様子を聞きたい',topic='その後の具合',detail='細かいことは聞かない',weight=.6,trigger_ref=thread['source_ref'])
    p.client.inner_update_responses=[dict(feeling=dict(sentence='気にかけている。',levels=BASELINE),wants=[want,{**want,'thread_ref':None}])]
    update_inner(p,'m11c-control',{'kind':'conversation'})
    assert len(p.store.list_threads('fixture'))==1
    assert p.store.list_threads('fixture')[0]['status']=='open'
    call=next(c for c in p.client.calls if c.get('schema',{}).get('title')=='InnerUpdate')
    assert json.loads(call['messages'][0]['content'])['private_facts_policy'][0]['allow_mention']


def test_allowed_private_proactive_reaches_actor_and_consumes_once(pipeline):
    p=pipeline
    with clock.at('2026-10-19 12:00'):
        sid=p.store.create_session('fixture')['id'];p.store.add_turn(sid,'話した','聞いた',{})
        fact(p.store)
    with clock.at('2026-10-20 12:00'):
        t=p.store.list_threads('fixture', eligible_only=True)[0]
        p.client.proactive_responses=[dict(speak=True,reason='一度様子を聞く',thread_ref=t['ref'],want_ref=t['ref'],private_topic=True)]
        p.client.text_responses=['その後、具合はどう？']
        p.client.extract_responses=[{'ledger_updates':[],'new_canon':[],'disclosures':[],'used_memories':[],'used_threads':[t['ref']]}]
        p.client.json_responses=[{'problems':[]}]
        result=p.proactive(sid);p.wait_for_steward()
        assert result['speak'] and result['assistant_text']=='その後、具合はどう？'
        assert p.store.get_trace(result['turn_id'])['Extract'].get('state')=='completed', p.store.get_trace(result['turn_id'])['Extract']
        assert not p.store.list_threads('fixture', eligible_only=True)


def test_episodizer_classification_and_revision_preserve_override(pipeline):
    p=pipeline
    def save(operation, category, fid=None, thread=None):
        sid=p.store.create_session('fixture')['id'];p.store.add_turn(sid,'本人の発言','返答',{})
        p.client.episodizer_responses=[{'episodes':[dict(summary='体調の話',details='本人が話した',importance=.5,pinned=False)],
            'user_facts':[dict(operation=operation,fact_id=fid,private=True,private_category=category,content='体調の出来事',importance=.7,pinned=False,event_date=clock.now().date().isoformat(),follow_up=True,follow_up_thread=thread)]}]
        p.close_conversation(sid);p.wait_for_steward()
    save('new','temporary_health');f=p.store.list_user_facts('fixture', current_only=True)[0];t=p.store.list_threads('fixture')[0]
    with p.store.connect() as db:db.execute('UPDATE user_facts SET mention_override=0 WHERE id=?',(f['id'],))
    save('confirm','temporary_health',f['id'],t['id'])
    assert p.store.list_user_facts('fixture', current_only=True)[0]['mention_override'] is False
    save('update','ongoing_health',f['id'],t['id'])
    rows=p.store.list_user_facts('fixture', current_only=True)
    assert len(rows)==1 and rows[0]['private_category']=='ongoing_health' and rows[0]['mention_override'] is False
    assert not p.store.list_threads('fixture', eligible_only=True)


def test_legacy_database_migration_defaults_to_other(tmp_path):
    import sqlite3
    from cbs.memory_store import SCHEMA
    from cbs.store import Store
    path=tmp_path/'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute('ALTER TABLE user_facts ADD COLUMN private INTEGER NOT NULL DEFAULT 0')
        db.execute('INSERT INTO user_facts (character_id,content,learned_at,last_confirmed_at,importance,pinned,private) VALUES (?,?,?,?,?,?,?)',
            ('fixture','昔の明言',clock.iso(),clock.iso(),.5,0,1))
    migrated=Store(path)
    f=migrated.list_user_facts('fixture')[0]
    assert f['private_category']=='other' and not f['allow_mention'] and f['mention_override'] is None
    assert not migrated.list_threads('fixture', eligible_only=True)


def test_per_fact_no_blocks_a_proactive_selection_even_when_judge_says_speak(pipeline):
    p=pipeline
    with clock.at('2026-10-19 12:00'):
        sid=p.store.create_session('fixture')['id'];p.store.add_turn(sid,'話した','聞いた',{})
        fid=fact(p.store);thread=p.store.list_threads('fixture')[0]
    web=TestClient(create_app(p))
    assert web.put(f'/api/note/facts/{fid}/mention',json={'mention_override':False}).status_code==200
    with clock.at('2026-10-20 12:00'):
        p.client.proactive_responses=[dict(speak=True,reason='様子を聞く',thread_ref=thread['ref'],want_ref=thread['ref'],private_topic=True)]
        before=sum('schema' not in c for c in p.client.calls)
        assert p.proactive(sid)['speak'] is False
        assert sum('schema' not in c for c in p.client.calls)==before
