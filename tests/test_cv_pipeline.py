import importlib

import numpy as np
import pytest
from PIL import Image

from inat2exif import app as inat2exif
from inat2exif import matcher


@pytest.fixture
def native_cv(monkeypatch):
    with monkeypatch.context() as patch:
        cv = importlib.import_module("cv2")
        patch.setattr(inat2exif, "cv2", cv)
        inat2exif.clear_cv_image_cache()
        yield cv
        inat2exif.clear_cv_image_cache()


def test_native_similarity_recognizes_identical_images_and_rejects_featureless_edges(native_cv):
    image = np.random.default_rng(42).integers(0, 256, (160, 240, 3), dtype=np.uint8)
    assert inat2exif.orb_similarity(image, image) == pytest.approx(1.0)
    assert inat2exif.histogram_similarity(image, image) == pytest.approx(1.0)
    assert inat2exif.edge_overlap_similarity(image, image) == pytest.approx(1.0)
    assert inat2exif.pair_similarity_score(image, image) == pytest.approx(1.0)
    blank = np.zeros_like(image)
    assert inat2exif.orb_similarity(blank, blank) == 0
    assert inat2exif.edge_overlap_similarity(blank, blank) == 0
    unrelated = np.random.default_rng(43).integers(0, 256, image.shape, dtype=np.uint8)
    assert 0 <= inat2exif.pair_similarity_score(image, unrelated) < 0.8


def test_native_image_loading_reduces_size_reuses_cache_and_handles_missing_files(native_cv, tmp_path, monkeypatch):
    path = tmp_path / "large.jpg"
    Image.new("RGB", (1600, 1200), "blue").save(path)
    calls = []
    original = native_cv.imread
    def read(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(native_cv, "imread", read)
    image = inat2exif.load_cv_image(path, max_edge=150)
    assert image.shape[:2] == (112, 150)
    assert inat2exif.load_cv_image(path, max_edge=150) is image
    assert len(calls) == 1
    assert calls[0][1] == native_cv.IMREAD_REDUCED_COLOR_8
    assert inat2exif.load_cv_image(tmp_path / "missing.jpg") is None
    small = tmp_path / "small.png"
    Image.new("RGB", (20, 10), "red").save(small)
    assert inat2exif.load_cv_image(small).shape[:2] == (10, 20)
    Image.new("RGB", (30, 15), "red").save(small)
    assert inat2exif.load_cv_image(small).shape[:2] == (15, 30)


def test_native_image_loading_retries_full_decode_when_reduced_decode_fails(native_cv, tmp_path, monkeypatch):
    path = tmp_path / "large.jpg"
    Image.new("RGB", (1600, 1200), "blue").save(path)
    flags = []
    original = native_cv.imread
    def read(path, flag):
        flags.append(flag)
        return None if flag != native_cv.IMREAD_COLOR else original(path, flag)
    monkeypatch.setattr(native_cv, "imread", read)
    image = matcher.load_cv_image(path, cv2_module=native_cv, max_edge=150)
    assert image.shape[:2] == (112, 150)
    assert flags == [native_cv.IMREAD_REDUCED_COLOR_8, native_cv.IMREAD_COLOR]


@pytest.mark.parametrize("correlation,expected", [(float("inf"), 0.5), (-2.0, 0.0), (2.0, 1.0)])
def test_histogram_score_is_finite_and_clamped(native_cv, monkeypatch, correlation, expected):
    monkeypatch.setattr(native_cv, "compareHist", lambda *args: correlation)
    image = np.zeros((10, 10, 3), dtype=np.uint8)
    assert inat2exif.histogram_similarity(image, image) == expected


def test_nonfinite_pair_score_does_not_become_a_confident_match(native_cv, monkeypatch):
    monkeypatch.setattr(matcher, "orb_similarity", lambda *args, **kwargs: float("nan"))
    image = np.zeros((10, 10, 3), dtype=np.uint8)
    assert inat2exif.pair_similarity_score(image, image) == 0
