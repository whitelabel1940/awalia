from dataclasses import dataclass, field
import json
from typing import Literal, Protocol

from pydantic import BaseModel
from cbs.llm.cost import metered, report_usage
from cbs.llm.errors import LLMError
from cbs.llm.failures import observed


@dataclass
class LLMResult:
    text: str
    parsed: BaseModel | None
    usage: dict
    latency_ms: float
    model: str
    raw_request: dict
    queries: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    upstream_provider: str | None = None

    def trace(self) -> dict:
        details = self.usage.get("input_tokens_details") or self.usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
        return {
            "queries": self.queries, "sources": self.sources,
            "model": self.model, "latency_ms": self.latency_ms, "usage": self.usage,
            "upstream_provider": self.upstream_provider,
            "cached_tokens": self.usage.get("cached_content_token_count", cached),
            "request": safe_content(self.raw_request),
            "response": {"text": self.text, "parsed": self.parsed.model_dump(mode="json") if self.parsed else None},
        }


def safe_content(value):
    if isinstance(value, str) and value.startswith('data:image/'):
        return '[photo bytes omitted]'
    if isinstance(value, dict):
        if value.get('type') == 'image' and 'source' in value:
            return {'type': 'image', 'source': '[photo bytes omitted]'}
        return {k: safe_content(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_content(v) for v in value]
    return value


def sanitize_content(client, value):
    if isinstance(value, str):
        return client.sanitize_text(value)
    return [{**part, **({'text': client.sanitize_text(part['text'])} if 'text' in part else {})} for part in value]


def image_messages(text, images):
    import base64
    return [{'role': 'user', 'content': [{'type': 'text', 'text': text}, *[
        {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(b).decode('ascii')}} for b in images]]}]


class LLMClient(Protocol):
    def transcribe_audio(self, model: str, system: str, audio: bytes) -> str: ...
    def generate_images_json(self, model: str, system: str, text: str, images: list[bytes], schema: type[BaseModel]) -> LLMResult: ...
    def generate_video_json(self, model: str, system: str, url: str, schema: type[BaseModel]) -> LLMResult: ...
    def generate_text_stream(self, model: str, system: str, messages: list[dict], on_delta) -> LLMResult: ...
    def generate_grounded(self, model: str, system: str, messages: list[dict], *, include_domains=None) -> LLMResult: ...
    def embed(self, texts: list[str], model: str | None = None, *, role: Literal["query", "document"] = "document") -> list[list[float]]: ...
    def sanitize_text(self, text: str) -> str: ...
    def generate_text(self, model: str, system: str, messages: list[dict]) -> LLMResult: ...
    def generate_json(self, model: str, system: str, messages: list[dict], schema: type[BaseModel]) -> LLMResult: ...


@dataclass
class FakeLLMClient:
    """Scripted responses only. Never constructs a provider or calls a network."""
    text_responses: list[str] = field(default_factory=lambda: ["うん、わたしも気になる。"])
    json_responses: list[dict | str] = field(default_factory=lambda: [{}, {"problems": []}])
    extract_responses: list[dict | str] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    embedding_responses: list[list[list[float]]] = field(default_factory=list)
    embedding_calls: list[dict] = field(default_factory=list)
    episodizer_responses: list[dict | str] = field(default_factory=list)
    import_facts_check_responses: list[dict | str] = field(default_factory=list)
    knowing_responses: list[dict | str] = field(default_factory=list)
    linker_responses: list[dict | str] = field(default_factory=list)
    digest_responses: list[dict | str] = field(default_factory=list)
    now_responses: list[dict | str] = field(default_factory=list)
    inner_life_responses: list[dict | str] = field(default_factory=list)
    inner_update_responses: list[dict | str] = field(default_factory=list)
    proactive_responses: list[dict | str] = field(default_factory=list)
    activity_responses: list[dict | str] = field(default_factory=list)
    share_review_responses: list[dict | str] = field(default_factory=list)
    expression_responses: list[dict | str] = field(default_factory=list)
    voice_design_responses: list[dict | str] = field(default_factory=list)
    reader_review_responses: list[dict | str] = field(default_factory=list)
    note_responses: list[dict | str] = field(default_factory=list)
    activity_note_responses: list[dict | str] = field(default_factory=list)
    diary_responses: list[dict | str] = field(default_factory=list)
    monologue_responses: list[dict | str] = field(default_factory=list)
    voice_check_responses: list[dict | str] = field(default_factory=list)
    scene_responses: list[dict | str] = field(default_factory=list)
    scene_summary_responses: list[dict | str] = field(default_factory=list)
    scene_trait_responses: list[dict | str] = field(default_factory=list)
    stream_chunks: list[list[str]] = field(default_factory=list)
    grounded_responses: list[dict] = field(default_factory=list)
    vision_responses: list[dict | str] = field(default_factory=list)
    stt_responses: list[str] = field(default_factory=lambda: ['今日はいい天気ですね。'])
    usage_responses: list[dict] = field(default_factory=list)

    error_responses: list[LLMError | None] = field(default_factory=list)

    def _usage(self):
        if self.error_responses:
            error = self.error_responses.pop(0)
            if error is not None:
                raise error
        data = self.usage_responses.pop(0) if self.usage_responses else {}
        report_usage(data)
        return data

    @observed
    @metered
    def transcribe_audio(self, model, system, audio):
        # Record only the size; even fake call histories never retain audio.
        self.calls.append({'model': model, 'role': 'stt', 'system': system, 'audio_bytes': len(audio)})
        self._usage()
        return self.stt_responses.pop(0)

    @observed
    @metered
    def generate_images_json(self, model, system, text, images, schema):
        return self.generate_json(model, system, image_messages(text, images), schema)

    @observed
    @metered
    def generate_video_json(self, model, system, url, schema):
        return self.generate_json(model, system, [{'role': 'user', 'content': url}], schema)

    @observed
    @metered
    def generate_grounded(self, model, system, messages, *, include_domains=None):
        request = {"model": model, "system": system, "messages": messages, "grounded": True}
        if include_domains is not None:
            request['include_domains'] = include_domains
        self.calls.append(request)
        response = self.grounded_responses.pop(0) if self.grounded_responses else {
            "text": "白檀の香りを調べた。落ち着く香りが気になる。", "queries": ["白檀 香り"],
            "sources": [{"title": "Fake source", "uri": "https://example.org/incense"}]}
        return LLMResult(response['text'], None, self._usage(), 0, model, request, response.get('queries', []), response.get('sources', []))

    @observed
    @metered
    def embed(self, texts, model=None, *, role="document"):
        self._usage()
        self.embedding_calls.append({"texts": texts, "model": model, "role": role})
        return self.embedding_responses.pop(0) if self.embedding_responses else [[1.0, 0.0] for _ in texts]

    def sanitize_text(self, text: str) -> str:
        return text

    @observed
    @metered
    def generate_text(self, model, system, messages):
        request = {"model": model, "system": system, "messages": messages}
        self.calls.append(request)
        return LLMResult(self.text_responses.pop(0), None, self._usage(), 0, model, request)

    @observed
    @metered
    def generate_text_stream(self, model, system, messages, on_delta):
        if not self.stream_chunks:
            result = self.generate_text(model, system, messages)
            on_delta(result.text)
            return result
        request = {"model": model, "system": system, "messages": messages}
        self.calls.append(request)
        chunks = self.stream_chunks.pop(0)
        for chunk in chunks:
            on_delta(chunk)
        return LLMResult("".join(chunks), None, self._usage(), 0, model, request)

    @observed
    @metered
    def generate_json(self, model, system, messages, schema):
        request = {"model": model, "system": system, "messages": messages, "schema": schema.model_json_schema()}
        self.calls.append(request)
        if schema.__name__ == 'PhotoDescriptions':
            response = self.vision_responses.pop(0) if self.vision_responses else {'descriptions': ['見えたこと: 白い皿にごはん。推測: 昼食かもしれない。'] * (len(messages[0]['content']) - 1)}
        elif schema.__name__ == 'BirthdayInitial':
            response = self.birthday_initial_responses.pop(0) if getattr(self, 'birthday_initial_responses', []) else {'birthday': None}
        elif schema.__name__ == 'ReaderReview':
            prior = json.loads(messages[-1]['content'])['reader']
            response = self.reader_review_responses.pop(0) if self.reader_review_responses else {
                'voice': prior.get('voice', {}), 'missing_items': [],
                'conflicts': prior.get('conflicts', []), 'setting_conflicts': prior.get('setting_conflicts', [])}
        elif schema.__name__ == 'VoiceDesign':
            response = self.voice_design_responses.pop(0) if self.voice_design_responses else {'description': '柔らかく明るい声。落ち着いた速さで自然に話す。'}
        elif schema.__name__ == "Expression":
            response = self.expression_responses.pop(0) if self.expression_responses else {"expression": "neutral"}
        elif schema.__name__ == 'DiaryEntry':
            response = self.diary_responses.pop(0) if self.diary_responses else {'summary':'日々のことを話した。','partner_comment':'また話せてうれしかったよ。'}
        elif schema.__name__ == 'Monologue':
            response = self.monologue_responses.pop(0) if self.monologue_responses else {'text': '少しゆっくり考えていたい。'}
        elif schema.__name__ == 'VoiceCheck':
            response = self.voice_check_responses.pop(0) if self.voice_check_responses else {'problems': []}
        elif schema.__name__ == 'SceneEdits':
            response = self.scene_responses.pop(0) if self.scene_responses else {'operations': [
                {'action': 'add', 'scene_id': None, 'text': '好意を伝えられると、照れながら理由を聞くみたい。'}]}
            if isinstance(response, dict) and 'caption' not in response:
                response = {**response, 'caption': 'あなたに好意を伝えられて、照れながら理由を聞いた。'}
        elif schema.__name__ == 'SceneSummary':
            response = self.scene_summary_responses.pop(0) if self.scene_summary_responses else {'text': '親しい相手からの好意には、照れながら向き合うみたい。'}
        elif schema.__name__ == 'SceneTrait':
            context = json.loads(messages[0]['content'])
            response = self.scene_trait_responses.pop(0) if self.scene_trait_responses else {
                'text': '相手の気持ちを丁寧に聞こうとする人みたい。', 'scene_ids': [context['scenes'][0]['id']]}
        elif schema.__name__ == "ActivityNote":
            response = self.activity_note_responses.pop(0) if self.activity_note_responses else {
                "title": "調べたこと", "summary": "調べたことのまとめ。", "impression": "面白いと感じたようだ。"}
        elif schema.__name__ in ("ConnectionCheck", "StewardConnectionCheck"):
            response = {"ok": True}
        elif schema.__name__ == "PastNote":
            response = self.note_responses.pop(0) if self.note_responses else {"text": "昔のことを話してくれた。"}
        elif schema.__name__ == 'InnerUpdate':
            response = self.inner_update_responses.pop(0) if self.inner_update_responses else {
                'feeling': {'sentence': '穏やかな気持ち。', 'levels': {'energy': 'ふつう', 'social': 'ふつう', 'concern': 'なし'}},
                'wants': [], 'current_expression': 'unchanged'}
        elif schema.__name__ == "InnerLife":
            response = self.inner_life_responses.pop(0) if self.inner_life_responses else {"action": "none", "reason": "今は何もしない"}
        elif schema.__name__ == "Proactive":
            response = self.proactive_responses.pop(0) if self.proactive_responses else {"speak": False, "reason": "静かにしておく"}
        elif schema.__name__ == "ActivityUpdate":
            response = self.activity_responses.pop(0) if self.activity_responses else {"interests": []}
        elif schema.__name__ == 'ShareReview':
            response = self.share_review_responses.pop(0) if self.share_review_responses else {'operation': 'new'}
        elif schema.__name__ == "Extract":
            response = self.extract_responses.pop(0) if self.extract_responses else {
                "ledger_updates": [], "new_canon": [], "disclosures": [], "used_memories": []}
        elif schema.__name__ == 'ImportReflection':
            response = self.import_responses.pop(0) if getattr(self,'import_responses',[]) else {'turns':[
                {'turn_id':t['id'],'extract':{'ledger_updates':[],'new_canon':[],'disclosures':[],'used_memories':[]},'emotional_weight':'low','user_disclosure':'no'}
                for t in json.loads(messages[-1]['content'])['turns']]}
        elif schema.__name__ == "EpisodizerOutput":
            response = self.episodizer_responses.pop(0) if self.episodizer_responses else {
                "episodes": [{"summary": "テスト会話", "details": "ユーザーとパートナーのテスト会話", "importance": 0.5, "pinned": False}], "user_facts": []}
        elif schema.__name__ == 'Understandings':
            response = self.knowing_responses.pop(0) if getattr(self, 'knowing_responses', []) else {'understandings': []}
        elif schema.__name__ == "NowSummary":
            response = self.now_responses.pop(0) if self.now_responses else {"summary": "作者設定の関心を持つパートナー。"}
        elif schema.__name__ == "EpisodeLinks":
            response = self.linker_responses.pop(0) if self.linker_responses else {"links": []}
        elif schema.__name__ == 'ImportFactsCheck':
            response = self.import_facts_check_responses.pop(0) if self.import_facts_check_responses else {
                'user_facts': json.loads(messages[-1]['content'])['proposed_facts']}
        elif schema.__name__ == "MonthlyDigest":
            response = self.digest_responses.pop(0) if self.digest_responses else {"summary": "月のテスト会話", "details": "出来事: テスト会話をした。\nユーザー: テストの事情を聞いた。\nやりとり: パートナーが応じた。"}
        else:
            response = self.json_responses.pop(0)
        text = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        try:
            parsed = schema.model_validate_json(text)
        except ValueError:
            result = LLMResult(text, None, self._usage(), 0, model, request)
            raise LLMError("Judge の JSON がスキーマに一致しません。", trace=result.trace()) from None
        return LLMResult(parsed.model_dump_json(), parsed, self._usage(), 0, model, request)


class RoutingLLMClient:
    """Routes "<provider>/<model>" to the configured provider; a bare model name uses [llm].provider."""

    def __init__(self, config, *, credentials=None):
        self._config = config
        self._credentials = credentials or {}
        self._clients = {}
        self._local_embedding = None

    def _split(self, model):
        name, _, rest = model.partition("/")
        if rest and (name in self._config.llm.providers or name in (self._config.llm.provider, "gemini", "local")):
            return name, rest
        return self._config.llm.provider, model

    def _client(self, name):
        if name not in self._clients:
            if name == "gemini" and name not in self._config.llm.providers:
                from cbs.llm.gemini import GeminiLLMClient
                self._clients[name] = GeminiLLMClient(self._config, **({'api_key': self._credentials[name]} if name in self._credentials else {}))
            else:
                provider = self._config.llm.providers.get(name)
                if provider is None:
                    raise ValueError(f"Unsupported LLM provider: {name}")
                if name == 'anthropic':
                    from cbs.llm.anthropic import AnthropicLLMClient
                    self._clients[name] = AnthropicLLMClient(provider, self._config.llm.tls_strict, **({'api_key': self._credentials[name]} if name in self._credentials else {}))
                else:
                    from cbs.llm.openai_client import OpenAILLMClient
                    self._clients[name] = OpenAILLMClient(name, provider, self._config.llm.tls_strict, self._config.llm.zdr_only, **({'api_key': self._credentials[name]} if name in self._credentials else {}))
        return self._clients[name]

    def sanitize_text(self, text):
        for value in self._credentials.values():
            if value:
                text = text.replace(value, "[REDACTED]")
        import os
        from cbs.config import key_env
        for name in {"gemini", self._config.llm.provider, *self._config.llm.providers}:
            key = os.environ.get(key_env(self._config, name) or "", "")
            if key:
                text = text.replace(key, "[REDACTED]")
        for client in self._clients.values():
            text = client.sanitize_text(text)
        return text

    @observed
    @metered
    def generate_grounded(self, model, system, messages, *, include_domains=None):
        name, bare = self._split(model)
        if name not in ('openrouter', 'anthropic', 'openai') and (name != "gemini" or name in self._config.llm.providers):
            raise LLMError("Search grounding is not supported by this provider.")
        return self._client(name).generate_grounded(bare, system, messages, **({'include_domains': include_domains} if include_domains is not None else {}))

    @observed
    @metered
    def generate_text(self, model, system, messages):
        name, bare = self._split(model)
        return self._actor_client(name, model).generate_text(bare, system, messages)

    def _actor_client(self, name, model):
        candidate = next((c for c in self._config.simple.actor_candidates if c.model == model), None)
        effort = candidate.reasoning_effort if candidate else None
        if (self._config.llm.mode != 'simple' or model != self._config.models.actor
                or effort is None or name not in self._config.llm.providers):
            return self._client(name)
        key = (name, 'actor', effort)
        if key not in self._clients:
            # Keep the shared judge/steward client and provider defaults intact.
            provider = self._config.llm.providers[name].model_copy(deep=True)
            provider.reasoning_effort['actor'] = effort
            from cbs.llm.openai_client import OpenAILLMClient
            self._clients[key] = OpenAILLMClient(name, provider, self._config.llm.tls_strict, self._config.llm.zdr_only, **({'api_key': self._credentials[name]} if name in self._credentials else {}))
        return self._clients[key]

    @observed
    @metered
    def generate_text_stream(self, model, system, messages, on_delta):
        name, bare = self._split(model)
        client = self._actor_client(name, model)
        if callable(getattr(client, "generate_text_stream", None)):
            return client.generate_text_stream(bare, system, messages, on_delta)
        return client.generate_text(bare, system, messages)

    @observed
    @metered
    def generate_json(self, model, system, messages, schema):
        name, bare = self._split(model)
        client = self._actor_client(name, model) if getattr(schema, 'llm_role', None) == 'actor' else self._client(name)
        return client.generate_json(bare, system, messages, schema)

    @observed
    @metered
    def generate_images_json(self, model, system, text, images, schema):
        name, bare = self._split(model)
        return self._client(name).generate_images_json(bare, system, text, images, schema)

    @observed
    @metered
    def transcribe_audio(self, model, system, audio):
        name, bare = self._split(model)
        if name not in {'gemini', 'openai', 'openrouter'}:
            raise LLMError('このサービスでは音声入力を使えません。')
        return self._client(name).transcribe_audio(bare, system, audio)

    @observed
    @metered
    def generate_video_json(self, model, system, url, schema):
        name, bare = self._split(model)
        if name not in {'gemini', 'openrouter'}:
            raise LLMError('この設定では動画入力を利用できません。')
        return self._client(name).generate_video_json(bare, system, url, schema)

    @observed
    @metered
    def embed(self, texts, model=None, *, role="document"):
        name, bare = self._split(model or self._config.models.embedding)
        if role not in ("query", "document"):
            raise ValueError("embedding role must be query or document")
        if name == "local":
            if self._local_embedding is None:
                from cbs.llm.local import LocalEmbeddingClient
                self._local_embedding = LocalEmbeddingClient(self._config)
            client = self._local_embedding
        else:
            client = self._client(name)
        return client.embed([self.sanitize_text(t) for t in texts], bare, role=role)


def create_client(config) -> LLMClient:
    # Credentials for unused/offline providers need not be present at startup.
    # sanitize_text covers all configured keys even before constructing a client.
    client = RoutingLLMClient(config)
    client._cost_default = True
    return client
