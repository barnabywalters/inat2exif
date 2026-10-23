"""Replace other iNaturalist users' details in the observation fixtures with fictional ones.

The fixtures are real API responses for the repository author's observations. Identifier
profiles, remarks and taxon photo credits belong to other people, so they are swapped for
consistent placeholders. Run from the repository root: uv run python tests/fixtures/anonymize.py
"""

import json
import re
import uuid
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent
OWNER_LOGIN = "barnabywalters"
FIRST_PLACEHOLDER_USER_ID = 900001
FIRST_PLACEHOLDER_IDENTIFICATION_ID = 900000001
PLACEHOLDER_ICON_PATTERN = re.compile(r"/users/icons/\d+/")
USER_ID_KEYS = {"user_id"}
USER_ID_LIST_KEYS = {"reviewed_by"}
PLACEHOLDER_REMARKS = {
    "Agreeing with this identification.",
    "Could be a similar species from the same group.",
}


def fixture_files() -> list[Path]:
    return sorted(FIXTURES.glob("real_cached_case/*.json")) + sorted(FIXTURES.glob("regression_*/cache/*/observation.json"))


def collect_people(value, users: dict[int, str], photographers: set[str]) -> None:
    if isinstance(value, dict):
        login = value.get("login")
        if (isinstance(login, str) and isinstance(value.get("id"), int) and login != OWNER_LOGIN
                and not login.startswith("example_identifier_")):
            users.setdefault(value["id"], login)
        name = value.get("attribution_name")
        if isinstance(name, str) and name and "Barnaby Walters" not in name and not name.startswith("Example Photographer "):
            photographers.add(name)
        for child in value.values():
            collect_people(child, users, photographers)
    elif isinstance(value, list):
        for child in value:
            collect_people(child, users, photographers)


class Anonymizer:
    def __init__(self, users: dict[int, str], photographers: set[str]):
        ordered = sorted(users)
        self.user_ids = {old: FIRST_PLACEHOLDER_USER_ID + index for index, old in enumerate(ordered)}
        self.logins = {users[old]: f"example_identifier_{index + 1}" for index, old in enumerate(ordered)}
        self.identification_ids: dict[int, int] = {}
        self.photographers = {
            name: f"Example Photographer {index + 1}"
            for index, name in enumerate(sorted(photographers, key=lambda name: (-len(name), name)))
        }

    def user(self, record: dict) -> dict:
        number = self.user_ids[record["id"]] - FIRST_PLACEHOLDER_USER_ID + 1
        login = f"example_identifier_{number}"
        replaced = dict(record)
        replaced.update({
            "id": self.user_ids[record["id"]],
            "login": login,
            "login_autocomplete": login,
            "login_exact": login,
            "name": f"Example Identifier {number}" if record.get("name") else record.get("name"),
            "name_autocomplete": f"Example Identifier {number}" if record.get("name_autocomplete") else record.get("name_autocomplete"),
            "orcid": None,
            "icon": None,
            "icon_url": None,
            "created_at": "2020-01-01T00:00:00+00:00",
        })
        for key in ("observations_count", "identifications_count", "journal_posts_count", "activity_count",
                    "species_count", "annotated_observations_count", "universal_search_rank"):
            if key in replaced:
                replaced[key] = 100 if replaced[key] else 0
        return replaced

    def text(self, value: str) -> str:
        for real, placeholder in self.photographers.items():
            value = value.replace(real, placeholder)
        for real, placeholder in self.logins.items():
            value = re.sub(rf"\b{re.escape(real)}\b", placeholder, value)
        return value

    def __call__(self, value, key: str | None = None):
        if isinstance(value, dict):
            if value.get("login") != OWNER_LOGIN and value.get("id") in self.user_ids and "login" in value:
                value = self.user(value)
            result = {}
            is_third_party_identification = (
                isinstance(value.get("user"), dict) and value["user"].get("id") in self.user_ids
                and "category" in value and "uuid" in value
            )
            for child_key, child in value.items():
                if child_key in USER_ID_KEYS and child in self.user_ids:
                    result[child_key] = self.user_ids[child]
                elif child_key in USER_ID_LIST_KEYS and isinstance(child, list):
                    result[child_key] = [self.user_ids.get(item, item) for item in child]
                elif child_key == "body" and isinstance(child, str) and is_third_party_identification:
                    result[child_key] = child if child in PLACEHOLDER_REMARKS else "Agreeing with this identification."
                elif child_key == "id" and is_third_party_identification:
                    result[child_key] = self.identification_ids.setdefault(
                        child, FIRST_PLACEHOLDER_IDENTIFICATION_ID + len(self.identification_ids))
                elif child_key == "uuid" and is_third_party_identification:
                    result[child_key] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"inat2exif-fixture:{child}"))
                else:
                    result[child_key] = self(child, child_key)
            return result
        if isinstance(value, list):
            return [self(child, key) for child in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def main() -> None:
    files = fixture_files()
    documents = {path: path.read_text(encoding="utf-8") for path in files}
    users: dict[int, str] = {}
    photographers: set[str] = set()
    for text in documents.values():
        collect_people(json.loads(text), users, photographers)
    anonymize = Anonymizer(users, photographers)
    for path, text in documents.items():
        data = anonymize(json.loads(text))
        if text.lstrip().startswith("[\n"):
            output = json.dumps(data, indent=2, ensure_ascii=False)
        else:
            output = json.dumps(data)
        path.write_text(output, encoding="utf-8")
    print(f"Replaced {len(users)} identifiers and {len(photographers)} photographers in {len(files)} files")


if __name__ == "__main__":
    main()
