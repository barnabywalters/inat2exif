# inat2exif

Local web app for matching iNaturalist observations to local photos and writing observation metadata into image files.

## What it does

- Fetches observations from iNaturalist for a configured username
- Indexes local JPEG photos by EXIF timestamp
- Matches observations to local photos using timestamp and CV scoring (see [How matching works](#how-matching-works))
- Lets you review selections in a browser UI, with a compact per-observation list of pending metadata changes
- Can search the whole library in the background for observations that still have no match
- Shows a size for each cache in the settings page, with buttons to clear them
- Writes Darwin Core and taxonomy metadata to image XMP fields
- Adds standard EXIF GPS coordinates from iNaturalist when the image has no existing EXIF or XMP GPS coordinates
- Supports configurable title/caption templates (Jinja)

GPS additions appear in the pending-changes preview and are also offered as a
metadata refresh for previously tagged images. Existing GPS coordinates (including
partial coordinate metadata) are never replaced. Only the coordinates returned
by iNaturalist are used: in public-only mode, private locations without coordinates
are skipped and obscured observations use the public, approximate location.
Optional sign-in uses your own private coordinates when available. Removing observation
metadata leaves GPS metadata intact.

## Download and run

Pre-built bundles for Windows (x64), macOS (Apple silicon and Intel) and Linux
(x86_64 and arm64) are produced by the GitHub Actions build. They include Python
and all libraries; nothing else needs to be installed.

- **Windows**: run the `-setup.exe` installer (no administrator rights needed),
  or unzip the `.zip` and run `inat2exif.exe`. A console window shows the server
  log; closing it stops the app.
- **macOS**: open the `.dmg` and drag **inat2exif** to Applications. The app runs
  in the background without a Dock icon; use **Quit inat2exif** in the page to stop
  it. Opening the app again while it is running reopens the browser page.
- **Linux**: make the `.AppImage` executable and run it, or extract the
  `.tar.gz` and run `inat2exif/inat2exif`. Press Ctrl+C in the terminal to stop it.

The app opens in your default browser at `http://127.0.0.1:8000/`. It only listens
on this computer. Starting it again while it is already running just reopens the
page. All builds are currently unsigned, so Windows SmartScreen and macOS
Gatekeeper warn on first launch (on macOS, right-click the app and choose
**Open**, or allow it under **System Settings → Privacy & Security**).

### Run from source

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/):

```bash
uv sync
uv run inat2exif
# or: uv run python -m inat2exif
```

Options:

- `--config PATH` to use a different config file for the session
- `--port N` (default `8000`) and `--host ADDRESS` (default `127.0.0.1`). The port
  is never chosen automatically, because iNaturalist sign-in redirect URLs must
  match exactly
- `--no-browser` to not open a browser page
- `--cache-dir PATH` to use a different cache directory
- `--log-level DEBUG` for verbose logging
- `--no-version-grouping` to disable `_v{n}` sibling grouping
- `--version`

### Where files are stored

| | Linux | macOS | Windows |
|---|---|---|---|
| Config (`config.json`) | `~/.config/inat2exif` | `~/Library/Application Support/inat2exif` | `%LOCALAPPDATA%\inat2exif` |
| Remembered OAuth app credentials | `~/.config/inat2exif/oauth-credentials.json` | `~/Library/Application Support/inat2exif/oauth-credentials.json` | Windows Credential Manager |
| Caches | `~/.cache/inat2exif` | `~/Library/Caches/inat2exif` | `%LOCALAPPDATA%\inat2exif\Cache` |
| Log (`inat2exif.log`, rotated) | `~/.local/state/inat2exif/log` | `~/Library/Logs/inat2exif` | `%LOCALAPPDATA%\inat2exif\Logs` |

Linux paths follow `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` and `XDG_STATE_HOME` when set.
Uninstalling the app does not remove these folders.

## Guided workflow

The sidebar shows five steps and highlights the current stage:

1. **Configure**: choose an iNaturalist account and enter the full photo folder
   path or use **Browse folders** to select a folder on the computer running the app.
   Review the metadata policy, taxonomy
   keyword option, and title/description templates. Settings are remembered;
   **Save settings only** does not start auto-matching or modify photos. The continue
   buttons stay at the bottom for first-time setup; once a complete configuration
   has been saved, they move to the top and stay visible while scrolling.
2. **Auto-match**: **Save settings and auto-match** stores the current form values and
   starts matching. Follow progress or cancel; image metadata is not written.
3. **Review matches**: start with days needing attention, compare reference
   images, and correct the local photo selections. Unmatched observations do not
   prevent you from continuing.
4. **Review changes**: inspect the proposed metadata additions, replacements,
   and removals before choosing **Save changes to files**. You can return to the
   matches to make corrections. If there are no changes, return to configuration.
5. **Save**: keep the tab open while metadata is written. Successful saves return
   to configuration with a completion message. Failed or partial saves show
   errors and offer a return to the remaining changes for review and retry.

Changing configuration requires another auto-match run before saving, including
when refining title or description templates. Starting a new run replaces the
session's current match selections. Empty title/description templates clear those
XMP fields when enabled; switch a field off to leave its existing value untouched.

## Tests

Install the Python test dependencies and run the full suite with coverage:

```bash
uv sync --extra dev
uv run pytest --cov --cov-report=term-missing --cov-report=html
```

The full coverage run must reach **90% Python statement coverage** across all
production modules, including the launcher. Open `htmlcov/index.html` for a
line-by-line report. Tests include native OpenCV comparisons and real image
metadata round trips; they do not need an iNaturalist account or network access.

For a focused run without a whole-project coverage threshold:

```bash
uv run pytest tests/test_app.py
# Or collect partial coverage without applying the full-suite threshold:
uv run pytest tests/test_app.py --cov --cov-fail-under=0
```

### VS Code Testing view

After installing the Python extension and selecting the project's `.venv`
interpreter, use the **Testing** sidebar to discover, run, and inspect individual
Python tests. Choose **Run Tests with Coverage** to browse per-file coverage and
show coverage in the editor. The workspace settings collect production-only
coverage and disable the whole-project threshold for focused editor runs; use the
full CLI command above to enforce the 90% gate.

### JavaScript UI tests

UI regression tests use Node.js's built-in runner and jsdom to render the actual
HTML and exercise the application script. Use Node.js **22.12+ (22.x) or 24+**:

```bash
npm ci
npm test
npm run test:coverage
```

The JavaScript coverage command independently enforces **90% line coverage** of
`src/inat2exif/web/app.js`, and also reports branch and function coverage. Python and JavaScript
coverage are separate measurements. UI tests use mocked HTTP responses and
controlled timers, run offline, and cover manual corrections/conflicts,
pending-change review, saving, configuration, OAuth controls, previews, and
whole-library search. They do not load the vendored CodeMirror editor bundle
or replace live-browser or live-OAuth testing.

## Building bundles

The template editor uses CodeMirror, bundled into
`src/inat2exif/web/vendor/codemirror.js` so the app works offline. After changing
`web-src/codemirror.js` or the CodeMirror npm versions, regenerate and commit it:

```bash
npm ci
npm run build:vendor
```

PyInstaller can only build for the platform it runs on. To build locally:

```bash
uv sync --extra build
uv run pyinstaller --noconfirm --distpath build/dist --workpath build/work packaging/pyinstaller/inat2exif.spec
build/dist/inat2exif/inat2exif --self-test   # macOS: build/dist/inat2exif.app/Contents/MacOS/inat2exif
```

`--self-test` checks that the bundled OpenCV and exiv2 libraries can write and
read image metadata. Platform packages are made by
`packaging/linux/build-appimage.sh VERSION`, `packaging/macos/build-dmg.sh VERSION`
and `packaging/windows/inat2exif.iss` (Inno Setup), and land in `dist/`.
`.github/workflows/build.yml` runs the tests and then all of these on each
platform, uploading the packages as workflow artifacts. Code signing and
notarization are not set up yet. The icon in `packaging/icons` is a placeholder;
`packaging/icons/render.py` regenerates the PNG/ICO/ICNS files from the SVG.

## Configuration

Settings are saved from the configuration page to `config.json` in the config
folder listed above, or to the file given with `--config`.

Common settings include:

- `username`
- `path`
- `ignore_hidden_files` (default `true`; skip names starting with `.` while indexing)
- `write_hierarchical_tags`
- `taxonomy_tag_prefix` (default `"taxonomy"`; use `""` for no top-level prefix)
- `write_dates`, `write_people`, `write_taxonomy`, `write_locality`, `write_gps`
- `write_title`, `write_description`
- `image_title_template`
- `image_caption_template`

All metadata categories default to enabled. Switching a category off excludes it
from writes and refresh checks; existing values are not removed. Observation
links and IDs remain enabled so later runs can recognize tagged images. Locality
XMP fields and standard EXIF GPS are independent options.

Taxonomy keyword prefix changes update the tags managed by inat2exif, without
removing unrelated user keywords. A small tracking record is stored in the
Darwin Core `dynamicProperties` field alongside any existing content, so custom
and unprefixed keywords can be refreshed or removed safely on later runs.
Legacy `taxonomy`-prefixed keywords are recognized without this record.
Explicitly deselecting a previously tagged photo still schedules removal of
the app's observation metadata; this is separate from disabling a sync category.

### Template previews

A built-in, illustrative observation is available immediately, even before
auto-matching or when offline. After entering a username, the app loads up to
five observations for the preview selector: the two most commented among
the latest 100 observations, the two most favourited across the account, and the
most recent. Duplicates are combined. iNaturalist does not offer a comment-count
sort, so the commented examples are not necessarily the most commented all-time.
Preview data is cached in memory for 15 minutes and never changes image files.
Loading errors are displayed while the built-in example remains usable.

### Optional iNaturalist sign-in and privacy statistics

Public-only mode remains the default and needs no credentials. To access your own
obscured/private locations:

1. Open the app using a loopback URL (for example `http://127.0.0.1:8000`).
2. Expand **Set up and sign in with your own OAuth application** in configuration.
3. Register an application at [iNaturalist OAuth applications](https://www.inaturalist.org/oauth/applications).
   Set its redirect URI to the exact callback URL shown in the app. Keep the same
   hostname and port when signing in; `localhost` and `127.0.0.1` are different.
   Application registration is subject to iNaturalist's current requirements.
4. Enter that application's client ID and secret locally, then choose **Sign in on
   iNaturalist**. Enter your account password only on iNaturalist, not in this app.
5. Review the signed-in username and run auto-match again.

This uses the authorization-code flow with a browser-bound, expiring OAuth state,
requests the `login` scope (not `write`), and exchanges the OAuth token for an
iNaturalist API token. No shared application secret is bundled with inat2exif.
Access tokens are kept only in this process; restarting requires signing in again.
Optionally select **Remember application credentials on this computer** to save
the client ID and secret for subsequent sign-ins. These are stored outside your
normal config. On Windows they go in **Windows Credential Manager**. On macOS
and Linux they go in `oauth-credentials.json` in the config folder: a
**plaintext**, owner-only file (0600) inside an owner-only directory (0700), not
an encrypted keychain. Do not include it in shared backups or support reports.
The app refuses to read files with unsafe permissions and reports errors instead
of silently falling back.
Saved secrets never appear in configuration/API responses or browser storage;
the client ID is prefilled and a blank secret reuses the saved one for that ID.
Changing the client ID requires entering its secret again.

Use **Forget saved application credentials** to remove them without ending
your current sign-in. Unchecking the remember option also removes saved
credentials when you next start sign-in. Disconnecting alone keeps remembered
application credentials, but always clears account access tokens. You can also
leave remembering off and let a password manager fill the credential fields.

This is a single-user local app, not a multi-user
server: do not expose it to a network or share its running process.

**While signed in, private locations are used by default**, including template
previews, locality XMP and missing EXIF GPS. Sharing exported images can therefore
reveal locations you hid on iNaturalist. Review pending changes carefully. Turn
off locality/GPS syncing and check location references in title/description
templates if you do not want to embed them. Existing GPS is never replaced,
even when the existing coordinates are approximate.

Authenticated observation responses and preview samples stay in memory and are
not written to the public observation cache. Authenticated runs fetch fresh
observations rather than incrementally reusing public data. Signing in or
disconnecting clears previous match results, requiring another auto-match.
Disconnecting clears in-memory sign-in tokens, private previews and match data; it does
not undo image metadata already saved or revoke the application's authorization
on iNaturalist. Revoke that separately in your iNaturalist account settings.
Authentication failures are shown explicitly, never silently retried as public
requests. Sign out before entering another account's username.

**Check public-data limitations** loads the account's public observations (without
photo downloads or disk caching) and reports privacy restrictions, missing
coordinates, missing usable capture timestamps, and photo observations skipped
by time-based auto-matching. Auto-match also reports these statistics for the data
it actually used, including how many private locations were available when
signed in. Counts overlap. Missing data can be genuinely absent, so these are
indicators of possible benefit, not a promise that signing in will recover dates
or make every observation matchable.

## How matching works

Each observation goes through these steps until it has a match:

1. **Exact time**: a single local photo group taken at the observation's exact second is selected.
2. **Same minute**: if the only photos are from the same minute, they're selected. If several observations or photo groups share the minute, every iNaturalist photo is compared with the local photos (ORB features, colour histogram and edges) and clear winners are selected.
3. **Timezone shift**: for observations that still have no match, photos taken at the same minute but a whole or half number of hours away (up to ±14h) are compared. This catches cameras or uploads with the wrong timezone.
4. **Within ±24h**: anything still unmatched is compared with every unused photo within ±24 hours. Only very clear matches are selected; plausible ones are offered as suggestions.
5. **Other photos of matched observations**: when an observation has more iNaturalist photos than matched local photos, the unused photos taken within ±10 minutes of the matched ones are compared, and clear matches are added.

Photos already selected for, or tagged with, another observation are never taken by the later steps. Observations with only sounds are marked **audio only**, and ones with neither photos nor sounds **no photos**, rather than as unmatched.

For anything still unmatched, the review UI's **Search whole library** button compares the observation's photos with every unused local photo, closest in time and aspect ratio first, and shows the best candidates as it goes.

## Caches

Caches live in the cache folder listed above (or `--cache-dir`) and can be inspected and cleared on the settings page:

- **iNaturalist observation data**: iNaturalist observation data, in SQLite.
- **iNaturalist observation photos**: downloaded iNaturalist photos used for image comparison.
- **Local photo metadata**: dates, XMP metadata and image sizes read from local photos, keyed by path and modification time.
- **Image comparison scores**: image comparison scores, keyed by both photos and the scoring algorithm version, so changes to the algorithm invalidate them.

The first auto-match run after clearing the caches (or after the CV algorithm changes) is much slower than later ones.

## Future improvements

- Add a fallback metadata writer path for files that `pyexiv2` cannot parse (for example problematic Olympus MakerNote directories), using `exiftool` only for those files so analysis can continue and metadata can still be written without stripping non-standard tags.

## Licence

inat2exif is free software under the GNU General Public License, version 3 or
later; see [LICENSE](LICENSE). It bundles third-party components under their own
licences, listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
The test photos in `tests/fixtures` are licensed separately under CC BY 4.0; see
[tests/fixtures/README.md](tests/fixtures/README.md).
