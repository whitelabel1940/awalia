"""Notebook controls: all writes scoped to the selected partner."""
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from cbs import clock


class Control(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['agree', 'disagree', 'forget']
    detail: str = Field(default='', max_length=1000)


class Preference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    stopped: bool
    clear: bool = False


class FactEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=2000)
    private: bool | None = None


class PrivatePreferences(BaseModel):
    model_config = ConfigDict(extra='forbid')
    categories: dict[str, bool]


class MentionPreference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mention_override: bool | None


def knowing_note(pipeline, *, private=False):
    store, cid = pipeline.store, pipeline.config.character.id
    facts = sorted(store.list_user_facts(cid,current_only=True),
                   key=lambda f: (clock.parse(f['learned_at']), f['id']), reverse=True)
    if private:
        return {'facts': [{k:f[k] for k in ('id','content','private','private_category','category_label','category_allow_mention','mention_override','allow_mention')} for f in facts if f['private']]}
    items = store.list_understandings(cid,pipeline.config.knowing)
    public=[]
    for i in items:
        if i['expired']:
            continue
        value={k:i[k] for k in ('id','kind','text','condition','stage','stage_label','status','expires_at','expired')}
        for key in ('evidence','counterevidence'):
            value[key]=[{k:o[k] for k in ('summary','observed_at','response')} for o in i[key]]
        public.append(value)
    return {'stopped': bool(store.knowing_setting(cid)['stopped']),
        'understandings': public,
        'facts': [{'id':f['id'],'content':f['content'],'private':False} for f in facts if not f['private']]}


def install_knowing(app, selected):
    @app.get('/api/note/private-settings')
    def private_settings(pipeline=Depends(selected)):
        return {'categories': pipeline.store.private_settings(pipeline.config.character.id)}

    @app.put('/api/note/private-settings')
    def save_private_settings(body: PrivatePreferences, pipeline=Depends(selected)):
        try:
            pipeline.store.set_private_settings(pipeline.config.character.id,body.categories)
        except ValueError:
            raise HTTPException(422,'種類を確認してください。') from None
        return private_settings(pipeline)

    @app.put('/api/note/facts/{fact_id}/mention')
    def mention(fact_id: int, body: MentionPreference, pipeline=Depends(selected)):
        store,cid = pipeline.store,pipeline.config.character.id
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute("SELECT * FROM user_facts WHERE id=? AND character_id=? AND status='current' AND private=1", (fact_id,cid)).fetchone()
            if not old:
                raise HTTPException(404,'事実がありません。')
            db.execute('UPDATE user_facts SET mention_override=? WHERE id=?', (body.mention_override,fact_id))
            store._sync_private_threads(db,cid)
        return {'ok':True}

    @app.get('/api/note/private')
    def private_note(pipeline=Depends(selected)):
        return knowing_note(pipeline,private=True)

    @app.put('/api/note/understandings/{understanding_id}')
    def control(understanding_id: int, body: Control, pipeline=Depends(selected)):
        try:
            pipeline.store.understanding_action(pipeline.config.character.id,understanding_id,body.action,pipeline.client.sanitize_text(body.detail))
        except KeyError:
            raise HTTPException(404,'理解がありません。') from None
        if body.action == 'disagree':
            from cbs.knowing import rebuild
            from cbs.steward import submit
            submit(pipeline.store,lambda: rebuild(pipeline,force=True))
        return {'ok':True}

    @app.put('/api/note/inference')
    def inference(body: Preference, pipeline=Depends(selected)):
        pipeline.store.set_knowing(pipeline.config.character.id,body.stopped,body.clear)
        return {'stopped':body.stopped}

    def fact_change(pipeline, fact_id, body=None):
        store, cid = pipeline.store,pipeline.config.character.id
        content = pipeline.client.sanitize_text(body.content).strip() if body else ''
        if body and not content:
            raise HTTPException(422,'内容を入力してください。')
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute("SELECT * FROM user_facts WHERE id=? AND character_id=? AND status='current'", (fact_id,cid)).fetchone()
            if not old:
                raise HTTPException(404,'事実がありません。')
            # A new revision preserves provenance and prevents old fact-ID updates overwriting the correction.
            if body:
                row = db.execute('INSERT INTO user_facts (character_id,content,learned_at,last_confirmed_at,source_episode,importance,pinned,private,private_category,mention_override,event_date,follow_up) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (cid,content,clock.iso(),clock.iso(),old['source_episode'],old['importance'],old['pinned'],old['private'] if body.private is None else body.private,
                     old['private_category'],old['mention_override'],old['event_date'],old['follow_up']))
                db.execute("UPDATE user_facts SET status='superseded',superseded_by=? WHERE id=?", (row.lastrowid,fact_id))
                if old['private']:
                    # Keep the one-shot status across a private fact correction, too.
                    for thread in db.execute('SELECT * FROM threads WHERE character_id=? AND source_ref=?', (cid,f'fact:{fact_id}')).fetchall():
                        db.execute('UPDATE threads SET source_ref=?,topic=?,detail=?,updated_at=? WHERE id=?',
                            (f'fact:{row.lastrowid}',content,content,clock.iso(),thread['id']))
                        store._thread_history(db,thread['id'],'fact_revision',dict(thread))
            else:
                db.execute("UPDATE user_facts SET status='deleted' WHERE id=?", (fact_id,))
            for thread in db.execute("SELECT * FROM threads WHERE character_id=? AND source_ref=? AND status='open'", (cid,f'fact:{fact_id}')).fetchall():
                store._thread_status(db,thread,'dropped')
            store._sync_private_threads(db,cid)
        if body:
            from cbs.steward import submit
            submit(store,pipeline._warm_reply_embeddings)
        return {'ok':True}

    @app.put('/api/note/facts/{fact_id}')
    def edit_fact(fact_id: int, body: FactEdit, pipeline=Depends(selected)):
        return fact_change(pipeline,fact_id,body)

    @app.delete('/api/note/facts/{fact_id}')
    def delete_fact(fact_id: int, pipeline=Depends(selected)):
        return fact_change(pipeline,fact_id)
