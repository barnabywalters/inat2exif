import os
import re
import json
import datetime as dt
from collections import defaultdict
from pathlib import Path

from inat2exif.utils import get_version_group_key, parse_version_info
from inat2exif.photo_cache import PhotoMetadataCache


def extract_exif_datetime(path: Path, pyexiv2_module) -> str | None:
    image = pyexiv2_module.Image(str(path))
    try:
        exif_data = image.read_exif()
        return exif_data.get("Exif.Photo.DateTimeOriginal")
    finally:
        close_fn = getattr(image, "close", None)
        if callable(close_fn):
            close_fn()


_EXIF_DATETIME_PATTERN = re.compile(r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})")
_JPEG_SUFFIXES = frozenset({".jpg", ".jpeg"})
_CACHE_COMMIT_INTERVAL = 500


def parse_exif_datetime(value: str) -> dt.datetime:
    """Parse an EXIF ``YYYY:MM:DD HH:MM:SS`` value, matching ``strptime`` results.

    Raises ValueError for invalid values just like ``strptime``.
    """
    match = _EXIF_DATETIME_PATTERN.fullmatch(value)
    if match is None:
        return dt.datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    return dt.datetime(*map(int, match.groups()))


def index_local_photos(
    root_path: Path,
    *,
    local_photo_cls,
    read_xmp_fn,
    parse_observation_id_from_xmp_fn,
    extract_exif_datetime_fn,
    logger,
    progress_cb=None,
    cancel_check=None,
    group_versions: bool = True,
    ignore_hidden_files: bool = True,
    photo_metadata_cache: PhotoMetadataCache | None = None,
):
    photos = []
    by_second = defaultdict(list)
    by_minute = defaultdict(list)
    by_id = {}
    version_groups = defaultdict(list)

    logger.info("Indexing local photos under %s", root_path)

    next_id = 1
    processed = 0
    skipped_directories = []

    def _walk_error(exc: OSError) -> None:
        directory_label = str(getattr(exc, "filename", "<unknown>"))
        skipped_directories.append(directory_label)
        logger.warning("Directory scan skipped %s due to error: %s", directory_label, exc)

    file_entries: list[tuple[str, str]] = []
    for current_dir, dirnames, filenames in os.walk(root_path, onerror=_walk_error):
        if ignore_hidden_files:
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            filenames = [name for name in filenames if not name.startswith(".")]
        file_entries.extend((current_dir, filename) for filename in filenames)
    total_files = len(file_entries)

    if progress_cb:
        progress_cb("index", f"Scanning local files ({total_files} files)", current=0, total=total_files)

    cached_rows = photo_metadata_cache.load_all_raw() if photo_metadata_cache is not None else {}
    uncommitted_cache_writes = 0

    try:
        for current_dir, filename in file_entries:
            processed += 1
            if cancel_check:
                cancel_check()

            if os.path.splitext(filename)[1].lower() not in _JPEG_SUFFIXES:
                continue
            path_str = os.path.join(current_dir, filename)

            # Try to serve metadata from cache to skip the expensive image reads.
            exif_dt: str | None = None
            xmp_data: dict = {}
            cache_hit = False
            if photo_metadata_cache is not None:
                try:
                    file_mtime = os.stat(path_str).st_mtime
                    cached = cached_rows.get(path_str)
                    if cached is not None and cached[0] == file_mtime:
                        exif_dt = cached[1]
                        xmp_data = json.loads(cached[2]) if cached[2] else {}
                        cache_hit = True
                except OSError:
                    pass  # stat failed – fall through to normal extraction

            path = Path(path_str)
            if not cache_hit:
                try:
                    exif_dt = extract_exif_datetime_fn(path)
                except Exception as exc:
                    logger.warning("Skipping %s due to EXIF read error: %s", path, exc)
                    continue
                if not exif_dt:
                    continue

                try:
                    xmp_data = read_xmp_fn(path)
                except Exception as exc:
                    logger.warning("Skipping %s due to XMP read error: %s", path, exc)
                    continue

                # Write back to cache so subsequent runs can skip these reads.
                if photo_metadata_cache is not None:
                    try:
                        photo_metadata_cache.put(path, file_mtime, exif_dt, xmp_data, commit=False)
                        uncommitted_cache_writes += 1
                        if uncommitted_cache_writes >= _CACHE_COMMIT_INTERVAL:
                            photo_metadata_cache.commit()
                            uncommitted_cache_writes = 0
                    except Exception as exc:
                        logger.debug("Photo metadata cache write failed for %s: %s", path, exc)

            if not exif_dt:
                continue

            existing_observation_id = parse_observation_id_from_xmp_fn(xmp_data)

            try:
                parsed = parse_exif_datetime(exif_dt)
            except ValueError:
                continue

            timestamp = parsed.timestamp()
            minute_timestamp = parsed.replace(second=0).timestamp()

            version_number = 0
            version_group = ""
            if group_versions:
                _base_name, version_number = parse_version_info(path)
                version_group = get_version_group_key(path)

            photo = local_photo_cls(
                id=next_id,
                path=path,
                exif_datetime_raw=exif_dt,
                timestamp=timestamp,
                minute_timestamp=minute_timestamp,
                existing_observation_id=existing_observation_id,
                version_group=version_group,
                version_number=version_number,
                embedded_xmp=xmp_data if existing_observation_id is not None else None,
            )
            next_id += 1

            photos.append(photo)
            by_id[photo.id] = photo
            by_second[photo.timestamp].append(photo)
            by_minute[photo.minute_timestamp].append(photo)
            if group_versions and version_group:
                version_groups[version_group].append(photo)

            if progress_cb and (len(photos) % 25 == 0):
                progress_cb("index", f"Indexed {len(photos)} JPEG files", current=processed, total=total_files)
    finally:
        if photo_metadata_cache is not None and uncommitted_cache_writes:
            try:
                photo_metadata_cache.commit()
            except Exception as exc:
                logger.debug("Photo metadata cache commit failed: %s", exc)

    if skipped_directories:
        logger.warning("Indexing skipped %s directories due to read errors", len(skipped_directories))
        if logger.isEnabledFor(10):
            for directory_label in skipped_directories[:20]:
                logger.debug("Skipped directory: %s", directory_label)

    if progress_cb:
        progress_cb("index", f"Finished indexing {len(photos)} JPEG files", current=processed, total=total_files)

    if group_versions:
        for _group_key, group_photos in version_groups.items():
            if len(group_photos) > 1:
                group_photos.sort(key=lambda p: p.version_number)
                sibling_ids = [p.id for p in group_photos]
                for photo in group_photos:
                    photo.version_siblings = sibling_ids

    return photos, by_second, by_minute, by_id
