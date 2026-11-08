"""Provider health shared by foreground/background calls and browser sessions."""
from contextvars import ContextVar
from functools import wraps
from threading import Lock

from cbs.llm.errors import LLMError, PROVIDERS, failure_notice

_active = ContextVar('llm_failure_observation', default=False)


class FailureState:
    def __init__(self, store):
        self.store = store
        self.lock = Lock()
        with store.connect() as db:
            self.providers = {row[0] for row in db.execute('SELECT provider FROM llm_failures')}

    def record(self, provider, error=None):
        if provider not in PROVIDERS:
            return
        with self.lock:
            if error is None and provider in self.providers:
                with self.store.connect() as db:
                    db.execute('DELETE FROM llm_failures WHERE provider=?', (provider,))
                self.providers.discard(provider)
            elif error is not None and error.kind in ('balance', 'limit', 'auth'):
                with self.store.connect() as db:
                    db.execute('INSERT INTO llm_failures(provider,kind,status_code) VALUES(?,?,?) '
                               'ON CONFLICT(provider) DO UPDATE SET kind=excluded.kind,status_code=excluded.status_code',
                               (provider, error.kind, error.status_code))
                self.providers.add(provider)

    def forget(self, provider):
        """A new key replaces the one that failed; the next call reports its own health."""
        with self.lock:
            with self.store.connect() as db:
                db.execute('DELETE FROM llm_failures WHERE provider=?', (provider,))
            self.providers.discard(provider)

    def notices(self, display_name, config=None):
        with self.store.connect() as db:
            rows = db.execute('SELECT provider,kind,status_code FROM llm_failures ORDER BY provider').fetchall()
        # A provider the current settings no longer use cannot stop the partner (e.g. Gemini
        # failed before a restored PC was connected to OpenRouter); keep the row, hide the banner.
        used = providers_in_use(config) if config is not None else None
        return [failure_notice(LLMError('', provider=r['provider'], kind=r['kind'], status_code=r['status_code']),
                               display_name, banner=True) for r in rows if used is None or r['provider'] in used]


def providers_in_use(config):
    from cbs.llm import RoutingLLMClient
    route = RoutingLLMClient(config)
    return {route._split(model)[0] for model in config.models.model_dump().values() if model}


def observed(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        if _active.get():
            return method(self, *args, **kwargs)
        token = _active.set(True)
        error = None
        try:
            return method(self, *args, **kwargs)
        except LLMError as exc:
            error = exc
            # Bind the configured route also for Fake and local validation failures.
            if exc.provider is None and hasattr(self, '_cost_config'):
                from cbs.llm import RoutingLLMClient
                model = kwargs.get('model') or (args[1] if method.__name__ == 'embed' and len(args) > 1 else
                                               args[0] if method.__name__ != 'embed' and args else None)
                exc.provider = RoutingLLMClient(self._cost_config)._split(model or self._cost_config.models.embedding)[0]
                if exc.provider not in ('openrouter', 'openai', 'anthropic', 'gemini', 'local'):
                    exc.provider = None
                exc.trace.update(exc.metadata())
            raise
        except Exception as exc:
            error = exc
            raise
        finally:
            try:
                if hasattr(self, 'failures'):
                    from cbs.llm import RoutingLLMClient
                    model = kwargs.get('model') or (args[1] if method.__name__ == 'embed' and len(args) > 1 else
                                                   args[0] if method.__name__ != 'embed' and args else None)
                    provider = RoutingLLMClient(self._cost_config)._split(model or self._cost_config.models.embedding)[0]
                    if error is None or isinstance(error, LLMError):
                        self.failures.record(getattr(error, 'provider', None) or provider, error)
            except Exception:
                # Health persistence must never replace a reply or its original error.
                pass
            finally:
                _active.reset(token)
    return call
