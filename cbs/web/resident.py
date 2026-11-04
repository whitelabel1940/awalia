"""The desktop owner's private localhost lifecycle channel (no provider credentials)."""
import secrets
import webbrowser
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from fastapi import Depends, HTTPException, Request


def is_resident(request: Request):
    token = request.app.state.resident_token
    return bool(token and request.client and request.client.host == '127.0.0.1'
                and secrets.compare_digest(request.headers.get('X-Awalia-Desktop', ''), token))


class ExternalURL(BaseModel):
    url: str = Field(max_length=8192)


def install_resident(app, token=None, shutdown=None, selected=None):
    app.state.resident_token = token
    app.state.resident_shutdown = shutdown
    app.state.resident_draining = False

    @app.post('/api/external/open')
    def open_external(body: ExternalURL, request: Request):
        # JSON bodies follow the existing APIs (no cross-origin CORS permission).
        # The desktop owner token additionally prevents arbitrary local pages opening tabs.
        if not is_resident(request):
            raise HTTPException(403, '常駐アプリからだけ使えます。')
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            raise HTTPException(403, 'アプリの画面から開いてください。')
        try:
            url = urlsplit(body.url)
            valid = (url.scheme in {'http', 'https'} and url.hostname and url.port != 0
                     and not url.username and not url.password
                     and not any(ord(c) <= 32 or ord(c) == 127 for c in body.url)
                     and '\\' not in body.url)
        except ValueError:
            valid = False
        if not valid:
            raise HTTPException(422, 'http(s) の URL を指定してください。')
        if not webbrowser.open(body.url, new=2):
            raise HTTPException(503, 'ブラウザを開けませんでした。')
        return {'opened': True}

    @app.get('/api/resident/restore-ready')
    def restore_ready(request: Request):
        if not is_resident(request):
            raise HTTPException(403, '常駐アプリからだけ使えます。')
        return {'ready': not app.state.first_run and (app.state.settings_dir / 'restore/staged').is_dir()}

    @app.get('/api/resident/face')
    def face(request: Request, pipeline=Depends(selected)):
        if not is_resident(request):
            raise HTTPException(403, '常駐アプリからだけ使えます。')
        from cbs import clock
        from cbs.autonomy import quiet_hours
        cid = pipeline.config.character.id
        expression = pipeline.store.resident_expression(
            cid, pipeline.config.autonomy.resident_expression_neutral_minutes)
        now = clock.now()
        return {'characterId': cid, 'name': pipeline.character.model.display_name,
                **expression,
                'quiet': quiet_hours(now.hour, pipeline.config.autonomy),
                # Fail closed on a stalled/restarting page, including a quiet-hour boundary.
                'validForMs': min(15000, int((3600 - now.minute * 60 - now.second - now.microsecond / 1e6) * 1000))}

    @app.get('/api/resident/health')
    def health(request: Request):
        if not is_resident(request):
            raise HTTPException(403, '常駐アプリからだけ使えます。')
        return {'ready': not app.state.resident_draining}

    @app.post('/api/resident/shutdown', status_code=202)
    def stop(request: Request):
        if not is_resident(request) or app.state.resident_shutdown is None:
            raise HTTPException(403, '常駐アプリからだけ使えます。')
        app.state.resident_draining = True
        # Uvicorn first finishes requests, then runs the normal lifespan drain.
        app.state.resident_shutdown()
        return {'status': 'draining'}
