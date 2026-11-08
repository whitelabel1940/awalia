"""EX2's saved display projection. Only background jobs call the LLM."""
import hashlib
import json
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from cbs import clock
from cbs.llm.cost import for_step
from cbs.runtime import read_prompt
from cbs.display_text import display_call

MAX_SCENES = 32  # Per item. One episode may contribute at most two scenes.
# 2: lines are one level more general; each episode gets a user-facing caption as evidence.
# 3: the partner is always the unstated subject; "あなた" is only the other person in the scene.
FORMAT = 3
SEEN_BEFORE = '2000-01-01T00:00:00+00:00'  # First-seen time for entries older than the `at` field.


class NoteField(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class SceneOperation(NoteField):
    action: Literal['add', 'attach', 'replace']
    scene_id: StrictInt | None
    text: str | None = Field(max_length=300)


class SceneEdits(NoteField):
    llm_role: ClassVar[str] = 'steward'
    operations: list[SceneOperation] = Field(min_length=1, max_length=2)
    caption: str = Field(min_length=1, max_length=200)


class SceneSummary(NoteField):
    llm_role: ClassVar[str] = 'steward'
    text: str = Field(min_length=1, max_length=400)


class SceneTrait(NoteField):
    llm_role: ClassVar[str] = 'steward'
    text: str = Field(min_length=1, max_length=400)
    scene_ids: list[StrictInt] = Field(min_length=1, max_length=8)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def sources(store, character):
    """Keep author prose out; accepted signature additions supply only learned prose."""
    cid = character.model.character_id
    labels = {d.id: getattr(d, 'description', None) or d.label for d in character.model.domains}
    open_domains = {d.id for d in character.model.domains if d.status == 'open'
                    and not any(e.startswith('author:') for e in d.evidence)}
    with store.connect() as db:
        turns = {r['id']: dict(r) for r in db.execute(
            "SELECT t.id,t.assistant_at,t.created_at FROM turns t JOIN sessions s ON s.id=t.session_id "
            "WHERE s.character_id=? AND t.status='completed'", (cid,))}
    episodes = {}
    for e in store.list_episodes(cid):
        if e['source_turn'] not in turns or e['domain'] not in labels:
            continue
        t = turns[e['source_turn']]
        episodes[e['id']] = {k: e[k] for k in ('id', 'domain', 'mode', 'situation', 'reaction', 'salient_conditions', 'source_turn')}
        episodes[e['id']]['date'] = clock.parse(t['assistant_at'] or t['created_at']).date().isoformat()
    versions = store.list_model_versions(cid)
    valid_refs = {r for v in versions if not v['invalidated'] for r in v['episode_refs']}
    invalid_refs = {r for v in versions if v['invalidated'] for r in v['episode_refs']} - valid_refs
    episodes = {i: e for i, e in episodes.items() if f'episode:{i}' not in invalid_refs}
    traits = {}
    for v in versions:
        if v['invalidated'] or v['diff'].get('source_turn') not in turns:
            continue
        refs = [int(r.split(':')[1]) for r in v['episode_refs'] if isinstance(r, str) and r.startswith('episode:') and r.split(':')[1].isdigit()]
        if not refs or any(i not in episodes for i in refs):
            continue
        for c in v['diff'].get('changes', []):
            if c['collection'] != 'signatures':
                continue
            before, after = c.get('before') or {}, c['after']
            learned = after.get('learned', [])[len(before.get('learned', [])):]
            acquired = {k: after[k] for k in ('when', 'tends_to') if k in after} if not before and after.get('origin') == 'acquired' else None
            if not learned and not acquired:
                continue
            # Each accepted learned addition is a new sentence, including additions
            # to the same signature. Never re-publish its older full snapshot.
            for index, addition in enumerate(learned or [None]):
                key = f"{v['id']}:{c['target']}:{index}"
                traits[key] = {'id': key, 'learned': [addition] if addition else [], 'acquired': acquired,
                               'episode_ids': sorted(set(refs)), 'version': v['id'], 'source_turn': v['diff']['source_turn']}
    # A learned addition to an author tendency can reference a non-Open item.
    # Expose its observed scenes too, never the author's original tendency.
    trait_refs = {i for t in traits.values() for i in t['episode_ids']}
    return labels, {i: e for i, e in episodes.items() if e['domain'] in open_domains or i in trait_refs}, traits


def load_state(store, cid):
    with store.connect() as db:
        row = db.execute("SELECT text FROM note_entries WHERE character_id=? AND kind='ex2' AND source_id=0", (cid,)).fetchone()
    return json.loads(row[0]) if row else empty_state()


def empty_state():
    return {'format': FORMAT, 'processed': [], 'scenes': [], 'summaries': {}, 'traits': {}, 'captions': {}}


def save_state(pipeline, state, *, notice_turn=None):
    cid = pipeline.config.character.id
    with pipeline.store.connect() as db:
        db.execute("INSERT INTO note_entries(character_id,kind,source_id,text,created_at) VALUES (?,'ex2',0,?,?) "
                   "ON CONFLICT(character_id,kind,source_id) DO UPDATE SET text=excluded.text,created_at=excluded.created_at",
                   (cid, json.dumps(state, ensure_ascii=False), clock.iso()))
        if notice_turn and pipeline.config.early.notify:
            turn = db.execute('SELECT t.* FROM turns t JOIN sessions s ON s.id=t.session_id WHERE t.id=? AND s.character_id=?',
                              (notice_turn, cid)).fetchone()
            if turn and turn['status'] == 'completed':
                day = clock.parse(turn['assistant_at'] or turn['created_at']).date().isoformat()
                db.execute('INSERT OR IGNORE INTO forming_notices VALUES (?,?,?,?)', (cid, day, notice_turn, clock.iso()))


def scene_view(scene, episodes, captions=None):
    evidence = [episodes[i] for i in scene['episode_ids']]
    captions = captions or {}
    return {**scene, 'dates': sorted({e['date'] for e in evidence}),
            'evidence': [{'date': e['date'], 'summary': captions.get(str(e['id'])) or (e['situation'].rstrip('。') + '。' + e['reaction']),
                          'source_turn': e['source_turn'], 'mode': e['mode']} for e in evidence]}


def summary_key(rows):
    return fingerprint(rows)


def read_note(store, character):
    """GET reads saved text only; stale/deleted evidence hides derived prose immediately."""
    labels, episodes, traits = sources(store, character)
    state = load_state(store, character.model.character_id)
    # A replacement may mention its older evidence, so never partially trim a row.
    rows = [scene_view(s, episodes, state.get('captions')) for s in state['scenes'] if set(s['episode_ids']) <= episodes.keys()]
    groups = []
    for domain, label in labels.items():
        scenes = [s for s in rows if s['domain'] == domain]
        if not scenes:
            continue
        saved = state['summaries'].get(domain, {})
        summary = saved.get('text') if saved.get('key') == summary_key(scenes) and len({d for s in scenes for d in s['dates']}) >= 2 else None
        groups.append({'id': domain, 'label': label, 'summary': summary, 'scenes': scenes})
    visible = {s['id']: s for s in rows}
    shown_traits = []
    for target, source in traits.items():
        saved = state['traits'].get(target, {})
        supporting = [s for s in rows if set(s['episode_ids']) & set(source['episode_ids'])]
        if (saved.get('key') != fingerprint(source) or saved.get('scene_key') != fingerprint(supporting)
                or not set(saved.get('scene_ids', [])) <= visible.keys()):
            continue
        shown_traits.append({'id': target, 'text': saved['text'], 'scenes': [
            {'id': i, 'label': labels[visible[i]['domain']], 'text': visible[i]['text']} for i in saved['scene_ids']]})
    # NEW marks only items and abstractions that appeared recently, not each added scene.
    # Time-dependent display flags must not enter the saved summary/trait hashes.
    from cbs.note import is_new
    for group in groups:
        first = min((d for s in group['scenes'] for d in s['dates']), default=None)
        summary_at = state['summaries'].get(group['id'], {}).get('at') if group['summary'] else None
        group['new'] = bool(first and is_new(first) or summary_at and is_new(summary_at))
    for trait in shown_traits:
        at = state['traits'][trait['id']].get('at')
        trait['new'] = bool(at and is_new(at))
    return groups, shown_traits


def fill_forming_notes(pipeline, *, source_turn=None):
    """Called on the serial Steward queue. Daily calls migrate/retry without notices."""
    from cbs.pipeline import INTERNAL_MARKERS
    labels, episodes, traits = sources(pipeline.store, pipeline.character)
    state = load_state(pipeline.store, pipeline.config.character.id)
    trace = []
    notice_turns = set(state.get('pending_notices', []))
    current_refs = {i for t in traits.values() if t['source_turn'] == source_turn for i in t['episode_ids']}
    if source_turn and pipeline.config.early.notify and (current_refs or any(e['source_turn'] == source_turn for e in episodes.values())):
        notice_turns.add(source_turn)
    pending = [i for i in episodes if i not in state['processed']]
    out_of_order = (source_turn is None and pending and state['processed']
                    and min(pending) < max(state['processed']))
    if state.get('format') != FORMAT and source_turn is None:
        # Rebuild once with the current wording rules on the daily pass; older lines stay until then.
        # Keep when each summary/trait first appeared, so a rewrite does not show NEW or a notice again.
        # Entries saved before `at` existed are old by definition.
        first = {kind: {k: {'at': v.get('at') or SEEN_BEFORE} for k, v in state.get(kind, {}).items()}
                 for kind in ('summaries', 'traits')}
        state = {**empty_state(), **first}
        save_state(pipeline, state)
    if not set(state['processed']) <= episodes.keys() or out_of_order:
        # Invalidation/redaction: no sentence inheriting removed evidence survives.
        # A new live turn may precede the first daily migration. Replay old→new
        # on that daily pass so old reactions cannot replace newer reactions.
        state = empty_state()
        save_state(pipeline, state)
    if notice_turns != set(state.get('pending_notices', [])):
        state['pending_notices'] = sorted(notice_turns)
        save_state(pipeline, state)

    def generate(schema, task, context):
        call_trace = {}
        try:
            messages = [{'role': 'user', 'content': json.dumps({'task': task, **context}, ensure_ascii=False)}]
            system = read_prompt('note_scenes')
            result = display_call(lambda: for_step(pipeline.client, 'note', 'memory').generate_json(
                pipeline.config.models.steward, system, messages, schema),
                lambda r: [o.text for o in r.parsed.operations] + [r.parsed.caption]
                if isinstance(r.parsed, SceneEdits) else [r.parsed.text])
            call_trace = pipeline._safe_trace(result.trace())
            output = schema.model_validate_json(pipeline.client.sanitize_text(result.parsed.model_dump_json()))
            texts = [o.text for o in output.operations] + [output.caption] if isinstance(output, SceneEdits) else [output.text]
            if any(t is not None and (not t.strip() or INTERNAL_MARKERS.search(t)) for t in texts):
                raise ValueError('invalid display text')
            return output, call_trace
        except Exception as error:
            trace.append({'task': task, 'status': 'failed', 'trace': pipeline._safe_trace(getattr(error, 'trace', None) or call_trace)})
            return None, call_trace

    for eid, episode in episodes.items():
        if eid in state['processed'] or (source_turn is not None and episode['source_turn'] != source_turn and eid not in current_refs):
            continue
        domain = episode['domain']
        existing = [s for s in state['scenes'] if s['domain'] == domain]
        output, call_trace = generate(SceneEdits, 'scenes', {'item': labels[domain], 'episode': episode,
            'scenes': [scene_view(s, episodes, state.get('captions')) for s in existing], 'max_scenes': MAX_SCENES})
        if output is None:
            continue
        try:
            targets = {s['id']: s for s in existing}
            used = set()
            for op in output.operations:
                if op.action == 'add':
                    if op.scene_id is not None or not op.text:
                        raise ValueError('add needs text, no id')
                elif op.scene_id not in targets or op.scene_id in used:
                    raise ValueError('unknown/duplicate scene id')
                elif (op.action == 'attach' and op.text is not None) or (op.action == 'replace' and not op.text):
                    raise ValueError('attach keeps text; replace needs text')
                if op.scene_id is not None:
                    used.add(op.scene_id)
            if len(existing) + sum(o.action == 'add' for o in output.operations) > MAX_SCENES:
                raise ValueError('scene limit')
            for op in output.operations:
                if op.action == 'add':
                    scene = {'id': max((s['id'] for s in state['scenes']), default=0) + 1,
                             'domain': domain, 'text': op.text, 'episode_ids': [eid]}
                    state['scenes'].append(scene)
                else:
                    scene = targets[op.scene_id]
                    scene['episode_ids'] = sorted(set(scene['episode_ids'] + [eid]))
                    if op.action == 'replace':
                        scene['text'] = op.text
            state['processed'].append(eid)
            state.setdefault('captions', {})[str(eid)] = output.caption
            notice = source_turn or (episode['source_turn'] if episode['source_turn'] in notice_turns else None)
            save_state(pipeline, state, notice_turn=notice if not existing else None)
            trace.append({'task': 'scenes', 'episode': eid, 'status': 'completed', 'trace': call_trace})
        except ValueError:
            trace.append({'task': 'scenes', 'episode': eid, 'status': 'failed', 'trace': call_trace})

    rows = [scene_view(s, episodes, state.get('captions')) for s in state['scenes']]
    for domain in labels:
        scenes = [s for s in rows if s['domain'] == domain]
        # Two distinct calendar days, even when both support the same scene row.
        if len({d for s in scenes for d in s['dates']}) < 2:
            continue
        key = summary_key(scenes)
        if state['summaries'].get(domain, {}).get('key') == key:
            continue
        output, call_trace = generate(SceneSummary, 'summary', {'item': labels[domain], 'scenes': scenes})
        if output:
            # `at` keeps when the summary first appeared; rewrites are not new.
            state['summaries'][domain] = {'key': key, 'text': output.text,
                                          'at': state['summaries'].get(domain, {}).get('at') or clock.iso()}
            save_state(pipeline, state)
            trace.append({'task': 'summary', 'status': 'completed', 'trace': call_trace})

    for target, source in traits.items():
        scenes = [s for s in rows if set(s['episode_ids']) & set(source['episode_ids'])]
        if not scenes or not set(source['episode_ids']) <= {i for s in scenes for i in s['episode_ids']}:
            continue  # Older evidence will be migrated on daily maintenance.
        key = fingerprint(source)
        previous = state['traits'].get(target, {})
        # Regenerate links/prose after a scene is rewritten as well.
        scene_key = fingerprint(scenes)
        if previous.get('key') == key and previous.get('scene_key') == scene_key:
            continue
        output, call_trace = generate(SceneTrait, 'trait', {'learned': source['learned'], 'acquired': source['acquired'], 'scenes': scenes})
        if output:
            ids = output.scene_ids
            if len(set(ids)) != len(ids) or not set(ids) <= {s['id'] for s in scenes}:
                trace.append({'task': 'trait', 'status': 'failed', 'trace': call_trace})
                continue
            state['traits'][target] = {'key': key, 'scene_key': scene_key, 'text': output.text, 'scene_ids': ids,
                                       'at': previous.get('at') or clock.iso()}
            save_state(pipeline, state, notice_turn=source['source_turn'] if not previous and source['source_turn'] in notice_turns else None)
            trace.append({'task': 'trait', 'status': 'completed', 'trace': call_trace})
    remaining = [tid for tid in notice_turns if
                 any(eid not in state['processed'] for eid, e in episodes.items() if e['source_turn'] == tid)
                 or any(key not in state['traits'] for key, t in traits.items() if t['source_turn'] == tid)]
    if remaining != state.get('pending_notices', []):
        state['pending_notices'] = sorted(remaining)
        save_state(pipeline, state)
    return trace
