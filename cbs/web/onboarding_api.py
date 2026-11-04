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


def explanation(screen, root=ROOT / 'onboarding'):
    if screen not in TITLES:
        raise HTTPException(404, '説明が見つかりません。')
    try:
        # Numbered in the order the screens appear, so the folder reads like the flow.
        raw = (root / f'{list(TITLES).index(screen):02d}_{screen}.md').read_bytes()
        text = raw.decode('utf-8-sig')
    except (OSError, UnicodeError):
        raw, text = b'', '# ' + TITLES[screen]
    return {'text': text, 'version': hashlib.sha256(raw).hexdigest()}


def install_onboarding(app, data_dir, atomic_write):
    path = data_dir / 'settings.toml'

    def saved():
        with SETTINGS_LOCK:
            return tomllib.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}

    @app.get('/api/onboarding/state')
    def state():
        return {'consent': saved().get('onboarding', {}).get('consent')}

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
