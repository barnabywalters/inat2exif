"""Small, bounded preview datasets, independent of the matching session."""

import threading
import time
from collections import OrderedDict
from copy import deepcopy
from urllib.parse import urlencode

import requests


DEMO_OBSERVATION = {
    "id": 0,
    "uri": "",
    "time_observed_at": "2026-06-15T10:30:00+00:00",
    "observed_on": "2026-06-15",
    "description": "A bee visiting flowers beside the walking trail.",
    "place_guess": "Example nature reserve",
    "location": "51.5,-0.1",
    "faves_count": 3,
    "comments_count": 2,
    "user": {"login": "example_observer", "name": "Example Observer"},
    "taxon": {"id": 1, "name": "Bombus terrestris", "preferred_common_name": "Buff-tailed Bumble Bee", "rank": "species"},
    "comments": [
        {"body": "A lovely sighting!", "user": {"login": "example_naturalist", "name": "Example Naturalist"}},
        {"body": "There were several feeding here today.", "user": {"login": "example_observer", "name": "Example Observer"}},
    ],
    "photos": [],
}

_CACHE: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
_LOCK = threading.Lock()
_TTL = 15 * 60
_MAX_USERS = 32


def preview_observations(username: str, *, request_headers: dict | None = None) -> list[dict]:
    username = username.strip().casefold()
    if not username:
        return []
    with _LOCK:
        cached = _CACHE.get(username)
        if not request_headers and cached is not None and time.monotonic() - cached[0] < _TTL:
            _CACHE.move_to_end(username)
            return deepcopy(cached[1])

    def fetch(order_by: str, per_page: int) -> list[dict]:
        query = urlencode({"user_id": username, "order_by": order_by, "order": "desc", "per_page": per_page})
        options = {"headers": request_headers, "allow_redirects": False} if request_headers else {}
        response = requests.get(f"https://api.inaturalist.org/v1/observations?{query}", timeout=20, **options)
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or any(not isinstance(obs, dict) or not isinstance(obs.get("id"), int) for obs in results):
            raise ValueError("Unexpected iNaturalist preview response")
        return results

    recent = fetch("observed_on", 100)
    favourites = fetch("votes", 2) if recent else []
    commented = sorted(recent, key=lambda obs: obs.get("comments_count") or len(obs.get("comments") or []), reverse=True)[:2]
    selected: dict[int, dict] = {}
    for reason, observations in (
        ("Most commented among latest 100", commented),
        ("Most favourited", favourites),
        ("Most recent", recent[:1]),
    ):
        for obs in observations:
            entry = selected.setdefault(obs["id"], {"observation": obs, "reasons": []})
            entry["reasons"].append(reason)
    samples = list(selected.values())
    if request_headers:
        return samples
    with _LOCK:
        _CACHE[username] = (time.monotonic(), samples)
        _CACHE.move_to_end(username)
        while len(_CACHE) > _MAX_USERS:
            _CACHE.popitem(last=False)
    return deepcopy(samples)
