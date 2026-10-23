"""Optional, process-local iNaturalist OAuth credentials and private observations."""

import secrets
import threading
import time
from urllib.parse import urlencode

import requests


WEBSITE = "https://www.inaturalist.org"
API = "https://api.inaturalist.org/v1"


class AuthenticationError(ValueError):
    pass


class INatAuth:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.pending: tuple[str, float, str, str, str] | None = None
        self.access_token: str | None = None
        self.login: str | None = None
        self.error: str | None = None
        self.api_token: tuple[float, str] | None = None
        self.samples: tuple[float, list[dict]] | None = None

    def status(self):
        with self.lock:
            return {"connected": self.access_token is not None, "username": self.login, "error": self.error}

    def begin(self, client_id: str, client_secret: str, redirect_uri: str):
        if not client_id.strip() or not client_secret.strip():
            raise AuthenticationError("Enter the registered application's client ID and client secret")
        state = secrets.token_urlsafe(32)
        with self.lock:
            self.pending = (state, time.monotonic(), client_id.strip(), client_secret.strip(), redirect_uri)
            self.error = None
        return state, WEBSITE + "/oauth/authorize?" + urlencode({
            "client_id": client_id.strip(), "redirect_uri": redirect_uri,
            "response_type": "code", "scope": "login", "state": state,
        })

    @staticmethod
    def _json(response):
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise AuthenticationError("Unexpected response from iNaturalist")
        return data

    @classmethod
    def _jwt(cls, access_token: str):
        data = cls._json(requests.get(
            WEBSITE + "/users/api_token",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=30, allow_redirects=False,
        ))
        token = data.get("api_token")
        if not isinstance(token, str) or not token:
            raise AuthenticationError("iNaturalist did not return an API token; please sign in again")
        return token

    def finish(self, state: str, cookie: str, code: str, denied: bool = False):
        with self.lock:
            pending = self.pending
            if (
                not pending or not state or not cookie or not state.isascii() or not cookie.isascii()
                or not secrets.compare_digest(state, pending[0])
                or not secrets.compare_digest(cookie, pending[0])
                or time.monotonic() - pending[1] > 600
            ):
                raise AuthenticationError("Sign-in expired or could not be verified. Please start again.")
            self.pending = None
            if denied or not code:
                raise AuthenticationError("iNaturalist sign-in was cancelled or declined")
            _, _, client_id, client_secret, redirect_uri = pending
            try:
                data = self._json(requests.post(
                    WEBSITE + "/oauth/token",
                    data={"grant_type": "authorization_code", "client_id": client_id,
                          "client_secret": client_secret, "redirect_uri": redirect_uri, "code": code},
                    timeout=30, allow_redirects=False,
                ))
                token = data.get("access_token")
                if not isinstance(token, str) or not token:
                    raise AuthenticationError("iNaturalist did not return an access token")
                jwt = self._jwt(token)
                user_data = self._json(requests.get(
                    API + "/users/me", headers={"Authorization": jwt}, timeout=30, allow_redirects=False,
                ))
                users = user_data.get("results")
                login = users[0].get("login") if isinstance(users, list) and users and isinstance(users[0], dict) else None
                if not isinstance(login, str) or not login:
                    raise AuthenticationError("Could not verify the signed-in iNaturalist account")
            except requests.RequestException:
                raise AuthenticationError("iNaturalist sign-in failed. Check your application settings and try again.") from None
            except ValueError as exc:
                if isinstance(exc, AuthenticationError):
                    raise
                raise AuthenticationError("iNaturalist returned an invalid sign-in response") from None
            self.access_token = token
            self.login = login
            self.api_token = (time.monotonic(), jwt)
            self.samples = None
            self.error = None

    def headers(self, username: str):
        with self.lock:
            if self.access_token is None:
                return None
            if self.login is None or username.strip().casefold() != self.login.casefold():
                raise AuthenticationError(f"Signed in as {self.login}. Use that username or disconnect for public-only access.")
            try:
                if self.api_token is None or time.monotonic() - self.api_token[0] > 1800:
                    self.api_token = (time.monotonic(), self._jwt(self.access_token))
                return {"Authorization": self.api_token[1]}
            except (requests.RequestException, ValueError):
                raise AuthenticationError("Authenticated access failed. Sign in again or disconnect to use public data.") from None

    def disconnect(self):
        with self.lock:
            self.pending = None
            self.access_token = None
            self.login = None
            self.error = None
            self.api_token = None
            self.samples = None


def with_private_location(observation: dict) -> dict:
    """Use owner-visible locations consistently in metadata and template contexts."""
    obs = dict(observation)
    if obs.get("private_location"):
        obs["location"] = obs["private_location"]
        obs.pop("geojson", None)
        if obs.get("private_geojson"):
            obs["geojson"] = obs["private_geojson"]
        # Both coordinate representations must describe the same location.
        obs.pop("latitude", None)
        obs.pop("longitude", None)
    if obs.get("private_place_guess"):
        obs["place_guess"] = obs["private_place_guess"]
    return obs
