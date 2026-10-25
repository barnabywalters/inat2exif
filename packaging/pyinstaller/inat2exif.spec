# PyInstaller spec for one-folder inat2exif builds.
# Build from the repository root with: uv run --extra build pyinstaller --noconfirm packaging/pyinstaller/inat2exif.spec
import os
import platform
import subprocess
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

ROOT = Path(SPECPATH).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from inat2exif import __version__  # noqa: E402

IS_MAC = sys.platform == "darwin"
ICONS = ROOT / "packaging" / "icons"

datas = collect_data_files("inat2exif") + copy_metadata("inat2exif")
datas += [(str(ROOT / "LICENSE"), "."), (str(ROOT / "THIRD_PARTY_NOTICES.md"), ".")]
binaries = []
hiddenimports = []
for package in ("pyexiv2",):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden
if IS_MAC:
    # pyexiv2 loads Exiv2 via ctypes, so PyInstaller misses its INIReader dependency.
    brew_prefix = subprocess.run(
        ["brew", "--prefix", "inih"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    inih_lib = Path(brew_prefix) / "lib"
    inireader_libs = list(inih_lib.glob("libINIReader*.dylib"))
    if not inireader_libs:
        raise FileNotFoundError(f"Homebrew INIReader library not found under {inih_lib}")
    binaries += [(str(path), ".") for path in sorted(inih_lib.glob("*.dylib"))]
hiddenimports += ["inat2exif.app", "keyring.backends.Windows"] if os.name == "nt" else ["inat2exif.app"]

a = Analysis(
    [str(ROOT / "packaging" / "pyinstaller" / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "IPython", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="inat2exif",
    console=not IS_MAC,
    argv_emulation=False,
    upx=False,
    codesign_identity=os.environ.get("INAT2EXIF_CODESIGN_IDENTITY") or None,
    entitlements_file=None,
    icon=str(ICONS / "inat2exif.ico") if os.name == "nt" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="inat2exif", upx=False)

if IS_MAC:
    app = BUNDLE(
        coll,
        name="inat2exif.app",
        icon=str(ICONS / "inat2exif.icns"),
        bundle_identifier="io.github.inat2exif",
        version=__version__,
        info_plist={
            "CFBundleDisplayName": "inat2exif",
            "CFBundleShortVersionString": __version__,
            # Matches the oldest macOS supported by the pyexiv2 wheel for this architecture.
            "LSMinimumSystemVersion": "14.0" if platform.machine() == "arm64" else "13.0",
            "NSHighResolutionCapable": True,
        },
    )
