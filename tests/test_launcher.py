import json
import logging
import runpy
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest

from inat2exif import __version__, launcher, paths
from inat2exif import app as web


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url))
    opened.clear()
    yield
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "inat2exif", False)]:
        root.removeHandler(handler)
        handler.close()


opened: list[str] = []


def test_parser_defaults_and_options(tmp_path):
    args = launcher.build_parser().parse_args([])
    assert (args.host, args.port, args.config, args.no_browser, args.foreground) == ("127.0.0.1", 8000, None, False, False)
    args = launcher.build_parser().parse_args(
        ["-c", str(tmp_path / "c.json"), "--port", "9001", "--no-browser", "--no-version-grouping", "--log-level", "DEBUG"]
    )
    assert args.config == tmp_path / "c.json" and args.port == 9001 and args.no_browser and args.no_version_grouping


def test_parser_reports_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        launcher.build_parser().parse_args(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"inat2exif {__version__}"


def test_legacy_positional_arguments_are_rejected():
    with pytest.raises(SystemExit):
        launcher.main(["someone", "data/test_data"])


@pytest.mark.parametrize(("host", "expected"), [
    ("127.0.0.1", "http://127.0.0.1:8000/"),
    ("0.0.0.0", "http://127.0.0.1:8000/"),
    ("", "http://127.0.0.1:8000/"),
    ("::", "http://[::1]:8000/"),
    ("::1", "http://[::1]:8000/"),
    ("localhost", "http://localhost:8000/"),
])
def test_app_url(host, expected):
    assert launcher.app_url(host, 8000) == expected


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self.body


@pytest.mark.parametrize(("body", "expected"), [
    (json.dumps({"app": "inat2exif", "version": "1"}).encode(), {"app": "inat2exif", "version": "1"}),
    (json.dumps({"app": "other"}).encode(), None),
    (b"[]", None),
    (b"not json", None),
])
def test_running_instance_identifies_only_inat2exif(monkeypatch, body, expected):
    requested = []
    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda url, timeout: requested.append(url) or FakeResponse(body))
    assert launcher.running_instance("http://127.0.0.1:1/") == expected
    assert requested == ["http://127.0.0.1:1/api/app-info"]


def test_running_instance_returns_none_when_unreachable(monkeypatch):
    def refuse(url, timeout):
        raise ConnectionRefusedError()
    monkeypatch.setattr(launcher.urllib.request, "urlopen", refuse)
    assert launcher.running_instance("http://127.0.0.1:1/") is None


def test_port_available_detects_listening_socket():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert launcher.port_available("127.0.0.1", port) is False
    assert launcher.port_available("127.0.0.1", port) is True


def test_port_available_rejects_unresolvable_host():
    assert launcher.port_available("host.invalid", 8000) is False


def test_configure_logging_writes_file_and_replaces_previous_handlers(tmp_path):
    log_path = launcher.configure_logging("INFO")
    launcher.configure_logging("DEBUG")
    tagged = [h for h in logging.getLogger().handlers if getattr(h, "inat2exif", False)]
    assert len(tagged) == 2
    assert logging.getLogger().level == logging.DEBUG
    logging.getLogger("inat2exif.test").info("hello log")
    for handler in tagged:
        handler.flush()
    assert log_path == tmp_path / "logs" / "inat2exif.log"
    assert "hello log" in log_path.read_text(encoding="utf-8")


def test_configure_logging_survives_missing_streams_and_unwritable_log_dir(tmp_path, monkeypatch):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    monkeypatch.setattr(paths, "log_dir", lambda: blocker / "logs")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert launcher.configure_logging("INFO") is None
    assert sys.stdout is not None and sys.stderr is not None
    sys.stdout.close()


def test_report_error_only_logs_when_not_frozen(monkeypatch, caplog):
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: pytest.fail("should not show a dialog"))
    with caplog.at_level(logging.ERROR):
        launcher.report_error("boom")
    assert "boom" in caplog.text


def test_report_error_shows_macos_alert_when_frozen(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(launcher.subprocess, "run", lambda command, check: calls.append(command))
    launcher.report_error('port "8000" busy')
    assert calls[0][:2] == ["osascript", "-e"]
    assert 'message "port \\"8000\\" busy"' in calls[0][2]


def test_report_error_waits_for_enter_in_frozen_windows_console(monkeypatch):
    prompts = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(launcher.os, "name", "nt")
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt))
    launcher.report_error("boom")
    assert prompts == ["Press Enter to close this window."]


@pytest.mark.parametrize(("frozen", "platform", "foreground", "expected"), [
    (True, "darwin", False, True),
    (True, "darwin", True, False),
    (True, "linux", False, False),
    (False, "darwin", False, False),
])
def test_should_detach(monkeypatch, frozen, platform, foreground, expected):
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    assert launcher.should_detach(types.SimpleNamespace(foreground=foreground)) is expected


def test_wait_for_instance_outcomes(monkeypatch):
    monkeypatch.setattr(launcher.time, "sleep", lambda seconds: None)
    responses = iter([None, {"app": "inat2exif"}])
    monkeypatch.setattr(launcher, "running_instance", lambda url: next(responses))
    assert launcher.wait_for_instance("u", timeout=5) is True

    monkeypatch.setattr(launcher, "running_instance", lambda url: None)
    assert launcher.wait_for_instance("u", types.SimpleNamespace(poll=lambda: 1), timeout=5) is False
    assert launcher.wait_for_instance("u", timeout=0) is False


@pytest.mark.parametrize("frozen", [False, True])
def test_launch_detached_starts_background_server_and_opens_browser(monkeypatch, frozen):
    spawned = []
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda command, **kwargs: spawned.append((command, kwargs)) or "process")
    monkeypatch.setattr(launcher, "wait_for_instance", lambda url, process: process == "process")
    assert launcher.launch_detached(["--port", "9000"], "http://x/", True, None) == 0
    command, kwargs = spawned[0]
    prefix = [sys.executable] if frozen else [sys.executable, "-m", "inat2exif"]
    assert command == [*prefix, "--port", "9000", "--foreground", "--no-browser"]
    assert kwargs["start_new_session"] is True and kwargs["stdout"] == subprocess.DEVNULL
    assert opened == ["http://x/"]


def test_launch_detached_reports_startup_failure(monkeypatch):
    errors = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda command, **kwargs: "process")
    monkeypatch.setattr(launcher, "wait_for_instance", lambda url, process: False)
    monkeypatch.setattr(launcher, "report_error", errors.append)
    assert launcher.launch_detached([], "http://x/", True, Path("/tmp/log")) == 1
    assert "/tmp/log" in errors[0] and opened == []


def test_open_browser_when_started(monkeypatch):
    monkeypatch.setattr(launcher.time, "sleep", lambda seconds: None)
    launcher.open_browser_when_started(types.SimpleNamespace(started=True, should_exit=False), "http://x/")
    launcher.open_browser_when_started(types.SimpleNamespace(started=True, should_exit=True), "http://y/")
    assert opened == ["http://x/"]


class FakeServer:
    instances: list["FakeServer"] = []

    def __init__(self, config):
        self.config = config
        self.started = False
        self.should_exit = False
        FakeServer.instances.append(self)

    def run(self):
        assert web._shutdown_handler is not None
        web._shutdown_handler()
        self.started = self.should_exit


class FakeConfig:
    def __init__(self, app, **kwargs):
        self.app = app
        self.kwargs = kwargs


@pytest.fixture
def fake_uvicorn(monkeypatch):
    FakeServer.instances.clear()
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(Server=FakeServer, Config=FakeConfig))
    monkeypatch.setattr(launcher, "running_instance", lambda url: None)
    monkeypatch.setattr(launcher, "port_available", lambda host, port: True)
    configured = []
    monkeypatch.setattr(web, "configure", lambda **kwargs: configured.append(kwargs))
    started_threads = []
    monkeypatch.setattr(launcher.threading, "Thread", lambda target, args, daemon: types.SimpleNamespace(
        start=lambda: started_threads.append(args)))
    return configured, started_threads


def test_main_serves_with_requested_options(fake_uvicorn, tmp_path, capsys):
    configured, started_threads = fake_uvicorn
    config = tmp_path / "custom.json"
    argv = ["--config", str(config), "--cache-dir", str(tmp_path / "cache"), "--host", "127.0.0.1",
            "--port", "8123", "--no-version-grouping", "--log-level", "WARNING"]
    assert launcher.main(argv) == 0
    assert configured == [{"config_path": config, "cache_dir": tmp_path / "cache", "group_versions": False}]
    server = FakeServer.instances[0]
    assert server.config.app is web.app
    assert server.config.kwargs == {"host": "127.0.0.1", "port": 8123, "log_config": None, "log_level": "warning"}
    assert started_threads[0][1] == "http://127.0.0.1:8123/"
    assert web._shutdown_handler is None
    out = capsys.readouterr().out
    assert "running at http://127.0.0.1:8123/" in out and "Log file:" in out


def test_main_reports_failed_server_start(fake_uvicorn, monkeypatch):
    _, started_threads = fake_uvicorn
    monkeypatch.setattr(FakeServer, "run", lambda self: None)
    monkeypatch.setattr(launcher, "configure_logging", lambda level: None)
    assert launcher.main(["--no-browser"]) == 1
    assert started_threads == []


def test_main_reuses_running_instance(monkeypatch):
    monkeypatch.setattr(launcher, "running_instance", lambda url: {"app": "inat2exif"})
    monkeypatch.setattr(launcher, "port_available", lambda *a: pytest.fail("should not probe the port"))
    assert launcher.main(["--port", "8124"]) == 0
    assert launcher.main(["--port", "8124", "--no-browser"]) == 0
    assert opened == ["http://127.0.0.1:8124/"]


def test_main_refuses_busy_port(monkeypatch):
    errors = []
    monkeypatch.setattr(launcher, "running_instance", lambda url: None)
    monkeypatch.setattr(launcher, "port_available", lambda host, port: False)
    monkeypatch.setattr(launcher, "report_error", errors.append)
    assert launcher.main(["--port", "8125"]) == 2
    assert "Port 8125" in errors[0]


def test_main_detaches_frozen_macos_app(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "running_instance", lambda url: None)
    monkeypatch.setattr(launcher, "port_available", lambda host, port: True)
    monkeypatch.setattr(launcher, "should_detach", lambda args: True)
    monkeypatch.setattr(launcher, "launch_detached", lambda *args: calls.append(args) or 0)
    assert launcher.main(["--port", "8126"]) == 0
    assert calls[0][:3] == (["--port", "8126"], "http://127.0.0.1:8126/", True)


def test_main_uses_sys_argv_by_default(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["inat2exif", "--no-browser"])
    monkeypatch.setattr(launcher, "running_instance", lambda url: {"app": "inat2exif"})
    assert launcher.main() == 0 and opened == []


def test_python_m_entry_point_exits_with_launcher_status(monkeypatch):
    monkeypatch.setattr(launcher, "main", lambda: 7)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("inat2exif", run_name="__main__")
    assert exit_info.value.code == 7


def test_self_test_round_trips_metadata(capsys):
    assert launcher.main(["--self-test"]) == 0
    assert "self-test passed" in capsys.readouterr().out


def test_self_test_reports_unwritable_image(monkeypatch):
    import cv2
    monkeypatch.setattr(cv2, "imwrite", lambda *args: False)
    with pytest.raises(RuntimeError, match="OpenCV"):
        launcher.self_test()


def test_self_test_detects_failed_metadata_round_trip(monkeypatch):
    import pyexiv2

    class ForgetfulImage:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def modify_xmp(self, values):
            pass

        def read_xmp(self):
            return {}
    monkeypatch.setattr(pyexiv2, "Image", ForgetfulImage)
    with pytest.raises(RuntimeError, match="round trip"):
        launcher.self_test()
