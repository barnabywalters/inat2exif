"""Tests for src/photo_cache.py and the end-to-end caching path in indexer."""

import importlib
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Ensure the real pyexiv2 is available before any test that needs it.
# Tests that require pyexiv2 are marked with @pytest.mark.needs_pyexiv2 so
# they are skipped cleanly when pyexiv2 is a stub.
# ---------------------------------------------------------------------------

def _real_pyexiv2():
    """Return the real pyexiv2 module, or None if only a stub is available."""
    module = sys.modules.get("pyexiv2")
    if module is not None:
        if getattr(module, "Image", None) is object or getattr(module, "__file__", None) is None:
            sys.modules.pop("pyexiv2", None)
    try:
        module = importlib.import_module("pyexiv2")
    except Exception:
        return None
    if getattr(module, "Image", None) in (None, object):
        return None
    return module


# Ensure cv2 stub exists before importing app modules.
if "cv2" not in sys.modules:
    import types
    sys.modules["cv2"] = types.ModuleType("cv2")

from inat2exif import app as inat2exif
from inat2exif import indexer as indexer_logic
from inat2exif.photo_cache import PhotoMetadataCache, _EXPECTED_COLUMNS, _TABLE_NAME


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REAL_PHOTO = Path("tests/fixtures/real_cached_case/photos/2024-04-27T150100s534.JPG")


def _run_indexer(root: Path, db_path: Path, *, monkeypatch=None):
    """Run index_local_photos on *root* using a real PhotoMetadataCache.

    *monkeypatch* is the pytest monkeypatch fixture.  When supplied the helper
    patches ``inat2exif.pyexiv2`` to the real module so that functions such as
    ``read_embedded_xmp`` and ``ensure_namespaces_registered`` use the correct
    implementation even when another test module has installed a stub first.
    """
    pyexiv2 = _real_pyexiv2()
    assert pyexiv2 is not None, "real pyexiv2 required"
    if monkeypatch is not None:
        monkeypatch.setattr(inat2exif, "pyexiv2", pyexiv2)
    with PhotoMetadataCache(db_path) as cache:
        photos, *_ = indexer_logic.index_local_photos(
            root,
            local_photo_cls=inat2exif.LocalPhoto,
            read_xmp_fn=inat2exif.read_embedded_xmp,
            parse_observation_id_from_xmp_fn=inat2exif.parse_observation_id_from_xmp,
            extract_exif_datetime_fn=lambda p: indexer_logic.extract_exif_datetime(p, pyexiv2),
            logger=inat2exif.LOGGER,
            group_versions=False,
            photo_metadata_cache=cache,
        )
    return photos


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path):
    """Yield a fresh DB path and delete the file (if created) on teardown."""
    path = tmp_path / "cache.db"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PhotoMetadataCache unit tests
# ---------------------------------------------------------------------------

class TestPhotoCacheBasics:
    def test_context_manager_creates_db(self, db_path: Path) -> None:
        with PhotoMetadataCache(db_path) as cache:
            assert db_path.exists()
            assert cache._conn is not None
        assert cache._conn is None

    def test_dimensions_round_trip_and_prune_with_missing_paths(self, db_path: Path) -> None:
        with PhotoMetadataCache(db_path) as cache:
            cache.put(Path("/photos/a.jpg"), 1.0, None, {})
            cache.put_dimensions([(Path("/photos/a.jpg"), 1.0, 6000, 4000), (Path("/photos/gone.png"), 2.0, None, None)])
        with PhotoMetadataCache(db_path) as cache:
            assert cache.load_dimensions() == {"/photos/a.jpg": (1.0, 6000, 4000), "/photos/gone.png": (2.0, None, None)}
            cache.delete_missing({"/photos/a.jpg"})
            assert cache.load_dimensions() == {"/photos/a.jpg": (1.0, 6000, 4000)}

    def test_get_returns_none_for_missing_entry(self, db_path: Path) -> None:
        path = Path("/some/photo.jpg")
        with PhotoMetadataCache(db_path) as cache:
            assert cache.get(path, 1234.0) is None

    def test_put_and_get_round_trip(self, db_path: Path) -> None:
        path = Path("/photos/img.jpg")
        xmp = {"Xmp.dc.subject": ["bird", "taxonomy|Animalia"]}
        with PhotoMetadataCache(db_path) as cache:
            cache.put(path, 9999.5, "2024:04:27 15:01:00", xmp)
            result = cache.get(path, 9999.5)
        assert result is not None
        exif_dt, xmp_out = result
        assert exif_dt == "2024:04:27 15:01:00"
        assert xmp_out == xmp

    def test_get_returns_none_when_mtime_differs(self, db_path: Path) -> None:
        path = Path("/photos/img.jpg")
        with PhotoMetadataCache(db_path) as cache:
            cache.put(path, 1000.0, "2024:04:27 15:01:00", {})
            assert cache.get(path, 1001.0) is None

    def test_put_replaces_stale_entry(self, db_path: Path) -> None:
        path = Path("/photos/img.jpg")
        with PhotoMetadataCache(db_path) as cache:
            cache.put(path, 1000.0, "2024:04:27 15:01:00", {"old": "data"})
            cache.put(path, 2000.0, "2024:04:27 15:02:00", {"new": "data"})
            result = cache.get(path, 2000.0)
        assert result is not None
        exif_dt, xmp_out = result
        assert exif_dt == "2024:04:27 15:02:00"
        assert xmp_out == {"new": "data"}

    def test_none_exif_datetime_is_stored_and_returned(self, db_path: Path) -> None:
        path = Path("/photos/no_exif.jpg")
        with PhotoMetadataCache(db_path) as cache:
            cache.put(path, 5000.0, None, {})
            result = cache.get(path, 5000.0)
        assert result is not None
        exif_dt, xmp_out = result
        assert exif_dt is None
        assert xmp_out == {}

    def test_complex_xmp_dict_survives_round_trip(self, db_path: Path) -> None:
        path = Path("/photos/tagged.jpg")
        xmp = {
            "Xmp.dwc.catalogNumber": "12345678",
            "Xmp.dwc.datasetName": "iNaturalist observations",
            "Xmp.lr.hierarchicalSubject": ["taxonomy|Animalia|Arthropoda", "custom tag"],
            "Xmp.dc.subject": ["taxonomy/Animalia/Arthropoda"],
        }
        with PhotoMetadataCache(db_path) as cache:
            cache.put(path, 1.0, "2024:04:27 15:01:00", xmp)
            result = cache.get(path, 1.0)
        assert result is not None
        _, xmp_out = result
        assert xmp_out == xmp

    def test_delete_missing_removes_stale_rows(self, db_path: Path) -> None:
        keep = Path("/photos/keep.jpg")
        drop = Path("/photos/drop.jpg")
        with PhotoMetadataCache(db_path) as cache:
            cache.put(keep, 1.0, "2024:04:27 15:01:00", {})
            cache.put(drop, 2.0, "2024:04:27 15:01:00", {})
            deleted = cache.delete_missing({str(keep)})
            assert deleted == 1
            assert cache.get(keep, 1.0) is not None
            assert cache.get(drop, 2.0) is None

    def test_cache_persists_across_open_close_open(self, db_path: Path) -> None:
        path = Path("/photos/persist.jpg")
        xmp = {"Xmp.dwc.catalogNumber": "99"}
        with PhotoMetadataCache(db_path) as cache:
            cache.put(path, 42.0, "2024:04:27 15:01:00", xmp)
        # Re-open
        with PhotoMetadataCache(db_path) as cache:
            result = cache.get(path, 42.0)
        assert result is not None
        _, xmp_out = result
        assert xmp_out == xmp


class TestPhotoCacheSchemaValidation:
    def test_compatible_schema_is_kept(self, db_path: Path) -> None:
        """Opening a DB that already has the right schema should not delete it."""
        path = Path("/photos/img.jpg")
        with PhotoMetadataCache(db_path) as cache:
            cache.put(path, 1.0, "2024:04:27 15:01:00", {"k": "v"})
        # Re-open – existing data must survive
        with PhotoMetadataCache(db_path) as cache:
            result = cache.get(path, 1.0)
        assert result is not None

    def test_incompatible_schema_triggers_recreation(self, db_path: Path) -> None:
        """A DB with wrong columns must be silently wiped and recreated."""
        # Build a DB with an old, incompatible schema by hand.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE photo_metadata "
            "(path TEXT PRIMARY KEY, mtime REAL, old_column TEXT)"
        )
        conn.execute(
            "INSERT INTO photo_metadata VALUES ('/old.jpg', 1.0, 'legacy')"
        )
        conn.commit()
        conn.close()

        # Opening with PhotoMetadataCache should wipe the old DB and create a fresh one.
        path = Path("/photos/new.jpg")
        with PhotoMetadataCache(db_path) as cache:
            # Old data must be gone.
            assert cache.get(Path("/old.jpg"), 1.0) is None
            # Must be able to write new data.
            cache.put(path, 2.0, "2024:04:27 15:01:00", {"fresh": "true"})
            result = cache.get(path, 2.0)
        assert result is not None
        _, xmp_out = result
        assert xmp_out == {"fresh": "true"}

    def test_missing_table_is_created_fresh(self, db_path: Path) -> None:
        """A brand-new DB file (no table) must be initialised without errors."""
        # Create an empty SQLite file with no tables.
        conn = sqlite3.connect(str(db_path))
        conn.close()
        with PhotoMetadataCache(db_path) as cache:
            cache.put(Path("/p.jpg"), 1.0, "2024:01:01 00:00:00", {})
            result = cache.get(Path("/p.jpg"), 1.0)
        assert result is not None

    def test_expected_columns_match_actual_schema(self, db_path: Path) -> None:
        """_EXPECTED_COLUMNS must reflect the columns that _SCHEMA actually creates."""
        with PhotoMetadataCache(db_path):
            pass
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(f"PRAGMA table_info({_TABLE_NAME})").fetchall()
        conn.close()
        actual = tuple((row[1], row[2]) for row in rows)
        assert actual == _EXPECTED_COLUMNS


# ---------------------------------------------------------------------------
# End-to-end indexer + cache tests using a real JPEG fixture
# ---------------------------------------------------------------------------

def _needs_pyexiv2(fn):
    """Decorator: skip test if real pyexiv2 is not available."""
    return pytest.mark.skipif(
        _real_pyexiv2() is None,
        reason="real pyexiv2 not available",
    )(fn)


@_needs_pyexiv2
def test_indexer_cache_e2e(tmp_path: Path, monkeypatch) -> None:
    """End-to-end cache lifecycle: populate, hit, invalidate on mtime change, re-hit."""
    pyexiv2_module = _real_pyexiv2()
    assert pyexiv2_module is not None

    monkeypatch.setattr(inat2exif, "pyexiv2", pyexiv2_module)
    inat2exif.ensure_namespaces_registered()

    photo = tmp_path / REAL_PHOTO.name
    shutil.copy2(REAL_PHOTO, photo)
    db = tmp_path / "cache.db"

    try:
        # ------------------------------------------------------------------
        # Run 1: empty cache → photo parsed, cache populated
        # ------------------------------------------------------------------
        photos = _run_indexer(tmp_path, db, monkeypatch=monkeypatch)
        assert len(photos) == 1
        assert db.exists()

        mtime_after_first = photo.stat().st_mtime
        with PhotoMetadataCache(db) as cache:
            cached = cache.get(photo, mtime_after_first)
        assert cached is not None
        exif_dt, _ = cached
        assert exif_dt == "2024:04:27 15:01:00"

        # ------------------------------------------------------------------
        # Run 2: same file, same mtime → cache hit, image not re-opened
        # ------------------------------------------------------------------
        open_count = 0
        original_read_xmp = inat2exif.read_embedded_xmp

        def counting_read_xmp(p: Path):
            nonlocal open_count
            open_count += 1
            return original_read_xmp(p)

        with PhotoMetadataCache(db) as cache:
            photos_second, *_ = indexer_logic.index_local_photos(
                tmp_path,
                local_photo_cls=inat2exif.LocalPhoto,
                read_xmp_fn=counting_read_xmp,
                parse_observation_id_from_xmp_fn=inat2exif.parse_observation_id_from_xmp,
                extract_exif_datetime_fn=lambda p: indexer_logic.extract_exif_datetime(p, pyexiv2_module),
                logger=inat2exif.LOGGER,
                group_versions=False,
                photo_metadata_cache=cache,
            )

        assert open_count == 0, "Expected cache hit on run 2; image was re-read unexpectedly"
        assert len(photos_second) == 1
        assert photos_second[0].exif_datetime_raw == photos[0].exif_datetime_raw
        assert photos_second[0].existing_observation_id == photos[0].existing_observation_id

        # ------------------------------------------------------------------
        # Run 3: XMP modified → mtime changes → cache miss → re-parsed
        # ------------------------------------------------------------------
        img = pyexiv2_module.Image(str(photo))
        try:
            img.modify_xmp({
                inat2exif.XMP_DWC_CATALOG_NUMBER_KEY: "99999999",
                inat2exif.XMP_DWC_DATASET_NAME_KEY: inat2exif.XMP_DWC_EXPECTED_DATASET_NAME,
            })
        finally:
            close_fn = getattr(img, "close", None)
            if callable(close_fn):
                close_fn()

        mtime_after_modify = photo.stat().st_mtime
        assert mtime_after_modify != mtime_after_first, (
            "File mtime did not change after XMP write; cache invalidation cannot be tested"
        )

        open_count = 0
        with PhotoMetadataCache(db) as cache:
            photos_third, *_ = indexer_logic.index_local_photos(
                tmp_path,
                local_photo_cls=inat2exif.LocalPhoto,
                read_xmp_fn=counting_read_xmp,
                parse_observation_id_from_xmp_fn=inat2exif.parse_observation_id_from_xmp,
                extract_exif_datetime_fn=lambda p: indexer_logic.extract_exif_datetime(p, pyexiv2_module),
                logger=inat2exif.LOGGER,
                group_versions=False,
                photo_metadata_cache=cache,
            )

        assert open_count == 1, "Expected re-parse after mtime change; image was not re-read"
        assert len(photos_third) == 1
        assert photos_third[0].existing_observation_id == 99999999

        # ------------------------------------------------------------------
        # Run 4: no further changes → cache hit again
        # ------------------------------------------------------------------
        open_count = 0
        with PhotoMetadataCache(db) as cache:
            photos_fourth, *_ = indexer_logic.index_local_photos(
                tmp_path,
                local_photo_cls=inat2exif.LocalPhoto,
                read_xmp_fn=counting_read_xmp,
                parse_observation_id_from_xmp_fn=inat2exif.parse_observation_id_from_xmp,
                extract_exif_datetime_fn=lambda p: indexer_logic.extract_exif_datetime(p, pyexiv2_module),
                logger=inat2exif.LOGGER,
                group_versions=False,
                photo_metadata_cache=cache,
            )

        assert open_count == 0, "Expected cache hit on run 4; image was re-read unexpectedly"
        assert len(photos_fourth) == 1
        assert photos_fourth[0].existing_observation_id == 99999999

    finally:
        db.unlink(missing_ok=True)
