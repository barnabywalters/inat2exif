import json
import os
import sys

import pytest

from inat2exif.oauth_credentials import CredentialFile, CredentialStorageError, KeyringCredentialStore, credential_store


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires POSIX owner-only file permissions")


@pytest.fixture
def storage(tmp_path):
    return CredentialFile(tmp_path / "private" / "oauth-credentials.json")


def test_atomic_replace_permissions_and_exact_stored_fields(storage):
    assert storage.load() is None
    storage.save("first", "secret")
    assert storage.load() == ("first", "secret")
    storage.save("second", "replacement")
    assert storage.load() == ("second", "replacement")
    assert json.loads(storage.path.read_text()) == {"client_id": "second", "client_secret": "replacement"}
    assert storage.path.stat().st_mode & 0o777 == 0o600
    assert storage.path.parent.stat().st_mode & 0o777 == 0o700
    assert list(storage.path.parent.iterdir()) == [storage.path]
    storage.forget()
    storage.forget()
    assert storage.load() is None


def test_insecure_file_is_rejected_not_silently_loaded(storage):
    storage.save("client", "secret")
    storage.path.chmod(0o644)
    with pytest.raises(CredentialStorageError, match="0600"):
        storage.load()
    storage.forget()


def test_insecure_directory_is_not_used_or_silently_chmodded(storage):
    storage.path.parent.mkdir(mode=0o755)
    with pytest.raises(CredentialStorageError, match="0700"):
        storage.save("client", "secret")
    assert not storage.path.exists()
    assert storage.path.parent.stat().st_mode & 0o777 == 0o755


def test_symlink_file_is_not_read_and_forgetting_does_not_delete_target(storage, tmp_path):
    storage.path.parent.mkdir(mode=0o700)
    target = tmp_path / "other.json"
    target.write_text('{"client_id":"other","client_secret":"secret"}')
    target.chmod(0o600)
    storage.path.symlink_to(target)
    with pytest.raises(CredentialStorageError, match="Could not read"):
        storage.load()
    storage.forget()
    assert target.exists()
    assert not storage.path.is_symlink()


def test_symlink_directory_is_rejected(storage, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    storage.path.parent.symlink_to(target)
    with pytest.raises(CredentialStorageError, match="0700"):
        storage.save("client", "secret")
    assert not list(target.iterdir())


@pytest.mark.parametrize("data", ["{}", '{"client_id":"id","client_secret":5}', '{"client_id":"id","client_secret":"secret","access_token":"token"}'])
def test_invalid_storage_never_defaults_to_success(storage, data):
    storage.path.parent.mkdir(mode=0o700)
    storage.path.write_text(data)
    storage.path.chmod(0o600)
    with pytest.raises(CredentialStorageError, match="invalid"):
        storage.load()


def test_failed_replace_preserves_previous_credentials_and_cleans_temporary_file(storage, monkeypatch):
    storage.save("original", "secret")
    def fail(*args):
        raise OSError("disk error")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(CredentialStorageError, match="Could not remember"):
        storage.save("new", "secret")
    assert storage.load() == ("original", "secret")
    assert list(storage.path.parent.iterdir()) == [storage.path]


def test_unsupported_platform_does_not_pretend_to_protect_secrets(storage, monkeypatch):
    monkeypatch.setattr(CredentialFile, "supported", staticmethod(lambda: False))
    with pytest.raises(CredentialStorageError, match="POSIX"):
        storage.save("client", "secret")
    assert not storage.path.parent.exists()


class FakeKeyring:
    def __init__(self, fail_on: str | None = None, initial: str | None = None):
        self.values = {} if initial is None else {(KeyringCredentialStore.SERVICE, KeyringCredentialStore.ACCOUNT): initial}
        self.fail_on = fail_on

    def _check(self, operation):
        if self.fail_on == operation:
            raise RuntimeError("backend unavailable")

    def get_password(self, service, account):
        self._check("get")
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self._check("set")
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        self._check("delete")
        del self.values[(service, account)]


def test_credential_store_uses_credential_manager_only_on_windows(tmp_path, monkeypatch):
    path = tmp_path / "oauth-credentials.json"
    monkeypatch.setattr(os, "name", "posix")
    file_store = credential_store(path)
    assert isinstance(file_store, CredentialFile) and file_store.location == str(path)
    monkeypatch.setattr(os, "name", "nt")
    keyring_store = credential_store(path)
    assert isinstance(keyring_store, KeyringCredentialStore)
    assert keyring_store.location == "Windows Credential Manager" and keyring_store.supported()


def test_keyring_store_round_trip_and_forget():
    backend = FakeKeyring()
    store = KeyringCredentialStore(backend)
    assert store.load() is None
    store.save(" client ", " secret ")
    stored = backend.values[(store.SERVICE, store.ACCOUNT)]
    assert json.loads(stored) == {"client_id": "client", "client_secret": "secret"}
    assert store.load() == ("client", "secret")
    store.forget()
    store.forget()
    assert store.load() is None


@pytest.mark.parametrize("stored", ["not json", "{}", '{"client_id":"id","client_secret":""}'])
def test_keyring_store_rejects_invalid_values(stored):
    with pytest.raises(CredentialStorageError, match="Could not read|invalid"):
        KeyringCredentialStore(FakeKeyring(initial=stored)).load()


def test_keyring_store_requires_both_values():
    backend = FakeKeyring()
    with pytest.raises(CredentialStorageError, match="Both"):
        KeyringCredentialStore(backend).save("client", " ")
    assert backend.values == {}


@pytest.mark.parametrize(("operation", "action", "message"), [
    ("get", lambda store: store.load(), "Could not read"),
    ("set", lambda store: store.save("client", "secret"), "Nothing was saved"),
    ("delete", lambda store: store.forget(), "Could not forget"),
])
def test_keyring_store_wraps_backend_errors(operation, action, message):
    backend = FakeKeyring(fail_on=operation, initial='{"client_id":"c","client_secret":"s"}')
    with pytest.raises(CredentialStorageError, match=message):
        action(KeyringCredentialStore(backend))


def test_keyring_store_imports_keyring_lazily(monkeypatch):
    backend = FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", backend)
    store = KeyringCredentialStore()
    store.save("client", "secret")
    assert store.load() == ("client", "secret")
