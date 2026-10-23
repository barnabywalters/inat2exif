# Third-party notices

inat2exif is licensed under the GNU General Public License, version 3 or later (see [LICENSE](LICENSE)).
The distributed application bundles the third-party components listed below. Each component remains under its own licence; the full licence texts are included with each Python package's metadata inside the bundle (`*.dist-info` folders) and are available from the project links.

The licence obligations of the bundled exiv2 library are the main reason inat2exif itself is GPL-licensed.

| Component | Licence | Project |
|---|---|---|
| pyexiv2 (includes exiv2) | GPL-3.0 (exiv2: GPL-2.0-or-later) | https://github.com/LeoHsiao1/pyexiv2, https://exiv2.org |
| OpenCV (opencv-python-headless, including bundled FFmpeg and other libraries) | Apache-2.0 (bundled libraries under their own licences) | https://github.com/opencv/opencv-python |
| NumPy | BSD-3-Clause and others | https://numpy.org |
| Pillow | MIT-CMU | https://python-pillow.org |
| FastAPI | MIT | https://fastapi.tiangolo.com |
| Starlette | BSD-3-Clause | https://www.starlette.io |
| Uvicorn | BSD-3-Clause | https://www.uvicorn.org |
| Pydantic, pydantic-core | MIT | https://docs.pydantic.dev |
| Jinja2, MarkupSafe | BSD-3-Clause | https://palletsprojects.com |
| Requests | Apache-2.0 | https://requests.readthedocs.io |
| urllib3 | MIT | https://urllib3.readthedocs.io |
| certifi | MPL-2.0 | https://github.com/certifi/python-certifi |
| charset-normalizer | MIT | https://github.com/jawah/charset_normalizer |
| idna | BSD-3-Clause | https://github.com/kjd/idna |
| orjson | Apache-2.0 or MIT | https://github.com/ijl/orjson |
| platformdirs | MIT | https://github.com/tox-dev/platformdirs |
| keyring (Windows builds only) | MIT | https://github.com/jaraco/keyring |
| AnyIO, h11, click, annotated-types, typing-extensions, typing-inspection | MIT / BSD-3-Clause / PSF-2.0 | See package metadata |
| CodeMirror 6 (`@codemirror/*`, `@lezer/*`, `@marijn/find-cluster-break`, `crelt`, `style-mod`, `w3c-keyname`) | MIT (text below) | https://codemirror.net |
| Python runtime | PSF-2.0 | https://www.python.org |
| PyInstaller bootloader | GPL-2.0-or-later with bootloader exception | https://pyinstaller.org |

CodeMirror is bundled into `src/inat2exif/web/vendor/codemirror.js` by `npm run build:vendor`.

iNaturalist is a joint initiative of the California Academy of Sciences and the National Geographic Society. inat2exif is not affiliated with or endorsed by iNaturalist.

## CodeMirror licence

Applies to the CodeMirror, Lezer, crelt, style-mod, w3c-keyname and find-cluster-break code in `web/vendor/codemirror.js`. Copyright (C) 2016-2024 by Marijn Haverbeke <marijn@haverbeke.berlin> and others.

```text
MIT License

Copyright (C) 2018-2021 by Marijn Haverbeke <marijn@haverbeke.berlin> and others

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
