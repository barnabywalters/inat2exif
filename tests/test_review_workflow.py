import datetime as dt
import threading

import pytest
from fastapi.testclient import TestClient

from inat2exif import app as inat2exif


@pytest.fixture
def reviewed_library(tmp_path, monkeypatch):
    state = inat2exif.MatcherState()
    state.cache_dir = tmp_path / "cache"
    state.ready = True
    when = dt.datetime(2026, 6, 15, 10, 30, tzinfo=dt.UTC)
    photos = {}
    for photo_id, name in [(1, "bee_v1.jpg"), (2, "bee_v2.jpg"), (3, "flower.jpg")]:
        path = tmp_path / name
        path.write_bytes(b"image")
        photos[photo_id] = inat2exif.LocalPhoto(
            id=photo_id, path=path, exif_datetime_raw="2026:06:15 10:30:00",
            timestamp=when.timestamp(), minute_timestamp=when.timestamp(),
            version_group="bee" if photo_id < 3 else "flower",
            version_number=photo_id, version_siblings=[1, 2] if photo_id < 3 else [3],
            embedded_xmp={}, existing_observation_id=None,
        )
    first = inat2exif.ObservationData(
        10, when, [{"id": 100}, {"url": "missing-id"}], "https://www.inaturalist.org/observations/10",
        metadata_xmp={"Xmp.dwc.scientificName": "Bombus terrestris"},
        template_context={"name": "Bombus terrestris"},
    )
    second = inat2exif.ObservationData(20, when + dt.timedelta(days=1), [], "u", has_sounds=True)
    state.matches = [
        inat2exif.MatchResult(first, "ambiguous", [1, 2, 3], [(1, 0.8), (2, 0.9), (3, 0.2)], selected_photo_ids={1, 2}),
        inat2exif.MatchResult(second, "audio-only", [], []),
    ]
    state.matches_by_obs_id = {match.observation.obs_id: match for match in state.matches}
    state.local_photos_by_id = photos
    state.obs_photo_by_id = {100: {"id": 100}}
    monkeypatch.setattr(inat2exif, "state", state)
    monkeypatch.setattr(inat2exif, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(inat2exif, "inat_auth", inat2exif.INatAuth())
    return state, TestClient(inat2exif.app)


def test_tree_and_review_routes_preserve_grouping_order_and_selection(reviewed_library):
    state, client = reviewed_library
    tree = client.get("/api/tree").json()
    assert tree["total"] == 2
    assert tree["years"][0]["year"] == 2026
    month = tree["years"][0]["months"][0]
    assert month["observation_count"] == 2
    assert [day["day"] for day in month["days"]] == [15, 16]
    assert month["days"][1]["status_counts"]["photoless"] == 1
    day = client.get("/api/day/2026/6/15").json()
    assert day["observation_count"] == 1
    row = day["rows"][0]
    assert row["obs_id"] == 10 and row["time"] == "10:30:00"
    assert row["observation_photos"] == [{"photo_id": 100, "url": "/api/image/observation/100"}]
    assert row["candidates"][0]["photo_ids"] == [1, 2]
    assert row["candidates"][0]["photo_id"] == 2
    assert row["candidates"][0]["score"] == 0.9
    assert row["candidates"][0]["selected"]
    assert client.get("/api/day/2025/6/15").json()["rows"] == []
    detail = client.get("/api/observation/10").json()
    assert detail["candidates"] == row["candidates"]
    assert detail["observed_at"] == state.matches[0].observation.observed_at.isoformat()
    assert client.get("/api/observation/999").status_code == 404
    selections = client.get("/api/selection-state").json()["rows"]
    assert selections[0] == {"obs_id": 10, "selected_photo_ids": [1, 2], "dirty_photo_ids": []}
    selected = client.post("/api/observation/10/selection", json={"selected_photo_ids": [3]}).json()
    assert selected["selected_count"] == 1
    assert state.matches[0].selected_photo_ids == {3}


def test_image_routes_supply_local_files_and_backfill_observation_photo_owner(reviewed_library, monkeypatch):
    state, client = reviewed_library
    calls = []
    def cached_photo(photo, cache_dir):
        calls.append((photo, cache_dir))
        return state.local_photos_by_id[1].path
    monkeypatch.setattr(inat2exif, "cache_observation_photo", cached_photo)
    assert client.get("/api/image/local/1").content == b"image"
    assert client.get("/api/image/observation/100").content == b"image"
    assert calls[0][0]["observation_id"] == 10
    assert calls[0][1] == state.cache_dir
    assert client.get("/api/image/local/999").status_code == 404
    assert client.get("/api/image/observation/999").status_code == 404
    def failed_download(*args):
        raise RuntimeError("Photo unavailable")
    monkeypatch.setattr(inat2exif, "cache_observation_photo", failed_download)
    result = client.get("/api/image/observation/100")
    assert result.status_code == 500
    assert result.json()["detail"] == "Photo unavailable"


def test_library_search_start_excludes_photos_claimed_by_other_observations(reviewed_library, monkeypatch):
    state, client = reviewed_library
    state.matches[1].selected_photo_ids = {3}
    state.local_photos_by_id[1].existing_observation_id = 10
    received = []
    def search(observation, photos, event):
        received.append((observation.obs_id, [photo.id for photo in photos]))
    monkeypatch.setattr(state, "_run_library_search", search)
    assert client.post("/api/observation/10/library-search").status_code == 200
    state.library_search_thread.join(timeout=2)
    assert not state.library_search_thread.is_alive()
    assert received == [(10, [1, 2])]
    assert client.get("/api/library-search").json()["obs_id"] == 10
    assert client.post("/api/library-search/cancel").status_code == 200
    assert state.library_search_cancel_event.is_set()
    assert client.post("/api/observation/20/library-search").status_code == 400
    assert client.post("/api/observation/999/library-search").status_code == 404
    state.analysis_status["running"] = True
    assert client.post("/api/observation/10/library-search").status_code == 409


def test_accepting_search_candidate_releases_other_selections_and_expands_versions(reviewed_library):
    state, client = reviewed_library
    state.matches[0].selected_photo_ids.clear()
    state.matches[1].selected_photo_ids = {1, 2}
    state.matches[0].candidate_photo_ids = []
    state.library_search_status.update(obs_id=10, candidates=[{"photo_id": 2, "score": 0.95}])
    result = client.post("/api/observation/10/library-search/accept", json={"photo_id": 2}).json()
    assert result["selected_photo_ids"] == [1, 2]
    assert result["released_from"] == [20]
    assert not state.matches[1].selected_photo_ids
    assert state.matches[0].status == "manual-search"
    assert state.matches[0].scored_candidates == [(1, 0.95), (2, 0.95), (3, 0.2)]
    assert state.library_search_cancel_event.is_set()
    assert client.post("/api/observation/999/library-search/accept", json={"photo_id": 2}).status_code == 404
    assert client.post("/api/observation/10/library-search/accept", json={"photo_id": 999}).status_code == 404


@pytest.mark.parametrize("outcome", ["success", "cancelled", "error"])
def test_analysis_worker_reports_completion_cancellation_or_failure(reviewed_library, monkeypatch, outcome):
    state, client = reviewed_library
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def analyze(username, path, *, ignore_hidden_files):
        calls.append((username, path, ignore_hidden_files))
        entered.set()
        assert release.wait(timeout=3)
        if outcome == "cancelled":
            state._cancel_check()
        if outcome == "error":
            raise RuntimeError("Unreadable library")
    monkeypatch.setattr(state, "analyze", analyze)
    root = state.local_photos_by_id[1].path.parent
    result = client.post("/api/session/start", json={"username": "tester", "path": str(root)})
    try:
        assert result.json() == {"started": True, "running": True}
        assert entered.wait(timeout=2)
        assert client.post("/api/session/start", json={"username": "tester", "path": str(root)}).json()["started"] is False
        if outcome == "cancelled":
            assert client.post("/api/session/cancel").json()["cancel_requested"]
    finally:
        release.set()
        state.analysis_thread.join(timeout=3)
    assert not state.analysis_thread.is_alive()
    assert calls == [("tester", root, True)]
    status = client.get("/api/session/status").json()
    assert not status["running"] and status["finished"]
    assert status["phase"] == {"success": "done", "cancelled": "cancelled", "error": "error"}[outcome]
    assert status["cancelled"] == (outcome == "cancelled")
    assert status["error"] == ("Unreadable library" if outcome == "error" else None)
    assert client.post("/api/session/cancel").json() == {"cancel_requested": False, "running": False}


def test_analysis_event_log_is_bounded_and_returned_status_is_independent(reviewed_library):
    state, _ = reviewed_library
    for index in range(205):
        state._log_event("fetch", str(index))
    events = state.analysis_progress()["events"]
    assert len(events) == 200
    assert events[0]["message"] == "5" and events[-1]["message"] == "204"
    events.clear()
    assert len(state.analysis_status["events"]) == 200


@pytest.mark.parametrize("mode", ["download-error", "cancelled", "stale-worker"])
def test_library_search_errors_cancellation_and_stale_workers_do_not_publish_success(reviewed_library, monkeypatch, mode):
    state, _ = reviewed_library
    event = threading.Event()
    state.library_search_cancel_event = event
    state.library_search_status.update(running=True)
    photo_path = state.local_photos_by_id[1].path
    if mode == "download-error":
        def download(*args):
            raise RuntimeError("Offline")
    else:
        download = lambda *args: photo_path
        event.set()
    monkeypatch.setattr(inat2exif, "cache_observation_photo", download)
    monkeypatch.setattr(inat2exif.matcher_logic, "jpeg_dimensions", lambda path: (100, 50))
    if mode == "stale-worker":
        state.library_search_cancel_event = threading.Event()
        before = dict(state.library_search_status)
    state._run_library_search(state.matches[0].observation, list(state.local_photos_by_id.values()), event)
    status = state.library_search_state()
    if mode == "stale-worker":
        assert status == before
    else:
        assert not status["running"] and status["finished"]
        assert status["phase"] == ("error" if mode == "download-error" else "cancelled")
        if mode == "download-error":
            assert "Couldn't download" in status["error"]


def test_pending_save_collision_is_explicit_and_does_not_write(reviewed_library):
    state, client = reviewed_library
    state.matches[1].selected_photo_ids = {1}
    result = client.get("/api/save-preview")
    assert result.status_code == 400
    detail = result.json()["detail"]
    assert detail["message"] == "Some photos are assigned to multiple observations"
    assert detail["conflicts"][0]["photo_name"] == "bee_v1.jpg"
    assert [entry["obs_id"] for entry in detail["conflicts"][0]["obs_ids"]] == [10, 20]
    assert client.post("/api/save").status_code == 400
    assert state.local_photos_by_id[1].path.read_bytes() == b"image"


@pytest.mark.parametrize("route", ["/api/tree", "/api/selection-state", "/api/pending-changes", "/api/save-preview", "/api/day/2026/6/15"])
def test_review_routes_require_completed_analysis(reviewed_library, route):
    state, client = reviewed_library
    state.ready = False
    result = client.get(route)
    assert result.status_code == 400
    assert result.json()["detail"] == "No analysis loaded. Start a session first."
