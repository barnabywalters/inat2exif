# Test fixtures

## Licence

All photos in this folder, including the cached `thumbnail.jpeg` files, are by
Barnaby Walters and licensed under
[Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/),
not the GPL that covers the rest of the repository.

## Observation data

The JSON files are iNaturalist API responses for Barnaby Walters' own public
observations. Details of other iNaturalist users (identifier profiles, user and
identification IDs, identification remarks, and the names credited on taxon
photos) have been replaced with fictional placeholders by
[`anonymize.py`](anonymize.py). Run it again after adding new real API responses:

```bash
uv run python tests/fixtures/anonymize.py
```
