import sys
import types
from pathlib import Path

import pytest


if "cv2" not in sys.modules:
    sys.modules["cv2"] = types.ModuleType("cv2")

if "pyexiv2" not in sys.modules:
    pyexiv2_stub = types.ModuleType("pyexiv2")
    pyexiv2_stub.Image = object
    pyexiv2_stub.xmp = types.SimpleNamespace(register_namespace=lambda *_args, **_kwargs: None)
    sys.modules["pyexiv2"] = pyexiv2_stub

from inat2exif import app as inat2exif
from inat2exif import indexer as indexer_logic


def test_indexer_parse_version_helpers() -> None:
    base, version = inat2exif.parse_version_info(Path("2024-04-27T104523s299_v1.JPG"))
    assert base == "2024-04-27T104523s299"
    assert version == 1
    assert inat2exif.get_version_group_key(Path("/photos/2024-04-27T104523s299_v1.JPG")) == "/photos/2024-04-27T104523s299"


def test_indexer_ignores_hidden_files_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    visible = tmp_path / "visible.JPG"
    hidden_file = tmp_path / ".hidden.JPG"
    hidden_dir = tmp_path / ".hidden"
    hidden_dir.mkdir()
    hidden_nested = hidden_dir / "nested.JPG"

    for path in (visible, hidden_file, hidden_nested):
        path.write_bytes(b"jpeg")

    monkeypatch.setattr(inat2exif, "extract_exif_datetime", lambda _p: "2024:04:27 15:01:00")

    photos, _by_second, _by_minute, _by_id = indexer_logic.index_local_photos(
        tmp_path,
        local_photo_cls=inat2exif.LocalPhoto,
        read_xmp_fn=lambda _p: {},
        parse_observation_id_from_xmp_fn=lambda _xmp: None,
        extract_exif_datetime_fn=inat2exif.extract_exif_datetime,
        logger=inat2exif.LOGGER,
        group_versions=True,
        ignore_hidden_files=True,
    )

    assert [photo.path.name for photo in photos] == ["visible.JPG"]


def test_indexer_skips_files_with_exif_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    broken = tmp_path / "broken.JPG"
    good = tmp_path / "good.JPG"
    broken.write_bytes(b"jpeg")
    good.write_bytes(b"jpeg")

    def fake_extract(path: Path) -> str | None:
        if path.name == "broken.JPG":
            raise RuntimeError("Directory Olympus with 3840 entries considered invalid; not read")
        return "2024:04:27 15:01:00"

    monkeypatch.setattr(inat2exif, "extract_exif_datetime", fake_extract)

    photos, _by_second, _by_minute, _by_id = indexer_logic.index_local_photos(
        tmp_path,
        local_photo_cls=inat2exif.LocalPhoto,
        read_xmp_fn=lambda _p: {},
        parse_observation_id_from_xmp_fn=lambda _xmp: None,
        extract_exif_datetime_fn=inat2exif.extract_exif_datetime,
        logger=inat2exif.LOGGER,
        group_versions=True,
        ignore_hidden_files=True,
    )

    assert [photo.path.name for photo in photos] == ["good.JPG"]


def test_parse_exif_datetime_matches_strptime() -> None:
    import datetime as dt

    import pytest

    from inat2exif.indexer import parse_exif_datetime

    for value in ("2024:04:27 15:01:09", "1999:12:31 23:59:59", "2024:4:7 5:1:9"):
        assert parse_exif_datetime(value) == dt.datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    for value in ("2024:02:30 10:00:00", "2024:13:01 10:00:00", "0000:00:00 00:00:00", "garbage", "2024:04:27 15:01:09 "):
        with pytest.raises(ValueError):
            dt.datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
        with pytest.raises(ValueError):
            parse_exif_datetime(value)
