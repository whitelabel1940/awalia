"""Small text-only ONNX embedding runner; no torch/transformers imports.

Revisions and outputs are pinned: changing an artifact requires a new model alias
so embedding_index can detect a change of vector space.
"""
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
import os
import logging
import re
import ssl
import time
from threading import Lock
import urllib.request
from urllib.error import HTTPError

from cbs.llm import LLMError


@dataclass(frozen=True)
class ModelSpec:
    repo: str
    revision: str
    file: str
    dimension: int
    query_prefix: str
    document_prefix: str
    output: str
    pooling: str
    max_length: int
    external: bool = False
    archived_margin: float = 0.08


GEMMA_REPO = "onnx-community/embeddinggemma-2-ONNX"
GEMMA_REV = "daa72c51243991dfcaf9f9137d2c573d8f7790c0"
MODELS = {
    "embeddinggemma-2-8bit": ModelSpec(GEMMA_REPO, GEMMA_REV, "onnx/model_quantized.onnx", 768,
        "task: search result | query: ", "title: none | text: ", "sentence_embedding", "pooled", 8192, True),
    "embeddinggemma-2-4bit": ModelSpec(GEMMA_REPO, GEMMA_REV, "onnx/model_q4.onnx", 768,
        "task: search result | query: ", "title: none | text: ", "sentence_embedding", "pooled", 8192, True),
    "ruri-v3-30m": ModelSpec("sirasagi62/ruri-v3-30m-ONNX", "cdf9391f1ff2198daa8f63f7ccf97d7b3e7415a0",
        "onnx/model_quantized.onnx", 256, "検索クエリ: ", "検索文書: ", "sentence_embedding", "pooled", 8192),
    "ruri-v3-130m": ModelSpec("sirasagi62/ruri-v3-130m-ONNX", "8c13617ef68a165a9a893cbc153e0fc9b3aaf6e9",
        "onnx/model_quantized.onnx", 512, "検索クエリ: ", "検索文書: ", "sentence_embedding", "pooled", 8192,
        archived_margin=0.06),
    "multilingual-e5-small": ModelSpec("intfloat/multilingual-e5-small", "614241f622f53c4eeff9890bdc4f31cfecc418b3",
        "onnx/model.onnx", 384, "query: ", "passage: ", "last_hidden_state", "mean", 512),
}


def model_files(spec):
    return ["config.json", "tokenizer.json", "tokenizer_config.json", spec.file] + ([spec.file + "_data"] if spec.external else [])


@contextmanager
def download_lock(directory, cancel=None):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".download.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            while True:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError()
                try:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def download_model(spec, root, tls_strict=True, progress=None, cancel=None):
    """Allowlisted files only. Resume partial downloads; publish complete files atomically."""
    directory = Path(root) / spec.repo / spec.revision
    with download_lock(directory, cancel):
        return _download_model(spec, directory, tls_strict, progress, cancel)


def _download_model(spec, directory, tls_strict, progress, cancel=None):
    context = ssl.create_default_context()
    if not tls_strict:
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    for filename in model_files(spec):
        if cancel is not None and cancel.is_set():
            raise InterruptedError()
        target = directory / filename
        if target.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        offset = partial.stat().st_size if partial.exists() else 0
        request = urllib.request.Request(f"https://huggingface.co/{spec.repo}/resolve/{spec.revision}/{filename}",
                                         headers={"Range": f"bytes={offset}-"} if offset else {})
        if progress:
            progress(filename, offset, None)
        logging.getLogger(__name__).info("Downloading embedding model %s: %s", spec.repo, filename)
        try:
            response = urllib.request.urlopen(request, context=context, timeout=120)
        except HTTPError as error:
            # A crash can occur after the last byte but before atomic publication.
            if error.code == 416 and offset and error.headers.get("Content-Range") == f"bytes */{offset}":
                error.close()
                partial.replace(target)
                continue
            raise
        with response:
            append = offset > 0 and response.status == 206
            content_range = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", "")) if append else None
            if append and (not content_range or int(content_range[1]) != offset):
                raise LLMError("モデルのダウンロード再開位置が一致しません。再試行してください。")
            done = offset if append else 0
            size = response.headers.get("Content-Length")
            total = int(content_range[3]) if append else (int(size) if size else None)
            with partial.open("ab" if append else "wb") as stream:
                while chunk := response.read(1024 * 1024):
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError()
                    stream.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(filename, done, total)
            if total is not None and done != total:
                raise LLMError("モデルのダウンロードが中断されました。再試行してください。")
        partial.replace(target)
    return directory


class LocalEmbeddingClient:
    def __init__(self, config, *, models_dir=None, progress=None):
        self.root = Path(models_dir or config._data_dir / 'models')
        self.tls_strict = config.llm.tls_strict
        self.progress = progress
        self._runners = {}
        self._runner_lock = Lock()

    def sanitize_text(self, text):
        return text

    def _runner(self, model):
        with self._runner_lock:
            return self._load_runner(model)

    def _load_runner(self, model):
        if model not in MODELS:
            raise LLMError("未対応のローカル埋め込みモデルです。cbs.llm.local.MODELS を確認してください。")
        if model not in self._runners:
            try:
                import numpy as np
                import onnxruntime as ort
                from tokenizers import Tokenizer
            except ImportError:
                raise LLMError('ローカル埋め込みには .venv/Scripts/python.exe -m pip install ".[local-embedding]" が必要です。') from None
            spec = MODELS[model]
            try:
                directory = download_model(spec, self.root, self.tls_strict, self.progress)
                tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
                tokenizer.enable_truncation(max_length=spec.max_length)
                # Run one text at a time: no padding-dependent pooling or large batch peaks.
                tokenizer.no_padding()
                options = ort.SessionOptions()
                options.intra_op_num_threads = 4
                session = ort.InferenceSession(str(directory / spec.file), sess_options=options,
                                               providers=["CPUExecutionProvider"])
                outputs = {o.name: o for o in session.get_outputs()}
                if spec.output not in outputs:
                    raise ValueError("unexpected ONNX outputs")
                self._runners[model] = (np, tokenizer, session)
            except Exception:
                raise LLMError("ローカル埋め込みモデルの取得・読み込みに失敗しました。接続と data/models を確認してください。") from None
        return self._runners[model]

    def embed(self, texts, model, *, role="document"):
        if role not in ("query", "document"):
            raise ValueError("embedding role must be query or document")
        if not texts:
            return []
        np, tokenizer, session = self._runner(model)
        spec = MODELS[model]
        prefix = spec.query_prefix if role == "query" else spec.document_prefix
        vectors = []
        for text in texts:
            tokens = tokenizer.encode(prefix + text)
            ids = np.asarray([tokens.ids], dtype=np.int64)
            mask = np.asarray([tokens.attention_mask], dtype=np.int64)
            available = {"input_ids": ids, "attention_mask": mask,
                         "token_type_ids": np.asarray([tokens.type_ids], dtype=np.int64),
                         "position_ids": np.arange(ids.shape[1], dtype=np.int64)[None, :],
                         # Text-only EmbeddingGemma 2 still declares these inputs.
                         "image_features": np.empty((0, 512), dtype=np.float32),
                         "video_features": np.empty((0, 512), dtype=np.float32),
                         "audio_features": np.empty((0, 512), dtype=np.float32)}
            try:
                feed = {i.name: available[i.name] for i in session.get_inputs()}
                value = session.run([spec.output], feed)[0].astype(np.float32)
                if spec.pooling == "mean":
                    if value.ndim != 3:
                        raise ValueError("expected token embeddings")
                    value = (value * mask[..., None]).sum(axis=1) / mask.sum(axis=1, keepdims=True)
                if value.shape != (1, spec.dimension) or not np.isfinite(value).all():
                    raise ValueError("invalid sentence embedding")
                norm = np.linalg.norm(value, axis=1, keepdims=True)
                if (norm <= 0).any():
                    raise ValueError("zero embedding")
                vectors.append((value / norm).astype(np.float32)[0].tolist())
            except Exception:
                raise LLMError("ローカル埋め込みの計算に失敗しました。ONNX の入出力を確認してください。") from None
        return vectors
