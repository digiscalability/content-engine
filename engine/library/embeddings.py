"""Text embeddings (C12): `BAAI/bge-small-en-v1.5` for scripts (FR-QA-3 now; transcript
chunks with FR-UND-3 later). ADR 0018.

The model runs on onnxruntime with its own tokenizer, from the local Hugging Face cache at a
pinned revision: no network at run time. `ce models fetch` downloads it once. Vectors are
unit length, so a dot product is the cosine. `FakeEmbedder` (hashed words) stands in for it
in unit tests and dry runs.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Protocol

import numpy as np

from engine.library.models import PINNED, ModelMissing

MODEL_REPO = PINNED["bge"].repo
MODEL_REVISION = PINNED["bge"].revision
MODEL_FILES = PINNED["bge"].files
MAX_TOKENS = 512
EmbeddingModelMissing = ModelMissing  # the name earlier slices use


class Embedder(Protocol):
    name: str

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """One unit-length float32 row per text."""
        ...


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def to_blob(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype="<f4").tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4").astype(np.float32)


def _normalise(rows: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    return (rows / np.where(norms == 0, 1, norms)).astype(np.float32)


def model_files(cache_dir: Path | None = None) -> dict[str, Path]:
    """The cached model files; `EmbeddingModelMissing` if any is absent."""
    return PINNED["bge"].paths(cache_dir)


def fetch_model(cache_dir: Path | None = None) -> list[Path]:
    """Download the pinned model files (about 134 MB) into the Hugging Face cache."""
    return PINNED["bge"].fetch(cache_dir)


class BgeSmall:
    """bge-small-en-v1.5 on the CPU: the [CLS] vector, normalised, as the model card says."""

    name = f"bge-small-en-v1.5@{MODEL_REVISION[:7]}"

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir
        self._session = None
        self._tokenizer = None

    def _load(self) -> None:
        import onnxruntime
        from tokenizers import Tokenizer

        files = model_files(self._cache_dir)
        tokenizer = Tokenizer.from_file(str(files["tokenizer.json"]))
        tokenizer.enable_truncation(MAX_TOKENS)
        tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        self._tokenizer = tokenizer
        self._session = onnxruntime.InferenceSession(
            str(files["onnx/model.onnx"]), providers=["CPUExecutionProvider"]
        )

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if self._session is None:
            self._load()
        encodings = self._tokenizer.encode_batch(list(texts))
        feeds = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encodings], dtype=np.int64),
        }
        (hidden,) = self._session.run(["last_hidden_state"], feeds)
        return _normalise(hidden[:, 0, :])


class FakeEmbedder:
    """Hashed words and word pairs: a copy scores 1.0, another topic scores low. Not
    semantic; for tests and dry runs only."""

    name = "fake-hashed-words"
    dims = 384  # the real model's size, so fake vectors fit the text_vec table

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        rows = np.zeros((len(texts), self.dims), dtype=np.float32)
        for row, text in zip(rows, texts, strict=True):
            words = re.findall(r"[a-z0-9']+", text.lower())
            for feature in [*words, *(f"{a} {b}" for a, b in pairwise(words))]:
                digest = hashlib.sha1(feature.encode("utf-8")).digest()
                # signed hashing: at 384 buckets, collisions cancel out instead of adding up
                row[int.from_bytes(digest[:4], "little") % self.dims] += (
                    1.0 if digest[4] & 1 else -1.0
                )
        return _normalise(rows)
