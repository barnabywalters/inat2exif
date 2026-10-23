import datetime as dt
from pathlib import Path

import pytest

from inat2exif.utils import get_version_group_key, parse_observation_datetime, parse_observation_id, parse_version_info


@pytest.mark.parametrize("value,expected", [
    (None, None), ([], None), ([42], 42), ([" 42 "], 42), (b"42", 42),
    ("  ", None), ("not-an-id", None), (42, 42),
])
def test_observation_ids_accept_metadata_encodings_and_reject_invalid_values(value, expected):
    assert parse_observation_id(value) == expected


def test_observation_datetime_preserves_offsets_and_missing_values():
    assert parse_observation_datetime(None) is None
    assert parse_observation_datetime("") is None
    assert parse_observation_datetime("2026-06-15T10:30:00Z") == dt.datetime(2026, 6, 15, 10, 30, tzinfo=dt.UTC)
    assert parse_observation_datetime("2026-06-15T12:30:00+02:00").utcoffset() == dt.timedelta(hours=2)


@pytest.mark.parametrize("name,base,version", [
    ("bee.jpg", "bee", 0), ("bee_v2.JPG", "bee", 2),
    ("bee_v02.jpg", "bee", 2), ("bee_vx.jpg", "bee_vx", 0),
])
def test_version_grouping_only_treats_numeric_suffixes_as_versions(name, base, version):
    path = Path("/photos") / name
    assert parse_version_info(path) == (base, version)
    assert get_version_group_key(path) == str(Path("/photos") / base)
