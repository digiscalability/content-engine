"""Script embeddings (C12, FR-QA-3): the fake used in tests and dry runs, and bge-small from
the local Hugging Face cache. Real-model tests carry the `model` marker."""

import numpy as np
import pytest

from engine.library.embeddings import (
    BgeSmall,
    EmbeddingModelMissing,
    FakeEmbedder,
    cosine,
    from_blob,
    to_blob,
)

SCRIPT = (
    "Most plans fail before they start. Shrink the first step until it feels almost silly. "
    "Open the file, write one line, then stop if you want. Starting is the hard part."
)
REWORDED = (
    "Most plans die before they begin. Make the first step so small it feels silly. "
    "Open the document, write a single line, and stop if you like. Starting is the hard bit."
)
OTHER = (
    "Silence is a tool. Before a hard conversation, wait three seconds longer than feels "
    "comfortable. The pause does the work words cannot."
)


def test_the_fake_is_deterministic_and_unit_length():
    fake = FakeEmbedder()
    first, again = fake.embed([SCRIPT]), fake.embed([SCRIPT])
    assert np.array_equal(first, again)
    assert abs(float(np.linalg.norm(first[0])) - 1.0) < 1e-6


def test_the_fake_scores_a_copy_high_and_another_topic_low():
    a, b, c = FakeEmbedder().embed([SCRIPT, SCRIPT, OTHER])
    assert cosine(a, b) > 0.999
    assert cosine(a, c) < 0.3


def test_blobs_round_trip():
    vector = FakeEmbedder().embed([SCRIPT])[0]
    assert np.array_equal(from_blob(to_blob(vector)), vector.astype(np.float32))


def test_a_missing_model_names_the_fetch_command(tmp_path):
    with pytest.raises(EmbeddingModelMissing, match="ce models fetch"):
        BgeSmall(cache_dir=tmp_path).embed([SCRIPT])


@pytest.mark.model
def test_bge_small_embeds_384_unit_vectors():
    vectors = BgeSmall().embed([SCRIPT, OTHER])
    assert vectors.shape == (2, 384)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)


@pytest.mark.model
def test_bge_small_sees_a_reworded_script_as_the_same():
    a, b, c = BgeSmall().embed([SCRIPT, REWORDED, OTHER])
    assert cosine(a, b) > 0.90  # FR-QA-3's limit: a light rewording is still a duplicate
    assert cosine(a, c) < 0.80


def test_fake_embedder_fits_the_text_vec_table():
    from engine.library.vectors import TEXT_DIMS

    assert FakeEmbedder().embed(["one line"]).shape == (1, TEXT_DIMS)
