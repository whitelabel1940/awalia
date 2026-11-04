from cbs.llm.cost import report_usage, report_response
import json
import os
import ssl
from time import perf_counter

from google import genai
from google.genai import types

from cbs.llm.errors import provider_error
from cbs.llm import LLMError, LLMResult, image_messages, sanitize_content


class GeminiLLMClient:
    def __init__(self, config, *, api_key=None, retry_attempts=None):
        self._key = api_key if api_key is not None else os.environ.get(config.llm.api_key_env, "")
        if not self._key:
            raise LLMError("API キーを設定してください。", kind="auth", provider="gemini")
        tls_context = ssl.create_default_context()
        if not config.llm.tls_strict:
            # Python 3.14's strict X.509 CA-format check rejects this environment's
            # issuer. Chain validation and hostname verification remain enabled.
            tls_context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self._client = genai.Client(api_key=self._key, http_options=types.HttpOptions(
            timeout=120_000, client_args={"verify": tls_context},
            retry_options=types.HttpRetryOptions(attempts=retry_attempts) if retry_attempts is not None else None,
        ))
        self._thinking = config.llm.thinking

    def sanitize_text(self, text):
        return text.replace(self._key, "[REDACTED]")

    def generate_images_json(self, model, system, text, images, schema):
        return self._generate(model, system, image_messages(text, images), schema)

    def generate_text(self, model, system, messages):
        return self._generate(model, system, messages)

    def transcribe_audio(self, model, system, audio):
        try:
            options = {'system_instruction': self.sanitize_text(system),
                       'automatic_function_calling': types.AutomaticFunctionCallingConfig(disable=True)}
            if self._thinking.get('stt'):
                options['thinking_config'] = types.ThinkingConfig(**self._thinking['stt'])
            result = self._client.models.generate_content(model=model,
                contents=[types.Content(role='user', parts=[types.Part.from_bytes(data=audio, mime_type='audio/wav')])],
                config=types.GenerateContentConfig(**options))
            report_response(result, 'usage_metadata')
            return self.sanitize_text(result.text or '')
        except Exception as error:
            raise provider_error(error, 'gemini') from None

    def generate_json(self, model, system, messages, schema):
        return self._generate(model, system, messages, schema)

    def generate_video_json(self, model, system, url, schema, *, max_output_tokens=None):
        from cbs.media import youtube_url
        url = youtube_url(url)
        if not url:
            raise LLMError('YouTube の動画URLではありません。')
        options = dict(system_instruction=self.sanitize_text(system),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            response_mime_type='application/json', response_json_schema=schema.model_json_schema())
        if self._thinking.get('steward'):
            options['thinking_config'] = types.ThinkingConfig(**self._thinking['steward'])
        if max_output_tokens is not None:
            options['max_output_tokens'] = max_output_tokens
        started = perf_counter()
        try:
            response = self._client.models.generate_content(model=model,
                contents=[types.Content(role='user', parts=[types.Part(file_data=types.FileData(file_uri=url))])],
                config=types.GenerateContentConfig(**options))
            report_response(response, 'usage_metadata')
            parsed = schema.model_validate_json(self.sanitize_text(response.text or ''))
            usage = response.usage_metadata.model_dump(mode='json', exclude_none=True) if response.usage_metadata else {}
            return LLMResult(parsed.model_dump_json(), parsed, usage, round((perf_counter()-started)*1000, 2), model, {})
        except Exception as error:
            # Never retain video input, raw response or provider exception text.
            raise provider_error(error, 'gemini') from None

    def generate_text_stream(self, model, system, messages, on_delta):
        system = self.sanitize_text(system)
        safe_messages = [{"role": m["role"], "content": sanitize_content(self, m["content"])} for m in messages]
        contents = [types.Content(role="model" if m["role"] == "assistant" else "user",
                                 parts=gemini_parts(m["content"])) for m in safe_messages]
        options = {"system_instruction": system, "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True)}
        if self._thinking.get("actor"):
            options["thinking_config"] = types.ThinkingConfig(**self._thinking["actor"])
        request = {"model": model, "system": system, "messages": safe_messages, "stream": True}
        started, text, usage = perf_counter(), "", {}
        try:
            stream = iter(self._client.models.generate_content_stream(model=model, contents=contents, config=types.GenerateContentConfig(**options)))
        except Exception as error:
            raise provider_error(error, 'gemini') from None
        try:
            while True:
                try:
                    chunk = next(stream)
                except StopIteration:
                    break
                except Exception as error:
                    raise provider_error(error, 'gemini') from None
                part = chunk.text or ""
                text += part
                if chunk.usage_metadata:
                    usage = chunk.usage_metadata.model_dump(mode="json", exclude_none=True)
                    report_usage(usage)
                on_delta(part)
        finally:
            if hasattr(stream, "close"):
                stream.close()
        return LLMResult(self.sanitize_text(text), None, usage, round((perf_counter() - started) * 1000, 2), model, request)

    def generate_grounded(self, model, system, messages, *, include_domains=None):
        # Native Google Search cannot guarantee domain restriction. Never fall
        # back to a prompt-only site: instruction for a restricted activity.
        if include_domains is not None:
            raise LLMError('この検索サービスはドメイン制限に対応していません。OpenRouterを使ってください。')
        return self._generate(model, system, messages, grounded=True)

    def embed(self, texts, model="gemini-embedding-2", *, role="document"):
        try:
            # Content entries are separate examples; bare strings become one multimodal input.
            contents = [types.Content(parts=[types.Part(text=self.sanitize_text(t))]) for t in texts]
            result = self._client.models.embed_content(model=model, contents=contents)
            report_response(result, 'metadata')
            return [list(e.values) for e in result.embeddings]
        except Exception as error:
            raise provider_error(error, 'gemini') from None

    def _generate(self, model, system, messages, schema=None, grounded=False):
        role = "steward" if grounded else (getattr(schema, "llm_role", "judge") if schema else "actor")
        options = {"system_instruction": self.sanitize_text(system),
                   "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True)}
        if self._thinking.get(role):
            options["thinking_config"] = types.ThinkingConfig(**self._thinking[role])
        if grounded:
            options["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        if schema:
            options.update(response_mime_type="application/json", response_json_schema=schema.model_json_schema())
        safe_messages = [{"role": m["role"], "content": sanitize_content(self, m["content"])} for m in messages]
        contents = [types.Content(role="model" if m["role"] == "assistant" else "user", parts=gemini_parts(m["content"])) for m in safe_messages]
        request = {"model": model, "system": options["system_instruction"], "messages": safe_messages,
                   "config": types.GenerateContentConfig(**options).model_dump(mode="json", exclude_none=True)}
        safe_request = json.loads(self.sanitize_text(json.dumps(request, ensure_ascii=False)))
        started = perf_counter()
        text, parsed, usage = "", None, {}
        try:
            response = self._client.models.generate_content(model=model, contents=contents, config=types.GenerateContentConfig(**options))
            text = self.sanitize_text(response.text or "").strip()
            usage = response.usage_metadata.model_dump(mode="json", exclude_none=True) if response.usage_metadata else {}
            report_usage(usage)
            if not text:
                raise ValueError("Empty response")
            parsed = schema.model_validate_json(text) if schema else None
        except Exception as error:
            # Provider errors may include URLs/headers; never expose their text.
            result = LLMResult(text, parsed, usage, round((perf_counter() - started) * 1000, 2), model, safe_request)
            raise provider_error(error, 'gemini', trace=result.trace()) from None
        result = LLMResult(text, parsed, usage, round((perf_counter() - started) * 1000, 2), model, safe_request)
        if grounded and response.candidates:
            metadata = response.candidates[0].grounding_metadata
            if metadata:
                result.queries = [self.sanitize_text(q) for q in metadata.web_search_queries or []]
                result.sources = [{"title": self.sanitize_text(c.web.title or ""), "uri": self.sanitize_text(c.web.uri or "")}
                                  for c in metadata.grounding_chunks or [] if c.web]
        return result


def gemini_parts(content):
    import base64
    if isinstance(content, str):
        return [types.Part(text=content)]
    return [types.Part(text=p['text']) if p['type'] == 'text' else types.Part.from_bytes(
        data=base64.b64decode(p['image_url']['url'].split(',', 1)[1]), mime_type='image/jpeg') for p in content]
