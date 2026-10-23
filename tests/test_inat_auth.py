import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi.testclient import TestClient

from inat2exif import app as inat2exif
from inat2exif import inat_auth, matcher, template_samples
from inat2exif.oauth_credentials import CredentialFile
from inat2exif.observation_store import ObservationStore


ORIGIN = "http://127.0.0.1:8000"


def response(data):
    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)


@pytest.fixture
def local_app(monkeypatch, tmp_path):
    auth = inat_auth.INatAuth()
    state = inat2exif.MatcherState()
    state.cache_dir = tmp_path / "cache"
    monkeypatch.setattr(inat2exif, "inat_auth", auth)
    monkeypatch.setattr(inat2exif, "state", state)
    monkeypatch.setattr(inat2exif, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(inat2exif, "OAUTH_CREDENTIALS_PATH", tmp_path / "credentials" / "oauth-credentials.json")
    monkeypatch.setattr(inat2exif, "credential_store", CredentialFile)
    client = TestClient(inat2exif.app, base_url=ORIGIN)
    return client, auth, state


@pytest.fixture
def oauth_server(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        assert url == inat_auth.WEBSITE + "/oauth/token"
        assert kwargs["allow_redirects"] is False
        assert kwargs["data"]["grant_type"] == "authorization_code"
        return response({"access_token": "test-access-token"})

    def get(url, **kwargs):
        calls.append((url, kwargs))
        assert kwargs["allow_redirects"] is False
        if url.endswith("/users/api_token"):
            assert kwargs["headers"]["Authorization"] == "Bearer test-access-token"
            return response({"api_token": "test-api-token"})
        assert url.endswith("/users/me")
        assert kwargs["headers"]["Authorization"] == "test-api-token"
        return response({"results": [{"id": 42, "login": "tester"}]})

    monkeypatch.setattr(inat_auth.requests, "post", post)
    monkeypatch.setattr(inat_auth.requests, "get", get)
    return calls


def begin(client):
    result = client.post("/api/auth/start", headers={"Origin": ORIGIN},
                         json={"client_id": "test-client", "client_secret": "test-secret"})
    assert result.status_code == 200
    query = parse_qs(urlsplit(result.json()["url"]).query)
    assert query["scope"] == ["login"]
    assert "test-secret" not in result.text
    assert query["redirect_uri"] == [ORIGIN + "/api/auth/callback"]
    assert "HttpOnly" in result.headers["set-cookie"]
    assert result.headers["cache-control"] == "no-store"
    return query["state"][0]


def connect(client):
    oauth_state = begin(client)
    return client.get("/api/auth/callback", params={"state": oauth_state, "code": "test-code"},
                      follow_redirects=False)


def test_oauth_success_verifies_identity_and_keeps_credentials_out_of_config(local_app, oauth_server):
    client, auth, state = local_app
    state.ready = True
    result = connect(client)
    assert result.status_code == 303
    assert result.headers["location"] == "/"
    assert result.headers["referrer-policy"] == "no-referrer"
    assert not state.ready
    status = client.get("/api/auth/status").json()
    assert status["connected"] and status["username"] == "tester"
    assert "token" not in json.dumps(status)
    assert "secret" not in json.dumps(client.get("/api/config").json())
    assert not inat2exif.CONFIG_PATH.exists()
    assert auth.pending is None
    assert auth.headers("TESTER") == {"Authorization": "test-api-token"}
    assert len(oauth_server) == 3


@pytest.mark.parametrize("origin", [None, "https://other.example"])
def test_oauth_mutations_require_same_origin(local_app, origin):
    client, _, _ = local_app
    headers = {"Origin": origin} if origin else {}
    assert client.post("/api/auth/start", headers=headers,
                       json={"client_id": "id", "client_secret": "secret"}).status_code == 403
    assert client.post("/api/auth/disconnect", headers=headers).status_code == 403


def test_oauth_rejects_network_hostnames(local_app):
    client, _, _ = local_app
    result = client.get("/api/auth/status", headers={"host": "other.example"})
    assert result.status_code == 200
    assert result.json()["available"] is False
    assert result.json()["redirect_uri"] is None
    assert client.post("/api/auth/start", headers={"host": "other.example", "Origin": "http://other.example"},
                       json={"client_id": "id", "client_secret": "secret"}).status_code == 400


@pytest.mark.parametrize("variant", ["wrong-state", "wrong-cookie", "expired", "denied", "non-ascii"])
def test_invalid_oauth_callback_never_connects(local_app, oauth_server, variant):
    client, auth, _ = local_app
    oauth_state = begin(client)
    if variant == "wrong-state":
        oauth_state = "incorrect"
    elif variant == "non-ascii":
        oauth_state = "\N{SNOWMAN}"
    elif variant == "wrong-cookie":
        client.cookies.clear()
    elif variant == "expired":
        pending = auth.pending
        auth.pending = (pending[0], -1000, *pending[2:])
    result = client.get("/api/auth/callback",
                        params={"state": oauth_state, "code": "code", "error": "access_denied" if variant == "denied" else ""},
                        follow_redirects=False)
    assert result.status_code == 303
    assert not auth.status()["connected"]
    assert auth.status()["error"]
    assert oauth_server == []


def test_callback_state_cannot_be_replayed(local_app, oauth_server):
    client, auth, _ = local_app
    oauth_state = begin(client)
    auth.finish(oauth_state, oauth_state, "test-code")
    with pytest.raises(inat_auth.AuthenticationError, match="expired"):
        auth.finish(oauth_state, oauth_state, "test-code")


def test_token_failure_is_explicit_and_does_not_echo_server_content(local_app, monkeypatch):
    client, auth, _ = local_app
    def fail(*args, **kwargs):
        raise requests.HTTPError("a sensitive upstream response")
    monkeypatch.setattr(inat_auth.requests, "post", fail)
    result = connect(client)
    assert result.status_code == 303
    assert not auth.status()["connected"]
    assert "failed" in auth.status()["error"]
    assert "sensitive" not in auth.status()["error"]


def test_identity_mismatch_and_jwt_failure_never_fall_back_to_public(local_app, oauth_server, monkeypatch):
    client, auth, _ = local_app
    connect(client)
    with pytest.raises(inat_auth.AuthenticationError, match="Signed in as tester"):
        auth.headers("another-account")
    auth.api_token = None
    def fail(*args, **kwargs):
        raise requests.HTTPError("expired")
    monkeypatch.setattr(inat_auth.requests, "get", fail)
    with pytest.raises(inat_auth.AuthenticationError, match="Authenticated access failed"):
        auth.headers("tester")
    assert auth.status()["connected"]


def test_disconnect_clears_private_results_and_blocks_changes_during_analysis(local_app, oauth_server):
    client, auth, state = local_app
    connect(client)
    auth.samples = (0, [{"private_location": "1,2"}])
    state.ready = True
    state.obs_photo_by_id = {1: {"private": "data"}}
    state.analysis_status["running"] = True
    assert client.post("/api/auth/disconnect", headers={"Origin": ORIGIN}).status_code == 409
    assert client.post("/api/auth/start", headers={"Origin": ORIGIN},
                       json={"client_id": "id", "client_secret": "secret"}).status_code == 409
    state.analysis_status["running"] = False
    assert client.post("/api/auth/disconnect", headers={"Origin": ORIGIN}).status_code == 200
    assert auth.samples is None and auth.access_token is None and auth.api_token is None
    assert not state.ready and not state.obs_photo_by_id
    assert state.privacy_report is None


def private_observation():
    return {
        "id": 1, "time_observed_at": "2026-06-15T10:30:00Z", "geoprivacy": "obscured",
        "location": "1,2", "geojson": {"coordinates": [2, 1]},
        "private_location": "51.5,-0.1", "private_place_guess": "Private reserve",
        "place_guess": "Approximate reserve", "photos": [],
    }


def test_private_coordinates_replace_both_public_coordinate_forms_and_template_values():
    original = private_observation()
    normalized = inat_auth.with_private_location(original)
    assert original["location"] == "1,2"
    record = inat2exif.build_observation_records([normalized])[0][0]
    assert record.metadata_xmp["Xmp.dwc.decimalLatitude"] == "51.5"
    assert record.metadata_xmp["Xmp.dwc.decimalLongitude"] == "-0.1"
    assert record.metadata_xmp["Xmp.dwc.verbatimLocality"] == "Private reserve"
    assert record.template_context["location"] == "51.5,-0.1"
    expected = inat2exif._effective_metadata_xmp(record, inat2exif.AppConfig())
    gps = inat2exif._missing_gps_payload({}, {}, expected)
    assert gps["Exif.GPSInfo.GPSLatitudeRef"] == "N"
    assert gps["Exif.GPSInfo.GPSLongitudeRef"] == "W"
    assert not inat2exif._missing_gps_payload({}, {"Exif.GPSInfo.GPSLatitude": "1/1 0/1 0/1"}, expected)


def test_privacy_report_counts_all_raw_observations_before_timestamp_filtering():
    raw = [private_observation(), {"id": 2, "geoprivacy": "private", "photos": [{"id": 2}]},
           {"id": 3, "photos": []}]
    public = inat2exif.observation_privacy_report(raw, authenticated=False)
    assert public == {
        "total": 3, "authenticated": False, "restricted_locations": 2, "obscured_locations": 1,
        "private_locations": 1, "missing_coordinates": 2, "missing_datetime": 2,
        "skipped_photo_observations": 1, "restricted_missing_datetime": 1, "private_locations_available": 0,
    }
    signed_in = inat2exif.observation_privacy_report(raw, authenticated=True)
    assert signed_in["private_locations_available"] == 1


def test_authenticated_fetch_bypasses_public_cache_and_never_writes_private_data(tmp_path):
    cache_dir = tmp_path / "cache"
    with ObservationStore(cache_dir / "observations.db") as store:
        store.upsert_many([{"id": 999, "updated_at": "2026-01-01T00:00:00Z"}])
    before = (cache_dir / "observations.db").read_bytes()
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response({"results": [private_observation()]})
    result = matcher.fetch_observations(
        "tester", cache_dir, requests_module=SimpleNamespace(get=get),
        time_module=SimpleNamespace(sleep=lambda _: None), cache_ttl_s=0, per_page=100,
        request_headers={"Authorization": "test-api-token"}, use_cache=False,
    )
    assert result[0]["private_location"] == "51.5,-0.1"
    assert "updated_since" not in calls[0][0]
    assert calls[0][1]["headers"] == {"Authorization": "test-api-token"}
    assert calls[0][1]["allow_redirects"] is False
    assert (cache_dir / "observations.db").read_bytes() == before
    assert not (cache_dir / "observations").exists()
    with pytest.raises(ValueError, match="public disk cache"):
        matcher.fetch_observations("tester", cache_dir, requests_module=None, time_module=None,
                                   cache_ttl_s=0, per_page=100, request_headers={"Authorization": "token"})


def test_analysis_uses_private_metadata_and_counts_skipped_observations(local_app, monkeypatch):
    _, auth, state = local_app
    auth.access_token = "test-access-token"
    auth.login = "tester"
    auth.api_token = (inat_auth.time.monotonic(), "test-api-token")
    def get(url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "test-api-token"
        return response({"results": [private_observation(), {"id": 2, "photos": [{"id": 3}]}]})
    monkeypatch.setattr(inat2exif.requests, "get", get)
    monkeypatch.setattr(inat2exif, "index_local_photos", lambda *args, **kwargs: ([], {}, {}, {}))
    result = state.analyze("tester", state.cache_dir.parent)
    assert result["privacy"]["total"] == 2
    assert result["privacy"]["skipped_photo_observations"] == 1
    assert result["privacy"]["authenticated"]
    assert state.matches[0].observation.metadata_xmp["Xmp.dwc.decimalLatitude"] == "51.5"
    assert not (state.cache_dir / "observations.db").exists()


def test_public_stats_never_use_credentials_or_create_cache(local_app, monkeypatch):
    client, auth, state = local_app
    auth.access_token = "must-not-be-sent"
    def get(url, **kwargs):
        assert "headers" not in kwargs
        return response({"results": [private_observation()]})
    monkeypatch.setattr(inat2exif.requests, "get", get)
    result = client.get("/api/privacy-report?username=tester")
    assert result.status_code == 200
    assert not result.json()["authenticated"]
    assert result.json()["restricted_locations"] == 1
    assert not state.cache_dir.exists()


def test_uncached_fetch_uses_id_cursors_for_accounts_beyond_page_limits(tmp_path):
    calls = []
    def get(url, **kwargs):
        query = parse_qs(urlsplit(url).query)
        calls.append(query)
        assert query["page"] == ["1"]
        assert query["order_by"] == ["id"] and query["order"] == ["asc"]
        first_id = int(query.get("id_above", ["0"])[0]) + 1
        return response({"results": [{"id": first_id}, {"id": first_id + 1}] if first_id < 5 else []})
    observations = matcher.fetch_observations(
        "tester", tmp_path, requests_module=SimpleNamespace(get=get),
        time_module=SimpleNamespace(sleep=lambda _: None), cache_ttl_s=0, per_page=2, use_cache=False,
    )
    assert [obs["id"] for obs in observations] == [1, 2, 3, 4]
    assert calls[-1]["id_above"] == ["4"]
    assert not list(tmp_path.iterdir())


def test_authenticated_previews_are_separate_and_cleared_on_disconnect(local_app, monkeypatch):
    client, auth, _ = local_app
    auth.access_token = "test-access-token"
    auth.login = "tester"
    auth.api_token = (inat_auth.time.monotonic(), "test-api-token")
    template_samples._CACHE.clear()
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        assert kwargs["headers"]["Authorization"] == "test-api-token"
        return response({"results": [private_observation()]})
    monkeypatch.setattr(template_samples.requests, "get", get)
    samples = client.get("/api/template-samples?username=tester")
    assert samples.status_code == 200
    preview = client.post("/api/template-preview", json={
        "username": "tester", "sample_obs_id": 1,
        "title_template": "{{location}}", "caption_template": "{{place_guess}}",
    })
    assert preview.json()["title"] == "51.5,-0.1"
    assert preview.json()["caption"] == "Private reserve"
    assert len(calls) == 2
    assert template_samples._CACHE == {}
    assert client.post("/api/auth/disconnect", headers={"Origin": ORIGIN}).status_code == 200
    assert auth.samples is None


def test_remembered_application_credentials_survive_restart_without_tokens(local_app, oauth_server, monkeypatch):
    client, auth, _ = local_app
    result = client.post("/api/auth/start", headers={"Origin": ORIGIN}, json={
        "client_id": "test-client", "client_secret": "test-secret", "remember_credentials": True,
    })
    assert result.status_code == 200
    oauth_state = parse_qs(urlsplit(result.json()["url"]).query)["state"][0]
    client.get("/api/auth/callback", params={"state": oauth_state, "code": "test-code"}, follow_redirects=False)
    assert auth.status()["connected"]
    path = inat2exif.OAUTH_CREDENTIALS_PATH
    assert json.loads(path.read_text()) == {"client_id": "test-client", "client_secret": "test-secret"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    monkeypatch.setattr(inat2exif, "inat_auth", inat_auth.INatAuth())
    status = client.get("/api/auth/status").json()
    assert not status["connected"] and status["credentials_saved"]
    assert status["client_id"] == "test-client"
    assert "test-secret" not in json.dumps(status)
    assert "test-access-token" not in path.read_text()
    assert not inat2exif.CONFIG_PATH.exists()
    result = client.post("/api/auth/start", headers={"Origin": ORIGIN},
                         json={"client_id": "test-client", "remember_credentials": True})
    assert result.status_code == 200
    assert inat2exif.inat_auth.pending[3] == "test-secret"
    assert not inat2exif.inat_auth.status()["connected"]


def test_unchecking_remember_uses_saved_secret_once_then_removes_it(local_app):
    client, auth, _ = local_app
    storage = CredentialFile(inat2exif.OAUTH_CREDENTIALS_PATH)
    storage.save("test-client", "test-secret")
    result = client.post("/api/auth/start", headers={"Origin": ORIGIN},
                         json={"client_id": "test-client", "remember_credentials": False})
    assert result.status_code == 200
    assert auth.pending[3] == "test-secret"
    assert not inat2exif.OAUTH_CREDENTIALS_PATH.exists()


def test_saved_secret_is_never_used_with_a_different_client_id(local_app):
    client, auth, _ = local_app
    CredentialFile(inat2exif.OAUTH_CREDENTIALS_PATH).save("test-client", "test-secret")
    result = client.post("/api/auth/start", headers={"Origin": ORIGIN},
                         json={"client_id": "different-client", "remember_credentials": True})
    assert result.status_code == 400
    assert auth.pending is None
    assert inat2exif.OAUTH_CREDENTIALS_PATH.exists()


def test_forgetting_credentials_preserves_current_login_and_cancels_pending_auth(local_app, oauth_server):
    client, auth, _ = local_app
    connect(client)
    CredentialFile(inat2exif.OAUTH_CREDENTIALS_PATH).save("test-client", "test-secret")
    begin(client)
    # begin without remembering removes the file; save it again to exercise forgetting.
    CredentialFile(inat2exif.OAUTH_CREDENTIALS_PATH).save("test-client", "test-secret")
    assert client.post("/api/auth/forget-credentials").status_code == 403
    assert client.post("/api/auth/forget-credentials", headers={"Origin": ORIGIN}).status_code == 200
    assert not inat2exif.OAUTH_CREDENTIALS_PATH.exists()
    assert auth.pending is None
    assert auth.status()["connected"]
    assert not client.get("/api/auth/status").json()["credentials_saved"]


def test_storage_error_is_visible_without_breaking_public_mode(local_app):
    client, _, _ = local_app
    path = inat2exif.OAUTH_CREDENTIALS_PATH
    path.parent.mkdir(mode=0o700)
    path.write_text("not-json-secret-data")
    path.chmod(0o600)
    status = client.get("/api/auth/status")
    assert status.status_code == 200
    assert status.json()["credential_storage_error"]
    assert "not-json-secret-data" not in status.text
    assert client.get("/api/config").status_code == 200
    assert client.post("/api/auth/forget-credentials", headers={"Origin": ORIGIN}).status_code == 200


def test_credential_write_failure_prevents_sign_in_without_claiming_success(local_app, monkeypatch):
    client, auth, _ = local_app
    def fail(*args):
        raise inat2exif.CredentialStorageError("Could not remember application credentials")
    monkeypatch.setattr(CredentialFile, "save", fail)
    result = client.post("/api/auth/start", headers={"Origin": ORIGIN}, json={
        "client_id": "test-client", "client_secret": "test-secret", "remember_credentials": True,
    })
    assert result.status_code == 400
    assert auth.pending is None
    assert "test-secret" not in result.text


def test_disconnect_preserves_only_application_credentials_on_disk(local_app, oauth_server):
    client, auth, _ = local_app
    connect(client)
    storage = CredentialFile(inat2exif.OAUTH_CREDENTIALS_PATH)
    storage.save("test-client", "test-secret")
    assert client.post("/api/auth/disconnect", headers={"Origin": ORIGIN}).status_code == 200
    assert auth.access_token is None and auth.api_token is None
    assert storage.load() == ("test-client", "test-secret")
    assert client.get("/api/auth/status").json()["credentials_saved"]
