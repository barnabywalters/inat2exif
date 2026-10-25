import datetime as dt
import json
import re
import shutil
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# Allow importing inat2exif without requiring native cv2/pyexiv2 wheels in test env.
if "cv2" not in sys.modules:
	sys.modules["cv2"] = types.ModuleType("cv2")

if "pyexiv2" not in sys.modules:
	pyexiv2_stub = types.ModuleType("pyexiv2")
	pyexiv2_stub.Image = object
	pyexiv2_stub.xmp = types.SimpleNamespace(register_namespace=lambda *_args, **_kwargs: None)
	sys.modules["pyexiv2"] = pyexiv2_stub

from inat2exif import app as inat2exif


def _make_photo(
	photo_id: int,
	name: str,
	timestamp: float,
	minute_timestamp: float,
	siblings: list[int] | None = None,
	version_group: str = "group",
	version_number: int = 0,
) -> inat2exif.LocalPhoto:
	return inat2exif.LocalPhoto(
		id=photo_id,
		path=Path(name),
		exif_datetime_raw="2024:04:27 10:45:23",
		timestamp=timestamp,
		minute_timestamp=minute_timestamp,
		existing_observation_id=None,
		version_group=version_group,
		version_number=version_number,
		version_siblings=siblings or [],
	)


@pytest.fixture(autouse=True)
def block_http_calls(monkeypatch: pytest.MonkeyPatch) -> None:
	def _blocked_get(*_args, **_kwargs):
		raise AssertionError("HTTP requests are blocked in tests")

	monkeypatch.setattr(inat2exif.requests, "get", _blocked_get)


@pytest.fixture(autouse=True)
def isolate_default_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(inat2exif, "DEFAULT_CACHE_DIR", tmp_path / "default-cache")


def test_fetch_observations_uses_cache_and_requests_incremental_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	cache_dir = tmp_path / "cache"
	(cache_dir / "123").mkdir(parents=True, exist_ok=True)
	(cache_dir / "123" / "observation.json").write_text(
		json.dumps(
			{
				"id": 123,
				"time_observed_at": "2024-04-27T10:45:23Z",
				"updated_at": "2024-04-27T10:50:00Z",
				"photos": [],
				"uri": "u",
			}
		),
		encoding="utf-8",
	)

	requests_seen: list[str] = []

	class FakeResponse:
		def raise_for_status(self) -> None:
			return None

		def json(self) -> dict:
			return {"results": []}

	def fake_get(url: str, timeout: int):
		assert timeout == 30
		requests_seen.append(url)
		return FakeResponse()

	monkeypatch.setattr(inat2exif.requests, "get", fake_get)
	monkeypatch.setattr(inat2exif.time, "sleep", lambda _seconds: None)

	observations = inat2exif.fetch_observations("tester", cache_dir)

	assert len(requests_seen) == 1
	assert "updated_since=2024-04-27T10%3A50%3A00Z" in requests_seen[0]
	assert len(observations) == 1
	assert observations[0]["id"] == 123


def test_fetch_observations_writes_per_observation_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	cache_dir = tmp_path / "cache"
	requests_seen: list[str] = []

	class FakeResponse:
		def __init__(self, payload: dict):
			self._payload = payload

		def raise_for_status(self) -> None:
			return None

		def json(self) -> dict:
			return self._payload

	def fake_get(url: str, timeout: int):
		assert timeout == 30
		requests_seen.append(url)
		return FakeResponse(
			{
				"results": [
					{"id": 101, "time_observed_at": "2024-04-27T10:45:23Z", "photos": [], "uri": "u1"},
					{"id": 202, "time_observed_at": "2024-04-27T10:46:23Z", "photos": [], "uri": "u2"},
				]
			}
		)

	monkeypatch.setattr(inat2exif.requests, "get", fake_get)
	monkeypatch.setattr(inat2exif.time, "sleep", lambda _seconds: None)

	observations = inat2exif.fetch_observations("tester", cache_dir, per_page=100)

	assert len(observations) == 2
	assert len(requests_seen) == 1
	from inat2exif.observation_store import ObservationStore

	with ObservationStore(cache_dir / "observations.db") as store:
		assert store.ids() == {101, 202}
		assert store.get(101)["uri"] == "u1"


def test_cache_observation_photo_writes_thumbnail_in_observation_folder(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	cache_dir = tmp_path / "cache"

	class FakeResponse:
		content = b"jpeg-bytes"

		def raise_for_status(self) -> None:
			return None

	def fake_get(url: str, timeout: int):
		assert timeout == 30
		assert "small.jpeg" in url
		return FakeResponse()

	monkeypatch.setattr(inat2exif.requests, "get", fake_get)

	path = inat2exif.cache_observation_photo(
		{
			"id": 500,
			"observation_id": 321,
			"url": "https://example.invalid/photos/500/square.jpeg",
		},
		cache_dir,
	)

	assert path == cache_dir / "observations" / "321" / "photo_500.jpeg"
	assert path.exists()
	assert path.read_bytes() == b"jpeg-bytes"


def test_build_observation_records_stores_observation_id_in_photo_lookup() -> None:
	raw = [
		{
			"id": 42,
			"time_observed_at": "2024-04-27T10:45:23Z",
			"photos": [{"id": 500, "url": "https://example.invalid/square.jpg"}],
			"uri": "https://www.inaturalist.org/observations/42",
		}
	]

	_records, photo_lookup = inat2exif.build_observation_records(raw)

	assert photo_lookup[500]["observation_id"] == 42


def test_observation_image_path_backfills_missing_observation_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	state = inat2exif.MatcherState()
	state.ready = True
	state.cache_dir = tmp_path / "cache"

	obs = inat2exif.ObservationData(
		obs_id=77,
		observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC),
		photos=[{"id": 900, "url": "https://example.invalid/square.jpg"}],
		uri="https://www.inaturalist.org/observations/77",
	)
	match = inat2exif.MatchResult(
		observation=obs,
		status="ambiguous",
		candidate_photo_ids=[],
		scored_candidates=[],
	)

	state.matches = [match]
	state.matches_by_obs_id = {77: match}
	state.obs_photo_by_id = {900: {"id": 900, "url": "https://example.invalid/square.jpg"}}

	seen: dict[str, object] = {}

	def fake_cache_observation_photo(photo: dict, _cache_dir: Path) -> Path:
		seen.update(photo)
		return tmp_path / "cache" / "77" / "thumbnail.jpeg"

	monkeypatch.setattr(inat2exif, "cache_observation_photo", fake_cache_observation_photo)

	path = state.observation_image_path(900)

	assert path == tmp_path / "cache" / "77" / "thumbnail.jpeg"
	assert seen["observation_id"] == 77


def test_parse_version_info_detects_suffix() -> None:
	base, version = inat2exif.parse_version_info(Path("2024-04-27T104523s299_v1.JPG"))
	assert base == "2024-04-27T104523s299"
	assert version == 1


@pytest.mark.parametrize(
	"filename,expected_version",
	[
		("2024-04-27T104523s299.JPG", 0),
		("2024-04-27T104523s299_v1.JPG", 1),
	],
)
def test_get_version_group_key_groups_versions_together(filename: str, expected_version: int) -> None:
	path = Path("/photos/day") / filename
	base, version = inat2exif.parse_version_info(path)
	assert version == expected_version
	assert inat2exif.get_version_group_key(path) == "/photos/day/2024-04-27T104523s299"
	assert base == "2024-04-27T104523s299"


def test_index_local_photos_links_version_siblings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	# Copy versioned fixture files to temp directory
	src_dir = Path("tests/fixtures/real_cached_case/photos")
	base_name = "2024-04-27T150100s534"
	for suffix in (".JPG", "_v1.JPG"):
		shutil.copy2(src_dir / f"{base_name}{suffix}", tmp_path / f"{base_name}{suffix}")

	def fake_extract_exif_datetime(path: Path) -> str | None:
		if path.name.startswith(base_name):
			return "2024:04:27 15:01:00"
		return None

	monkeypatch.setattr(inat2exif, "extract_exif_datetime", fake_extract_exif_datetime)
	monkeypatch.setattr(inat2exif, "read_embedded_xmp", lambda _p: {})

	photos, _by_second, _by_minute, _by_id = inat2exif.index_local_photos(tmp_path, group_versions=True)
	assert len(photos) == 2

	sibling_sets = [set(photo.version_siblings) for photo in photos]
	assert all(len(siblings) == 2 for siblings in sibling_sets)

	groups = {photo.version_group for photo in photos}
	assert len(groups) == 1


def test_index_local_photos_ignores_hidden_files_and_folders_by_default(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	visible = tmp_path / "visible.JPG"
	hidden_file = tmp_path / ".hidden.JPG"
	hidden_dir = tmp_path / ".hidden-dir"
	hidden_dir.mkdir()
	hidden_nested = hidden_dir / "nested.JPG"

	for path in (visible, hidden_file, hidden_nested):
		path.write_bytes(b"jpeg")

	def fake_extract_exif_datetime(path: Path) -> str | None:
		if path.suffix.lower() in {".jpg", ".jpeg"}:
			return "2024:04:27 15:01:00"
		return None

	monkeypatch.setattr(inat2exif, "extract_exif_datetime", fake_extract_exif_datetime)
	monkeypatch.setattr(inat2exif, "read_embedded_xmp", lambda _p: {})

	photos, _by_second, _by_minute, _by_id = inat2exif.index_local_photos(tmp_path, group_versions=True)

	assert [photo.path.name for photo in photos] == ["visible.JPG"]


def test_index_local_photos_can_include_hidden_files_and_folders(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	visible = tmp_path / "visible.JPG"
	hidden_file = tmp_path / ".hidden.JPG"
	hidden_dir = tmp_path / ".hidden-dir"
	hidden_dir.mkdir()
	hidden_nested = hidden_dir / "nested.JPG"

	for path in (visible, hidden_file, hidden_nested):
		path.write_bytes(b"jpeg")

	def fake_extract_exif_datetime(path: Path) -> str | None:
		if path.suffix.lower() in {".jpg", ".jpeg"}:
			return "2024:04:27 15:01:00"
		return None

	monkeypatch.setattr(inat2exif, "extract_exif_datetime", fake_extract_exif_datetime)
	monkeypatch.setattr(inat2exif, "read_embedded_xmp", lambda _p: {})

	photos, _by_second, _by_minute, _by_id = inat2exif.index_local_photos(
		tmp_path,
		group_versions=True,
		ignore_hidden_files=False,
	)

	assert sorted(photo.path.name for photo in photos) == [".hidden.JPG", "nested.JPG", "visible.JPG"]


def test_analyze_auto_exact_selects_all_version_siblings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	observed_at = dt.datetime(2024, 4, 27, 10, 45, 23, tzinfo=dt.UTC)
	timestamp = observed_at.timestamp()
	minute_timestamp = observed_at.replace(second=0).timestamp()

	photo_a = _make_photo(
		1,
		str(tmp_path / "2024-04-27T104523s299.JPG"),
		timestamp,
		minute_timestamp,
		siblings=[1, 2],
		version_group="/photos/2024-04-27T104523s299",
		version_number=0,
	)
	photo_b = _make_photo(
		2,
		str(tmp_path / "2024-04-27T104523s299_v1.JPG"),
		timestamp,
		minute_timestamp,
		siblings=[1, 2],
		version_group="/photos/2024-04-27T104523s299",
		version_number=1,
	)

	def fake_fetch_observations(*_args, **_kwargs):
		return [
			{
				"id": 42,
				"time_observed_at": "2024-04-27T10:45:23Z",
				"photos": [{"id": 1001, "url": "https://example.invalid/square.jpg"}],
				"uri": "https://www.inaturalist.org/observations/42",
			}
		]

	def fake_index_local_photos(*_args, **_kwargs):
		return [photo_a, photo_b], {timestamp: [photo_a, photo_b]}, {minute_timestamp: [photo_a, photo_b]}, {1: photo_a, 2: photo_b}

	monkeypatch.setattr(inat2exif, "fetch_observations", fake_fetch_observations)
	monkeypatch.setattr(inat2exif, "index_local_photos", fake_index_local_photos)

	state = inat2exif.MatcherState()
	state.group_versions = True

	summary = state.analyze("tester", tmp_path)
	assert summary["total"] == 1

	match = state.matches[0]
	assert match.status == "auto-exact"
	assert match.selected_photo_ids == {1, 2}

	detail = state.observation_detail(42)
	assert len(detail["candidates"]) == 1
	assert detail["candidates"][0]["selected"] is True
	assert "2024-04-27T104523s299.JPG" in detail["candidates"][0]["name"]
	assert "2024-04-27T104523s299_v1.JPG" in detail["candidates"][0]["name"]


def test_update_selection_expands_to_siblings() -> None:
	state = inat2exif.MatcherState()
	state.ready = True
	state.group_versions = True

	obs = inat2exif.ObservationData(obs_id=77, observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC), photos=[], uri="u")
	match = inat2exif.MatchResult(observation=obs, status="ambiguous", candidate_photo_ids=[1, 2], scored_candidates=[])
	state.matches = [match]
	state.matches_by_obs_id = {77: match}
	state.local_photos_by_id = {
		1: _make_photo(1, "a.JPG", 1.0, 1.0, siblings=[1, 2], version_number=0),
		2: _make_photo(2, "a_v1.JPG", 1.0, 1.0, siblings=[1, 2], version_number=1),
	}

	result = state.update_selection(77, [2])
	assert result["selected_count"] == 2
	assert state.matches_by_obs_id[77].selected_photo_ids == {1, 2}


def test_save_selected_writes_all_group_versions(monkeypatch: pytest.MonkeyPatch) -> None:
	written: list[tuple[Path, inat2exif.ObservationData]] = []

	def fake_write_observation_metadata(path: Path, observation: inat2exif.ObservationData) -> None:
		written.append((path, observation))

	monkeypatch.setattr(inat2exif, "write_observation_metadata", fake_write_observation_metadata)

	state = inat2exif.MatcherState()
	state.ready = True
	state.group_versions = True

	obs = inat2exif.ObservationData(obs_id=88, observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC), photos=[], uri="u")
	match = inat2exif.MatchResult(
		observation=obs,
		status="auto-exact",
		candidate_photo_ids=[1, 2],
		scored_candidates=[(1, 1.0), (2, 1.0)],
		selected_photo_ids={2},
	)

	state.matches = [match]
	state.matches_by_obs_id = {88: match}
	state.local_photos_by_id = {
		1: _make_photo(1, "a.JPG", 1.0, 1.0, siblings=[1, 2], version_number=0),
		2: _make_photo(2, "a_v1.JPG", 1.0, 1.0, siblings=[1, 2], version_number=1),
	}

	result = state.save_selected()
	assert result["saved"] == 2
	assert result["failed"] == 0
	assert {item[0].name for item in written} == {"a.JPG", "a_v1.JPG"}
	assert all(item[1].obs_id == 88 for item in written)


def test_build_observation_metadata_xmp_maps_core_naturtag_fields() -> None:
	raw_obs = {
		"id": 32989972,
		"uri": "https://www.inaturalist.org/observations/32989972",
		"place_guess": "Jester Park, Granger, Iowa",
		"location": "41.78763132,-93.79020984",
		"captive": False,
		"taxon": {
			"id": 120183,
			"name": "Araneus marmoreus",
			"rank": "species",
			"preferred_common_name": "Marbled Orbweaver",
		},
		"user": {
			"login": "jkcook",
			"name": "Jordan Cook",
		},
	}
	observed = dt.datetime(2019, 9, 18, 15, 56, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
	fields = inat2exif.build_observation_metadata_xmp(raw_obs, 32989972, raw_obs["uri"], observed)

	assert fields["Xmp.dwc.occurrenceID"] == raw_obs["uri"]
	assert fields["Xmp.dwc.catalogNumber"] == "32989972"
	assert fields["Xmp.dwc.datasetName"] == "iNaturalist observations"
	assert fields["Xmp.dwc.eventDate"] == observed.isoformat()
	assert fields["Xmp.dwc.recordedBy"] == "Jordan Cook"
	assert fields["Xmp.dwc.inaturalistLogin"] == "jkcook"
	assert fields["Xmp.dwc.scientificName"] == "Araneus marmoreus"
	assert fields["Xmp.dwc.vernacularName"] == "Marbled Orbweaver"
	assert fields["Xmp.dwc.decimalLatitude"] == "41.78763132"
	assert fields["Xmp.dwc.decimalLongitude"] == "-93.79020984"


def test_analyze_marks_stale_tagged_photos_dirty_and_selected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	observed_at = dt.datetime(2024, 4, 27, 10, 45, 23, tzinfo=dt.UTC)
	timestamp = observed_at.timestamp()
	minute_timestamp = observed_at.replace(second=0).timestamp()

	photo = _make_photo(
		1,
		str(tmp_path / "2024-04-27T104523s299.JPG"),
		timestamp,
		minute_timestamp,
		siblings=[1],
		version_group="/photos/2024-04-27T104523s299",
		version_number=0,
	)
	photo.existing_observation_id = 42

	def fake_fetch_observations(*_args, **_kwargs):
		return [
			{
				"id": 42,
				"time_observed_at": "2024-04-27T10:45:23Z",
				"photos": [{"id": 1001, "url": "https://example.invalid/square.jpg"}],
				"uri": "https://www.inaturalist.org/observations/42",
			}
		]

	def fake_index_local_photos(*_args, **_kwargs):
		return [photo], {timestamp: [photo]}, {minute_timestamp: [photo]}, {1: photo}

	monkeypatch.setattr(inat2exif, "fetch_observations", fake_fetch_observations)
	monkeypatch.setattr(inat2exif, "index_local_photos", fake_index_local_photos)
	monkeypatch.setattr(inat2exif, "metadata_refresh_needed_with_details", lambda _path, _obs, **_kwargs: (True, ["MISSING: Xmp.dwc.eventDate"]))

	state = inat2exif.MatcherState()
	state.group_versions = True
	state.analyze("tester", tmp_path)

	assert state.refresh_dirty_photo_ids == {1}
	assert state.matches[0].selected_photo_ids == {1}
	assert state.local_photos_by_id[1].metadata_refresh_needed is True


def test_save_selected_includes_refresh_dirty_queue(monkeypatch: pytest.MonkeyPatch) -> None:
	written: list[tuple[Path, inat2exif.ObservationData]] = []

	def fake_write_observation_metadata(path: Path, observation: inat2exif.ObservationData) -> None:
		written.append((path, observation))

	monkeypatch.setattr(inat2exif, "write_observation_metadata", fake_write_observation_metadata)

	state = inat2exif.MatcherState()
	state.ready = True
	state.group_versions = True

	obs = inat2exif.ObservationData(obs_id=88, observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC), photos=[], uri="u")
	match = inat2exif.MatchResult(
		observation=obs,
		status="ambiguous",
		candidate_photo_ids=[1],
		scored_candidates=[],
		selected_photo_ids=set(),
	)

	photo = _make_photo(1, "a.JPG", 1.0, 1.0, siblings=[1], version_number=0)
	photo.existing_observation_id = 88
	photo.metadata_refresh_needed = True

	state.matches = [match]
	state.matches_by_obs_id = {88: match}
	state.local_photos_by_id = {1: photo}
	state.refresh_dirty_photo_ids = {1}

	result = state.save_selected()
	assert result["saved"] == 1
	assert result["failed"] == 0
	assert [item[0].name for item in written] == ["a.JPG"]
	assert state.refresh_dirty_photo_ids == set()
	assert state.local_photos_by_id[1].metadata_refresh_needed is False


def test_auto_cv_keeps_non_versioned_selection_and_matchable_unselected_is_actionable() -> None:
	chosen = _make_photo(76, "single.JPG", 10.0, 10.0, siblings=[], version_number=0)
	version_a = _make_photo(77, "group.JPG", 10.0, 10.0, siblings=[77, 78], version_number=0)
	version_b = _make_photo(78, "group_v1.JPG", 10.0, 10.0, siblings=[77, 78], version_number=1)
	by_id = {76: chosen, 77: version_a, 78: version_b}

	expanded = inat2exif.expand_with_version_siblings({76}, by_id)
	assert expanded == {76}

	obs = inat2exif.ObservationData(obs_id=1, observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC), photos=[], uri="u")
	match = inat2exif.MatchResult(
		observation=obs,
		status="auto-cv",
		candidate_photo_ids=[76, 77, 78],
		scored_candidates=[(76, 0.9), (77, 0.2), (78, 0.1)],
		selected_photo_ids=set(),
	)
	assert inat2exif.observation_visual_state(match) == "ambiguous"
	assert inat2exif.tree_status_key(match) == "actionable"


def test_same_second_multi_obs_auto_cv_when_unique_strong_matches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	obs_time = dt.datetime(2024, 4, 27, 15, 1, 0, tzinfo=dt.UTC)
	minute_ts = obs_time.replace(second=0).timestamp()

	# Local images are in the same minute, but not exact second, so exact matching is impossible.
	photo_a = _make_photo(1, str(tmp_path / "a.JPG"), obs_time.replace(second=10).timestamp(), minute_ts, siblings=[1], version_group="grp-a")
	photo_b = _make_photo(2, str(tmp_path / "b.JPG"), obs_time.replace(second=20).timestamp(), minute_ts, siblings=[2], version_group="grp-b")

	def fake_fetch_observations(*_args, **_kwargs):
		return [
			{
				"id": 210387504,
				"time_observed_at": "2024-04-27T15:01:00Z",
				"photos": [{"id": 1001, "url": "https://example.invalid/obs1.jpg"}],
				"uri": "https://www.inaturalist.org/observations/210387504",
			},
			{
				"id": 210387503,
				"time_observed_at": "2024-04-27T15:01:00Z",
				"photos": [{"id": 1002, "url": "https://example.invalid/obs2.jpg"}],
				"uri": "https://www.inaturalist.org/observations/210387503",
			},
		]

	def fake_index_local_photos(*_args, **_kwargs):
		by_second: dict[float, list[inat2exif.LocalPhoto]] = {}
		by_minute = {minute_ts: [photo_a, photo_b]}
		by_id = {1: photo_a, 2: photo_b}
		return [photo_a, photo_b], by_second, by_minute, by_id

	def fake_cache_observation_photo(photo: dict, _cache_dir: Path) -> Path:
		url = str(photo.get("url", ""))
		if url.endswith("obs1.jpg"):
			return tmp_path / "obs1.jpg"
		return tmp_path / "obs2.jpg"

	def fake_score_candidates(observation_photo_paths: list[Path], _candidates: list[inat2exif.LocalPhoto], **_kwargs) -> list[tuple[int, float]]:
		name = observation_photo_paths[0].name if observation_photo_paths else ""
		if name == "obs1.jpg":
			return [(1, 0.92), (2, 0.21)]
		return [(2, 0.90), (1, 0.22)]

	monkeypatch.setattr(inat2exif, "fetch_observations", fake_fetch_observations)
	monkeypatch.setattr(inat2exif, "index_local_photos", fake_index_local_photos)
	monkeypatch.setattr(inat2exif, "cache_observation_photo", fake_cache_observation_photo)
	monkeypatch.setattr(inat2exif, "score_candidates", fake_score_candidates)
	monkeypatch.setattr(inat2exif, "score_candidates_per_photo", lambda paths, cands, **_kw: [fake_score_candidates(paths, cands)])

	state = inat2exif.MatcherState()
	state.group_versions = True
	state.analyze("tester", tmp_path)

	match_a = state.matches_by_obs_id[210387504]
	match_b = state.matches_by_obs_id[210387503]

	assert match_a.status == "auto-cv"
	assert match_b.status == "auto-cv"
	assert match_a.selected_photo_ids == {1}
	assert match_b.selected_photo_ids == {2}


def test_same_second_multi_obs_ambiguous_when_no_clear_cv_assignment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	obs_time = dt.datetime(2024, 4, 27, 15, 1, 0, tzinfo=dt.UTC)
	minute_ts = obs_time.replace(second=0).timestamp()

	photo_a = _make_photo(1, str(tmp_path / "a.JPG"), obs_time.replace(second=10).timestamp(), minute_ts, siblings=[1], version_group="grp-a")
	photo_b = _make_photo(2, str(tmp_path / "b.JPG"), obs_time.replace(second=20).timestamp(), minute_ts, siblings=[2], version_group="grp-b")

	def fake_fetch_observations(*_args, **_kwargs):
		return [
			{
				"id": 210387504,
				"time_observed_at": "2024-04-27T15:01:00Z",
				"photos": [{"id": 1001, "url": "https://example.invalid/obs1.jpg"}],
				"uri": "https://www.inaturalist.org/observations/210387504",
			},
			{
				"id": 210387503,
				"time_observed_at": "2024-04-27T15:01:00Z",
				"photos": [{"id": 1002, "url": "https://example.invalid/obs2.jpg"}],
				"uri": "https://www.inaturalist.org/observations/210387503",
			},
		]

	def fake_index_local_photos(*_args, **_kwargs):
		by_second: dict[float, list[inat2exif.LocalPhoto]] = {}
		by_minute = {minute_ts: [photo_a, photo_b]}
		by_id = {1: photo_a, 2: photo_b}
		return [photo_a, photo_b], by_second, by_minute, by_id

	def fake_cache_observation_photo(photo: dict, _cache_dir: Path) -> Path:
		url = str(photo.get("url", ""))
		if url.endswith("obs1.jpg"):
			return tmp_path / "obs1.jpg"
		return tmp_path / "obs2.jpg"

	def fake_score_candidates(observation_photo_paths: list[Path], _candidates: list[inat2exif.LocalPhoto], **_kwargs) -> list[tuple[int, float]]:
		name = observation_photo_paths[0].name if observation_photo_paths else ""
		if name == "obs1.jpg":
			return [(1, 0.84), (2, 0.82)]
		return [(2, 0.83), (1, 0.81)]

	monkeypatch.setattr(inat2exif, "fetch_observations", fake_fetch_observations)
	monkeypatch.setattr(inat2exif, "index_local_photos", fake_index_local_photos)
	monkeypatch.setattr(inat2exif, "cache_observation_photo", fake_cache_observation_photo)
	monkeypatch.setattr(inat2exif, "score_candidates", fake_score_candidates)
	monkeypatch.setattr(inat2exif, "score_candidates_per_photo", lambda paths, cands, **_kw: [fake_score_candidates(paths, cands)])

	state = inat2exif.MatcherState()
	state.group_versions = True
	state.analyze("tester", tmp_path)

	match_a = state.matches_by_obs_id[210387504]
	match_b = state.matches_by_obs_id[210387503]

	assert match_a.status == "ambiguous"
	assert match_b.status == "ambiguous"
	assert match_a.selected_photo_ids == set()
	assert match_b.selected_photo_ids == set()


def test_same_minute_shared_group_perfect_for_multiple_obs_is_ambiguous(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	# Regression for obs:210387504 and obs:210387503 style cases:
	# one shared image version group strongly matches both observations in the same minute.
	obs_time_a = dt.datetime(2024, 4, 27, 15, 1, 0, tzinfo=dt.UTC)
	obs_time_b = dt.datetime(2024, 4, 27, 15, 1, 30, tzinfo=dt.UTC)
	minute_ts = obs_time_a.replace(second=0).timestamp()

	photo_base = _make_photo(
		69,
		str(tmp_path / "2024-04-27T150100s534.JPG"),
		obs_time_a.replace(second=12).timestamp(),
		minute_ts,
		siblings=[69, 70],
		version_group="shared-150100s534",
		version_number=0,
	)
	photo_v1 = _make_photo(
		70,
		str(tmp_path / "2024-04-27T150100s534_v1.JPG"),
		obs_time_a.replace(second=12).timestamp(),
		minute_ts,
		siblings=[69, 70],
		version_group="shared-150100s534",
		version_number=1,
	)

	def fake_fetch_observations(*_args, **_kwargs):
		return [
			{
				"id": 210387504,
				"time_observed_at": "2024-04-27T15:01:00Z",
				"photos": [{"id": 1001, "url": "https://example.invalid/obs-a.jpg"}],
				"uri": "https://www.inaturalist.org/observations/210387504",
			},
			{
				"id": 210387503,
				"time_observed_at": "2024-04-27T15:01:30Z",
				"photos": [{"id": 1002, "url": "https://example.invalid/obs-b.jpg"}],
				"uri": "https://www.inaturalist.org/observations/210387503",
			},
		]

	def fake_index_local_photos(*_args, **_kwargs):
		by_second: dict[float, list[inat2exif.LocalPhoto]] = {}
		by_minute = {minute_ts: [photo_base, photo_v1]}
		by_id = {69: photo_base, 70: photo_v1}
		return [photo_base, photo_v1], by_second, by_minute, by_id

	def fake_cache_observation_photo(photo: dict, _cache_dir: Path) -> Path:
		url = str(photo.get("url", ""))
		if url.endswith("obs-a.jpg"):
			return tmp_path / "obs-a.jpg"
		return tmp_path / "obs-b.jpg"

	def fake_score_candidates(_observation_photo_paths: list[Path], _candidates: list[inat2exif.LocalPhoto], **_kwargs) -> list[tuple[int, float]]:
		# Perfect visual match for both versions for both observations, i.e. CV cannot disambiguate.
		return [(69, 1.0), (70, 1.0)]

	monkeypatch.setattr(inat2exif, "fetch_observations", fake_fetch_observations)
	monkeypatch.setattr(inat2exif, "index_local_photos", fake_index_local_photos)
	monkeypatch.setattr(inat2exif, "cache_observation_photo", fake_cache_observation_photo)
	monkeypatch.setattr(inat2exif, "score_candidates", fake_score_candidates)
	monkeypatch.setattr(inat2exif, "score_candidates_per_photo", lambda paths, cands, **_kw: [fake_score_candidates(paths, cands)])

	state = inat2exif.MatcherState()
	state.group_versions = True
	state.analyze("tester", tmp_path)

	match_a = state.matches_by_obs_id[210387504]
	match_b = state.matches_by_obs_id[210387503]

	assert match_a.status == "ambiguous"
	assert match_b.status == "ambiguous"
	assert match_a.selected_photo_ids == set()
	assert match_b.selected_photo_ids == set()


def test_real_cached_case_shared_group_never_selected_for_both(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	cache_path = Path("tests/fixtures/real_cached_case/observations_subset.json")
	assert cache_path.exists(), "Expected cached observations file for regression test"

	with cache_path.open("r", encoding="utf-8") as handle:
		cached = json.load(handle)

	by_obs_id = {int(item["id"]): item for item in cached}
	obs_a = by_obs_id.get(210387504)
	obs_b = by_obs_id.get(210387503)
	assert obs_a is not None and obs_b is not None, "Expected obs 210387504 and 210387503 in cache"

	def fake_fetch_observations(*_args, **_kwargs):
		return [obs_a, obs_b]

	def fake_extract_exif_datetime(path: Path) -> str | None:
		# Parse timestamps from filenames like 2024-04-27T150100s534_v1.JPG.
		match = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2})(\d{2})(\d{2})", path.stem)
		if not match:
			return None
		year, month, day, hour, minute, second = match.groups()
		return f"{year}:{month}:{day} {hour}:{minute}:{second}"

	def fake_score_candidates(_obs_paths: list[Path], candidates: list[inat2exif.LocalPhoto], **_kwargs) -> list[tuple[int, float]]:
		# Keep this fixture deterministic and independent of cv2 availability.
		return [(candidate.id, 1.0) for candidate in candidates]

	monkeypatch.setattr(inat2exif, "fetch_observations", fake_fetch_observations)
	monkeypatch.setattr(inat2exif, "extract_exif_datetime", fake_extract_exif_datetime)
	monkeypatch.setattr(inat2exif, "read_embedded_xmp", lambda _path: {})
	monkeypatch.setattr(inat2exif, "score_candidates", fake_score_candidates)
	monkeypatch.setattr(inat2exif, "score_candidates_per_photo", lambda paths, cands, **_kw: [fake_score_candidates(paths, cands)])

	state = inat2exif.MatcherState()
	state.cache_dir = tmp_path / "cache"
	state.group_versions = True
	state.analyze("barnabywalters", Path("tests/fixtures/real_cached_case/photos"))

	match_a = state.matches_by_obs_id[210387504]
	match_b = state.matches_by_obs_id[210387503]

	assert match_a.status == "ambiguous"
	assert match_b.status == "ambiguous"
	assert match_a.selected_photo_ids == set()
	assert match_b.selected_photo_ids == set()

	detail_a = state.observation_detail(210387504)
	detail_b = state.observation_detail(210387503)

	assert any("2024-04-27T150100s534_v1.JPG" in c["name"] for c in detail_a["candidates"])
	assert any("2024-04-27T150100s534_v1.JPG" in c["name"] for c in detail_b["candidates"])


def test_same_second_regression_keeps_all_manual_candidates_for_each_observation(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	"""Regression for 2026-04-24T2225xx photos: same-second observations must not lock to one exact candidate."""
	fixture_root = Path("tests/fixtures/regression_2026_04_24")
	cache_dir = tmp_path / "cache"
	shutil.copytree(
		fixture_root / "cache",
		cache_dir,
		ignore=shutil.ignore_patterns("*.db", "*.db-*"),
	)
	obs_ids = [353114080, 353114110, 353114116, 353114115, 353114131]

	raw_observations = []
	for obs_id in obs_ids:
		obs_path = fixture_root / "cache" / str(obs_id) / "observation.json"
		assert obs_path.exists(), f"Missing observation fixture for {obs_id}"
		with obs_path.open("r", encoding="utf-8") as handle:
			raw_observations.append(json.load(handle))

	expected_by_obs = {
		353114080: "2026-04-24T222500s433_1_v1.jpg",
		353114110: "2026-04-24T222525s794_1_v1.jpg",
		353114116: "2026-04-24T222537s051_1_v1.jpg",
		353114115: "2026-04-24T222544s460_1.jpg",
		353114131: "2026-04-24T222555s742.jpg",
	}

	def fake_fetch_observations(*_args, **_kwargs):
		return raw_observations

	def fake_extract_exif_datetime(path: Path) -> str | None:
		# Parse EXIF-like timestamp from filenames such as 2026-04-24T222525s794_1_v1.jpg.
		match = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2})(\d{2})(\d{2})", path.stem)
		if not match:
			return None
		year, month, day, hour, minute, second = match.groups()
		return f"{year}:{month}:{day} {hour}:{minute}:{second}"

	monkeypatch.setattr(inat2exif, "fetch_observations", fake_fetch_observations)
	monkeypatch.setattr(inat2exif, "extract_exif_datetime", fake_extract_exif_datetime)
	monkeypatch.setattr(inat2exif, "read_embedded_xmp", lambda _path: {})

	state = inat2exif.MatcherState()
	state.group_versions = True
	state.cache_dir = cache_dir
	state.analyze("barnabywalters", fixture_root)

	local_id_by_name = {photo.path.name: photo.id for photo in state.local_photos_by_id.values()}

	for obs_id, expected_name in expected_by_obs.items():
		detail = state.observation_detail(obs_id)
		candidate_names = [item["name"] for item in detail["candidates"]]
		assert len(detail["candidates"]) == 5
		assert any(expected_name in name for name in candidate_names), (
			f"Expected {expected_name} to be manually selectable for obs {obs_id}; "
			f"got candidates: {candidate_names}"
		)

		selected_photo_id = local_id_by_name[expected_name]
		state.update_selection(obs_id, [selected_photo_id])
		assert selected_photo_id in state.matches_by_obs_id[obs_id].selected_photo_ids


def test_single_obs_multi_photo_n_to_n_all_confident_auto_selects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	"""Observation with 2 photos, 2 local photos in same minute, all scoring above threshold.

	The single-winner margin check fails (both locals score well), but the N-to-N
	rule should auto-select all local photos as "auto-cv".
	"""
	obs_time = dt.datetime(2024, 4, 27, 15, 1, 0, tzinfo=dt.UTC)
	minute_ts = obs_time.replace(second=0).timestamp()

	# Use distinct exif_datetime_raw so minute_groups has len > 1, preventing auto-minute.
	photo_a = inat2exif.LocalPhoto(
		id=1, path=tmp_path / "a.JPG",
		exif_datetime_raw="2024:04:27 15:01:10",
		timestamp=obs_time.replace(second=10).timestamp(),
		minute_timestamp=minute_ts,
		existing_observation_id=None,
		version_group="grp-a", version_number=0, version_siblings=[1],
	)
	photo_b = inat2exif.LocalPhoto(
		id=2, path=tmp_path / "b.JPG",
		exif_datetime_raw="2024:04:27 15:01:20",
		timestamp=obs_time.replace(second=20).timestamp(),
		minute_timestamp=minute_ts,
		existing_observation_id=None,
		version_group="grp-b", version_number=0, version_siblings=[2],
	)

	def fake_fetch_observations(*_args, **_kwargs):
		return [
			{
				"id": 300001,
				"time_observed_at": "2024-04-27T15:01:00Z",
				"photos": [
					{"id": 2001, "url": "https://example.invalid/obs1.jpg"},
					{"id": 2002, "url": "https://example.invalid/obs2.jpg"},
				],
				"uri": "https://www.inaturalist.org/observations/300001",
			}
		]

	def fake_index_local_photos(*_args, **_kwargs):
		by_second: dict[float, list[inat2exif.LocalPhoto]] = {}
		by_minute = {minute_ts: [photo_a, photo_b]}
		by_id = {1: photo_a, 2: photo_b}
		return [photo_a, photo_b], by_second, by_minute, by_id

	def fake_cache_observation_photo(photo: dict, _cache_dir: Path) -> Path:
		return tmp_path / str(photo.get("id", "x"))

	def fake_score_candidates(_obs_paths: list[Path], _candidates: list[inat2exif.LocalPhoto], **_kwargs) -> list[tuple[int, float]]:
		# Both candidates score above threshold, but the margin (0.03) is below CV_MARGIN_THRESHOLD (0.08).
		return [(1, 0.85), (2, 0.82)]

	monkeypatch.setattr(inat2exif, "fetch_observations", fake_fetch_observations)
	monkeypatch.setattr(inat2exif, "index_local_photos", fake_index_local_photos)
	monkeypatch.setattr(inat2exif, "cache_observation_photo", fake_cache_observation_photo)
	monkeypatch.setattr(inat2exif, "score_candidates", fake_score_candidates)
	monkeypatch.setattr(inat2exif, "score_candidates_per_photo", lambda paths, cands, **_kw: [fake_score_candidates(paths, cands)])

	state = inat2exif.MatcherState()
	state.group_versions = True
	state.analyze("tester", tmp_path)

	match = state.matches_by_obs_id[300001]
	assert match.status == "auto-cv"
	assert match.selected_photo_ids == {1, 2}


def test_single_obs_multi_photo_n_to_n_one_below_threshold_stays_ambiguous(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	"""N-to-N case where one local photo's score is below the threshold — should stay ambiguous."""
	obs_time = dt.datetime(2024, 4, 27, 15, 1, 0, tzinfo=dt.UTC)
	minute_ts = obs_time.replace(second=0).timestamp()

	# Use distinct exif_datetime_raw so minute_groups has len > 1, preventing auto-minute.
	photo_a = inat2exif.LocalPhoto(
		id=1, path=tmp_path / "a.JPG",
		exif_datetime_raw="2024:04:27 15:01:10",
		timestamp=obs_time.replace(second=10).timestamp(),
		minute_timestamp=minute_ts,
		existing_observation_id=None,
		version_group="grp-a", version_number=0, version_siblings=[1],
	)
	photo_b = inat2exif.LocalPhoto(
		id=2, path=tmp_path / "b.JPG",
		exif_datetime_raw="2024:04:27 15:01:20",
		timestamp=obs_time.replace(second=20).timestamp(),
		minute_timestamp=minute_ts,
		existing_observation_id=None,
		version_group="grp-b", version_number=0, version_siblings=[2],
	)

	def fake_fetch_observations(*_args, **_kwargs):
		return [
			{
				"id": 300002,
				"time_observed_at": "2024-04-27T15:01:00Z",
				"photos": [
					{"id": 2001, "url": "https://example.invalid/obs1.jpg"},
					{"id": 2002, "url": "https://example.invalid/obs2.jpg"},
				],
				"uri": "https://www.inaturalist.org/observations/300002",
			}
		]

	def fake_index_local_photos(*_args, **_kwargs):
		by_second: dict[float, list[inat2exif.LocalPhoto]] = {}
		by_minute = {minute_ts: [photo_a, photo_b]}
		by_id = {1: photo_a, 2: photo_b}
		return [photo_a, photo_b], by_second, by_minute, by_id

	def fake_cache_observation_photo(photo: dict, _cache_dir: Path) -> Path:
		return tmp_path / str(photo.get("id", "x"))

	def fake_score_candidates(_obs_paths: list[Path], _candidates: list[inat2exif.LocalPhoto], **_kwargs) -> list[tuple[int, float]]:
		# Top is above threshold, second is below — margin also fails single-winner (0.03 < 0.08).
		# min_score (0.27) is below CV_SCORE_THRESHOLD (0.28), so N-to-N should not trigger.
		return [(1, 0.30), (2, 0.27)]

	monkeypatch.setattr(inat2exif, "fetch_observations", fake_fetch_observations)
	monkeypatch.setattr(inat2exif, "index_local_photos", fake_index_local_photos)
	monkeypatch.setattr(inat2exif, "cache_observation_photo", fake_cache_observation_photo)
	monkeypatch.setattr(inat2exif, "score_candidates", fake_score_candidates)
	monkeypatch.setattr(inat2exif, "score_candidates_per_photo", lambda paths, cands, **_kw: [fake_score_candidates(paths, cands)])

	state = inat2exif.MatcherState()
	state.group_versions = True
	state.analyze("tester", tmp_path)

	match = state.matches_by_obs_id[300002]
	assert match.status == "ambiguous"
	assert match.selected_photo_ids == set()


def test_single_obs_multi_photo_n_to_n_count_mismatch_stays_ambiguous(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	"""More local photos than observation photos — N-to-N count check should not trigger."""
	obs_time = dt.datetime(2024, 4, 27, 15, 1, 0, tzinfo=dt.UTC)
	minute_ts = obs_time.replace(second=0).timestamp()

	# Use distinct exif_datetime_raw so minute_groups has len > 1, preventing auto-minute.
	photo_a = inat2exif.LocalPhoto(
		id=1, path=tmp_path / "a.JPG",
		exif_datetime_raw="2024:04:27 15:01:10",
		timestamp=obs_time.replace(second=10).timestamp(),
		minute_timestamp=minute_ts,
		existing_observation_id=None,
		version_group="grp-a", version_number=0, version_siblings=[1],
	)
	photo_b = inat2exif.LocalPhoto(
		id=2, path=tmp_path / "b.JPG",
		exif_datetime_raw="2024:04:27 15:01:20",
		timestamp=obs_time.replace(second=20).timestamp(),
		minute_timestamp=minute_ts,
		existing_observation_id=None,
		version_group="grp-b", version_number=0, version_siblings=[2],
	)
	photo_c = inat2exif.LocalPhoto(
		id=3, path=tmp_path / "c.JPG",
		exif_datetime_raw="2024:04:27 15:01:30",
		timestamp=obs_time.replace(second=30).timestamp(),
		minute_timestamp=minute_ts,
		existing_observation_id=None,
		version_group="grp-c", version_number=0, version_siblings=[3],
	)

	def fake_fetch_observations(*_args, **_kwargs):
		return [
			{
				"id": 300003,
				"time_observed_at": "2024-04-27T15:01:00Z",
				"photos": [
					{"id": 2001, "url": "https://example.invalid/obs1.jpg"},
					{"id": 2002, "url": "https://example.invalid/obs2.jpg"},
				],
				"uri": "https://www.inaturalist.org/observations/300003",
			}
		]

	def fake_index_local_photos(*_args, **_kwargs):
		by_second: dict[float, list[inat2exif.LocalPhoto]] = {}
		by_minute = {minute_ts: [photo_a, photo_b, photo_c]}
		by_id = {1: photo_a, 2: photo_b, 3: photo_c}
		return [photo_a, photo_b, photo_c], by_second, by_minute, by_id

	def fake_cache_observation_photo(photo: dict, _cache_dir: Path) -> Path:
		return tmp_path / str(photo.get("id", "x"))

	def fake_score_candidates(_obs_paths: list[Path], _candidates: list[inat2exif.LocalPhoto], **_kwargs) -> list[tuple[int, float]]:
		# All three above threshold, but 3 groups != 2 obs photos — N-to-N must not trigger.
		return [(1, 0.85), (2, 0.83), (3, 0.81)]

	monkeypatch.setattr(inat2exif, "fetch_observations", fake_fetch_observations)
	monkeypatch.setattr(inat2exif, "index_local_photos", fake_index_local_photos)
	monkeypatch.setattr(inat2exif, "cache_observation_photo", fake_cache_observation_photo)
	monkeypatch.setattr(inat2exif, "score_candidates", fake_score_candidates)
	monkeypatch.setattr(inat2exif, "score_candidates_per_photo", lambda paths, cands, **_kw: [fake_score_candidates(paths, cands)])

	state = inat2exif.MatcherState()
	state.group_versions = True
	state.analyze("tester", tmp_path)

	match = state.matches_by_obs_id[300003]
	assert match.status == "ambiguous"
	assert match.selected_photo_ids == set()


def test_write_observation_metadata_writes_dwc_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	written_payloads: list[dict[str, str]] = []

	class FakeImage:
		def __init__(self, _path: str):
			self.closed = False

		def modify_xmp(self, payload: dict[str, str]) -> None:
			written_payloads.append(dict(payload))

		def close(self) -> None:
			self.closed = True

	fake_pyexiv2 = types.SimpleNamespace(Image=FakeImage)
	monkeypatch.setattr(inat2exif, "pyexiv2", fake_pyexiv2)

	obs = inat2exif.ObservationData(
		obs_id=123,
		observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC),
		photos=[],
		uri="https://www.inaturalist.org/observations/123",
		metadata_xmp={"Xmp.dwc.occurrenceID": "https://www.inaturalist.org/observations/123"},
	)

	inat2exif.write_observation_metadata(tmp_path / "x.jpg", obs)

	assert len(written_payloads) == 1
	assert written_payloads[0]["Xmp.dwc.occurrenceID"] == "https://www.inaturalist.org/observations/123"


def test_write_observation_metadata_renders_title_and_caption_templates(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	written_payloads: list[dict[str, str]] = []
	config_path = tmp_path / "config.json"
	config_path.write_text(
		json.dumps(
			{
				"image_title_template": "Animal: {{name}} Faved {{faves_count}} time(s)",
				"image_caption_template": "Observation {{obs_id}} by {{user.login}}",
			}
		),
		encoding="utf-8",
	)
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)

	class FakeImage:
		def __init__(self, _path: str):
			self.closed = False

		def modify_xmp(self, payload: dict[str, str]) -> None:
			written_payloads.append(dict(payload))

		def close(self) -> None:
			self.closed = True

	fake_pyexiv2 = types.SimpleNamespace(Image=FakeImage)
	monkeypatch.setattr(inat2exif, "pyexiv2", fake_pyexiv2)

	obs = inat2exif.ObservationData(
		obs_id=123,
		observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC),
		photos=[],
		uri="https://www.inaturalist.org/observations/123",
		metadata_xmp={"Xmp.dwc.occurrenceID": "https://www.inaturalist.org/observations/123"},
		template_context={
			"name": "Cicindela campestris",
			"faves_count": 3,
			"obs_id": 123,
			"user": {"login": "barnabywalters"},
		},
	)

	inat2exif.write_observation_metadata(tmp_path / "x.jpg", obs)

	assert len(written_payloads) == 1
	assert written_payloads[0]["Xmp.dc.title"] == "Animal: Cicindela campestris Faved 3 time(s)"
	assert written_payloads[0]["Xmp.dc.description"] == "Observation 123 by barnabywalters"


def test_write_observation_metadata_preserves_hierarchical_tags_when_disabled(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	written_payloads: list[dict[str, object]] = []
	config_path = tmp_path / "config.json"
	config_path.write_text(json.dumps({"write_hierarchical_tags": False}), encoding="utf-8")
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)

	class FakeImage:
		def __init__(self, _path: str):
			self.closed = False

		def read_xmp(self):
			return {
				"Xmp.lr.hierarchicalSubject": ["custom|tag", "taxonomy|Animalia|Insecta"],
				"Xmp.dc.subject": ["custom", "taxonomy/Animalia/Insecta"],
				"Xmp.digiKam.TagsList": ["custom", "taxonomy/Animalia/Insecta"],
			}

		def modify_xmp(self, payload: dict[str, object]) -> None:
			written_payloads.append(dict(payload))

		def close(self) -> None:
			self.closed = True

	fake_pyexiv2 = types.SimpleNamespace(Image=FakeImage)
	monkeypatch.setattr(inat2exif, "pyexiv2", fake_pyexiv2)

	obs = inat2exif.ObservationData(
		obs_id=123,
		observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC),
		photos=[],
		uri="https://www.inaturalist.org/observations/123",
		metadata_xmp={
			"Xmp.dwc.occurrenceID": "https://www.inaturalist.org/observations/123",
			"Xmp.lr.hierarchicalSubject": ["taxonomy|Animalia|Insecta"],
			"Xmp.dc.subject": ["taxonomy/Animalia/Insecta"],
			"Xmp.digiKam.TagsList": ["taxonomy/Animalia/Insecta"],
		},
	)

	inat2exif.write_observation_metadata(tmp_path / "x.jpg", obs)

	assert len(written_payloads) == 1
	assert written_payloads[0]["Xmp.dwc.occurrenceID"] == "https://www.inaturalist.org/observations/123"
	assert not any(key in written_payloads[0] for key in inat2exif.TAG_XMP_KEYS)


def test_write_observation_metadata_removes_bare_taxonomy_root_on_merge(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	"""Regression: photos with old-format bare 'taxonomy' entries (without a slash)
	in Xmp.dc.subject / Xmp.digiKam.TagsList must not accumulate them after each
	save.  Previously _merge_subject_tags only stripped 'taxonomy/...' paths but
	left bare 'taxonomy' strings intact, so the duplicate entries persisted and
	metadata_refresh_needed kept returning True forever.
	"""
	written_payloads: list[dict[str, object]] = []

	class FakeImage:
		def __init__(self, _path: str):
			pass

		def read_xmp(self):
			# Simulate old-format tags as written by naturtag / earlier inat2exif:
			# bare "taxonomy" repeated several times, plus full slash-delimited paths.
			return {
				"Xmp.dc.subject": [
					"wildlife",
					"hamster",
					"taxonomy",
					"taxonomy",
					"taxonomy",
					"taxonomy",
					"taxonomy",
					"taxonomy/Animalia/Chordata/Vertebrata/Mammalia/Cricetidae/Cricetus/Cricetus cricetus",
					"taxonomy/Animalia/Chordata/Vertebrata/Mammalia/Cricetidae/Cricetus",
					"taxonomy/Animalia/Chordata/Vertebrata/Mammalia/Cricetidae",
					"taxonomy/Animalia/Chordata/Vertebrata/Mammalia",
					"taxonomy/Animalia/Chordata/Vertebrata",
					"taxonomy/Animalia/Chordata",
					"taxonomy/Animalia",
					"taxonomy",
				],
				"Xmp.digiKam.TagsList": [
					"wildlife",
					"hamster",
					"taxonomy",
					"taxonomy",
					"taxonomy",
					"taxonomy",
					"taxonomy",
					"taxonomy/Animalia/Chordata/Vertebrata/Mammalia/Cricetidae/Cricetus/Cricetus cricetus",
					"taxonomy/Animalia/Chordata/Vertebrata/Mammalia",
					"taxonomy/Animalia",
					"taxonomy",
				],
				"Xmp.lr.hierarchicalSubject": [
					"wildlife",
					"hamster",
					"taxonomy",
					"taxonomy|Animalia",
					"taxonomy|Animalia|Chordata|Vertebrata|Mammalia|Cricetidae|Cricetus|Cricetus cricetus",
				],
			}

		def modify_xmp(self, payload: dict[str, object]) -> None:
			written_payloads.append(dict(payload))

		def close(self) -> None:
			pass

	monkeypatch.setattr(inat2exif, "pyexiv2", types.SimpleNamespace(Image=FakeImage))

	new_digikam = [
		"taxonomy/Animalia/Chordata/Vertebrata/Mammalia/Cricetidae/Cricetus/Cricetus cricetus",
		"taxonomy/Animalia/Chordata/Vertebrata/Mammalia/Cricetidae/Cricetus",
		"taxonomy/Animalia/Chordata/Vertebrata/Mammalia/Cricetidae",
		"taxonomy/Animalia/Chordata/Vertebrata/Mammalia",
		"taxonomy/Animalia/Chordata/Vertebrata",
		"taxonomy/Animalia/Chordata",
		"taxonomy/Animalia",
		"taxonomy",
	]
	new_lr = [
		"taxonomy|Animalia|Chordata|Vertebrata|Mammalia|Cricetidae|Cricetus|Cricetus cricetus",
		"taxonomy|Animalia|Chordata|Vertebrata|Mammalia|Cricetidae|Cricetus",
		"taxonomy|Animalia|Chordata|Vertebrata|Mammalia|Cricetidae",
		"taxonomy|Animalia|Chordata|Vertebrata|Mammalia",
		"taxonomy|Animalia|Chordata|Vertebrata",
		"taxonomy|Animalia|Chordata",
		"taxonomy|Animalia",
		"taxonomy",
	]

	obs = inat2exif.ObservationData(
		obs_id=58969891,
		observed_at=dt.datetime(2020, 9, 8, 18, 40, tzinfo=dt.timezone(dt.timedelta(hours=2))),
		photos=[],
		uri="https://www.inaturalist.org/observations/58969891",
		metadata_xmp={
			"Xmp.dwc.catalogNumber": "58969891",
			"Xmp.dwc.datasetName": "iNaturalist observations",
			"Xmp.dc.subject": new_digikam,
			"Xmp.digiKam.TagsList": new_digikam,
			"Xmp.lr.hierarchicalSubject": new_lr,
		},
	)

	inat2exif.write_observation_metadata(tmp_path / "x.jpg", obs)

	assert len(written_payloads) == 1
	result_dc = written_payloads[0]["Xmp.dc.subject"]
	result_digikam = written_payloads[0]["Xmp.digiKam.TagsList"]
	result_lr = written_payloads[0]["Xmp.lr.hierarchicalSubject"]

	# After merging, there must be exactly ONE bare "taxonomy" entry in each
	# subject/taglist field — the root from the new hierarchy.  The 5+ legacy
	# copies must not survive.
	assert isinstance(result_dc, list)
	assert result_dc.count("taxonomy") == 1, (
		f"Expected exactly one bare 'taxonomy' in Xmp.dc.subject after merge, got: {result_dc}"
	)
	assert isinstance(result_digikam, list)
	assert result_digikam.count("taxonomy") == 1, (
		f"Expected exactly one bare 'taxonomy' in Xmp.digiKam.TagsList after merge, got: {result_digikam}"
	)
	# Non-taxonomy user tags must be preserved
	assert "wildlife" in result_dc
	assert "hamster" in result_dc
	assert "wildlife" in result_digikam
	assert "hamster" in result_digikam
	# Lightroom field uses startswith("taxonomy") logic already, but verify no duplicates either
	assert isinstance(result_lr, list)
	assert result_lr.count("taxonomy") == 1, (
		f"Expected exactly one bare 'taxonomy' in Xmp.lr.hierarchicalSubject after merge, got: {result_lr}"
	)


def test_read_existing_observation_id_uses_dwc_dataset_and_catalog_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	class FakeImage:
		def __init__(self, _path: str):
			pass

		def read_xmp(self):
			return {
				"Xmp.dwc.datasetName": "iNaturalist observations",
				"Xmp.dwc.catalogNumber": "210387353",
			}

		def close(self):
			return None

	fake_pyexiv2 = types.SimpleNamespace(Image=FakeImage)
	monkeypatch.setattr(inat2exif, "pyexiv2", fake_pyexiv2)

	obs_id = inat2exif.read_existing_observation_id(tmp_path / "photo.jpg")
	assert obs_id == 210387353


def test_read_existing_observation_id_requires_expected_dwc_dataset_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	class FakeImage:
		def __init__(self, _path: str):
			pass

		def read_xmp(self):
			return {
				"Xmp.dwc.datasetName": "something else",
				"Xmp.dwc.catalogNumber": "210387353",
			}

		def close(self):
			return None

	fake_pyexiv2 = types.SimpleNamespace(Image=FakeImage)
	monkeypatch.setattr(inat2exif, "pyexiv2", fake_pyexiv2)

	obs_id = inat2exif.read_existing_observation_id(tmp_path / "photo.jpg")
	assert obs_id is None


def test_selection_state_returns_full_dataset_selected_and_dirty_ids() -> None:
	state = inat2exif.MatcherState()
	state.ready = True

	obs_a = inat2exif.ObservationData(obs_id=101, observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC), photos=[], uri="u1")
	obs_b = inat2exif.ObservationData(obs_id=202, observed_at=dt.datetime(2024, 4, 28, tzinfo=dt.UTC), photos=[], uri="u2")

	match_a = inat2exif.MatchResult(
		observation=obs_a,
		status="auto-cv",
		candidate_photo_ids=[1, 2, 3],
		scored_candidates=[(1, 0.9)],
		selected_photo_ids={2, 3},
	)
	match_b = inat2exif.MatchResult(
		observation=obs_b,
		status="ambiguous",
		candidate_photo_ids=[4, 5],
		scored_candidates=[],
		selected_photo_ids=set(),
	)

	state.matches = [match_a, match_b]
	state.refresh_dirty_photo_ids = {1, 5, 999}

	payload = state.selection_state()
	rows = {row["obs_id"]: row for row in payload["rows"]}

	assert set(rows.keys()) == {101, 202}
	assert rows[101]["selected_photo_ids"] == [2, 3]
	assert rows[101]["dirty_photo_ids"] == [1]
	assert rows[202]["selected_photo_ids"] == []
	assert rows[202]["dirty_photo_ids"] == [5]


def test_api_selection_state_routes_to_global_state(monkeypatch: pytest.MonkeyPatch) -> None:
	class FakeState:
		def selection_state(self) -> dict:
			return {"rows": [{"obs_id": 7, "selected_photo_ids": [11], "dirty_photo_ids": [11]}]}

	monkeypatch.setattr(inat2exif, "state", FakeState())

	result = inat2exif.api_selection_state()
	assert result == {"rows": [{"obs_id": 7, "selected_photo_ids": [11], "dirty_photo_ids": [11]}]}


def test_save_selected_deduplicates_version_siblings(monkeypatch: pytest.MonkeyPatch) -> None:
	written: list[Path] = []

	def fake_write_observation_metadata(path: Path, _observation: inat2exif.ObservationData) -> None:
		written.append(path)

	monkeypatch.setattr(inat2exif, "write_observation_metadata", fake_write_observation_metadata)

	state = inat2exif.MatcherState()
	state.ready = True
	state.group_versions = True

	obs = inat2exif.ObservationData(obs_id=303, observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC), photos=[], uri="u")
	match = inat2exif.MatchResult(
		observation=obs,
		status="auto-exact",
		candidate_photo_ids=[1, 2],
		scored_candidates=[(1, 1.0), (2, 1.0)],
		selected_photo_ids={1, 2},
	)

	state.matches = [match]
	state.matches_by_obs_id = {303: match}
	state.local_photos_by_id = {
		1: _make_photo(1, "a.JPG", 1.0, 1.0, siblings=[1, 2], version_number=0),
		2: _make_photo(2, "a_v1.JPG", 1.0, 1.0, siblings=[1, 2], version_number=1),
	}

	result = state.save_selected()
	assert result["saved"] == 2
	assert result["planned_count"] == 2
	assert sorted(path.name for path in written) == ["a.JPG", "a_v1.JPG"]


def test_save_preview_matches_exact_files_to_be_written(monkeypatch: pytest.MonkeyPatch) -> None:
	written: list[Path] = []

	def fake_write_observation_metadata(path: Path, _observation: inat2exif.ObservationData) -> None:
		written.append(path)

	monkeypatch.setattr(inat2exif, "write_observation_metadata", fake_write_observation_metadata)

	state = inat2exif.MatcherState()
	state.ready = True
	state.group_versions = True

	obs = inat2exif.ObservationData(obs_id=404, observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC), photos=[], uri="u")
	match = inat2exif.MatchResult(
		observation=obs,
		status="auto-cv",
		candidate_photo_ids=[10, 11, 12],
		scored_candidates=[],
		selected_photo_ids={11},
	)

	photo_10 = _make_photo(10, "dirty.JPG", 1.0, 1.0, siblings=[10], version_number=0)
	photo_10.existing_observation_id = 404
	photo_10.metadata_refresh_needed = True

	state.matches = [match]
	state.matches_by_obs_id = {404: match}
	state.local_photos_by_id = {
		10: photo_10,
		11: _make_photo(11, "sel.JPG", 1.0, 1.0, siblings=[11, 12], version_number=0),
		12: _make_photo(12, "sel_v1.JPG", 1.0, 1.0, siblings=[11, 12], version_number=1),
	}
	state.refresh_dirty_photo_ids = {10}

	preview = state.save_preview()
	assert preview["total"] == 3
	assert preview["refresh_count"] == 1
	assert preview["new_count"] == 2
	assert sorted(Path(item["path"]).name for item in preview["files"]) == ["dirty.JPG", "sel.JPG", "sel_v1.JPG"]

	result = state.save_selected()
	assert result["saved"] == 3
	assert sorted(path.name for path in written) == ["dirty.JPG", "sel.JPG", "sel_v1.JPG"]


def test_compute_metadata_diff_reports_tag_insertions_and_removals() -> None:
	expected = {
		"Xmp.lr.hierarchicalSubject": ["taxonomy|Animalia|Insecta|Diphyus", "taxonomy|Animalia|Insecta", "taxonomy|Animalia", "taxonomy"],
		"Xmp.dc.title": "New title",
		"Xmp.dc.description": "",
		"Xmp.dwc.taxonID": "42",
		"Xmp.dwc.eventDate": "2024-04-27",
	}
	embedded = {
		"Xmp.lr.hierarchicalSubject": ["holiday", "taxonomy|Animalia|Insecta|Ichneumon", "taxonomy|Animalia|Insecta", "taxonomy|Animalia", "taxonomy"],
		"Xmp.dc.title": "Old title",
		"Xmp.dwc.taxonID": "42",
	}

	diff = inat2exif._public_metadata_diff(inat2exif.compute_metadata_diff(embedded, expected))
	by_key = {entry["key"]: entry for entry in diff}

	assert set(by_key) == {"Xmp.lr.hierarchicalSubject", "Xmp.dc.title", "Xmp.dwc.eventDate"}
	assert by_key["Xmp.lr.hierarchicalSubject"] == {
		"key": "Xmp.lr.hierarchicalSubject",
		"kind": "tags",
		"delimiter": "|",
		"added": ["taxonomy|Animalia|Insecta|Diphyus"],
		"removed": ["taxonomy|Animalia|Insecta|Ichneumon"],
	}
	assert by_key["Xmp.dc.title"] == {"key": "Xmp.dc.title", "kind": "value", "old": "Old title", "new": "New title"}
	assert by_key["Xmp.dwc.eventDate"] == {"key": "Xmp.dwc.eventDate", "kind": "value", "old": None, "new": "2024-04-27"}


def test_compute_removal_diff_requires_dataset_marker() -> None:
	embedded = {
		"Xmp.dwc.taxonID": "42",
		"Xmp.dc.subject": ["holiday", "taxonomy/Animalia"],
		"Xmp.exif.Unrelated": "keep",
	}
	assert inat2exif.compute_removal_diff(embedded) is None

	embedded["Xmp.dwc.datasetName"] = inat2exif.XMP_DWC_EXPECTED_DATASET_NAME
	diff = inat2exif.compute_removal_diff(embedded)
	by_key = {entry["key"]: entry for entry in diff}
	assert set(by_key) == {"Xmp.dwc.taxonID", "Xmp.dc.subject", "Xmp.dwc.datasetName"}
	assert by_key["Xmp.dc.subject"]["removed"] == ["taxonomy/Animalia"]
	assert by_key["Xmp.dwc.taxonID"] == {"key": "Xmp.dwc.taxonID", "kind": "value", "old": "42", "new": None}


def test_pending_changes_groups_identical_diffs_per_observation(monkeypatch: pytest.MonkeyPatch) -> None:
	expected = {"Xmp.dc.title": "Wasp", "Xmp.dwc.taxonID": "42"}
	monkeypatch.setattr(inat2exif, "_effective_metadata_xmp", lambda _obs, _config=None: dict(expected))
	embedded_by_name = {
		"a.JPG": {},
		"a_v1.JPG": {},
		"b.JPG": {"Xmp.dc.title": "Wasp"},
	}
	monkeypatch.setattr(inat2exif, "read_embedded_xmp", lambda path: embedded_by_name[Path(path).name])

	state = inat2exif.MatcherState()
	state.ready = True
	state.group_versions = True
	obs = inat2exif.ObservationData(
		obs_id=505,
		observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC),
		photos=[],
		uri="https://www.inaturalist.org/observations/505",
		template_context={"name": "Diphyus", "common_name": "Ichneumon wasp"},
	)
	match = inat2exif.MatchResult(
		observation=obs,
		status="auto-cv",
		candidate_photo_ids=[1, 2, 3],
		scored_candidates=[],
		selected_photo_ids={1, 3},
	)
	state.matches = [match]
	state.matches_by_obs_id = {505: match}
	state.local_photos_by_id = {
		1: _make_photo(1, "a.JPG", 1.0, 1.0, siblings=[1, 2], version_number=0),
		2: _make_photo(2, "a_v1.JPG", 1.0, 1.0, siblings=[1, 2], version_number=1),
		3: _make_photo(3, "b.JPG", 1.0, 1.0, siblings=[3], version_group="other"),
	}

	result = state.pending_changes()
	assert result["total"] == 3
	assert result["new_count"] == 3
	[obs_entry] = result["observations"]
	assert obs_entry["name"] == "Diphyus"
	assert obs_entry["common_name"] == "Ichneumon wasp"
	assert (obs_entry["year"], obs_entry["month"], obs_entry["day"]) == (2024, 4, 27)
	groups = sorted(obs_entry["groups"], key=lambda group: len(group["files"]))
	assert [Path(file["path"]).name for file in groups[0]["files"]] == ["b.JPG"]
	assert groups[0]["diff"] == [{"key": "Xmp.dwc.taxonID", "kind": "value", "old": None, "new": "42"}]
	assert sorted(Path(file["path"]).name for file in groups[1]["files"]) == ["a.JPG", "a_v1.JPG"]
	assert {entry["key"] for entry in groups[1]["diff"]} == {"Xmp.dc.title", "Xmp.dwc.taxonID"}


def test_grouped_candidates_include_deduplicated_refresh_diffs(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(inat2exif, "_effective_metadata_xmp", lambda _obs, _config=None: {"Xmp.dc.title": "Wasp"})
	state = inat2exif.MatcherState()
	state.ready = True
	obs = inat2exif.ObservationData(obs_id=606, observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC), photos=[], uri="u")
	match = inat2exif.MatchResult(
		observation=obs,
		status="tagged",
		candidate_photo_ids=[1, 2],
		scored_candidates=[],
		selected_photo_ids={1, 2},
	)
	photos = {
		1: _make_photo(1, "a.JPG", 1.0, 1.0, siblings=[1, 2], version_number=0),
		2: _make_photo(2, "a_v1.JPG", 1.0, 1.0, siblings=[1, 2], version_number=1),
	}
	for photo in photos.values():
		photo.existing_observation_id = 606
		photo.embedded_xmp = {"Xmp.dc.title": "Bee"}
	state.matches = [match]
	state.matches_by_obs_id = {606: match}
	state.local_photos_by_id = photos
	state.refresh_dirty_photo_ids = {1, 2}

	[candidate] = state._build_grouped_candidates(match)
	assert candidate["dirty"] is True
	assert candidate["metadata_diffs"] == [{
		"names": ["a.JPG", "a_v1.JPG"],
		"diff": [{"key": "Xmp.dc.title", "kind": "value", "old": "Bee", "new": "Wasp"}],
	}]


def test_save_plan_empty_immediately_after_save(monkeypatch: pytest.MonkeyPatch) -> None:
	"""After saving, save_preview must return 0 without requiring re-analysis."""
	monkeypatch.setattr(inat2exif, "write_observation_metadata", lambda _path, _obs: None)

	state = inat2exif.MatcherState()
	state.ready = True
	state.group_versions = False

	obs = inat2exif.ObservationData(obs_id=55, observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC), photos=[], uri="u")
	photo = _make_photo(1, "photo.JPG", 1.0, 1.0, siblings=[1], version_group="")
	# photo.existing_observation_id is None — untagged
	match = inat2exif.MatchResult(
		observation=obs,
		status="auto-exact",
		candidate_photo_ids=[1],
		scored_candidates=[(1, 1.0)],
		selected_photo_ids={1},
	)

	state.matches = [match]
	state.matches_by_obs_id = {55: match}
	state.local_photos_by_id = {1: photo}

	assert state.save_preview()["total"] == 1, "One untagged photo should be pending before save"

	result = state.save_selected()
	assert result["saved"] == 1
	assert result["failed"] == 0

	assert state.save_preview()["total"] == 0, "Plan must be empty immediately after save"
	# Calling save again should write nothing
	result2 = state.save_selected()
	assert result2["saved"] == 0


def test_save_then_reanalyze_shows_no_pending_saves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	"""End-to-end: analyze → save → re-analyze all show 0 files pending after saving.

	The simulated filesystem starts with one untagged photo.  After the first
	analysis the photo is auto-matched and the save plan contains it.  After
	saving, the plan must be immediately empty (in-memory update).  A second
	analysis pass (re-scan) must also produce an empty plan because the photo
	now carries the correct tag.
	"""
	observed_at = dt.datetime(2024, 4, 27, 10, 45, 23, tzinfo=dt.UTC)
	timestamp = observed_at.timestamp()
	minute_ts = observed_at.replace(second=0).timestamp()

	# Simulated filesystem state: maps filename → tagged obs_id (None = untagged)
	sim_tagged: dict[str, int | None] = {"photo.JPG": None}

	def make_photo_from_sim() -> inat2exif.LocalPhoto:
		p = _make_photo(1, str(tmp_path / "photo.JPG"), timestamp, minute_ts, siblings=[1], version_group="")
		p.existing_observation_id = sim_tagged["photo.JPG"]
		return p

	def fake_fetch_observations(*_args, **_kwargs):
		return [
			{
				"id": 42,
				"time_observed_at": "2024-04-27T10:45:23Z",
				"photos": [{"id": 1001, "url": "https://example.invalid/obs.jpg"}],
				"uri": "https://www.inaturalist.org/observations/42",
			}
		]

	def fake_index_local_photos(*_args, **_kwargs):
		photo = make_photo_from_sim()
		return [photo], {timestamp: [photo]}, {minute_ts: [photo]}, {1: photo}

	def fake_write_observation_metadata(path: Path, observation: inat2exif.ObservationData) -> None:
		sim_tagged[path.name] = observation.obs_id

	monkeypatch.setattr(inat2exif, "fetch_observations", fake_fetch_observations)
	monkeypatch.setattr(inat2exif, "index_local_photos", fake_index_local_photos)
	monkeypatch.setattr(inat2exif, "write_observation_metadata", fake_write_observation_metadata)
	monkeypatch.setattr(inat2exif, "metadata_refresh_needed_with_details", lambda _p, _o, **_kwargs: (False, []))

	state = inat2exif.MatcherState()
	state.group_versions = False

	# --- Pass 1: initial analysis ---
	state.analyze("tester", tmp_path)
	assert state.matches_by_obs_id[42].selected_photo_ids == {1}
	assert state.save_preview()["total"] == 1, "One untagged photo should be pending"

	# --- Save ---
	result = state.save_selected()
	assert result["saved"] == 1
	assert result["failed"] == 0
	assert sim_tagged["photo.JPG"] == 42, "Simulated write should record the obs_id"

	# Immediately after save: plan must be empty (in-memory update)
	assert state.save_preview()["total"] == 0, "In-memory plan must be empty right after save"

	# --- Pass 2: re-analysis (photo now carries the correct tag) ---
	state.analyze("tester", tmp_path)

	assert state.matches_by_obs_id[42].has_existing_tag is True
	assert state.refresh_dirty_photo_ids == set()
	assert state.save_preview()["total"] == 0, "Re-analysis must produce an empty save plan"


def test_save_then_reanalyze_keeps_selection_when_duplicate_obs_share_one_photo(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Regression: selecting one of two duplicate-matching observations must remain stable.

	A legitimate edge case is two observations matching the same local photo.
	When one observation is manually selected and saved, re-analysis must keep that
	existing tagged selection and must not schedule metadata removal.
	"""
	fixture_photo = Path("tests/fixtures/real_cached_case/photos/2024-04-27T150100s534.JPG")
	assert fixture_photo.exists(), "Expected real fixture photo for regression test"

	observed_at = dt.datetime(2024, 4, 27, 15, 1, 0, tzinfo=dt.UTC)
	timestamp = observed_at.timestamp()
	minute_timestamp = observed_at.replace(second=0).timestamp()

	# Simulated persisted metadata tag state for the fixture photo across analyze cycles.
	sim_tagged_obs_id: int | None = None

	def make_photo_from_sim() -> inat2exif.LocalPhoto:
		photo = _make_photo(
			1,
			str(fixture_photo),
			timestamp,
			minute_timestamp,
			siblings=[1],
			version_group="",
		)
		photo.existing_observation_id = sim_tagged_obs_id
		return photo

	def fake_fetch_observations(*_args, **_kwargs):
		# Two observations at the same second, both matching the same single photo.
		return [
			{
				"id": 420001,
				"time_observed_at": "2024-04-27T15:01:00Z",
				"photos": [{"id": 1001, "url": "https://example.invalid/obs-a.jpg"}],
				"uri": "https://www.inaturalist.org/observations/420001",
			},
			{
				"id": 420002,
				"time_observed_at": "2024-04-27T15:01:00Z",
				"photos": [{"id": 1002, "url": "https://example.invalid/obs-b.jpg"}],
				"uri": "https://www.inaturalist.org/observations/420002",
			},
		]

	def fake_index_local_photos(*_args, **_kwargs):
		photo = make_photo_from_sim()
		return [photo], {timestamp: [photo]}, {minute_timestamp: [photo]}, {1: photo}

	def fake_score_candidates(_obs_paths: list[Path], _candidates: list[inat2exif.LocalPhoto], **_kwargs) -> list[tuple[int, float]]:
		return [(1, 1.0)]

	def fake_cache_observation_photo(_photo: dict, _cache_dir: Path) -> Path:
		return fixture_photo

	def fake_write_observation_metadata(_path: Path, observation: inat2exif.ObservationData) -> None:
		nonlocal sim_tagged_obs_id
		sim_tagged_obs_id = observation.obs_id

	monkeypatch.setattr(inat2exif, "fetch_observations", fake_fetch_observations)
	monkeypatch.setattr(inat2exif, "index_local_photos", fake_index_local_photos)
	monkeypatch.setattr(inat2exif, "score_candidates", fake_score_candidates)
	monkeypatch.setattr(inat2exif, "score_candidates_per_photo", lambda paths, cands, **_kw: [fake_score_candidates(paths, cands)])
	monkeypatch.setattr(inat2exif, "cache_observation_photo", fake_cache_observation_photo)
	monkeypatch.setattr(inat2exif, "write_observation_metadata", fake_write_observation_metadata)
	monkeypatch.setattr(inat2exif, "metadata_refresh_needed_with_details", lambda _p, _o, **_kwargs: (False, []))

	state = inat2exif.MatcherState()
	state.group_versions = False

	# Pass 1: both are ambiguous because the same photo can match both observations.
	state.analyze("tester", fixture_photo.parent)
	assert state.matches_by_obs_id[420001].selected_photo_ids == set()
	assert state.matches_by_obs_id[420002].selected_photo_ids == set()

	# Manually assign the shared photo to one observation and save.
	state.update_selection(420002, [1])
	assert state.matches_by_obs_id[420002].selected_photo_ids == {1}
	assert state.save_preview()["total"] == 1

	result = state.save_selected()
	assert result["saved"] == 1
	assert result["failed"] == 0
	assert sim_tagged_obs_id == 420002

	# Pass 2: re-analysis must preserve the tagged observation as selected.
	state.analyze("tester", fixture_photo.parent)

	assert state.matches_by_obs_id[420002].has_existing_tag is True
	assert state.matches_by_obs_id[420002].selected_photo_ids == {1}
	assert state.save_preview()["total"] == 0


def test_remove_observation_metadata_removes_our_fields(monkeypatch: pytest.MonkeyPatch) -> None:
	"""remove_observation_metadata deletes our XMP fields, strips only taxonomy tags, and preserves the rest."""
	modifications: list[dict] = []

	class FakeImage:
		def __init__(self, _path: str) -> None:
			pass

		def read_xmp(self) -> dict:
			return {
				"Xmp.dwc.datasetName": "iNaturalist observations",
				"Xmp.dwc.catalogNumber": "42",
				"Xmp.dwc.occurrenceID": "https://www.inaturalist.org/observations/42",
				"Xmp.dc.subject": ["holiday", "taxonomy/Animalia", "taxonomy"],
				"Xmp.lr.hierarchicalSubject": ["taxonomy|Animalia", "taxonomy"],
				"Xmp.digiKam.TagsList": ["Places/Vienna"],
				"Xmp.xmp.Rating": "5",  # must be preserved
			}

		def modify_xmp(self, data: dict) -> None:
			modifications.append(dict(data))

		def close(self) -> None:
			pass

	monkeypatch.setattr(inat2exif, "pyexiv2", types.SimpleNamespace(Image=FakeImage))

	result = inat2exif.remove_observation_metadata(Path("photo.jpg"))

	assert result is True
	assert modifications == [{
		"Xmp.dwc.datasetName": None,
		"Xmp.dwc.catalogNumber": None,
		"Xmp.dwc.occurrenceID": None,
		"Xmp.dc.subject": ["holiday"],
		"Xmp.lr.hierarchicalSubject": None,
	}]


def test_remove_observation_metadata_skips_without_our_marker(monkeypatch: pytest.MonkeyPatch) -> None:
	"""remove_observation_metadata returns False and does not write when datasetName is not ours."""
	write_called = False

	class FakeImage:
		def __init__(self, _path: str) -> None:
			pass

		def read_xmp(self) -> dict:
			return {
				"Xmp.dwc.datasetName": "some other dataset",
				"Xmp.dwc.catalogNumber": "99",
			}

		def modify_xmp(self, _data: dict) -> None:
			nonlocal write_called
			write_called = True

		def close(self) -> None:
			pass

	monkeypatch.setattr(inat2exif, "pyexiv2", types.SimpleNamespace(Image=FakeImage))

	result = inat2exif.remove_observation_metadata(Path("photo.jpg"))

	assert result is False
	assert not write_called


def test_save_plan_includes_removal_for_deselected_photos() -> None:
	"""_build_save_plan_locked includes reason='remove' for tagged photos not selected for any obs."""
	state = inat2exif.MatcherState()
	state.ready = True
	state.group_versions = False

	obs = inat2exif.ObservationData(obs_id=99, observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC), photos=[], uri="u")
	match = inat2exif.MatchResult(
		observation=obs,
		status="ambiguous",
		candidate_photo_ids=[1],
		scored_candidates=[],
		selected_photo_ids=set(),
	)
	photo = _make_photo(1, "tagged.JPG", 1.0, 1.0, siblings=[1], version_group="")
	photo.existing_observation_id = 99

	state.matches = [match]
	state.matches_by_obs_id = {99: match}
	state.local_photos_by_id = {1: photo}

	plan = state._build_save_plan_locked()

	assert len(plan) == 1
	assert plan[0]["reason"] == "remove"
	assert plan[0]["photo_id"] == 1
	assert plan[0]["obs_id"] == 99


def test_save_selected_removes_metadata_for_deselected_photos(monkeypatch: pytest.MonkeyPatch) -> None:
	"""save_selected calls remove_observation_metadata and clears existing_observation_id."""
	removed_paths: list[Path] = []

	def fake_remove(path: Path) -> bool:
		removed_paths.append(path)
		return True

	monkeypatch.setattr(inat2exif, "remove_observation_metadata", fake_remove)

	state = inat2exif.MatcherState()
	state.ready = True
	state.group_versions = False

	obs = inat2exif.ObservationData(obs_id=77, observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC), photos=[], uri="u")
	match = inat2exif.MatchResult(
		observation=obs,
		status="ambiguous",
		candidate_photo_ids=[1],
		scored_candidates=[],
		selected_photo_ids=set(),
	)
	photo = _make_photo(1, "deselected.JPG", 1.0, 1.0, siblings=[1], version_group="")
	photo.existing_observation_id = 77

	state.matches = [match]
	state.matches_by_obs_id = {77: match}
	state.local_photos_by_id = {1: photo}

	preview = state.save_preview()
	assert preview["total"] == 1
	assert preview["remove_count"] == 1

	result = state.save_selected()
	assert result["saved"] == 1
	assert result["remove_count"] == 1
	assert result["failed"] == 0
	assert removed_paths == [Path("deselected.JPG")]
	assert photo.existing_observation_id is None

	assert state.save_preview()["total"] == 0


# Tests for hierarchical tags functionality
def test_build_hierarchical_tags_with_ancestors_in_taxon() -> None:
	"""Test building hierarchical tags when ancestors are in taxon."""
	obs = {
		"taxon": {
			"name": "Corythucha",
			"ancestor_ids": [48460, 1, 47120, 372739, 47158],
			"ancestors": [
				{"id": 1, "name": "Animalia"},
				{"id": 47120, "name": "Arthropoda"},
				{"id": 372739, "name": "Hexapoda"},
				{"id": 47158, "name": "Insecta"},
			],
		}
	}
	lightroom, digikam = inat2exif.build_hierarchical_tags(obs)
	
	assert isinstance(lightroom, list)
	assert isinstance(digikam, list)
	# Most specific first
	assert lightroom[0] == "taxonomy|Animalia|Arthropoda|Hexapoda|Insecta|Corythucha"
	assert digikam[0] == "taxonomy/Animalia/Arthropoda/Hexapoda/Insecta/Corythucha"
	# All intermediate levels present
	assert "taxonomy|Animalia|Arthropoda|Hexapoda|Insecta" in lightroom
	assert "taxonomy|Animalia|Arthropoda|Hexapoda" in lightroom
	assert "taxonomy|Animalia|Arthropoda" in lightroom
	assert "taxonomy|Animalia" in lightroom
	assert "taxonomy/Animalia/Arthropoda/Hexapoda/Insecta" in digikam
	assert "taxonomy/Animalia/Arthropoda/Hexapoda" in digikam
	assert "taxonomy/Animalia/Arthropoda" in digikam
	assert "taxonomy/Animalia" in digikam
	# "taxonomy" alone should also be included
	assert "taxonomy" in lightroom
	assert "taxonomy" in digikam


def test_build_hierarchical_tags_falls_back_to_identifications() -> None:
	"""Test building hierarchical tags falls back to identifications when taxon lacks ancestors."""
	obs = {
		"taxon": {
			"name": "Cicindela",
			"ancestor_ids": [48460, 1, 47120],
			# No ancestors field in taxon
		},
		"identifications": [
			{
				"taxon": {
					"ancestors": [
						{"id": 1, "name": "Animalia"},
						{"id": 47120, "name": "Arthropoda"},
					],
				}
			}
		],
	}
	lightroom, digikam = inat2exif.build_hierarchical_tags(obs)
	
	assert isinstance(lightroom, list)
	assert isinstance(digikam, list)
	assert lightroom[0] == "taxonomy|Animalia|Arthropoda|Cicindela"
	assert digikam[0] == "taxonomy/Animalia/Arthropoda/Cicindela"
	assert "taxonomy|Animalia|Arthropoda" in lightroom
	assert "taxonomy|Animalia" in lightroom
	assert "taxonomy/Animalia/Arthropoda" in digikam
	assert "taxonomy/Animalia" in digikam


def test_build_hierarchical_tags_returns_none_without_ancestors() -> None:
	"""Test building hierarchical tags returns None when no ancestors available."""
	obs = {
		"taxon": {
			"ancestor_ids": [48460, 1, 47120],
			# No ancestors
		}
	}
	lightroom, digikam = inat2exif.build_hierarchical_tags(obs)
	
	assert lightroom is None
	assert digikam is None


def test_build_hierarchical_tags_ignores_life_taxon() -> None:
	"""Test that taxon ID 48460 (life) is replaced with 'taxonomy'."""
	obs = {
		"taxon": {
			"ancestor_ids": [48460, 1, 47120],
			"ancestors": [
				{"id": 1, "name": "Animalia"},
				{"id": 47120, "name": "Arthropoda"},
			],
		}
	}
	lightroom, digikam = inat2exif.build_hierarchical_tags(obs)
	
	# Should start with "taxonomy", not the ID 48460
	assert isinstance(lightroom, list)
	assert all(tag.startswith("taxonomy") for tag in lightroom)
	assert all(tag.startswith("taxonomy") for tag in digikam)
	assert all("48460" not in tag for tag in lightroom)
	assert all("48460" not in tag for tag in digikam)


def test_build_hierarchical_tags_does_not_duplicate_leaf_when_already_last() -> None:
	"""If the leaf name already matches the final ancestor segment, don't duplicate it."""
	obs = {
		"taxon": {
			"name": "Insecta",
			"ancestor_ids": [48460, 1, 47120, 47158],
			"ancestors": [
				{"id": 1, "name": "Animalia"},
				{"id": 47120, "name": "Arthropoda"},
				{"id": 47158, "name": "Insecta"},
			],
		},
	}
	lightroom, digikam = inat2exif.build_hierarchical_tags(obs)

	assert isinstance(lightroom, list)
	assert lightroom[0] == "taxonomy|Animalia|Arthropoda|Insecta"
	assert digikam[0] == "taxonomy/Animalia/Arthropoda/Insecta"
	# No duplicate "Insecta" at the end
	assert lightroom[0].count("Insecta") == 1
	assert digikam[0].count("Insecta") == 1


def test_merge_hierarchical_tags_with_string_existing() -> None:
	"""Test merging hierarchical tags when existing is a string."""
	existing = "Bird, Mammal"
	new = ["taxonomy|Animalia|Insecta", "taxonomy|Animalia"]
	result = inat2exif._merge_hierarchical_tags(existing, new)
	
	assert isinstance(result, list)
	assert "Bird" in result
	assert "Mammal" in result
	assert "taxonomy|Animalia|Insecta" in result
	assert "taxonomy|Animalia" in result


def test_merge_hierarchical_tags_with_list_existing() -> None:
	"""Test merging hierarchical tags when existing is a list (as pyexiv2 provides)."""
	existing = ["food", "ersatzkekse"]
	new = ["taxonomy|Animalia|Insecta", "taxonomy|Animalia"]
	result = inat2exif._merge_hierarchical_tags(existing, new)
	
	assert isinstance(result, list)
	assert "food" in result
	assert "ersatzkekse" in result
	assert "taxonomy|Animalia|Insecta" in result
	assert "taxonomy|Animalia" in result


def test_merge_hierarchical_tags_replaces_old_taxonomy() -> None:
	"""Test that old taxonomy tags are replaced, not duplicated."""
	existing = ["food", "taxonomy|old|wrong", "taxonomy|old", "ersatzkekse"]
	new = ["taxonomy|Animalia|Insecta", "taxonomy|Animalia"]
	result = inat2exif._merge_hierarchical_tags(existing, new)
	
	assert isinstance(result, list)
	assert "food" in result
	assert "ersatzkekse" in result
	assert "taxonomy|Animalia|Insecta" in result
	assert "taxonomy|Animalia" in result
	assert "taxonomy|old|wrong" not in result
	assert "taxonomy|old" not in result


def test_merge_hierarchical_tags_no_existing() -> None:
	"""Test merging when no existing tags."""
	result = inat2exif._merge_hierarchical_tags(None, ["taxonomy|Animalia|Insecta", "taxonomy|Animalia"])
	
	assert result == ["taxonomy|Animalia|Insecta", "taxonomy|Animalia"]


def test_merge_subject_tags_with_string_existing() -> None:
	"""Test merging subject tags when existing is a string."""
	existing = "bird, insect"
	new = ["taxonomy/Animalia/Insecta", "taxonomy/Animalia"]
	result = inat2exif._merge_subject_tags(existing, new)
	
	assert isinstance(result, list)
	assert "bird" in result
	assert "insect" in result
	assert "taxonomy/Animalia/Insecta" in result
	assert "taxonomy/Animalia" in result


def test_merge_subject_tags_with_list_existing() -> None:
	"""Test merging subject tags when existing is a list."""
	existing = ["bird", "insect", "spider"]
	new = ["taxonomy/Animalia/Insecta", "taxonomy/Animalia"]
	result = inat2exif._merge_subject_tags(existing, new)
	
	assert isinstance(result, list)
	assert "bird" in result
	assert "insect" in result
	assert "spider" in result
	assert "taxonomy/Animalia/Insecta" in result
	assert "taxonomy/Animalia" in result


def test_merge_subject_tags_replaces_old_taxonomy() -> None:
	"""Test that old taxonomy subject tags are replaced."""
	existing = ["bird", "taxonomy/old/wrong", "taxonomy/old", "insect"]
	new = ["taxonomy/Animalia/Insecta", "taxonomy/Animalia"]
	result = inat2exif._merge_subject_tags(existing, new)
	
	assert isinstance(result, list)
	assert "bird" in result
	assert "insect" in result
	assert "taxonomy/Animalia/Insecta" in result
	assert "taxonomy/Animalia" in result
	assert "taxonomy/old/wrong" not in result
	assert "taxonomy/old" not in result


def test_build_observation_metadata_xmp_includes_hierarchical_tags() -> None:
	"""Test that hierarchical tags are included in XMP metadata."""
	raw_obs = {
		"id": 345165313,
		"uri": "https://www.inaturalist.org/observations/345165313",
		"place_guess": "Austria",
		"location": "48.1,16.3",
		"captive": False,
		"taxon": {
			"id": 211584,
			"name": "Scolopostethus",
			"rank": "genus",
			"ancestor_ids": [48460, 1, 47120, 372739, 47158],
			"ancestors": [
				{"id": 1, "name": "Animalia"},
				{"id": 47120, "name": "Arthropoda"},
				{"id": 372739, "name": "Hexapoda"},
				{"id": 47158, "name": "Insecta"},
			],
		},
		"user": {
			"login": "testuser",
			"name": "Test User",
		},
	}
	observed = dt.datetime(2026, 3, 25, 11, 33, tzinfo=dt.UTC)
	fields = inat2exif.build_observation_metadata_xmp(raw_obs, 345165313, raw_obs["uri"], observed)
	
	# Check hierarchical tags are present
	assert "Xmp.lr.hierarchicalSubject" in fields
	assert "Xmp.dc.subject" in fields
	assert "Xmp.digiKam.TagsList" in fields
	
	# Check content — all three fields should be lists of all hierarchy levels
	lr = fields["Xmp.lr.hierarchicalSubject"]
	dc = fields["Xmp.dc.subject"]
	dk = fields["Xmp.digiKam.TagsList"]
	assert isinstance(lr, list)
	assert isinstance(dc, list)
	assert isinstance(dk, list)
	# Most specific (leaf) tag first
	assert lr[0] == "taxonomy|Animalia|Arthropoda|Hexapoda|Insecta|Scolopostethus"
	assert dc[0] == "taxonomy/Animalia/Arthropoda/Hexapoda/Insecta/Scolopostethus"
	assert dk[0] == "taxonomy/Animalia/Arthropoda/Hexapoda/Insecta/Scolopostethus"
	# Intermediate levels present
	assert "taxonomy|Animalia|Arthropoda|Hexapoda|Insecta" in lr
	assert "taxonomy|Animalia|Arthropoda" in lr
	assert "taxonomy|Animalia" in lr
	assert "taxonomy/Animalia/Arthropoda/Hexapoda/Insecta" in dc
	assert "taxonomy/Animalia/Arthropoda" in dc
	assert "taxonomy/Animalia" in dc


# ── Config API tests ──────────────────────────────────────────────────────────

def test_load_config_returns_defaults_when_file_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", tmp_path / "config.json")
	config = inat2exif.load_config()
	assert isinstance(config, inat2exif.AppConfig)
	assert config.ignore_hidden_files is True
	assert config.write_hierarchical_tags is True
	assert config.image_title_template == ""
	assert config.image_caption_template == ""


def test_load_config_reads_existing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	config_path = tmp_path / "config.json"
	config_path.write_text(json.dumps({"custom_key": "hello"}), encoding="utf-8")
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)

	config = inat2exif.load_config()
	assert config.model_extra["custom_key"] == "hello"


def test_load_config_returns_defaults_on_invalid_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	config_path = tmp_path / "config.json"
	config_path.write_text("not valid json {{", encoding="utf-8")
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)

	config = inat2exif.load_config()
	assert isinstance(config, inat2exif.AppConfig)


def test_save_config_writes_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	config_path = tmp_path / "config.json"
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)

	config = inat2exif.AppConfig.model_validate({"preference": "value"})
	inat2exif.save_config(config)

	assert config_path.exists()
	saved = json.loads(config_path.read_text(encoding="utf-8"))
	assert saved["preference"] == "value"


def test_save_and_load_config_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	config_path = tmp_path / "config.json"
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)

	original = inat2exif.AppConfig.model_validate({"template": "my template", "count": 3})
	inat2exif.save_config(original)
	loaded = inat2exif.load_config()

	assert loaded.model_extra["template"] == "my template"
	assert loaded.model_extra["count"] == 3


def test_api_get_config_returns_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", tmp_path / "config.json")
	client = TestClient(inat2exif.app)

	response = client.get("/api/config")
	assert response.status_code == 200
	assert isinstance(response.json(), dict)


def test_api_put_config_saves_and_returns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	config_path = tmp_path / "config.json"
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)
	client = TestClient(inat2exif.app)

	response = client.put(
		"/api/config",
		json={
			"template": "hello",
			"ignore_hidden_files": False,
			"write_hierarchical_tags": False,
			"image_title_template": "{{name}}",
			"image_caption_template": "{{obs_id}}",
		},
	)
	assert response.status_code == 200
	assert response.json()["template"] == "hello"
	assert response.json()["ignore_hidden_files"] is False
	assert response.json()["write_hierarchical_tags"] is False
	assert response.json()["image_title_template"] == "{{name}}"
	assert response.json()["image_caption_template"] == "{{obs_id}}"
	assert config_path.exists()


def test_api_get_config_reflects_latest_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	"""GET /api/config must read from disk, not a stale in-process cache."""
	config_path = tmp_path / "config.json"
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)
	client = TestClient(inat2exif.app)

	# Write a config via the PUT endpoint
	client.put("/api/config", json={"foo": "bar"})

	# Overwrite the file directly to simulate an external modification
	config_path.write_text(json.dumps({"foo": "updated"}), encoding="utf-8")

	response = client.get("/api/config")
	assert response.json()["foo"] == "updated"


def test_api_template_fields_uses_observation_schema(monkeypatch: pytest.MonkeyPatch) -> None:
	class FakeResponse:
		def raise_for_status(self) -> None:
			return None

		def json(self) -> dict[str, object]:
			return {
				"components": {
					"schemas": {
						"Observation": {
							"type": "object",
							"properties": {
								"id": {"type": "integer", "description": "Observation identifier"},
								"uri": {"type": "string", "format": "uri"},
								"taxon": {"$ref": "#/components/schemas/Taxon"},
								"user": {"$ref": "#/components/schemas/User"},
								"photos": {
									"type": "array",
									"items": {"$ref": "#/components/schemas/Photo"},
								},
							},
						},
						"Taxon": {
							"type": "object",
							"properties": {
								"name": {"type": "string", "description": "Scientific name"},
								"preferred_common_name": {"type": "string", "nullable": True},
							},
						},
						"User": {
							"type": "object",
							"properties": {
								"login": {"type": "string"},
								"name": {"type": "string", "nullable": True},
							},
						},
						"Photo": {
							"type": "object",
							"properties": {
								"url": {"type": "string", "format": "uri"},
								"license_code": {"type": "string", "enum": ["cc-by", "cc0"]},
							},
						},
					}
				}
			}

	def fake_get(url: str, timeout: int) -> FakeResponse:
		assert url == inat2exif.INAT_API_DOCS_URL
		assert timeout == inat2exif.INAT_API_DOCS_TIMEOUT_S
		return FakeResponse()

	monkeypatch.setattr(inat2exif.requests, "get", fake_get)
	monkeypatch.setattr(inat2exif, "load_sample_observation", lambda: {"id": 321, "uri": "https://example.invalid/observations/321"})
	monkeypatch.setattr(inat2exif, "build_sample_template_context", lambda: {"uri": "https://example.invalid/observations/321"})
	inat2exif._api_docs_cache["document"] = None
	inat2exif._api_docs_cache["fetched_at"] = 0.0

	client = TestClient(inat2exif.app)
	response = client.get("/api/template-fields")

	assert response.status_code == 200
	payload = response.json()
	assert payload["source"] == "schema"

	fields = {entry["path"]: entry for entry in payload["fields"]}
	assert fields["id"]["kind"] == "integer"
	assert fields["taxon.name"]["description"] == "Scientific name"
	assert fields["taxon.preferred_common_name"]["nullable"] is True
	assert fields["user.login"]["kind"] == "string"
	assert fields["photos"]["kind"] == "array"
	assert fields["photos[].url"]["format"] == "uri"
	assert fields["photos[].license_code"]["enum"] == ["cc-by", "cc0"]
	assert fields["name"]["source"] == "alias"
	assert fields["username"]["source"] == "alias"
	assert fields["obs_id"]["kind"] == "integer"


def test_load_config_applies_defaults_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	defaults_path = tmp_path / "config_defaults.json"
	defaults_path.write_text(
		json.dumps(
			{
				"username": "alice",
				"path": "/photos/default",
				"image_title_template": "{{name}}",
			}
		),
		encoding="utf-8",
	)
	config_path = tmp_path / "config.json"
	config_path.write_text(json.dumps({"username": "bob"}), encoding="utf-8")

	monkeypatch.setattr(inat2exif, "DEFAULT_CONFIG_PATH", defaults_path)
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)

	loaded = inat2exif.load_config()
	assert loaded.username == "bob"
	assert loaded.path == "/photos/default"
	assert loaded.image_title_template == "{{name}}"


def test_api_session_start_uses_saved_config_when_payload_missing(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	defaults_path = tmp_path / "config_defaults.json"
	defaults_path.write_text(json.dumps({"username": "", "path": ""}), encoding="utf-8")
	photo_root = tmp_path / "photos"
	photo_root.mkdir()
	config_path = tmp_path / "config.json"
	config_path.write_text(json.dumps({"username": "tester", "path": str(photo_root)}), encoding="utf-8")

	monkeypatch.setattr(inat2exif, "DEFAULT_CONFIG_PATH", defaults_path)
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)

	called: dict[str, object] = {}

	def fake_start_analysis(username: str, root: Path, ignore_hidden_files: bool = True):
		called["username"] = username
		called["root"] = root
		called["ignore_hidden_files"] = ignore_hidden_files
		return {"started": True, "running": False, "summary": None}

	monkeypatch.setattr(inat2exif.state, "start_analysis", fake_start_analysis)
	client = TestClient(inat2exif.app)

	response = client.post("/api/session/start")
	assert response.status_code == 200
	assert called["username"] == "tester"
	assert called["root"] == photo_root.resolve()
	assert called["ignore_hidden_files"] is True


def test_api_session_start_passes_ignore_hidden_files_setting(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	defaults_path = tmp_path / "config_defaults.json"
	defaults_path.write_text(json.dumps({"username": "", "path": "", "ignore_hidden_files": True}), encoding="utf-8")
	photo_root = tmp_path / "photos"
	photo_root.mkdir()
	config_path = tmp_path / "config.json"
	config_path.write_text(
		json.dumps({"username": "tester", "path": str(photo_root), "ignore_hidden_files": False}),
		encoding="utf-8",
	)

	monkeypatch.setattr(inat2exif, "DEFAULT_CONFIG_PATH", defaults_path)
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)

	called: dict[str, object] = {}

	def fake_start_analysis(username: str, root: Path, ignore_hidden_files: bool = True):
		called["username"] = username
		called["root"] = root
		called["ignore_hidden_files"] = ignore_hidden_files
		return {"started": True, "running": False, "summary": None}

	monkeypatch.setattr(inat2exif.state, "start_analysis", fake_start_analysis)
	client = TestClient(inat2exif.app)

	response = client.post("/api/session/start")
	assert response.status_code == 200
	assert called["username"] == "tester"
	assert called["root"] == photo_root.resolve()
	assert called["ignore_hidden_files"] is False


# ── Metadata refresh tests ──────────────────────────────────────────────────

def test_filter_taxonomy_tags_pipes() -> None:
	"""Test filtering taxonomy tags from pipe-delimited string."""
	tags = "custom|taxonomy|Animalia|bird|taxonomy|Animalia|Insecta"
	result = inat2exif._filter_taxonomy_tags(tags, "|")
	assert sorted(result) == sorted(["taxonomy|Animalia", "taxonomy|Animalia|Insecta"])


def test_filter_taxonomy_tags_slashes() -> None:
	"""Test filtering taxonomy tags from slash-delimited string."""
	tags = "custom/tag|taxonomy/Animalia|insect|taxonomy/Animalia/Insecta"
	result = inat2exif._filter_taxonomy_tags(tags, "/")
	assert sorted(result) == sorted(["taxonomy/Animalia", "taxonomy/Animalia/Insecta"])


def test_filter_taxonomy_tags_list() -> None:
	"""Test filtering taxonomy tags from list."""
	tags = ["custom", "taxonomy|Animalia", "bird", "taxonomy|Animalia|Insecta"]
	result = inat2exif._filter_taxonomy_tags(tags, "|")
	assert sorted(result) == sorted(["taxonomy|Animalia", "taxonomy|Animalia|Insecta"])


def test_metadata_refresh_ignores_non_taxonomy_tags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	"""Metadata refresh should ignore pre-existing non-taxonomy tags."""
	photo_path = tmp_path / "test.jpg"
	photo_path.write_bytes(b"fake jpeg")

	def fake_read_embedded_xmp(path: Path):
		return {
			"Xmp.lr.hierarchicalSubject": "custom|tag|taxonomy|Animalia|Insecta",
			"Xmp.dc.subject": ["custom_tag", "taxonomy/Animalia/Insecta"],
			"Xmp.digiKam.TagsList": ["bird", "taxonomy/Animalia", "other"],
		}

	monkeypatch.setattr(inat2exif, "read_embedded_xmp", fake_read_embedded_xmp)

	obs = inat2exif.ObservationData(
		obs_id=123,
		observed_at=dt.datetime(2026, 3, 25, tzinfo=dt.UTC),
		photos=[],
		uri="https://example.com",
		metadata_xmp={
			"Xmp.lr.hierarchicalSubject": ["taxonomy|Animalia|Insecta"],
			"Xmp.dc.subject": ["taxonomy/Animalia/Insecta"],
			"Xmp.digiKam.TagsList": ["taxonomy/Animalia"],
		},
	)

	needed, mismatches = inat2exif.metadata_refresh_needed_with_details(photo_path, obs)
	assert not needed
	assert mismatches == []


def test_metadata_refresh_detects_taxonomy_tag_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	"""Metadata refresh should detect changes in taxonomy tags we manage."""
	photo_path = tmp_path / "test.jpg"
	photo_path.write_bytes(b"fake jpeg")

	def fake_read_embedded_xmp(path: Path):
		return {
			"Xmp.lr.hierarchicalSubject": "custom|tag|taxonomy|Animalia|Arthropoda",
			"Xmp.dc.subject": ["custom", "taxonomy/Animalia/Arthropoda"],
			"Xmp.digiKam.TagsList": ["bird", "taxonomy/Animalia"],
		}

	monkeypatch.setattr(inat2exif, "read_embedded_xmp", fake_read_embedded_xmp)

	obs = inat2exif.ObservationData(
		obs_id=123,
		observed_at=dt.datetime(2026, 3, 25, tzinfo=dt.UTC),
		photos=[],
		uri="https://example.com",
		metadata_xmp={
			"Xmp.lr.hierarchicalSubject": ["taxonomy|Animalia|Insecta"],
			"Xmp.dc.subject": ["taxonomy/Animalia/Insecta"],
			"Xmp.digiKam.TagsList": ["taxonomy/Animalia/Insecta"],
		},
	)

	needed, mismatches = inat2exif.metadata_refresh_needed_with_details(photo_path, obs)
	assert needed
	assert len(mismatches) > 0  # Should detect the taxonomy tag differences


def test_metadata_refresh_round_trip_list_tags_do_not_false_positive(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	"""Round-tripped list tags with custom terms should not be marked dirty.

	This test covers a real pyexiv2-style representation where XMP array fields
	are returned as Python lists. If we flatten those lists to the first item,
	taxonomy tags can disappear when a custom tag comes first.
	"""
	photo_path = tmp_path / "test.jpg"
	photo_path.write_bytes(b"fake jpeg")

	class FakeImage:
		def __init__(self, _path: str) -> None:
			pass

		def read_xmp(self) -> dict:
			return {
				"Xmp.lr.hierarchicalSubject": [
					"custom|tag",
					"taxonomy|Animalia|Insecta",
				],
				"Xmp.dc.subject": [
					"custom_tag",
					"taxonomy/Animalia/Insecta",
				],
				"Xmp.digiKam.TagsList": [
					"bird",
					"taxonomy/Animalia",
				],
			}

		def close(self) -> None:
			return None

	monkeypatch.setattr(inat2exif.pyexiv2, "Image", FakeImage)

	obs = inat2exif.ObservationData(
		obs_id=123,
		observed_at=dt.datetime(2026, 3, 25, tzinfo=dt.UTC),
		photos=[],
		uri="https://example.com",
		metadata_xmp={
			"Xmp.lr.hierarchicalSubject": ["taxonomy|Animalia|Insecta"],
			"Xmp.dc.subject": ["taxonomy/Animalia/Insecta"],
			"Xmp.digiKam.TagsList": ["taxonomy/Animalia"],
		},
	)

	needed, mismatches = inat2exif.metadata_refresh_needed_with_details(photo_path, obs)
	assert not needed
	assert mismatches == []


def test_metadata_refresh_ignores_existing_taxonomy_tags_when_hierarchical_writes_disabled(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	photo_path = tmp_path / "test.jpg"
	photo_path.write_bytes(b"fake jpeg")
	config_path = tmp_path / "config.json"
	config_path.write_text(json.dumps({"write_hierarchical_tags": False}), encoding="utf-8")
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)

	def fake_read_embedded_xmp(path: Path):
		return {
			"Xmp.lr.hierarchicalSubject": "taxonomy|Animalia|Insecta",
			"Xmp.dc.subject": ["taxonomy/Animalia/Insecta"],
			"Xmp.digiKam.TagsList": ["taxonomy/Animalia/Insecta"],
		}

	monkeypatch.setattr(inat2exif, "read_embedded_xmp", fake_read_embedded_xmp)

	obs = inat2exif.ObservationData(
		obs_id=123,
		observed_at=dt.datetime(2026, 3, 25, tzinfo=dt.UTC),
		photos=[],
		uri="https://example.com",
		metadata_xmp={
			"Xmp.lr.hierarchicalSubject": ["taxonomy|Animalia|Insecta"],
			"Xmp.dc.subject": ["taxonomy/Animalia/Insecta"],
			"Xmp.digiKam.TagsList": ["taxonomy/Animalia/Insecta"],
		},
	)

	needed, mismatches = inat2exif.metadata_refresh_needed_with_details(photo_path, obs)
	assert not needed
	assert mismatches == []


def test_metadata_refresh_detects_stale_rendered_title_and_caption(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	photo_path = tmp_path / "test.jpg"
	photo_path.write_bytes(b"fake jpeg")
	config_path = tmp_path / "config.json"
	config_path.write_text(
		json.dumps(
			{
				"image_title_template": "Animal: {{name}} Faved {{faves_count}} time(s)",
				"image_caption_template": "Observation {{obs_id}} by {{user.login}}",
			}
		),
		encoding="utf-8",
	)
	monkeypatch.setattr(inat2exif, "CONFIG_PATH", config_path)

	def fake_read_embedded_xmp(path: Path):
		return {
			"Xmp.dc.title": "Animal: Cicindela campestris Faved 2 time(s)",
			"Xmp.dc.description": "Observation 123 by somebodyelse",
		}

	monkeypatch.setattr(inat2exif, "read_embedded_xmp", fake_read_embedded_xmp)

	obs = inat2exif.ObservationData(
		obs_id=123,
		observed_at=dt.datetime(2026, 3, 25, tzinfo=dt.UTC),
		photos=[],
		uri="https://example.com",
		metadata_xmp={},
		template_context={
			"name": "Cicindela campestris",
			"faves_count": 3,
			"obs_id": 123,
			"user": {"login": "barnabywalters"},
		},
	)

	needed, mismatches = inat2exif.metadata_refresh_needed_with_details(photo_path, obs)
	assert needed
	assert any("Xmp.dc.title" in mismatch for mismatch in mismatches)
	assert any("Xmp.dc.description" in mismatch for mismatch in mismatches)


@pytest.mark.parametrize("latitude,longitude,lat_ref,lon_ref", [
	(41.78763132, -93.79020984, "N", "W"),
	(-41.78763132, 93.79020984, "S", "E"),
	(0, 0, "N", "E"),
	(90, 180, "N", "E"),
	(-90, -180, "S", "W"),
	(12.99999999999, -179.99999999999, "N", "W"),
])
def test_missing_gps_payload_encodes_coordinates(latitude, longitude, lat_ref, lon_ref) -> None:
	from fractions import Fraction

	expected = {"Xmp.dwc.decimalLatitude": str(latitude), "Xmp.dwc.decimalLongitude": str(longitude)}
	payload = inat2exif._missing_gps_payload({}, {}, expected)
	assert payload["Exif.GPSInfo.GPSVersionID"] == "2 3 0 0"
	for name, coordinate, reference in (("Latitude", latitude, lat_ref), ("Longitude", longitude, lon_ref)):
		degrees, minutes, seconds = map(Fraction, payload[f"Exif.GPSInfo.GPS{name}"].split())
		assert 0 <= minutes < 60
		assert 0 <= seconds < 60
		assert float(degrees + minutes / 60 + seconds / 3600) == pytest.approx(abs(coordinate), abs=1e-9)
		assert payload[f"Exif.GPSInfo.GPS{name}Ref"] == reference


@pytest.mark.parametrize("prefix", ["Exif.GPSInfo.", "Xmp.exif."])
@pytest.mark.parametrize("suffix,value", [
	("GPSLatitude", "0/1 0/1 0/1"),
	("GPSLongitude", 0),
	("GPSLatitudeRef", "S"),
	("GPSLongitudeRef", "W"),
])
def test_missing_gps_payload_preserves_even_partial_coordinates(prefix, suffix, value) -> None:
	existing = {prefix + suffix: value}
	xmp, exif = (existing, {}) if prefix == "Xmp.exif." else ({}, existing)
	expected = {"Xmp.dwc.decimalLatitude": "41", "Xmp.dwc.decimalLongitude": "-93"}
	assert inat2exif._missing_gps_payload(xmp, exif, expected) == {}


@pytest.mark.parametrize("latitude,longitude", [
	("nan", "0"), ("0", "inf"), ("91", "0"), ("0", "-181"), ("bad", "0"),
])
def test_missing_gps_payload_rejects_invalid_coordinates(latitude, longitude) -> None:
	with pytest.raises(ValueError, match="Invalid iNaturalist GPS coordinates"):
		inat2exif._missing_gps_payload({}, {}, {
			"Xmp.dwc.decimalLatitude": latitude, "Xmp.dwc.decimalLongitude": longitude,
		})


@pytest.mark.parametrize("expected", [{}, {"Xmp.dwc.decimalLatitude": "41"}, {"Xmp.dwc.decimalLongitude": "-93"}])
def test_missing_gps_payload_requires_coordinate_pair(expected) -> None:
	assert inat2exif._missing_gps_payload({}, {}, expected) == {}


def test_gps_refresh_preview_and_write_agree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	expected = {"Xmp.dwc.decimalLatitude": "41.78763132", "Xmp.dwc.decimalLongitude": "-93.79020984"}
	exif = {"Exif.Photo.DateTimeOriginal": "2024:04:27 10:45:23", "Exif.GPSInfo.GPSVersionID": "2 2 0 0"}
	xmp = dict(expected)
	closed = []

	class FakeImage:
		def __init__(self, path):
			pass

		def read_exif(self):
			return dict(exif)

		def read_xmp(self):
			return dict(xmp)

		def modify_exif(self, payload):
			exif.update(payload)

		def modify_xmp(self, payload):
			xmp.update(payload)

		def close(self):
			closed.append(True)

	monkeypatch.setattr(inat2exif.pyexiv2, "Image", FakeImage)
	monkeypatch.setattr(inat2exif, "_effective_metadata_xmp", lambda *_args: dict(expected))
	photo = _make_photo(1, str(tmp_path / "image.jpg"), 1, 1)
	photo.existing_observation_id = 42
	photo.embedded_xmp = dict(xmp)
	obs = inat2exif.ObservationData(42, dt.datetime(2024, 4, 27, tzinfo=dt.UTC), [], "u")
	needed, mismatches = inat2exif.metadata_refresh_needed_with_details(photo.path, obs, embedded=photo.embedded_xmp)
	assert needed
	assert "MISSING: Exif.GPSInfo.GPSLatitude" in mismatches

	state = inat2exif.MatcherState()
	state.ready = True
	state.cache_dir = tmp_path / "cache"
	match = inat2exif.MatchResult(obs, "tagged", [1], [], selected_photo_ids={1})
	state.matches = [match]
	state.matches_by_obs_id = {42: match}
	state.local_photos_by_id = {1: photo}
	state.refresh_dirty_photo_ids = {1}
	preview = state.pending_changes()
	[entry] = preview["observations"]
	[group] = entry["groups"]
	expected_gps = inat2exif._missing_gps_payload(xmp, exif, expected)
	assert {item["key"]: item["new"] for item in group["diff"]} == expected_gps
	assert next(item for item in group["diff"] if item["key"] == "Exif.GPSInfo.GPSVersionID")["old"] == "2 2 0 0"
	assert state._refresh_diff_for_photo(photo, inat2exif.AppConfig()) == group["diff"]

	result = state.save_selected()
	assert result["saved"] == 1
	assert result["failed"] == 0
	assert exif == {"Exif.Photo.DateTimeOriginal": "2024:04:27 10:45:23", **expected_gps}
	assert inat2exif.metadata_refresh_needed_with_details(photo.path, obs) == (False, [])
	assert closed


@pytest.mark.parametrize("existing_xmp,existing_exif", [
	({"Xmp.exif.GPSLatitude": "41,30N"}, {}),
	({}, {"Exif.GPSInfo.GPSLongitude": "1/1 2/1 3/1"}),
])
def test_write_observation_metadata_preserves_existing_gps(monkeypatch, tmp_path, existing_xmp, existing_exif) -> None:
	expected = {"Xmp.dwc.decimalLatitude": "41", "Xmp.dwc.decimalLongitude": "-93"}
	xmp = dict(existing_xmp)

	class FakeImage:
		def __init__(self, path):
			pass

		def read_exif(self):
			return dict(existing_exif)

		def read_xmp(self):
			return dict(xmp)

		def modify_exif(self, payload):
			pytest.fail("Existing coordinates must not be changed")

		def modify_xmp(self, payload):
			xmp.update(payload)

		def close(self):
			pass

	monkeypatch.setattr(inat2exif.pyexiv2, "Image", FakeImage)
	monkeypatch.setattr(inat2exif, "_effective_metadata_xmp", lambda *_args: dict(expected))
	obs = inat2exif.ObservationData(42, dt.datetime(2024, 4, 27, tzinfo=dt.UTC), [], "u")
	inat2exif.write_observation_metadata(tmp_path / "image.jpg", obs)
	assert xmp == {**existing_xmp, **expected}


def test_gps_read_error_prevents_writing_and_closes_image(monkeypatch, tmp_path) -> None:
	expected = {"Xmp.dwc.decimalLatitude": "41", "Xmp.dwc.decimalLongitude": "-93"}
	closed = []

	class FakeImage:
		def __init__(self, path):
			pass

		def read_xmp(self):
			return {}

		def read_exif(self):
			raise RuntimeError("Cannot read EXIF")

		def modify_xmp(self, payload):
			pytest.fail("Must not write when checking existing GPS fails")

		def close(self):
			closed.append(True)

	monkeypatch.setattr(inat2exif.pyexiv2, "Image", FakeImage)
	monkeypatch.setattr(inat2exif, "_effective_metadata_xmp", lambda *_args: dict(expected))
	obs = inat2exif.ObservationData(42, dt.datetime(2024, 4, 27, tzinfo=dt.UTC), [], "u")
	with pytest.raises(RuntimeError, match="Cannot read EXIF"):
		inat2exif.write_observation_metadata(tmp_path / "image.jpg", obs)
	with pytest.raises(RuntimeError, match="Cannot read EXIF"):
		inat2exif._metadata_diff_for_photo(tmp_path / "image.jpg", {}, expected)
	assert closed == [True, True]


def test_cache_api_reports_sizes_and_clears_individually_or_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	cache_dir = tmp_path / "cache"
	(cache_dir / "observations" / "5").mkdir(parents=True)
	(cache_dir / "observations" / "5" / "thumbnail.jpeg").write_bytes(b"x" * 30)
	(cache_dir / "observations" / "6").mkdir(parents=True)
	(cache_dir / "observations" / "6" / "observation.json").write_bytes(b"{}")
	(cache_dir / "observations.db").write_bytes(b"o" * 100)
	(cache_dir / "local_photos.db").write_bytes(b"l" * 50)
	(cache_dir / "cv_scores.db").write_bytes(b"c" * 20)
	(cache_dir / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
	state = inat2exif.MatcherState()
	state.cache_dir = cache_dir
	monkeypatch.setattr(inat2exif, "state", state)
	client = TestClient(inat2exif.app)

	info = client.get("/api/cache").json()
	sizes = {entry["name"]: entry["size_bytes"] for entry in info["caches"]}
	assert sizes == {"observations": 102, "observation_photos": 30, "local_photos": 50, "cv_scores": 20}
	assert info["total_bytes"] == 202

	response = client.delete("/api/cache/observation_photos")
	assert response.status_code == 200
	assert response.json()["freed_bytes"] == 30
	assert not (cache_dir / "observations" / "5").exists()
	assert (cache_dir / "observations" / "6" / "observation.json").exists()
	assert (cache_dir / "observations.db").exists()

	assert client.delete("/api/cache/bogus").status_code == 404

	state.analysis_status["running"] = True
	assert client.delete("/api/cache/all").status_code == 409
	assert (cache_dir / "local_photos.db").exists()
	state.analysis_status["running"] = False

	response = client.delete("/api/cache/all")
	assert response.status_code == 200
	assert response.json()["total_bytes"] == 0
	assert not (cache_dir / "observations.db").exists()
	assert not (cache_dir / "local_photos.db").exists()
	assert not (cache_dir / "cv_scores.db").exists()
	assert (cache_dir / "CACHEDIR.TAG").exists()


def test_audio_only_observation_has_its_own_visual_state() -> None:
	obs = inat2exif.ObservationData(obs_id=5, observed_at=dt.datetime(2024, 4, 27, tzinfo=dt.UTC), photos=[], uri="u", has_sounds=True)
	match = inat2exif.MatchResult(observation=obs, status="audio-only", candidate_photo_ids=[], scored_candidates=[])
	assert inat2exif.observation_visual_state(match) == "audio-only"
	assert not inat2exif.needs_manual_attention(match)


def _library_search_state(tmp_path: Path) -> inat2exif.MatcherState:
	state = inat2exif.MatcherState()
	state.ready = True
	state.group_versions = False
	state.cache_dir = tmp_path
	when = dt.datetime(2024, 4, 27, 10, 0, tzinfo=dt.UTC)
	lost = inat2exif.ObservationData(obs_id=10, observed_at=when, photos=[{"id": 100, "url": "u"}], uri="u")
	owner = inat2exif.ObservationData(obs_id=20, observed_at=when, photos=[{"id": 200, "url": "u"}], uri="u")
	lost_match = inat2exif.MatchResult(observation=lost, status="no-match", candidate_photo_ids=[], scored_candidates=[])
	owner_match = inat2exif.MatchResult(
		observation=owner, status="auto-exact", candidate_photo_ids=[3], scored_candidates=[(3, 1.0)], selected_photo_ids={3}
	)
	state.matches = [lost_match, owner_match]
	state.matches_by_obs_id = {10: lost_match, 20: owner_match}
	ts = when.timestamp()
	state.local_photos_by_id = {
		pid: _make_photo(pid, str(tmp_path / f"p{pid}.jpg"), ts + offset, ts + offset, version_group=f"g{pid}")
		for pid, offset in ((1, 60.0), (2, 86400.0 * 400), (3, 0.0))
	}
	for photo in state.local_photos_by_id.values():
		photo.path.write_bytes(b"jpeg")
	return state


def test_library_search_streams_ranked_candidates_and_accept_moves_the_photo(
	monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
	scores = {1: 0.05, 2: 0.9, 3: 0.5}
	scored_order: list[int] = []

	def fake_score(_paths, candidates, score_cache=None):
		scored_order.extend(c.id for c in candidates)
		return [(c.id, scores[c.id]) for c in candidates]

	monkeypatch.setattr(inat2exif, "cache_observation_photo", lambda photo, _cache_dir: tmp_path / f"obs{photo['id']}.jpg")
	dimension_reads: list[str] = []

	def fake_dimensions(path):
		dimension_reads.append(Path(path).name)
		return (300, 200)

	monkeypatch.setattr(inat2exif.matcher_logic, "jpeg_dimensions", fake_dimensions)
	monkeypatch.setattr(inat2exif, "score_candidates", fake_score)
	monkeypatch.setattr(inat2exif.matcher_logic, "DEFAULT_CV_MAX_WORKERS", 1)

	state = _library_search_state(tmp_path)
	state.start_library_search(10)
	state.library_search_thread.join(timeout=10)
	status = state.library_search_state()

	assert status["finished"] and not status["running"] and status["error"] is None
	assert status["phase"] == "done"
	# Photos closest in time are compared first.
	assert scored_order == [1, 2]
	# Photo 3 is already selected for obs 20, so it's never compared or shown.
	assert 3 not in scored_order
	assert [c["photo_id"] for c in status["candidates"]] == [2]
	assert status["candidates"][0]["offset"] == "+400d"
	assert sorted(name for name in dimension_reads if name.startswith("p")) == ["p1.jpg", "p2.jpg"]

	# Image sizes are remembered, so a repeat search doesn't re-read local file headers.
	dimension_reads.clear()
	state.start_library_search(10)
	state.library_search_thread.join(timeout=10)
	assert state.library_search_state()["phase"] == "done"
	assert not [name for name in dimension_reads if name.startswith("p")]

	result = state.accept_library_search_candidate(10, 3)

	assert result["released_from"] == [20]
	assert state.matches_by_obs_id[10].selected_photo_ids == {3}
	assert state.matches_by_obs_id[10].status == "manual-search"
	assert state.matches_by_obs_id[20].selected_photo_ids == set()
	assert state.library_search_cancel_event.is_set()


def test_library_search_rejects_photoless_observations(tmp_path: Path) -> None:
	state = _library_search_state(tmp_path)
	state.matches_by_obs_id[10].observation.photos = []
	with pytest.raises(inat2exif.HTTPException) as excinfo:
		state.start_library_search(10)
	assert excinfo.value.status_code == 400
