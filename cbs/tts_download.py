"""Explicit, cancellable speech installation. No downloads on the playback path."""
import json
import hashlib
from pathlib import Path
import subprocess
import threading
import time
from uuid import uuid4

from cbs.tts import ASSET, CREATE_FLAGS, TTSError, release_directory, verified_archive

# Conservative engine allowance; catalog sizes replace model estimates after installation.
ARCHIVE_BYTES = 32 * 1024 * 1024
MODEL_BYTES = {'q8_0': 1199474528, 'q4_k': 859871072}


def catalog(exe, quant, model_name='irodori-tts'):
    result = subprocess.run([str(exe), 'models', '--json'], capture_output=True, timeout=30,
                            check=True, creationflags=CREATE_FLAGS)
    model = next(m for m in json.loads(result.stdout)['models'] if m['name'] == model_name)
    return next(f for f in model['files'] if f['type'] == quant)


def select_device(exe, device='auto'):
    if device != 'auto':
        return device
    result = subprocess.run([str(exe), 'devices'], capture_output=True, timeout=30,
                            check=True, creationflags=CREATE_FLAGS)
    gpu = any('gpu' in line.split() for line in result.stdout.decode('utf-8', errors='replace').splitlines())
    return 'gpu' if gpu else 'cpu'


def select_model(exe, choice='auto', device='auto'):
    actual = select_device(exe, device)
    quant = ('q8_0' if actual == 'gpu' else 'q4_k') if choice == 'auto' else choice
    reason = ('GPU が使えるため q8 を選びました。' if actual == 'gpu' else 'CPU で動かすため軽い q4 を選びました。') if choice == 'auto' else '指定されたモデルを使います。'
    return actual, quant, reason


def installed_model(exe, quant):
    info = catalog(exe, quant)
    if not info['fetched'] or not Path(info['path']).is_file():
        raise TTSError('設定の声から音声モデルを取得してください。')
    return info['path']


class SpeechInstaller:
    """Replace this boundary for offline tests; speech pull owns model resume/verification."""
    def __init__(self, directory):
        self.directory = Path(directory)

    def engine_plan(self):
        directory = release_directory(self.directory)
        exe = directory / 'bin' / 'speech.exe'
        archive = directory / 'speech.zip'
        valid = False
        try:
            sums = (directory / 'SHA256SUMS').read_text()
            digest = next(line.split()[0] for line in sums.splitlines() if line.split()[-1].lstrip('*') == ASSET)
            with archive.open('rb') as stream:
                valid = hashlib.file_digest(stream, 'sha256').hexdigest() == digest.lower()
        except (OSError, StopIteration, IndexError):
            pass
        if valid:
            # Repair/verify extracted files before executing the CLI, including during planning.
            exe = verified_archive(self.directory, allow_download=False)
        binary = 0 if valid and exe.is_file() else ARCHIVE_BYTES
        update = directory != self.directory and binary != 0
        return exe, binary, update

    def plan(self, choice):
        exe, binary, update = self.engine_plan()
        quant = choice
        reason = '取得後に GPU を調べます。自動では q8 の容量を上限として確認します。' if choice == 'auto' else '指定されたモデルを使います。'
        info = None
        if binary == 0:
            _, quant, reason = select_model(exe, choice)
            info = catalog(exe, quant)
        elif choice == 'auto':
            quant = 'q8_0'
        if update:
            reason = '音声エンジンを 0.9.4 に更新してください。取得ボタンで更新できます。既存の声とモデルは引き継ぎます。'
        total = binary + (info['size'] if info else MODEL_BYTES[quant])
        partial = release_directory(self.directory) / 'speech.zip.part'
        done = min(partial.stat().st_size, binary) if partial.is_file() else 0
        engine_done = done
        model_done = 0
        if info:
            model_done = info['size'] if info['fetched'] else info.get('partial', 0)
            done += model_done
        return {'choice': choice, 'model': quant, 'reason': reason, 'total': total, 'completed': done,
                'engine_bytes': binary, 'model_bytes': info['size'] if info else MODEL_BYTES[quant],
                'engine_completed': engine_done, 'model_completed': model_done,
                'download_bytes': max(0, total - done), 'ready': binary == 0 and bool(info and info['fetched']),
                'engine_update': update}

    def acquire(self, choice, cancel, progress):
        exe = verified_archive(self.directory, cancel=cancel,
                               progress=lambda done, total: progress('engine', done, total or ARCHIVE_BYTES))
        if cancel.is_set():
            raise InterruptedError()
        _, quant, reason = select_model(exe, choice)
        pull_model(exe, 'irodori-tts', quant, cancel, progress)
        return quant, reason


def pull_model(exe, model_name, quant, cancel, progress):
    # Never remove completed models. pull validates/reuses them and resumes its partials.
    process = subprocess.Popen([str(exe), 'pull', model_name + (':' + quant if model_name == 'irodori-tts' else '')],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_FLAGS)
    try:
        while True:
            info = catalog(exe, quant, model_name)
            progress('model', info['size'] if info['fetched'] else info.get('partial', 0), info['size'])
            if cancel.is_set():
                raise InterruptedError()
            code = process.poll()
            if code is not None:
                info = catalog(exe, quant, model_name)
                if code != 0 or not info['fetched']:
                    raise TTSError('モデルを取得できませんでした。通信状態を確認して再開してください。')
                break
            cancel.wait(.5)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


class DownloadJob:
    def __init__(self, installer, *, name='音声'):
        self.installer = installer
        self.name = name
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.thread = None
        self.quotes = {}
        self.state = None

    def quote(self, choice):
        plan = self.installer.plan(choice)
        token = uuid4().hex
        with self.lock:
            self.quotes = {token: plan}
        return {**plan, 'token': token}

    def status(self, choice):
        with self.lock:
            if self.state and (self.state['choice'] == choice or self.state['status'] == 'downloading'):
                return dict(self.state)
        plan = self.installer.plan(choice)
        return {**plan, 'status': 'ready' if plan['ready'] else 'missing', 'percent': round(100 * plan['completed'] / plan['total'], 1), 'eta_seconds': None}

    def start(self, token, confirmed):
        with self.lock:
            if not confirmed or token not in self.quotes:
                raise TTSError('取得する容量を確認してから開始してください。')
            if self.thread and self.thread.is_alive():
                raise TTSError(f'{self.name}の取得はすでに進んでいます。')
            plan = self.quotes.pop(token)
            self.cancelled.clear()
            self.state = {**plan, 'status': 'downloading', 'stage': 'engine',
                          'percent': round(100 * plan['completed'] / plan['total'], 1), 'eta_seconds': None, 'error': None}
            self.thread = threading.Thread(target=self._work, args=(plan,), daemon=True)
            self.thread.start()
            return dict(self.state)

    def _work(self, plan):
        started, baseline = time.monotonic(), {}
        def progress(stage, done, total):
            with self.lock:
                baseline.setdefault(stage, done)
                elapsed = time.monotonic() - started
                speed = (done - baseline[stage]) / elapsed if elapsed > 0 else 0
                engine = plan.get('engine_bytes', 0)
                model = plan.get('model_bytes', plan['total'])
                if stage == 'engine':
                    engine = total or engine
                    plan['engine_bytes'] = engine
                    completed, overall = done + plan.get('model_completed', 0), engine + model
                else:
                    completed, overall = engine + done, engine + (total or model)
                self.state.update(stage=stage, completed=completed, total=overall,
                                  percent=round(min(100, 100 * completed / overall), 1) if overall else None,
                                  eta_seconds=round((overall - completed) / speed) if overall and speed > 0 else None)
        try:
            quant, reason = self.installer.acquire(plan['choice'], self.cancelled, progress)
            with self.lock:
                self.state.update(status='cancelled' if self.cancelled.is_set() else 'ready', model=quant, reason=reason,
                                  completed=self.state['total'], percent=100, eta_seconds=0, ready=True)
        except InterruptedError:
            with self.lock:
                self.state.update(status='cancelled', eta_seconds=None)
        except Exception:
            with self.lock:
                self.state.update(status='cancelled' if self.cancelled.is_set() else 'error',
                                  error=None if self.cancelled.is_set() else '取得に失敗しました。通信状態と空き容量を確認して再開してください。', eta_seconds=None)

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            if self.state and self.state['status'] == 'downloading':
                self.state['stage'] = 'cancelling'

    def close(self):
        self.cancel()
        if self.thread:
            self.thread.join(timeout=40)
