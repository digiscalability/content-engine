"""Face detection for the people rule (FR-UND-5, §8.3): YuNet from the pinned cache, and a
fake that sees magenta patches (fixtures mark their drawn people with one)."""

import numpy as np
import pytest
from library_fixtures import STILL_KINDS, draw_still

from engine.library.faces import FakeFaces, YuNet
from engine.library.models import PINNED, ModelMissing


def bgr(image) -> np.ndarray:
    return np.asarray(image.convert("RGB"))[:, :, ::-1].copy()


def test_fake_faces_counts_magenta_patches():
    frame = np.zeros((360, 640, 3), np.uint8)
    assert FakeFaces().count(frame) == 0
    frame[10:60, 10:60] = (255, 0, 255)  # BGR magenta, about 1.1% of the frame
    frame[200:260, 400:460] = (255, 0, 255)
    assert FakeFaces().count(frame) == 2


def test_fake_faces_ignores_specks():
    frame = np.zeros((360, 640, 3), np.uint8)
    frame[10:14, 10:14] = (255, 0, 255)
    assert FakeFaces().count(frame) == 0


def test_the_drawn_person_carries_the_fake_marker():
    assert FakeFaces().count(bgr(draw_still("person"))) >= 1
    assert all(FakeFaces().count(bgr(draw_still(kind))) == 0 for kind in STILL_KINDS)


def test_a_missing_yunet_model_names_the_fetch_command(tmp_path):
    with pytest.raises(ModelMissing, match="ce models fetch"):
        YuNet(cache_dir=tmp_path).count(np.zeros((90, 160, 3), np.uint8))


def test_yunet_is_pinned():
    yunet = PINNED["yunet"]
    assert yunet.repo == "opencv/face_detection_yunet"
    assert yunet.files == ("face_detection_yunet_2023mar.onnx",)


@pytest.mark.model
def test_real_yunet_sees_no_face_in_the_scene_stills():
    detector = YuNet()
    assert [detector.count(bgr(draw_still(kind))) for kind in STILL_KINDS] == [0] * 5


@pytest.mark.model
def test_real_yunet_sees_the_drawn_person():
    assert YuNet().count(bgr(draw_still("person"))) >= 1
