"""Opt-in, fixed-schema usage events. This is the only PostHog boundary."""
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import platform
import re
from threading import RLock
import tomllib
from uuid import uuid4

import httpx

from cbs import clock
from cbs.config import ROOT
from cbs.news import current_version

# Keep exactly the same set as onboarding.js ONBOARDING_STEPS.
ONBOARDING_STEPS = frozenset('''welcome ai method oauth combination kind actor role-actor role-judge role-embedding role-research role-vision download keys zdr check summary partner
bringing profile import world partner-fields user-fields now likes now-question scenes scenes-question
open-style depth depth-question life life-question more more-question checks review confirmation review-wait
look look-style look-profile look-face look-hair look-outfit look-confirm look-sheet look-single look-faces
look-cover look-cover-import voice voice-autoplay voice-engine voice-download voice-method voice-description
voice-reference voice-url voice-model voice-name voice-style voice-confirm voice_input input-model
input-download input-device input-options input-confirm export export-trial export-options export-confirm done
entry-summary entry-actor entry-custom'''.split())
STAGES = frozenset('reading questions building validation review saving'.split())
WHERE = frozenset('reply bootstrap import backup voice autonomy server'.split())
PROVIDERS = frozenset('gemini openai anthropic openrouter local'.split())
ROLES = ('actor', 'judge', 'steward', 'embedding', 'research', 'vision', 'stt')


def explanation():
    try:
        raw = (ROOT / 'onboarding' / 'telemetry.md').read_bytes()
        text = raw.decode('utf-8-sig').strip()
    except (OSError, UnicodeError):
        raw, text = b'', ''
    return {'text': text, 'version': hashlib.sha256(raw).hexdigest()}


async def send_batch(host, key, batch):
    async with httpx.AsyncClient(timeout=4, follow_redirects=False) as client:
        response = await client.post(host.rstrip('/') + '/batch/', json={'api_key': key, 'batch': batch})
        response.raise_for_status()


def record_error(store, where, error, stage=None):
    telemetry = getattr(store, 'telemetry', None)
    if telemetry is not None:
        try:
            telemetry.error(where, error, stage)
        except (OSError, ValueError, KeyError, TypeError):
            pass


class Telemetry:
    def __init__(self, config, data_dir, store, *, first_run=False):
        self.config = config
        self.root = Path(data_dir) / 'telemetry'
        self.settings_path = Path(data_dir) / 'settings.toml'
        self.store = store
        self.first_run = first_run
        self.lock = RLock()
        self.sending = asyncio.Lock()
        self.current = current_version()
        self.consent = self._settings().get('telemetry', {})

    def _settings(self):
        return tomllib.loads(self.settings_path.read_text(encoding='utf-8')) if self.settings_path.is_file() else {}

    def usable(self):
        return bool(self.config.telemetry.key and not self.first_run and
                    (not (ROOT / '.git').exists() or os.environ.get('AWALIA_TELEMETRY_DEV') == '1'))

    def active(self):
        return self.usable() and self.consent.get('enabled') is True and (self.root / 'id').is_file()

    def snapshot(self):
        with self.lock:
            return {'enabled': self.consent.get('enabled') is True and (self.root / 'id').is_file(), 'available': self.usable(),
                    'configured': bool(self.config.telemetry.key), **explanation(),
                    'privacy_url': self.config.telemetry.privacy_url}

    def set_enabled(self, enabled, version):
        from cbs.web.settings_api import SETTINGS_LOCK, atomic_write, toml_text
        with self.lock, SETTINGS_LOCK:
            if enabled and version != explanation()['version']:
                from fastapi import HTTPException
                raise HTTPException(409, '説明が更新されました。もう一度読んでください。')
            if enabled and not self.usable():
                return self.snapshot()
            previous = self._settings()
            if enabled and not self.active():
                self.root.mkdir(parents=True, exist_ok=True)
                atomic_write(self.root / 'id', str(uuid4()))
                self.consent = {'enabled': True, 'at': clock.iso(), 'version': version}
            elif not enabled:
                self.consent = {**self.consent, 'enabled': False}
                for name in ('id', 'queue.jsonl', 'state.json'):
                    (self.root / name).unlink(missing_ok=True)
            previous['telemetry'] = self.consent
            atomic_write(self.settings_path, toml_text(previous))
            return self.snapshot()

    def _read(self, name, default):
        try:
            text = (self.root / name).read_text(encoding='utf-8')
            return [json.loads(line) for line in text.splitlines() if line] if name.endswith('.jsonl') else json.loads(text)
        except (OSError, ValueError):
            return default

    def _write(self, name, value):
        from cbs.web.settings_api import atomic_write
        self.root.mkdir(parents=True, exist_ok=True)
        text = ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in value) if name.endswith('.jsonl') else json.dumps(value)
        atomic_write(self.root / name, text)

    def _safe(self, value, pattern):
        secrets = [self.config.telemetry.key]
        names = [self.config.llm.api_key_env, *(p.api_key_env for p in self.config.llm.providers.values())]
        secrets.extend(os.environ.get(name, '') for name in names if name)
        return value if isinstance(value, str) and re.fullmatch(pattern, value) and not any(s and s in value for s in secrets) else 'other'

    def _event(self, name, fields, unique=None):
        # Callers provide only explicitly constructed, validated fields.
        with self.lock:
            if not self.active():
                return
            state = self._read('state.json', {})
            today = clock.now().date()
            state = {k: v for k, v in state.items() if v >= (today - timedelta(days=30)).isoformat()}
            if unique and unique in state:
                return
            event = {'event': name, 'timestamp': clock.iso(),
                     'properties': {**fields, 'version': self.current, '$process_person_profile': False,
                                    '$geoip_disable': True, 'distinct_id': (self.root / 'id').read_text(encoding='utf-8')}}
            queue = self._read('queue.jsonl', [])
            queue.append(event)
            self._write('queue.jsonl', queue)
            if unique:
                state[unique] = today.isoformat()
                self._write('state.json', state)

    def app_started(self):
        if not self.active():
            return
        ai = {}
        for role in ROLES:
            value = getattr(self.config.models, role)
            if not value:
                continue
            provider, sep, model = value.partition('/')
            if not sep or provider not in {*self.config.llm.providers, self.config.llm.provider, 'gemini', 'local'}:
                provider, model = self.config.llm.provider, value
            provider = provider if provider in PROVIDERS else 'other'
            model = self._safe(model, r'[A-Za-z0-9._:/-]{1,80}')
            ai[role] = f'{provider}/{model}'
        self._event('app_started', {'os': platform.system() + ' ' + platform.version()
                    if platform.system() == 'Windows' else platform.system() + ' ' + platform.release(), 'ai': ai},
                    'app:' + clock.now().date().isoformat())

    def step(self, step, first):
        if step in ONBOARDING_STEPS and type(first) is bool:
            self._event('onboarding_step', {'step': step, 'first': first})

    def share(self, where, service):
        if where in {'start', 'diary'} and service in {'x', 'bluesky', 'misskey', 'save', 'copy'}:
            self._event('share', {'where': where, 'service': service})

    def error(self, where, error, stage=None):
        if where not in WHERE or not self.active():
            return
        name = self._safe(type(error).__name__, r'[A-Za-z_][A-Za-z0-9_.]{0,80}')
        fields = {'where': where, 'type': name}
        if where == 'bootstrap' and stage in STAGES:
            fields['stage'] = stage
        try:
            self._event('error', fields, f'error:{clock.now().date().isoformat()}:{where}:{name}')
        except OSError:
            pass  # Usage recording must never replace the original failure.

    def daily_turns(self):
        with self.lock:
            if not self.active():
                return
            today = clock.now().date()
            first = max(clock.parse(self.consent['at']).date() + timedelta(days=1), today - timedelta(days=7))
            # Only counts leave SQLite; no text, trace, partner identity or lengths are read.
            with self.store.connect() as db:
                rows = db.execute("SELECT date(created_at, 'localtime') AS day, COUNT(*) AS turns FROM turns "
                                  "WHERE kind='conversation' AND user_text<>'' AND created_at>=? "
                                  "GROUP BY day", ((first - timedelta(days=1)).isoformat(),)).fetchall()
            for row in rows:
                day = row['day']
                if first.isoformat() <= day < today.isoformat():
                    self._event('daily_turns', {'date': day, 'turns': row['turns']}, 'turns:' + day)

    def sent(self):
        with self.lock:
            return list(reversed(self._read('sent.jsonl', [])))

    def _prepare_batch(self):
        with self.lock:
            if not self.active():
                return None
            self.daily_turns()
            cutoff = clock.now() - timedelta(days=30)
            batch = [e for e in self._read('queue.jsonl', []) if clock.parse(e['timestamp']) >= cutoff]
            self._write('queue.jsonl', batch)
            return (self.root / 'id').read_text(encoding='utf-8'), batch

    def _complete_batch(self, identity, batch):
        with self.lock:
            if not self.active() or (self.root / 'id').read_text(encoding='utf-8') != identity:
                return
            self._write('sent.jsonl', (self._read('sent.jsonl', []) + batch)[-200:])
            self._write('queue.jsonl', self._read('queue.jsonl', [])[len(batch):])

    async def flush(self):
        async with self.sending:
            try:
                prepared = await asyncio.to_thread(self._prepare_batch)
                if prepared is None:
                    return
                identity, batch = prepared
                if not batch:
                    return
                await asyncio.wait_for(send_batch(self.config.telemetry.host, self.config.telemetry.key, batch), timeout=5)
                await asyncio.to_thread(self._complete_batch, identity, batch)
            except Exception:
                pass  # Best effort: a failed send is retried next hour and never stops the loop.

    async def run(self):
        while True:
            # The app stays resident for days; the per-day key keeps this to one per day.
            try:
                await asyncio.to_thread(self.app_started)
            except OSError:
                pass
            await self.flush()
            await asyncio.sleep(3600)


def install_telemetry(app, pipeline, data_dir):
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, ConfigDict, StrictBool

    class ConsentInput(BaseModel):
        model_config = ConfigDict(extra='forbid')
        enabled: StrictBool
        version: str = ''

    class StepInput(BaseModel):
        model_config = ConfigDict(extra='forbid')
        step: str
        first: StrictBool

    class ShareInput(BaseModel):
        model_config = ConfigDict(extra='forbid')
        where: str
        service: str

    telemetry = Telemetry(pipeline.config, data_dir, pipeline.store, first_run=app.state.first_run)
    app.state.telemetry = pipeline.store.telemetry = telemetry

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, error):
        # FastAPI's default validation response echoes rejected values, including
        # a credential accidentally pasted into an unknown field or a wrong type.
        if request.url.path.startswith('/api/telemetry'):
            return JSONResponse({'detail': '入力を確認してください。'}, status_code=422)
        return await request_validation_exception_handler(request, error)

    @app.get('/api/telemetry')
    def get_telemetry():
        return telemetry.snapshot()

    @app.post('/api/telemetry')
    def consent(body: ConsentInput):
        return telemetry.set_enabled(body.enabled, body.version)

    @app.get('/api/telemetry/sent')
    def sent():
        return telemetry.sent()

    @app.post('/api/telemetry/step')
    def step(body: StepInput):
        try:
            telemetry.step(body.step, body.first)
        except OSError:
            pass
        return {'ok': True}

    @app.post('/api/telemetry/share')
    def share(body: ShareInput):
        try:
            telemetry.share(body.where, body.service)
        except OSError:
            pass
        return {'ok': True}

    @app.middleware('http')
    async def server_errors(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception as error:
            if request.url.path.startswith('/api/'):
                telemetry.error('server', error)
            raise  # Keep the server's own 500 response and traceback in the log.
        if response.status_code == 500 and request.url.path.startswith('/api/'):
            telemetry.error('server', RuntimeError())
        return response
