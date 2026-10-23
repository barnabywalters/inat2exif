import datetime as dt
import re
from pathlib import Path

VERSION_PATTERN = re.compile(r"^(.*)_v(\d+)$")


def parse_version_info(path: Path) -> tuple[str, int]:
    """Parse filename for version suffix. Returns (base_name, version_number).
    Files without _v{x} suffix are treated as version 0.
    """
    stem = path.stem
    match = VERSION_PATTERN.match(stem)
    if match:
        base = match.group(1)
        version = int(match.group(2))
        return base, version
    return stem, 0


def get_version_group_key(path: Path) -> str:
    """Get the grouping key for versioned files. All versions of the same photo
    will share the same key.
    """
    base, _ = parse_version_info(path)
    return str(path.parent / base)


def parse_observation_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_observation_id(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, list):
        if not value:
            return None
        value = value[0]
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None
