"""Owner-only storage for application credentials, never account access tokens."""

import json
import os
import stat
import tempfile
from pathlib import Path

from inat2exif import paths


class CredentialStorageError(ValueError):
    pass


def default_credentials_path() -> Path:
    return paths.config_dir() / "oauth-credentials.json"


def _validated(data) -> tuple[str, str]:
    if (
        not isinstance(data, dict) or set(data) != {"client_id", "client_secret"}
        or any(not isinstance(value, str) or not value.strip() for value in data.values())
    ):
        raise CredentialStorageError("The saved application credentials are invalid; forget them and enter them again.")
    return data["client_id"], data["client_secret"]


def credential_store(path: Path):
    """Use owner-only files on POSIX and Windows Credential Manager on Windows."""
    return KeyringCredentialStore() if os.name == "nt" else CredentialFile(path)


class CredentialFile:
    def __init__(self, path: Path):
        self.path = path

    @property
    def location(self) -> str:
        return str(self.path)

    @staticmethod
    def supported() -> bool:
        return os.name == "posix"

    def _directory(self, *, create: bool = False) -> bool:
        if not self.supported():
            raise CredentialStorageError("Remembering credentials requires POSIX file permissions on this platform; use a password manager instead.")
        if create:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            info = self.path.parent.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise CredentialStorageError("The credential directory must be owned by you with owner-only permissions (0700).")
        return True

    def load(self) -> tuple[str, str] | None:
        try:
            if not self._directory():
                return None
            try:
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise CredentialStorageError("The credential file must be a regular file owned by you with permissions 0600.")
                data = json.load(stream)
            return _validated(data)
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise CredentialStorageError("Could not read saved application credentials. Check the credential file and its permissions.") from None

    def save(self, client_id: str, client_secret: str) -> None:
        if not client_id.strip() or not client_secret.strip():
            raise CredentialStorageError("Both application credentials are required before remembering them.")
        temporary: Path | None = None
        try:
            self._directory(create=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) != 0o600:
                    raise CredentialStorageError("The filesystem could not enforce owner-only credential file permissions.")
                json.dump({"client_id": client_id.strip(), "client_secret": client_secret.strip()}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise CredentialStorageError("Could not remember application credentials. Nothing was saved; check the credential directory.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def forget(self) -> None:
        try:
            if self._directory():
                self.path.unlink(missing_ok=True)
        except OSError:
            raise CredentialStorageError("Could not forget the saved credentials. Check the credential file's permissions.") from None


class KeyringCredentialStore:
    """Application credentials in the operating system's credential manager."""

    SERVICE = "inat2exif OAuth application"
    ACCOUNT = "application"
    location = "Windows Credential Manager"

    def __init__(self, backend=None):
        self._backend = backend

    def _keyring(self):
        if self._backend is None:
            import keyring

            self._backend = keyring
        return self._backend

    @staticmethod
    def supported() -> bool:
        return True

    def load(self) -> tuple[str, str] | None:
        try:
            value = self._keyring().get_password(self.SERVICE, self.ACCOUNT)
            return None if value is None else _validated(json.loads(value))
        except CredentialStorageError:
            raise
        except Exception:
            raise CredentialStorageError(f"Could not read saved application credentials from {self.location}.") from None

    def save(self, client_id: str, client_secret: str) -> None:
        if not client_id.strip() or not client_secret.strip():
            raise CredentialStorageError("Both application credentials are required before remembering them.")
        value = json.dumps({"client_id": client_id.strip(), "client_secret": client_secret.strip()})
        try:
            self._keyring().set_password(self.SERVICE, self.ACCOUNT, value)
        except Exception:
            raise CredentialStorageError(f"Could not remember application credentials in {self.location}. Nothing was saved.") from None

    def forget(self) -> None:
        try:
            if self._keyring().get_password(self.SERVICE, self.ACCOUNT) is not None:
                self._keyring().delete_password(self.SERVICE, self.ACCOUNT)
        except Exception:
            raise CredentialStorageError(f"Could not forget the saved credentials in {self.location}.") from None
