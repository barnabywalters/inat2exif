"""Inventory and clearing of the on-disk caches kept under the cache directory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

OBSERVATION_STORE_FILENAME = "observations.db"
OBSERVATION_PHOTOS_DIRNAME = "observations"
PHOTO_METADATA_CACHE_FILENAME = "local_photos.db"
CV_SCORE_CACHE_FILENAME = "cv_scores.db"

_LEGACY_OBSERVATION_JSON = "observation.json"
_SQLITE_SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")


@dataclass(frozen=True)
class CacheSpec:
    name: str
    label: str
    description: str


CACHE_SPECS: tuple[CacheSpec, ...] = (
    CacheSpec(
        "observations",
        "iNaturalist observation data",
        "Your observations as downloaded from iNaturalist. Clearing it forces a full re-download on the next auto-match run.",
    ),
    CacheSpec(
        "observation_photos",
        "iNaturalist observation photos",
        "Observation photos used for image comparison and review. They are re-downloaded when needed.",
    ),
    CacheSpec(
        "local_photos",
        "Local photo metadata",
        "Dates, XMP metadata and image sizes read from your photo library. Clearing it makes the next scan re-read every file.",
    ),
    CacheSpec(
        "cv_scores",
        "Image comparison scores",
        "Similarity scores between observation photos and local photos. Clearing it makes ambiguous matches be recompared.",
    ),
)
CACHE_NAMES = tuple(spec.name for spec in CACHE_SPECS)


def _sqlite_files(path: Path) -> list[Path]:
    return [path.with_name(path.name + suffix) for suffix in _SQLITE_SIDECAR_SUFFIXES]


def _photo_dir_files(cache_dir: Path, *, observation_json: bool) -> list[Path]:
    root = cache_dir / OBSERVATION_PHOTOS_DIRNAME
    files: list[Path] = []
    for current_dir, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if (filename == _LEGACY_OBSERVATION_JSON) == observation_json:
                files.append(Path(current_dir) / filename)
    return files


def _cache_files(cache_dir: Path, name: str) -> list[Path]:
    if name == "observations":
        # Includes any legacy JSON layout that hasn't been migrated into the store yet.
        return (
            _sqlite_files(cache_dir / OBSERVATION_STORE_FILENAME)
            + sorted(cache_dir.glob("observations_*.json"))
            + _photo_dir_files(cache_dir, observation_json=True)
        )
    if name == "observation_photos":
        return _photo_dir_files(cache_dir, observation_json=False)
    if name == "local_photos":
        return _sqlite_files(cache_dir / PHOTO_METADATA_CACHE_FILENAME)
    if name == "cv_scores":
        return _sqlite_files(cache_dir / CV_SCORE_CACHE_FILENAME)
    raise KeyError(name)


def _remove_empty_dirs(root: Path) -> None:
    if not root.is_dir():
        return
    for current_dir, _dirnames, _filenames in os.walk(root, topdown=False):
        path = Path(current_dir)
        if path != root:
            try:
                path.rmdir()
            except OSError:
                pass


def describe_caches(cache_dir: Path) -> list[dict[str, object]]:
    entries = []
    for spec in CACHE_SPECS:
        size = 0
        count = 0
        for path in _cache_files(cache_dir, spec.name):
            try:
                size += path.stat().st_size
                count += 1
            except OSError:
                continue
        entries.append(
            {
                "name": spec.name,
                "label": spec.label,
                "description": spec.description,
                "size_bytes": size,
                "file_count": count,
            }
        )
    return entries


def clear_cache(cache_dir: Path, name: str) -> int:
    """Delete the files belonging to the named cache, returning bytes freed."""
    freed = 0
    for path in _cache_files(cache_dir, name):
        try:
            size = path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            continue
        freed += size
    if name in ("observations", "observation_photos"):
        _remove_empty_dirs(cache_dir / OBSERVATION_PHOTOS_DIRNAME)
    return freed


def clear_all_caches(cache_dir: Path) -> int:
    return sum(clear_cache(cache_dir, name) for name in CACHE_NAMES)


__all__ = [
    "CACHE_NAMES",
    "CACHE_SPECS",
    "CV_SCORE_CACHE_FILENAME",
    "OBSERVATION_PHOTOS_DIRNAME",
    "OBSERVATION_STORE_FILENAME",
    "PHOTO_METADATA_CACHE_FILENAME",
    "clear_all_caches",
    "clear_cache",
    "describe_caches",
]
