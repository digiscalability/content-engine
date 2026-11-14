"""Face detection for the people rule (FR-UND-5, SRD §8.3, C13). ADR 0022.

`YuNet` is OpenCV's face detector (`cv2.FaceDetectorYN`) on the pinned ONNX model. No face is
matched to Abbas yet: every face sends its asset to the review list, where Abbas marks it.
`FakeFaces` counts magenta patches, the mark generated fixtures give their drawn people.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from engine.library.models import PINNED

MIN_PATCH_SHARE = 0.01  # the fake ignores specks under 1% of the frame


class FaceDetector(Protocol):
    name: str

    def count(self, image_bgr: np.ndarray) -> int:
        """How many faces one BGR frame shows."""
        ...


class YuNet:
    name = f"yunet-2023mar@{PINNED['yunet'].revision[:7]}"

    def __init__(self, cache_dir: Path | None = None, score: float = 0.6) -> None:
        self._cache_dir, self._score = cache_dir, score
        self._detector = None

    def count(self, image_bgr: np.ndarray) -> int:
        height, width = image_bgr.shape[:2]
        if self._detector is None:
            model = PINNED["yunet"].paths(self._cache_dir)["face_detection_yunet_2023mar.onnx"]
            self._detector = cv2.FaceDetectorYN.create(str(model), "", (width, height),
                                                       self._score)  # fmt: skip
        self._detector.setInputSize((width, height))
        _, faces = self._detector.detect(image_bgr)
        return 0 if faces is None else len(faces)


class FakeFaces:
    name = "fake-magenta-faces"

    def count(self, image_bgr: np.ndarray) -> int:
        b, g, r = (image_bgr[:, :, i].astype(np.int16) for i in range(3))
        mask = ((r > 200) & (b > 200) & (g < 60)).astype(np.uint8)
        found, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        least = MIN_PATCH_SHARE * mask.size
        return int(sum(1 for n in range(1, found) if stats[n, cv2.CC_STAT_AREA] >= least))
