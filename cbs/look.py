"""Author-owned appearance assets; image generation happens outside the app."""
from cbs.llm.cost import for_step
from io import BytesIO
from pathlib import Path
import yaml
from cbs.paths import resolve_paths
from cbs.expression import EXPRESSIONS
from cbs.runtime import read_prompt
from pydantic import BaseModel, ConfigDict, Field
import json
from typing import ClassVar


def field_catalog():
    return yaml.safe_load(Path(__file__).with_name('look_fields.yaml').read_text(encoding='utf-8'))


def field_value(data, path, default=''):
    for key in path.split('.'):
        if not isinstance(data, dict) or key not in data:
            return default
        data = data[key]
    return data


def set_field(data, path, value):
    keys = path.split('.')
    for key in keys[:-1]:
        data = data.setdefault(key, {})
    data[keys[-1]] = value


def normalize_look(data, *, world=None):
    """Migrate in memory; only an explicit save writes version 2."""
    result = {'version': 2, 'accent': {'amber':'mauve','akane':'madder','tokiwa':'evergreen','usuzumi':'charcoal'}.get(data.get('accent'), data.get('accent', 'mauve')), 'person': {}, 'image': {}}
    for field in field_catalog()['fields']:
        initial = field['initial']
        if isinstance(initial, dict):
            value = (world or {}).get(initial['world'], '')
            initial = initial.get('values', {}).get(value, '') if 'values' in initial else (
                '' if value in initial.get('except', []) else value)
        set_field(result, field['path'], field_value(data, field['path'], initial))
    return result


def validate_look(data):
    fields = {f['path']: f for f in field_catalog()['fields']}
    def visit(value, prefix=''):
        for key, item in value.items():
            path = f'{prefix}.{key}' if prefix else key
            if isinstance(item, dict):
                if not any(p.startswith(path + '.') for p in fields):
                    raise ValueError('見た目の欄を確認してください。')
                visit(item, path)
            elif path in ('version', 'accent'):
                continue
            elif path not in fields:
                raise ValueError('見た目の欄を確認してください。')
            else:
                spec = fields[path]
                valid = (isinstance(item, bool) if spec.get('type') == 'checkbox' else isinstance(item, str))
                if not valid or (spec.get('type') == 'select' and item not in spec['options']):
                    raise ValueError('見た目の値を確認してください。')
    visit(data)
    if sum(len(field_value(data, p)) for p, f in fields.items() if not f.get('type')) > 4000:
        raise ValueError('見た目の入力は合計4000字以内にしてください。')
    return data


def look_lines(data, *, actor=False):
    result = []
    for f in field_catalog()['fields']:
        path = f['path']
        if f.get('use') and data['image']['use'] != f['use']:
            continue
        if path.startswith('person.expressions.'):
            continue  # Sheet-only lines have their own position after the grid order.
        if actor and (path.startswith(('person.style.', 'image.')) or path in ('person.avoid', 'person.face.habits')):
            continue
        if f.get('type'):
            continue
        value = field_value(data, path)
        if not actor:
            if data['image']['use'] == 'sheet' and 'sheet_value' in f:
                value = f['sheet_value']
            value = value or f.get('prompt_default', '')
        if isinstance(value, str) and value.strip():
            result.append(f"{f['title']}: {value}")
    return result


def author_look(data):
    lines = look_lines(normalize_look(data), actor=True)
    # Keep the legacy-only packet exactly compatible.
    return data.get('features', '') if len(lines) == 1 and data.get('features') else '。'.join(lines)


def image_prompt(data):
    data = normalize_look(data)
    template = read_prompt('look_' + data['image']['use'])
    # Substitute once so user text containing template tokens stays literal.
    reference = read_prompt('look_reference') if data['image']['from_reference'] else ''
    expressions = []
    for field in field_catalog()['fields']:
        value = field_value(data, field['path'])
        if field['path'].startswith('person.expressions.') and value.strip():
            expressions.append(f"{field['title']}: {value}")
    expression_lines = '\n各表情の描き方:\n' + '\n'.join(expressions) + '\n' if expressions else ''
    # Replace template tokens before inserting author text, which may itself contain tokens.
    before, after = template.split('{{fields}}')
    ending = after.split('{{expressions}}')
    return (before.replace('{{reference}}', reference) + '\n'.join(look_lines(data))
            + expression_lines.join(ending))


class DraftItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str
    value: str = Field(max_length=4000)
    source: str
    quote: str


class LookDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    llm_role: ClassVar[str] = 'steward'
    assignments: list[DraftItem] = Field(default_factory=list, max_length=40)
    suggestions: list[DraftItem] = Field(default_factory=list, max_length=10)


class LookExpressions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    llm_role: ClassVar[str] = 'steward'
    suggestions: list[DraftItem] = Field(default_factory=list, max_length=9)


def draft_sources(character, model=None):
    return {**character.sources,
            'model': yaml.safe_dump((model or character.model).model_dump(mode='json'), allow_unicode=True),
            'expression': yaml.safe_dump(character.expression.model_dump(mode='json', by_alias=True), allow_unicode=True),
            'world': yaml.safe_dump(character.world, allow_unicode=True),
            'interests': yaml.safe_dump(character.interests, allow_unicode=True)}


def draft_look(config, client, sources, *, purpose='other'):
    fields = {f['path']: f for f in field_catalog()['fields']
              if not f.get('type') and not expression_field(f['path'])}
    result = for_step(client, 'look', purpose).generate_json(config.models.steward, read_prompt('look_draft'),
                                 [{'role': 'user', 'content': json.dumps({'sources': sources,
                                   'fields': list(fields.values())}, ensure_ascii=False)}], LookDraft)
    return verify_draft(result, client, sources, fields, ('assignments', 'suggestions')), result


def expression_field(path):
    return path == 'person.face.habits' or path.startswith('person.expressions.')


def suggest_expressions(config, client, sources, *, purpose='other'):
    fields = {f['path']: f for f in field_catalog()['fields'] if expression_field(f['path'])}
    result = for_step(client, 'look', purpose).generate_json(config.models.steward, read_prompt('look_expression'),
                                 [{'role': 'user', 'content': json.dumps({'sources': sources,
                                   'fields': list(fields.values())}, ensure_ascii=False)}], LookExpressions)
    return verify_draft(result, client, sources, fields, ('suggestions',)), result


def verify_draft(result, client, sources, fields, groups):
    """Share LK1's structural and continuous-source-quote checks."""
    from cbs.bootstrap import quote_matches
    checked, dropped, total = {}, 0, 0
    for group in groups:
        accepted, seen = [], set()
        for item in getattr(result.parsed, group):
            spec = fields.get(item.path)
            if (not spec or item.path in seen or not item.value.strip()
                    or (group == 'suggestions' and not spec.get('suggestion'))
                    or not quote_matches(item.quote, sources.get(item.source, ''))
                    or total + len(item.value) > 4000):
                dropped += 1
                continue
            accepted.append({key: client.sanitize_text(value) for key, value in item.model_dump().items()})
            seen.add(item.path)
            total += len(item.value)
        checked[group] = accepted
    checked['dropped'] = dropped
    return checked


def character_path(root, cid):
    root = (Path(root) if root is not None else resolve_paths()[1]).resolve()
    path = (root / cid).resolve()
    if path.parent != root or not path.is_dir():
        raise ValueError("キャラクターがありません。")
    return path


def look_meta(root, cid, *, world=None):
    path = character_path(root, cid)
    data = yaml.safe_load((path / "look.yaml").read_text(encoding="utf-8")) if (path / "look.yaml").is_file() else {}
    return {**normalize_look(data or {}, world=world),
            "cover": (path / "look" / "cover.png").is_file(),
            "expressions": [e for e in EXPRESSIONS if (path / "look" / (e + ".png")).is_file()]}


def image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("PNG / JPEG / WebP の画像を指定してください。")


def save_image(path, data, *, cover=False):
    image_type(data)
    try:
        from PIL import Image, ImageOps
    except ImportError:
        if cover:
            raise ValueError("横長画像の取り込みには Pillow が必要です。") from None
        pass
    else:
        try:
            with Image.open(BytesIO(data)) as image:
                if image.format not in ("PNG", "JPEG", "WEBP"):
                    raise ValueError()
                image = ImageOps.exif_transpose(image)
                if cover:
                    if image.width <= image.height:
                        raise ValueError()
                    image = image.convert("RGBA")
                    image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                else:
                    image = ImageOps.fit(image.convert("RGBA"), (512, 512), method=Image.Resampling.LANCZOS)
                stream = BytesIO()
                image.save(stream, format="PNG")
                data = stream.getvalue()
        except Exception:
            raise ValueError("横長の PNG / JPEG / WebP を指定してください。" if cover else "画像を読み込めませんでした。") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def sheet_prompt(features, free_text, world):
    return image_prompt({'features': features})
