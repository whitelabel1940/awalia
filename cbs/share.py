"""Local share invitation state; composing and image creation stay in the UI."""
import json
from pathlib import Path
from threading import RLock
from typing import Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict

from cbs import clock


class StartInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    character_id: str
    action: Literal['shared', 'dismissed']


def install_share(app, selected, data_dir):
    path = Path(data_dir) / 'share' / 'state.json'
    lock = RLock()

    def read():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    @app.get('/api/share/state')
    def state(pipeline=Depends(selected)):
        cid = pipeline.config.character.id
        available = not app.state.first_run
        with lock:
            answered = bool(read().get(cid, {}).get('start'))
        # The screen asks after every reply; once answered, skip the count over all turns.
        count = 0
        if available and not answered:
            with pipeline.store.connect() as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM turns t JOIN sessions s ON s.id=t.session_id "
                    "WHERE s.character_id=? AND t.kind='conversation' "
                    "AND (t.user_text<>'' OR EXISTS (SELECT 1 FROM photos p WHERE p.source_turn=t.id) "
                    "OR EXISTS (SELECT 1 FROM photo_redacted_turns p WHERE p.id=t.id)) "
                    "AND NOT EXISTS (SELECT 1 FROM import_items i WHERE i.session_id=s.id)", (cid,)
                ).fetchone()[0]
        with lock:
            invite = available and count > 10 and not read().get(cid, {}).get('start')
        return {'available': available, 'invite': invite, 'name': pipeline.character.model.display_name,
                'day': clock.now().date().isoformat(), **pipeline.config.share.model_dump()}

    @app.post('/api/share/start')
    def start(body: StartInput):
        pipeline = selected(body.character_id)
        if not app.state.first_run:
            from cbs.web.settings_api import atomic_write
            with lock:
                values = read()
                values.setdefault(pipeline.config.character.id, {}).setdefault('start', body.action)
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(path, json.dumps(values, ensure_ascii=False))
        return {'ok': True}
