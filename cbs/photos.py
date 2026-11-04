"""PH1: bounded, metadata-free photos and one vision call per attached batch."""
from cbs.llm.cost import for_step
import base64
import binascii
from io import BytesIO
import json
from typing import ClassVar
from uuid import uuid4
import warnings

from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field

from cbs.llm import LLMError, RoutingLLMClient
from cbs.config import key_env, key_set
from cbs.runtime import read_prompt
from cbs.timing import span

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
 id TEXT PRIMARY KEY, character_id TEXT NOT NULL, source_turn INTEGER NOT NULL REFERENCES turns(id),
 description TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS photo_redacted_turns (id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS photo_turn_sources (
 photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
 turn_id INTEGER NOT NULL REFERENCES turns(id), PRIMARY KEY(photo_id,turn_id)
);
CREATE TABLE IF NOT EXISTS photo_job_sources (
 photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
 job_id INTEGER NOT NULL REFERENCES memory_jobs(id), PRIMARY KEY(photo_id,job_id)
);
CREATE TABLE IF NOT EXISTS photo_notice (id INTEGER PRIMARY KEY CHECK(id=1));
"""


class PhotoDescriptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    llm_role: ClassVar[str] = 'vision'
    descriptions: list[str] = Field(min_length=1, max_length=12)


def vision_status(config, *, require_key=True):
    model = config.models.vision
    if not model:
        return {'enabled': False, 'reason': '写真を読むモデルが設定されていません。'}
    routing = RoutingLLMClient(config)
    provider, bare = routing._split(model)
    full = provider + '/' + bare
    capable = config.photos.candidates.get(full, False)
    capable = capable or any(c.model == full and c.vision for c in config.simple.actor_candidates + config.simple.judge_candidates)
    if not capable:
        return {'enabled': False, 'reason': 'このモデルは写真の読み取り対応が未確認です。'}
    if require_key and key_env(config, provider) and not key_set(config, provider):
        return {'enabled': False, 'reason': '写真を読むサービスのキーを設定してください。'}
    if provider not in {'gemini', *config.llm.providers}:
        return {'enabled': False, 'reason': '写真を読むサービスを設定してください。'}
    return {'enabled': True, 'reason': '', 'max_count': config.photos.max_count,
            'max_bytes': config.photos.max_bytes}


def normalize_photo(encoded, config):
    """Decode based on bytes, never extension/MIME. Rebuild pixels to drop all metadata."""
    if not isinstance(encoded, str) or len(encoded) > ((config.max_bytes + 2) // 3) * 4:
        raise ValueError('写真の容量が上限を超えています。')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('写真を読み取れませんでした。') from None
    if len(raw) > config.max_bytes:
        raise ValueError('写真の容量が上限を超えています。')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as source:
                if source.format not in {'PNG', 'JPEG', 'WEBP'}:
                    raise ValueError()
                if source.width * source.height > config.max_pixels or getattr(source, 'n_frames', 1) != 1:
                    raise ValueError()
                image = ImageOps.exif_transpose(source)
                image.thumbnail((config.max_edge, config.max_edge))
                rgba = image.convert('RGBA')
                clean = Image.new('RGB', image.size, 'white')
                clean.paste(rgba, mask=rgba.getchannel('A'))
                output = BytesIO()
                clean.save(output, format='JPEG', quality=88)
                if len(output.getvalue()) > config.max_bytes:
                    raise ValueError()
                return {'id': uuid4().hex, 'bytes': output.getvalue(), 'width': clean.width, 'height': clean.height}
    except Exception:
        raise ValueError('PNG・JPEG・WebP の静止写真を、容量・大きさの上限内で選んでください。') from None


def describe(pipeline, text, encoded, event_sink=None):
    if not encoded:
        return [], None
    from cbs.llm import FakeLLMClient
    status = vision_status(pipeline.config, require_key=not isinstance(pipeline.client, FakeLLMClient))
    if not status['enabled']:
        raise ValueError(status['reason'])
    if len(encoded) > pipeline.config.photos.max_count:
        raise ValueError(f'写真は一度に{pipeline.config.photos.max_count}枚までです。')
    photos = [normalize_photo(value, pipeline.config.photos) for value in encoded]
    if event_sink:
        event_sink('stage', {'stage': 'vision'})
    with pipeline.store.connect() as db:
        db.execute('INSERT OR IGNORE INTO photo_notice VALUES (1)')
    with span('Vision'):
        response = for_step(pipeline.client, 'vision', 'media').generate_images_json(pipeline.config.models.vision, read_prompt('vision'),
            pipeline.client.sanitize_text(text), [p['bytes'] for p in photos], PhotoDescriptions)
    descriptions = PhotoDescriptions.model_validate(response.parsed).descriptions
    if len(descriptions) != len(photos) or any(not d.strip() or len(d) > 6000 for d in descriptions):
        raise LLMError('写真の説明を受け取れませんでした。もう一度送ってください。')
    for photo, description in zip(photos, descriptions):
        photo['description'] = pipeline.client.sanitize_text(description).strip()
    return photos, response.trace()


def photo_block(photo):
    return f"\n\n[photo:{photo['id']} ユーザーが見せてくれた写真の説明]\n{photo['description']}\n[/photo:{photo['id']}]"


def photo_path(store, photo_id):
    # Only IDs retrieved from SQLite enter here; never use an upload filename.
    if len(photo_id) != 32 or any(c not in '0123456789abcdef' for c in photo_id):
        raise KeyError(photo_id)
    return store.path.parent / 'photos' / (photo_id + '.jpg')


def save_photos(store, db, cid, turn_id, photos):
    written = []
    try:
        for photo in photos:
            path = photo_path(store, photo['id'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(photo['bytes'])
            written.append(path)
            db.execute('INSERT INTO photos VALUES (?,?,?,?,?,?)',
                (photo['id'], cid, turn_id, photo['description'], photo['width'], photo['height']))
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise


def public_photos(store, turn_id):
    with store.connect() as db:
        return [dict(r) for r in db.execute('SELECT id,description,width,height FROM photos WHERE source_turn=? ORDER BY rowid', (turn_id,))]


def display_text(text, photos):
    for photo in photos:
        text = text.replace(photo_block(photo), '').replace(photo_block(photo).lstrip(), '')
    return text.strip()


def input_sources(db, turn_id, load, packet, photos):
    """Union structural evidence IDs; deliberately conservative for mixed summaries."""
    load = load if isinstance(load, dict) else {}
    packet = packet if isinstance(packet, dict) else {}
    turns = {t['id'] for t in load.get('recent_turns', [])}
    jobs, episodes, memories, observations = set(), set(), set(), set()

    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                target = {'source_turn': turns, 'job_id': jobs,
                          'source_episode': memories}.get(key)
                if target is not None and isinstance(item, int):
                    target.add(item)
                if key == 'source_turns' and isinstance(item, list):
                    turns.update(i for i in item if isinstance(i, int))
                if key in ('episode_refs', 'episode_ids') and isinstance(item, list):
                    (episodes if key == 'episode_refs' else memories).update(i for i in item if isinstance(i, int))
                walk(item)
            ref = value.get('ref', '')
            if isinstance(ref, str) and ref.startswith('understanding:') and ref[14:].isdigit():
                row = db.execute('SELECT support_json,counter_json FROM user_understandings WHERE id=?', (int(ref[14:]),)).fetchone()
                if row:
                    observations.update(json.loads(row[0]) + json.loads(row[1]))
    walk(load)
    walk(packet)
    for oid in observations:
        row = db.execute('SELECT source_turns_json FROM user_observations WHERE id=?', (oid,)).fetchone()
        if row:
            turns.update(json.loads(row[0]))
    for eid in episodes:
        row = db.execute('SELECT source_turn FROM episodes WHERE id=?', (eid,)).fetchone()
        if row and row[0]:
            turns.add(row[0])
    for mid in memories:
        row = db.execute('SELECT job_id FROM memory_episodes WHERE id=?', (mid,)).fetchone()
        if row and row[0]:
            jobs.add(row[0])
    for jid in jobs:
        turns.update(r[0] for r in db.execute('SELECT t.id FROM turns t JOIN memory_jobs j ON j.conversation_id=t.conversation_id AND t.id BETWEEN j.first_turn AND j.last_turn WHERE j.id=?', (jid,)))
    sources = {p['id'] for p in photos}
    for jid in jobs:
        sources.update(r[0] for r in db.execute('SELECT photo_id FROM photo_job_sources WHERE job_id=?', (jid,)))
    # One query regardless of the number of related turns.
    if turns:
        db.execute('CREATE TEMP TABLE photo_input_turns (id INTEGER PRIMARY KEY)')
        db.executemany('INSERT INTO photo_input_turns VALUES (?)', [(t,) for t in turns])
        sources.update(r[0] for r in db.execute('SELECT DISTINCT photo_id FROM photo_turn_sources WHERE turn_id IN (SELECT id FROM photo_input_turns)'))
        db.execute('DROP TABLE photo_input_turns')
    return sources


def save_turn_sources(db, turn_id, trace, photos):
    """Persist provenance independently of retained/compacted traces."""
    if not photos and not db.execute('SELECT 1 FROM photos LIMIT 1').fetchone():
        return
    sources = input_sources(db, turn_id, trace.get('Load', {}).get('output', {}),
                            trace.get('Packet', {}).get('output', {}), photos)
    db.executemany('INSERT OR IGNORE INTO photo_turn_sources VALUES (?,?)', [(p, turn_id) for p in sources])


def save_job_sources(db, job, turns, trace):
    if not db.execute('SELECT 1 FROM photos LIMIT 1').fetchone():
        return
    messages = trace.get('request', {}).get('messages', [])
    context = json.loads(messages[-1]['content']) if messages else {}
    sources = input_sources(db, job['last_turn'] + 1, {'recent_turns': turns}, context, [])
    db.execute('DELETE FROM photo_job_sources WHERE job_id=?', (job['id'],))
    db.executemany('INSERT OR IGNORE INTO photo_job_sources VALUES (?,?)', [(p, job['id']) for p in sources])


def delete_photo(pipeline, photo_id):
    """Explicit forgetting: invalidate mixed summaries by provenance, never keyword semantics.

    A memory job summarizes several turns, so removing one input invalidates that
    entire job. Keep other raw turns/photos; rebuild on the normal background queue.
    """
    store, cid = pipeline.store, pipeline.config.character.id
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('PRAGMA defer_foreign_keys=ON')
        photo = db.execute('SELECT * FROM photos WHERE id=? AND character_id=?', (photo_id, cid)).fetchone()
        if not photo:
            raise KeyError(photo_id)
        store._knowing_revision(db, cid)
        photo = dict(photo)
        turn = db.execute('SELECT * FROM turns WHERE id=?', (photo['source_turn'],)).fetchone()
        affected = {turn['id'], *[r[0] for r in db.execute('SELECT turn_id FROM photo_turn_sources WHERE photo_id=?', (photo_id,))]}
        db.execute('CREATE TEMP TABLE affected_turns (id INTEGER PRIMARY KEY)')
        db.executemany('INSERT INTO affected_turns VALUES (?)', [(i,) for i in affected])
        jobs = {r[0] for r in db.execute('SELECT DISTINCT j.id FROM memory_jobs j JOIN turns t ON t.conversation_id=j.conversation_id AND t.id BETWEEN j.first_turn AND j.last_turn JOIN affected_turns a ON a.id=t.id')}
        jobs.update(r[0] for r in db.execute('SELECT job_id FROM photo_job_sources WHERE photo_id=?', (photo_id,)))
        db.execute('CREATE TEMP TABLE affected_jobs (id INTEGER PRIMARY KEY)')
        db.executemany('INSERT INTO affected_jobs VALUES (?)', [(i,) for i in jobs])
        db.execute('CREATE TEMP TABLE affected_memories AS SELECT id FROM memory_episodes WHERE job_id IN (SELECT id FROM affected_jobs)')
        db.execute('CREATE TEMP TABLE affected_facts AS SELECT id FROM user_facts WHERE source_episode IN (SELECT id FROM affected_memories) ')
        db.execute('CREATE TEMP TABLE affected_observations AS SELECT id FROM user_observations WHERE job_id IN (SELECT id FROM affected_jobs)')
        for row in db.execute('SELECT id,source_turns_json FROM user_observations WHERE character_id=?', (cid,)).fetchall():
            if affected.intersection(json.loads(row['source_turns_json'])):
                db.execute('INSERT INTO affected_observations SELECT ? WHERE ? NOT IN (SELECT id FROM affected_observations)', (row['id'], row['id']))
        observation_ids = {r[0] for r in db.execute('SELECT id FROM affected_observations')}
        for row in db.execute('SELECT * FROM user_understandings WHERE character_id=?', (cid,)).fetchall():
            if observation_ids.intersection(json.loads(row['support_json']) + json.loads(row['counter_json'])):
                db.execute('DELETE FROM knowing_questions WHERE understanding_id=?', (row['id'],))
                db.execute('DELETE FROM user_understandings WHERE id=?', (row['id'],))
        db.execute('DELETE FROM user_observations WHERE id IN (SELECT id FROM affected_observations)')
        db.execute('UPDATE user_facts SET superseded_by=NULL WHERE superseded_by IN (SELECT id FROM affected_facts)')
        db.execute('DELETE FROM user_facts WHERE id IN (SELECT id FROM affected_facts)')
        db.execute('DELETE FROM episode_links WHERE source_episode IN (SELECT id FROM affected_memories) OR target_episode IN (SELECT id FROM affected_memories)')
        db.execute('DELETE FROM episode_link_runs WHERE source_episode IN (SELECT id FROM affected_memories)')
        digests = [r[0] for r in db.execute('SELECT DISTINCT digest_id FROM digest_episodes WHERE episode_id IN (SELECT id FROM affected_memories)')]
        for did in digests:
            db.execute('DELETE FROM digest_episodes WHERE digest_id=?', (did,))
            db.execute('DELETE FROM monthly_digests WHERE id=?', (did,))
        db.execute('DELETE FROM memory_episodes WHERE id IN (SELECT id FROM affected_memories)')
        db.execute("UPDATE memory_jobs SET status='pending',trace_json='{}' WHERE id IN (SELECT id FROM affected_jobs)")
        db.execute('CREATE TEMP TABLE affected_episodes AS SELECT id FROM episodes WHERE source_turn IN (SELECT id FROM affected_turns)')
        episode_ids = {r[0] for r in db.execute('SELECT id FROM affected_episodes')}
        removed_targets = set()
        for row in db.execute('SELECT id,diff_json,episode_refs_json FROM model_versions WHERE character_id=? ORDER BY id', (cid,)).fetchall():
            diff = json.loads(row['diff_json'])
            changes = diff.get('changes', [])
            if episode_ids.intersection(json.loads(row['episode_refs_json'])):
                removed_targets.update((c['collection'], c['target']) for c in changes)
            kept = [c for c in changes if (c['collection'], c['target']) not in removed_targets]
            if len(kept) != len(changes):
                # Later full snapshots of the same field inherit the forgotten evidence.
                # Keep independent fields and IDs referenced by bootstrap records.
                db.execute("UPDATE model_versions SET diff_json=?,rationale='',episode_refs_json='[]' WHERE id=?",
                           (json.dumps({**diff, 'changes': kept}, ensure_ascii=False), row['id']))
                db.execute("DELETE FROM note_entries WHERE character_id=? AND source_id=? AND kind LIKE 'forming:%'", (cid, row['id']))
        db.execute('UPDATE ledger_events SET parent_event=NULL WHERE parent_event IN (SELECT id FROM ledger_events WHERE source_turn IN (SELECT id FROM affected_turns))')
        db.execute("DELETE FROM note_entries WHERE character_id=? AND kind='past' AND source_id IN (SELECT id FROM reveal_events WHERE source_turn IN (SELECT id FROM affected_turns))", (cid,))
        for table in ('ledger_events', 'canon_facts', 'episodes', 'steward_runs', 'pending_corrections', 'forming_notices'):
            db.execute(f'DELETE FROM {table} WHERE source_turn IN (SELECT id FROM affected_turns)')
        # EX2 text may paraphrase the forgotten scene; rebuild from remaining evidence.
        db.execute("DELETE FROM note_entries WHERE character_id=? AND kind='ex2'", (cid,))
        db.execute('CREATE TEMP TABLE affected_threads AS SELECT id FROM threads WHERE source_turn IN (SELECT id FROM affected_turns) OR source_ref IN (SELECT \'fact:\'||id FROM affected_facts)')
        db.execute('DELETE FROM thread_history WHERE thread_id IN (SELECT id FROM affected_threads)')
        db.execute('DELETE FROM threads WHERE id IN (SELECT id FROM affected_threads)')
        db.execute('CREATE TEMP TABLE affected_interests AS SELECT interest_id AS id FROM interest_evidence WHERE source_turn IN (SELECT id FROM affected_turns) OR source_episode IN (SELECT id FROM affected_memories)')
        db.execute('DELETE FROM interest_changes WHERE interest_id IN (SELECT id FROM affected_interests)')
        db.execute('DELETE FROM interest_evidence WHERE interest_id IN (SELECT id FROM affected_interests)')
        db.execute("DELETE FROM interests WHERE id IN (SELECT id FROM affected_interests) AND origin<>'author'")
        # Questions born from those turns (AU1) would carry the photo's content into later research.
        db.execute("DELETE FROM research_candidates WHERE character_id=? AND source_kind='conversation' AND source_ref IN (SELECT 'turn:'||id FROM affected_turns)", (cid,))
        db.execute('DELETE FROM now_summaries WHERE character_id=?', (cid,))
        earliest = db.execute("SELECT MIN(created_at) FROM inner_update_runs WHERE character_id=? AND trigger_ref IN (SELECT 'memory_job:'||id FROM affected_jobs)", (cid,)).fetchone()[0]
        if earliest:
            # Feelings and monologues consume the previous state: remove their suffix.
            for table in ('inner_states', 'monologues'):
                db.execute(f'DELETE FROM {table} WHERE character_id=? AND created_at>=?', (cid, earliest))
        db.execute('DELETE FROM diaries WHERE character_id=? AND day IN (SELECT substr(created_at,1,10) FROM turns WHERE id IN (SELECT id FROM affected_turns))', (cid,))
        # Traces contain copies of old prompts. Remove them, including paraphrased replies
        # from model context; the visible original conversation remains readable.
        db.execute("UPDATE turns SET trace_json='{}' WHERE id IN (SELECT id FROM affected_turns)")
        db.execute('UPDATE turns SET user_text=? WHERE id=?', (display_text(turn['user_text'], [photo]), turn['id']))
        db.execute('DELETE FROM photos WHERE id=?', (photo_id,))
        db.execute('CREATE TABLE IF NOT EXISTS photo_redacted_turns (id INTEGER PRIMARY KEY)')
        db.executemany('INSERT OR IGNORE INTO photo_redacted_turns VALUES (?)', [(i,) for i in affected])
        for table in ('knowing_runs', 'inner_update_runs', 'memory_maintenance'):
            db.execute(f"UPDATE {table} SET trace_json='{{}}' WHERE character_id=?", (cid,))
        db.execute('DELETE FROM reply_embeddings')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('写真の関連記録を削除できませんでした。')
        photo_path(store, photo_id).unlink(missing_ok=True)


def model_turns(store, turns):
    with store.connect() as db:
        removed = {r[0] for r in db.execute('SELECT id FROM photo_redacted_turns')}
    return [{**t, 'assistant_text': '[写真削除に伴い、この返答は記憶の対象外]'} if t['id'] in removed else t for t in turns]
