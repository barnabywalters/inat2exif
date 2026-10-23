import json
import os
import types

import pytest
from fastapi.testclient import TestClient

from inat2exif import __version__
from inat2exif import app as web

ORIGIN = "http://127.0.0.1:8000"


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    state = web.MatcherState()
    state.cache_dir = tmp_path / "cache"
    monkeypatch.setattr(web, "state", state)
    monkeypatch.setattr(web, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(web, "_shutdown_handler", None)
    return state, TestClient(web.app, base_url=ORIGIN)


def test_app_info_reports_identity_and_quit_availability(lifecycle):
    _, client = lifecycle
    assert client.get("/api/app-info").json() == {"app": "inat2exif", "version": __version__, "quit_available": False}
    web.set_shutdown_handler(lambda: None)
    assert client.get("/api/app-info").json()["quit_available"] is True


def test_quit_calls_shutdown_handler(lifecycle):
    _, client = lifecycle
    calls = []
    web.set_shutdown_handler(lambda: calls.append(True))
    response = client.post("/api/app/quit", headers={"Origin": ORIGIN})
    assert response.status_code == 200 and response.json() == {"quitting": True}
    assert calls == [True]


@pytest.mark.parametrize("headers", [{}, {"Origin": "http://evil.example"}])
def test_quit_rejects_cross_site_requests(lifecycle, headers):
    _, client = lifecycle
    web.set_shutdown_handler(lambda: pytest.fail("must not quit"))
    assert client.post("/api/app/quit", headers=headers).status_code == 403


def test_quit_is_unavailable_without_launcher(lifecycle):
    _, client = lifecycle
    response = client.post("/api/app/quit", headers={"Origin": ORIGIN})
    assert response.status_code == 409 and "launcher" in response.json()["detail"]


def test_quit_waits_for_saves(lifecycle):
    state, client = lifecycle
    state.lock = types.SimpleNamespace(acquire=lambda timeout: False)
    web.set_shutdown_handler(lambda: pytest.fail("must not quit"))
    response = client.post("/api/app/quit", headers={"Origin": ORIGIN})
    assert response.status_code == 409 and "save" in response.json()["detail"]


@pytest.mark.parametrize("status_name", ["analysis_status", "library_search_status"])
def test_quit_refuses_while_tasks_run(lifecycle, status_name):
    state, client = lifecycle
    getattr(state, status_name)["running"] = True
    web.set_shutdown_handler(lambda: pytest.fail("must not quit"))
    response = client.post("/api/app/quit", headers={"Origin": ORIGIN})
    assert response.status_code == 409 and "Cancel" in response.json()["detail"]
    assert state.lock.acquire(blocking=False)
    state.lock.release()


def test_configure_applies_launcher_options(lifecycle, tmp_path, monkeypatch):
    state, _ = lifecycle
    monkeypatch.chdir(tmp_path)
    web.configure(config_path=web.Path("custom.json"), cache_dir=web.Path("cache-here"), group_versions=False)
    assert web.CONFIG_PATH == tmp_path / "custom.json"
    assert state.cache_dir == tmp_path / "cache-here"
    assert state.group_versions is False
    web.configure()
    assert web.CONFIG_PATH == tmp_path / "custom.json" and state.group_versions is True


def test_save_config_creates_directory_and_replaces_atomically(lifecycle, tmp_path, monkeypatch):
    path = tmp_path / "nested" / "config.json"
    monkeypatch.setattr(web, "CONFIG_PATH", path)
    config = web.AppConfig(username="someone", path=str(tmp_path))
    web.save_config(config)
    assert json.loads(path.read_text())["username"] == "someone"

    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        web.save_config(web.AppConfig(username="other", path=str(tmp_path)))
    assert json.loads(path.read_text())["username"] == "someone"
    assert sorted(p.name for p in path.parent.iterdir()) == ["config.json"]


def test_default_locations_are_per_user():
    from inat2exif import paths
    assert web.BASE_DIR == paths.RESOURCE_DIR
    assert (paths.RESOURCE_DIR / "web" / "index.html").is_file()
    assert (paths.RESOURCE_DIR / "web" / "vendor" / "codemirror.js").is_file()
    assert (paths.RESOURCE_DIR / "config_defaults.json").is_file()
    for directory in (paths.config_dir(), paths.cache_dir(), paths.log_dir()):
        assert "inat2exif" in directory.parts


def test_vendored_editor_is_served(lifecycle):
    _, client = lifecycle
    response = client.get("/web/vendor/codemirror.js")
    assert response.status_code == 200
    assert "MIT License" in response.text[:200]
