"""Standard per-user and bundled resource locations."""

from pathlib import Path

from platformdirs import user_cache_path, user_config_path, user_log_path

APP_NAME = "inat2exif"
RESOURCE_DIR = Path(__file__).resolve().parent


def config_dir() -> Path:
    return user_config_path(APP_NAME, appauthor=False)


def cache_dir() -> Path:
    return user_cache_path(APP_NAME, appauthor=False)


def log_dir() -> Path:
    return user_log_path(APP_NAME, appauthor=False)
