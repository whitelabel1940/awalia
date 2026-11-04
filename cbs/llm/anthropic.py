"""Anthropic Messages over httpx; credentials never enter requests in traces."""
from cbs.llm.cost import report_usage
import json
import os
import ssl
from time import perf_counter

import httpx

from cbs.llm.errors import provider_error
from cbs.llm import LLMError, LLMResult, image_messages, sanitize_content
from cbs.llm.openai_client import openrouter_sources


def output_schema(schema):
    """Send supported constraints; validate the full original model locally."""
    unsupported = {'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
                   'multipleOf', 'minLength', 'maxLength', 'maxItems', 'uniqueItems'}
    def visit(value):
        if isinstance(value, list):
            return [visit(v) for v in value]
        if not isinstance(value, dict):
            return value
        result = {k: visit(v) for k, v in value.items() if k not in unsupported}
        constraints = {k: v for k, v in value.items() if k in unsupported}
        if value.get('minItems', 0) not in (0, 1):
            constraints['minItems'] = result.pop('minItems')
        if constraints:
            result['description'] = (result.get('description', '') + ' ' + json.dumps(constraints)).strip()
        if result.get('type') == 'object':
            result['additionalProperties'] = False
        return result
    return visit(schema.model_json_schema())


class AnthropicLLMClient:
    def __init__(self, provider_config, tls_strict=True, *, api_key=None):
        self._key = api_key if api_key is not None else os.environ.get(provider_config.api_key_env or '', '')
        if not self._key:
            raise LLMError('API キーを設定してください。', kind='auth', provider='anthropic')
        context = ssl.create_default_context()
        if not tls_strict:
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        headers = {'x-api-key': self._key, 'anthropic-version': '2023-06-01'}
        # Keys not scoped to a workspace must name one; the ID is not secret but stays in the environment beside the key.
        if workspace := os.environ.get('ANTHROPIC_WORKSPACE_ID', '').strip():
            headers['anthropic-workspace-id'] = workspace
        self._client = httpx.Client(base_url=provider_config.base_url or 'https://api.anthropic.com/v1/',
            headers=headers, verify=context, timeout=120)

    def sanitize_text(self, text):
        return text.replace(self._key, '[REDACTED]')

    def generate_images_json(self, model, system, text, images, schema):
        return self._generate(model, system, image_messages(text, images), schema)

    def generate_text(self, model, system, messages):
        return self._generate(model, system, messages)

    def generate_json(self, model, system, messages, schema):
        return self._generate(model, system, messages, schema)

    def generate_grounded(self, model, system, messages, *, include_domains=None):
        return self._generate(model, system, messages, grounded=True, include_domains=include_domains)

    def _request(self, model, system, messages, schema=None, grounded=False, include_domains=None):
        body = {'model': model, 'system': self.sanitize_text(system),
                'messages': [{'role': m['role'], 'content': anthropic_content(sanitize_content(self, m['content']))} for m in messages],
                'max_tokens': 4096 if schema or grounded else 1536}
        if schema:
            body['output_config'] = {'format': {'type': 'json_schema', 'schema': output_schema(schema)}}
        if grounded:
            tool = {'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 3}
            if include_domains is not None:
                if not include_domains:
                    raise LLMError('検索対象のドメインがありません。')
                tool['allowed_domains'] = include_domains
            body['tools'] = [tool]
        trace = {k: v for k, v in body.items() if k != 'output_config'}
        trace.update(provider='anthropic', api='messages', schema=schema.__name__ if schema else None)
        return body, trace

    def _generate(self, model, system, messages, schema=None, grounded=False, include_domains=None):
        body, request = self._request(model, system, messages, schema, grounded, include_domains)
        started, text, usage = perf_counter(), '', {}
        sources, queries, results = [], [], []
        try:
            response = self._client.post('messages', json=body)
            response.raise_for_status()
            data = response.json()
            usage = data.get('usage') or {}
            report_usage(usage)
            for block in data.get('content', []):
                if block['type'] == 'text':
                    text += self.sanitize_text(block.get('text', ''))
                    for citation in block.get('citations') or []:
                        sources.extend(openrouter_sources([{'type': 'url_citation', 'url_citation': citation}], self.sanitize_text))
                elif block['type'] == 'server_tool_use' and block.get('name') == 'web_search':
                    queries.append(self.sanitize_text(block.get('input', {}).get('query', '')))
                elif block['type'] == 'web_search_tool_result' and isinstance(block.get('content'), dict):
                    raise ValueError('Search tool error')
                elif block['type'] == 'web_search_tool_result':
                    results.extend(r for r in block.get('content') or [] if r.get('type') == 'web_search_result')
            if not text.strip() or data.get('stop_reason') != 'end_turn':
                raise ValueError('Incomplete/refused response')
            if grounded and not sources:
                # The reply sometimes cites nothing inline; fall back to the pages the search returned.
                sources = openrouter_sources([{'type': 'url_citation', 'url_citation': r} for r in results[:3]], self.sanitize_text)
            if grounded and not sources:
                raise ValueError('Search returned no citations')
            parsed = schema.model_validate_json(text) if schema else None
        except Exception as error:
            trace = LLMResult(text, None, usage, (perf_counter()-started)*1000, model, request).trace()
            raise provider_error(error, 'anthropic', trace=trace) from None
        return LLMResult(text.strip(), parsed, usage, (perf_counter()-started)*1000, model, request,
                         queries, list({s['uri']: s for s in sources}.values()))

    def generate_text_stream(self, model, system, messages, on_delta):
        body, request = self._request(model, system, messages)
        body['stream'] = request['stream'] = True
        started, text, usage, complete, stop = perf_counter(), '', {}, False, None
        # Hold a possible credential prefix across SSE chunks before delivering text.
        pending = ''
        try:
            with self._client.stream('POST', 'messages', json=body) as response:
                if response.is_error:
                    response.read()
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith('data:'):
                        continue
                    event = json.loads(line[5:].strip())
                    kind = event.get('type')
                    if kind == 'error':
                        raise ValueError('Stream error')
                    if kind == 'message_start':
                        usage.update(event.get('message', {}).get('usage', {}))
                        report_usage(usage)
                    if kind == 'content_block_delta' and event.get('delta', {}).get('type') == 'text_delta':
                        part = event['delta']['text']
                        text += part
                        pending = self.sanitize_text(pending + part)
                        hold = next((n for n in range(min(len(self._key)-1, len(pending)), 0, -1)
                                     if pending.endswith(self._key[:n])), 0)
                        ready = pending[:-hold] if hold else pending
                        pending = pending[-hold:] if hold else ''
                        if ready:
                            on_delta(ready)
                    if kind == 'message_delta':
                        usage.update(event.get('usage', {}))
                        report_usage(usage)
                        stop = event.get('delta', {}).get('stop_reason', stop)
                    if kind == 'message_stop':
                        complete = True
            if not complete or stop != 'end_turn' or not text.strip():
                raise ValueError('Incomplete stream')
        except Exception as error:
            raise provider_error(error, 'anthropic') from None
        if pending:
            on_delta(self.sanitize_text(pending))
        return LLMResult(self.sanitize_text(text), None, usage, (perf_counter()-started)*1000, model, request)


def anthropic_content(content):
    if isinstance(content, str):
        return content
    result = []
    for part in content:
        if part['type'] == 'text':
            result.append(part)
        else:
            header, data = part['image_url']['url'].split(',', 1)
            result.append({'type': 'image', 'source': {'type': 'base64', 'media_type': header[5:].split(';')[0], 'data': data}})
    return result
