"""Numeric usage at the LLM boundary; purposes are explicit caller steps."""
from contextvars import ContextVar
from functools import wraps

step = ContextVar('cost_step', default=('other', 'other'))
usage = ContextVar('cost_usage', default=None)


def report_usage(value):
    """For APIs returning plain vectors/text instead of LLMResult."""
    if usage.get() is not None:
        usage.set(value or {})


def report_response(response, field='usage'):
    """Optional accounting metadata must never break a successful API call."""
    try:
        value = getattr(response, field, None)
        report_usage(value.model_dump(mode='json', exclude_none=True) if value else {})
    except Exception:
        report_usage({})


class StepClient:
    def __init__(self, client, role, purpose):
        self.client, self.role, self.purpose = client, role, purpose

    def __getattr__(self, name):
        method = getattr(self.client, name)
        def call(*args, **kwargs):
            recorder = getattr(self.client, 'costs', None)
            if self.purpose == 'autonomy' and recorder is not None and recorder.paused():
                from cbs.llm import LLMError
                raise LLMError('月の目安額を超えたため自主活動を休止しています。')
            token = step.set((self.role, self.purpose))
            try:
                return method(*args, **kwargs)
            finally:
                step.reset(token)
        return call


def for_step(client, role, purpose):
    return StepClient(client, role, purpose)


def metered(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        recorder = getattr(self, 'costs', None)
        if recorder is None and getattr(self, '_cost_default', False):
            # Standalone CLI calls have no pipeline to attach the main DB.
            # Application pipelines bind their Store before any inference.
            try:
                from cbs.store import Store
                from cbs.costs import bind
                bind(self, Store(self._config._data_dir / 'cbs.sqlite3'), self._config)
                recorder = self.costs
            except Exception:
                pass
        if recorder is None:
            return method(self, *args, **kwargs)
        # Fake methods delegate to one another. Record the outer request once.
        if usage.get() is not None:
            return method(self, *args, **kwargs)
        token = usage.set({})
        result = error = None
        try:
            result = method(self, *args, **kwargs)
            return result
        except Exception as exc:
            error = exc
            raise
        finally:
            try:
                model = kwargs.get('model') or (args[1] if method.__name__ == 'embed' and len(args) > 1 else
                                               args[0] if method.__name__ != 'embed' and args else None)
                model = model or self._cost_config.models.embedding
                from cbs.llm import RoutingLLMClient
                provider, bare = RoutingLLMClient(self._cost_config)._split(model)
                data = getattr(result, 'usage', None) or usage.get()
                if error is not None and getattr(error, 'trace', None):
                    data = error.trace.get('usage') or data
                role, purpose = step.get()
                recorder.record(provider, self.sanitize_text(provider + '/' + bare), role, purpose, data)
            except Exception:
                # Accounting can never fail a response, nor copy an exception/body.
                pass
            finally:
                usage.reset(token)
    return call
