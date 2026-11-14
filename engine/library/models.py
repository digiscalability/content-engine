"""The engine's local models, each pinned to one Hugging Face revision (C12; ADR 0018, 0022).

They load from the local Hugging Face cache only: no network at run time. `ce models fetch`
downloads them once, and only after Abbas has agreed to each download.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ModelMissing(RuntimeError):
    """A pinned model is not in the local cache."""


@dataclass(frozen=True)
class PinnedModel:
    key: str
    repo: str
    revision: str
    files: tuple[str, ...]
    purpose: str
    size_mb: float

    @property
    def short_name(self) -> str:
        return self.repo.split("/")[-1]

    def paths(self, cache_dir: Path | None = None) -> dict[str, Path]:
        """The cached files by name; `ModelMissing` if any is absent."""
        from huggingface_hub import hf_hub_download
        from huggingface_hub.errors import LocalEntryNotFoundError

        try:
            return {
                name: Path(hf_hub_download(self.repo, name, revision=self.revision,
                                           cache_dir=cache_dir, local_files_only=True))
                for name in self.files
            }  # fmt: skip
        except LocalEntryNotFoundError as exc:
            raise ModelMissing(
                f"{self.repo} is not in the local model cache; run `uv run ce models fetch`"
            ) from exc

    def cached(self, cache_dir: Path | None = None) -> bool:
        try:
            self.paths(cache_dir)
        except ModelMissing:
            return False
        return True

    def fetch(self, cache_dir: Path | None = None) -> list[Path]:
        """Download the pinned files into the Hugging Face cache."""
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        from huggingface_hub import hf_hub_download

        return [
            Path(hf_hub_download(self.repo, name, revision=self.revision, cache_dir=cache_dir))
            for name in self.files
        ]


PINNED: dict[str, PinnedModel] = {
    "bge": PinnedModel(
        "bge", "BAAI/bge-small-en-v1.5", "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
        ("config.json", "tokenizer.json", "onnx/model.onnx"),
        "text embeddings: QA self-dedupe (FR-QA-3) and transcript search (FR-UND-3)", 134,
    ),
    # OpenAI's CLIP ViT-B/32 (open_clip's ViT-B-32 'openai' weights) exported to ONNX
    "clip": PinnedModel(
        "clip", "Xenova/clip-vit-base-patch32", "6b7c523703ae26e39be4b03774966f8571c7f523",
        ("onnx/text_model.onnx", "onnx/vision_model.onnx", "tokenizer.json"),
        "keyframe embeddings and visual search (FR-UND-3)", 608,
    ),
    "yunet": PinnedModel(
        "yunet", "opencv/face_detection_yunet", "3cc26e7f1014a5ee5d74a42acee58bafc9d0a310",
        ("face_detection_yunet_2023mar.onnx",),
        "face detection for the people rule (FR-UND-5)", 0.2,
    ),
}  # fmt: skip
