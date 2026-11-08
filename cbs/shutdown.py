"""R12: one shutdown clock, and cancellation at upkeep transaction boundaries."""
import asyncio
from contextvars import ContextVar
from threading import Event, Lock, Thread
from time import monotonic

SHUTDOWN_SECONDS = 19.0  # Uvicorn + lifespan + Python worker joins; below 20s.
GRACEFUL_SECONDS = 5
_upkeep_stop = ContextVar('upkeep_stop', default=None)


class UpkeepStopped(BaseException):
    """Do not record shutdown as an LLM failure or complete unfinished work."""


def checkpoint():
    stopped = _upkeep_stop.get()
    if stopped is not None and stopped.is_set():
        raise UpkeepStopped()


def run_upkeep(store, function, *args, **kwargs):
    token = _upkeep_stop.set(store.upkeep_stop)
    try:
        checkpoint()
        return function(*args, **kwargs)
    except UpkeepStopped:
        return None
    finally:
        _upkeep_stop.reset(token)


class Shutdown:
    def __init__(self, directory, stop):
        self.path = directory / 'shutdown.log'
        self.stop = stop
        self.started = None
        self.active = {}
        self.lock = Lock()
        self.finished = Event()
        self.exit_process = None  # Only the real server arms the process watchdog.

    def _write(self, name, seconds):
        # Names are constants at call sites; never serialize exception/request text.
        try:
            with self.path.open('a', encoding='ascii') as stream:
                stream.write(f'{name} {seconds:.3f}\n')
        except OSError:
            pass

    def begin(self):
        with self.lock:
            if self.started is not None:
                return
            self.started = monotonic()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text('', encoding='ascii')
            except OSError:
                pass
            self.stop.set()
            if self.exit_process is not None:
                Thread(target=self._watch, name='shutdown-deadline', daemon=True).start()

    def _watch(self):
        if not self.finished.wait(self.remaining()):
            with self.lock:
                for name, started in self.active.items():
                    self._write(name + '_cutoff', monotonic() - started)
                self._write('process_cutoff', monotonic() - self.started)
            # Provider/native calls cannot be killed safely as Python threads.
            # SQLite recovers uncommitted transactions on the next open.
            self.exit_process(0)

    def remaining(self):
        return max(0, SHUTDOWN_SECONDS - (monotonic() - self.started))

    def start(self, name):
        with self.lock:
            self.active[name] = monotonic()

    def end(self, name, suffix=''):
        with self.lock:
            started = self.active.pop(name, None)
            if started is not None:
                self._write(name + suffix, monotonic() - started)

    async def stage(self, name, work, *, limit=None):
        self.start(name)
        task = asyncio.ensure_future(work)
        timeout = self.remaining()
        if limit is not None:
            timeout = min(timeout, limit)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            self.end(name, '_cutoff')
            raise
        if not done:
            task.cancel()
            self.end(name, '_cutoff')
            return
        try:
            task.result()
        except (Exception, asyncio.CancelledError):
            self.end(name, '_error')
        else:
            self.end(name)

    def finish(self):
        with self.lock:
            self.finished.set()
            self._write('total', monotonic() - self.started)
