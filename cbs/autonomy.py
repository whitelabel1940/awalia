"""Serialized inner life and proactive judgments. No keyword/meaning rules."""
from cbs.storage import dump_trace
from cbs.llm.cost import for_step
import json
import math
from datetime import date, timedelta
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from pydantic.json_schema import SkipJsonSchema

from cbs import clock
from cbs.runtime import read_prompt, character_prefix
from cbs.display_text import display_call
from cbs.sharing import ShareChoice


class AutonomyField(BaseModel):
    model_config = ConfigDict(extra='forbid')


class FollowUp(AutonomyField):
    topic: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    event_date: date | None = None
    weight: float = Field(default=0.7, ge=0, le=1)


class InnerLife(AutonomyField):
    llm_role: ClassVar[str] = 'steward'
    action: Literal['none', 'research', 'browse_shopping', 'read_source', 'read_book', 'watch_video', 'world_activity', 'read_wikipedia', 'reflect', 'prepare_share']
    reason: str
    topic: str = ''
    task: str = ''
    interest_id: int | None = None
    research_candidate_id: int | None = None
    thread_ref: str | None = None
    source_item_id: int | None = None
    book_id: int | None = None
    video_url: str = ''
    book_proposals: list['BookProposal'] = Field(default_factory=list, max_length=5)


class BookProposal(AutonomyField):
    title: str = Field(min_length=1, max_length=500)
    author: str = Field(default='', max_length=200)


class ReadingImpression(AutonomyField):
    llm_role: ClassVar[str] = 'steward'
    summary: str = Field(min_length=1, max_length=1800)
    impression: str = Field(min_length=1, max_length=1000)
    quote: str = Field(default='', max_length=140)

    @field_validator('quote', mode='before')
    @classmethod
    def bounded_quote(cls, value):
        # Length enforcement is a code rule; models need not count characters exactly.
        return value[:140] if isinstance(value, str) else value


class VideoImpression(AutonomyField):
    llm_role: ClassVar[str] = 'steward'
    understood: bool
    summary: str = Field(min_length=1, max_length=1800)
    impression: str = Field(min_length=1, max_length=1000)


class ShoppingItem(AutonomyField):
    title: str = Field(min_length=1, max_length=500)
    uri: str


class ShoppingImpression(ReadingImpression):
    products: list[ShoppingItem] = Field(min_length=1, max_length=3)


def shopping_ready(pipeline):
    from cbs.config import research_provider, research_ready
    from cbs.llm import FakeLLMClient
    provider = research_provider(pipeline.config)
    api = {'openrouter': 'chat', 'anthropic': 'messages', 'openai': 'responses'}
    return bool(pipeline.config.autonomy.tools.browse_shopping.domains and
                (isinstance(pipeline.client, FakeLLMClient) or
                 (provider in api and research_ready(pipeline.config)
                  and pipeline.config.llm.providers[provider].api == api[provider])))


class WikiChoice(AutonomyField):
    llm_role: ClassVar[str] = 'steward'
    page_id: int


class WorldPlan(AutonomyField):
    llm_role: ClassVar[str] = 'steward'
    mode: Literal['adaptation', 'generation']
    reason: str
    task: str
    source_item_id: int | None = None
    material_kind: Literal['source', 'research'] = 'source'


class WorldActivity(AutonomyField):
    llm_role: ClassVar[str] = 'steward'
    summary: str = Field(min_length=1, max_length=1800)
    impression: str = Field(min_length=1, max_length=1000)
    source_title: str = Field(min_length=1, max_length=500)
    facts: list[str] = Field(min_length=1, max_length=5)

    @field_validator('summary', 'impression', 'source_title', 'facts')
    @classmethod
    def in_world_only(cls, value):
        import re
        values = value if isinstance(value, list) else [value]
        if any(not v.strip() or len(v) > 1800 or re.search(r'https?://', v, re.I) for v in values):
            raise ValueError('世界内の記録に実URLや空の事実は保存できません。')
        return value


ResearchQuestion = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=280)]


class ActivityUpdate(AutonomyField):
    llm_role: ClassVar[str] = 'steward'
    interests: list['ActivityInterest'] = Field(default_factory=list, max_length=3)
    research_questions: list[ResearchQuestion] = Field(default_factory=list, max_length=2)
    share: ShareChoice = Field(default_factory=ShareChoice)


class ActivityInterest(AutonomyField):
    operation: Literal['new', 'reinforce', 'change', 'weaken']
    interest_id: int | None = None
    topic: str
    kind: Literal['like', 'dislike', 'curiosity', 'into', 'opinion']
    detail: str
    importance: float = Field(default=0.5, ge=0, le=1)


class Proactive(AutonomyField):
    llm_role: ClassVar[str] = 'judge'
    day_refs: list[str] = Field(default_factory=list)
    # Set by code after the decision (model_copy); not part of the LLM's output schema.
    habit: SkipJsonSchema[dict | None] = None
    spoken_context: SkipJsonSchema[list[dict]] = Field(default_factory=list)
    early_domain: str | None = None
    private_topic: bool = False
    speak: bool
    reason: str
    thread_ref: str | None = None
    want_ref: str | None = None
    why_now: str = ''


def real_world(pipeline):
    return pipeline.character.world.get('reality') == '現実どおり' and pipeline.character.world.get('contact') != 'C'


def research_connection(pipeline):
    from cbs.config import research_ready
    from cbs.llm import FakeLLMClient
    return isinstance(pipeline.client, FakeLLMClient) or research_ready(pipeline.config)


def research_allowed(pipeline):
    return research_connection(pipeline) and pipeline.config.autonomy.research and real_world(pipeline)


def world_research_ready(pipeline, budget):
    from cbs.config import research_ready
    from cbs.llm import FakeLLMClient
    tool = budget['tools']['research']
    return bool(pipeline.config.autonomy.research and tool['enabled'] and tool['remaining']
                and (isinstance(pipeline.client, FakeLLMClient) or research_ready(pipeline.config)))


TOOL_DESCRIPTIONS = {
    'browse_shopping': '買い物・クラウドファンディングの検索結果だけを眺める。購入や購入の勧めはしない',
    'research': '公開情報を調べ、出典つきで考えをまとめる',
    'read_source': '登録された読む先の未読候補から一つ読む',
    'read_book': '青空文庫の本の続きを一区切り読む。新しい本なら候補を最大5つ挙げる',
    'watch_video': '候補のYouTube動画を一本観る。公開状態と長さを確認してから観る',
    'world_activity': '世界観に沿って活動する。翻案と生成の選択もLLMが世界の軸から決める',
    'read_wikipedia': 'Web検索の調べ物が使えない時、日本語Wikipediaの記事を検索して読む',
    'reflect': 'すでに知ったことについて考える',
    'prepare_share': '気にかけていることを話せる形に整える',
    'none': '今は何もしない',
}


def reading_tools(pipeline):
    from cbs.reading import ReadingTools
    from cbs.config import ShoppingToolConfig
    if not hasattr(pipeline, 'reading_tools'):
        pipeline.reading_tools = ReadingTools(pipeline.store, blocked_domains=pipeline.config.autonomy.tools.browse_shopping.domains)
    domains = list(dict.fromkeys(ShoppingToolConfig().domains + pipeline.config.autonomy.tools.browse_shopping.domains))
    pipeline.reading_tools.interval_hours = pipeline.config.autonomy.source_refresh_hours
    pipeline.reading_tools.blocked_domains = domains
    from cbs.fetch import PublicFetcher
    if isinstance(pipeline.reading_tools._fetch, PublicFetcher):
        pipeline.reading_tools._fetch.blocked_domains = domains
    return pipeline.reading_tools


def url_is_youtube_feed(url):
    from urllib.parse import urlsplit
    p = urlsplit(url)
    return p.hostname in ('youtube.com', 'www.youtube.com') and p.path == '/feeds/videos.xml'


def activity_availability(pipeline):
    """Read local settings/candidates only; shared by settings and tool selection.

    Switches and daily budgets deliberately stay outside these states.
    """
    from cbs.config import key_env, key_set
    from cbs.llm import FakeLLMClient, RoutingLLMClient
    from cbs.media import video_ready, video_model, video_candidates, youtube_url
    cfg, store = pipeline.config, pipeline.store
    cid = cfg.character.id
    fake = isinstance(pipeline.client, FakeLLMClient)
    provider = RoutingLLMClient(cfg)._split(cfg.models.steward)[0]
    ready = fake or not key_env(cfg, provider) or key_set(cfg, provider)
    world = real_world(pipeline)
    world_activity = pipeline.character.world.get('reality') != '現実どおり'
    research = research_connection(pipeline)
    articles = [i for i in store.source_candidates(cid) if not youtube_url(i['url'])]
    videos = video_candidates(store, cid)
    video = video_ready(pipeline)
    tools = {}
    for name in TOOL_DESCRIPTIONS:
        state, reason, fix = 'ok', '', None
        if name in type(cfg.autonomy.tools).model_fields and not (world or name == 'world_activity'):
            state, reason, fix = 'unavailable', '世界の設定により、現実の活動は行いません。', None
        elif name == 'world_activity' and not world_activity:
            state, reason, fix = 'unavailable', '現実どおりの世界では、世界の中で過ごす活動は行いません。', None
        elif state == 'ok':
            if name == 'research' and not research:
                state, reason, fix = 'unavailable', '調べ物に使えるキーまたはサービスの設定がありません。', '/wizard?ai=1'
            elif name == 'browse_shopping' and not shopping_ready(pipeline):
                from cbs.config import research_provider
                state, reason, fix = 'unavailable', ('Gemini に直接つないでいる間は眺めません（検索するサイトを限れないため）。' if research_provider(cfg) == 'gemini' else 'OpenRouter（かんたん設定）でつないでいる時だけ眺めます。'), '/wizard?ai=1'
            elif name == 'watch_video':
                if not video:
                    unrestricted = cfg.model_copy(deep=True)
                    unrestricted.llm.zdr_only = False
                    zdr_blocked = cfg.llm.zdr_only and (video_model(unrestricted, fake=fake) or '').startswith('openrouter/')
                    state, reason, fix = 'unavailable', ('「会話を保存しない提供元だけを使う」にしている間は観られません。' if zdr_blocked else '動画は OpenRouter か Gemini でつないでいる時に観られます。'), '/wizard?ai=1'
                elif not videos:
                    state, reason = 'waiting', 'まだ観る動画がありません。よく読むサイトに YouTube チャンネルを登録すると観られます。調べ物で見つけた動画を観ることもあります。'
            elif name == 'read_source' and not articles:
                state, reason = 'waiting', ('よく読むサイトを登録すると読みます。' if not store.list_sources(cid) else 'まだ読む記事がありません。新しい記事を待っています。')
            # Mirrors available_tools: Wikipedia steps aside only while research itself can be chosen.
            elif name == 'read_wikipedia' and research_allowed(pipeline) and cfg.autonomy.tools.research.enabled:
                reason = '調べ物ができない時の代わりに読みます。今は調べ物が使えるため、選ばれません。'
        if name != 'none' and not ready and state != 'unavailable':
            state, reason, fix = 'unavailable', '自分の時間に使う AI のキーがありません。', '/wizard?ai=1'
        tools[name] = {'state': state, 'reason': reason, 'fix': fix}
    return {'tools': tools, 'real_world': world, 'world_activity': world_activity,
            'ready': ready, 'video_ready': video, 'articles': articles, 'videos': videos}


def autonomy_status(pipeline):
    if not hasattr(pipeline, 'character'):
        return {'tools': {}, 'real_world': False, 'world_activity': False}
    status = activity_availability(pipeline)
    return {k: status[k] for k in ('tools', 'real_world', 'world_activity')}


def available_tools(pipeline, budget, *, refresh=False):
    cid = pipeline.config.character.id
    status = activity_availability(pipeline)
    ready, world = status['ready'], status['real_world']
    sources = pipeline.store.list_sources(cid)
    source_budget = budget['tools']['read_source']
    world_budget = budget['tools']['world_activity']
    world_activity = status['world_activity']
    source_refresh = (source_budget['enabled'] and source_budget['remaining']
                      and (world or (world_activity and world_budget['enabled'] and world_budget['remaining'])))
    video_refresh = world and status['video_ready'] and budget['tools']['watch_video']['enabled'] and budget['tools']['watch_video']['remaining']
    if refresh and ready and budget['activities_remaining'] and (source_refresh or video_refresh):
        for source in sources:
            if source['enabled'] and (source_refresh or url_is_youtube_feed(source['url'])):
                reading_tools(pipeline).refresh(cid, source['id'])
    status = activity_availability(pipeline) if refresh else status
    articles = status['articles']
    books = [b for b in pipeline.store.list_books(cid) if not b['finished_at']]
    result = []
    for name, description in TOOL_DESCRIPTIONS.items():
        remaining = budget['tools'].get(name, {}).get('remaining', budget['activities_remaining'])
        allowed = name == 'none' or bool(budget['activities_remaining'] and remaining and ready)
        allowed = allowed and status['tools'][name]['state'] == 'ok'
        candidates = []
        if name in budget['tools']:
            allowed = allowed and budget['tools'][name]['enabled']
        if name == 'research':
            allowed = allowed and pipeline.config.autonomy.research
            candidates = pipeline.store.research_candidates(cid)
        elif name == 'read_source':
            candidates = articles
        elif name == 'read_book':
            candidates = books
        elif name == 'watch_video':
            if not status['video_ready']:
                continue  # No supported input method: never offer this tool.
            candidates = status['videos']
        elif name == 'world_activity':
            candidates = articles if source_budget['enabled'] and source_budget['remaining'] else []
        elif name == 'read_wikipedia':
            research = budget['tools']['research']
            allowed = allowed and not (research_allowed(pipeline) and research['enabled'] and research['remaining'])
        result.append({'name': name, 'description': description, 'available': bool(allowed),
                       'remaining': remaining, 'candidates': candidates,
                       **({'research_available': world_research_ready(pipeline, budget)} if name == 'world_activity' else {})})
    return result


def no_long_copy(body, values):
    compact_body = ''.join(body.split())
    for value in values:
        compact = ''.join(value.split())
        if (compact_body and compact_body in compact) or any(
                compact[n:n+180] in compact_body for n in range(max(0, len(compact)-179))):
            raise ValueError('記事本文の長い転記は保存しません。')


def private_ask(pipeline, prompt, schema, context):
    # Fetched content must never be written even if validation fails.
    def generate():
        return for_step(pipeline.client, 'steward', 'autonomy').generate_json(pipeline.config.models.steward,
            read_prompt(prompt) + character_prefix(pipeline.character, actor=True, store=pipeline.store),
            [{'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}], schema)
    fields = {ReadingImpression: ('summary', 'impression'), ShoppingImpression: ('summary', 'impression'),
              WorldActivity: ('summary', 'impression', 'source_title', 'facts')}.get(schema)
    def texts(r):
        values = [text for field in fields for text in
                  (getattr(r.parsed, field) if isinstance(getattr(r.parsed, field), list) else [getattr(r.parsed, field)])]
        if isinstance(r.parsed, ShoppingImpression):
            values.extend(p.title for p in r.parsed.products)
        return values
    result = display_call(generate, texts) if fields else generate()
    parsed = schema.model_validate_json(pipeline.client.sanitize_text(result.parsed.model_dump_json()))
    return parsed, {k: getattr(result, k) for k in ('model', 'usage', 'latency_ms', 'upstream_provider')}


def without_tracking(uri):
    """Drop affiliate and tracking parameters that search results carry (someone else's tag=, utm_*, ref=)."""
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    parts = urlsplit(uri)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not (k in ('tag', 'ref', 'ref_', 'linkCode', 'linkId', 'camp', 'creative', 'scid', 'affiliate', 'aff_id')
                    or k.startswith('utm_') or k.startswith('pd_rd'))]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ''))


def shopping_activity(pipeline, decision):
    from cbs.fetch import in_domains
    domains = pipeline.config.autonomy.tools.browse_shopping.domains
    observed_at = clock.iso()
    result = for_step(pipeline.client, 'steward', 'autonomy').generate_grounded(pipeline.config.models.research or pipeline.config.models.steward,
        read_prompt('browse_shopping_search'),
        [{'role': 'user', 'content': pipeline.client.sanitize_text(decision.task)}], include_domains=domains)
    safe = pipeline._safe_trace({'queries': result.queries, 'sources': result.sources})
    if not safe['queries'] or not safe['sources'] or any(not in_domains(s.get('uri', ''), domains) for s in safe['sources']):
        raise ValueError('対象サイトの検索出典を確認できませんでした。')
    parsed, metadata = private_ask(pipeline, 'browse_shopping', ShoppingImpression,
        {'observed_at': observed_at, 'search_result': pipeline.client.sanitize_text(result.text), 'sources': safe['sources']})
    cited = {s['uri'] for s in safe['sources']}
    if any(p.uri not in cited for p in parsed.products):
        raise ValueError('検索結果にない品のURLです。')
    sources = [{**p.model_dump(), 'uri': without_tracking(p.uri), 'observed_at': observed_at} for p in parsed.products]
    summary = f'検索結果を見た日時: {observed_at}\n' + parsed.summary + '\n\n' + parsed.impression
    return summary, {'queries': safe['queries'], 'sources': sources}, {
        'observed_at': observed_at, 'include_domains': domains, 'reading': 'search_results_only',
        'search': {k: getattr(result, k) for k in ('model', 'usage', 'latency_ms', 'upstream_provider')}, **metadata}


def extra_activity(pipeline, decision, tools, activity_id):
    from cbs.media import youtube_url, video_metadata, video_model, wikipedia_search, wikipedia_read
    from cbs.llm import FakeLLMClient
    from cbs.reading import html
    reader, cid = reading_tools(pipeline), pipeline.config.character.id
    candidates = next(t['candidates'] for t in tools if t['name'] == decision.action)
    item, facts = None, []
    if decision.action == 'watch_video':
        url = youtube_url(decision.video_url)
        candidate = next((c for c in candidates if c['uri'] == url), None)
        if not candidate:
            raise ValueError('候補にない動画です。')
        source = video_metadata(reader.fetch, url, pipeline.config.autonomy.tools.watch_video.max_minutes)
        result = display_call(lambda: for_step(pipeline.client, 'steward', 'autonomy').generate_video_json(video_model(pipeline.config, fake=isinstance(pipeline.client, FakeLLMClient)),
            read_prompt('watch_video') + character_prefix(pipeline.character, actor=True, store=pipeline.store), url, VideoImpression),
            lambda r: [r.parsed.summary, r.parsed.impression])
        parsed = VideoImpression.model_validate_json(pipeline.client.sanitize_text(result.parsed.model_dump_json()))
        if not parsed.understood:
            raise ValueError('動画を参照できませんでした。')
        metadata = {k: getattr(result, k) for k in ('model', 'usage', 'latency_ms', 'upstream_provider')}
        sources = [{'title': source['title'], 'uri': source['uri']}]
        if candidate['source_item_id']:
            item = {'id': candidate['source_item_id']}
    elif decision.action == 'read_wikipedia':
        found = wikipedia_search(reader.fetch, decision.task)
        if not found:
            raise ValueError('記事候補がありません。')
        choice, _ = private_ask(pipeline, 'read_wikipedia', WikiChoice, {'stage': 'select', 'task': decision.task, 'candidates': found})
        candidate = next((c for c in found if c['page_id'] == choice.page_id), None)
        if not candidate:
            raise ValueError('候補にない記事です。')
        source, body = wikipedia_read(reader.fetch, candidate)
        parsed, metadata = private_ask(pipeline, 'read_wikipedia', ReadingImpression,
            {'stage': 'read', **source, 'body': body, 'scope': '提供された本文部分のみ'})
        no_long_copy(body, [parsed.summary, parsed.impression])
        sources = [source]
    else:
        context = {'stage': 'plan', 'decision': decision.model_dump(), 'materials': candidates,
                   'world_facts': pipeline.store.list_world_facts(cid),
                   'research_available': world_research_ready(pipeline, pipeline.store.autonomy_budget(cid))}
        plan, _ = private_ask(pipeline, 'world_activity', WorldPlan, context)
        context.update(stage='compose', plan=plan.model_dump())
        sources, body = [], ''
        if plan.mode == 'adaptation':
            if plan.material_kind == 'research':
                if not world_research_ready(pipeline, pipeline.store.autonomy_budget(cid)) or not plan.task.strip():
                    raise ValueError('翻案用の調べ物の設定・予算を満たしません。')
                # Reserve the nested search attempt too; failed calls consume its daily budget.
                task = pipeline.client.sanitize_text(plan.task)
                with pipeline.store.connect() as db:
                    db.execute('UPDATE activities SET queries_json=? WHERE id=?', (json.dumps([task], ensure_ascii=False), activity_id))
                result = for_step(pipeline.client, 'steward', 'autonomy').generate_grounded(pipeline.config.models.research or pipeline.config.models.steward,
                    read_prompt('research'), [{'role': 'user', 'content': task}])
                if not result.queries or not result.sources:
                    raise ValueError('翻案素材の検索に出典がありません。')
                safe_search = pipeline._safe_trace({'queries': result.queries, 'sources': result.sources})
                with pipeline.store.connect() as db:
                    db.execute('UPDATE activities SET queries_json=? WHERE id=?',
                               (json.dumps(safe_search['queries'], ensure_ascii=False), activity_id))
                body = pipeline.client.sanitize_text(result.text)[:24000]
                sources = [{**s, 'internal': True} for s in pipeline._safe_trace({'sources': result.sources})['sources']]
                context['material'] = {'body': body}
            else:
                item = next((c for c in candidates if c['id'] == plan.source_item_id), None)
                if not item:
                    raise ValueError('翻案素材が候補にありません。')
                # A source read nested in adaptation uses the same per-tool budget.
                with pipeline.store.connect() as db:
                    db.execute('INSERT INTO reading_receipts (activity_id,source_item_id) VALUES (?,?)',
                               (activity_id, item['id']))
                page = reader.fetch(item['url'], max_bytes=2_000_000)
                body = html(page.text()).text()
                context['material'] = {'title': item['title'], 'body': body}
                sources = [{'title': item['title'], 'uri': page.url, 'internal': True}]
            if not body.strip():
                raise ValueError('翻案素材の本文がありません。')
        parsed, metadata = private_ask(pipeline, 'world_activity', WorldActivity, context)
        if body:
            no_long_copy(body, [parsed.summary, parsed.impression, parsed.source_title, *parsed.facts])
        facts = [{'content': f, 'source_title': parsed.source_title} for f in parsed.facts]
        sources.insert(0, {'title': parsed.source_title, 'in_world': True})
        metadata.update(mode=plan.mode, reason=plan.reason, material_kind=plan.material_kind if plan.mode == 'adaptation' else None)
    return parsed.summary + '\n\n' + parsed.impression, sources, item, facts, metadata


def read_activity(pipeline, decision, tools):
    """Return only permitted persisted fields; never retain raw reading request/response traces."""
    reader, cid = reading_tools(pipeline), pipeline.config.character.id
    book, item = None, None
    if decision.action == 'read_source':
        candidates = next(t['candidates'] for t in tools if t['name'] == 'read_source')
        item = next((i for i in candidates if i['id'] == decision.source_item_id), None)
        if item is None:
            raise ValueError('候補にない記事です。')
        from cbs.reading import html
        page = reader.fetch(item['url'], max_bytes=2_000_000)
        body = html(page.text()).text()
        source = {'title': item['title'], 'uri': page.url}
        context = {'title': item['title'], 'body': body}
    else:
        candidates = next(t['candidates'] for t in tools if t['name'] == 'read_book')
        if decision.book_id is not None:
            book = next((b for b in candidates if b['id'] == decision.book_id), None)
            if book is None:
                raise ValueError('候補にない本です。')
        else:
            work = reader.match_book(decision.book_proposals)
            book = reader.prepare_book(cid, work)
        body = reader.book_chunk(book)
        source = {'title': book['title'], 'uri': book['url'], 'author': book['author'],
                  'book_id': book['id'], 'chunk_number': book['read_count']+1, 'chunk_count': book['chunk_count']}
        context = {**source, 'body': body, 'previous_impressions': book['read_impressions'],
                   'finished': book['read_count']+1 == book['chunk_count']}
    if not body.strip():
        raise ValueError('本文がありません。')
    result = display_call(lambda: for_step(pipeline.client, 'steward', 'autonomy').generate_json(pipeline.config.models.steward,
        read_prompt(decision.action) + character_prefix(pipeline.character, actor=True, store=pipeline.store),
        [{'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}], ReadingImpression),
        lambda r: [r.parsed.summary, r.parsed.impression])
    parsed = ReadingImpression.model_validate_json(pipeline.client.sanitize_text(result.parsed.model_dump_json()))
    if item:
        no_long_copy(body, [parsed.summary, parsed.impression])
    # A quote is bounded and must occur verbatim in the actual read section.
    source['quote'] = parsed.quote if parsed.quote in body else ''
    metadata = {k: getattr(result, k) for k in ('model', 'usage', 'latency_ms', 'upstream_provider')}
    return parsed.summary + '\n\n' + parsed.impression, [source], book, item, metadata


def ask(pipeline, prompt, schema, context, model=None):
    result = for_step(pipeline.client, 'steward', 'autonomy').generate_json(model or pipeline.config.models.steward,
        read_prompt(prompt) + character_prefix(pipeline.character, actor=True, store=pipeline.store),
        [{'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}], schema)
    # Sanitize parsed values too, before any persistence.
    return schema.model_validate_json(pipeline.client.sanitize_text(result.parsed.model_dump_json())), pipeline._safe_trace(result.trace())


def catch_up(pipeline, max_runs=3):
    """Runs in the existing per-DB queue. Skipped backlog is never replayed indefinitely."""
    store, cid = pipeline.store, pipeline.config.character.id
    store.expire_threads(cid, pipeline.config.autonomy.thread_expiry_days)
    store.sync_plan_threads(cid)
    current = clock.now()
    if not pipeline.config.autonomy.inner_life:
        with store.connect() as db:
            db.execute('INSERT INTO autonomy_schedule VALUES (?, ?) ON CONFLICT(character_id) DO UPDATE SET last_at=excluded.last_at', (cid, clock.iso()))
        return
    interval = timedelta(hours=pipeline.config.autonomy.inner_life_interval_hours)
    with store.connect() as db:
        row = db.execute('SELECT last_at FROM autonomy_schedule WHERE character_id=?', (cid,)).fetchone()
        if row is None:
            db.execute('INSERT INTO autonomy_schedule VALUES (?, ?)', (cid, clock.iso()))
            return
    elapsed = current - clock.parse(row['last_at'])
    runs = min(3, max_runs, max(0, int(elapsed / interval)))
    for _ in range(runs):
        inner_life(pipeline)
    if runs:
        with store.connect() as db:
            db.execute('UPDATE autonomy_schedule SET last_at=? WHERE character_id=?', (current.isoformat(), cid))


def inner_life_context(pipeline, *, refresh=False):
    """Shared by execution and the decision-only manual replay."""
    store, cid = pipeline.store, pipeline.config.character.id
    budget = store.autonomy_budget(cid)
    interests = store.list_interests(cid)
    coverage = store.interest_activity_coverage(cid)
    tools = available_tools(pipeline, budget, refresh=refresh)
    context = {'now': clock.stamp(clock.iso()), 'last_conversation_gap': clock.gap(store.previous_utterance(cid)),
               'now_summary': store.now_summary(cid), 'feeling': store.current_feeling(cid),
               'interests': [{k: v for k, v in i.items() if k != 'embedding'} | coverage[i['id']]
                             for i in sorted(interests, key=lambda i: -i['salience']) if i['status'] != 'fading'],
               'threads': store.list_threads(cid, eligible_only=True), 'activities': store.conversation_activities(cid),
               'world_facts': store.list_world_facts(cid),
               'budget': budget, 'research_allowed': research_allowed(pipeline), 'tools': tools}
    return context, interests


def inner_life(pipeline, *, refresh=True):
    if getattr(pipeline.store, 'costs', None) and pipeline.store.costs.paused():
        return
    store, cid = pipeline.store, pipeline.config.character.id
    if not pipeline.config.autonomy.inner_life:
        return
    from cbs.inner import update_long_gap
    update_long_gap(pipeline)
    if getattr(store, 'costs', None) and store.costs.paused():
        return
    context, interests = inner_life_context(pipeline, refresh=refresh)
    budget, tools = context['budget'], context['tools']
    trace, run_id, activity_id = {'tools': tools, 'budget_before': budget}, None, None
    try:
        decision, judgment = ask(pipeline, 'inner_life', InnerLife, context)
        trace['judgment'] = judgment
        with store.connect() as db:
            cursor = db.execute('INSERT INTO inner_life_runs (character_id, created_at, action, reason, status, trace_json) VALUES (?, ?, ?, ?, ?, ?)',
                                (cid, clock.iso(), decision.action, decision.reason, 'running', dump_trace(pipeline._safe_trace(trace))))
            run_id = cursor.lastrowid
        if decision.action == 'none':
            status = 'completed'
        elif getattr(store, 'costs', None) and store.costs.paused():
            status = 'limited'
            trace['limit'] = '月の目安額を超えたため自主活動を休止しています。'
        elif not next((t['available'] for t in tools if t['name'] == decision.action), False):
            status = 'limited'
            trace['limit'] = '道具の活動予算・世界・設定・対象条件により実行しない'
        else:
            candidate = None
            if decision.action == 'research' and decision.research_candidate_id is not None:
                candidate = next((c for t in tools if t['name'] == 'research' for c in t['candidates']
                                  if c['id'] == decision.research_candidate_id), None)
                if candidate is None:
                    raise ValueError('候補にない問いです。')
                if candidate['interest_id'] is not None:
                    decision.interest_id = candidate['interest_id']
                decision.topic = candidate['question']
            # Reserve the attempt before external work: failures also consume budget.
            with store.connect() as db:
                cursor = db.execute('INSERT INTO activities (character_id, run_id, kind, topic, summary, created_at, interest_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                                    (cid, run_id, decision.action, decision.topic, '', clock.iso(),
                                     decision.interest_id if decision.interest_id in {i['id'] for i in interests} else None))
                activity_id = cursor.lastrowid
            book, item, world_facts = None, None, []
            if decision.action == 'browse_shopping':
                summary, safe, metadata = shopping_activity(pipeline, decision)
                trace['execution'] = pipeline._safe_trace(metadata)
            elif decision.action in ('read_source', 'read_book'):
                summary, sources, book, item, metadata = read_activity(pipeline, decision, tools)
                safe = pipeline._safe_trace({'queries': [], 'sources': sources})
                trace['execution'] = {'reading': True, 'sources': safe['sources'], **pipeline._safe_trace(metadata)}
            elif decision.action in ('watch_video', 'world_activity', 'read_wikipedia'):
                summary, sources, item, world_facts, metadata = extra_activity(pipeline, decision, tools, activity_id)
                if decision.action == 'world_activity':
                    decision.topic = sources[0]['title']
                with store.connect() as db:
                    queries = json.loads(db.execute('SELECT queries_json FROM activities WHERE id=?', (activity_id,)).fetchone()[0])
                safe = pipeline._safe_trace({'queries': queries, 'sources': sources})
                trace['execution'] = {'sources': safe['sources'], **pipeline._safe_trace(metadata)}
            elif decision.action == 'research':
                # Pass only a self-contained, public task. No user facts, thread details or conversation are sent to search.
                task = ({'question': candidate['question'], 'source': {k: candidate[k] for k in
                         ('source_kind', 'source_ref', 'source_note')}} if candidate else decision.task)
                result = display_call(lambda: for_step(pipeline.client, 'steward', 'autonomy').generate_grounded(pipeline.config.models.research or pipeline.config.models.steward,
                    read_prompt('research'), [{'role': 'user', 'content': pipeline.client.sanitize_text(
                        json.dumps(task, ensure_ascii=False) if candidate else task)}]), lambda r: [r.text])
                trace['execution'] = pipeline._safe_trace(result.trace())
                safe = pipeline._safe_trace({'queries': result.queries, 'sources': result.sources})
                with store.connect() as db:
                    db.execute('UPDATE activities SET queries_json=?, sources_json=? WHERE id=?',
                               (json.dumps(safe['queries'], ensure_ascii=False), json.dumps(safe['sources'], ensure_ascii=False), activity_id))
                if not result.queries or not result.sources:
                    raise ValueError('Search was not grounded')
            else:
                result = display_call(lambda: for_step(pipeline.client, 'steward', 'autonomy').generate_text(pipeline.config.models.steward,
                    read_prompt('activity') + character_prefix(pipeline.character, actor=True, store=pipeline.store),
                    [{'role': 'user', 'content': json.dumps({'decision': decision.model_dump(), 'context': context}, ensure_ascii=False)}]), lambda r: [r.text])
                trace['execution'] = pipeline._safe_trace(result.trace())
            if decision.action not in ('browse_shopping', 'read_source', 'read_book', 'watch_video', 'world_activity', 'read_wikipedia'):
                summary = pipeline.client.sanitize_text(result.text).strip()
                safe = pipeline._safe_trace({'queries': result.queries, 'sources': result.sources})
            if not summary:
                raise ValueError('Empty activity')
            with store.connect() as db:
                db.execute("UPDATE activities SET topic=?, summary=?, queries_json=?, sources_json=?, status='completed' WHERE id=?",
                           (decision.topic, summary, json.dumps(safe['queries'], ensure_ascii=False), json.dumps(safe['sources'], ensure_ascii=False), activity_id))
                if item:
                    db.execute('UPDATE source_items SET read_at=? WHERE id=?', (clock.iso(), item['id']))
                if book:
                    count = book['read_count'] + 1
                    db.execute('UPDATE books SET read_count=?, finished_at=? WHERE id=?',
                               (count, clock.iso() if count == book['chunk_count'] else None, book['id']))
                if book or item:
                    db.execute('INSERT OR REPLACE INTO reading_receipts (activity_id,book_id,chunk_number,source_item_id) VALUES (?,?,?,?)',
                               (activity_id, book['id'] if book else None, book['read_count']+1 if book else None, item['id'] if item else None))
                for fact in world_facts:
                    db.execute('INSERT INTO world_facts (character_id,content,source_title,activity_id,created_at) VALUES (?,?,?,?,?)',
                               (cid, fact['content'], fact['source_title'], activity_id, clock.iso()))
                if candidate:
                    db.execute("UPDATE research_candidates SET status='used', used_activity=? WHERE id=? AND character_id=? AND status='open'",
                               (activity_id, candidate['id'], cid))
                if decision.interest_id in {i['id'] for i in interests if i['kind'] in ('curiosity', 'into')}:
                    store._thread(db, cid, 'curiosity', decision.topic,
                        summary if decision.action == 'world_activity' else decision.reason, f'interest:{decision.interest_id}', 0.5)
            # An interest extraction failure never erases an executed activity.
            try:
                from cbs.sharing import share_candidates
                shares = share_candidates(pipeline, decision.topic + '\n' + summary)
                update, extraction = display_call(lambda: ask(pipeline, 'activity_extract', ActivityUpdate,
                    {'activity': summary, 'activity_ref': f'activity:{activity_id}', 'share_candidates': shares,
                     'interests': [{k: v for k, v in i.items() if k != 'embedding'} for i in interests]}),
                    lambda r: [r[0].share.detail, *[text for i in r[0].interests for text in (i.topic, i.detail)]])
                trace['interest_extract'] = extraction
                with store.connect() as db:
                    store.apply_share_choice(db, cid, update.share, shares, decision.topic, summary,
                                             f'activity:{activity_id}', 0.8)
                    store.save_research_questions(db, cid, update.research_questions, 'activity',
                        f'activity:{activity_id}', f'{decision.action}: {decision.topic}'[:180])
                from cbs.memory import HaruExpressed
                proposals = [HaruExpressed(**i.model_dump(), evidence_turns=[0]) for i in update.interests]
                store.require_embedding_model()
                vectors = for_step(pipeline.client, 'steward', 'autonomy').embed([i.topic + '\n' + i.detail for i in proposals], pipeline.config.models.embedding, role="document") if proposals else []
                if len(vectors) != len(proposals) or any(not v or not all(math.isfinite(n) for n in v) for v in vectors):
                    raise ValueError('Invalid activity embeddings')
                with store.connect() as db:
                    trace['interest_updates'] = store.apply_interests(db, cid, proposals, vectors,
                        [{'id': 0, 'source_turn': None, 'assistant_text': summary, 'created_at': clock.iso()}],
                        [None], None, trace.setdefault('warnings', []), activity_id=activity_id)
            except Exception:
                trace.setdefault('warnings', []).append('活動は保存済み。今の層の更新に失敗しました。')
            status = 'completed'
    except Exception as error:
        trace['error'] = 'Inner Life に失敗しました。会話と保存済みの活動を保持します。'
        from cbs.telemetry import record_error
        record_error(pipeline.store, 'autonomy', error)
        trace['error_type'] = type(error).__name__
        if hasattr(error, 'metadata'):
            trace.update(error.metadata())
        if getattr(error, 'trace', None) and activity_id is not None and decision.action in ('browse_shopping', 'read_source', 'read_book', 'watch_video', 'world_activity', 'read_wikipedia'):
            trace['execution_metadata'] = pipeline._safe_trace({k: error.trace.get(k) for k in ('model', 'usage', 'latency_ms', 'upstream_provider')})
        if getattr(error, 'trace', None) and (activity_id is None or decision.action not in ('browse_shopping', 'read_source', 'read_book', 'watch_video', 'world_activity', 'read_wikipedia')):
            trace['execution_error'] = pipeline._safe_trace(error.trace)
        status = 'failed'
    with store.connect() as db:
        trace['budget_after'] = store.autonomy_budget(cid)
        if run_id is None:
            db.execute('INSERT INTO inner_life_runs (character_id, created_at, action, reason, status, trace_json) VALUES (?, ?, ?, ?, ?, ?)',
                       (cid, clock.iso(), 'none', '判断に失敗', status, dump_trace(pipeline._safe_trace(trace))))
        else:
            db.execute('UPDATE inner_life_runs SET status=?, trace_json=? WHERE id=?',
                       (status, dump_trace(pipeline._safe_trace(trace)), run_id))
        if activity_id and status == 'failed':
            db.execute("UPDATE activities SET status='failed' WHERE id=? AND status='running'", (activity_id,))
    if activity_id and status == 'completed':
        from cbs.inner import update_inner
        activity = next(a for a in store.conversation_activities(cid) if a['id'] == activity_id)
        update_inner(pipeline, f'activity:{activity_id}', {'kind': 'activity', 'activity': activity})
        from cbs.note import fill_activity_notes
        fill_activity_notes(pipeline)


def quiet_hours(hour, config):
    start, end = config.quiet_start_hour, config.quiet_end_hour
    return start <= hour < end if start < end else (hour >= start or hour < end) if start != end else False


def proactive(pipeline, session_id, mode='opening', *, cancel=None):
    """Read saved state first; maintenance is released after foreground work."""
    if getattr(pipeline.store, 'costs', None) and pipeline.store.costs.paused():
        return {'speak': False, 'reason': '月の目安額を超えたため自主活動を休止しています。'}
    from threading import Event
    cancel = cancel if cancel is not None else Event()
    store, cid = pipeline.store, pipeline.config.character.id
    session = store.get_session(session_id)
    if session['character_id'] != cid:
        raise KeyError(session_id)
    if mode == 'opening':
        with store.connect() as db:
            db.execute('INSERT INTO web_openings VALUES (?, ?) ON CONFLICT(session_id) DO UPDATE SET opened_at=excluded.opened_at',
                       (session_id, clock.iso()))
    store.expire_threads(cid, pipeline.config.autonomy.thread_expiry_days)
    store.sync_plan_threads(cid)
    threads = store.list_threads(cid, eligible_only=True)
    early = store.early_candidates(pipeline.character, pipeline.config.early)
    with store.connect() as db:
        last = db.execute("SELECT t.* FROM turns t JOIN sessions s ON s.id=t.session_id WHERE s.character_id=? AND t.status='completed' ORDER BY julianday(COALESCE(t.assistant_at,t.created_at)) DESC,t.id DESC LIMIT 1", (cid,)).fetchone()
    gap_seconds = (clock.now() - clock.parse(last['assistant_at'] or last['created_at'])).total_seconds() if last else None
    from cbs.days import due_habits, days_context
    important_days = days_context(store, cid, pipeline.config.days)
    habit, habit_run = None, None
    if mode in ('idle', 'habit'):
        for candidate in due_habits(store, cid, pipeline.config.days, pipeline.config.autonomy):
            habit_run = store.claim_habit(cid, candidate)
            if habit_run:
                habit = candidate
                mode = 'habit'
                break
        if mode == 'habit' and habit is None:
            return {'speak': False, 'reason': '時刻内の未処理の習慣はありません。'}
    if mode == 'opening':
        cfg = pipeline.config.autonomy
        if cfg.opening == 'off':
            return {'speak': False, 'reason': '話しかけはオフ'}
        if cfg.opening == 'reserved' and gap_seconds is not None and gap_seconds < cfg.reserved_gap_hours * 3600:
            return {'speak': False, 'reason': '控えめな話しかけの待機時間内'}
    if mode == 'opening' and gap_seconds is not None and gap_seconds < pipeline.config.memory.gap_minutes * 60:
        return {'speak': False, 'reason': '前の会話からの間が短い'}
    idle_previous = []
    if mode == 'idle':
        cfg = pipeline.config.autonomy
        rule = cfg.idle_rule()
        if not cfg.idle_nudge or rule is None or quiet_hours(clock.now().hour, cfg) or not store.autonomy_budget(cid)['idle_remaining']:
            return {'speak': False, 'reason': 'idle設定・頻度・静かな時間帯・回数制限'}
        idle_seconds = rule[0] * 60
        if not last or last['session_id'] != session_id or last['kind'] == 'proactive' or gap_seconds < idle_seconds:
            return {'speak': False, 'reason': '発言からの待機条件を満たさない'}
        with store.connect() as db:
            opened = db.execute('SELECT opened_at FROM web_openings WHERE session_id=?', (session_id,)).fetchone()
        if not opened or (clock.now() - clock.parse(opened['opened_at'])).total_seconds() < idle_seconds:
            return {'speak': False, 'reason': '画面を開いてからの待機条件を満たさない'}
        with store.connect() as db:
            # Integer anchors are legacy first judgments; SQLite accepts the new text
            # anchors in the existing INTEGER-affinity column without a migration.
            idle_previous = [dict(r) for r in db.execute(
                "SELECT created_at,speak,reason,why_now FROM proactive_runs WHERE character_id=? AND mode='idle' "
                "AND (anchor=? OR CAST(anchor AS TEXT) GLOB ?) ORDER BY id",
                (cid, last['id'], f"idle:{last['id']}:*"))]
        if idle_previous:
            if any(r['speak'] for r in idle_previous):
                return {'speak': False, 'reason': 'ユーザーの返答を待っています'}
            wait = idle_seconds * cfg.idle_recheck_multiplier * cfg.idle_recheck_growth ** (len(idle_previous)-1)
            if (clock.now() - clock.parse(idle_previous[-1]['created_at'])).total_seconds() < wait:
                return {'speak': False, 'reason': 'idleの再判断まで待機中'}
        # 話題がなくても判断へ進む（なんとなく声をかけるかも LLM が決める）。間・上限・静かな時間帯は上で code が守る。
    conversation_id = store.prepare_conversation(session_id, clock.iso())
    anchor = conversation_id if mode == 'opening' else habit_run if mode == 'habit' else f"idle:{last['id']}:{len(idle_previous)+1}"
    with store.connect() as db:
        cursor = db.execute('INSERT OR IGNORE INTO proactive_runs (character_id, session_id, conversation_id, mode, anchor, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                            (cid, session_id, conversation_id, mode, anchor, clock.iso()))
        if not cursor.rowcount:
            return {'speak': False, 'reason': 'この会話では判断済み'}
        run_id = cursor.lastrowid
    trace, result = {}, {'speak': False, 'reason': '判断に失敗したため話しかけない'}
    try:
        model = store.current_model(pipeline.character)
        from cbs.sharing import spoken_context, thread_text
        activities = store.conversation_activities(cid)
        spoken = spoken_context(pipeline, [thread_text(t) for t in threads] +
                                [a['topic'] + '\n' + a['summary'] for a in activities])
        decision, trace = ask(pipeline, 'proactive', Proactive,
            {'mode': mode, 'habit': habit, 'important_days': important_days, 'now': clock.stamp(clock.iso()), 'gap_seconds': gap_seconds, 'threads': threads,
             'frequency': pipeline.config.autonomy.opening,
             'idle_previous': idle_previous,
             'user_elapsed_seconds': (clock.now() - clock.parse(last['created_at'])).total_seconds() if last and last['user_text'] else None,
             'early_candidates': early,
             'wants': [t for t in threads if t['direction'] and t['motive']],
             'feeling': store.current_feeling(cid),
             'relationship_domains': [d.model_dump(mode='json') for d in model.domains
                                      if d.id in {f'R{i:02}' for i in range(1, 11)}],
             'relationship_answers': {ref: value for ref, value in store.bootstrap_answers(cid).items()
                                      if ref.startswith('author:R')},
             'recent_turns': store.history(last['session_id'])[-6:] if last else [],
             'spoken_context': spoken,
             'activities': activities, 'world_facts': store.list_world_facts(cid),
             'private_facts_policy': store.private_context(cid)}, pipeline.config.models.judge)
        result = decision.model_dump(exclude={'private_topic'})
        available = {t['ref'] for t in threads}
        # thread_ref remains compatible with older callers; want_ref is the first-stage choice.
        chosen = decision.want_ref or decision.thread_ref
        result.update(want_ref=chosen, thread_ref=chosen)
        available_days = {d['id'] for d in important_days['today'] if d['may_mention']}
        if set(decision.day_refs) - available_days:
            result.update(speak=False, reason='大切な日の候補が利用不可')
        elif mode == 'habit' and (chosen or decision.early_domain or decision.private_topic):
            result.update(speak=False, reason='習慣以外の話題はこの枠では選びません。')
        elif decision.want_ref and decision.thread_ref and decision.want_ref != decision.thread_ref:
            result.update(speak=False, reason='想いとスレッドの参照が一致しない')
        elif chosen and chosen not in available:
            result.update(speak=False, reason='選択されたスレッドは利用不可')
        elif decision.private_topic and not any(t['ref']==chosen and t.get('private') and t.get('may_initiate') for t in threads):
            result.update(speak=False, reason='私的な話題は本人から話すときだけ使います。')
        elif decision.early_domain and (decision.early_domain not in {d['id'] for d in early} or chosen or decision.private_topic):
            result.update(speak=False, reason='使い始めの話題の候補・上限・選択が一致しない')
        elif decision.speak and getattr(store, 'costs', None) and store.costs.paused():
            result.update(speak=False, reason='月の目安額を超えたため自主活動を休止しています。')
        elif decision.speak:
            # Keep exactly the history the judge saw, including across conversations.
            decision = decision.model_copy(update={'thread_ref': chosen, 'want_ref': chosen, 'habit': habit,
                                                   'spoken_context': spoken})
            result.update(thread_ref=chosen, want_ref=chosen)
            response = pipeline.proactive_reply(session_id, decision, cancel)
            if response is None:
                result.update(speak=False, reason='ユーザーが先に話しかけたため取りやめ')
            else:
                result.update(response)
                if habit:
                    result['habit'] = habit
                with store.connect() as db:
                    store.mark_days(db, cid, decision.day_refs, available_days, response['turn_id'], important_days['local_day'])
    except Exception as error:
        trace['error'] = '自発発言の判断・生成に失敗。通常の会話は続行可能。'
        from cbs.telemetry import record_error
        record_error(pipeline.store, 'autonomy', error)
        # Type and the failed LLM call's sanitized trace only; provider messages may carry URLs.
        trace['error_type'] = type(error).__name__
        if hasattr(error, 'metadata'):
            trace.update(error.metadata())
        if getattr(error, 'trace', None):
            trace['failed_call'] = error.trace
        result.update(speak=False, reason='自発発言に失敗したため話しかけない')
    if cancel.is_set() and not result.get('turn_id'):
        result = {'speak': False, 'reason': 'ユーザーが先に話しかけたため取りやめ'}
    with store.connect() as db:
        db.execute('UPDATE proactive_runs SET speak=?, reason=?, thread_ref=?, source_turn=?, trace_json=?, want_ref=?, why_now=?, early_domain=? WHERE id=?',
                   (result['speak'], result['reason'], result.get('thread_ref'), result.get('turn_id'),
                    dump_trace(pipeline._safe_trace(trace)), result.get('want_ref'), result.get('why_now', ''),
                    decision.early_domain if result.get('turn_id') else None, run_id))
    return result


def inspector_activity(pipeline):
    store, cid = pipeline.store, pipeline.config.character.id
    store.expire_threads(cid, pipeline.config.autonomy.thread_expiry_days)
    return {'threads': store.list_threads(cid, with_history=True), 'activities': store.list_activities(cid),
            'decisions': store.list_inner_life(cid), 'budget': store.autonomy_budget(cid),
            'opening_decisions': store.list_proactive(cid)}
