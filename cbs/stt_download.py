"""Haas-ja acquisition through the shared speech.cpp installer and download job."""
from pathlib import Path

from cbs.tts import TTSError, release_directory, verified_archive
from cbs.tts_download import SpeechInstaller, catalog, pull_model, select_device


class HaasInstaller:
    def __init__(self, directory, config):
        self.directory = Path(directory)
        self.config = config
        self.device = config.stt.device
        self.engine = None

    def engine_name(self):
        return self.engine or self.device

    def auto_device(self):
        exe, binary, _ = SpeechInstaller(self.directory).engine_plan()
        return select_device(exe) if binary == 0 else 'auto'

    def probe_device(self, device=None):
        device = device or self.device
        self.engine = self.auto_device() if device == 'auto' else device

    def plan(self, choice):
        spec = self.config.stt.local_models[choice]
        exe, binary, update = SpeechInstaller(self.directory).engine_plan()
        info = catalog(exe, 'q8_0', spec.catalog) if binary == 0 else None
        size = info['size'] if info else spec.size
        completed = size if info and info['fetched'] and Path(info['path']).is_file() else info.get('partial', 0) if info else 0
        partial = release_directory(self.directory) / 'speech.zip.part'
        engine_done = min(partial.stat().st_size, binary) if partial.is_file() else 0
        reason = ('音声エンジンを 0.9.4 に更新して Haas-ja を取得します。' if update else
                  '音声エンジン speech.cpp と Haas-ja を取得します。' if binary else
                  'Haas-ja は日本語向けの高速な文字起こしです。この PC で動かします。')
        return dict(choice=choice, model=choice, engine=self.engine_name(), reason=reason,
                    total=binary + size, completed=engine_done + completed,
                    engine_bytes=binary, engine_completed=engine_done, model_bytes=size,
                    model_completed=completed, download_bytes=max(0, binary + size - engine_done - completed),
                    ready=binary == 0 and bool(info and info['fetched'] and Path(info['path']).is_file()), engine_update=update)

    def acquire(self, choice, cancel, progress):
        exe = verified_archive(self.directory, cancel=cancel, tls_strict=self.config.llm.tls_strict,
                               progress=lambda d, t: progress('engine', d, t))
        if cancel.is_set():
            raise InterruptedError()
        pull_model(exe, self.config.stt.local_models[choice].catalog, 'q8_0', cancel, progress)
        return choice, 'Haas-ja の準備ができました。日本語をこの PC で文字起こしします。'

    def verify(self, choice):
        if not self.plan(choice)['ready']:
            raise TTSError('設定から音声エンジンと Haas-ja を取得してください。')
        exe = verified_archive(self.directory, allow_download=False)
        info = catalog(exe, 'q8_0', self.config.stt.local_models[choice].catalog)
        if not info['fetched'] or not Path(info['path']).is_file():
            raise TTSError('設定から Haas-ja を再取得してください。')
        return exe, info['path'], select_device(exe, self.device)
