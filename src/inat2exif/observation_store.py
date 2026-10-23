"""SQLite-backed store for raw iNaturalist observation JSON.

Each observation is stored as a zlib-compressed JSON blob (encoded with orjson
when available) (roughly 14x smaller
than the plain JSON files used previously) alongside its parsed ``updated_at``
timestamp, so the latest update can be found without decoding any blobs.

The schema version is tracked with ``PRAGMA user_version``; a mismatch causes
the table to be dropped and recreated (the data is re-fetchable from the API).
"""

import gc
import json
import os
import sqlite3
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

try:
    import orjson
except ImportError:  # pragma: no cover - orjson is a declared dependency
    orjson = None

from inat2exif.utils import parse_observation_id

SCHEMA_VERSION = 1
_COMPRESSION_LEVEL = 6
_DECOMPRESS_CHUNK_SIZE = 64
_DECOMPRESS_MAX_WORKERS = 8


def parse_api_datetime(value) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def observation_updated_at(obs: dict) -> datetime | None:
    parsed = parse_api_datetime(obs.get("updated_at"))
    if parsed is not None:
        return parsed
    updated_details = obs.get("updated_at_details")
    if isinstance(updated_details, dict):
        parsed = parse_api_datetime(updated_details.get("date"))
        if parsed is not None:
            return parsed
    # Fallback keeps incremental polling working for sparse payloads.
    return parse_api_datetime(obs.get("time_observed_at"))


def _dumps(obs: dict) -> bytes:
    if orjson is not None:
        try:
            return orjson.dumps(obs)
        except TypeError:
            pass  # e.g. integers beyond 64 bits; the stdlib encoder handles them
    return json.dumps(obs, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _loads(data: bytes) -> dict:
    return orjson.loads(data) if orjson is not None else json.loads(data)


def encode_observation(obs: dict) -> bytes:
    return zlib.compress(_dumps(obs), _COMPRESSION_LEVEL)


def decode_observation(blob: bytes) -> dict:
    return _loads(zlib.decompress(blob))


def _decompress_chunk(blobs: list[bytes]) -> list[bytes]:
    return [zlib.decompress(blob) for blob in blobs]


class ObservationStore:
    """Context-managed store of raw observation dicts keyed by observation ID."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> "ObservationStore":
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            conn.execute("DROP TABLE IF EXISTS observations")
            conn.execute(
                """
                CREATE TABLE observations (
                    id          INTEGER PRIMARY KEY,
                    updated_ts  REAL,
                    data        BLOB NOT NULL
                )
                """
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        self._conn = conn
        return self

    def __exit__(self, *_exc) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        assert self._conn is not None, "ObservationStore must be used as a context manager"
        return self._conn

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0])

    def ids(self) -> set[int]:
        return {int(row[0]) for row in self.conn.execute("SELECT id FROM observations")}

    def latest_updated_at(self) -> datetime | None:
        value = self.conn.execute("SELECT MAX(updated_ts) FROM observations").fetchone()[0]
        if value is None:
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc)

    def upsert_many(self, observations: list[dict]) -> int:
        rows = []
        for obs in observations:
            if not isinstance(obs, dict):
                continue
            obs_id = parse_observation_id(obs.get("id"))
            if obs_id is None:
                continue
            updated = observation_updated_at(obs)
            rows.append((obs_id, updated.timestamp() if updated else None, encode_observation(obs)))
        if rows:
            with self.conn:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO observations (id, updated_ts, data) VALUES (?, ?, ?)",
                    rows,
                )
        return len(rows)

    def load_all(self) -> list[dict]:
        blobs = [row[0] for row in self.conn.execute("SELECT data FROM observations ORDER BY id")]
        chunks = [blobs[i : i + _DECOMPRESS_CHUNK_SIZE] for i in range(0, len(blobs), _DECOMPRESS_CHUNK_SIZE)]
        workers = max(1, min(_DECOMPRESS_MAX_WORKERS, os.cpu_count() or 1, len(chunks)))
        # zlib releases the GIL, so worker threads decompress later chunks while this
        # thread parses earlier ones. Parsing thousands of nested dicts would also
        # trigger many pointless GC passes; the objects are all kept, so pause GC.
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            observations: list[dict] = []
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="obs-decompress") as executor:
                for chunk in executor.map(_decompress_chunk, chunks):
                    observations.extend(_loads(data) for data in chunk)
            return observations
        finally:
            if gc_was_enabled:
                gc.enable()

    def get(self, obs_id: int) -> dict | None:
        row = self.conn.execute("SELECT data FROM observations WHERE id = ?", (obs_id,)).fetchone()
        return decode_observation(row[0]) if row else None
