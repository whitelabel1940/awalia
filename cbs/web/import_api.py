"""File preview → selection → explicit quote acceptance → durable background work."""
import base64
import binascii
import json
import zipfile

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from cbs.import_formats import MAX_BYTES
from cbs.import_jobs import ImportJobs
from cbs.llm import LLMError


class ImportInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class PreviewInput(ImportInput):
    filename: str = Field(min_length=1,max_length=500)
    payload: str = Field(max_length=(MAX_BYTES*4//3+8))


class PlanInput(ImportInput):
    preview_id: str
    keys: list[str] = Field(min_length=1,max_length=10000)


class StartInput(ImportInput):
    token: str
    confirmed: bool = False


class AboutInput(ImportInput):
    text: str = Field(min_length=1, max_length=32000)


class AboutSave(ImportInput):
    token: str
    keys: list[str] = Field(max_length=64)
    decisions: dict[str, int | None] = Field(default_factory=dict, max_length=64)


def install_import(app, selected, *, prefix='/api/import', state_name='import_jobs'):
    jobs = ImportJobs(selected)
    setattr(app.state, state_name, jobs)

    def call(fn,*args):
        try:
            try:
                return fn(*args)
            except Exception as error:
                from cbs.telemetry import record_error
                record_error(app.state.pipeline.store, 'import', error)
                raise
        except KeyError:
            raise HTTPException(404,'取り込みがありません。') from None
        except (UnicodeError,json.JSONDecodeError,binascii.Error,zipfile.BadZipFile):
            raise HTTPException(422,'書き出しファイルの形式を確認してください。') from None
        except ValueError as error:
            raise HTTPException(409,str(error)) from None
        except LLMError:
            raise HTTPException(502,'内容を読み取れませんでした。使うAIを確認して再試行してください。') from None

    @app.post(prefix+'/about/review')
    def about_review(body: AboutInput,pipeline=Depends(selected)):
        from cbs.import_profiles import review_about
        return call(review_about,pipeline,body.text)

    @app.post(prefix+'/about/save')
    def about_save(body: AboutSave,pipeline=Depends(selected)):
        from cbs.import_profiles import save_about
        return call(save_about,pipeline,body.token,body.keys,body.decisions)

    @app.post(prefix+'/preview')
    def preview(body: PreviewInput,pipeline=Depends(selected)):
        return call(lambda: jobs.preview(pipeline,body.filename,base64.b64decode(body.payload,validate=True)))

    @app.post(prefix+'/plan')
    def quote(body: PlanInput,pipeline=Depends(selected)):
        return call(jobs.quote,pipeline,body.preview_id,body.keys)

    @app.post(prefix+'/start')
    def start(body: StartInput,pipeline=Depends(selected)):
        return call(jobs.start,pipeline,body.token,body.confirmed)

    @app.get(prefix)
    def batches(pipeline=Depends(selected)):
        with pipeline.store.connect() as db:
            rows = db.execute("SELECT id FROM import_batches WHERE character_id=? AND status NOT IN ('preview','quoted') ORDER BY created_at DESC,id DESC LIMIT 20",(pipeline.config.character.id,)).fetchall()
        return [pipeline.store.import_status(pipeline.config.character.id,r['id']) for r in rows]

    @app.get(prefix+'/{token}')
    def status(token: str,pipeline=Depends(selected)):
        return call(pipeline.store.import_status,pipeline.config.character.id,token)

    @app.post(prefix+'/{token}/cancel')
    def cancel(token: str,pipeline=Depends(selected)):
        return call(jobs.cancel,pipeline,token)

    @app.post(prefix+'/{token}/undo')
    def undo(token: str,pipeline=Depends(selected)):
        def reverse():
            pipeline.store.undo_import(pipeline.config.character.id,token)
            return pipeline.store.import_status(pipeline.config.character.id,token)
        return call(reverse)
