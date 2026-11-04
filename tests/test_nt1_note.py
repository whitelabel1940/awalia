"""NT1 read projections: frozen time, isolated state, fake LLM only."""
import json

import pytest

from cbs import clock
from cbs.note import public_note
from cbs.note_scenes import load_state
from cbs.web.knowing_api import knowing_note
from shared_helpers import record, trait_version
from cbs.note import fill_forming_notes


@pytest.mark.parametrize('now,expected', [
    ('2026-10-25T23:59:59+09:00', True),
    ('2026-10-26T00:00:00+09:00', False),
    ('2026-10-26T00:00:01+09:00', False),
])
def test_new_boundary_and_saved_summary(pipeline, now, expected):
    p = pipeline
    record(p, '2026-10-19')
    record(p, '2026-10-20')
    with clock.at('2026-10-20T12:00:00+09:00'):
        fill_forming_notes(p)
    before = load_state(p.store, 'fixture')
    with p.store.connect() as db:
        for status in ('established', 'tentative'):
            db.execute("INSERT INTO interests(character_id,topic,kind,detail,origin,status,first_seen,last_seen) VALUES ('fixture',?,'curiosity','','conversation',?,?,?)",
                       (status, status, '2026-10-19T00:00:00+09:00', now))
    with clock.at(now):
        note = public_note(p)
    interest = next(i for i in note['interests'] if i['topic'] == 'established')
    assert interest['new'] is expected
    assert not any(i['topic'] == 'tentative' for i in note['interests'])
    group = note['self_forming'][0]
    assert group['summary']
    assert 'new' not in group['scenes'][0]
    assert load_state(p.store, 'fixture') == before
    assert '"new"' not in json.dumps(before)


@pytest.mark.parametrize('now,expected', [
    ('2026-10-27T11:59:59+09:00', True),
    ('2026-10-27T12:00:00+09:00', False),
])
def test_new_group_and_trait_from_first_appearance(pipeline, now, expected):
    p = pipeline
    record(p, '2026-10-19')
    record(p, '2026-10-20')
    trait_version(p, ['episode:1', 'episode:2'], target='new_signature')
    with clock.at('2026-10-20T12:00:00+09:00'):
        fill_forming_notes(p)
    # The summary records when it first appeared.
    state = load_state(p.store, 'fixture')
    domain = next(iter(state['summaries']))
    assert state['summaries'][domain]['at'].startswith('2026-10-20T12:00:00')
    with clock.at(now):
        note = public_note(p)
    group = next(g for g in note['self_forming'] if g['id'] == domain)
    assert group['new'] is expected  # the scenes start 10-19, so only the summary keeps it new here
    assert len(note['self_traits']) == 1 and note['self_traits'][0]['new'] is expected
    assert all('new' not in s for g in note['self_forming'] for s in g['scenes'])


def test_fact_order_by_time_then_id(pipeline):
    p = pipeline
    expected = {False: [], True: []}
    with clock.at('2026-11-01'), p.store.connect() as db:
        for private in (False, True):
            for at in ('2026-10-31T00:00:00+09:00', '2026-11-01T00:00:00+09:00',
                       '2026-10-30T00:00:00+09:00', '2026-10-31T15:00:00+00:00'):
                row = db.execute('INSERT INTO user_facts(character_id,content,learned_at,last_confirmed_at,private,importance,pinned) VALUES (?,?,?,?,?,0.5,0)',
                                 ('fixture', at, at, at, private))
                expected[private].append(row.lastrowid)
    for private in (False, True):
        ids = expected[private]
        assert [f['id'] for f in knowing_note(p, private=private)['facts']] == [ids[3], ids[1], ids[0], ids[2]]
