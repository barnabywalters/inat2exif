"""Persistent cache of computer-vision pair similarity scores.

A score is keyed on both image files' path, mtime and size plus an algorithm
version string, so a hit is exactly the score that would be recomputed. Lookups
are safe from worker threads (they read a preloaded dict); new scores are
buffered in memory and written by the owning thread in ``flush``.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 1

_FileKey = tuple[str, int, int]


def _file_key(path: Path | str) -> _FileKey | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size)


class CVScoreCache:
    def __init__(self, db_path: Path | str, algorithm_version: str) -> None:
        self._db_path = Path(db_path)
        self._algorithm_version = algorithm_version
        self._conn: sqlite3.Connection | None = None
        self._scores: dict[tuple, float] = {}
        self._pending: list[tuple] = []
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def __enter__(self) -> "CVScoreCache":
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            conn.execute("DROP TABLE IF EXISTS pair_scores")
            conn.execute(
                """
                CREATE TABLE pair_scores (
                    algorithm   TEXT NOT NULL,
                    obs_path    TEXT NOT NULL,
                    obs_mtime   INTEGER NOT NULL,
                    obs_size    INTEGER NOT NULL,
                    local_path  TEXT NOT NULL,
                    local_mtime INTEGER NOT NULL,
                    local_size  INTEGER NOT NULL,
                    score       REAL NOT NULL,
                    PRIMARY KEY (obs_path, local_path)
                )
                """
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        conn.execute("DELETE FROM pair_scores WHERE algorithm != ?", (self._algorithm_version,))
        conn.commit()
        for obs_path, obs_mtime, obs_size, local_path, local_mtime, local_size, score in conn.execute(
            "SELECT obs_path, obs_mtime, obs_size, local_path, local_mtime, local_size, score FROM pair_scores"
        ):
            self._scores[((obs_path, obs_mtime, obs_size), (local_path, local_mtime, local_size))] = score
        self._conn = conn
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._conn is None:
            return
        try:
            self.flush()
        finally:
            self._conn.close()
            self._conn = None

    def key(self, obs_path: Path | str, local_path: Path | str) -> tuple | None:
        obs_key = _file_key(obs_path)
        local_key = _file_key(local_path)
        if obs_key is None or local_key is None:
            return None
        return (obs_key, local_key)

    def get(self, key: tuple | None) -> float | None:
        if key is None:
            return None
        score = self._scores.get(key)
        with self._lock:
            if score is None:
                self.misses += 1
            else:
                self.hits += 1
        return score

    def put(self, key: tuple | None, score: float) -> None:
        if key is None:
            return
        with self._lock:
            self._scores[key] = score
            self._pending.append((self._algorithm_version, *key[0], *key[1], score))

    def flush(self) -> None:
        if self._conn is None:
            return
        with self._lock:
            pending, self._pending = self._pending, []
        if not pending:
            return
        with self._conn:
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO pair_scores
                    (algorithm, obs_path, obs_mtime, obs_size, local_path, local_mtime, local_size, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                pending,
            )
