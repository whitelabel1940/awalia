"""Confirmed, resumable import jobs sharing the existing memory work queue."""
from cbs.llm.cost import for_step
import json
from pathlib import Path
import shutil
import threading
from time import perf_counter
from typing import ClassVar, Literal
from uuid import uuid4

from pydantic import Field

from cbs import clock
from cbs.import_formats import Conversation, parse_export
from cbs.memory import (MonthlyDigest, digest_candidates, episodize, link_episode, timed_memory, timed_turn)
from cbs.pipeline import Extract, JudgeField
from cbs.runtime import read_prompt, character_prefix
from cbs.steward import submit


class ReflectedTurn(JudgeField):
    turn_id: int
    extract: Extract
    emotional_weight: Literal['low','medium','high']
    user_disclosure: Literal['yes','no']


class ImportReflection(JudgeField):
    llm_role: ClassVar[str] = 'steward'
    turns: list[ReflectedTurn] = Field(max_length=40)


def exchanges(conversation):
    """Lossless mapping to paired turns, including assistant-first/consecutive roles."""
    pending = None
    for message in conversation.messages:
        if message.role == 'user':
            if pending:
                yield pending, None
            pending = message
        else:
            yield pending, message
            pending = None
    if pending:
        yield pending, None


def estimate(pipeline, conversations):
    count = sum(len(list(exchanges(c))) for c in conversations)
    chars = sum(len(m.text) for c in conversations for m in c.messages)
    # Include both sweeps (gap/day) and the 40-exchange maximum.
    chunks = 0
    for c in conversations:
        n, previous, start, chunk_chars = 0, None, None, 0
        for user, assistant in exchanges(c):
            at = (user or assistant).at
            pair_chars = len(user.text if user else '')+len(assistant.text if assistant else '')
            if previous and ((clock.parse(at)-clock.parse(previous)).total_seconds() >= pipeline.config.memory.gap_minutes*60 or
                             clock.day(at,pipeline.config.memory.day_boundary_hour) != clock.day(start,pipeline.config.memory.day_boundary_hour)):
                chunks += bool(n)
                n,chunk_chars = 0,0
            if n and chunk_chars+pair_chars>48000:
                chunks += 1
                n,chunk_chars = 0,0
            start = at if not n else start
            n += 1
            chunk_chars += pair_chars
            previous = (assistant or user).at
            if n >= min(40,pipeline.config.memory.max_exchanges):
                chunks += 1
                n,chunk_chars = 0,0
        chunks += bool(n)
    # Conservative planning allowance, calibrated in reports/IM_import.md.
    # Exact charges depend on configured models and extraction sizes.
    return {'conversations':len(conversations), 'messages':sum(len(c.messages) for c in conversations),
            'exchanges':count, 'chunks':chunks, 'characters':chars,
            'estimated_seconds':round(chunks*24+30),
            'estimated_usd':round(chunks*.016+chars*.0000005,4),
            'estimate_note':'Lunaでの記憶整理を基準にした目安です。選んだAI・内容・再試行で時間と料金は変わります。',
            'models':pipeline.config.models.model_dump(),
            # Include float32 vectors, their hex undo records and LLM traces.
            'storage_bytes':max(262144,chars*40+count*262144+chunks*65536)}


class ImportJobs:
    def __init__(self, selected):
        self.selected = selected
        self.lock = threading.RLock()
        self.futures = {}
        self.errors = {}
        self.pipelines = {}

    def preview(self, pipeline, filename, payload):
        conversations = parse_export(filename,payload)
        identities, hashes = pipeline.store.imported_identity(pipeline.config.character.id)
        seen, listing = set(hashes), []
        for c in conversations:
            duplicate = c.content_hash in seen or (c.format,c.source_id,c.content_hash) in identities
            listing.append({**c.listing(),'duplicate':duplicate})
            if not c.error:
                seen.add(c.content_hash)
        # A preview is durable but does not reserve any dedup identities.
        token = uuid4().hex
        with pipeline.store.connect() as db:
            db.execute("INSERT INTO import_batches VALUES (?,?,'preview',?,?,?,NULL,0)",
                (token,pipeline.config.character.id,json.dumps({'listing':listing,'payloads':[c.dump() for c in conversations]},ensure_ascii=False),clock.iso(),clock.iso()))
        return {'preview_id':token,'conversations':listing}

    def quote(self, pipeline, preview_id, keys):
        status = pipeline.store.import_status(pipeline.config.character.id,preview_id)
        if status['status'] != 'preview':
            raise ValueError('ファイルを選び直してください。')
        wanted = set(keys)
        choices = [Conversation.load(c) for c in status['quote']['payloads']]
        identities, hashes = pipeline.store.imported_identity(pipeline.config.character.id)
        selected = []
        for c in choices:
            if c.key not in wanted or c.error or c.content_hash in hashes or (c.format,c.source_id,c.content_hash) in identities:
                continue
            hashes.add(c.content_hash)
            selected.append(c)
        if not selected:
            raise ValueError('まだ取り込んでいない会話を選んでください。')
        plan = estimate(pipeline,selected)
        plan['preview_id'] = preview_id
        plan['available_bytes'] = shutil.disk_usage(pipeline.store.path.parent).free
        if plan['storage_bytes'] > plan['available_bytes']:
            raise ValueError('保存先の空き容量が足りません。')
        token = uuid4().hex
        with pipeline.store.connect() as db:
            db.execute("INSERT INTO import_batches VALUES (?,?,'quoted',?,?,?,NULL,0)",
                (token,pipeline.config.character.id,json.dumps(plan,ensure_ascii=False),clock.iso(),clock.iso()))
            for c in selected:
                db.execute('INSERT INTO import_items(batch_id,character_id,format,source_id,content_hash,payload_json,last_at) VALUES (?,?,?,?,?,?,?)',
                    (token,pipeline.config.character.id,c.format,c.source_id,c.content_hash,json.dumps(c.dump(),ensure_ascii=False),c.listing()['last_at']))
        return {**plan,'token':token}

    def start(self, pipeline, token, confirmed):
        with self.lock, pipeline.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            status = pipeline.store.import_status(pipeline.config.character.id,token)
            if not confirmed or status['status'] not in ('quoted','cancelled','failed'):
                raise ValueError('時間と費用の目安を確認してから始めてください。')
            if status['quote']['models'] != pipeline.config.models.model_dump():
                raise ValueError('使うAIが変わりました。見積もりをやり直してください。')
            if status['quote']['storage_bytes'] > shutil.disk_usage(pipeline.store.path.parent).free:
                raise ValueError('保存先の空き容量が足りません。')
            if db.execute("SELECT 1 FROM import_batches WHERE status IN ('running','cancelling')").fetchone():
                raise ValueError('別の取り込みが進んでいます。終わるか止まるまでお待ちください。')
            # Two quoted batches may overlap. Recheck identities under the write lock.
            for row in db.execute('SELECT * FROM import_items WHERE batch_id=?',(token,)).fetchall():
                if row['session_id']:
                    continue
                duplicate = db.execute("SELECT 1 FROM import_items i JOIN import_batches b ON b.id=i.batch_id WHERE i.character_id=? AND i.batch_id<>? AND b.status NOT IN ('preview','quoted','undone') AND (i.content_hash=? OR (i.format=? AND i.source_id=? AND i.content_hash=?))", (pipeline.config.character.id,token,row['content_hash'],row['format'],row['source_id'],row['content_hash'])).fetchone()
                if duplicate:
                    db.execute("UPDATE import_items SET status='duplicate' WHERE id=?",(row['id'],))
            db.execute("UPDATE import_batches SET status='running',cancel_requested=0,error=NULL,updated_at=? WHERE id=?",(clock.iso(),token))
            db.execute("DELETE FROM import_batches WHERE id=? AND character_id=? AND status='preview'",(status['quote'].get('preview_id'),pipeline.config.character.id))
        self.pipelines[token] = pipeline
        self.futures[token] = submit(pipeline.store,self._work,pipeline,token)
        return pipeline.store.import_status(pipeline.config.character.id,token)

    def cancel(self, pipeline, token):
        with pipeline.store.connect() as db:
            if not db.execute("UPDATE import_batches SET cancel_requested=1,status='cancelling' WHERE id=? AND character_id=? AND status='running'",(token,pipeline.config.character.id)).rowcount:
                raise ValueError('進んでいる取り込みがありません。')
        return pipeline.store.import_status(pipeline.config.character.id,token)

    def _cancelled(self, pipeline, token):
        with pipeline.store.connect() as db:
            return bool(db.execute('SELECT cancel_requested FROM import_batches WHERE id=?',(token,)).fetchone()[0])

    def _ingest(self, pipeline, item):
        store, cid = pipeline.store,pipeline.config.character.id
        conversation = Conversation.load(json.loads(item['payload_json']))
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # The source conversation owns a separate session: never close a live chat.
            sid = uuid4().hex
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)',(sid,cid,conversation.title,conversation.messages[0].at))
            for user,assistant in exchanges(conversation):
                at, end = (user or assistant).at,(assistant or user).at
                cv = store._conversation(db,sid,at)
                row = db.execute('SELECT * FROM conversations WHERE id=?',(cv,)).fetchone()
                queued_chars = db.execute('SELECT COALESCE(SUM(length(user_text)+length(assistant_text)),0) FROM turns WHERE conversation_id=? AND id>?',(cv,row['queued_through'])).fetchone()[0]
                if queued_chars + len(user.text if user else '') + len(assistant.text if assistant else '') > 48000:
                    store._chunk(db,row,force=True,max_size=40)
                trace = {'Import': {'batch_id':item['batch_id'],'format':conversation.format,'source_id':conversation.source_id,
                    'user_time_source':user.time_source if user else None,'assistant_time_source':assistant.time_source if assistant else None}}
                db.execute("INSERT INTO turns(session_id,user_text,assistant_text,status,trace_json,created_at,assistant_at,conversation_id) VALUES (?,?,?,'completed',?,?,?,?)",
                    (sid,user.text if user else '',assistant.text if assistant else '',json.dumps(trace,ensure_ascii=False),at,end,cv))
                db.execute('UPDATE conversations SET last_activity_at=? WHERE id=?',(end,cv))
                store._chunk(db,db.execute('SELECT * FROM conversations WHERE id=?',(cv,)).fetchone(),max_size=40)
            for cv in db.execute("SELECT * FROM conversations WHERE session_id=? AND status='open'",(sid,)).fetchall():
                store._close(db,cv,cv['last_activity_at'])
            jobs = db.execute('SELECT j.* FROM memory_jobs j JOIN conversations c ON c.id=j.conversation_id WHERE c.session_id=?',(sid,)).fetchall()
            for job in jobs:
                db.execute('INSERT INTO import_steps(job_id,item_id) VALUES (?,?)',(job['id'],item['id']))
            db.execute("UPDATE import_items SET session_id=?,status='processing' WHERE id=?",(sid,item['id']))

    def _reflect(self, pipeline, job, token):
        store,cid = pipeline.store,pipeline.config.character.id
        with store.connect() as db:
            step = db.execute('SELECT * FROM import_steps WHERE job_id=?',(job['id'],)).fetchone()
            if step['reflected']:
                return
            turns = [dict(r) for r in db.execute('SELECT * FROM turns WHERE conversation_id=? AND id BETWEEN ? AND ? ORDER BY id',(job['conversation_id'],job['first_turn'],job['last_turn']))]
        context = {'turns':[timed_turn({k:v for k,v in t.items() if k!='trace_json'}) for t in turns],
            'plans':store.list_plans(cid)[-64:], 'canon_facts':store.list_canon(cid)[-64:],
            'secrets':{'definitions':pipeline.character.reveal.model_dump(mode='json'),'saved':store.list_secrets(cid)}, 'current_datetime':turns[-1]['assistant_at']}
        system = read_prompt('extract')+'\n'+read_prompt('import_context')+'\n'+read_prompt('import_reflect')+character_prefix(pipeline.character, actor=True, store=pipeline.store)
        result = for_step(pipeline.client, 'import', 'import').generate_json(pipeline.config.models.steward,system,[{'role':'user','content':json.dumps(context,ensure_ascii=False)}],ImportReflection)
        output = ImportReflection.model_validate_json(pipeline.client.sanitize_text(result.parsed.model_dump_json()))
        by_id = {t['id']:t for t in turns}
        if len({r.turn_id for r in output.turns}) != len(turns) or {r.turn_id for r in output.turns} != by_id.keys():
            raise ValueError('reflection must cover source turns exactly once')
        with store.connect() as db,store.import_journal(db,token):
            db.execute('BEGIN IMMEDIATE')
            for reflected in sorted(output.turns,key=lambda r:r.turn_id):
                t = by_id[reflected.turn_id]
                trace = json.loads(t['trace_json'])
                trace['Packet'] = {'input':{'emotional_weight':reflected.emotional_weight,'user_disclosure':reflected.user_disclosure}}
                extract = reflected.extract.model_dump(mode='json')
                for field in ('used_memories','used_threads','understanding_questions','new_threads','research_questions'):
                    extract[field] = []
                # All learned-at fields retain this historical utterance time.
                with clock.at(t['assistant_at']):
                    store._apply_post_check(db,t['session_id'],t['id'],cid,extract,trace)
                db.execute('UPDATE turns SET trace_json=? WHERE id=?',(json.dumps(trace,ensure_ascii=False),t['id']))
            db.execute('UPDATE import_steps SET reflected=1,trace_json=? WHERE job_id=?',(json.dumps(pipeline._safe_trace(result.trace()),ensure_ascii=False),job['id']))

    def _work(self, pipeline, token):
        store,cid = pipeline.store,pipeline.config.character.id
        started = perf_counter()
        try:
            with store.connect() as db:
                items = [dict(r) for r in db.execute("SELECT * FROM import_items WHERE batch_id=? AND status NOT IN ('completed','duplicate') ORDER BY last_at DESC,id DESC",(token,))]
            for item in items:
                if self._cancelled(pipeline,token):
                    raise InterruptedError()
                if not item['session_id']:
                    self._ingest(pipeline,item)
                with store.connect() as db:
                    jobs = [dict(r) for r in db.execute('SELECT j.* FROM memory_jobs j JOIN import_steps p ON p.job_id=j.id WHERE p.item_id=? ORDER BY j.first_turn',(item['id'],))]
                for job in jobs:
                    if self._cancelled(pipeline,token):
                        raise InterruptedError()
                    self._reflect(pipeline,job,token)
                    if job['status'] != 'completed':
                        with store.connect() as db:
                            db.execute("UPDATE memory_jobs SET status='pending' WHERE id=?",(job['id'],))
                        ids = episodize(pipeline,job,followups=False)
                        with store.connect() as db:
                            if db.execute('SELECT status FROM memory_jobs WHERE id=?',(job['id'],)).fetchone()[0] != 'completed':
                                raise ValueError('episodizer failed')
                    with store.connect() as db:
                        unlinked = [r[0] for r in db.execute("SELECT e.id FROM memory_episodes e LEFT JOIN episode_link_runs r ON r.source_episode=e.id WHERE e.job_id=? AND (r.status IS NULL OR r.status<>'completed')",(job['id'],))]
                    for eid in unlinked:
                        if self._cancelled(pipeline,token):
                            raise InterruptedError()
                        link_episode(pipeline,eid)
                        with store.connect() as db:
                            if db.execute('SELECT status FROM episode_link_runs WHERE source_episode=?',(eid,)).fetchone()[0] != 'completed':
                                raise ValueError('episode linking failed')
                with store.connect() as db:
                    db.execute("UPDATE import_items SET status='completed' WHERE id=?",(item['id'],))
                    db.execute('UPDATE import_batches SET updated_at=? WHERE id=?',(clock.iso(),token))
            if self._cancelled(pipeline,token):
                raise InterruptedError()
            self._summaries(pipeline,token)
            if self._cancelled(pipeline,token):
                raise InterruptedError()
            with store.connect() as db:
                db.execute("UPDATE import_batches SET status='completed',updated_at=?,error=NULL WHERE id=?",(clock.iso(),token))
        except InterruptedError:
            with store.connect() as db:
                db.execute("UPDATE import_batches SET status='cancelled',updated_at=? WHERE id=?",(clock.iso(),token))
        except Exception as error:
            from cbs.telemetry import record_error
            record_error(store, 'import', error)
            self.errors[token] = error
            with store.connect() as db:
                db.execute("UPDATE import_batches SET status='failed',error=?,updated_at=? WHERE id=?",('取り込みを中断しました。保存した分を保持しています。続きから再開できます。',clock.iso(),token))
        return {'elapsed_seconds':round(perf_counter()-started,3)}

    def _summaries(self,pipeline,token):
        store,cid = pipeline.store,pipeline.config.character.id
        episodes = store.list_memory_episodes(cid)
        candidates = digest_candidates(episodes,pipeline.config.memory)
        existing = {d['month']:d for d in store.list_monthly_digests(cid)}
        for month in sorted({clock.parse(e['started_at']).strftime('%Y-%m') for e in candidates}):
            if self._cancelled(pipeline,token):
                raise InterruptedError()
            ids = {e['id'] for e in candidates if clock.parse(e['started_at']).strftime('%Y-%m')==month}
            ids.update(existing.get(month,{}).get('episode_ids',[]))
            previous = existing.get(month)
            old_ids = set(previous.get('episode_ids',[])) if previous else set()
            originals = sorted((e for e in episodes if e['id'] in ids-old_ids),key=lambda e:(e['started_at'],e['id']))
            for start in range(0,len(originals),100):
                if self._cancelled(pipeline,token):
                    raise InterruptedError()
                group = originals[start:start+100]
                group_ids = {e['id'] for e in group}
                context = {'month':month,'episodes':[timed_memory(e) for e in group],
                    'previous_digest':{k:previous[k] for k in ('summary','details')} if previous else None,
                    'user_facts':[timed_memory(f) for f in store.list_user_facts(cid) if f['source_episode'] in group_ids][:64]}
                result = for_step(pipeline.client, 'import', 'import').generate_json(pipeline.config.models.steward,read_prompt('monthly_digest')+read_prompt('import_digest')+character_prefix(pipeline.character, actor=True, store=pipeline.store),[{'role':'user','content':json.dumps(context,ensure_ascii=False)}],MonthlyDigest)
                output = MonthlyDigest.model_validate_json(pipeline.client.sanitize_text(result.parsed.model_dump_json()))
                vector = for_step(pipeline.client, 'import', 'import').embed([output.summary+'\n'+output.details],pipeline.config.models.embedding,role='document')[0]
                old_ids.update(group_ids)
                store.save_monthly_digest(cid,month,output,vector,sorted(old_ids),batch_id=token,original_at=max(e['ended_at'] for e in group))
                previous = output.model_dump()
        from cbs.knowing import rebuild
        result = rebuild(pipeline,force=True,batch_id=token)
        if result.get('status') == 'failed':
            raise ValueError('understanding rebuild failed')

    def close(self):
        # Caller owns application shutdown. No process (including speech.exe) is touched.
        for token,future in list(self.futures.items()):
            if future.done():
                continue
            pipeline = self.pipelines[token]
            with pipeline.store.connect() as db:
                db.execute('UPDATE import_batches SET cancel_requested=1 WHERE id=?',(token,))
