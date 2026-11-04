from pathlib import Path
import os
import json
import re
from typing import Literal
import tomllib

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from cbs.paths import ROOT, resolve_paths

SIMPLE_MODELS = {
    "actor": "openrouter/google/gemini-3.8-flash",
    "judge": "openrouter/openai/gpt-6-luna",
    "steward": "openrouter/openai/gpt-6-luna",
    "embedding": "openrouter/google/gemini-embedding-2",
    "research": "openrouter/google/gemini-3.8-flash",
    "vision": "openrouter/google/gemini-3.8-flash",
    "stt": "openrouter/google/gemini-3.8-flash",
}


class ProviderConfig(BaseModel):
    # api="responses" for OpenAI; "chat" for OpenAI-compatible local servers (Ollama, LM Studio, llama.cpp).
    api: Literal["responses", "chat", "messages"] = "responses"
    api_key_env: str | None = None
    base_url: str | None = None
    # Reasoning effort keyed by role (actor / judge / steward); no model-name branching.
    reasoning_effort: dict[str, str] = Field(default_factory=dict)


class LLMConfig(BaseModel):
    mode: Literal["simple", "advanced"] = "advanced"
    # Runtime copy; persisted advanced choices stay in [models].
    advanced_models: dict = Field(default_factory=dict, exclude=True)
    # Default provider for bare model names. "gemini" uses api_key_env / thinking below.
    provider: str = "gemini"
    api_key_env: str
    tls_strict: bool = True
    # Gemini SDK ThinkingConfig options, keyed by role; no model-name branching.
    thinking: dict[str, dict] = Field(default_factory=dict)
    # Additional providers, referenced from [models] as "<name>/<model>".
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    # OpenRouter only: route every request to endpoints that do not retain prompts (Zero Data Retention).
    zdr_only: bool = False


class Models(BaseModel):
    actor: str
    judge: str
    steward: str
    embedding: str = "gemini-embedding-2"
    research: str | None = None
    vision: str | None = None
    stt: str | None = None


class ModelCandidate(BaseModel):
    model: str
    label: str
    description: str
    input_usd: float = Field(ge=0)
    output_usd: float = Field(ge=0)
    status: Literal["pending", "passed", "failed"] = "pending"
    evidence: str = ""
    vision: bool = False
    # Optional per-candidate override; absent preserves the provider's actor default.
    reasoning_effort: Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] | None = None


class SimpleConfig(BaseModel):
    stt: str | None = None
    models: dict[str, str] = Field(default_factory=dict)
    actor: str | None = None
    judge: str | None = None
    actor_candidates: list[ModelCandidate] = Field(default_factory=list)
    judge_candidates: list[ModelCandidate] = Field(default_factory=list)

    @field_validator("actor", "judge")
    @classmethod
    def openrouter_model(cls, value):
        if value is not None and (len(value) > 256 or not re.fullmatch(r"openrouter/[a-zA-Z0-9][a-zA-Z0-9._:/-]*", value)):
            raise ValueError("OpenRouter のモデル名を確認してください。")
        return value


class OnboardingChoice(BaseModel):
    model: str
    label: str
    description: str = ""


class OnboardingConfig(BaseModel):
    actor_choices: dict[str, list[OnboardingChoice]] = Field(default_factory=dict)


class AdvancedPreset(BaseModel):
    id: str
    label: str
    description: str = ""
    models: Models


class AdvancedConfig(BaseModel):
    presets: list[AdvancedPreset] = Field(default_factory=list)


class MemoryConfig(BaseModel):
    candidate_max: int = Field(default=32, ge=1, le=256)
    candidate_min_per_kind: dict[str, int] = Field(default_factory=lambda: {
        'memory': 4, 'fact': 4, 'canon': 2, 'plan': 2, 'episode': 2,
        'thread': 2, 'digest': 2, 'interest': 2, 'understanding': 2})

    @model_validator(mode='after')
    def candidate_limits(self):
        allowed = {'memory','fact','canon','plan','episode','thread','digest','interest','understanding'}
        if set(self.candidate_min_per_kind) - allowed or any(v < 0 for v in self.candidate_min_per_kind.values()):
            raise ValueError('candidate minima must be nonnegative counts of known kinds')
        if sum(self.candidate_min_per_kind.values()) > self.candidate_max:
            raise ValueError('candidate minima must fit candidate_max')
        return self

    interest_fade_days: int = Field(default=45, gt=0)
    # Current-self items become established after this many distinct days spanning at least this many days.
    interest_established_min_days: int = Field(default=3, ge=1)
    interest_established_min_span_days: int = Field(default=7, ge=0)
    gap_minutes: int = Field(default=30, gt=0)
    day_boundary_hour: int = Field(default=4, ge=0, le=23)
    max_exchanges: int = Field(default=40, gt=0)
    digest_age_days: int = Field(default=60, ge=0)
    digest_strength_threshold: float = Field(default=0.2, ge=0)
    archived_margin: float = Field(default=0.08, ge=0, le=1)
    archived_margins: dict[str, float] = Field(default_factory=dict)

    @field_validator('archived_margins')
    @classmethod
    def valid_margins(cls, values):
        import math
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in values.values()):
            raise ValueError('archived_margins must be finite values between 0 and 1')
        return values


class ToolConfig(BaseModel):
    model_config = {'extra': 'forbid'}
    enabled: bool = True
    daily_max: int = Field(default=3, ge=0, le=100)


class ShoppingToolConfig(ToolConfig):
    daily_max: int = Field(default=1, ge=0, le=100)
    domains: list[str] = Field(default_factory=lambda: ['amazon.co.jp', 'rakuten.co.jp', 'makuake.com', 'camp-fire.jp', 'readyfor.jp'], max_length=50)

    @field_validator('domains')
    @classmethod
    def valid_domains(cls, values):
        if any(len(v) > 253 or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}', v) for v in values):
            raise ValueError('対象サイトは小文字のドメイン名で指定してください。')
        return list(dict.fromkeys(values))


class AutonomyTools(BaseModel):
    model_config = {'extra': 'forbid'}
    research: ToolConfig = Field(default_factory=ToolConfig)
    read_source: ToolConfig = Field(default_factory=ToolConfig)
    read_book: ToolConfig = Field(default_factory=lambda: ToolConfig(daily_max=2))
    watch_video: 'VideoToolConfig' = Field(default_factory=lambda: VideoToolConfig())
    world_activity: ToolConfig = Field(default_factory=lambda: ToolConfig(daily_max=2))
    read_wikipedia: ToolConfig = Field(default_factory=ToolConfig)
    browse_shopping: ShoppingToolConfig = Field(default_factory=ShoppingToolConfig)


class VideoToolConfig(ToolConfig):
    daily_max: int = Field(default=1, ge=0, le=100)
    max_minutes: int = Field(default=20, ge=1, le=120)
    model: str = 'openrouter/google/gemini-3.8-flash'


class IdlePace(BaseModel):
    minutes: int = Field(gt=0)
    daily_max: int = Field(ge=0, le=50)


class AutonomyConfig(BaseModel):
    source_refresh_hours: float = Field(default=24, ge=24)
    inner_life: bool = True
    # Existing settings remain effective until the new tool settings are saved.
    research_daily_max: int | None = Field(default=None, ge=0, le=100)
    tools: AutonomyTools = Field(default_factory=AutonomyTools)
    activities_daily_max: int = Field(default=6, ge=0, le=100)
    # 話しかけの頻度。opening の扱いは many / normal が同じ。idle の間と上限は idle_pace。
    opening: Literal["many", "normal", "reserved", "off"] = "normal"
    reserved_gap_hours: float = Field(default=24, gt=0)
    inner_life_interval_hours: float = Field(default=4, gt=0)
    research: bool = True
    research_candidate_expiry_days: int = Field(default=14, gt=0)
    research_candidate_max: int = Field(default=12, ge=0, le=100)
    thread_expiry_days: int = Field(default=14, gt=0)
    proactive_history_days: int = Field(default=3, gt=0, le=30)
    idle_nudge: bool = False
    idle_poll_seconds: int = Field(default=180, ge=60, le=3600)
    resident_opening_delay_seconds: int = Field(default=90, ge=0, le=3600)
    resident_expression_neutral_minutes: float = Field(default=10, gt=0, le=1440)
    idle_minutes: int = Field(default=60, gt=0)  # 旧設定。idle の間は idle_pace を使う
    idle_pace: dict[str, IdlePace] = Field(default_factory=lambda: {
        'many': IdlePace(minutes=20, daily_max=6), 'normal': IdlePace(minutes=30, daily_max=4),
        'reserved': IdlePace(minutes=90, daily_max=2)})
    idle_recheck_multiplier: float = Field(default=3, ge=1, le=100)
    idle_recheck_growth: float = Field(default=2, ge=1, le=10)
    idle_min_weight: float = Field(default=0.7, ge=0, le=1)  # 旧設定。idle は重みで絞らない

    def idle_rule(self):
        """(minutes, daily_max) for the chosen frequency; None when it is off."""
        pace = self.idle_pace.get(self.opening)
        return (pace.minutes, pace.daily_max) if pace else None
    quiet_start_hour: int = Field(default=0, ge=0, le=23)
    quiet_end_hour: int = Field(default=8, ge=0, le=23)


class EarlyConfig(BaseModel):
    notify: bool = True
    days: int = Field(default=3, ge=0, le=30)
    daily_max: int = Field(default=1, ge=0, le=10)
    total_max: int = Field(default=3, ge=0, le=30)


class InnerConfig(BaseModel):
    baseline_hours: float = Field(default=12, gt=0)
    want_decay_per_day: float = Field(default=0.1, ge=0, le=1)
    long_gap_hours: float = Field(default=24, gt=0)
    show_monologue: bool = True


class PrivateCategory(BaseModel):
    model_config = {'extra': 'forbid'}
    label: str
    allow_mention: bool = False


def private_categories():
    # The catalog and defaults have one source, including for standalone Stores.
    with (ROOT / 'config.toml').open('rb') as stream:
        return tomllib.load(stream)['knowing']['private_categories']


class KnowingConfig(BaseModel):
    private_categories: dict[str, PrivateCategory] = Field(default_factory=private_categories, validate_default=True)
    candidate_max: int = Field(default=6, ge=0, le=12)
    observation_max: int = Field(default=120, ge=1, le=500)
    probable_min_days: int = Field(default=2, ge=2)
    known_min_days: int = Field(default=3, ge=3)
    known_min_span_days: int = Field(default=14, ge=1)
    situation_days: int = Field(default=10, ge=1)
    fade_days: int = Field(default=90, ge=1)
    questions_daily_max: int = Field(default=1, ge=0, le=1)
    question_gap_days: int = Field(default=7, ge=7)

    @field_validator('private_categories')
    @classmethod
    def fallback_category(cls, value):
        if 'other' not in value or value['other'].allow_mention:
            raise ValueError('private_categories.other must default to no mention')
        return value


class PipelineConfig(BaseModel):
    post_check: Literal["sync", "async"] = "sync"
    background_wait_seconds: float = Field(default=30, gt=0, le=120)
    # Welcome-flow calls that sometimes stall on the provider side (30 s usually, 4-5 min rarely):
    # after this many seconds the same request is sent once more and the first answer wins. 0 = off.
    bootstrap_hedge_seconds: float = Field(default=90, ge=0, le=600)
    bootstrap_hedge_prompts: list[str] = Field(default_factory=lambda: ['bootstrap_builder'])


class CharacterConfig(BaseModel):
    id: str = ""


class PersonaConfig(BaseModel):
    model_config = {'extra': 'forbid'}
    open_style: Literal["hinted", "free"] | None = None
    # Legacy global value is retained only as a fallback for existing partners.
    ai_question: Literal["immersive", "playful", "honest"] = "immersive"


class VoiceStyle(BaseModel):
    model_config = {'extra': 'forbid'}
    mode: Literal['emoji', 'instructions', 'none'] = 'none'
    values: dict[str, str] = Field(default_factory=dict)


class TTSConfig(BaseModel):
    model_config = {'extra': 'forbid'}
    enabled: bool = False
    autoplay: bool = True
    autoplay_proactive: bool = False  # Speak-ups arrive unannounced (e.g. during a meeting): show them, read only on request.
    engine: Literal['speech.cpp', 'external'] = 'speech.cpp'
    base_url: str = 'http://127.0.0.1:8080/v1'
    model: str = ''  # External servers only.
    local_model: Literal['auto', 'q8_0', 'q4_k'] = 'auto'
    device: Literal['auto', 'gpu', 'cpu'] = 'auto'
    duration_scale: float = Field(default=0.8, ge=0.5, le=2.0, allow_inf_nan=False)
    cache_days: int = Field(default=7, ge=0, le=365)
    styles: dict[str, VoiceStyle] = Field(default_factory=lambda: {
        'speech.cpp': VoiceStyle(mode='emoji', values={
            'neutral': '', 'smile': '😊', 'shy': '😳', 'surprised': '😮',
            'sad': '😢', 'troubled': '😟', 'thinking': '🤔', 'pout': '😠'}),
        'external': VoiceStyle()})

    @field_validator('base_url')
    @classmethod
    def valid_url(cls, value):
        from urllib.parse import urlsplit
        url = urlsplit(value)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('音声サーバーのURLを確認してください。')
        return value.rstrip('/')

    @field_validator('styles')
    @classmethod
    def valid_styles(cls, value):
        if set(value) - {'speech.cpp', 'external'} or value.get('external', VoiceStyle()).mode == 'emoji':
            raise ValueError('外部エンジンには指示文または調子なしを指定してください。')
        for style in value.values():
            if set(style.values) - {'neutral', 'smile', 'shy', 'surprised', 'sad', 'troubled', 'thinking', 'pout'} or any(len(v) > 500 for v in style.values.values()):
                raise ValueError('声の調子の対応表を確認してください。')
            if style.mode == 'emoji' and any(len(v.encode('utf-8')) > 24 for v in style.values.values()):
                raise ValueError('文頭の声の調子は短い絵文字にしてください。')
        return value


class DaysConfig(BaseModel):
    nearby_days: int = Field(default=14, ge=0, le=366)
    hundred_days: bool = True
    yearly: bool = True
    habit_window_minutes: int = Field(default=15, ge=0, le=60)


class RequestsConfig(BaseModel):
    max_lines: int = Field(default=40, ge=1, le=200)
    max_line_chars: int = Field(default=300, ge=1, le=2000)
    max_field_chars: int = Field(default=1000, ge=1, le=4000)


class PhotoConfig(BaseModel):
    max_count: int = Field(default=4, ge=1, le=12)
    max_edge: int = Field(default=1600, ge=128, le=4096)
    max_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    max_pixels: int = Field(default=40_000_000, ge=1, le=100_000_000)
    # Capability catalog; unknown models fail closed. No model-name heuristics.
    candidates: dict[str, bool] = Field(default_factory=dict)


class STTLocalModel(BaseModel):
    catalog: str
    size: int = Field(gt=0)


class STTConfig(BaseModel):
    model_config = {'extra': 'forbid'}
    auto_send: bool = False
    max_seconds: int = Field(default=60, ge=1, le=120)
    silence_seconds: float = Field(default=2, ge=.5, le=10)
    silence_rms: float = Field(default=.015, gt=0, le=.2)
    device: Literal['auto', 'cpu', 'gpu'] = 'auto'
    # Audio input capability catalog, independent of model names and transport.
    candidates: dict[str, bool] = Field(default_factory=dict)
    local_models: dict[str, STTLocalModel] = Field(default_factory=dict)


class BackupConfig(BaseModel):
    model_config = {'extra': 'forbid'}
    daily_keep: int = Field(default=7, ge=1, le=90)
    weekly_keep: int = Field(default=4, ge=0, le=52)
    export_keep: int = Field(default=4, ge=1, le=100)
    folder: str = Field(default='', max_length=2048)
    frequency: Literal['off', 'daily', 'weekly'] = 'off'
    restore_max_bytes: int = Field(default=2 * 1024**3, ge=1024, le=32 * 1024**3)
    restore_max_expanded_bytes: int = Field(default=8 * 1024**3, ge=1024, le=128 * 1024**3)


class CostPrice(BaseModel):
    input_usd: float = Field(ge=0, allow_inf_nan=False)
    output_usd: float = Field(ge=0, allow_inf_nan=False)
    cache_read_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cache_write_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class CostsConfig(BaseModel):
    monthly_guide_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    pause_autonomy: bool = False
    detail_days: int = Field(default=90, ge=1)
    prices: dict[str, CostPrice] = Field(default_factory=dict)


class FirstRunConfig(BaseModel):
    keep: int = Field(default=5, ge=1, le=100)


class DisplayConfig(BaseModel):
    split_replies: bool = True
    delay_user: bool = False
    read_seconds: float = Field(default=1.2, ge=0, le=30, allow_inf_nan=False)
    typing_seconds: float = Field(default=2.5, ge=0, le=30, allow_inf_nan=False)
    user_delay_seconds: float = Field(default=1.0, ge=0, le=5, allow_inf_nan=False)
    bubble_max_seconds: float = Field(default=1.5, ge=0, le=5, allow_inf_nan=False)
    # Shape-only split: at least two sentences and this many characters -> 2 bubbles; this many -> up to 3.
    split_min_chars: int = Field(default=50, ge=1, le=2000)
    split_three_chars: int = Field(default=140, ge=1, le=4000)


class NewsConfig(BaseModel):
    enabled: bool = True
    url: str = "https://raw.githubusercontent.com/whitelabel1940/awalia/main/news/news.json"

    @field_validator('url')
    @classmethod
    def secure_url(cls, value):
        from cbs.news import https_url
        return https_url(value)


class ShareConfig(BaseModel):
    url: str = 'https://github.com/whitelabel1940/awalia'
    hashtag: str = 'Awalia'


class TelemetryConfig(BaseModel):
    host: str = 'https://eu.i.posthog.com'
    key: str = Field(default='', repr=False, exclude=True)
    privacy_url: str = ''

    @field_validator('host', 'privacy_url')
    @classmethod
    def secure_url(cls, value):
        from cbs.news import https_url
        return https_url(value) if value else value


class Config(BaseModel):
    _data_dir: Path = PrivateAttr(default_factory=lambda: resolve_paths()[0])
    _characters_root: Path = PrivateAttr(default_factory=lambda: resolve_paths()[1])
    llm: LLMConfig
    models: Models
    simple: SimpleConfig = Field(default_factory=SimpleConfig)
    advanced: AdvancedConfig = Field(default_factory=AdvancedConfig)
    onboarding: OnboardingConfig = Field(default_factory=OnboardingConfig)
    character: CharacterConfig
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    knowing: KnowingConfig = Field(default_factory=KnowingConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    inner: InnerConfig = Field(default_factory=InnerConfig)
    early: EarlyConfig = Field(default_factory=EarlyConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    days: DaysConfig = Field(default_factory=DaysConfig)
    requests: RequestsConfig = Field(default_factory=RequestsConfig)
    photos: PhotoConfig = Field(default_factory=PhotoConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    costs: CostsConfig = Field(default_factory=CostsConfig)
    first_run: FirstRunConfig = Field(default_factory=FirstRunConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    share: ShareConfig = Field(default_factory=ShareConfig)
    storage: "StorageConfig" = Field(default_factory=lambda: StorageConfig())


class StorageConfig(BaseModel):
    full_trace_days: int = Field(default=14, ge=0)


def overlay(base, values):
    result = dict(base)
    for key, value in values.items():
        result[key] = overlay(result.get(key, {}), value) if isinstance(value, dict) else value
    return result


def read_secrets(path):
    result = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, sep, value = line.partition("=")
            name, value = name.strip(), value.strip()
            try:
                if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                    raise ValueError()
                if value.startswith('"'):
                    value = json.loads(value)
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                if not isinstance(value, str):
                    raise ValueError()
                result[name] = value
            except ValueError:
                raise ValueError("data/secrets.env の形式を確認してください。") from None
    return result


def load_secrets(path, *, skip=()):
    for name, value in read_secrets(path).items():
        if name in skip:
            continue
        if name == "OPENROUTER_API_KEY" and value == "":
            os.environ[name] = ""  # Explicit local disconnect overrides inherited credentials.
        else:
            os.environ.setdefault(name, value)


def load_config(path: Path = ROOT / "config.toml", *, data_dir=None, secrets_dir=None, without_keys=False) -> Config:
    # CBS_DATA_DIR lets tests and isolated runs ignore the user's own settings and keys.
    data_dir, characters_root = resolve_paths(data_dir)
    with path.open("rb") as stream:
        values = tomllib.load(stream)
    settings = {}
    if (data_dir / "settings.toml").is_file():
        with (data_dir / "settings.toml").open("rb") as stream:
            settings = tomllib.load(stream)
            values = overlay(values, settings)
    values.setdefault("llm", {})["mode"] = settings.get("llm", {}).get("mode", "advanced" if settings.get("models") else values["llm"].get("mode", "simple"))
    if without_keys:
        # Clear inherited credentials too, including custom provider env names.
        names = {values['llm'].get('api_key_env'), 'GEMINI_API_KEY', 'GPT_API_KEY',
                 'OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY'}
        names.update(p.get('api_key_env') for p in values['llm'].get('providers', {}).values())
        for name in names - {None}:
            os.environ.pop(name, None)
    trial_names = set()
    if secrets_dir is not None or without_keys:
        # Keys entered in the trial UI stay in the trial, never in the real file.
        trial_names = read_secrets(data_dir / 'secrets.env').keys()
        load_secrets(data_dir / 'secrets.env')
    if not without_keys:
        load_secrets(Path(secrets_dir or data_dir) / 'secrets.env', skip=trial_names)
    # SP2: migrate the old local selection, including the saved advanced-mode draft.
    for models in (values.get('models', {}), values.get('llm', {}).get('advanced_models', {})):
        if models.get('stt') == 'local/kotoba-whisper':
            models['stt'] = 'local/haas-ja'
    config = apply_llm_mode(Config.model_validate(values))
    config._data_dir = data_dir
    config._characters_root = characters_root
    return config


def apply_llm_mode(config):
    if config.llm.mode == "simple":
        if not config.llm.advanced_models:
            config.llm.advanced_models = config.models.model_dump()
        config.models = Models.model_validate({**(config.simple.models or SIMPLE_MODELS),
            'vision': config.simple.models.get('vision', SIMPLE_MODELS['vision']),
            'stt': config.simple.models.get('stt', SIMPLE_MODELS['stt']),
            **{role: getattr(config.simple, role) for role in ("actor", "judge", "stt") if getattr(config.simple, role)}})
    return config


def research_provider(config):
    value = config.models.research or config.models.steward
    name, sep, _ = value.partition("/")
    return name if sep and name in {"gemini", *config.llm.providers} else config.llm.provider


def research_ready(config):
    provider = research_provider(config)
    return provider in ("gemini", "openrouter", "anthropic", "openai") and key_set(config, provider)


def equivalent_embedding_model(model):
    return {"openrouter/google/gemini-embedding-2": "gemini/gemini-embedding-2"}.get(model, model)


def embedding_model(config):
    value = config.models.embedding
    return value if "/" in value else config.llm.provider + "/" + value


def archived_margin(config):
    model = embedding_model(config)
    overrides = config.memory.archived_margins
    if model in overrides:
        return overrides[model]
    if model.startswith('local/'):
        from cbs.llm.local import MODELS
        if spec := MODELS.get(model.removeprefix('local/')):
            return spec.archived_margin
    return config.memory.archived_margin


def key_env(config, provider):
    if provider == "gemini":
        return config.llm.api_key_env
    item = config.llm.providers.get(provider)
    return item.api_key_env if item else None


def key_set(config, provider):
    name = key_env(config, provider)
    return bool(name and os.environ.get(name))
