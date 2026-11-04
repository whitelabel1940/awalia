"""Per-turn monotonic timeline, shared only with that turn's workers."""
from cbs.llm.cost import for_step
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock
from time import perf_counter

current = ContextVar('turn_timeline', default=None)


class Timeline:
    def __init__(self):
        self.started = perf_counter()
        self.received_at = datetime.now(timezone.utc).isoformat()
        self.lock = Lock()
        self.events = []

    def mark(self, name, started=None, **details):
        end = perf_counter()
        with self.lock:
            self.events.append({'name': name, 'at_ms': round((end-self.started)*1000, 2),
                                **({'duration_ms': round((end-started)*1000, 2)} if started is not None else {}),
                                **details})

    def snapshot(self):
        with self.lock:
            return {'clock': 'monotonic; milliseconds since request received',
                    'received_at': self.received_at, 'events': list(self.events)}


@contextmanager
def span(name, **details):
    started = perf_counter()
    completed = False
    try:
        yield
        completed = True
    finally:
        if timeline := current.get():
            timeline.mark(name, started, state='completed' if completed else 'failed', **details)


def embed(pipeline, texts, *, role, purpose, model=None):
    with span('embedding', role=role, purpose=purpose, inputs=len(texts), calls=1):
        return for_step(pipeline.client, 'embedding', 'chat' if role == 'query' else 'memory').embed(texts, model or pipeline.config.models.embedding, role=role)
