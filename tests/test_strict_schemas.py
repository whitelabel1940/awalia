"""Every structured output sent to a provider must satisfy OpenAI's strict JSON schema rules.

A free-form dict or a nested model without extra='forbid' gets HTTP 400 (invalid_json_schema)
and silently disables that step (HB1 broke Extract this way).
"""
import importlib
import pkgutil

import pytest
from pydantic import BaseModel
from openai.lib._pydantic import to_strict_json_schema

import cbs


def output_models():
    seen = {}
    for info in pkgutil.walk_packages(cbs.__path__, 'cbs.'):
        if info.name.startswith('cbs.web'):
            continue
        try:
            module = importlib.import_module(info.name)
        except Exception:
            continue
        for value in vars(module).values():
            if (isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == module.__name__
                    and 'llm_role' in getattr(value, '__class_vars__', set())):
                seen[f'{value.__module__}.{value.__name__}'] = value
    return seen


def problems(node, path='$'):
    found = []
    if isinstance(node, dict):
        if node.get('type') == 'object' or 'properties' in node:
            if node.get('additionalProperties') is not False:
                found.append(f'{path}: additionalProperties must be false')
            if set(node.get('required', [])) != set(node.get('properties', {})):
                found.append(f'{path}: every property must be required')
        for key, value in node.items():
            found += problems(value, f'{path}.{key}')
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found += problems(value, f'{path}[{i}]')
    return found


MODELS = output_models()


def test_found_output_models():
    assert len(MODELS) >= 20


@pytest.mark.parametrize('name', sorted(MODELS))
def test_strict_schema(name):
    assert problems(to_strict_json_schema(MODELS[name])) == []
