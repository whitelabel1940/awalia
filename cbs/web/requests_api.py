"""Partner-scoped adjustments, without waiting for optional maintenance."""
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class RequestLine(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str | None = None
    text: str


class RequestSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=0)
    fields: dict[str, str]
    lines: list[RequestLine]


class UndoRequests(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=0)


def install_requests(app, selected):
    def view(p):
        return {**p.store.current_requests(p.character), 'limits': p.config.requests.model_dump()}

    @app.get('/api/partner/requests')
    def get_requests(p=Depends(selected)):
        return view(p)

    @app.put('/api/partner/requests')
    def put_requests(body: RequestSettings, p=Depends(selected)):
        try:
            with p._lock:
                p.store.save_requests(p.character,
                    {k: p.client.sanitize_text(v) for k, v in body.fields.items()},
                    [{**l.model_dump(), 'text': p.client.sanitize_text(l.text)} for l in body.lines],
                    p.config.requests, body.version)
            return view(p)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.post('/api/partner/requests/undo')
    def undo_requests(body: UndoRequests, p=Depends(selected)):
        try:
            with p._lock:
                p.store.save_requests(p.character, {}, [], p.config.requests, body.version, undo=True)
            return view(p)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
