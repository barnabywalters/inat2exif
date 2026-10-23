from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient

from inat2exif import app as inat2exif


@pytest.fixture
def catalog_docs():
    return {"components": {"schemas": {
        "Observation": {"type": "object", "properties": {
            "user": {"$ref": "#/components/schemas/User", "description": "The observer"},
            "comments": {"type": "array", "items": {"$ref": "#/components/schemas/Comment"}},
            "location": {"anyOf": [{"type": "string", "format": "latlon"}, {"type": "null"}, None]},
            "date": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            "quality": {"allOf": [{"type": "string", "enum": ["research", "casual"]}, None]},
            "invalid": {"$ref": 1}, "ignored": "invalid property", 123: {},
        }},
        "User": {"type": "object", "properties": {
            "login": {"type": "string"}, "self": {"$ref": "#/components/schemas/User"},
        }},
        "Comment": {"type": "object", "properties": {"body": {"type": "string"}}},
    }}}


def test_schema_catalog_handles_arrays_nullable_variants_aliases_and_recursive_refs(catalog_docs):
    entries = inat2exif.build_template_field_catalog_from_schema(catalog_docs)
    by_path = {entry["path"]: entry for entry in entries}
    assert list(by_path) == sorted(by_path)
    assert by_path["user"]["description"] == "The observer"
    assert by_path["user.login"]["kind"] == "string"
    assert by_path["user.self"]["kind"] == "object"
    assert "user.self.self" not in by_path
    assert by_path["comments"]["kind"] == "array"
    assert by_path["comments[].body"]["kind"] == "string"
    assert by_path["location"]["nullable"] is True
    assert by_path["location"]["format"] == "latlon"
    assert by_path["date"]["kind"] == "mixed"
    assert by_path["quality"]["enum"] == ["research", "casual"]
    assert by_path["name"]["source"] == "alias"
    assert "invalid" not in by_path
    assert inat2exif._collect_schema_template_fields(catalog_docs, {"oneOf": [{"type": "string"}, {"type": "integer"}]}) == []


@pytest.mark.parametrize("schema,kind", [
    ({"enum": ["a"]}, "enum"), ({"properties": {}}, "object"), ({"items": {}}, "array"),
    ({"allOf": []}, "mixed"), ({}, "unknown"),
])
def test_schema_type_inference(schema, kind):
    assert inat2exif._schema_kind(schema) == kind


@pytest.mark.parametrize("document,message", [
    ({}, "components"), ({"components": {}}, "components.schemas"),
    ({"components": {"schemas": {}}}, "Observation"),
])
def test_invalid_schema_documents_report_the_missing_structure(document, message):
    with pytest.raises(KeyError, match=message):
        inat2exif.build_template_field_catalog_from_schema(document)


@pytest.mark.parametrize("ref,error", [
    ("https://other.example/schema", ValueError),
    ("#/missing", KeyError), ("#/scalar", ValueError),
])
def test_invalid_openapi_refs_fail_explicitly(ref, error):
    with pytest.raises(error):
        inat2exif._resolve_openapi_ref({"scalar": 42}, ref)


def test_sample_catalog_retains_nested_values_and_truncates_long_previews():
    entries = inat2exif.build_template_field_catalog_from_context({
        "taxon": {"name": "Bee", 99: "not a field"},
        "comments": [{"body": "x" * 100}, None], "empty": None,
    })
    fields = {entry["path"]: entry for entry in entries}
    assert fields["taxon"]["kind"] == "object"
    assert fields["taxon.name"]["sample"] == "Bee"
    assert fields["comments"]["sample"] == "list[2]"
    assert fields["comments[0].body"]["sample"] == "x" * 77 + "..."
    assert fields["comments[1]"]["sample"] == ""
    assert fields["empty"]["sample"] == ""
    assert all("99" not in path for path in fields)


def test_docs_cache_fetches_once_and_refreshes_after_expiration(monkeypatch, catalog_docs):
    monkeypatch.setattr(inat2exif, "_api_docs_cache", {})
    calls = []
    now = [100.0]
    monkeypatch.setattr(inat2exif.time, "time", lambda: now[0])
    def get(url, timeout):
        calls.append((url, timeout))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: catalog_docs)
    monkeypatch.setattr(inat2exif.requests, "get", get)
    assert inat2exif.load_inat_api_docs() is catalog_docs
    assert inat2exif.load_inat_api_docs() is catalog_docs
    assert len(calls) == 1
    now[0] += inat2exif.CACHE_TTL_S + 1
    assert inat2exif.load_inat_api_docs() is catalog_docs
    assert len(calls) == 2
    assert calls[0] == (inat2exif.INAT_API_DOCS_URL, inat2exif.INAT_API_DOCS_TIMEOUT_S)


def test_schema_endpoint_uses_schema_or_explicit_sample_fallback(monkeypatch, catalog_docs):
    monkeypatch.setattr(inat2exif, "load_inat_api_docs", lambda: catalog_docs)
    client = TestClient(inat2exif.app)
    result = client.get("/api/template-fields").json()
    assert result["source"] == "schema"
    assert any(entry["path"] == "user.login" for entry in result["fields"])
    def offline():
        raise requests.ConnectionError("Offline")
    monkeypatch.setattr(inat2exif, "load_inat_api_docs", offline)
    fields, source = inat2exif.build_template_field_catalog()
    assert source == "sample-fallback"
    assert any(entry["path"] == "name" for entry in fields)


def test_nonobject_api_docs_are_rejected_not_cached(monkeypatch):
    monkeypatch.setattr(inat2exif, "_api_docs_cache", {})
    monkeypatch.setattr(inat2exif.requests, "get", lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: [],
    ))
    with pytest.raises(ValueError, match="did not return an object"):
        inat2exif.load_inat_api_docs()
    assert not inat2exif._api_docs_cache
