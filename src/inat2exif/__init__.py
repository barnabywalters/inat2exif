"""Match iNaturalist observations to local photos and write observation metadata."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("inat2exif")
except PackageNotFoundError:  # pragma: no cover - only when running an uninstalled source tree
    __version__ = "0+unknown"
