from pathlib import Path
from typing import Literal
import json
import yaml
from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from cbs.look import (character_path, look_meta, save_image, image_type, field_catalog,
                      normalize_look, validate_look, image_prompt, draft_look, draft_sources,
                      suggest_expressions)
from cbs.expression import EXPRESSIONS
from cbs.web.settings_api import atomic_write
from cbs.bootstrap_schema import WorldInput


class LookMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accent: Literal["mauve", "madder", "evergreen", "charcoal", "amber", "akane", "tokiwa", "usuzumi"] = "mauve"
    features: str = Field(default="", max_length=4000)
    version: Literal[2] = 2
    person: dict = Field(default_factory=dict)
    image: dict = Field(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def legacy_accent(cls, data):
        if isinstance(data, dict) and 'accent' in data:
            data = {**data, 'accent': {'amber':'mauve','akane':'madder','tokiwa':'evergreen','usuzumi':'charcoal'}.get(data['accent'],data['accent'])}
        return data

    @model_validator(mode='after')
    def valid_fields(self):
        validate_look(self.model_dump(include={'version', 'accent', 'features', 'person', 'image'}))
        return self


class LookPrompt(LookMeta):
    free_text: str = Field(default="", max_length=20000)
    world: WorldInput | None = None


def install_look(app, selected, get_pipeline, characters_root):
    def original_input(pipeline):
        path = character_path(characters_root, pipeline.config.character.id) / 'source' / 'input.json'
        return path.read_text(encoding='utf-8') if path.is_file() else ''

    def initial_world(pipeline):
        original = original_input(pipeline)
        # The generated world has axes; the author's original preset selects YAML defaults.
        return {**(json.loads(original).get('world', {}) if original else {}), **pipeline.character.world}

    def slot(expression):
        if expression not in (*EXPRESSIONS, 'cover'):
            raise HTTPException(422, "表情の枠を確認してください。")

    @app.get("/api/look")
    def get_look(pipeline=Depends(selected)):
        return look_meta(characters_root, pipeline.config.character.id, world=initial_world(pipeline))

    @app.get('/api/look/fields')
    def fields():
        return field_catalog()

    @app.post('/api/look/draft')
    def draft(pipeline=Depends(selected)):
        return suggestions(pipeline, draft_look)

    @app.post('/api/look/expression-suggestions')
    def expressions(pipeline=Depends(selected)):
        return suggestions(pipeline, suggest_expressions)

    def suggestions(pipeline, generate):
        from cbs.llm import LLMError
        try:
            sources = draft_sources(pipeline.store.current_character(pipeline.character), pipeline.store.current_model(pipeline.character))
            sources['current_interests'] = json.dumps([
                {k: i[k] for k in ('topic', 'detail', 'status')}
                for i in pipeline.store.list_interests(pipeline.config.character.id)
                if i['status'] != 'fading'], ensure_ascii=False)
            if original := original_input(pipeline):
                sources['input.json'] = original
            checked, _ = generate(pipeline.config, pipeline.client, sources)
            return checked
        except LLMError:
            raise HTTPException(502, '下書きを作れませんでした。もう一度お試しください。') from None

    @app.put("/api/look/meta")
    def put_meta(body: LookMeta, pipeline=Depends(selected)):
        old = look_meta(characters_root, pipeline.config.character.id, world=initial_world(pipeline))
        data = {**{key: old[key] for key in ('accent', 'features', 'person', 'image')}, **body.model_dump(exclude_unset=True)}
        data = normalize_look(data)
        try:
            validate_look(data)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        data = yaml.safe_load(pipeline.client.sanitize_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False)))
        path = character_path(characters_root, pipeline.config.character.id)
        atomic_write(path / "look.yaml", yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        return look_meta(characters_root, pipeline.config.character.id)

    @app.post("/api/look/prompt")
    def prompt(body: LookPrompt, character_id: str | None = None):
        # Before confirmation there is no published partner to select yet.
        pipeline = app.state.pipeline if body.world else selected(character_id)
        return {"prompt": pipeline.client.sanitize_text(image_prompt(body.model_dump(include={'person', 'image', 'features'})))}

    @app.put("/api/look/{expression}")
    async def upload(expression: str, request: Request, pipeline=Depends(selected)):
        slot(expression)
        if request.headers.get("content-type", "").split(";", 1)[0] not in ("image/png", "image/jpeg", "image/webp"):
            raise HTTPException(415, "PNG / JPEG / WebP の画像を送ってください。")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 5 * 1024 * 1024:
                raise HTTPException(413, "画像は5MB以下にしてください。")
        try:
            save_image(character_path(characters_root, pipeline.config.character.id) / "look" / (expression + ".png"), bytes(data), cover=expression == 'cover')
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return look_meta(characters_root, pipeline.config.character.id)

    @app.delete("/api/look/{expression}")
    def delete(expression: str, pipeline=Depends(selected)):
        slot(expression)
        (character_path(characters_root, pipeline.config.character.id) / "look" / (expression + ".png")).unlink(missing_ok=True)
        return look_meta(characters_root, pipeline.config.character.id)

    @app.get("/looks/{cid}/{expression}")
    def image(cid: str, expression: str):
        slot(expression)
        get_pipeline(cid)
        path = character_path(characters_root, cid) / "look"
        selected_path = path / (expression + ".png")
        if not selected_path.is_file() and expression != 'cover':
            selected_path = path / "neutral.png"
        if not selected_path.is_file():
            raise HTTPException(404, "画像がありません。")
        data = selected_path.read_bytes()
        return Response(data, media_type=image_type(data))
