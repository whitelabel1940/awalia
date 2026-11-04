"""ER1: inspect provider failures locally and retain only bounded metadata."""
import json

import httpx
import openai

KINDS = {'balance', 'limit', 'auth', 'busy', 'network', 'unknown'}
PROVIDERS = {'openrouter': 'OpenRouter', 'openai': 'OpenAI', 'anthropic': 'Anthropic',
             'gemini': 'Gemini', 'local': 'ローカルのサーバー'}
LINKS = {
    'openrouter': {'balance': 'https://openrouter.ai/settings/credits', 'limit': 'https://openrouter.ai/settings/keys'},
    'openai': {'balance': 'https://platform.openai.com/settings/organization/billing'},
    'anthropic': {'balance': 'https://console.anthropic.com/settings/billing'},
    'gemini': {'balance': 'https://aistudio.google.com/'},
}


class LLMError(RuntimeError):
    """Safe message and metadata, never the provider exception/body/URL/key ID."""
    def __init__(self, message, trace=None, *, kind='unknown', provider=None, status_code=None):
        super().__init__(message)
        self.kind = kind if kind in KINDS else 'unknown'
        self.provider = provider if provider in PROVIDERS else None
        self.status_code = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        self.trace = {**(trace or {}), **self.metadata()}

    def metadata(self):
        return {'error_kind': self.kind, 'status_code': self.status_code, 'provider': self.provider}


def provider_error(error, provider, trace=None):
    if isinstance(error, LLMError):
        return LLMError(str(error), trace=trace or error.trace, kind=error.kind,
                        provider=error.provider or provider, status_code=error.status_code)
    status = getattr(error, 'status_code', None)
    response = getattr(error, 'response', None)
    if status is None:
        status = getattr(response, 'status_code', None) or getattr(error, 'code', None)
    status = status if type(status) is int else None
    body = getattr(error, 'body', None) or getattr(error, 'details', None)
    if body is None and response is not None:
        try:
            # Streaming HTTP errors may not have consumed their response yet.
            response.read()
            body = response.json()
        except Exception:
            body = None
    if isinstance(body, dict):
        detail = body.get('error', body)
        detail = detail if isinstance(detail, dict) else {}
    else:
        detail = {}
    message = detail.get('message', '')
    message = message.lower() if isinstance(message, str) else ''
    kind = 'unknown'
    if isinstance(error, (openai.APIConnectionError, httpx.TransportError, ConnectionError, TimeoutError)):
        kind = 'network'
    elif provider == 'openrouter':
        meta = detail.get('metadata') or {}
        if status == 402:
            kind = 'balance'
        elif status == 403 and isinstance(meta, dict) and (meta.get('scope') == 'key_limit' or 'limit_source' in meta):
            kind = 'limit'
        elif status == 401:
            kind = 'auth'
        elif status in (408, 429, 502, 503, 504, 529):
            kind = 'busy'
    elif provider == 'openai':
        if status == 429:
            kind = 'balance' if detail.get('code') == 'insufficient_quota' else 'busy'
        elif status in (401, 403):
            kind = 'auth'
        elif status and 500 <= status <= 599:
            kind = 'busy'
    elif provider == 'anthropic':
        if status == 400 and 'credit balance is too low' in message:
            kind = 'balance'
        elif status == 400 and 'usage limits' in message:
            kind = 'limit'
        elif status in (401, 403):
            kind = 'auth'
        elif status == 429 or (status and 500 <= status <= 599):
            kind = 'busy'
    elif provider == 'gemini':
        if status == 429:
            kind = 'balance' if 'prepayment credits are depleted' in message or 'billing' in message else 'busy'
        elif status in (401, 403) or (status == 400 and 'API_KEY_INVALID' in json.dumps(detail)):
            kind = 'auth'
        elif status and 500 <= status <= 599:
            kind = 'busy'
    return LLMError('AI の呼び出しに失敗しました。', trace=trace, kind=kind, provider=provider, status_code=status)


def failure_notice(error, display_name='パートナー', *, banner=False):
    provider = error.provider
    name = PROVIDERS.get(provider, 'AI サービス')
    messages = {
        'balance': f'{name} の残高が足りないため、{display_name}が返事を書けませんでした。{name} の支払いページでチャージしてください。',
        'limit': 'API キーやアカウントに設定した利用上限に達しています。提供元の設定で上限を上げるか外してください。',
        'auth': 'API キーが使えません。設定の「AI」でキーを確かめてください。',
        'busy': f'{name}が混み合っています。少し待ってからもう一度送ってください。',
        'network': ('ローカルのサーバーにつながりませんでした。サーバーが起動しているか確かめてください。' if provider == 'local' else
                    f'{name}につながりませんでした。ネット接続を確かめてください。'),
        'unknown': f'{display_name}の返事を用意できませんでした。少し待ってからもう一度送ってください。',
    }
    message = messages[error.kind]
    if banner:
        message = f'{display_name}が話せない状態です: ' + message
    url = LINKS.get(provider, {}).get(error.kind)
    if error.kind == 'limit' and not url:
        url = LINKS.get(provider, {}).get('balance')
    return {'kind': error.kind, 'provider': provider, 'status_code': error.status_code, 'message': message,
            'link': {'url': url, 'label': 'キーの設定を開く' if provider == 'openrouter' and error.kind == 'limit' else '提供元の設定を開く'} if url else None}
