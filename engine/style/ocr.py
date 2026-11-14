"""Text on screen for the style analyser (SRD §9.1), behind `TextDetector` so tests use a
fake (NFR-8). RapidOCR runs PP-OCR models through onnxruntime on the CPU; its models ship
inside the wheel, so nothing is downloaded at run time (ADR 0015)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np

from engine.style.measure import TextBox


@runtime_checkable
class TextDetector(Protocol):
    def detect(self, frame: np.ndarray) -> list[TextBox]:
        """Text boxes in one RGB frame (height x width x 3, uint8)."""
        ...


@dataclass
class RapidOcrDetector:
    min_score: float = 0.5
    _engine: Any = field(default=None, repr=False)

    def detect(self, frame: np.ndarray) -> list[TextBox]:
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR  # loads the models once

            self._engine = RapidOCR()
        result, _ = self._engine(np.ascontiguousarray(frame[:, :, ::-1]))  # it reads BGR
        boxes = []
        for points, text, score in result or []:
            if float(score) < self.min_score or not text.strip():
                continue
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            boxes.append(TextBox(int(min(xs)), int(min(ys)), int(max(xs) - min(xs)),
                                 int(max(ys) - min(ys)), text.strip()))  # fmt: skip
        return boxes


@dataclass
class FakeTextDetector:
    """Answers from a function of the frame number, for tests and dry runs."""

    boxes: Callable[[int], list[TextBox]] = lambda n: []
    calls: int = 0

    def detect(self, frame: np.ndarray) -> list[TextBox]:
        found = self.boxes(self.calls)
        self.calls += 1
        return found
