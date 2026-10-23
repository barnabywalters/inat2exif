"""Regenerate raster icons from inat2exif.svg. Requires ImageMagick and Pillow."""

import subprocess
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

subprocess.run(
    ["magick", "-background", "none", str(HERE / "inat2exif.svg"), "-resize", "512x512", str(HERE / "inat2exif.png")],
    check=True,
)
image = Image.open(HERE / "inat2exif.png").convert("RGBA")
image.save(HERE / "inat2exif.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
image.save(HERE / "inat2exif.icns")
image.resize((32, 32), Image.LANCZOS).save(ROOT / "src" / "inat2exif" / "web" / "favicon.png")
