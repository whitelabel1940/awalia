from cbs.llm.cost import report_usage, report_response
import json
import os
import ssl
from time import perf_counter

import httpx
import openai

from cbs.llm.errors import provider_error
from cbs.llm import LLMError, LLMResult, image_messages, sanitize_content


class OpenAILLMClient:
    """OpenAI Responses API, or Chat Completions for OpenAI-compatible local servers (Ollama, LM Studio, llama.cpp)."""

    def __init__(self, name, provider_config, tls_strict=True, zdr=False, *, api_key=None):
        self.name = name
        self._zdr = zdr and name == "openrouter"
        self._key = api_key if api_key is not None else (os.environ.get(provider_config.api_key_env, "") if provider_config.api_key_env else "")
        if provider_config.api_key_env and not self._key:
            raise LLMError("API キーを設定してください。", kind="auth", provider=name)
        tls_context = ssl.create_default_context()
        if not tls_strict:
            # Same relaxation as the Gemini client: chain and hostname verification stay on.
            tls_context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self._client = openai.OpenAI(api_key=self._key or "not-needed", base_url=provider_config.base_url,
                                     timeout=120, max_retries=0, http_client=httpx.Client(verify=tls_context))
        self._api = provider_config.api
        self._reasoning = provider_config.reasoning_effort

    def sanitize_text(self, text):
        return text.replace(self._key, "[REDACTED]") if self._key else text

    def generate_grounded(self, model, system, messages, *, include_domains=None):
        if not ((self.name == "openrouter" and self._api == 'chat') or (self.name == 'openai' and self._api == 'responses')):
            raise LLMError("このサービスでは出典つきの調べ物を利用できません。")
        return self._generate(model, system, messages, grounded=True, include_domains=include_domains)

    def _chat_extra(self, role, grounded=False, include_domains=None):
        if self.name != "openrouter":
            return {}
        extra = {"provider": {"zdr": True}} if self._zdr else {}
        if self._reasoning.get(role):
            extra["reasoning"] = {"effort": self._reasoning[role]}
        if grounded:
            parameters = {'engine': 'exa', 'max_results': 3, 'max_total_results': 6, 'max_uses': 2}
            if include_domains is not None:
                if not include_domains:
                    raise LLMError('検索対象のドメインがありません。')
                parameters['allowed_domains'] = include_domains
            extra['tools'] = [{'type': 'openrouter:web_search', 'parameters': parameters}]
            extra['max_tool_calls'] = 2
        return {"extra_body": extra}

    def generate_images_json(self, model, system, text, images, schema):
        return self._generate(model, system, image_messages(text, images), schema)

    def generate_text(self, model, system, messages):
        return self._generate(model, system, messages)

    def transcribe_audio(self, model, system, audio):
        import base64
        try:
            if self.name == 'openai':
                result = self._client.audio.transcriptions.create(model=model,
                    file=('voice.wav', audio, 'audio/wav'), prompt=self.sanitize_text(system), language='ja')
                report_response(result, 'usage')
                return self.sanitize_text(result.text)
            if self.name != 'openrouter' or self._api != 'chat':
                raise ValueError()
            result = self._client.chat.completions.create(model=model, messages=[
                {'role': 'system', 'content': self.sanitize_text(system)},
                {'role': 'user', 'content': [{'type': 'input_audio', 'input_audio': {
                    'data': base64.b64encode(audio).decode('ascii'), 'format': 'wav'}}]}],
                **self._chat_extra('stt'))
            report_response(result, 'usage')
            return self.sanitize_text(result.choices[0].message.content or '')
        except Exception as error:
            raise provider_error(error, self.name) from None

    def generate_json(self, model, system, messages, schema):
        return self._generate(model, system, messages, schema)

    def generate_video_json(self, model, system, url, schema):
        from openai.lib._pydantic import to_strict_json_schema
        from cbs.media import youtube_url
        if self.name != 'openrouter' or model != 'google/gemini-3.8-flash' or not youtube_url(url):
            raise LLMError('この設定またはURLでは動画入力を利用できません。')
        started = perf_counter()
        try:
            response = self._client.chat.completions.create(model=model,
                messages=[{'role': 'system', 'content': self.sanitize_text(system)},
                          {'role': 'user', 'content': [
                              {'type': 'video_url', 'video_url': {'url': youtube_url(url)}}]}],
                response_format={'type': 'json_schema', 'json_schema': {
                    'name': schema.__name__, 'schema': to_strict_json_schema(schema), 'strict': True}},
                extra_body={'provider': {'only': ['Google AI Studio'], 'allow_fallbacks': False, **({'zdr': True} if self._zdr else {})}})
            report_response(response, 'usage')
            text = self.sanitize_text(response.choices[0].message.content or '')
            parsed = schema.model_validate_json(text)
            return LLMResult(parsed.model_dump_json(), parsed,
                response.usage.model_dump(mode='json', exclude_none=True) if response.usage else {},
                round((perf_counter()-started)*1000, 2), model, {},
                upstream_provider=self.sanitize_text(getattr(response, 'provider', '') or ''))
        except Exception as error:
            # No video input, raw response or provider exception may enter persisted traces.
            raise provider_error(error, self.name) from None

    def generate_text_stream(self, model, system, messages, on_delta):
        system = self.sanitize_text(system)
        messages = [{"role": m["role"], "content": sanitize_content(self, m["content"])} for m in messages]
        request = {"model": model, "provider": self.name, "api": self._api, "system": system, "messages": messages, "stream": True}
        started, text, usage = perf_counter(), "", {}
        upstream_provider = None
        try:
            if self._api == "responses":
                options = {"model": model, "instructions": system, "input": messages, "store": False, "stream": True}
                if self._reasoning.get("actor"):
                    options["reasoning"] = {"effort": self._reasoning["actor"]}
                response = self._client.responses.create(**options)
            else:
                response = self._client.chat.completions.create(model=model,
                    messages=[{"role": "system", "content": system}, *messages], stream=True,
                    stream_options={"include_usage": True}, **self._chat_extra("actor"))
            stream = iter(response)
        except (openai.BadRequestError, NotImplementedError):
            # Some compatible servers accept only non-streaming completions.
            # No callback has run yet, so the endpoint can return a single reply.
            return self.generate_text(model, system, messages)
        except Exception as error:
            raise provider_error(error, self.name) from None
        try:
            while True:
                try:
                    chunk = next(stream)
                except StopIteration:
                    break
                except Exception as error:
                    raise provider_error(error, self.name) from None
                if self._api == "responses":
                    part = chunk.delta if chunk.type == "response.output_text.delta" else ""
                    if chunk.type == "response.completed" and chunk.response.usage:
                        usage = chunk.response.usage.model_dump(mode="json", exclude_none=True)
                        report_usage(usage)
                    if chunk.type in ("error", "response.failed", "response.incomplete"):
                        raise LLMError("actor のストリーミングが完了しませんでした。")
                else:
                    upstream_provider = getattr(chunk, "provider", None) or upstream_provider
                    if upstream_provider:
                        upstream_provider = self.sanitize_text(upstream_provider)
                    part = chunk.choices[0].delta.content or "" if chunk.choices else ""
                    if chunk.usage:
                        usage = chunk.usage.model_dump(mode="json", exclude_none=True)
                        report_usage(usage)
                text += part
                on_delta(part)
        finally:
            response.close()
        return LLMResult(self.sanitize_text(text), None, usage, round((perf_counter() - started) * 1000, 2), model, request,
                         upstream_provider=upstream_provider)

    def embed(self, texts, model, *, role="document"):
        try:
            result = self._client.embeddings.create(model=model, input=[self.sanitize_text(t) for t in texts],
                                                    **({"extra_body": {"provider": {"zdr": True}}} if self._zdr else {}))
            report_response(result, 'usage')
            return [list(e.embedding) for e in sorted(result.data, key=lambda e: e.index)]
        except Exception as error:
            raise provider_error(error, self.name) from None

    def _generate(self, model, system, messages, schema=None, grounded=False, include_domains=None):
        role = getattr(schema, "llm_role", "judge") if schema else "actor"
        system = self.sanitize_text(system)
        safe_messages = [{"role": m["role"], "content": sanitize_content(self, m["content"])} for m in messages]
        request = {"model": model, "provider": self.name, "api": self._api, "system": system, "messages": safe_messages,
                   "reasoning_effort": self._reasoning.get(role), "schema": schema.__name__ if schema else None}
        started = perf_counter()
        text, parsed, usage, sources = "", None, {}, []
        upstream_provider = None
        if grounded:
            if self._api == 'chat':
                request.update({k: v for k, v in self._chat_extra(role, True, include_domains)['extra_body'].items()
                                if k in ('tools', 'max_tool_calls')})
        try:
            if self._api == "responses":
                options = {"model": model, "instructions": system, "input": responses_messages(safe_messages), "store": False}
                if grounded:
                    tool = {'type': 'web_search'}
                    if include_domains is not None:
                        if not include_domains:
                            raise ValueError('Empty search domains')
                        tool['filters'] = {'allowed_domains': include_domains}
                    options.update(tools=[tool], tool_choice='required', include=['web_search_call.action.sources'])
                    request['tools'] = [tool]
                if self._reasoning.get(role):
                    options["reasoning"] = {"effort": self._reasoning[role]}
                if schema:
                    response = self._client.responses.parse(text_format=schema, **options)
                    parsed = response.output_parsed
                else:
                    response = self._client.responses.create(**options)
                text = self.sanitize_text(response.output_text or "").strip()
                usage = response.usage.model_dump(mode="json", exclude_none=True) if response.usage else {}
                report_usage(usage)
                if grounded:
                    for output in response.model_dump(mode='json').get('output', []):
                        for content in output.get('content', []):
                            for annotation in content.get('annotations', []):
                                if annotation.get('type') == 'url_citation':
                                    sources.extend(openrouter_sources([{'type': 'url_citation', 'url_citation': annotation}], self.sanitize_text))
                    sources = list({s['uri']: s for s in sources}.values())
                    if response.status != 'completed' or not sources:
                        raise ValueError('Incomplete search or missing citations')
            else:
                options = {"model": model, "messages": [{"role": "system", "content": system}, *safe_messages]}
                if schema:
                    # Strict structured output (OpenAI behind OpenRouter) rejects schemas whose optional
                    # fields are not all listed in "required"; convert the same way responses.parse does.
                    from openai.lib._pydantic import to_strict_json_schema
                    options["response_format"] = {"type": "json_schema", "json_schema": {
                        "name": schema.__name__, "schema": to_strict_json_schema(schema), "strict": True}}
                response = self._client.chat.completions.create(**options, **self._chat_extra(role, grounded, include_domains))
                upstream_provider = getattr(response, "provider", None)
                if upstream_provider:
                    upstream_provider = self.sanitize_text(upstream_provider)
                text = self.sanitize_text(response.choices[0].message.content or "").strip()
                usage = response.usage.model_dump(mode="json", exclude_none=True) if response.usage else {}
                report_usage(usage)
                if grounded:
                    message = response.choices[0].message.model_dump(mode="json")
                    sources = openrouter_sources(message.get("annotations", []), self.sanitize_text)
                    if not sources or response.choices[0].finish_reason != 'stop':
                        raise ValueError('Incomplete search or missing citations')
            if not text:
                raise ValueError("Empty response")
            if schema:
                parsed = schema.model_validate_json(text)
        except Exception as error:
            # Provider errors may include URLs/headers; never expose their text.
            result = LLMResult(text, None, usage, round((perf_counter() - started) * 1000, 2), model, request,
                               upstream_provider=upstream_provider)
            raise provider_error(error, self.name, trace=result.trace()) from None
        return LLMResult(text, parsed, usage, round((perf_counter() - started) * 1000, 2), model, request,
                         [m["content"] for m in safe_messages if m["role"] == "user"] if grounded else [], sources, upstream_provider)


def openrouter_sources(annotations, sanitize=lambda value: value):
    from urllib.parse import urlsplit
    sources = []
    for annotation in annotations or []:
        citation = annotation.get("url_citation", {}) if isinstance(annotation, dict) else {}
        url = sanitize(citation.get("url", ""))
        if annotation.get("type") == "url_citation" and urlsplit(url).scheme in ("https", "http") and url not in {s["uri"] for s in sources}:
            sources.append({"uri": url, "title": sanitize(citation.get("title") or url)})
    return sources


def responses_messages(messages):
    return [{**m, 'content': [{'type': 'input_text', 'text': p['text']} if p['type'] == 'text' else
        {'type': 'input_image', 'image_url': p['image_url']['url']} for p in m['content']]} if isinstance(m['content'], list) else m for m in messages]
