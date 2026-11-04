"""HB1 storage, calendar, post-reply and proactive delivery. Fake clients only."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from cbs import clock
from cbs.days import calendar_days, days_context, due_habits, initialize_birthday
from cbs.llm import FakeLLMClient
from cbs.note import public_note
from cbs.store import Store
from cbs.web import create_app


def day(kind='user_birthday', month=10, number=21, **kw):
    return dict(kind=kind, name='大切な日', month=month, day=number, year=None, **kw)


def habit(**kw):
    return dict(name='おはよう', time='07:00', weekdays=[0,1,2,3,4,5,6], allow_quiet=False, enabled=True) | kw


def save(p, days=(), habits=()):
    return p.store.save_days(p.config.character.id, list(days), list(habits), p.store.days_state(p.config.character.id)['version'])


def operation(p, rows, version=None):
    state = p.store.days_state('fixture')
    sid = p.store.create_session('fixture')['id']
    tid = p.store.add_turn(sid, '日付', '覚えたよ', {})
    with p.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        result = p.store.apply_day_operations(db, 'fixture', tid, rows, state['version'] if version is None else version)
    return result, tid


def test_dates_replace_no_year_unlimited_restart_scope_and_stale(pipeline):
    p = pipeline
    state = save(p, [day(), day('partner_birthday', 2, 29), *[day('anniversary', name=f'記念日{i}') for i in []]])
    ref = state['days'][0]['id']
    result, tid = operation(p, [day(number=22)])
    assert result['applied'] == [0]
    current = p.store.days_state('fixture')
    assert current['days'][0]['id'] == ref and current['days'][0]['day'] == 22
    assert current['days'][0]['year'] is None
    assert Store(p.store.path).days_state('fixture') == current
    assert p.store.days_state('other')['days'] == []
    assert p.store.day_notices('fixture', tid)[tid] == ['「大切な日」を大切な日として覚えました']
    result, _ = operation(p, [day(number=23)], state['version'])
    assert result['rejected'] and p.store.days_state('fixture') == current
    many = [day('anniversary') | {'name': f'記念日{i}'} for i in range(60)]
    assert len(save(p, many)['days']) == 60


@pytest.mark.parametrize('raw', [day(month=2, number=30), day(month=2, number=29) | {'year':2025},
    day() | {'name':''}, day() | {'id':'foreign'}, day() | {'kind':'other'}])
def test_invalid_day_operations_are_rejected_without_losing_valid_ones(pipeline, raw):
    result, _ = operation(pipeline, [raw, day('anniversary')])
    assert result['applied'] == [1] and result['rejected'] == [0]


def test_partner_birthday_once_and_named_anniversary_correction(pipeline):
    p = pipeline
    state = save(p, [day('partner_birthday'), day('anniversary')])
    result, _ = operation(p, [day('partner_birthday', 12, 1), day('anniversary', 3, 1, id=state['days'][1]['id'])])
    assert result['applied'] == [1]
    assert p.store.days_state('fixture')['days'][0]['month'] == 10
    assert p.store.days_state('fixture')['days'][1]['month'] == 3
    # The one settings editor remains the explicit correction path.
    rows = p.store.days_state('fixture')['days'];rows[0]['month'] = 11
    assert save(p, rows)['days'][0]['month'] == 11


def test_calendar_existing_day_count_hundred_years_nearby_and_leap(pipeline):
    p = pipeline
    sid = p.store.create_session('fixture')['id']
    p.store.add_turn(sid, '最初', 'こんにちは', {}, created_at='2026-07-13T23:00:00+09:00')
    save(p, [day(), day('anniversary', 11, 5), day('partner_birthday', 2, 29)])
    with clock.at('2026-10-21 12:00'):
        candidates = calendar_days(p.store, 'fixture', p.config.days)
        assert next(d for d in candidates if d['id']=='auto:100')['in_days'] == 0
        assert next(d for d in candidates if d['kind']=='partner_birthday')['date']=='2028-02-29'
        nearby = public_note(p)['nearby_days']
        assert len(nearby)==2 and all(d['in_days']==0 for d in nearby)
        assert next(d for d in days_context(p.store,'fixture',p.config.days,user_reply=True)['today'] if d['kind']=='user_birthday')['may_mention']
        # Imported UTC timestamps still count on their local calendar day.
        p.store.add_turn(sid,'取り込みの会話','返答',{},created_at='2026-10-20T15:05:00+00:00')
        assert not next(d for d in days_context(p.store,'fixture',p.config.days,user_reply=True)['today'] if d['kind']=='user_birthday')['first_user_reply']
    with clock.at('2027-07-13 12:00'):
        assert next(d for d in calendar_days(p.store,'fixture',p.config.days) if d['id']=='auto:year:1')['in_days']==0
    p.config.days.yearly=False;p.config.days.hundred_days=False
    assert not any(d['id'].startswith('auto:') for d in calendar_days(p.store,'fixture',p.config.days))


def test_extract_sse_packet_check_and_history_after_reply(pipeline):
    p = pipeline
    p.client = FakeLLMClient(text_responses=['覚えておくね。'], json_responses=[{}, {'problems':[]}],
        extract_responses=[{'ledger_updates':[], 'new_canon':[], 'disclosures':[], 'used_memories':[], 'day_operations':[day()], 'used_days':[]}])
    sid=p.store.create_session('fixture')['id'];events=[]
    with clock.at('2026-10-21 12:00'):
        response=p.run(sid,'今日が誕生日です',event_sink=lambda kind,data:events.append((kind,data)))
        p.wait_for_steward()
    tid=response['turn_id']
    assert p.store.days_state('fixture')['days'][0]['kind']=='user_birthday'
    assert next(i for i,e in enumerate(events) if e[0]=='reply') < next(i for i,e in enumerate(events) if e[0]=='important_days')
    actor=next(c for c in p.client.calls if c.get('schema') is None)
    assert 'important_days' in json.loads(actor['messages'][0]['content'])['packet']
    check=next(c for c in p.client.calls if c.get('schema') and c['schema']['title']=='Check')
    assert 'important_days' in json.loads(check['messages'][0]['content'])['packet']
    with TestClient(create_app(p)) as client:
        assert client.get(f'/api/sessions/{sid}/turns').json()[0]['day_notices']


def test_day_mentions_first_reply_proactive_and_next_day(pipeline):
    p=pipeline;state=save(p,[day(),day('partner_birthday')])
    sid=p.store.create_session('fixture')['id']
    p.client=FakeLLMClient(text_responses=['お誕生日おめでとう。'],json_responses=[{}, {'problems':[]}],
        extract_responses=[{'ledger_updates':[], 'new_canon':[], 'disclosures':[], 'used_memories':[], 'used_days':[state['days'][0]['id'],'foreign']}])
    with clock.at('2026-10-21 12:00'):
        response=p.run(sid,'こんにちは');p.wait_for_steward()
        context=days_context(p.store,'fixture',p.config.days)
        assert not next(d for d in context['today'] if d['kind']=='user_birthday')['may_mention']
        assert next(d for d in context['today'] if d['kind']=='partner_birthday')['may_mention']
        assert not any(d['may_mention'] for d in days_context(p.store,'fixture',p.config.days,user_reply=True)['today'])
        p.client.proactive_responses=[{'speak':True,'reason':'お祝い','day_refs':[state['days'][1]['id']]}]
    with clock.at('2026-10-21 15:00'):
        p.client.text_responses=['今日は私の誕生日だね。'];p.client.json_responses=[{'problems':[]}]
        assert p.proactive(sid)['speak'];p.wait_for_steward()
        assert not any(d['may_mention'] for d in days_context(p.store,'fixture',p.config.days)['today'])
    with clock.at('2027-10-21 12:00'):
        assert all(d['may_mention'] for d in days_context(p.store,'fixture',p.config.days)['today'])


@pytest.mark.parametrize('answer,expected', [(None,False), (day('partner_birthday',3,9),True)])
def test_author_initial_once_explicit_quote_and_empty(pipeline, answer, expected):
    p=pipeline
    with p.store.connect() as db:db.execute('DELETE FROM birthday_initializations')
    p.character.sources={'author':'誕生日は3月9日です。'}
    p.client.birthday_initial_responses=[{'birthday':answer,'source_ref':'author','quote':'誕生日は3月9日です。'}]
    before=len(p.client.calls)
    initialize_birthday(p);initialize_birthday(p)
    assert len(p.client.calls)==before+1
    assert bool(p.store.days_state('fixture')['days']) is expected


def test_author_initial_rejects_unverified_quote_and_preserves_settings(pipeline):
    p=pipeline
    with p.store.connect() as db:db.execute('DELETE FROM birthday_initializations')
    p.client.birthday_initial_responses=[{'birthday':day('partner_birthday'),'source_ref':'missing','quote':'x'}]
    initialize_birthday(p)
    assert not p.store.days_state('fixture')['days']


def test_author_initial_does_not_replace_a_decided_partner_birthday(pipeline):
    p=pipeline
    save(p,[day('partner_birthday')])
    with p.store.connect() as db:db.execute('DELETE FROM birthday_initializations')
    p.character.sources={'author':'誕生日は3月9日です。'}
    p.client.birthday_initial_responses=[{'birthday':day('partner_birthday',3,9),'source_ref':'author','quote':'誕生日は3月9日です。'}]
    initialize_birthday(p)
    assert p.store.days_state('fixture')['days'][0]['month']==10


def test_asked_partner_birthday_is_saved_from_the_final_reply(pipeline):
    p=pipeline
    p.client=FakeLLMClient(text_responses=['私は3月9日だよ。'],json_responses=[{}, {'problems':[]}],
        extract_responses=[{'ledger_updates':[], 'new_canon':[], 'disclosures':[], 'used_memories':[],
                            'day_operations':[day('partner_birthday',3,9)]}])
    sid=p.store.create_session('fixture')['id']
    response=p.run(sid,'あなたの誕生日はいつ？');p.wait_for_steward()
    state=p.store.days_state('fixture')
    assert state['days'][0]['month']==3 and state['days'][0]['day']==9
    assert p.store.day_notices('fixture',response['turn_id'])[response['turn_id']]


@pytest.mark.parametrize('at,weekdays,allow,expected', [('06:44',[2],True,False),('06:45',[2],True,True),
    ('07:15',[2],True,True),('07:16',[2],True,False),('07:00',[3],True,False),('07:00',[2],False,False)])
def test_habit_window_weekday_and_quiet(pipeline, at, weekdays, allow, expected):
    p=pipeline;save(p,habits=[habit(weekdays=weekdays,allow_quiet=allow)])
    with clock.at('2026-10-21 '+at):
        assert bool(due_habits(p.store,'fixture',p.config.days,p.config.autonomy)) is expected


def test_habit_quiet_exception_daily_cap_independent_no_answer_and_restart(pipeline):
    p=pipeline;save(p,habits=[habit(allow_quiet=True)])
    p.config.autonomy.idle_nudge=False;p.config.autonomy.opening='off'
    p.client=FakeLLMClient(proactive_responses=[{'speak':True,'reason':'朝の習慣'}],text_responses=['おはよう。'],json_responses=[{'problems':[]}])
    sid=p.store.create_session('fixture')['id']
    with clock.at('2026-10-21 07:00'):
        result=p.proactive(sid,'idle');p.wait_for_steward()
        assert result['speak'] and result['habit']['allow_quiet']
        assert not p.proactive(sid,'habit')['speak']
        assert not due_habits(Store(p.store.path),'fixture',p.config.days,p.config.autonomy)
    decision=next(c for c in p.client.calls if c.get('schema') and c['schema']['title']=='Proactive')
    data=json.loads(decision['messages'][0]['content']);assert data['mode']=='habit' and data['habit']['time']=='07:00'
    with clock.at('2026-10-22 07:00'):
        assert due_habits(p.store,'fixture',p.config.days,p.config.autonomy)
    assert p.store.list_proactive('fixture')[0]['mode']=='habit'


def test_habit_silent_once_and_midnight_scheduled_day(pipeline):
    p=pipeline;save(p,habits=[habit(time='00:00',weekdays=[3],allow_quiet=True)])
    sid=p.store.create_session('fixture')['id']
    with clock.at('2026-10-21 23:50'):
        assert p.proactive(sid,'habit')['speak'] is False
    with clock.at('2026-10-22 00:05'):
        assert not p.proactive(sid,'habit')['speak']
    assert len([c for c in p.client.calls if c.get('schema') and c['schema']['title']=='Proactive'])==1


def test_habit_claim_is_atomic_and_editor_change_cancels_inflight_decision(pipeline):
    p=pipeline;save(p,habits=[habit(allow_quiet=True)])
    entered,release=Event(),Event()
    class Blocking(FakeLLMClient):
        def generate_json(self,model,system,messages,schema):
            if schema.__name__=='Proactive':
                entered.set();assert release.wait(5)
            return super().generate_json(model,system,messages,schema)
    p.client=Blocking(proactive_responses=[{'speak':True,'reason':'習慣'}])
    sid=p.store.create_session('fixture')['id']
    def attempt():
        with clock.at('2026-10-21 07:00'):
            return p.proactive(sid,'habit')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(attempt)
        try:
            assert entered.wait(3)
            save(p,habits=[])
        finally:
            release.set()
        assert not future.result(5)['speak']
    assert not any(not c.get('schema') for c in p.client.calls)
    with p.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM habit_runs').fetchone()[0]==1


def test_api_editor_validation_scope_and_no_get_generation(pipeline):
    p=pipeline
    with TestClient(create_app(p)) as client:
        p.wait_for_steward()
        before=len(p.client.calls)
        state=client.get('/api/partner/days').json()
        assert len(p.client.calls)==before
        response=client.put('/api/partner/days',json={**state,'days':[day()],'habits':[habit()]})
        assert response.status_code==200
        assert client.get('/api/settings').json()['days']['has_habits']
        assert client.put('/api/partner/days',json=state).status_code==422
        bad=response.json();bad['days'][0]['month']=13
        assert client.put('/api/partner/days',json=bad).status_code==422
        for time in ('24:00','07:٠٠'):
            bad=response.json();bad['habits'][0]['time']=time
            assert client.put('/api/partner/days',json=bad).status_code==422


def test_prompt_semantics_exceptions_and_no_new_foreground_calls():
    from cbs.runtime import read_prompt
    for name in ('actor','check','extract'):
        assert '限定的例外' in read_prompt(name)
    assert 'mode=habit' in read_prompt('proactive')
    assert '他人の誕生日' in read_prompt('extract')
