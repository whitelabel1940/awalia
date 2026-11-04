"""Local embedding acquisition using the M10b download-job lifecycle."""
from pathlib import Path

from cbs.llm.local import MODELS, download_model, model_files

# Sizes of the pinned text-only artifacts (K1 verification). Planning is offline.
FILE_SIZES = {
    'embeddinggemma-2-8bit': [5031, 32170510, 1599, 495165, 313724928],
    'embeddinggemma-2-4bit': [5031, 32170510, 1599, 490742, 174028800],
    'ruri-v3-30m': [1341, 6724873, 3779, 37142404],
    'ruri-v3-130m': [1342, 6724873, 3779, 133302950],
    'multilingual-e5-small': [655, 17082730, 443, 470268510],
}


class EmbeddingInstaller:
    def __init__(self, directory, tls_strict=True):
        self.directory = Path(directory)
        self.tls_strict = tls_strict

    def sizes(self, choice):
        return dict(zip(model_files(MODELS[choice]), FILE_SIZES[choice], strict=True))

    def plan(self, choice):
        spec = MODELS[choice]
        sizes = self.sizes(choice)
        directory = self.directory / spec.repo / spec.revision
        completed = 0
        ready = True
        for name, size in sizes.items():
            path = directory / name
            if path.is_file() and path.stat().st_size == size:
                completed += size
            else:
                ready = False
                partial = path.with_name(path.name + '.partial')
                completed += min(size, partial.stat().st_size) if partial.is_file() else 0
        total = sum(sizes.values())
        return {'choice': choice, 'model': choice, 'reason': 'ローカルで記憶を検索するためのモデルです。',
                'total': total, 'completed': completed, 'engine_bytes': 0, 'engine_completed': 0,
                'model_bytes': total, 'model_completed': completed,
                'download_bytes': max(0, total - completed), 'ready': ready}

    def acquire(self, choice, cancel, progress):
        spec = MODELS[choice]
        sizes = self.sizes(choice)
        directory = self.directory / spec.repo / spec.revision
        done = {}
        for name, size in sizes.items():
            target = directory / name
            partial = target.with_name(target.name + '.partial')
            done[name] = target.stat().st_size if target.is_file() else min(size, partial.stat().st_size) if partial.is_file() else 0

        def update(name, completed, total):
            done[name] = completed
            progress('model', sum(done.values()), sum(sizes.values()))

        download_model(spec, self.directory, self.tls_strict, update, cancel)
        if cancel.is_set():
            raise InterruptedError()
        if not self.plan(choice)['ready']:
            raise ValueError('Incomplete embedding artifacts')
        progress('model', sum(sizes.values()), sum(sizes.values()))
        return choice, '記憶の検索モデルを取得しました。'
