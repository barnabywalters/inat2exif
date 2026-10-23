"""Command-line and desktop entry point for the local inat2exif server."""

import argparse
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from inat2exif import __version__, paths

LOGGER = logging.getLogger("inat2exif")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
STARTUP_TIMEOUT_SECONDS = 120.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inat2exif", description="Run the inat2exif local web app")
    parser.add_argument("--host", default="127.0.0.1", help="Address to listen on (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument(
        "-c", "--config", type=Path,
        help=f"Config file to use (default: {paths.config_dir() / 'config.json'})",
    )
    parser.add_argument("--cache-dir", type=Path, help=f"Cache directory (default: {paths.cache_dir()})")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the app in a web browser")
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Application log level",
    )
    parser.add_argument(
        "--no-version-grouping", action="store_true",
        help="Disable version file grouping (files with _v{x} suffixes)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # Used by the macOS app bundle to run the server after the launcher exits.
    parser.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    # Used by packaging CI to check that native libraries work inside a bundle.
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def app_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"", "0.0.0.0"} else "::1" if host == "::" else host
    if ":" in browser_host:
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}/"


def running_instance(url: str, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(url + "api/app-info", timeout=timeout) as response:
            info = json.load(response)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and info.get("app") == "inat2exif" else None


def port_available(host: str, port: int) -> bool:
    try:
        addresses = socket.getaddrinfo(host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except OSError:
        return False
    family, kind, protocol, _name, address = addresses[0]
    with socket.socket(family, kind, protocol) as sock:
        # Match uvicorn on POSIX; on Windows this option would allow binding over another server.
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(address)
        except OSError:
            return False
    return True


def configure_logging(level: str) -> Path | None:
    # Windowed bundles have no console streams.
    if sys.stdout is None or sys.stderr is None:
        devnull = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stdout or devnull
        sys.stderr = sys.stderr or devnull
    root = logging.getLogger()
    root.setLevel(getattr(logging, level))
    for handler in [handler for handler in root.handlers if getattr(handler, "inat2exif", False)]:
        root.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter(LOG_FORMAT)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.inat2exif = True
    root.addHandler(console)
    log_path = paths.log_dir() / "inat2exif.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("Could not write a log file: %s", exc)
        return None
    file_handler.setFormatter(formatter)
    file_handler.inat2exif = True
    root.addHandler(file_handler)
    return log_path


def report_error(message: str) -> None:
    LOGGER.error("%s", message)
    if not getattr(sys, "frozen", False):
        return
    if sys.platform == "darwin":
        script = f"display alert {json.dumps('inat2exif could not start')} message {json.dumps(message)} as critical"
        subprocess.run(["osascript", "-e", script], check=False)
    elif os.name == "nt" and sys.stdin is not None and sys.stdin.isatty():
        input("Press Enter to close this window.")


def self_test() -> int:
    import tempfile

    import cv2
    import numpy as np
    import pyexiv2

    from inat2exif import app as web

    assert (paths.RESOURCE_DIR / "web" / "vendor" / "codemirror.js").is_file()
    assert web.load_config_defaults()
    with tempfile.TemporaryDirectory() as directory:
        image_path = Path(directory) / "self-test.jpg"
        if not cv2.imwrite(str(image_path), np.full((16, 16, 3), 127, dtype=np.uint8)):
            raise RuntimeError("OpenCV could not write a JPEG")
        with pyexiv2.Image(str(image_path)) as image:
            image.modify_xmp({"Xmp.dc.subject": ["inat2exif self-test"]})
        with pyexiv2.Image(str(image_path)) as image:
            if image.read_xmp().get("Xmp.dc.subject") != ["inat2exif self-test"]:
                raise RuntimeError("Metadata round trip failed")
    print(f"inat2exif {__version__} self-test passed", flush=True)
    return 0


def should_detach(args: argparse.Namespace) -> bool:
    return bool(getattr(sys, "frozen", False)) and sys.platform == "darwin" and not args.foreground


def wait_for_instance(url: str, process: subprocess.Popen | None = None, timeout: float = STARTUP_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if running_instance(url):
            return True
        if process is not None and process.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def launch_detached(argv: list[str], url: str, open_browser: bool, log_path: Path | None) -> int:
    command = [sys.executable, *argv, "--foreground", "--no-browser"]
    if not getattr(sys, "frozen", False):
        command[1:1] = ["-m", "inat2exif"]
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )
    if not wait_for_instance(url, process):
        report_error(f"The inat2exif server did not start. See the log for details: {log_path or 'unavailable'}")
        return 1
    if open_browser:
        webbrowser.open(url)
    return 0


def open_browser_when_started(server, url: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while not server.started and not server.should_exit and time.monotonic() < deadline:
        time.sleep(0.1)
    if server.started and not server.should_exit:
        webbrowser.open(url)


def serve(args: argparse.Namespace, url: str, log_path: Path | None) -> int:
    import uvicorn

    from inat2exif import app as web

    web.configure(config_path=args.config, cache_dir=args.cache_dir, group_versions=not args.no_version_grouping)
    server = uvicorn.Server(uvicorn.Config(
        web.app, host=args.host, port=args.port, log_config=None, log_level=args.log_level.lower(),
    ))
    web.set_shutdown_handler(lambda: setattr(server, "should_exit", True))
    if not args.no_browser:
        threading.Thread(target=open_browser_when_started, args=(server, url), daemon=True).start()
    print(f"inat2exif {__version__} is running at {url}", flush=True)
    print("Use Quit in the app or press Ctrl+C here to stop it.", flush=True)
    if log_path:
        print(f"Log file: {log_path}", flush=True)
    try:
        server.run()
    finally:
        web.set_shutdown_handler(None)
    return 0 if server.started else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if args.self_test:
        return self_test()
    log_path = configure_logging(args.log_level)
    url = app_url(args.host, args.port)
    if running_instance(url):
        LOGGER.info("inat2exif is already running at %s", url)
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    if not port_available(args.host, args.port):
        report_error(
            f"Port {args.port} on {args.host} is already used by another program. Close it or start inat2exif with "
            "--port and a different number. If you use iNaturalist sign-in, register the new callback URL too."
        )
        return 2
    if should_detach(args):
        return launch_detached(argv, url, not args.no_browser, log_path)
    return serve(args, url, log_path)
