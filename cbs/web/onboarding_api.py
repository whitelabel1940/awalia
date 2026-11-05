"""Editable onboarding copy and consent; no LLM or credentials."""
from datetime import datetime, timezone
import hashlib
import tomllib

from fastapi import HTTPException
from cbs.config import ROOT, overlay
from cbs.web.settings_api import SETTINGS_LOCK, toml_text

TITLES = dict(zip(
    'consent ai partner world now scenes depth life more review look voice voice_input export done'.split(),
    ['はじめに', 'AI について', 'パートナーについて', '世界と名前', '今のこと', '場面の質問',
     '深いところ', 'これまでの人生', 'もう少しだけ', '迎える前に', '見た目', '声', '声で入力', '書き出し', '準備ができました']))


# Author copy that belongs to a part of a screen rather than to a step of the flow.
PARTS = {'import_help': '会話の書き出し方'}


def explanation(screen, root=ROOT / 'onboarding'):
    if screen not in TITLES and screen not in PARTS:
        raise HTTPException(404, '説明が見つかりません。')
    try:
        # Numbered in the order the screens appear, so the folder reads like the flow.
        name = f'{list(TITLES).index(screen):02d}_{screen}.md' if screen in TITLES else f'{screen}.md'
        raw = (root / name).read_bytes()
        text = raw.decode('utf-8-sig')
    except (OSError, UnicodeError):
        raw, text = b'', '' if screen in PARTS else '# ' + TITLES[screen]
    return {'text': text, 'version': hashlib.sha256(raw).hexdigest()}


def install_onboarding(app, data_dir, atomic_write, setup_helper_config):
    path = data_dir / 'settings.toml'

    def saved():
        with SETTINGS_LOCK:
            return tomllib.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}

    @app.get('/api/onboarding/state')
    def state():
        return {'consent': saved().get('onboarding', {}).get('consent')}

    @app.get('/api/onboarding/setup-helper')
    async def helper():
        from cbs.web.setup_helper import setup_helper
        return await setup_helper(setup_helper_config, data_dir,
                                  first_run=app.state.first_run)

    @app.get('/api/onboarding/{screen}')
    def copy(screen: str):
        return explanation(screen)

    @app.post('/api/onboarding/consent')
    def consent(body: dict):
        version = explanation('consent')['version']
        if body != {'version': version}:
            raise HTTPException(409, '前文が更新されました。もう一度読んでください。')
        with SETTINGS_LOCK:
            previous = saved()
            value = previous.get('onboarding', {}).get('consent') or {
                'at': datetime.now(timezone.utc).isoformat(), 'version': version}
            atomic_write(path, toml_text(overlay(previous, {'onboarding': {'consent': value}})))
        return {'consent': value}
