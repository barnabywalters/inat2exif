import datetime as dt
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from inat2exif import app as inat2exif
from inat2exif import template_samples


def observation():
    when = dt.datetime(2026, 6, 15, tzinfo=dt.UTC)
    raw = dict(template_samples.DEMO_OBSERVATION)
    return inat2exif.ObservationData(
        42, when, [], "https://www.inaturalist.org/observations/42",
        metadata_xmp={
            **inat2exif.build_observation_metadata_xmp(raw, 42, "u", when),
            "Xmp.lr.hierarchicalSubject": ["taxonomy|Animalia|Insecta", "taxonomy|Animalia", "taxonomy"],
            "Xmp.dc.subject": ["taxonomy/Animalia/Insecta", "taxonomy/Animalia", "taxonomy"],
        },
    )


@pytest.mark.parametrize("setting,keys", list(inat2exif.METADATA_CATEGORIES.items()) + [
    ("write_title", {"Xmp.dc.title"}),
    ("write_description", {"Xmp.dc.description"}),
    ("write_hierarchical_tags", inat2exif.TAG_XMP_KEYS),
])
def test_disabled_categories_are_not_written_or_previewed(setting, keys):
    config = inat2exif.AppConfig(**{setting: False})
    expected = inat2exif._effective_metadata_xmp(observation(), config)
    assert all(key not in expected for key in keys)
    assert "Xmp.dwc.catalogNumber" in expected
    embedded = {key: "keep my existing value" for key in keys}
    written = inat2exif._tag_write_payload(embedded, expected)
    assert all(key not in written for key in keys)
    assert not any(entry["key"] in keys for entry in inat2exif.compute_metadata_diff(embedded, expected))


def test_gps_and_locality_can_be_selected_independently():
    obs = observation()
    expected = inat2exif._effective_metadata_xmp(obs, inat2exif.AppConfig(write_locality=False))
    assert "Xmp.dwc.decimalLatitude" not in expected
    assert inat2exif._missing_gps_payload({}, {}, expected)["Exif.GPSInfo.GPSLatitudeRef"] == "N"
    expected = inat2exif._effective_metadata_xmp(obs, inat2exif.AppConfig(write_gps=False))
    assert expected["Xmp.dwc.decimalLatitude"] == "51.5"
    assert inat2exif._missing_gps_payload({}, {}, expected) == {}


@pytest.mark.parametrize("prefix,root", [("taxonomy", "taxonomy/Animalia"), ("Nature", "Nature/Animalia"), ("", "Animalia")])
def test_prefix_changes_share_merge_diff_and_removal_rules(prefix, root):
    obs = observation()
    expected = inat2exif._effective_metadata_xmp(obs, inat2exif.AppConfig(taxonomy_tag_prefix=prefix))
    assert root in expected["Xmp.dc.subject"]
    assert not any(tag == "" for tag in expected["Xmp.dc.subject"])
    embedded = {"Xmp.dc.subject": ["holiday", "taxonomy/Old"], "Xmp.dwc.dynamicProperties": "other-app-data=keep"}
    payload = inat2exif._tag_write_payload(embedded, expected)
    assert payload["Xmp.dc.subject"] == ["holiday", *expected["Xmp.dc.subject"]]
    assert payload[inat2exif.TAG_MANIFEST_KEY].startswith("other-app-data=keep\n")
    diff = inat2exif.compute_metadata_diff(embedded, expected)
    tags = next(entry for entry in diff if entry["key"] == "Xmp.dc.subject")
    assert tags["removed"] == ["taxonomy/Old"]
    assert tags["added"] == expected["Xmp.dc.subject"]
    embedded.update(payload)
    assert inat2exif.compute_metadata_diff(embedded, expected) == []
    # A second prefix change must remove only previously owned tags.
    expected2 = inat2exif._effective_metadata_xmp(obs, inat2exif.AppConfig(taxonomy_tag_prefix="Other"))
    embedded.update(inat2exif._tag_write_payload(embedded, expected2))
    assert embedded["Xmp.dc.subject"] == ["holiday", *expected2["Xmp.dc.subject"]]
    changes = inat2exif._removal_changes(embedded)
    assert changes["Xmp.dc.subject"] == ["holiday"]
    assert changes[inat2exif.TAG_MANIFEST_KEY] == "other-app-data=keep"


def test_existing_unprefixed_user_tags_are_not_claimed_or_removed():
    expected = inat2exif._effective_metadata_xmp(observation(), inat2exif.AppConfig(taxonomy_tag_prefix=""))
    embedded = {"Xmp.dc.subject": ["Animalia", "holiday"]}
    embedded.update(inat2exif._tag_write_payload(embedded, expected))
    assert embedded["Xmp.dc.subject"].count("Animalia") == 1
    assert "Animalia" not in inat2exif._tag_manifest(embedded)["Xmp.dc.subject"]
    changes = inat2exif._removal_changes(embedded)
    assert changes["Xmp.dc.subject"] == ["Animalia", "holiday"]


def test_corrupt_tag_tracking_fails_instead_of_guessing():
    with pytest.raises(ValueError, match="tracking metadata"):
        inat2exif._tag_write_payload({inat2exif.TAG_MANIFEST_KEY: "inat2exif:managedTags=not-json"}, {"Xmp.dc.subject": ["Animalia"]})


def test_invalid_existing_keyword_value_is_not_silently_replaced():
    with pytest.raises(ValueError, match="Unexpected taxonomy keyword value"):
        inat2exif._tag_write_payload({"Xmp.dc.subject": {"unexpected": "value"}}, {"Xmp.dc.subject": ["Animalia"]})


@pytest.mark.parametrize("prefix", ["animals/plants", "a|b", "a,b", "a\nb"])
def test_invalid_prefix_is_rejected(prefix):
    response = TestClient(inat2exif.app).put("/api/config", json={"taxonomy_tag_prefix": prefix})
    assert response.status_code == 422


def test_builtin_sample_preview_without_matching_or_network(monkeypatch, tmp_path):
    monkeypatch.setattr(inat2exif, "SAMPLE_OBSERVATION_PATH", tmp_path / "missing.json")
    client = TestClient(inat2exif.app)
    response = client.post("/api/template-preview", json={
        "title_template": "{{name}}: {{faves_count}}",
        "caption_template": "{% for c in comments %}{{c.user.name}}: {{c.body}} {% endfor %}",
    })
    assert response.status_code == 200
    assert response.json()["title"] == "Bombus terrestris: 3"
    assert "A lovely sighting!" in response.json()["caption"]
    sample_file = tmp_path / "missing.json"
    sample_file.write_text("{}")
    assert inat2exif.build_sample_template_context()["name"] == "Bombus terrestris"


@pytest.fixture
def sample_api(monkeypatch):
    template_samples._CACHE.clear()
    requests = []
    def fake_get(url, timeout):
        assert timeout == 20
        query = parse_qs(urlparse(url).query)
        requests.append(query)
        recent = [
            {"id": 1, "comments_count": 0, "taxon": {"name": "Recent"}},
            {"id": 2, "comments_count": 10, "taxon": {"name": "Commented"}},
            {"id": 3, "comments_count": 5},
        ]
        results = recent if query["order_by"] == ["observed_on"] else [{"id": 4, "faves_count": 20}, {"id": 2, "faves_count": 10}]
        class Response:
            def raise_for_status(self): pass
            def json(self): return {"results": results}
        return Response()
    monkeypatch.setattr(template_samples.requests, "get", fake_get)
    yield requests
    template_samples._CACHE.clear()


def test_samples_rank_deduplicate_and_cache_per_username(sample_api):
    samples = template_samples.preview_observations("Example")
    assert [entry["observation"]["id"] for entry in samples] == [2, 3, 4, 1]
    assert len(samples[0]["reasons"]) == 2
    assert sample_api[0]["per_page"] == ["100"]
    assert sample_api[1]["order_by"] == ["votes"]
    template_samples.preview_observations("example")
    assert len(sample_api) == 2
    template_samples.preview_observations("someone-else")
    assert len(sample_api) == 4
    samples[0]["observation"]["id"] = 99
    assert template_samples.preview_observations("Example")[0]["observation"]["id"] == 2


def test_sample_cache_expires_and_is_bounded(sample_api):
    template_samples.preview_observations("example")
    template_samples._CACHE["example"] = (template_samples.time.monotonic() - template_samples._TTL - 1, [])
    assert template_samples.preview_observations("example")
    assert len(sample_api) == 4
    for index in range(template_samples._MAX_USERS + 1):
        template_samples.preview_observations(f"observer-{index}")
    assert len(template_samples._CACHE) == template_samples._MAX_USERS
    assert "example" not in template_samples._CACHE


def test_selecting_a_user_sample_renders_that_observation(sample_api):
    client = TestClient(inat2exif.app)
    response = client.get("/api/template-samples", params={"username": "example"})
    assert response.status_code == 200
    assert len(response.json()["samples"]) == 4
    response = client.post("/api/template-preview", json={
        "username": "example", "sample_obs_id": 2, "title_template": "{{name}} {{comments_count}}",
    })
    assert response.json()["title"] == "Commented 10"
    assert client.post("/api/template-preview", json={"username": "example", "sample_obs_id": 999}).status_code == 400


def test_sample_network_failures_are_explicit(monkeypatch):
    template_samples._CACHE.clear()
    def fail(*args, **kwargs):
        raise template_samples.requests.ConnectionError("offline")
    monkeypatch.setattr(template_samples.requests, "get", fail)
    response = TestClient(inat2exif.app).get("/api/template-samples", params={"username": "example"})
    assert response.status_code == 502
    assert "offline" in response.json()["detail"]
    assert not template_samples._CACHE


def test_folder_browser_lists_only_directories_and_returns_usable_path(tmp_path):
    (tmp_path / "photos").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "image.jpg").write_text("not a directory")
    client = TestClient(inat2exif.app)
    data = client.get("/api/folders", params={"path": str(tmp_path)}).json()
    assert data["path"] == str(tmp_path)
    assert data["parent"] == str(tmp_path.parent)
    assert data["folders"] == [{"name": "photos", "path": str(tmp_path / "photos")}]
    data = client.get("/api/folders", params={"path": str(tmp_path), "show_hidden": True}).json()
    assert len(data["folders"]) == 2
    assert client.get("/api/folders", params={"path": str(tmp_path / "missing")}).status_code == 400
    assert client.get("/api/folders", params={"path": str(tmp_path / "image.jpg")}).status_code == 400


def test_folder_browser_exposes_permission_errors(monkeypatch, tmp_path):
    def deny(self): raise PermissionError("Permission denied")
    monkeypatch.setattr(Path, "iterdir", deny)
    response = TestClient(inat2exif.app).get("/api/folders", params={"path": str(tmp_path)})
    assert response.status_code == 400
    assert "Permission denied" in response.json()["detail"]


def test_config_status_requires_saved_complete_config(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    monkeypatch.setattr(inat2exif, "CONFIG_PATH", path)
    client = TestClient(inat2exif.app)
    assert client.get("/api/config/status").json() == {"ready": False}
    path.write_text(json.dumps({"username": "example", "path": "/photos"}))
    assert client.get("/api/config/status").json() == {"ready": True}
    path.write_text(json.dumps({"username": "example", "path": ""}))
    assert client.get("/api/config/status").json() == {"ready": False}
