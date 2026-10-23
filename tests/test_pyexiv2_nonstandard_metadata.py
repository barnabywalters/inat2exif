import datetime as dt
import importlib
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image

from inat2exif import app as inat2exif


def test_gps_metadata_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pyexiv2_module = _import_real_pyexiv2()
    if pyexiv2_module is None:
        pytest.skip("real pyexiv2 is unavailable in this test environment")
    monkeypatch.setattr(inat2exif, "pyexiv2", pyexiv2_module)
    monkeypatch.setattr(inat2exif, "load_config", lambda: inat2exif.AppConfig())
    path = tmp_path / "gps.jpg"
    Image.new("RGB", (16, 16), "red").save(path)
    with Image.open(path) as image:
        pixels = image.tobytes()
    image = pyexiv2_module.Image(str(path))
    try:
        image.modify_exif({"Exif.Photo.DateTimeOriginal": "2024:04:27 10:45:23"})
    finally:
        image.close()
    obs = inat2exif.ObservationData(
        42, dt.datetime(2024, 4, 27, tzinfo=dt.UTC), [], "u",
        metadata_xmp=inat2exif.build_observation_metadata_xmp(
            {"geojson": {"type": "Point", "coordinates": [-93.79020984, 41.78763132]}},
            42, "u", dt.datetime(2024, 4, 27, tzinfo=dt.UTC),
        ),
    )
    inat2exif.write_observation_metadata(path, obs)
    image = pyexiv2_module.Image(str(path))
    try:
        exif = image.read_exif()
    finally:
        image.close()
    for key, expected in (("GPSLatitude", 41.78763132), ("GPSLongitude", 93.79020984)):
        degrees, minutes, seconds = map(Fraction, exif["Exif.GPSInfo." + key].split())
        assert float(degrees + minutes / 60 + seconds / 3600) == pytest.approx(expected, abs=1e-9)
    assert exif["Exif.GPSInfo.GPSLatitudeRef"] == "N"
    assert exif["Exif.GPSInfo.GPSLongitudeRef"] == "W"
    assert exif["Exif.Photo.DateTimeOriginal"] == "2024:04:27 10:45:23"
    assert inat2exif.metadata_refresh_needed_with_details(path, obs) == (False, [])

    obs.metadata_xmp["Xmp.dwc.decimalLatitude"] = "-10"
    obs.metadata_xmp["Xmp.dwc.decimalLongitude"] = "20"
    inat2exif.write_observation_metadata(path, obs)
    assert inat2exif.remove_observation_metadata(path)
    image = pyexiv2_module.Image(str(path))
    try:
        assert image.read_exif() == exif
    finally:
        image.close()
    with Image.open(path) as image:
        assert image.tobytes() == pixels


FIXTURE_PATH = Path("tests/fixtures/2009-06-26T075743_edited.jpeg")
PIXEL_FIXTURE_PATH = Path("tests/fixtures/2026-04-09T121738s583.jpg")


def _import_real_pyexiv2():
    """Import the real pyexiv2 module, bypassing any test stubs if possible."""
    module = sys.modules.get("pyexiv2")
    if module is not None:
        image_attr = getattr(module, "Image", None)
        if image_attr is object or getattr(module, "__file__", None) is None:
            sys.modules.pop("pyexiv2", None)

    try:
        module = importlib.import_module("pyexiv2")
    except Exception:
        return None

    image_attr = getattr(module, "Image", None)
    if image_attr is None or image_attr is object:
        return None
    return module


def _read_exif_error(path: Path, pyexiv2_module) -> str | None:
    try:
        image = pyexiv2_module.Image(str(path))
    except Exception as exc:
        return str(exc)
    try:
        image.read_exif()
        return None
    except Exception as exc:
        return str(exc)
    finally:
        close_fn = getattr(image, "close", None)
        if callable(close_fn):
            close_fn()


def test_taxonomy_prefix_round_trip_preserves_user_tags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pyexiv2_module = _import_real_pyexiv2()
    if pyexiv2_module is None:
        pytest.skip("real pyexiv2 is unavailable in this test environment")
    monkeypatch.setattr(inat2exif, "pyexiv2", pyexiv2_module)
    config = inat2exif.AppConfig(taxonomy_tag_prefix="")
    monkeypatch.setattr(inat2exif, "load_config", lambda: config)
    path = tmp_path / "tags.jpg"
    Image.new("RGB", (16, 16)).save(path)
    image = pyexiv2_module.Image(str(path))
    try:
        image.modify_xmp({
            "Xmp.dc.subject": ["holiday", "Animalia", "taxonomy/Old"],
            "Xmp.dwc.dynamicProperties": "other-app=keep",
        })
    finally:
        image.close()
    obs = inat2exif.ObservationData(
        42, dt.datetime(2024, 4, 27, tzinfo=dt.UTC), [], "u",
        metadata_xmp={
            "Xmp.dwc.datasetName": "iNaturalist observations", "Xmp.dwc.catalogNumber": "42",
            "Xmp.dc.subject": ["taxonomy/Animalia/Insecta", "taxonomy/Animalia", "taxonomy"],
        },
    )
    for prefix in ("", "Nature", "taxonomy"):
        config.taxonomy_tag_prefix = prefix
        inat2exif.write_observation_metadata(path, obs)
        assert inat2exif.metadata_refresh_needed_with_details(path, obs) == (False, [])
        xmp = inat2exif.read_embedded_xmp(path)
        expected_tag = f"{prefix}/Animalia/Insecta" if prefix else "Animalia/Insecta"
        assert xmp["Xmp.dc.subject"] == ["holiday", "Animalia", expected_tag, *([f"{prefix}/Animalia", prefix] if prefix else [])]
    config.write_hierarchical_tags = False
    before = inat2exif.read_embedded_xmp(path)
    inat2exif.write_observation_metadata(path, obs)
    assert inat2exif.read_embedded_xmp(path)["Xmp.dc.subject"] == before["Xmp.dc.subject"]
    assert inat2exif.remove_observation_metadata(path)
    after = inat2exif.read_embedded_xmp(path)
    assert after["Xmp.dc.subject"] == ["holiday", "Animalia"]
    assert after["Xmp.dwc.dynamicProperties"] == "other-app=keep"


def _exiftool_json(path: Path) -> dict:
    result = subprocess.run(
        ["exiftool", "-j", "-G1", "-a", "-u", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    if not payload or not isinstance(payload[0], dict):
        raise AssertionError("Unexpected exiftool JSON output")
    return payload[0]


@pytest.mark.integration
def test_write_observation_metadata_preserves_nonstandard_exif_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pyexiv2_module = _import_real_pyexiv2()
    if pyexiv2_module is None:
        pytest.skip("real pyexiv2 is unavailable in this test environment")
    if not FIXTURE_PATH.exists():
        pytest.skip(f"fixture not found: {FIXTURE_PATH}")

    target = tmp_path / FIXTURE_PATH.name
    target.write_bytes(FIXTURE_PATH.read_bytes())

    before_json = _exiftool_json(target)
    before_olympus_camera_type = before_json.get("Olympus:CameraType")
    before_olympus_id = before_json.get("Olympus:CameraID")
    before_datetime_original = before_json.get("ExifIFD:DateTimeOriginal")

    assert before_olympus_camera_type is not None
    assert before_olympus_id is not None

    before_error = _read_exif_error(target, pyexiv2_module)
    assert before_error is not None
    assert "Olympus" in before_error

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "write_hierarchical_tags": True,
                "image_title_template": "",
                "image_caption_template": "",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(inat2exif, "pyexiv2", pyexiv2_module)
    monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)

    obs = inat2exif.ObservationData(
        obs_id=999999,
        observed_at=dt.datetime(2009, 6, 26, 7, 57, 43, tzinfo=dt.UTC),
        photos=[],
        uri="https://www.inaturalist.org/observations/999999",
        metadata_xmp={
            "Xmp.dwc.occurrenceID": "https://www.inaturalist.org/observations/999999",
            "Xmp.dwc.datasetName": "iNaturalist observations",
            "Xmp.dwc.catalogNumber": "999999",
        },
    )

    with pytest.raises(RuntimeError, match="Olympus"):
        inat2exif.write_observation_metadata(target, obs)

    after_error = _read_exif_error(target, pyexiv2_module)
    assert after_error is not None
    assert "Olympus" in after_error

    after_json = _exiftool_json(target)
    assert after_json.get("Olympus:CameraType") == before_olympus_camera_type
    assert after_json.get("Olympus:CameraID") == before_olympus_id
    assert after_json.get("ExifIFD:DateTimeOriginal") == before_datetime_original


@pytest.mark.integration
def test_write_observation_metadata_succeeds_for_pixel_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pyexiv2_module = _import_real_pyexiv2()
    if pyexiv2_module is None:
        pytest.skip("real pyexiv2 is unavailable in this test environment")
    if not PIXEL_FIXTURE_PATH.exists():
        pytest.skip(f"fixture not found: {PIXEL_FIXTURE_PATH}")

    target = tmp_path / PIXEL_FIXTURE_PATH.name
    target.write_bytes(PIXEL_FIXTURE_PATH.read_bytes())

    before_json = _exiftool_json(target)
    before_make = before_json.get("IFD0:Make")
    before_model = before_json.get("IFD0:Model")
    before_datetime_original = before_json.get("ExifIFD:DateTimeOriginal")

    assert before_make is not None
    assert before_model is not None
    assert before_datetime_original is not None

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "write_hierarchical_tags": True,
                "image_title_template": "",
                "image_caption_template": "",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(inat2exif, "pyexiv2", pyexiv2_module)
    monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)

    obs_id = 348570994
    obs = inat2exif.ObservationData(
        obs_id=obs_id,
        observed_at=dt.datetime(2026, 4, 9, 12, 17, 38, tzinfo=dt.UTC),
        photos=[],
        uri=f"https://www.inaturalist.org/observations/{obs_id}",
        metadata_xmp={
            "Xmp.dwc.occurrenceID": f"https://www.inaturalist.org/observations/{obs_id}",
            "Xmp.dwc.datasetName": "iNaturalist observations",
            "Xmp.dwc.catalogNumber": str(obs_id),
            "Xmp.dc.subject": ["taxonomy/Animalia/Chordata"],
            "Xmp.lr.hierarchicalSubject": ["taxonomy|Animalia|Chordata"],
        },
    )

    inat2exif.write_observation_metadata(target, obs)

    # Verify metadata was written and can be read back through the normal lookup path.
    assert inat2exif.read_existing_observation_id(target) == obs_id

    after_json = _exiftool_json(target)
    assert after_json.get("IFD0:Make") == before_make
    assert after_json.get("IFD0:Model") == before_model
    assert after_json.get("ExifIFD:DateTimeOriginal") == before_datetime_original
