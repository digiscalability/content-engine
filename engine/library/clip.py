"""Keyframe and text embeddings in one space (C12, FR-UND-3): CLIP ViT-B/32. ADR 0022.

`ClipOnnx` runs the pinned ONNX export of OpenAI's weights on onnxruntime (CPU), with the
model's own tokenizer and preprocessing: shortest side to 224 (bicubic), centre crop,
scaled to 0-1 and normalised with CLIP's mean and std. Rows are unit length, so a dot
product is the cosine. `FakeClip` stands in for it in unit tests and dry runs.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image

from engine.library.models import PINNED

CLIP_SIZE = 224
CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
CLIP_STD = (0.26862954, 0.26130258, 0.27577711)
MAX_TOKENS = 77
END_OF_TEXT = "<|endoftext|>"
DIMS = 512


class ImageTextEmbedder(Protocol):
    name: str

    def embed_images(self, paths: Sequence[Path]) -> np.ndarray:
        """One unit-length float32 row per image."""
        ...

    def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        """One unit-length float32 row per text, in the images' space."""
        ...


def _normalise(rows: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    return (rows / np.where(norms == 0, 1, norms)).astype(np.float32)


def clip_pixels(image: Image.Image) -> np.ndarray:
    """One image as CLIP's 1x3x224x224 input."""
    image = image.convert("RGB")
    scale = CLIP_SIZE / min(image.size)
    resized = image.resize(
        (max(CLIP_SIZE, round(image.width * scale)), max(CLIP_SIZE, round(image.height * scale))),
        Image.Resampling.BICUBIC,
    )
    left, top = (resized.width - CLIP_SIZE) // 2, (resized.height - CLIP_SIZE) // 2
    crop = resized.crop((left, top, left + CLIP_SIZE, top + CLIP_SIZE))
    pixels = np.asarray(crop, dtype=np.float32) / 255.0
    pixels = (pixels - np.array(CLIP_MEAN, np.float32)) / np.array(CLIP_STD, np.float32)
    return pixels.transpose(2, 0, 1)[None].astype(np.float32)


class ClipOnnx:
    name = f"clip-vit-b-32@{PINNED['clip'].revision[:7]}"

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir
        self._text = self._vision = self._tokenizer = None

    def _load(self) -> None:
        import onnxruntime
        from tokenizers import Tokenizer

        files = PINNED["clip"].paths(self._cache_dir)
        tokenizer = Tokenizer.from_file(str(files["tokenizer.json"]))
        tokenizer.enable_truncation(MAX_TOKENS)
        pad = tokenizer.token_to_id(END_OF_TEXT)
        tokenizer.enable_padding(pad_id=pad, pad_token=END_OF_TEXT)
        options = {"providers": ["CPUExecutionProvider"]}
        self._tokenizer = tokenizer
        self._text = onnxruntime.InferenceSession(str(files["onnx/text_model.onnx"]), **options)
        self._vision = onnxruntime.InferenceSession(str(files["onnx/vision_model.onnx"]), **options)

    def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        if self._text is None:
            self._load()
        encodings = self._tokenizer.encode_batch(list(texts))
        feeds = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
        }
        wanted = {i.name for i in self._text.get_inputs()}
        (embeds,) = self._text.run(["text_embeds"], {k: v for k, v in feeds.items() if k in wanted})
        return _normalise(embeds)

    def embed_images(self, paths: Sequence[Path]) -> np.ndarray:
        if self._vision is None:
            self._load()
        rows = []
        for path in paths:
            with Image.open(path) as image:
                (embeds,) = self._vision.run(["image_embeds"], {"pixel_values": clip_pixels(image)})
            rows.append(embeds[0])
        return _normalise(np.array(rows, dtype=np.float32).reshape(len(rows), DIMS))


# The fake: colours stand for subjects ------------------------------------------------------

PROTOTYPES = {
    "orange": (255, 140, 0),
    "blue": (30, 90, 200),
    "black": (10, 10, 30),
    "green": (30, 140, 50),
    "white": (240, 240, 235),
    "red": (210, 30, 30),
}
WORDS = {
    "orange": {"sun", "sunrise", "dawn", "morning", "orange"},
    "blue": {"ocean", "sea", "wave", "waves", "water", "blue"},
    "black": {"night", "star", "stars", "starry", "moon", "dark"},
    "green": {"forest", "tree", "trees", "pine", "grass", "field", "green"},
    "white": {"notebook", "page", "paper", "write", "white"},
    "red": {"fire", "red", "heart"},
}


class FakeClip:
    """An image is its share of pixels nearest each colour; a text is the colours its words
    name. Not semantic beyond that; for tests and dry runs only."""

    name = "fake-colour-clip"

    def embed_images(self, paths: Sequence[Path]) -> np.ndarray:
        protos = np.array(list(PROTOTYPES.values()), dtype=np.float32)
        rows = np.zeros((len(paths), DIMS), dtype=np.float32)
        for row, path in zip(rows, paths, strict=True):
            with Image.open(path) as image:
                pixels = np.asarray(image.convert("RGB").resize((64, 36)), np.float32)
            nearest = np.argmin(
                ((pixels.reshape(-1, 1, 3) - protos[None]) ** 2).sum(axis=2), axis=1
            )
            row[: len(protos)] = np.bincount(nearest, minlength=len(protos)) / len(nearest)
        return _normalise(rows)

    def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        rows = np.zeros((len(texts), DIMS), dtype=np.float32)
        for row, text in zip(rows, texts, strict=True):
            words = set(re.findall(r"[a-z]+", text.lower()))
            for n, colour in enumerate(PROTOTYPES):
                if words & WORDS[colour]:
                    row[n] = 1.0
        return _normalise(rows)
