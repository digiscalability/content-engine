"""CLIP keyframe embeddings (C12, FR-UND-3): the pinned ONNX export, its preprocessing, and
the colour fake used in tests and dry runs. Real-model tests carry the `model` marker."""

import numpy as np
import pytest
from library_fixtures import SAMPLE_BEATS, STILL_KINDS, draw_still
from PIL import Image

from engine.library.clip import CLIP_MEAN, CLIP_STD, ClipOnnx, FakeClip, clip_pixels
from engine.library.models import PINNED, ModelMissing
from engine.library.vectors import IMAGE_DIMS


def save(image: Image.Image, path) -> object:
    image.save(path)
    return path


def test_clip_pixels_shape_and_normalisation():
    pixels = clip_pixels(Image.new("RGB", (640, 360), (128, 128, 128)))
    assert pixels.shape == (1, 3, 224, 224) and pixels.dtype == np.float32
    for channel in range(3):
        expected = (128 / 255 - CLIP_MEAN[channel]) / CLIP_STD[channel]
        assert np.allclose(pixels[0, channel], expected, atol=1e-5)


def test_clip_pixels_centre_crops_the_long_side():
    image = Image.new("RGB", (400, 200), (0, 0, 0))
    image.paste((255, 255, 255), (0, 0, 80, 200))  # a white band on the far left, cropped off
    pixels = clip_pixels(image)
    black = (0 - CLIP_MEAN[0]) / CLIP_STD[0]
    assert np.allclose(pixels[0, 0], black, atol=1e-4)


def test_fake_clip_ranks_colour_words(tmp_path):
    fake = FakeClip()
    orange = save(Image.new("RGB", (64, 36), (250, 140, 10)), tmp_path / "o.png")
    image = fake.embed_images([orange])
    sunrise, ocean = fake.embed_texts(["the sun rising at dawn", "waves on the ocean"])
    assert image.shape == (1, IMAGE_DIMS)
    assert float(image[0] @ sunrise) > float(image[0] @ ocean)
    assert abs(float(np.linalg.norm(image[0])) - 1) < 1e-6


def test_fake_clip_text_without_colour_words_is_zero():
    assert not FakeClip().embed_texts(["nothing visual here"]).any()


def test_a_missing_clip_model_names_the_fetch_command(tmp_path):
    with pytest.raises(ModelMissing, match="ce models fetch"):
        ClipOnnx(cache_dir=tmp_path).embed_texts(["a sunrise"])


def test_clip_is_pinned_to_the_openai_vit_b_32_export():
    clip = PINNED["clip"]
    assert clip.repo == "Xenova/clip-vit-base-patch32"
    assert clip.revision == "6b7c523703ae26e39be4b03774966f8571c7f523"
    assert set(clip.files) == {"onnx/text_model.onnx", "onnx/vision_model.onnx",
                               "tokenizer.json"}  # fmt: skip


@pytest.mark.model
def test_real_clip_matches_each_fixture_still_to_its_beat(tmp_path):
    clip = ClipOnnx()
    paths = [save(draw_still(kind), tmp_path / f"{kind}.png") for kind in STILL_KINDS]
    images = clip.embed_images(paths)
    texts = clip.embed_texts([beat for _, beat in SAMPLE_BEATS])
    assert images.shape == (len(STILL_KINDS), IMAGE_DIMS)
    for row, (kind, beat) in enumerate(SAMPLE_BEATS):
        best = STILL_KINDS[int(np.argmax(images @ texts[row]))]
        assert best == kind, f"{beat!r} matched {best}"
