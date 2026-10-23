import sys
import types
import json
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import pytest


if "cv2" not in sys.modules:
    sys.modules["cv2"] = types.ModuleType("cv2")

if "pyexiv2" not in sys.modules:
    pyexiv2_stub = types.ModuleType("pyexiv2")
    pyexiv2_stub.Image = object
    pyexiv2_stub.xmp = types.SimpleNamespace(register_namespace=lambda *_args, **_kwargs: None)
    sys.modules["pyexiv2"] = pyexiv2_stub

from inat2exif import matcher as matcher_logic


@dataclass(slots=True)
class _LocalPhoto:
    id: int
    exif_datetime_raw: str
    timestamp: float
    minute_timestamp: float
    existing_observation_id: int | None = None
    version_group: str = ""
    version_number: int = 0
    version_siblings: list[int] = field(default_factory=list)
    metadata_refresh_needed: bool = False
    metadata_mismatches: list[str] = field(default_factory=list)
    path: Path = Path("x.jpg")


@dataclass(slots=True)
class _Observation:
    obs_id: int
    observed_at: dt.datetime
    photos: list[dict]
    uri: str
    has_sounds: bool = False


@dataclass(slots=True)
class _MatchResult:
    observation: _Observation
    status: str
    candidate_photo_ids: list[int]
    scored_candidates: list[tuple[int, float]]
    has_existing_tag: bool = False
    selected_photo_ids: set[int] = field(default_factory=set)
    detail: str = ""


def test_matcher_observation_photo_url_transforms_square_to_small() -> None:
    url = matcher_logic.observation_photo_url({"url": "https://x.invalid/photos/123/square.jpeg"}, size="small")
    assert url == "https://x.invalid/photos/123/small.jpeg"


def test_matcher_cache_observation_photo_writes_thumbnail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeResponse:
        content = b"jpeg-bytes"

        def raise_for_status(self) -> None:
            return None

    class FakeRequests:
        @staticmethod
        def get(_url: str, timeout: int):
            assert timeout == 30
            return FakeResponse()

    path = matcher_logic.cache_observation_photo(
        {"id": 500, "observation_id": 321, "url": "https://example.invalid/photos/500/square.jpeg"},
        tmp_path / "cache",
        requests_module=FakeRequests,
    )

    assert path == tmp_path / "cache" / "observations" / "321" / "photo_500.jpeg"
    assert path.read_bytes() == b"jpeg-bytes"


def test_matcher_fetch_observations_reads_new_layout_cache_and_fetches_incremental_updates(tmp_path: Path) -> None:
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

    class Requests:
        @staticmethod
        def get(url: str, timeout: int):
            assert timeout == 30
            requests_seen.append(url)
            return FakeResponse()

    class TimeModule:
        @staticmethod
        def time() -> float:
            return (cache_dir / "123" / "observation.json").stat().st_mtime + 1

        @staticmethod
        def sleep(_seconds: float) -> None:
            return None

    observations = matcher_logic.fetch_observations(
        "tester",
        cache_dir,
        requests_module=Requests,
        time_module=TimeModule,
        cache_ttl_s=60 * 60 * 24,
        per_page=100,
    )

    assert len(requests_seen) == 1
    assert "updated_since=2024-04-27T10%3A50%3A00Z" in requests_seen[0]
    assert len(observations) == 1
    assert observations[0]["id"] == 123


def test_matcher_fetch_observations_migrates_json_cache_and_upserts_fetched(tmp_path: Path) -> None:
    from inat2exif.observation_store import ObservationStore

    cache_dir = tmp_path / "cache"
    obs_cache_dir = cache_dir / "observations"
    for obs_id in (1, 2):
        (obs_cache_dir / str(obs_id)).mkdir(parents=True)
        (obs_cache_dir / str(obs_id) / "observation.json").write_text(
            json.dumps({"id": obs_id, "updated_at": "2024-04-27T10:50:00Z", "species_guess": "old"}),
            encoding="utf-8",
        )
    (obs_cache_dir / "1" / "thumbnail.jpeg").write_bytes(b"thumb")
    (obs_cache_dir / "999").mkdir()  # orphaned dir for an unknown observation is pruned
    requested: list[str] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"results": [{"id": 2, "updated_at": "2024-04-28T00:00:00Z", "species_guess": "new"}]}

    def _get(url, timeout):
        requested.append(url)
        return FakeResponse()

    observations = matcher_logic.fetch_observations(
        "tester",
        cache_dir,
        requests_module=types.SimpleNamespace(get=_get),
        time_module=types.SimpleNamespace(time=lambda: 0.0, sleep=lambda _s: None),
        cache_ttl_s=60,
        per_page=100,
    )

    assert "updated_since=2024-04-27T10%3A50%3A00Z" in requested[0]
    by_id = {obs["id"]: obs for obs in observations}
    assert by_id[1]["species_guess"] == "old"
    assert by_id[2]["species_guess"] == "new"
    assert not list(obs_cache_dir.glob("*/observation.json"))
    assert (obs_cache_dir / "1" / "thumbnail.jpeg").read_bytes() == b"thumb"
    assert not (obs_cache_dir / "2").exists()
    assert not (obs_cache_dir / "999").exists()
    with ObservationStore(cache_dir / "observations.db") as store:
        assert store.ids() == {1, 2}
        assert store.get(2)["species_guess"] == "new"


def test_matcher_match_observations_auto_exact() -> None:
    observed_at = dt.datetime(2024, 4, 27, 10, 45, 23, tzinfo=dt.UTC)
    ts = observed_at.timestamp()
    minute_ts = observed_at.replace(second=0).timestamp()

    obs = _Observation(
        obs_id=42,
        observed_at=observed_at,
        photos=[{"id": 1001, "url": "https://example.invalid/square.jpg"}],
        uri="https://www.inaturalist.org/observations/42",
    )
    photo_a = _LocalPhoto(id=1, exif_datetime_raw="2024:04:27 10:45:23", timestamp=ts, minute_timestamp=minute_ts)
    photo_b = _LocalPhoto(id=2, exif_datetime_raw="2024:04:27 10:45:23", timestamp=ts, minute_timestamp=minute_ts)

    by_second = {ts: [photo_a, photo_b]}
    by_minute = {minute_ts: [photo_a, photo_b]}
    by_id = {1: photo_a, 2: photo_b}

    matches, refresh_ids = matcher_logic.match_observations(
        [obs],
        by_second,
        by_minute,
        by_id,
        match_result_cls=_MatchResult,
        group_versions=False,
        cache_dir=Path("data/cache"),
        cache_observation_photo_fn=lambda _photo, _cache_dir: Path("thumb.jpg"),
        score_candidates_fn=lambda _obs_paths, _minute: [(1, 0.9), (2, 0.8)],
        expand_with_version_siblings_fn=lambda selected, _by_id: set(selected),
        metadata_refresh_check_fn=lambda _photo, _obs: (False, []),
        cv_score_threshold=0.28,
        cv_margin_threshold=0.08,
    )

    assert len(matches) == 1
    assert matches[0].status == "auto-exact"
    assert matches[0].selected_photo_ids == {1, 2}
    assert refresh_ids == set()


def test_matcher_metadata_refresh_check_reports_progress_and_selects_dirty_photos() -> None:
    observed_at = dt.datetime(2024, 4, 27, 10, 45, 23, tzinfo=dt.UTC)
    ts = observed_at.timestamp()
    minute_ts = observed_at.replace(second=0).timestamp()

    obs = _Observation(obs_id=42, observed_at=observed_at, photos=[], uri="https://www.inaturalist.org/observations/42")
    tagged_dirty = _LocalPhoto(
        id=1, exif_datetime_raw="2020:01:01 00:00:00", timestamp=0.0, minute_timestamp=0.0, existing_observation_id=42
    )
    tagged_clean = _LocalPhoto(
        id=2, exif_datetime_raw="2020:01:01 00:00:00", timestamp=0.0, minute_timestamp=0.0, existing_observation_id=42
    )
    tagged_unknown_obs = _LocalPhoto(
        id=3, exif_datetime_raw="2020:01:01 00:00:00", timestamp=0.0, minute_timestamp=0.0, existing_observation_id=999
    )
    untagged = _LocalPhoto(id=4, exif_datetime_raw="2024:04:27 10:45:23", timestamp=ts, minute_timestamp=minute_ts)
    by_id = {p.id: p for p in (tagged_dirty, tagged_clean, tagged_unknown_obs, untagged)}

    checked_photo_ids: list[int] = []

    def _refresh_check(photo, _obs):
        checked_photo_ids.append(photo.id)
        return (photo.id == 1, ["MISSING: Xmp.dc.title"] if photo.id == 1 else [])

    events: list[tuple[str, str, int | None, int | None]] = []
    matches, refresh_ids = matcher_logic.match_observations(
        [obs],
        {ts: [untagged]},
        {minute_ts: [untagged]},
        by_id,
        match_result_cls=_MatchResult,
        group_versions=False,
        cache_dir=Path("data/cache"),
        cache_observation_photo_fn=lambda _photo, _cache_dir: Path("thumb.jpg"),
        score_candidates_fn=lambda _obs_paths, _minute: [],
        expand_with_version_siblings_fn=lambda selected, _by_id: set(selected),
        metadata_refresh_check_fn=_refresh_check,
        cv_score_threshold=0.28,
        cv_margin_threshold=0.08,
        progress_cb=lambda phase, message, current=None, total=None: events.append((phase, message, current, total)),
    )

    assert sorted(checked_photo_ids) == [1, 2]
    assert refresh_ids == {1}
    assert tagged_dirty.metadata_refresh_needed is True
    assert 1 in matches[0].candidate_photo_ids
    assert 1 in matches[0].selected_photo_ids

    refresh_events = [event for event in events if event[0] == "refresh"]
    assert refresh_events[0][2:] == (0, 2)
    assert refresh_events[-1] == ("refresh", "1 of 2 tagged photos need a metadata refresh", 2, 2)


def _jpeg_header_bytes(width: int, height: int, *, sof_marker: int = 0xC0) -> bytes:
    # APP1 payload deliberately contains a fake SOF marker to check segments are skipped, not scanned.
    app1_payload = b"Exif\x00\x00" + b"\xff\xc0\x00\x11\x08\x00\x10\x00\x10"
    app1 = b"\xff\xe1" + (len(app1_payload) + 2).to_bytes(2, "big") + app1_payload
    sof_payload = b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03" + b"\x00" * 9
    sof = bytes([0xFF, sof_marker]) + (len(sof_payload) + 2).to_bytes(2, "big") + sof_payload
    return b"\xff\xd8" + app1 + sof + b"\xff\xda\x00\x02" + b"\xff\xd9"


def test_matcher_jpeg_dimensions_reads_sof_header(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.jpg"
    baseline.write_bytes(_jpeg_header_bytes(5472, 3648))
    progressive = tmp_path / "progressive.jpg"
    progressive.write_bytes(_jpeg_header_bytes(800, 600, sof_marker=0xC2))
    not_jpeg = tmp_path / "not.jpg"
    not_jpeg.write_bytes(b"\x89PNG\r\n\x1a\n")

    assert matcher_logic.jpeg_dimensions(baseline) == (5472, 3648)
    assert matcher_logic.jpeg_dimensions(progressive) == (800, 600)
    assert matcher_logic.jpeg_dimensions(not_jpeg) is None
    assert matcher_logic.jpeg_dimensions(tmp_path / "missing.jpg") is None


@pytest.mark.parametrize(
    ("width", "height", "expected_flag"),
    [
        (5472, 3648, "IMREAD_REDUCED_COLOR_4"),
        (8192, 5464, "IMREAD_REDUCED_COLOR_8"),
        (2048, 1536, "IMREAD_REDUCED_COLOR_2"),
        (2000, 1500, "IMREAD_COLOR"),
        (500, 375, "IMREAD_COLOR"),
    ],
)
def test_matcher_load_cv_image_decodes_at_reduced_size_without_going_below_max_edge(
    tmp_path: Path, width: int, height: int, expected_flag: str
) -> None:
    path = tmp_path / "photo.jpg"
    path.write_bytes(_jpeg_header_bytes(width, height))
    flags = {"IMREAD_COLOR": 1, "IMREAD_REDUCED_COLOR_2": 17, "IMREAD_REDUCED_COLOR_4": 33, "IMREAD_REDUCED_COLOR_8": 65}
    used_flags: list[int] = []

    def _imread(_path: str, flag: int):
        used_flags.append(flag)
        return None

    fake_cv2 = types.SimpleNamespace(imread=_imread, **flags)
    assert matcher_logic.load_cv_image(path, cv2_module=fake_cv2, max_edge=1024) is None
    assert used_flags[0] == flags[expected_flag]
    if expected_flag != "IMREAD_COLOR":
        # A failed reduced decode falls back to a full decode.
        assert used_flags[1:] == [flags["IMREAD_COLOR"]]


def test_matcher_parallel_cv_scoring_preserves_order_and_reports_progress() -> None:
    base = dt.datetime(2024, 4, 27, 10, 45, 0, tzinfo=dt.UTC)
    observations = []
    by_minute: dict[float, list[_LocalPhoto]] = {}
    by_id: dict[int, _LocalPhoto] = {}
    for i in range(6):
        observed_at = base + dt.timedelta(minutes=i)
        minute_ts = observed_at.timestamp()
        observations.append(
            _Observation(obs_id=100 + i, observed_at=observed_at, photos=[{"id": i}], uri=f"u{i}")
        )
        photos = [
            _LocalPhoto(
                id=i * 10 + j,
                exif_datetime_raw=f"2024:04:27 10:{45 + i}:{10 + j}",
                timestamp=minute_ts + 10 + j,
                minute_timestamp=minute_ts,
            )
            for j in range(2)
        ]
        by_minute[minute_ts] = photos
        by_id.update({p.id: p for p in photos})

    def _score(_obs_paths, minute):
        # Even minutes get a clear winner, odd minutes stay ambiguous.
        if len(minute) != 2:
            return [(photo.id, 0.0) for photo in minute]  # wider-window fallback finds nothing
        first, second = minute
        minute_index = (first.id // 10)
        if minute_index % 2 == 0:
            return [(first.id, 0.9), (second.id, 0.1)]
        return [(second.id, 0.2), (first.id, 0.19)]

    events: list[tuple[str, str, int | None, int | None]] = []
    matches, _refresh = matcher_logic.match_observations(
        observations,
        {},
        by_minute,
        by_id,
        match_result_cls=_MatchResult,
        group_versions=False,
        cache_dir=Path("data/cache"),
        cache_observation_photo_fn=lambda _photo, _cache_dir: Path("thumb.jpg"),
        score_candidates_fn=_score,
        expand_with_version_siblings_fn=lambda selected, _by_id: set(selected),
        metadata_refresh_check_fn=lambda _photo, _obs: (False, []),
        cv_score_threshold=0.28,
        cv_margin_threshold=0.08,
        progress_cb=lambda phase, message, current=None, total=None: events.append((phase, message, current, total)),
        cv_max_workers=4,
    )

    assert [m.observation.obs_id for m in matches] == [100 + i for i in range(6)]
    for i, match in enumerate(matches):
        if i % 2 == 0:
            assert match.status == "auto-cv"
            assert match.selected_photo_ids == {i * 10}
        else:
            assert match.status == "ambiguous"
            assert match.selected_photo_ids == set()

    cv_events = [event for event in events if event[0] == "cv"]
    assert cv_events[0][2:] == (0, 6)
    assert cv_events[-1][2:] == (6, 6)


def test_matcher_same_second_multiple_observations_do_not_lock_to_single_exact_candidate() -> None:
    observed_at = dt.datetime(2026, 4, 24, 22, 25, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    second_ts = observed_at.timestamp()
    minute_ts = observed_at.replace(second=0).timestamp()

    obs_a = _Observation(
        obs_id=353114080,
        observed_at=observed_at,
        photos=[{"id": 644332587, "url": "https://example.invalid/a.jpg"}],
        uri="https://www.inaturalist.org/observations/353114080",
    )
    obs_b = _Observation(
        obs_id=353114110,
        observed_at=observed_at,
        photos=[{"id": 644332595, "url": "https://example.invalid/b.jpg"}],
        uri="https://www.inaturalist.org/observations/353114110",
    )

    exact_second_photo = _LocalPhoto(
        id=1,
        exif_datetime_raw="2026:04:24 22:25:00",
        timestamp=second_ts,
        minute_timestamp=minute_ts,
    )
    minute_sibling_photo = _LocalPhoto(
        id=2,
        exif_datetime_raw="2026:04:24 22:25:25",
        timestamp=observed_at.replace(second=25).timestamp(),
        minute_timestamp=minute_ts,
    )

    by_second = {second_ts: [exact_second_photo]}
    by_minute = {minute_ts: [exact_second_photo, minute_sibling_photo]}
    by_id = {1: exact_second_photo, 2: minute_sibling_photo}

    matches, _refresh_ids = matcher_logic.match_observations(
        [obs_a, obs_b],
        by_second,
        by_minute,
        by_id,
        match_result_cls=_MatchResult,
        group_versions=False,
        cache_dir=Path("data/cache"),
        cache_observation_photo_fn=lambda _photo, _cache_dir: Path("thumb.jpg"),
        score_candidates_fn=lambda _obs_paths, _minute: [],
        expand_with_version_siblings_fn=lambda selected, _by_id: set(selected),
        metadata_refresh_check_fn=lambda _photo, _obs: (False, []),
        cv_score_threshold=0.28,
        cv_margin_threshold=0.08,
    )

    assert len(matches) == 2
    for match in matches:
        assert match.status == "ambiguous"
        assert set(match.candidate_photo_ids) == {1, 2}
        assert match.selected_photo_ids == set()


def test_score_candidates_reuses_cached_pair_scores_without_loading_images(tmp_path: Path) -> None:
    from inat2exif.cv_score_cache import CVScoreCache

    obs_path = tmp_path / "obs.jpeg"
    local_a = tmp_path / "a.jpg"
    local_b = tmp_path / "b.jpg"
    for path in (obs_path, local_a, local_b):
        path.write_bytes(path.name.encode())
    candidates = [
        types.SimpleNamespace(id=1, path=local_a),
        types.SimpleNamespace(id=2, path=local_b),
    ]
    loaded: list[Path] = []
    pair_scores = {local_a.name: 0.4, local_b.name: 0.9}

    def _load(path):
        loaded.append(Path(path))
        return Path(path).name

    def _score(_obs_img, local_img):
        return pair_scores[local_img]

    def _run(version: str = "v1"):
        loaded.clear()
        with CVScoreCache(tmp_path / "cv_scores.db", version) as cache:
            scored = matcher_logic.score_candidates(
                [obs_path], candidates, load_cv_image_fn=_load, pair_similarity_score_fn=_score, score_cache=cache
            )
            return scored, cache.hits, cache.misses

    assert _run() == ([(2, 0.9), (1, 0.4)], 0, 2)
    assert len(loaded) == 3

    # Second run: every pair is served from the persisted cache with no image decodes.
    assert _run() == ([(2, 0.9), (1, 0.4)], 2, 0)
    assert loaded == []

    # Changing a local file invalidates only that pair.
    pair_scores[local_a.name] = 0.95
    local_a.write_bytes(b"changed contents")
    assert _run() == ([(1, 0.95), (2, 0.9)], 1, 1)
    assert sorted(p.name for p in loaded) == ["a.jpg", "obs.jpeg"]

    # A new algorithm version discards all previously cached scores.
    assert _run("v2")[1:] == (0, 2)


def test_score_candidates_without_cache_scores_zero_for_unloadable_images(tmp_path: Path) -> None:
    candidates = [types.SimpleNamespace(id=1, path=tmp_path / "a.jpg"), types.SimpleNamespace(id=2, path=tmp_path / "b.jpg")]

    def _load(path):
        return None if Path(path).name == "b.jpg" else "img"

    scored = matcher_logic.score_candidates(
        [tmp_path / "obs.jpeg"], candidates, load_cv_image_fn=_load, pair_similarity_score_fn=lambda _a, _b: 0.7
    )
    assert scored == [(1, 0.7), (2, 0.0)]
    assert matcher_logic.score_candidates(
        [tmp_path / "obs.jpeg"], candidates, load_cv_image_fn=lambda _p: None, pair_similarity_score_fn=lambda _a, _b: 0.7
    ) == [(1, 0.0), (2, 0.0)]


def _photo_at(photo_id: int, when: dt.datetime, **kwargs) -> _LocalPhoto:
    return _LocalPhoto(
        id=photo_id,
        exif_datetime_raw=when.strftime("%Y:%m:%d %H:%M:%S"),
        timestamp=when.timestamp(),
        minute_timestamp=when.replace(second=0).timestamp(),
        path=Path(f"local-{photo_id}.jpg"),
        **kwargs,
    )


def _run_match(observations, photos, scores: dict[tuple[int, int], float], *, batched: bool = True):
    """Match with a fake scorer: ``scores[(obs_photo_id, local_photo_id)]``, default 0."""
    by_second: dict[float, list] = {}
    by_minute: dict[float, list] = {}
    for photo in photos:
        by_second.setdefault(photo.timestamp, []).append(photo)
        by_minute.setdefault(photo.minute_timestamp, []).append(photo)

    def per_photo(paths, candidates):
        result = []
        for path in paths:
            obs_photo_id = int(Path(path).stem)
            scored = [(c.id, scores.get((obs_photo_id, c.id), 0.0)) for c in candidates]
            result.append(sorted(scored, key=lambda item: item[1], reverse=True))
        return result

    def pair_score(obs_path, local_path):
        local_id = int(Path(local_path).stem.removeprefix("local-"))
        return scores.get((int(Path(obs_path).stem), local_id), 0.0)

    def batches(jobs, **kwargs):
        return matcher_logic.score_candidate_batches(
            jobs, load_cv_image_fn=str, pair_similarity_score_fn=pair_score, **kwargs
        )

    return matcher_logic.match_observations(
        observations,
        by_second,
        by_minute,
        {photo.id: photo for photo in photos},
        match_result_cls=_MatchResult,
        group_versions=False,
        cache_dir=Path("cache"),
        cache_observation_photo_fn=lambda photo, _cache_dir: Path(f"{photo['id']}.jpg"),
        score_candidates_fn=lambda paths, candidates: per_photo(paths, candidates)[0],
        score_candidates_per_photo_fn=per_photo,
        score_candidate_batches_fn=batches if batched else None,
        expand_with_version_siblings_fn=lambda selected, _by_id: set(selected),
        metadata_refresh_check_fn=lambda _photo, _obs: (False, []),
        cv_score_threshold=0.28,
        cv_margin_threshold=0.08,
        cv_max_workers=1,
    )


def test_matcher_audio_only_observation_gets_its_own_status() -> None:
    when = dt.datetime(2024, 4, 27, 10, 45, 23, tzinfo=dt.UTC)
    obs = _Observation(obs_id=7, observed_at=when, photos=[], uri="u", has_sounds=True)
    silent = _Observation(obs_id=8, observed_at=when, photos=[], uri="u")
    matches, _ = _run_match([obs, silent], [_photo_at(1, when)], {})

    statuses = {match.observation.obs_id: match.status for match in matches}
    assert statuses == {7: "audio-only", 8: "no-photos"}
    assert all(not match.selected_photo_ids for match in matches)


@pytest.mark.parametrize("batched", [True, False])
def test_matcher_finds_timezone_shifted_photo(batched: bool) -> None:
    when = dt.datetime(2021, 10, 30, 19, 16, 14, tzinfo=dt.UTC)
    obs = _Observation(obs_id=1, observed_at=when, photos=[{"id": 100}], uri="u")
    shifted = _photo_at(5, when + dt.timedelta(hours=1, seconds=3))
    decoy = _photo_at(6, when + dt.timedelta(hours=2))
    matches, _ = _run_match([obs], [shifted, decoy], {(100, 5): 0.86, (100, 6): 0.2}, batched=batched)

    assert matches[0].status == "auto-shift"
    assert matches[0].selected_photo_ids == {5}
    assert matches[0].detail == "Local photo time +1h from iNaturalist time"


def test_matcher_timezone_shift_skips_photos_claimed_by_other_observations() -> None:
    when = dt.datetime(2021, 10, 30, 19, 16, 14, tzinfo=dt.UTC)
    other_when = when + dt.timedelta(hours=1)
    lost = _Observation(obs_id=1, observed_at=when, photos=[{"id": 100}], uri="u")
    owner = _Observation(obs_id=2, observed_at=other_when, photos=[{"id": 200}], uri="u")
    photo = _photo_at(5, other_when)
    matches, _ = _run_match([lost, owner], [photo], {(100, 5): 0.9, (200, 5): 0.9})

    by_obs = {match.observation.obs_id: match for match in matches}
    assert by_obs[2].selected_photo_ids == {5}
    assert not by_obs[1].selected_photo_ids


def test_matcher_nearby_tier_accepts_clear_match_and_suggests_unclear_ones() -> None:
    when = dt.datetime(2022, 6, 1, 12, 0, 0, tzinfo=dt.UTC)
    clear = _Observation(obs_id=1, observed_at=when, photos=[{"id": 100}], uri="u")
    unclear_when = when + dt.timedelta(days=3)
    unclear = _Observation(obs_id=2, observed_at=unclear_when, photos=[{"id": 200}], uri="u")
    photos = [
        _photo_at(5, when + dt.timedelta(hours=5, minutes=17)),
        _photo_at(6, when - dt.timedelta(hours=3, minutes=2)),
        _photo_at(7, unclear_when + dt.timedelta(hours=2, minutes=7)),
        _photo_at(8, unclear_when - dt.timedelta(hours=7, minutes=41)),
    ]
    scores = {(100, 5): 0.82, (100, 6): 0.25, (200, 7): 0.4, (200, 8): 0.38}
    matches, _ = _run_match([clear, unclear], photos, scores)

    by_obs = {match.observation.obs_id: match for match in matches}
    assert by_obs[1].status == "auto-nearby"
    assert by_obs[1].selected_photo_ids == {5}
    assert by_obs[1].detail == "Local photo time +5h17m from iNaturalist time"
    assert by_obs[2].status == "ambiguous"
    assert not by_obs[2].selected_photo_ids
    assert set(by_obs[2].candidate_photo_ids) == {7, 8}
    assert by_obs[2].detail == "2 possible match(es) found within ±24h"


@pytest.mark.parametrize("batched", [True, False])
def test_matcher_finds_companion_photos_taken_minutes_after_the_match(batched: bool) -> None:
    when = dt.datetime(2023, 6, 16, 16, 26, 30, tzinfo=dt.UTC)
    obs = _Observation(obs_id=1, observed_at=when, photos=[{"id": 100}, {"id": 101}], uri="u")
    photos = [
        _photo_at(5, when),
        _photo_at(6, when + dt.timedelta(seconds=70)),
        _photo_at(7, when + dt.timedelta(minutes=3)),
        _photo_at(8, when + dt.timedelta(minutes=20)),
    ]
    scores = {(100, 5): 0.9, (101, 6): 0.85, (101, 7): 0.2, (101, 8): 0.95}
    matches, _ = _run_match([obs], photos, scores, batched=batched)

    assert matches[0].status == "auto-exact"
    # Photo 8 looks right but is outside the ±10 minute window.
    assert matches[0].selected_photo_ids == {5, 6}
    assert 6 in matches[0].candidate_photo_ids
    assert matches[0].detail == "1 more photo found within ±10 min of the matched one"


def test_matcher_companion_photos_respect_claims_and_covered_photos() -> None:
    when = dt.datetime(2023, 6, 16, 16, 26, 30, tzinfo=dt.UTC)
    # Both of obs 1's photos are crops of local photo 5, so nothing else should be added.
    cropped = _Observation(obs_id=1, observed_at=when, photos=[{"id": 100}, {"id": 101}], uri="u")
    later = when + dt.timedelta(days=1)
    lost = _Observation(obs_id=2, observed_at=later, photos=[{"id": 200}, {"id": 201}], uri="u")
    owner = _Observation(obs_id=3, observed_at=later + dt.timedelta(seconds=70), photos=[{"id": 300}], uri="u")
    photos = [
        _photo_at(5, when),
        _photo_at(6, when + dt.timedelta(seconds=40)),
        _photo_at(10, later),
        _photo_at(11, later + dt.timedelta(seconds=70)),
    ]
    scores = {
        (100, 5): 0.9, (101, 5): 0.88, (101, 6): 0.5,
        (200, 10): 0.9, (201, 11): 0.9, (300, 11): 0.9,
    }
    matches, _ = _run_match([cropped, lost, owner], photos, scores)

    by_obs = {match.observation.obs_id: match for match in matches}
    assert by_obs[1].selected_photo_ids == {5}
    assert by_obs[2].selected_photo_ids == {10}
    assert by_obs[3].selected_photo_ids == {11}
    assert not by_obs[1].detail and not by_obs[2].detail


def test_select_groups_per_photo_measures_margin_between_version_groups() -> None:
    original = _LocalPhoto(id=1, exif_datetime_raw="", timestamp=0, minute_timestamp=0, version_group="g")
    edit = _LocalPhoto(id=2, exif_datetime_raw="", timestamp=0, minute_timestamp=0, version_group="g")
    other = _LocalPhoto(id=3, exif_datetime_raw="", timestamp=0, minute_timestamp=0)
    by_id = {1: original, 2: edit, 3: other}

    # The original and its edit score almost the same; they're one group, so it's still a clear match.
    chosen = matcher_logic.select_groups_per_photo(
        [[(2, 0.9), (1, 0.89), (3, 0.3)]], by_id, score_threshold=0.4, margin_threshold=0.1
    )
    assert chosen == {"g": 2}

    unclear = matcher_logic.select_groups_per_photo(
        [[(2, 0.9), (3, 0.85)]], by_id, score_threshold=0.4, margin_threshold=0.1
    )
    assert unclear == {}


def test_score_candidates_per_photo_decodes_each_local_image_once() -> None:
    loads: list[str] = []
    candidates = [_LocalPhoto(id=i, exif_datetime_raw="", timestamp=0, minute_timestamp=0, path=Path(f"l{i}.jpg")) for i in (1, 2)]

    def load(path):
        loads.append(str(path))
        return str(path)

    per_photo = matcher_logic.score_candidates_per_photo(
        [Path("o1.jpg"), Path("o2.jpg")],
        candidates,
        load_cv_image_fn=load,
        pair_similarity_score_fn=lambda obs, local: 0.9 if (obs, local) in {("o1.jpg", "l1.jpg"), ("o2.jpg", "l2.jpg")} else 0.1,
    )

    assert per_photo == [[(1, 0.9), (2, 0.1)], [(2, 0.9), (1, 0.1)]]
    assert sorted(loads) == ["l1.jpg", "l2.jpg", "o1.jpg", "o2.jpg"]


def test_histogram_similarity_treats_nan_correlation_as_uncorrelated() -> None:
    shapes: list[tuple] = []

    class Hist:
        def reshape(self, *shape):
            shapes.append(shape)
            return self

    fake_cv2 = types.SimpleNamespace(
        COLOR_BGR2HSV=0,
        HISTCMP_CORREL=0,
        cvtColor=lambda img, _code: img,
        calcHist=lambda *_args: Hist(),
        normalize=lambda *_args: None,
        compareHist=lambda *_args: float("nan"),
    )

    assert matcher_logic.histogram_similarity(object(), object(), cv2_module=fake_cv2) == 0.5
    assert shapes == [(-1, 1), (-1, 1)]


def test_library_search_priority_prefers_close_times_and_matching_shapes() -> None:
    obs_aspects = [1.5]
    same_day_same_shape = matcher_logic.library_search_priority(3 * 3600, 1.5, obs_aspects)
    same_day_square = matcher_logic.library_search_priority(3 * 3600, 1.0, obs_aspects)
    next_year_same_shape = matcher_logic.library_search_priority(365 * 86400, 1.5, obs_aspects)
    unknown_shape = matcher_logic.library_search_priority(3 * 3600, None, obs_aspects)

    assert same_day_same_shape < same_day_square
    assert same_day_same_shape < next_year_same_shape
    assert unknown_shape == same_day_same_shape
    assert matcher_logic.aspect_ratio((4000, 6000)) == matcher_logic.aspect_ratio((6000, 4000)) == 1.5


def test_score_candidate_batches_reads_shared_local_photos_once() -> None:
    loads: list[str] = []
    shared = _LocalPhoto(id=1, exif_datetime_raw="", timestamp=0, minute_timestamp=0, path=Path("l1.jpg"))
    only_b = _LocalPhoto(id=2, exif_datetime_raw="", timestamp=0, minute_timestamp=0, path=Path("l2.jpg"))
    progress: list[tuple[int, int]] = []

    def load(path):
        loads.append(str(path))
        return str(path)

    results = matcher_logic.score_candidate_batches(
        [([Path("a.jpg")], [shared]), ([Path("b1.jpg"), Path("b2.jpg")], [shared, only_b])],
        load_cv_image_fn=load,
        pair_similarity_score_fn=lambda obs, local: {("a.jpg", "l1.jpg"): 0.7, ("b2.jpg", "l2.jpg"): 0.8}.get((obs, local), 0.1),
        workers=2,
        progress=lambda done, total: progress.append((done, total)),
    )

    assert results == [
        [[(1, 0.7)]],
        [[(1, 0.1), (2, 0.1)], [(2, 0.8), (1, 0.1)]],
    ]
    assert sorted(path for path in loads if path.startswith("l")) == ["l1.jpg", "l2.jpg"]
    assert progress[-1] == (2, 2)


def test_format_time_offset_uses_days_for_long_offsets() -> None:
    assert matcher_logic.format_time_offset(3600) == "+1h"
    assert matcher_logic.format_time_offset(-(5 * 3600 + 17 * 60)) == "−5h17m"
    assert matcher_logic.format_time_offset(47 * 3600) == "+47h"
    assert matcher_logic.format_time_offset(669 * 3600 + 14 * 60) == "+27d21h"
    assert matcher_logic.format_time_offset(-3 * 86400) == "−3d"
