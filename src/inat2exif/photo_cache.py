"""SQLite-backed cache for local photo metadata (EXIF datetime + full XMP dict).

The cache avoids re-opening image files on every indexing run by storing the
values extracted during the last scan alongside the file's mtime.  When a file
is re-encountered and its mtime is unchanged the cached values are returned
immediately; otherwise the image is re-read and the cache entry is refreshed.

The on-disk schema is validated on every open.  If the columns do not match
what this version of the code expects the database file is deleted and
recreated from scratch so that stale data from a different schema version is
never used silently.

Usage::

    with PhotoMetadataCache(db_path) as cache:
        result = cache.get(path, mtime)
        if result is None:
            # extract metadata the slow way …
            cache.put(path, mtime, exif_datetime_raw, xmp_metadata)
"""

import json
import sqlite3
from pathlib import Path


_TABLE_NAME = "photo_metadata"

# Canonical ordered list of (column_name, type) pairs that must exist.
# Changing this list triggers an automatic drop-and-recreate of the database.
_EXPECTED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("path",             "TEXT"),
    ("mtime",            "REAL"),
    ("exif_datetime_raw","TEXT"),
    ("xmp_metadata",     "TEXT"),
)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_TABLE_NAME} (
    path                TEXT PRIMARY KEY,
    mtime               REAL NOT NULL,
    exif_datetime_raw   TEXT,
    xmp_metadata        TEXT
);
"""


# Image sizes, read lazily (e.g. by the whole-library search) rather than during indexing.
_DIMENSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_dimensions (
    path    TEXT PRIMARY KEY,
    mtime   REAL NOT NULL,
    width   INTEGER,
    height  INTEGER
);
"""


def _check_schema(conn: sqlite3.Connection) -> bool:
    """Return True if the existing table's columns match _EXPECTED_COLUMNS."""
    rows = conn.execute(f"PRAGMA table_info({_TABLE_NAME})").fetchall()
    if not rows:
        # Table does not yet exist – will be created by _SCHEMA.
        return True
    # Each row: (cid, name, type, notnull, dflt_value, pk)
    actual = tuple((row[1], row[2]) for row in rows)
    return actual == _EXPECTED_COLUMNS


class PhotoMetadataCache:
    """Cache for photo EXIF datetime and full embedded XMP metadata.

    Designed to be used as a context manager so the underlying SQLite
    connection is properly closed even when an error occurs.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "PhotoMetadataCache":
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        if not _check_schema(conn):
            # Schema mismatch – close, delete, and start fresh.
            conn.close()
            self._db_path.unlink(missing_ok=True)
            conn = sqlite3.connect(str(self._db_path))
        conn.execute(_SCHEMA)
        conn.execute(_DIMENSIONS_SCHEMA)
        conn.commit()
        self._conn = conn
        return self

    def __exit__(self, *_exc) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self, path: Path, mtime: float
    ) -> tuple[str | None, dict[str, object]] | None:
        """Return ``(exif_datetime_raw, xmp_metadata)`` if the cache has a
        fresh entry for *path*, or ``None`` if the entry is absent or stale.
        """
        assert self._conn is not None, "PhotoMetadataCache must be used as a context manager"
        row = self._conn.execute(
            "SELECT mtime, exif_datetime_raw, xmp_metadata FROM photo_metadata WHERE path = ?",
            (str(path),),
        ).fetchone()
        if row is None:
            return None
        cached_mtime, exif_dt, xmp_json = row
        if cached_mtime != mtime:
            return None
        xmp: dict[str, object] = json.loads(xmp_json) if xmp_json else {}
        return exif_dt, xmp

    def put(
        self,
        path: Path,
        mtime: float,
        exif_datetime_raw: str | None,
        xmp_metadata: dict[str, object],
        *,
        commit: bool = True,
    ) -> None:
        """Insert or replace the cache entry for *path*.

        Pass ``commit=False`` when writing many rows and call :meth:`commit` once
        afterwards; committing every row forces a disk sync per photo.
        """
        assert self._conn is not None, "PhotoMetadataCache must be used as a context manager"
        self._conn.execute(
            """
            INSERT OR REPLACE INTO photo_metadata
                (path, mtime, exif_datetime_raw, xmp_metadata)
            VALUES (?, ?, ?, ?)
            """,
            (str(path), mtime, exif_datetime_raw, json.dumps(xmp_metadata)),
        )
        if commit:
            self._conn.commit()

    def commit(self) -> None:
        assert self._conn is not None, "PhotoMetadataCache must be used as a context manager"
        self._conn.commit()

    def load_all_raw(self) -> dict[str, tuple[float, str | None, str | None]]:
        """Return every row as ``{path: (mtime, exif_datetime_raw, xmp_json)}``.

        One bulk query is far cheaper than a lookup per file when indexing a whole
        library; the XMP JSON is left unparsed so callers only decode fresh hits.
        """
        assert self._conn is not None, "PhotoMetadataCache must be used as a context manager"
        return {
            row[0]: (row[1], row[2], row[3])
            for row in self._conn.execute(
                "SELECT path, mtime, exif_datetime_raw, xmp_metadata FROM photo_metadata"
            )
        }

    def load_dimensions(self) -> dict[str, tuple[float, int | None, int | None]]:
        """Return every cached image size as ``{path: (mtime, width, height)}``.

        Width and height are ``None`` for files whose size couldn't be read.
        """
        assert self._conn is not None, "PhotoMetadataCache must be used as a context manager"
        return {
            row[0]: (row[1], row[2], row[3])
            for row in self._conn.execute("SELECT path, mtime, width, height FROM photo_dimensions")
        }

    def put_dimensions(self, rows) -> None:
        """Store ``(path, mtime, width, height)`` rows in one transaction."""
        assert self._conn is not None, "PhotoMetadataCache must be used as a context manager"
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO photo_dimensions (path, mtime, width, height) VALUES (?, ?, ?, ?)",
                [(str(path), mtime, width, height) for path, mtime, width, height in rows],
            )

    def delete_missing(self, known_paths: set[str]) -> int:
        """Remove cache rows whose paths are no longer present on disk.

        Returns the number of rows deleted.  Call this periodically to stop
        the database growing without bound as photos are moved or deleted.
        """
        assert self._conn is not None, "PhotoMetadataCache must be used as a context manager"
        cursor = self._conn.execute("SELECT path FROM photo_metadata")
        stale = [row[0] for row in cursor if row[0] not in known_paths]
        if stale:
            self._conn.executemany("DELETE FROM photo_metadata WHERE path = ?", [(p,) for p in stale])
            self._conn.commit()
        stale_dims = [row[0] for row in self._conn.execute("SELECT path FROM photo_dimensions") if row[0] not in known_paths]
        if stale_dims:
            self._conn.executemany("DELETE FROM photo_dimensions WHERE path = ?", [(p,) for p in stale_dims])
            self._conn.commit()
        return len(stale)

