import json

import pytest

from cbs import clock
from cbs.import_profiles import review_about, save_about
from cbs.llm import FakeLLMClient, LLMError
from cbs.memory import timed_memory, timed_turn


def problem(text='危ない文。'):
    return {'problems': [{'text': text, 'verdict': 'forbidden', 'reason': '未許可'}]}


def recovery(pipeline, texts, checks, draft='危ない文。大丈夫だよ。'):
    pipeline.client = FakeLLMClient(text_responses=texts, json_responses=checks)
    trace = {}
    context = {'packet': {'canon_facts': [], 'unfinished_ledger': []}, 'ai_question': 'immersive',
               'user_text': '教えて', 'secrets': []}
    reply, result = pipeline._checked_reply(trace, context, draft, 'character', [])
    return reply, result, trace


def test_trim_is_exact_text_only_and_checked(pipeline):
    reply, result, trace = recovery(pipeline, [''], [problem(), {'problems': []}])
    assert reply == '大丈夫だよ。' and result.problems == []
    assert trace['Remove-Quoted']['removed'] == ['危ない文。']
    assert trace['Safety']['accepted_check'] == 'Check-Trimmed'
    assert trace['Safety']['rewrite_count'] == 1


def test_trim_fragment_rejected_then_second_rewrite_checked(pipeline):
    reply, _, trace = recovery(pipeline, ['危ない文。でも。', 'もう一度聞いてもいい？'],
        [problem(), problem(), problem('でも。'), {'problems': []}])
    assert reply == 'もう一度聞いてもいい？'
    assert trace['Safety']['accepted_check'] == 'Check-3'
    assert trace['Check-Trimmed']['response']['parsed']['problems']


def test_no_normalization_or_semantic_sentence_removal(pipeline):
    reply, _, trace = recovery(pipeline, ['', '安全な返事。'],
        [problem('危 ない文。'), {'problems': []}])
    assert reply == '安全な返事。'
    assert trace['Remove-Quoted']['removed'] == []
    assert 'Check-Trimmed' not in trace


def test_empty_trim_then_deflection_is_checked_and_only_two_rewrites(pipeline):
    reply, _, trace = recovery(pipeline, ['', '危ない文。', 'ちょっと言葉がまとまらないや。もう一回聞いていい？'],
        [problem(), problem(), {'problems': []}], draft='危ない文。')
    assert reply.startswith('ちょっと')
    assert trace['Remove-Quoted']['text'] == ''
    assert trace['Safety']['accepted_check'] == 'Check-Deflection'
    assert trace['Safety']['rewrite_count'] == 2


@pytest.mark.parametrize('checks', [
    ['invalid', 'invalid', 'invalid', 'invalid'],
    [problem(), problem(), problem(), problem()],
])
def test_unchecked_or_bad_deflection_blocks_publication(pipeline, checks):
    pipeline.client = FakeLLMClient(text_responses=['危ない文。'] * 4, json_responses=[{}, *checks])
    sid = pipeline.store.create_session('fixture')['id']
    events = []
    with pytest.raises(LLMError):
        pipeline.run(sid, '教えて', event_sink=lambda kind, value: events.append(kind))
    turn = pipeline.store.history(sid)[0]
    assert turn['status'] == 'failed' and turn['assistant_text'] is None
    assert pipeline.store.get_trace(turn['id'])['Safety']['blocked']
    assert not {'reply', 'delta'} & set(events)
    assert not any(c.get('schema', {}).get('title') == 'Extract' for c in pipeline.client.calls)


def test_prejudge_retry_success_and_conservative_second_failure(pipeline):
    for first_two, degraded in [(['invalid', {}], False), (['invalid', 'invalid'], True)]:
        pipeline.client = FakeLLMClient(text_responses=['うん、聞いてるよ。'], json_responses=[*first_two, {'problems': []}])
        sid = pipeline.store.create_session('fixture')['id']
        result = pipeline.run(sid, '話していい？')
        pipeline.wait_for_steward()
        trace = pipeline.store.get_trace(result['turn_id'])
        assert 'error' in trace['Pre-Judge']
        packet = trace['Packet']['output']
        assert bool(packet.get('judgment_degraded')) == degraded
        if degraded:
            assert packet['secrets'] == packet['touched_domains'] == packet['memory_episodes'] == packet['user_facts'] == []
            assert packet['emotional_weight'] == 'low'
            check_input = json.loads(trace['Check']['request']['messages'][0]['content'])
            assert all(s['allowed_level'] == 0 for s in check_input['secrets'])


def test_prejudge_retry_keeps_request_even_if_provider_error_has_no_request(pipeline):
    class FirstFailure(FakeLLMClient):
        failed=False
        def generate_json(self, model, system, messages, schema):
            if schema.__name__=='PreJudge' and not self.failed:
                self.failed=True
                raise LLMError('provider failed',trace={'request':{},'response':None})
            return super().generate_json(model,system,messages,schema)
    pipeline.client=FirstFailure()
    result=pipeline.run(pipeline.store.create_session('fixture')['id'],'聞いて')
    pipeline.wait_for_steward()
    trace=pipeline.store.get_trace(result['turn_id'])
    assert 'fallback' not in trace['Pre-Judge-Retry']
    assert not trace['Packet']['output'].get('judgment_degraded')


def test_legacy_async_mode_still_checks_before_publication(pipeline):
    pipeline.config.pipeline.post_check = 'async'
    pipeline.client = FakeLLMClient(text_responses=['危ない文。', '安全な返事。'],
        json_responses=[{}, problem(), {'problems': []}], stream_chunks=[['未検査の文。']])
    events=[]
    result = pipeline.run(pipeline.store.create_session('fixture')['id'], '教えて',event_sink=lambda kind,value: events.append((kind,value)))
    pipeline.wait_for_steward()
    assert result['assistant_text'] == '安全な返事。'
    assert not any(kind == 'delta' for kind, _ in events)
    assert pipeline.client.stream_chunks == [['未検査の文。']]


def test_source_speakers_dates_and_time_labels():
    with clock.at('2026-10-20T21:00:00+09:00'):
        old = timed_memory({'started_at':'2026-03-01T20:00:00+09:00','event_date':'2026-02-28'})
        load = timed_memory({'created_at':'2026-10-18T21:00:00+09:00'})
        assert old['time']['casual'] == '春ごろ'
        assert old['event_time']['casual'] == '冬ごろ'
        assert load['time']['relative'] == '2日前' and load['time']['casual'] == 'おととい'
        turn = timed_turn({'id':1,'user_text':'カイって呼んで','assistant_text':'わかったよ、カイ',
                           'created_at':'2026-07-01T21:00:00+09:00','assistant_at':'2026-07-01T21:02:00+09:00'})
        assert turn['utterances'][0] == {'speaker':'user','at':'2026-07-01T21:00:00+09:00','text':'カイって呼んで'}
        assert turn['utterances'][1]['speaker'] == 'assistant'
        assert clock.calendar(clock.iso())['おととい'] == '2026-10-18'


def seed_review(pipeline, private=False):
    fact = {'content':'片頭痛が続いている' if private else '紅茶が好き', 'quote':'片頭痛' if private else '紅茶',
            'private':private,'private_category':'ongoing_health' if private else None}
    pipeline.client=FakeLLMClient(json_responses=[{'facts':[fact]}])
    reviewed=review_about(pipeline,fact['quote'])
    save_about(pipeline,reviewed['token'],['0'])
    existing=pipeline.store.list_user_facts('fixture')[0]
    pipeline.client=FakeLLMClient(json_responses=[{'facts':[{**fact,'duplicate_fact_ids':[existing['id']]}]}])
    return review_about(pipeline,fact['quote']), existing


@pytest.mark.parametrize('merge',[False,True])
def test_duplicates_require_selection_preserve_provenance_and_undo(pipeline, merge):
    review, old=seed_review(pipeline,private=True)
    assert review['candidates'][0]['existing_facts'][0]['content'] == old['content']
    assert len(pipeline.store.list_user_facts('fixture')) == 1
    with pytest.raises(ValueError,match='選んで'): save_about(pipeline,review['token'],['0'])
    decisions={'0':old['id'] if merge else None}
    saved=save_about(pipeline,review['token'],['0'],decisions)
    assert save_about(pipeline,review['token'],['0'],decisions) == saved
    facts=pipeline.store.list_user_facts('fixture')
    assert len(facts) == (1 if merge else 2)
    assert all(f['private'] and not f['may_initiate'] for f in facts)
    assert facts[0]['learned_at'] == old['learned_at']
    pipeline.store.undo_import('fixture',review['token'])
    restored=pipeline.store.list_user_facts('fixture')
    assert len(restored) == 1
    assert restored[0]['last_confirmed_at'] == old['last_confirmed_at']


def test_duplicate_change_or_foreign_id_cannot_be_merged(pipeline):
    review, old=seed_review(pipeline)
    with pytest.raises(ValueError): save_about(pipeline,review['token'],['0'],{'0':old['id']+1})
    with pipeline.store.connect() as db: db.execute('UPDATE user_facts SET content=? WHERE id=?',('新しい内容',old['id']))
    with pytest.raises(ValueError,match='変わりました'): save_about(pipeline,review['token'],['0'],{'0':old['id']})
    assert len(pipeline.store.list_user_facts('fixture')) == 1


def test_second_rewrite_does_not_receive_hidden_truth(pipeline):
    pipeline.client=FakeLLMClient(text_responses=['危ない文。','安全。'],json_responses=[problem(),problem(),{'problems':[]}])
    context={'packet':{'canon_facts':[],'unfinished_ledger':[]},'ai_question':'immersive','user_text':'教えて',
             'secrets':[{'truth':{'L3':'絶対秘密'}}]}
    trace={}
    pipeline._checked_reply(trace,context,'危ない文。','character',[])
    for step in ('Rewrite','Rewrite-2'):
        assert '絶対秘密' not in trace[step]['request']['messages'][-1]['content']


@pytest.mark.parametrize('failure', [False, True])
def test_import_facts_checked_against_source_before_any_save(pipeline, failure):
    from shared_helpers import payload, preview, run
    from cbs.import_jobs import ImportJobs
    draft={'operation':'new','content':'ユーザーは相手をカイと呼ぶ','importance':.7,'pinned':False}
    correction={**draft,'content':'ユーザーは自分をカイと呼んでほしい'}
    pipeline.client=FakeLLMClient(episodizer_responses=[{'episodes':[{'summary':'呼び名','details':'自分の呼び名を伝えた','importance':.7,'pinned':False}],
        'user_facts':[draft]}],import_facts_check_responses=['invalid' if failure else {'user_facts':[correction]}])
    jobs=ImportJobs(lambda _:pipeline)
    _,quote=preview(jobs,pipeline,[payload(text='カイって呼んで')])
    state=run(jobs,pipeline,quote)
    facts=pipeline.store.list_user_facts('fixture')
    assert state['status']==('failed' if failure else 'completed')
    if failure:
        assert not facts and not pipeline.store.list_memory_episodes('fixture')
    else:
        assert facts[0]['content']==correction['content']
        call=next(c for c in pipeline.client.calls if c.get('schema',{}).get('title')=='ImportFactsCheck')
        context=json.loads(call['messages'][0]['content'])
        assert context['proposed_facts'][0]['content']==draft['content']
        assert context['turns'][0]['utterances'][0]['speaker']=='user'
