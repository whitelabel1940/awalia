import base64
import json
import threading

import pytest
from fastapi.testclient import TestClient

from cbs.bootstrap import Bootstrapper, checked_reader, load_questionnaire
from cbs.bootstrap_schema import BootstrapInput, ReaderOutput
from cbs.import_profiles import import_pipeline, partner_sources, review_about, save_about
from cbs.import_jobs import ImportJobs
from cbs.llm import FakeLLMClient
from cbs.web import create_app
from shared_helpers import payload
from test_bootstrap import package_data
from test_web_bootstrap import finish


@pytest.mark.parametrize('background',[False,True])
def test_welcome_import_owned_before_and_after_publication(pipeline,tmp_path,monkeypatch,background):
    root=tmp_path/'characters'
    app=create_app(pipeline,characters_root=root,work_root=tmp_path/'work',settings_dir=tmp_path/'settings')
    cid='new_partner'
    with TestClient(app) as client:
        path='/api/bootstrap/import/'+cid
        p=client.post(path+'/preview',json={'filename':'x.json','payload':base64.b64encode(json.dumps(payload()).encode()).decode()}).json()
        q=client.post(path+'/plan',json={'preview_id':p['preview_id'],'keys':[p['conversations'][0]['key']]}).json()
        entered,release=threading.Event(),threading.Event()
        jobs=app.state.bootstrap_import_jobs
        original=jobs._reflect
        def delayed(*args):
            entered.set()
            assert release.wait(15)
            return original(*args)
        if background:monkeypatch.setattr(jobs,'_reflect',delayed)
        assert client.post(path+'/start',json={'token':q['token'],'confirmed':True}).status_code==200
        if background:assert entered.wait(5)
        else:jobs.futures[q['token']].result(20)
        assert pipeline.store.relationship_metrics(cid)['days']==1
        assert not pipeline.store.list_memory_episodes('fixture')
        assert not root.exists()
        assert client.get('/api/bootstrap/import/other/'+q['token']).status_code==404
        data=BootstrapInput(id=cid,basics={'name':'New'},import_batch_ids=[q['token']],now={'likes':[{'topic':'Tea','elaborate':'keep'}]})
        pipeline.client=FakeLLMClient(json_responses=[{}, package_data(data),{'problems':[]},{'introduction':'Continued','open_now':[]}])
        created=finish(client,client.post('/api/bootstrap/create',json=data.model_dump(mode='json',by_alias=True)))
        assert created['status']=='done',created
        context=json.loads(pipeline.client.calls[0]['messages'][0]['content'])
        assert context['imported_partner_messages'][0]['text']=='ボクの言葉'
        assert '昔の話' not in context['text']
        assert client.post('/api/bootstrap/jobs/'+created['id']+'/confirm').status_code==200
        release.set()
        jobs.futures[q['token']].result(20)
        assert pipeline.store.previous_utterance(cid).startswith('2026-04-01')
        assert pipeline.store.list_memory_episodes(cid)[0]['created_at'].startswith('2026-04-01')
        assert client.get(path).status_code==409
        assert client.get('/api/import?character_id='+cid).json()[0]['status']=='completed'


def test_reader_reuses_quote_verification_and_rejects_cross_message_quotes(pipeline,tmp_path):
    sources=['ボクはほうじ茶が好き。','カイ、またね。']
    reader=ReaderOutput.model_validate({'items':[{'id':'text:1','category':'now','content':'好き','quote':'好き。カイ','covers':[]}],
        'voice':{'first_person':{'value':'ボク','quote':'ボクは'}}})
    checked,problems=checked_reader(reader,sources,load_questionnaire())
    assert problems and not checked.items and checked.voice.first_person.value=='ボク'
    jobs=ImportJobs(lambda _:pipeline)
    p=jobs.preview(pipeline,'x.json',json.dumps(payload()).encode());q=jobs.quote(pipeline,p['preview_id'],[p['conversations'][0]['key']])
    with pytest.raises(ValueError):partner_sources(pipeline.store,'other',[q['token']])
    with pytest.raises(ValueError):partner_sources(pipeline.store,'fixture',[q['token']]) # unaccepted quote


def test_import_ai_conflict_blocks_builder_until_llm_rechecks_choice(pipeline,tmp_path):
    jobs=ImportJobs(lambda _:pipeline)
    p=jobs.preview(pipeline,'x.json',json.dumps(payload(assistant='AIとして話します')).encode())
    q=jobs.quote(pipeline,p['preview_id'],[p['conversations'][0]['key']]);jobs.start(pipeline,q['token'],True);jobs.futures[q['token']].result(20)
    reader={'setting_conflicts':[{'setting':'persona.ai_question','topic':'答え方','quote':'AIとして','detail':'選んだ答え方と異なる'}]}
    pipeline.client=FakeLLMClient(json_responses=[reader,{}])
    engine=Bootstrapper(pipeline.config,pipeline.client,store=pipeline.store,characters_root=tmp_path/'characters',trace_root=tmp_path/'traces')
    data=BootstrapInput(id='fixture',basics={'name':'テスト係'},import_batch_ids=[q['token']])
    with pytest.raises(ValueError,match='確認が必要'):engine.create(data)
    assert len(pipeline.client.calls)==2 # Reader + independent review, no Builder
    from cbs.bootstrap_schema import BootstrapPersona
    data.persona=BootstrapPersona(ai_question='honest')
    data.review_notes={'setting:AIとして':'正直に答える'}
    assert not engine.read(data).setting_conflicts


def test_import_builder_fix_is_semantically_rechecked(pipeline,tmp_path):
    jobs=ImportJobs(lambda _:pipeline)
    preview=jobs.preview(pipeline,'x.json',json.dumps(payload()).encode())
    q=jobs.quote(pipeline,preview['preview_id'],[preview['conversations'][0]['key']]);jobs.start(pipeline,q['token'],True);jobs.futures[q['token']].result(20)
    data=BootstrapInput(id='fixture',basics={'name':'テスト係'},import_batch_ids=[q['token']],now={'likes':[{'topic':'Tea','elaborate':'keep'}]})
    package=package_data(data)
    pipeline.client=FakeLLMClient(json_responses=[{},package,{'problems':['Same unknown has two secrets']},package,{'problems':['Still inconsistent']}])
    engine=Bootstrapper(pipeline.config,pipeline.client,store=pipeline.store,characters_root=tmp_path/'characters',trace_root=tmp_path/'traces')
    with pytest.raises(ValueError,match='Still inconsistent'):engine.create(data)
    assert not (tmp_path/'characters/fixture').exists()


def test_import_unknown_labels_require_unambiguous_ownership():
    from cbs.bootstrap import validate_package
    from cbs.bootstrap_schema import CharacterPackage
    from cbs import clock
    data=BootstrapInput(id='test',basics={'name':'Test'},import_batch_ids=['source'],now={'likes':[{'topic':'Tea','elaborate':'keep'}]})
    raw=package_data(data)
    for field,ident in [('phases','phase'),('events','event')]:
        raw['model']['life_history'][field]=[{'id':ident,'kind':'past','known':'known','evidence':[],'open':['same unknown'],'major_unknowns':['same unknown']}]
    package=CharacterPackage.model_validate(raw)
    errors=validate_package(package,data,ReaderOutput(),load_questionnaire(),clock.now().date())
    assert any('one owning life_ref' in e for e in errors)


def test_reader_review_corrects_mixed_items_and_their_domain_quotes(pipeline,tmp_path):
    from cbs.bootstrap_schema import ReaderReview
    first='今日はカイの話を短く聞くよ。'
    second='働けるかでカイの大切さは決まらない。'
    pipeline.client=FakeLLMClient(json_responses=[{'items':[{'id':'text:1','category':'relationship','content':'二つの返答が混ざった古い読み取り','quote':first}]}],
        reader_review_responses=[{'voice':{},'revised_items':[{'id':'text:1','category':'relationship','content':'疲れた日は短く聞く','quote':first}],
            'missing_items':[{'id':'ignored','category':'relationship','content':'働けるかで大切さは決まらないと肯定する','quote':second}],
            'mentions':[{'domain':'R01','kind':'text','quotes':[first,second]}]}])
    engine=Bootstrapper(pipeline.config,pipeline.client,trace_root=tmp_path/'traces')
    reader=engine.read_text(first+second)
    assert [i.id for i in reader.items]==['text:1','text:review:1']
    assert reader.items[0].content=='疲れた日は短く聞く'
    assert reader.items[1].quote==second
    assert reader.mentions[0].quotes==[first,second]


def test_reader_review_cannot_invent_revision_ids(pipeline,tmp_path):
    revised={'revised_items':[{'id':'text:forged','category':'now','content':'原文','quote':'原文'}]}
    pipeline.client=FakeLLMClient(json_responses=[{}],reader_review_responses=[revised,revised])
    engine=Bootstrapper(pipeline.config,pipeline.client,trace_root=tmp_path/'traces')
    with pytest.raises(ValueError,match='existing Reader item ID'):engine.read_text('原文')
    retry=json.loads(pipeline.client.calls[-1]['messages'][0]['content'])
    assert retry['previous_review']['revised_items'][0]['id']=='text:forged'


def test_import_support_count_is_applied_by_code(pipeline,tmp_path):
    import yaml
    quotes=['疲れた日は短く聞くよ。','不安な時は急がず聞くよ。']
    jobs=ImportJobs(lambda _:pipeline)
    p=jobs.preview(pipeline,'x.json',json.dumps([payload('one',assistant=quotes[0]),payload('two',at='2026-05-01',assistant=quotes[1])]).encode())
    q=jobs.quote(pipeline,p['preview_id'],[c['key'] for c in p['conversations']]);jobs.start(pipeline,q['token'],True);jobs.futures[q['token']].result(20)
    data=BootstrapInput(id='fixture',basics={'name':'テスト係'},import_batch_ids=[q['token']],now={'likes':[{'topic':'Tea','elaborate':'keep'}]})
    items=[{'id':f'text:{i+1}','category':'relationship','content':quote,'quote':quote} for i,quote in enumerate(quotes)]
    reader={'items':items,'mentions':[{'domain':'R01','kind':'text','quotes':quotes}]}
    package=package_data(data)
    next(d for d in package['model']['domains'] if d['id']=='R01').update(status='supported',known='相手の状況に合わせて聞く',evidence=['text:1','text:2'])
    package['expression']['items']=[{'ref':i['id'],'content':i['content'],'elaborate':'may_elaborate'} for i in items]
    pipeline.client=FakeLLMClient(json_responses=[reader,package,{'problems':[]},{'introduction':'続き','open_now':[]}])
    engine=Bootstrapper(pipeline.config,pipeline.client,store=pipeline.store,characters_root=tmp_path/'characters',trace_root=tmp_path/'traces')
    directory=engine.create(data)
    saved=yaml.safe_load((directory/'model.yaml').read_text(encoding='utf-8'))
    assert next(d for d in saved['domains'] if d['id']=='R01')['status']=='well_supported'
    assert len(pipeline.client.calls)==5 # no Builder retry for the deterministic count


def test_paste_review_explicit_subset_private_scoped_idempotent_and_undo(pipeline):
    pipeline.client=FakeLLMClient(json_responses=[{'facts':[
        {'content':'紅茶が好き','quote':'紅茶が好き','private':False,'private_category':None},
        {'content':'片頭痛が続いている','quote':'片頭痛が続いている','private':True,'private_category':'ongoing_health'}]}])
    reviewed=review_about(pipeline,'紅茶が好き。片頭痛が続いている。')
    assert not pipeline.store.list_user_facts('fixture')
    with pytest.raises(ValueError):save_about(pipeline,reviewed['token'],['fabricated'])
    with pytest.raises(KeyError):save_about(import_pipeline(pipeline,'other'),reviewed['token'],['0'])
    result=save_about(pipeline,reviewed['token'],['1'])
    assert result['saved']==1
    assert save_about(pipeline,reviewed['token'],['1'])==result
    fact=pipeline.store.list_user_facts('fixture')[0]
    assert fact['private'] and fact['private_category']=='ongoing_health' and not fact['may_initiate']
    assert len(pipeline.client.embedding_calls)==1
    assert not pipeline.store.list_observations('fixture')
    pipeline.store.undo_import('fixture',reviewed['token'])
    assert not pipeline.store.list_user_facts('fixture')
    with pipeline.store.connect() as db:
        assert db.execute('SELECT text,candidates_json FROM import_reviews WHERE id=?',(reviewed['token'],)).fetchone()[:]==('', '[]')
    with pytest.raises(ValueError):save_about(pipeline,reviewed['token'],['1'])


@pytest.mark.parametrize('fact',[
    {'content':'誤った引用','quote':'原文にない','private':False,'private_category':None},
    {'content':'健康','quote':'健康','private':True,'private_category':'invented'},
])
def test_about_unverifiable_output_never_persisted(pipeline,fact):
    pipeline.client=FakeLLMClient(json_responses=[{'facts':[fact]}])
    with pytest.raises(ValueError):review_about(pipeline,'健康です。')
    with pipeline.store.connect() as db:assert db.execute('SELECT COUNT(*) FROM import_reviews').fetchone()[0]==0
