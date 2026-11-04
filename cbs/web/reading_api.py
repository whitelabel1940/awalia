"""Read-only source registration; work shares the per-DB autonomy queue."""
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from cbs.autonomy import reading_tools
from cbs.steward import submit


class SourceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(min_length=1, max_length=2048)


class SourceState(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


def install_reading(app, selected):
    def owned(p, sid):
        if not any(s['id'] == sid for s in p.store.list_sources(p.config.character.id)):
            raise HTTPException(404, '登録したサイトが見つかりません。')

    def queued(p, fn, *args):
        with p._lock:
            p.wait_for_steward()
            return submit(p.store, fn, *args).result()

    @app.get('/api/sources')
    def sources(p=Depends(selected)):
        return p.store.list_sources(p.config.character.id)

    @app.post('/api/sources')
    def register(body: SourceInput, p=Depends(selected)):
        try:
            return queued(p, reading_tools(p).register, p.config.character.id, body.url)
        except ValueError:
            raise HTTPException(422, '公開されている http / https のURLを指定してください。') from None

    @app.post('/api/sources/{sid}/test')
    def test_source(sid: int, p=Depends(selected)):
        owned(p, sid)
        return queued(p, reading_tools(p).refresh, p.config.character.id, sid)

    @app.patch('/api/sources/{sid}')
    def enable(sid: int, body: SourceState, p=Depends(selected)):
        owned(p, sid)
        def update():
            with p.store.connect() as db:
                db.execute('UPDATE sources SET enabled=? WHERE id=? AND character_id=?', (body.enabled, sid, p.config.character.id))
        queued(p, update)
        return {'ok': True}

    @app.delete('/api/sources/{sid}')
    def delete(sid: int, p=Depends(selected)):
        owned(p, sid)
        def remove():
            with p.store.connect() as db:
                db.execute('DELETE FROM sources WHERE id=? AND character_id=?', (sid, p.config.character.id))
        queued(p, remove)
        return {'ok': True}
