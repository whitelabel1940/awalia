from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse

from cbs.photos import delete_photo, photo_path


def install_photos(app, selected):
    @app.post('/api/photos/notice')
    def notice(pipeline=Depends(selected)):
        with pipeline.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO photo_notice VALUES (1)')
        return {'seen': True}

    @app.get('/api/photos/{photo_id}')
    def image(photo_id: str, pipeline=Depends(selected)):
        with pipeline.store.connect() as db:
            row = db.execute('SELECT 1 FROM photos WHERE id=? AND character_id=?',
                (photo_id, pipeline.config.character.id)).fetchone()
        if not row:
            raise HTTPException(404, '写真がありません。')
        try:
            path = photo_path(pipeline.store, photo_id)
            if not path.is_file():
                raise KeyError(photo_id)
        except KeyError:
            raise HTTPException(404, '写真がありません。') from None
        return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})

    @app.delete('/api/photos/{photo_id}')
    def remove(photo_id: str, pipeline=Depends(selected)):
        with pipeline._lock:
            # Deletion waits for existing writers so a late Extract cannot restore evidence.
            pipeline.wait_for_steward()
            try:
                delete_photo(pipeline, photo_id)
            except KeyError:
                raise HTTPException(404, '写真がありません。') from None
            except (OSError, ValueError):
                raise HTTPException(409, '写真を削除できませんでした。少し待ってやり直してください。') from None
        return {'deleted': True}
