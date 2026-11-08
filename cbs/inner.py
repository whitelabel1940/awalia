"""Persistent feelings and wants. Only the steward interprets events."""
from cbs.storage import dump_trace
import json
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from cbs import clock

LEVELS = {'energy': ('低い', 'ふつう', '高い'), 'social': ('低い', 'ふつう', '高い'),
          'concern': ('なし', '少し', '強い')}
BASELINE = {'energy': 'ふつう', 'social': 'ふつう', 'concern': 'なし'}
Direction = Literal['気にかける', '伝えたい', '自分のことを話したい', '思い出を話したい', '黙ってそばにいたい', '知りたい', '約束']


class InnerField(BaseModel):
    model_config = ConfigDict(extra='forbid')


class FeelingLevels(InnerField):
    energy: Literal['低い', 'ふつう', '高い']
    social: Literal['低い', 'ふつう', '高い']
    concern: Literal['なし', '少し', '強い']


class Feeling(InnerField):
    sentence: str = Field(min_length=1, max_length=500)
    levels: FeelingLevels


class Want(InnerField):
    operation: Literal['new', 'append', 'skip'] = 'new'
    private: bool = False
    thread_ref: str | None = None
    direction: Direction
    motive: str = Field(min_length=1, max_length=600)
    topic: str = Field(min_length=1, max_length=300)
    detail: str = Field(min_length=1, max_length=1000)
    weight: float = Field(ge=0, le=1)
    trigger_ref: str = Field(min_length=1)


class InnerUpdate(InnerField):
    llm_role: ClassVar[str] = 'steward'
    feeling: Feeling
    wants: list[Want] = Field(default_factory=list, max_length=5)
    current_expression: Literal['neutral', 'smile', 'shy', 'surprised', 'sad', 'troubled', 'thinking', 'pout', 'unchanged'] = 'unchanged'


def update_inner(pipeline, trigger_ref, event):
    """Called on the per-DB queue. Each source event is applied atomically at most once."""
    if getattr(pipeline.store, 'costs', None) and pipeline.store.costs.paused():
        return
    store, cid, trace, run_id = pipeline.store, pipeline.config.character.id, {}, None
    try:
        with store.connect() as db:
            # Only a fully committed update is terminal. A process can stop
            # between the durable running marker and its atomic application.
            db.execute("DELETE FROM inner_update_runs WHERE character_id=? AND trigger_ref=? AND status='running'", (cid, trigger_ref))
            cursor = db.execute("INSERT OR IGNORE INTO inner_update_runs (character_id,created_at,trigger_ref,status) VALUES (?,?,?,'running')",
                                (cid, clock.iso(), trigger_ref))
            if not cursor.rowcount:
                return
            run_id = cursor.lastrowid
        from cbs.autonomy import ask
        model = store.current_model(pipeline.character)
        threads = store.list_threads(cid, eligible_only=True)
        from cbs.sharing import ShareChoice, share_candidates
        shares = share_candidates(pipeline, json.dumps(event, ensure_ascii=False))
        threads = list({t['ref']: t for t in [*threads, *shares]}.values())
        context = {'now': clock.iso(), 'feeling': store.current_feeling(cid), 'wants': threads,
                   'share_candidates': shares,
                   'trigger_ref': trigger_ref, 'event': event, 'character_summary': model.summary,
                   'signatures': [s.model_dump(mode='json') for s in model.signatures],
                   'demeanor': pipeline.store.current_character(pipeline.character).expression.model_dump(mode='json').get('demeanor', {}),
                   'private_facts_policy': store.private_context(cid)}
        context['current_expression'] = store.resident_expression(
            cid, pipeline.config.autonomy.resident_expression_neutral_minutes)['expression']
        # Order inner choices by decision start, rather than slow completion time.
        expression_at = context['now']
        output, state_trace = ask(pipeline, 'inner_update', InnerUpdate, context)
        trace['inner'] = state_trace
        from cbs.voice_text import Monologue, voice_call
        monologue = voice_call(pipeline, 'note_monologue', Monologue,
                              {'event': event, 'feeling': output.feeling.model_dump(mode='json')},
                              lambda e: [e.text], purpose='autonomy', trace=trace)
        available = {t['ref']: t for t in threads}
        allowed_triggers = {trigger_ref} | {t['trigger_ref'] for t in threads if t['trigger_ref']} | {t['source_ref'] for t in threads}
        with store.connect() as db:
            db.execute('INSERT INTO inner_states (character_id,created_at,sentence,levels_json,trigger_ref,current_expression,expression_at) VALUES (?,?,?,?,?,?,?)',
                (cid, clock.iso(), output.feeling.sentence, output.feeling.levels.model_dump_json(), trigger_ref,
                 None if output.current_expression == 'unchanged' else output.current_expression, expression_at))
            for n, want in enumerate(output.wants):
                if want.operation == 'skip':
                    continue
                if want.trigger_ref not in allowed_triggers or (want.thread_ref and want.thread_ref not in available):
                    trace.setdefault('warnings', []).append('想いの未知の参照を除外しました。')
                    continue
                if want.private and not (want.thread_ref and available[want.thread_ref].get('private') and available[want.thread_ref].get('may_initiate')):
                    if want.thread_ref:
                        old = db.execute('SELECT * FROM threads WHERE id=? AND character_id=?', (available[want.thread_ref]['id'],cid)).fetchone()
                        store._thread_status(db,old,'dropped')
                    trace.setdefault('warnings', []).append('私的な話題の想いは自発話に使いません。')
                    continue
                if want.operation == 'append' and not want.thread_ref:
                    trace.setdefault('warnings', []).append('追記先のない想いを除外しました。')
                    continue
                if want.thread_ref and available[want.thread_ref]['status'] == 'used' and want.operation != 'append':
                    trace.setdefault('warnings', []).append('使用済みの想いには明示された新しい点だけ追記できます。')
                    continue
                shared = not want.thread_ref or available[want.thread_ref]['kind'] in ('share', 'want')
                if shared:
                    tid = store.apply_share_choice(db, cid, ShareChoice(
                        operation='append' if want.thread_ref else 'new', thread_ref=want.thread_ref, detail=want.detail),
                        threads, want.topic, want.detail, f'inner_update:{run_id}:{n}', want.weight, kind='want')
                elif want.thread_ref:
                    tid = available[want.thread_ref]['id']
                before = dict(db.execute('SELECT * FROM threads WHERE id=? AND character_id=?', (tid, cid)).fetchone())
                # Preserve kind/source/due date. Only an explicit append above
                # can reopen a used share/want; follow-up eligibility is intact.
                db.execute('UPDATE threads SET topic=?,detail=?,direction=?,motive=?,trigger_ref=?,weight=?,weight_at=?,updated_at=? WHERE id=?',
                    (before['topic'] if shared else want.topic, before['detail'] if shared else want.detail,
                     want.direction, want.motive, want.trigger_ref, want.weight, clock.iso(), clock.iso(), tid))
                store._thread_history(db, tid, 'inner_update', before)
            db.execute('INSERT INTO monologues (character_id,created_at,text,trigger_ref) VALUES (?,?,?,?)',
                       (cid, clock.iso(), monologue.text, trigger_ref))
            db.execute("UPDATE inner_update_runs SET status='completed',trace_json=? WHERE id=?",
                       (dump_trace(pipeline._safe_trace(trace)), run_id))
    except Exception as error:
        # Never expose an exception string: provider errors may contain credentials.
        trace = pipeline._safe_trace(trace or getattr(error, 'trace', None) or {})
        trace['error'] = '内面の更新に失敗しました。会話・活動と前の内面を保持します。'
        if run_id is not None:
            try:
                with store.connect() as db:
                    db.execute("UPDATE inner_update_runs SET status='failed',trace_json=? WHERE id=?",
                               (dump_trace(trace), run_id))
            except Exception:
                pass  # A failed diagnostic write must not propagate into a conversation/activity.


def update_long_gap(pipeline):
    previous = pipeline.store.previous_utterance(pipeline.config.character.id)
    if previous and (clock.now() - clock.parse(previous)).total_seconds() >= pipeline.config.inner.long_gap_hours * 3600:
        update_inner(pipeline, f'gap:{previous}:{clock.now().date().isoformat()}',
                     {'kind': 'gap', 'previous_utterance': previous, 'elapsed': clock.gap(previous)})


def inspector_inner(pipeline):
    store, cid = pipeline.store, pipeline.config.character.id
    store.expire_threads(cid, pipeline.config.autonomy.thread_expiry_days)
    return {'feelings': store.list_inner_states(cid), 'wants': store.list_threads(cid, with_history=True),
            'monologues': store.list_monologues(cid), 'decisions': store.list_proactive(cid),
            'updates': store.list_inner_updates(cid)}
