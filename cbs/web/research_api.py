"""Developer-only management of the separate research DB."""
from datetime import date

from fastapi import Depends, HTTPException

from cbs import clock
from cbs.config import ROOT
from cbs.research import attach


def install_research(app, pipeline):
    research = attach(pipeline)

    def developer():
        if not (ROOT / '.git').exists():
            raise HTTPException(404, '見つかりません。')

    @app.get('/api/research', dependencies=[Depends(developer)])
    def status():
        return research.status()

    @app.post('/api/research/export', dependencies=[Depends(developer)])
    def export(body: dict):
        if (set(body) - {'folder', 'cloud_confirmed'} or not isinstance(body.get('folder'), str)
                or not body['folder'] or type(body.get('cloud_confirmed', False)) is not bool):
            raise HTTPException(422, '書き出し先を確認してください。')
        try:
            filename = research.export(body['folder'], cloud_confirmed=body.get('cloud_confirmed', False))
        except ValueError as error:
            if str(error) == 'cloud_confirmation_required':
                return {'cloud_confirmation_required': True}
            raise HTTPException(422, str(error)) from None
        return {'filename': filename}

    @app.post('/api/research/delete', dependencies=[Depends(developer)])
    def delete(body: dict):
        if set(body) - {'before', 'confirmed'} or body.get('confirmed') is not True:
            raise HTTPException(422, '削除の確認が必要です。')
        before = body.get('before')
        try:
            if before is not None:
                before = clock.parse(date.fromisoformat(before).isoformat() + 'T00:00:00').isoformat()
            if research.error or research.status()['reason']:
                raise ValueError()
            if research.db.path.exists():
                research.db.delete(before)
        except (ValueError, TypeError):
            raise HTTPException(422, '削除する日付と記録の状態を確認してください。') from None
        except Exception:
            raise HTTPException(422, '判断の記録を削除できませんでした。') from None
        return research.status()
