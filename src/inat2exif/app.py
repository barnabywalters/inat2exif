import datetime as dt
import gc
import json
import logging
import math
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import pyexiv2
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment
from pydantic import BaseModel, ConfigDict, field_validator
from inat2exif import indexer as indexer_logic
from inat2exif import matcher as matcher_logic
from inat2exif import utils as shared_utils
from inat2exif import __version__, paths
from inat2exif import caches as cache_admin
from inat2exif.caches import CV_SCORE_CACHE_FILENAME, PHOTO_METADATA_CACHE_FILENAME
from inat2exif.cv_score_cache import CVScoreCache
from inat2exif.photo_cache import PhotoMetadataCache
from inat2exif.template_samples import DEMO_OBSERVATION, preview_observations
from inat2exif.inat_auth import INatAuth, AuthenticationError, with_private_location
from inat2exif.oauth_credentials import CredentialStorageError, credential_store, default_credentials_path

inat_auth = INatAuth()
OAUTH_CREDENTIALS_PATH = default_credentials_path()

CACHE_TTL_S = 60 * 60 * 24
OBS_PER_PAGE = 100
CV_SCORE_THRESHOLD = 0.28
CV_MARGIN_THRESHOLD = 0.08
INAT_API_DOCS_URL = "https://api.inaturalist.org/v2/api-docs"
INAT_API_DOCS_TIMEOUT_S = 20

# XMP Namespace Configuration
# NOTE: Custom namespaces (inat:observationID) cannot be reliably written with pyexiv2.
# See: https://github.com/pyexiv2/pyexiv2/issues/...
# Instead, we store observation IDs using standard DWC fields:
#   - Xmp.dwc.catalogNumber = observation ID  (unique identifier)
#   - Xmp.dwc.datasetName = "iNaturalist observations"  (dataset marker)
# This provides a robust fallback that works across different pyexiv2 versions.
# The metadata_refresh_needed() function recognizes this pattern when reading.

XMP_DWC_NAMESPACE = "http://rs.tdwg.org/dwc/index.htm/"
XMP_DWC_PREFIX = "dwc"
XMP_DCTERMS_NAMESPACE = "http://purl.org/dc/terms/"
XMP_DCTERMS_PREFIX = "dcterms"
XMP_DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
XMP_DC_PREFIX = "dc"
XMP_LR_NAMESPACE = "http://ns.adobe.com/lightroom/1.0/"
XMP_LR_PREFIX = "lr"
XMP_DIGIKAM_NAMESPACE = "http://www.digikam.org/ns/1.0/"
XMP_DIGIKAM_PREFIX = "digiKam"
XMP_DWC_DATASET_NAME_KEY = "Xmp.dwc.datasetName"
XMP_DWC_CATALOG_NUMBER_KEY = "Xmp.dwc.catalogNumber"
XMP_DWC_EXPECTED_DATASET_NAME = "iNaturalist observations"

# Exhaustive set of every XMP key that build_observation_metadata_xmp() may write.
# Used by remove_observation_metadata() to safely strip only our fields
# (for tag fields, only the taxonomy tags within them).
OUR_XMP_KEYS: frozenset[str] = frozenset({
	"Xmp.dwc.occurrenceID",
	"Xmp.dwc.occurrenceDetails",
	"Xmp.dwc.catalogNumber",
	"Xmp.dwc.collectionCode",
	"Xmp.dwc.institutionCode",
	"Xmp.dwc.datasetName",
	"Xmp.dwc.basisOfRecord",
	"Xmp.dwc.eventDate",
	"Xmp.dwc.decimalLatitude",
	"Xmp.dwc.decimalLongitude",
	"Xmp.dwc.recordedBy",
	"Xmp.dwc.identifiedBy",
	"Xmp.dwc.verbatimLocality",
	"Xmp.dwc.countryCode",
	"Xmp.dwc.stateProvince",
	"Xmp.dwc.inaturalistLogin",
	"Xmp.dwc.establishmentMeans",
	"Xmp.dwc.captive",
	"Xmp.dwc.taxonID",
	"Xmp.dwc.scientificName",
	"Xmp.dwc.taxonRank",
	"Xmp.dwc.vernacularName",
	"Xmp.dcterms.references",
	"Xmp.dwc.kingdom",
	"Xmp.dwc.phylum",
	"Xmp.dwc.class",
	"Xmp.dwc.order",
	"Xmp.dwc.family",
	"Xmp.dwc.genus",
	"Xmp.dwc.specificEpithet",
	"Xmp.lr.hierarchicalSubject",
	"Xmp.dc.subject",
	"Xmp.digiKam.TagsList",
	"Xmp.dc.title",
	"Xmp.dc.description",
})

BASE_DIR = paths.RESOURCE_DIR
STATIC_DIR = BASE_DIR / "web"
SAMPLE_OBSERVATION_PATH = BASE_DIR / "obs_excerpt.json"
DEFAULT_CACHE_DIR = paths.cache_dir()

_API_DOCS_CACHE_LOCK = threading.Lock()
_api_docs_cache: dict[str, object] = {
	"fetched_at": 0.0,
	"document": None,
}

VERSION_PATTERN = re.compile(r"^(.*)_v(\d+)$")
LOGGER = logging.getLogger("inat2exif")

TAG_XMP_KEYS: tuple[str, ...] = (
	"Xmp.lr.hierarchicalSubject",
	"Xmp.dc.subject",
	"Xmp.digiKam.TagsList",
)

TEMPLATE_TEXT_XMP_KEYS: tuple[str, ...] = (
	"Xmp.dc.title",
	"Xmp.dc.description",
)

TAG_MANIFEST_KEY = "Xmp.dwc.dynamicProperties"
# Standard DWC storage works on pyexiv2 builds that cannot write custom namespaces.
TAG_MANIFEST_PREFIX = "inat2exif:managedTags="
METADATA_CATEGORIES: dict[str, frozenset[str]] = {
	"write_dates": frozenset({"Xmp.dwc.eventDate"}),
	"write_people": frozenset({"Xmp.dwc.recordedBy", "Xmp.dwc.identifiedBy", "Xmp.dwc.inaturalistLogin"}),
	"write_taxonomy": frozenset({
		"Xmp.dwc.taxonID", "Xmp.dwc.scientificName", "Xmp.dwc.taxonRank", "Xmp.dwc.vernacularName",
		"Xmp.dwc.kingdom", "Xmp.dwc.phylum", "Xmp.dwc.class", "Xmp.dwc.order",
		"Xmp.dwc.family", "Xmp.dwc.genus", "Xmp.dwc.specificEpithet",
		"Xmp.dwc.establishmentMeans", "Xmp.dwc.captive",
	}),
	"write_locality": frozenset({
		"Xmp.dwc.decimalLatitude", "Xmp.dwc.decimalLongitude", "Xmp.dwc.verbatimLocality",
		"Xmp.dwc.countryCode", "Xmp.dwc.stateProvince",
	}),
}

TEMPLATE_ENV = Environment(autoescape=False)


def _format_tag_value_for_log(value) -> str:
	if isinstance(value, list):
		return "[" + ", ".join(str(item) for item in value) + "]"
	return str(value)


def parse_version_info(path: Path) -> tuple[str, int]:
	return shared_utils.parse_version_info(path)


def get_version_group_key(path: Path) -> str:
	return shared_utils.get_version_group_key(path)


@dataclass(slots=True)
class LocalPhoto:
	id: int
	path: Path
	exif_datetime_raw: str
	timestamp: float
	minute_timestamp: float
	existing_observation_id: int | None
	metadata_refresh_needed: bool = False
	metadata_mismatches: list[str] = field(default_factory=list)
	version_group: str = ""
	version_number: int = 0
	version_siblings: list[int] = field(default_factory=list)
	# XMP read during indexing; reused by the metadata refresh check to avoid re-opening the file.
	embedded_xmp: dict[str, object] | None = field(default=None, repr=False)


@dataclass(slots=True)
class ObservationData:
	obs_id: int
	observed_at: dt.datetime
	photos: list[dict]
	uri: str
	metadata_xmp: dict[str, object] = field(default_factory=dict)
	template_context: dict[str, object] = field(default_factory=dict)
	has_sounds: bool = False


@dataclass(slots=True)
class MatchResult:
	observation: ObservationData
	status: str
	candidate_photo_ids: list[int]
	scored_candidates: list[tuple[int, float]]
	has_existing_tag: bool = False
	selected_photo_ids: set[int] = field(default_factory=set)
	# Human-readable explanation of how the match was found, e.g. a time offset.
	detail: str = ""


class StartSessionRequest(BaseModel):
	username: str = ""
	path: str = ""


class SelectionRequest(BaseModel):
	selected_photo_ids: list[int]


class LibrarySearchAcceptRequest(BaseModel):
	photo_id: int


class TemplatePreviewRequest(BaseModel):
	title_template: str = ""
	caption_template: str = ""
	username: str = ""
	sample_obs_id: int | None = None


class AppConfig(BaseModel):
	"""User preferences and configuration for inat2exif.

	Uses extra="allow" so that future fields (e.g. metadata templates) can be
	added without requiring a schema migration.
	"""
	model_config = ConfigDict(extra="allow")
	username: str = ""
	path: str = ""
	ignore_hidden_files: bool = True
	write_hierarchical_tags: bool = True
	taxonomy_tag_prefix: str = "taxonomy"
	write_dates: bool = True
	write_people: bool = True
	write_taxonomy: bool = True
	write_locality: bool = True
	write_gps: bool = True
	write_title: bool = True
	write_description: bool = True
	image_title_template: str = ""
	image_caption_template: str = ""

	@field_validator("taxonomy_tag_prefix")
	@classmethod
	def validate_tag_prefix(cls, value: str) -> str:
		value = value.strip()
		if any(character in value for character in "|/,\r\n"):
			raise ValueError("Use a single tag name without |, /, commas or line breaks")
		return value


# Module-level path to the config file. Override in tests via monkeypatch.
CONFIG_PATH: Path = paths.config_dir() / "config.json"
DEFAULT_CONFIG_PATH: Path = BASE_DIR / "config_defaults.json"


def load_config_defaults() -> dict:
	"""Load default config values from DEFAULT_CONFIG_PATH."""
	try:
		raw = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
		if isinstance(raw, dict):
			return raw
	except (FileNotFoundError, json.JSONDecodeError, ValueError):
		pass
	return {
		"username": "",
		"path": "",
		"ignore_hidden_files": True,
		"write_hierarchical_tags": True,
		"image_title_template": "",
		"image_caption_template": "",
	}


def load_config() -> AppConfig:
	"""Load config from CONFIG_PATH, returning defaults if the file is missing or invalid."""
	defaults = load_config_defaults()
	try:
		raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
		if not isinstance(raw, dict):
			raw = {}
		return AppConfig.model_validate({**defaults, **raw})
	except FileNotFoundError:
		return AppConfig.model_validate(defaults)
	except (json.JSONDecodeError, ValueError) as exc:
		LOGGER.warning("%s could not be read; using defaults: %s", CONFIG_PATH, exc)
		return AppConfig.model_validate(defaults)


def save_config(config: AppConfig) -> None:
	"""Atomically write config to CONFIG_PATH as formatted JSON."""
	CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
	temporary = CONFIG_PATH.with_name(f".{CONFIG_PATH.name}.tmp")
	try:
		temporary.write_text(config.model_dump_json(indent=2), encoding="utf-8")
		os.replace(temporary, CONFIG_PATH)
	finally:
		temporary.unlink(missing_ok=True)


class AnalysisCancelled(Exception):
	pass


def parse_observation_datetime(value: str | None) -> dt.datetime | None:
	return shared_utils.parse_observation_datetime(value)


def parse_observation_id(value) -> int | None:
	return shared_utils.parse_observation_id(value)


def ensure_namespaces_registered() -> None:
	# Register standard XMP namespaces (DWC, DCTERMS, DC, LR, digiKam) with pyexiv2.
	# Custom inat namespace is not registered - we use DWC fallback instead.
	if not hasattr(pyexiv2, "registerNs"):
		return
	for namespace, prefix in [
		(XMP_DWC_NAMESPACE, XMP_DWC_PREFIX),
		(XMP_DCTERMS_NAMESPACE, XMP_DCTERMS_PREFIX),
		(XMP_DC_NAMESPACE, XMP_DC_PREFIX),
		(XMP_LR_NAMESPACE, XMP_LR_PREFIX),
		(XMP_DIGIKAM_NAMESPACE, XMP_DIGIKAM_PREFIX),
	]:
		try:
			pyexiv2.registerNs(prefix, namespace)
		except Exception as exc:
			LOGGER.debug("XMP namespace registration failed for prefix %s: %s", prefix, exc)


def _merge_hierarchical_tags(existing: str | list | None, new: list[str] | str | None) -> list | None:
	"""Merge new hierarchical tags with existing ones.
	
	Lightroom hierarchical subjects can include pipe-delimited hierarchies (e.g.,
	"taxonomy|Animalia|Arthropoda") mixed with flat tags. Existing values may be
	either strings or lists (as returned by pyexiv2 for XMP array fields).
	We replace any existing taxonomy hierarchies (tags starting with "taxonomy")
	with the new ones, while preserving other unrelated tags.
	"""
	if new is None:
		return existing
	
	# Normalise new to a list
	new_tags = new if isinstance(new, list) else [new]
	
	if existing is None:
		return new_tags
	
	# Convert existing to list if it's a string
	if isinstance(existing, str):
		existing_tags = [tag.strip() for tag in existing.split(",") if tag.strip()]
	elif isinstance(existing, list):
		existing_tags = [str(tag).strip() for tag in existing if tag]
	else:
		return new_tags
	
	# Filter out any existing taxonomy tags (they start with "taxonomy")
	# and keep all other tags
	non_taxonomy_tags = [tag for tag in existing_tags if not tag.startswith("taxonomy")]
	
	# Add the new taxonomy tags
	return non_taxonomy_tags + new_tags


def _merge_subject_tags(existing: str | list | None, new: list[str] | str | None) -> list | None:
	"""Merge new subject tags with existing ones.
	
	Subject tags are typically stored as comma-separated strings or XMP array lists.
	For digikam, our hierarchies use slash delimiters (e.g., "taxonomy/Animalia/Arthropoda").
	We replace any existing taxonomy hierarchies with the new ones, while preserving
	other unrelated tags.
	"""
	if new is None:
		return existing
	
	# Normalise new to a list
	new_tags = new if isinstance(new, list) else [new]
	
	if existing is None:
		return new_tags
	
	# Convert existing to list if it's a string
	if isinstance(existing, str):
		existing_tags = [tag.strip() for tag in existing.split(",") if tag.strip()]
	elif isinstance(existing, list):
		existing_tags = [str(tag).strip() for tag in existing if tag]
	else:
		return new_tags
	
	# Filter out any existing taxonomy tags (they start with "taxonomy")
	# and keep all other tags. Note: we match the bare root "taxonomy" too so
	# old-format photos with repeated bare "taxonomy" entries don't accumulate.
	non_taxonomy_tags = [tag for tag in existing_tags if not tag.startswith("taxonomy")]
	
	# Add the new taxonomy tags
	return non_taxonomy_tags + new_tags


def _tag_values(value: object) -> list[str]:
	if isinstance(value, list):
		return [str(tag).strip() for tag in value if str(tag).strip()]
	if isinstance(value, str):
		return [tag.strip() for tag in value.split(",") if tag.strip()]
	if value is None:
		return []
	raise ValueError("Unexpected taxonomy keyword value; expected text or a list")


def _tag_manifest(xmp: dict[str, object]) -> dict[str, list[str]]:
	value = xmp.get(TAG_MANIFEST_KEY, "")
	text = str(value[0]) if isinstance(value, list) and value else str(value)
	for line in text.splitlines():
		if line.startswith(TAG_MANIFEST_PREFIX):
			try:
				manifest = json.loads(line[len(TAG_MANIFEST_PREFIX):])
			except json.JSONDecodeError as exc:
				raise ValueError("Invalid inat2exif taxonomy tag tracking metadata") from exc
			if not isinstance(manifest, dict) or any(
				key not in TAG_XMP_KEYS or not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags)
				for key, tags in manifest.items()
			):
				raise ValueError("Invalid inat2exif taxonomy tag tracking metadata")
			return manifest
	return {}


def _managed_tags(xmp: dict[str, object], key: str) -> list[str]:
	manifest = _tag_manifest(xmp)
	if key in manifest:
		return [tag for tag in _existing_tag_values(xmp, key) if tag in manifest[key]]
	value = xmp.get(key)
	if value is None and key != "Xmp.lr.hierarchicalSubject":
		value = xmp.get("Xmp.Bag.subject")
	return _filter_taxonomy_tags(value, _tag_delimiter(key))


def _existing_tag_values(xmp: dict[str, object], key: str) -> list[str]:
	value = xmp.get(key)
	if value is None and key != "Xmp.lr.hierarchicalSubject":
		value = xmp.get("Xmp.Bag.subject")
	if isinstance(value, str) and not _tag_manifest(xmp):
		managed = _filter_taxonomy_tags(value, _tag_delimiter(key))
		if managed and "|taxonomy" in value:
			# Older readers flattened arrays with pipes; keep the user-tag portion.
			user_part = value[:value.index("|taxonomy")]
			return [user_part] + managed
	return _tag_values(value)


def _manifest_text(xmp: dict[str, object], manifest: dict[str, list[str]]) -> str:
	value = xmp.get(TAG_MANIFEST_KEY, "")
	text = str(value[0]) if isinstance(value, list) and value else str(value or "")
	lines = [line for line in text.splitlines() if not line.startswith(TAG_MANIFEST_PREFIX)]
	if manifest:
		lines.append(TAG_MANIFEST_PREFIX + json.dumps(manifest, sort_keys=True))
	return "\n".join(lines)


def _tag_write_payload(xmp: dict[str, object], payload: dict[str, object]) -> dict[str, object]:
	result = dict(payload)
	if not any(key in payload for key in TAG_XMP_KEYS):
		return result
	manifest = _tag_manifest(xmp)
	for key in TAG_XMP_KEYS:
		if key not in payload:
			continue
		managed = _managed_tags(xmp, key)
		user_tags = [tag for tag in _existing_tag_values(xmp, key) if tag not in managed]
		new_tags = _tag_values(payload[key])
		owned = [tag for tag in new_tags if tag not in user_tags]
		result[key] = user_tags + owned
		manifest[key] = owned
	if any(key in payload for key in TAG_XMP_KEYS):
		result[TAG_MANIFEST_KEY] = _manifest_text(xmp, manifest)
	return result


def _write_xmp_payload(image, xmp_payload: dict[str, object]) -> None:
	# Read existing XMP first to enable merging
	xmp_data = {}
	write_payload = {key: value for key, value in xmp_payload.items() if not key.startswith("_")}
	if hasattr(image, "read_xmp"):
		xmp_data = image.read_xmp() or {}

	if LOGGER.isEnabledFor(logging.DEBUG):
		LOGGER.debug(
			"_write_xmp_payload start: existing_tag_values=%s incoming_tag_values=%s",
			{key: _format_tag_value_for_log(xmp_data.get(key)) for key in TAG_XMP_KEYS if key in xmp_data},
			{key: _format_tag_value_for_log(xmp_payload.get(key)) for key in TAG_XMP_KEYS if key in xmp_payload},
		)
	
	write_payload = _tag_write_payload(xmp_data, write_payload)
	for key in TAG_XMP_KEYS:
		if key in write_payload and not write_payload[key]:
			write_payload[key] = ""

	for key in TEMPLATE_TEXT_XMP_KEYS:
		if key not in write_payload:
			continue
		value = str(write_payload[key]).strip()
		if value:
			write_payload[key] = value
		else:
			write_payload[key] = ""
	
	# Write back using available API
	if hasattr(image, "modify_xmp"):
		if LOGGER.isEnabledFor(logging.DEBUG):
			LOGGER.debug(
				"Writing XMP via modify_xmp with tag_values=%s",
				{key: _format_tag_value_for_log(write_payload.get(key)) for key in TAG_XMP_KEYS if key in write_payload},
			)
		image.modify_xmp(write_payload)
	elif hasattr(image, "write_xmp"):
		# write_xmp performs full replacement of XMP data. To avoid metadata loss,
		# refuse writes unless incremental modify_xmp is available.
		raise RuntimeError("Unsafe XMP writer (write_xmp-only): refusing to modify metadata to avoid metadata loss")
	else:
		raise RuntimeError("No writable XMP API found in pyexiv2")



def parse_observation_id_from_xmp(xmp_data: dict) -> int | None:
	"""Derive the iNaturalist observation ID from an already-read XMP dict.

	Checks for the standard Darwin Core catalogNumber + datasetName pattern
	used when writing metadata via pyexiv2 (custom namespace fallback).
	"""
	lower_keys = {str(k).lower(): v for k, v in (xmp_data or {}).items()}
	dataset_value = lower_keys.get(XMP_DWC_DATASET_NAME_KEY.lower())
	catalog_value = lower_keys.get(XMP_DWC_CATALOG_NUMBER_KEY.lower())
	if dataset_value is None or catalog_value is None:
		return None
	if str(dataset_value).strip().casefold() != XMP_DWC_EXPECTED_DATASET_NAME.casefold():
		return None
	return parse_observation_id(catalog_value)


def read_existing_observation_id(path: Path) -> int | None:
	"""Read existing observation ID from photo metadata.
	Falls back to standard Darwin Core fields (catalogNumber + datasetName) for
	compatibility with photos saved via pyexiv2's custom namespace limitation."""
	ensure_namespaces_registered()
	image = pyexiv2.Image(str(path))
	try:
		if hasattr(image, "read_xmp"):
			xmp_data = image.read_xmp()
			# Fallback for runtimes where custom inat namespace cannot be written:
			# detect previously-tagged photos from standard Darwin Core fields.
			lower_keys = {str(key).lower(): value for key, value in (xmp_data or {}).items()}
			dataset_value = lower_keys.get(XMP_DWC_DATASET_NAME_KEY.lower())
			catalog_value = lower_keys.get(XMP_DWC_CATALOG_NUMBER_KEY.lower())
			if dataset_value is None or catalog_value is None:
				return None

			dataset_text = str(dataset_value).strip().casefold()
			if dataset_text != XMP_DWC_EXPECTED_DATASET_NAME.casefold():
				return None

			return parse_observation_id(catalog_value)
	finally:
		close_fn = getattr(image, "close", None)
		if callable(close_fn):
			close_fn()
	return None


def read_embedded_xmp(path: Path) -> dict[str, object]:
	ensure_namespaces_registered()
	image = pyexiv2.Image(str(path))
	try:
		if hasattr(image, "read_xmp"):
			raw_xmp = image.read_xmp() or {}
			cleaned: dict[str, object] = {}
			for key, value in raw_xmp.items():
				if value is None:
					continue
				if isinstance(value, list):
					if not value:
						continue
					key_text = str(key)
					if key_text in TAG_XMP_KEYS:
						# Preserve full arrays for managed taxonomy fields so compare logic
						# can see taxonomy entries even when custom tags appear first.
						# TODO: If we add more list-valued managed fields, move read/write/
						# compare normalization to a shared canonicalization helper.
						cleaned[key_text] = [str(item).strip() for item in value if str(item).strip()]
					else:
						cleaned[key_text] = str(value[0])
					continue
				# Handle dict values (lang-alt fields like Xmp.dwc.vernacularName)
				# These come back as {'lang="x-default"': value} from pyexiv2
				if isinstance(value, dict):
					if not value:
						continue
					# Try to extract the "x-default" language value, or fall back to first value
					default_val = value.get("x-default") or next(iter(value.values()), None)
					if default_val is not None:
						cleaned[str(key)] = str(default_val)
					continue
				cleaned[str(key)] = str(value)
			return cleaned
		return {}
	finally:
		close_fn = getattr(image, "close", None)
		if callable(close_fn):
			close_fn()


def metadata_refresh_needed(path: Path, observation: ObservationData) -> bool:
	"""Check if metadata needs refresh. For detailed info, use metadata_refresh_needed_with_details()."""
	_needed, _ = metadata_refresh_needed_with_details(path, observation)
	return _needed


def _filter_taxonomy_tags(tags: object, delimiter: str = "|") -> list[str]:
	"""Extract only taxonomy-prefixed tags from a tag field.
	
	Handles multiple formats:
	- List of complete paths: ["taxonomy|Animalia|Insecta"]
	- String with items separated by |, paths delimited by /: "custom/tag|taxonomy/Animalia|insect|taxonomy/Animalia/Insecta"
	- String with mixed separators and paths: "custom|taxonomy|Animalia|bird|taxonomy|Animalia|Insecta"
	
	Args:
		tags: Tag value as string, list of paths, or None
		delimiter: Character used within hierarchical paths ("|" or "/")
	
	Returns:
		List of complete taxonomy tags/paths starting with "taxonomy"
	"""
	if not isinstance(tags, (str, list)):
		return []
	
	if isinstance(tags, list):
		# For lists, just filter items starting with "taxonomy"
		return [str(t).strip() for t in tags if str(t).strip() == "taxonomy" or str(t).strip().startswith(f"taxonomy{delimiter}")]
	
	# Handle string input
	items = [t.strip() for t in tags.split("|") if t.strip()] if "|" in tags else [tags]
	result = []
	i = 0
	
	while i < len(items):
		item = items[i]
		
		# Check if this item is a complete taxonomy path (starts with taxonomy and contains delimiter)
		if item.startswith("taxonomy") and (delimiter in item or item == "taxonomy"):
			# This could be a complete path, or the start of one that's split across items
			if item == "taxonomy":
				# Word "taxonomy" alone - collect following capitalized words
				group = [item]
				i += 1
				while i < len(items) and items[i] and items[i][0].isupper():
					group.append(items[i])
					i += 1
				result.append(delimiter.join(group))
				continue
			elif delimiter in item:
				# Already a complete path with internal delimiter
				result.append(item)
				i += 1
			else:
				# Starts with "taxonomy" but no delimiter - shouldn't happen, skip
				i += 1
		else:
			i += 1
	
	return result


def build_observation_template_context(obs: dict, obs_id: int, uri: str, observed: dt.datetime) -> dict[str, object]:
	"""Build the template context used for title and caption rendering."""
	# Shallow copy: only top-level keys are added, and templates never mutate nested data.
	context = dict(obs)
	taxon = obs.get("taxon") if isinstance(obs.get("taxon"), dict) else {}
	user = obs.get("user") if isinstance(obs.get("user"), dict) else {}
	context.setdefault("taxon", taxon)
	context.setdefault("user", user)
	context["obs_id"] = obs_id
	context["uri"] = uri
	context["observed_at"] = observed.isoformat()
	context["name"] = _first_nonempty(taxon.get("name"), obs.get("species_guess"))
	context["common_name"] = taxon.get("preferred_common_name")
	context["username"] = _first_nonempty(user.get("login"), user.get("name"))
	return context


@lru_cache(maxsize=32)
def _compile_observation_template(template: str):
	return TEMPLATE_ENV.from_string(template)


def _render_observation_template(template: str, context: dict[str, object]) -> str:
	"""Render a config-defined Jinja template against an observation context."""
	rendered = _compile_observation_template(template).render(**context)
	return str(rendered).strip()


def load_sample_observation() -> dict:
	"""Load a representative observation for template editing previews."""
	try:
		raw = json.loads(SAMPLE_OBSERVATION_PATH.read_text(encoding="utf-8"))
	except (FileNotFoundError, json.JSONDecodeError, ValueError):
		return deepcopy(DEMO_OBSERVATION)
	if isinstance(raw, list) and raw and isinstance(raw[0], dict):
		return raw[0]
	if isinstance(raw, dict) and raw:
		return raw
	return deepcopy(DEMO_OBSERVATION)


def build_sample_template_context() -> dict[str, object]:
	"""Build template context from the sample observation file."""
	obs = load_sample_observation()
	if not obs:
		return {}
	observed = parse_observation_datetime(obs.get("time_observed_at")) or dt.datetime.now(dt.UTC)
	obs_id = int(obs.get("id") or 0)
	uri = obs.get("uri") or f"https://www.inaturalist.org/observations/{obs_id}"
	return build_observation_template_context(obs, obs_id=obs_id, uri=uri, observed=observed)


def _flatten_template_fields(value, prefix: str = "") -> list[dict[str, object]]:
	entries: list[dict[str, object]] = []
	if isinstance(value, dict):
		if prefix:
			entries.append({"path": prefix, "kind": "object", "sample": "object", "source": "sample"})
		for key, nested in value.items():
			if not isinstance(key, str):
				continue
			next_prefix = f"{prefix}.{key}" if prefix else key
			entries.extend(_flatten_template_fields(nested, next_prefix))
		return entries
	if isinstance(value, list):
		if prefix:
			entries.append({"path": prefix, "kind": "array", "sample": f"list[{len(value)}]", "source": "sample"})
		for index, nested in enumerate(value):
			next_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
			entries.extend(_flatten_template_fields(nested, next_prefix))
		return entries
	kind = type(value).__name__
	sample = "" if value is None else str(value)
	if len(sample) > 80:
		sample = sample[:77] + "..."
	entries.append({"path": prefix, "kind": kind, "sample": sample, "source": "sample"})
	return entries


def load_inat_api_docs() -> dict[str, object]:
	"""Load the iNaturalist OpenAPI document with a simple in-memory cache."""
	with _API_DOCS_CACHE_LOCK:
		cached_document = _api_docs_cache.get("document")
		cached_at = float(_api_docs_cache.get("fetched_at") or 0.0)
		if isinstance(cached_document, dict) and (time.time() - cached_at) < CACHE_TTL_S:
			return cached_document

	response = requests.get(INAT_API_DOCS_URL, timeout=INAT_API_DOCS_TIMEOUT_S)
	response.raise_for_status()
	data = response.json()
	if not isinstance(data, dict):
		raise ValueError("iNaturalist API docs did not return an object")

	with _API_DOCS_CACHE_LOCK:
		_api_docs_cache["document"] = data
		_api_docs_cache["fetched_at"] = time.time()
	return data


def _resolve_openapi_ref(api_docs: dict[str, object], ref: str) -> dict[str, object]:
	if not ref.startswith("#/"):
		raise ValueError(f"Unsupported OpenAPI ref: {ref}")
	current: object = api_docs
	for segment in ref[2:].split("/"):
		if not isinstance(current, dict) or segment not in current:
			raise KeyError(f"OpenAPI ref not found: {ref}")
		current = current[segment]
	if not isinstance(current, dict):
		raise ValueError(f"OpenAPI ref does not resolve to an object: {ref}")
	return current


def _merge_schema_metadata(schema: dict[str, object], resolved: dict[str, object]) -> dict[str, object]:
	merged = dict(resolved)
	for key, value in schema.items():
		if key == "$ref":
			continue
		merged[key] = value
	return merged


def _schema_kind(schema: dict[str, object]) -> str:
	schema_type = schema.get("type")
	if isinstance(schema_type, str):
		return schema_type
	if "enum" in schema:
		return "enum"
	if "properties" in schema:
		return "object"
	if "items" in schema:
		return "array"
	if "anyOf" in schema or "oneOf" in schema or "allOf" in schema:
		return "mixed"
	return "unknown"


def _schema_entry(path: str, schema: dict[str, object], *, source: str = "schema") -> dict[str, object]:
	entry: dict[str, object] = {
		"path": path,
		"kind": _schema_kind(schema),
		"source": source,
	}
	description = schema.get("description")
	if isinstance(description, str) and description.strip():
		entry["description"] = " ".join(description.split())
	enum_values = schema.get("enum")
	if isinstance(enum_values, list) and enum_values:
		entry["enum"] = enum_values
	if schema.get("nullable") is True:
		entry["nullable"] = True
	format_value = schema.get("format")
	if isinstance(format_value, str) and format_value:
		entry["format"] = format_value
	return entry


def _collect_schema_template_fields(
	api_docs: dict[str, object],
	schema: dict[str, object],
	path: str = "",
	ref_stack: tuple[str, ...] = (),
) -> list[dict[str, object]]:
	if "$ref" in schema:
		ref = schema["$ref"]
		if not isinstance(ref, str):
			return []
		if ref in ref_stack:
			return [_schema_entry(path, {"type": "object"})] if path else []
		resolved = _resolve_openapi_ref(api_docs, ref)
		return _collect_schema_template_fields(api_docs, _merge_schema_metadata(schema, resolved), path, ref_stack + (ref,))

	if "allOf" in schema and isinstance(schema["allOf"], list):
		entries: list[dict[str, object]] = []
		for subschema in schema["allOf"]:
			if isinstance(subschema, dict):
				entries.extend(_collect_schema_template_fields(api_docs, subschema, path, ref_stack))
		return entries

	variant_key = "anyOf" if isinstance(schema.get("anyOf"), list) else "oneOf" if isinstance(schema.get("oneOf"), list) else None
	if variant_key:
		variants = schema.get(variant_key)
		assert isinstance(variants, list)
		non_null_variants = []
		nullable = bool(schema.get("nullable"))
		for variant in variants:
			if not isinstance(variant, dict):
				continue
			if variant.get("type") == "null":
				nullable = True
				continue
			non_null_variants.append(variant)
		if len(non_null_variants) == 1:
			merged_variant = dict(non_null_variants[0])
			if nullable:
				merged_variant["nullable"] = True
			return _collect_schema_template_fields(api_docs, merged_variant, path, ref_stack)
		return [_schema_entry(path, {"type": "mixed", "nullable": nullable, "description": schema.get("description")})] if path else []

	entries: list[dict[str, object]] = []
	properties = schema.get("properties")
	if isinstance(properties, dict):
		if path:
			entries.append(_schema_entry(path, schema))
		for key, subschema in properties.items():
			if not isinstance(key, str) or not isinstance(subschema, dict):
				continue
			next_path = f"{path}.{key}" if path else key
			entries.extend(_collect_schema_template_fields(api_docs, subschema, next_path, ref_stack))
		return entries

	items = schema.get("items")
	if isinstance(items, dict):
		if path:
			entries.append(_schema_entry(path, schema))
			entries.extend(_collect_schema_template_fields(api_docs, items, f"{path}[]", ref_stack))
		return entries

	if path:
		entries.append(_schema_entry(path, schema))
	return entries


def _template_alias_entries() -> list[dict[str, object]]:
	return [
		{
			"path": "obs_id",
			"kind": "integer",
			"description": "Alias for the observation id used by template rendering.",
			"source": "alias",
		},
		{
			"path": "observed_at",
			"kind": "string",
			"format": "date-time",
			"description": "Alias for the observed timestamp in ISO 8601 form.",
			"source": "alias",
		},
		{
			"path": "name",
			"kind": "string",
			"description": "Alias for the taxon scientific name, falling back to species_guess.",
			"source": "alias",
		},
		{
			"path": "common_name",
			"kind": "string",
			"description": "Alias for taxon.preferred_common_name.",
			"source": "alias",
		},
		{
			"path": "username",
			"kind": "string",
			"description": "Alias for user.login, falling back to user.name.",
			"source": "alias",
		},
	]


def build_template_field_catalog_from_context(context: dict[str, object]) -> list[dict[str, object]]:
	seen: dict[str, dict[str, object]] = {}
	for entry in _flatten_template_fields(context):
		path = entry.get("path", "")
		if not isinstance(path, str) or not path:
			continue
		seen[path] = entry
	return [seen[key] for key in sorted(seen)]


def build_template_field_catalog_from_schema(api_docs: dict[str, object]) -> list[dict[str, object]]:
	components = api_docs.get("components")
	if not isinstance(components, dict):
		raise KeyError("OpenAPI document is missing components")
	schemas = components.get("schemas")
	if not isinstance(schemas, dict):
		raise KeyError("OpenAPI document is missing components.schemas")
	observation_schema = schemas.get("Observation")
	if not isinstance(observation_schema, dict):
		raise KeyError("OpenAPI document is missing components.schemas.Observation")

	seen: dict[str, dict[str, object]] = {}
	for entry in _collect_schema_template_fields(api_docs, observation_schema):
		path = entry.get("path", "")
		if not isinstance(path, str) or not path:
			continue
		seen[path] = entry
	for entry in _template_alias_entries():
		path = entry["path"]
		seen[str(path)] = entry
	return [seen[key] for key in sorted(seen)]


def build_template_field_catalog() -> tuple[list[dict[str, object]], str]:
	"""Return template fields from the OpenAPI schema, falling back to sample data."""
	try:
		api_docs = load_inat_api_docs()
		return build_template_field_catalog_from_schema(api_docs), "schema"
	except (requests.RequestException, KeyError, ValueError) as exc:
		LOGGER.warning("template field catalog falling back to sample data: %s", exc)
		return build_template_field_catalog_from_context(build_sample_template_context()), "sample-fallback"


def _effective_metadata_xmp(observation: ObservationData, config: AppConfig | None = None) -> dict[str, object]:
	"""Build the metadata payload to write or compare for an observation."""
	if config is None:
		config = load_config()
	payload = dict(observation.metadata_xmp)
	template_context = observation.template_context or build_observation_template_context(
		{},
		obs_id=observation.obs_id,
		uri=observation.uri,
		observed=observation.observed_at,
	)
	if not config.write_hierarchical_tags:
		for key in TAG_XMP_KEYS:
			payload.pop(key, None)
	else:
		prefix = config.taxonomy_tag_prefix.strip()
		for key in TAG_XMP_KEYS:
			if key not in payload:
				continue
			delimiter = _tag_delimiter(key)
			tags = []
			for tag in _tag_values(payload[key]):
				parts = tag.split(delimiter)
				if parts[0] == "taxonomy":
					parts = parts[1:]
				if prefix:
					parts.insert(0, prefix)
				if parts:
					tags.append(delimiter.join(parts))
			payload[key] = tags

	if not config.write_gps:
		payload["_gps_enabled"] = False
	elif not config.write_locality and "Xmp.dwc.decimalLatitude" in payload and "Xmp.dwc.decimalLongitude" in payload:
		payload["_gps_coordinates"] = (
			payload.get("Xmp.dwc.decimalLatitude"), payload.get("Xmp.dwc.decimalLongitude"),
		)
	for setting, keys in METADATA_CATEGORIES.items():
		if not getattr(config, setting):
			for key in keys:
				payload.pop(key, None)

	title_template = config.image_title_template.strip()
	caption_template = config.image_caption_template.strip()
	if config.write_title:
		payload["Xmp.dc.title"] = _render_observation_template(title_template, template_context) if title_template else ""
	if config.write_description:
		payload["Xmp.dc.description"] = _render_observation_template(caption_template, template_context) if caption_template else ""
	return payload


def _tag_delimiter(key: str) -> str:
	return "|" if key == "Xmp.lr.hierarchicalSubject" else "/"


def _multiset_difference(left: list[str], right: list[str]) -> list[str]:
	"""Items of ``left`` not matched by an item in ``right``, keeping ``left``'s order."""
	remaining = Counter(right)
	result = []
	for item in left:
		if remaining[item] > 0:
			remaining[item] -= 1
		else:
			result.append(item)
	return result


def compute_metadata_diff(embedded: dict[str, object], expected: dict[str, object]) -> list[dict[str, object]]:
	"""Structured differences between a file's XMP and the payload we would write.

	Uses the same rules as the refresh check: tag fields only compare the
	taxonomy tags we manage, and an empty title/caption matches a missing one.
	Tag entries carry ``added``/``removed`` lists; value entries carry
	``old`` (``None`` when missing) and ``new``. Keys starting with ``_`` are
	internal (used to format the legacy mismatch strings).
	"""
	diff: list[dict[str, object]] = []
	for key, value in expected.items():
		if key.startswith("_"):
			continue
		embedded_value = embedded.get(key)

		if key in TAG_XMP_KEYS:
			delimiter = _tag_delimiter(key)
			merged = _tag_write_payload(embedded, {key: value})
			expected_tags = _tag_values(merged[key])
			embedded_tags = _existing_tag_values(embedded, key)
			if sorted(expected_tags) != sorted(embedded_tags):
				diff.append({
					"key": key,
					"kind": "tags",
					"delimiter": delimiter,
					"added": _multiset_difference(expected_tags, embedded_tags),
					"removed": _multiset_difference(embedded_tags, expected_tags),
					"_expected": expected_tags,
					"_current": embedded_tags,
				})
			continue

		expected_str = str(value).strip()
		if embedded_value is None:
			if key in TEMPLATE_TEXT_XMP_KEYS and not expected_str:
				continue
			diff.append({"key": key, "kind": "value", "old": None, "new": expected_str})
			continue
		embedded_str = str(embedded_value).strip()
		if embedded_str != expected_str:
			diff.append({"key": key, "kind": "value", "old": embedded_str, "new": expected_str})
	return diff


def _has_our_dataset_marker(xmp: dict[str, object]) -> bool:
	lower_keys = {str(k).lower(): v for k, v in xmp.items()}
	dataset_value = lower_keys.get(XMP_DWC_DATASET_NAME_KEY.lower())
	return dataset_value is not None and str(dataset_value).strip().casefold() == XMP_DWC_EXPECTED_DATASET_NAME.casefold()


def _split_taxonomy_tags(value: object) -> tuple[list[str], list[str]]:
	"""Split a tag field into ``(user_tags, taxonomy_tags)``.

	Uses the same "starts with taxonomy" rule as the merge on write, so removal
	strips exactly the tags we manage and keeps everything else.
	"""
	if isinstance(value, list):
		tags = [str(tag).strip() for tag in value]
	elif isinstance(value, str):
		tags = [tag.strip() for tag in value.split(",")]
	else:
		return [], []
	tags = [tag for tag in tags if tag]
	return [tag for tag in tags if not tag.startswith("taxonomy")], [tag for tag in tags if tag.startswith("taxonomy")]


def _removal_changes(xmp: dict[str, object]) -> dict[str, str | list[str] | None] | None:
	"""The ``modify_xmp`` payload that strips our metadata, or ``None`` if the file isn't ours.

	Plain fields are deleted (``None``); tag fields keep their non-taxonomy tags.
	"""
	if not _has_our_dataset_marker(xmp):
		return None
	changes: dict[str, str | list[str] | None] = {}
	for key in sorted(OUR_XMP_KEYS):
		if key not in xmp:
			continue
		if key in TAG_XMP_KEYS:
			taxonomy_tags = _managed_tags(xmp, key)
			user_tags = [tag for tag in _existing_tag_values(xmp, key) if tag not in taxonomy_tags]
			if not taxonomy_tags:
				continue
			changes[key] = user_tags or None
			continue
		changes[key] = None
	if _tag_manifest(xmp):
		changes[TAG_MANIFEST_KEY] = _manifest_text(xmp, {}) or None
	return changes


def compute_removal_diff(embedded: dict[str, object]) -> list[dict[str, object]] | None:
	"""What remove_observation_metadata() would remove, or ``None`` if it would skip the file."""
	changes = _removal_changes(embedded)
	if changes is None:
		return None
	diff: list[dict[str, object]] = []
	for key in changes:
		value = embedded.get(key)
		if key in TAG_XMP_KEYS:
			taxonomy_tags = _managed_tags(embedded, key)
			diff.append({"key": key, "kind": "tags", "delimiter": _tag_delimiter(key), "added": [], "removed": taxonomy_tags})
			continue
		old = "" if value is None else str(value).strip()
		if old:
			diff.append({"key": key, "kind": "value", "old": old, "new": changes[key]})
	return diff


def _format_metadata_diff_entry(entry: dict[str, object]) -> str:
	key = entry["key"]
	if entry["kind"] == "tags":
		return f"{key}: expected {repr(entry['_expected'])}, got {repr(entry['_current'])}"
	if entry["old"] is None:
		return f"MISSING: {key}"
	return f"{key}: expected {repr(entry['new'])}, got {repr(entry['old'])}"


def _public_metadata_diff(diff: list[dict[str, object]]) -> list[dict[str, object]]:
	return [{k: v for k, v in entry.items() if not k.startswith("_")} for entry in diff]


def _missing_gps_payload(
	embedded_xmp: dict[str, object],
	embedded_exif: dict[str, object],
	expected: dict[str, object],
) -> dict[str, str]:
	"""Add a complete GPS pair only when neither metadata format has coordinates."""
	if expected.get("_gps_enabled") is False:
		return {}
	for metadata, prefix in ((embedded_exif, "Exif.GPSInfo."), (embedded_xmp, "Xmp.exif.")):
		for suffix in ("GPSLatitude", "GPSLongitude", "GPSLatitudeRef", "GPSLongitudeRef"):
			value = metadata.get(prefix + suffix)
			if value is not None and str(value).strip():
				return {}

	latitude = expected.get("Xmp.dwc.decimalLatitude")
	longitude = expected.get("Xmp.dwc.decimalLongitude")
	coordinates = expected.get("_gps_coordinates")
	if isinstance(coordinates, tuple) and len(coordinates) == 2:
		latitude, longitude = coordinates
	if latitude is None or longitude is None:
		return {}
	if not isinstance(latitude, (str, int, float)) or not isinstance(longitude, (str, int, float)):
		raise ValueError("Invalid iNaturalist GPS coordinates")
	try:
		lat, lon = float(latitude), float(longitude)
	except (TypeError, ValueError) as exc:
		raise ValueError("Invalid iNaturalist GPS coordinates") from exc
	if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
		raise ValueError(f"Invalid iNaturalist GPS coordinates: {latitude}, {longitude}")

	def dms(value: float) -> str:
		# Round before splitting so seconds cannot round up to an invalid 60.
		degrees, remainder = divmod(round(abs(value) * 3_600_000_000), 3_600_000_000)
		minutes, seconds = divmod(remainder, 60_000_000)
		return f"{degrees}/1 {minutes}/1 {seconds}/1000000"

	return {
		"Exif.GPSInfo.GPSVersionID": "2 3 0 0",
		"Exif.GPSInfo.GPSLatitude": dms(lat),
		"Exif.GPSInfo.GPSLatitudeRef": "S" if lat < 0 else "N",
		"Exif.GPSInfo.GPSLongitude": dms(lon),
		"Exif.GPSInfo.GPSLongitudeRef": "W" if lon < 0 else "E",
	}


def _metadata_diff_for_photo(
	path: Path,
	embedded: dict[str, object],
	expected: dict[str, object],
) -> list[dict[str, object]]:
	diff = compute_metadata_diff(embedded, expected)
	if expected.get("_gps_enabled") is False or (
		("Xmp.dwc.decimalLatitude" not in expected or "Xmp.dwc.decimalLongitude" not in expected)
		and "_gps_coordinates" not in expected
	):
		return diff
	image = pyexiv2.Image(str(path))
	try:
		embedded_exif = image.read_exif()
		gps_payload = _missing_gps_payload(embedded, embedded_exif, expected)
	finally:
		image.close()
	diff.extend(compute_metadata_diff(embedded_exif, dict(gps_payload)))
	return diff


def metadata_refresh_needed_with_details(
	path: Path,
	observation: ObservationData,
	*,
	embedded: dict[str, object] | None = None,
	config: AppConfig | None = None,
) -> tuple[bool, list[str]]:
	"""Check if metadata needs refresh and return detailed mismatch info.
	
	For tag fields (hierarchicalSubject, dc.subject, digiKam.TagsList),
	only compares taxonomy-prefixed tags we manage, ignoring pre-existing user tags.
	For other XMP fields, does a full comparison. GPS is added only when absent.

	Pass ``embedded`` (XMP already read for this file) and ``config`` to skip
	re-reading them when checking many photos in a row.
	"""
	if embedded is None:
		embedded = read_embedded_xmp(path)
	expected = _effective_metadata_xmp(observation, config)

	mismatches = [_format_metadata_diff_entry(entry) for entry in _metadata_diff_for_photo(path, embedded, expected)]

	if mismatches:
		LOGGER.debug(
			"Metadata refresh needed for %s (obs_id=%s): %s",
			path.name,
			observation.obs_id,
			"; ".join(mismatches)
		)
		return True, mismatches
	return False, []


def _get_nested(mapping: dict | None, *keys: str):
	value = mapping
	for key in keys:
		if not isinstance(value, dict):
			return None
		value = value.get(key)
	return value


def _first_nonempty(*values):
	for value in values:
		if value is None:
			continue
		if isinstance(value, str):
			value = value.strip()
			if not value:
				continue
		return value
	return None


def _location_parts(obs: dict) -> tuple[float | None, float | None]:
	geojson_coordinates = _get_nested(obs, "geojson", "coordinates")
	if isinstance(geojson_coordinates, list) and len(geojson_coordinates) >= 2:
		longitude = geojson_coordinates[0]
		latitude = geojson_coordinates[1]
		if isinstance(longitude, (int, float)) and isinstance(latitude, (int, float)):
			return float(latitude), float(longitude)

	location = obs.get("location")
	if isinstance(location, str) and "," in location:
		lat_text, lon_text = [part.strip() for part in location.split(",", 1)]
		try:
			return float(lat_text), float(lon_text)
		except ValueError:
			pass

	return None, None


def build_hierarchical_tags(obs: dict) -> tuple[list[str] | None, list[str] | None]:
	"""Build hierarchical tags from taxon ancestor data.
	
	Replaces the "life" taxon (48460) with "taxonomy" namespace, then uses
	ancestor names to create a hierarchical tag structure.
	
	Returns all intermediate hierarchy levels from most specific to least specific,
	so that DigiKam can search images at any level in the taxonomy hierarchy.
	
	Falls back to identifications if ancestors aren't available in the main
	observation taxon.
	
	Returns:
		Tuple of (lightroom_hierarchical_subjects, digikam_tags_list)
		- lightroom_hierarchical_subjects: list of pipe-delimited hierarchy strings for Adobe Lightroom
		- digikam_tags_list: list of slash-delimited hierarchy strings for digikam
	"""
	taxon = obs.get("taxon")
	if not isinstance(taxon, dict):
		return None, None
	
	ancestor_ids = taxon.get("ancestor_ids")
	ancestors = taxon.get("ancestors")
	
	# Fall back to identifications if ancestors not in main taxon
	if (not ancestors or not isinstance(ancestors, list)) and obs.get("identifications"):
		identifications = obs.get("identifications", [])
		if identifications and isinstance(identifications[0], dict):
			ident_taxon = identifications[0].get("taxon")
			if isinstance(ident_taxon, dict):
				ancestors = ident_taxon.get("ancestors")
	
	if not ancestor_ids or not isinstance(ancestors, list):
		return None, None
	
	# Create a map of taxon ID to ancestor data
	ancestors_by_id = {ancestor["id"]: ancestor for ancestor in ancestors if isinstance(ancestor, dict)}
	
	# Build the tag hierarchy, replacing 48460 with "taxonomy"
	tags = []
	for taxon_id in ancestor_ids:
		if taxon_id == 48460:
			# Replace "life" with "taxonomy" namespace
			tags.append("taxonomy")
		elif taxon_id in ancestors_by_id:
			name = ancestors_by_id[taxon_id].get("name")
			if name:
				tags.append(name)

	# Append the current leaf taxon name at the end of the hierarchy.
	leaf_name = taxon.get("name")
	if isinstance(leaf_name, str):
		leaf_name = leaf_name.strip()
	if leaf_name and (not tags or tags[-1] != leaf_name):
		tags.append(leaf_name)
	
	if not tags:
		return None, None
	
	# Build all intermediate hierarchy levels (from most specific to least specific).
	# We include all levels (including the root "taxonomy" tag) so that images are
	# understood as belonging to each ancestor level in the hierarchy.
	lightroom_subjects: list[str] = []
	digikam_tags_list: list[str] = []
	for i in range(len(tags), 0, -1):
		segment = tags[:i]
		lightroom_subjects.append("|".join(segment))
		digikam_tags_list.append("/".join(segment))
	
	if not lightroom_subjects:
		return None, None
	
	return lightroom_subjects, digikam_tags_list


def build_observation_metadata_xmp(obs: dict, obs_id: int, uri: str, observed: dt.datetime) -> dict[str, object]:
	# NOTE: This currently writes only scalar XMP tags that map cleanly to pyexiv2's
	# dict-based XMP API. It is intentionally a subset of naturtag's output.
	#
	# Reference sample (naturtag full output):
	# https://raw.githubusercontent.com/pyinat/naturtag/refs/heads/main/assets/demo_images/48849031626_af2065ab64_k.xmp
	#
	# Missing items compared to that file include:
	# 1) RDF collection fields (for example lr:hierarchicalSubject, dc:subject,
	#    dc:creator, dcterms:modified, dcterms:references Bag/Seq/Alt variants).
	# 2) Rich taxonomy keyword trees (both canonical and common-name hierarchies).
	# 3) Additional Darwin Core fields from identifications/place data when present
	#    (for example identificationID/dateIdentified and fuller locality fields).
	#
	# How to add this safely:
	# 1) Build an internal metadata model that separates scalar values from list/alt
	#    values (Seq/Bag/Alt) instead of flattening everything to dict[str, str].
	# 2) Serialize complex values as proper RDF structures (or use a library that
	#    supports writing list-valued XMP nodes), then merge with existing XMP.
	# 3) Derive taxonomy hierarchy from taxon + ancestors list by rank order;
	#    do not assume ancestors is a dict keyed by rank.
	# 4) Add golden-file tests that compare generated XMP fragments against expected
	#    naturtag-compatible structures for representative observations.
	# Build metadata dict. Note: We store observation ID via catalogNumber + datasetName
	# (Darwin Core standard) rather than custom namespace, for broad compatibility.
	metadata: dict[str, object] = {
		"Xmp.dwc.occurrenceID": uri,
		"Xmp.dwc.occurrenceDetails": uri,
		"Xmp.dwc.catalogNumber": str(obs_id),
		"Xmp.dwc.collectionCode": "Observations",
		"Xmp.dwc.institutionCode": "iNaturalist",
		"Xmp.dwc.datasetName": "iNaturalist observations",
		"Xmp.dwc.basisOfRecord": "HumanObservation",
		"Xmp.dwc.eventDate": observed.isoformat(),
	}

	taxon = obs.get("taxon") if isinstance(obs.get("taxon"), dict) else {}
	user = obs.get("user") if isinstance(obs.get("user"), dict) else {}
	place_guess = _first_nonempty(obs.get("place_guess"), obs.get("location_name"))
	latitude, longitude = _location_parts(obs)

	if latitude is not None:
		metadata["Xmp.dwc.decimalLatitude"] = str(latitude)
	if longitude is not None:
		metadata["Xmp.dwc.decimalLongitude"] = str(longitude)

	for key, value in {
		"Xmp.dwc.recordedBy": _first_nonempty(user.get("name"), user.get("login")),
		"Xmp.dwc.identifiedBy": _first_nonempty(obs.get("identifications_most_agree"), user.get("name"), user.get("login")),
		"Xmp.dwc.verbatimLocality": place_guess,
		"Xmp.dwc.countryCode": _get_nested(obs.get("place") if isinstance(obs.get("place"), dict) else {}, "country_code"),
		"Xmp.dwc.stateProvince": _get_nested(obs.get("place"), "admin_level_1"),
		"Xmp.dwc.inaturalistLogin": user.get("login"),
		"Xmp.dwc.establishmentMeans": "captive" if obs.get("captive") else "wild",
		"Xmp.dwc.captive": "captive" if obs.get("captive") else "wild",
		"Xmp.dwc.taxonID": _first_nonempty(taxon.get("id"), obs.get("taxon_id")),
		"Xmp.dwc.scientificName": taxon.get("name"),
		"Xmp.dwc.taxonRank": taxon.get("rank"),
		"Xmp.dwc.vernacularName": taxon.get("preferred_common_name"),
		"Xmp.dcterms.references": uri,
	}.items():
		if value is None:
			continue
		metadata[key] = str(value)

	for rank_key, xmp_key in {
		"kingdom": "Xmp.dwc.kingdom",
		"phylum": "Xmp.dwc.phylum",
		"class": "Xmp.dwc.class",
		"order": "Xmp.dwc.order",
		"family": "Xmp.dwc.family",
		"genus": "Xmp.dwc.genus",
		"species": "Xmp.dwc.specificEpithet",
	}.items():
		taxon_value = _get_nested(taxon, "ancestors", rank_key)
		if taxon_value:
			metadata[xmp_key] = str(taxon_value)

	# Add hierarchical tags for Adobe Lightroom and digikam
	lightroom_subjects, digikam_tags_list = build_hierarchical_tags(obs)
	if lightroom_subjects is not None:
		metadata["Xmp.lr.hierarchicalSubject"] = lightroom_subjects
	if digikam_tags_list is not None:
		metadata["Xmp.dc.subject"] = digikam_tags_list
		metadata["Xmp.digiKam.TagsList"] = digikam_tags_list

	return metadata


def write_observation_metadata(path: Path, observation: ObservationData) -> None:
	"""Write observation metadata to photo file.
	Uses Darwin Core XMP fields and adds EXIF GPS only when no GPS is present."""
	ensure_namespaces_registered()
	xmp_payload = _effective_metadata_xmp(observation)
	if LOGGER.isEnabledFor(logging.DEBUG):
		LOGGER.debug(
			"write_observation_metadata: path=%s obs_id=%s outgoing_tag_values=%s",
			path,
			observation.obs_id,
			{key: _format_tag_value_for_log(xmp_payload.get(key)) for key in TAG_XMP_KEYS if key in xmp_payload},
		)
	image = pyexiv2.Image(str(path))
	try:
		gps_payload = {}
		if xmp_payload.get("_gps_enabled") is not False and (
			("Xmp.dwc.decimalLatitude" in xmp_payload and "Xmp.dwc.decimalLongitude" in xmp_payload)
			or "_gps_coordinates" in xmp_payload
		):
			gps_payload = _missing_gps_payload(image.read_xmp() or {}, image.read_exif(), xmp_payload)
			if gps_payload and not hasattr(image, "modify_exif"):
				raise RuntimeError("No incremental EXIF writer found in pyexiv2")
		_write_xmp_payload(image, xmp_payload)
		if gps_payload:
			image.modify_exif(gps_payload)
	finally:
		close_fn = getattr(image, "close", None)
		if callable(close_fn):
			close_fn()


def remove_observation_metadata(path: Path) -> bool:
	"""Remove iNaturalist observation metadata from a photo file.
	Only removes our specific XMP fields (and only the taxonomy tags within tag
	fields), and only when the file has our datasetName marker (confirming we
	wrote it). Leaves all other metadata, including the user's own tags,
	untouched. Returns True if fields were removed, False if skipped."""
	ensure_namespaces_registered()
	image = pyexiv2.Image(str(path))
	try:
		if not hasattr(image, "read_xmp") or not hasattr(image, "modify_xmp"):
			return False
		xmp_data = image.read_xmp() or {}
		changes = _removal_changes(xmp_data)
		if changes is None:
			return False
		if changes:
			# pyexiv2 deletes a key when its value is None (an empty string would leave an empty field behind).
			image.modify_xmp(changes)
		return True
	finally:
		close_fn = getattr(image, "close", None)
		if callable(close_fn):
			close_fn()


def fetch_observations(
	username: str,
	cache_dir: Path,
	per_page: int = OBS_PER_PAGE,
	progress_cb=None,
	cancel_check=None,
) -> list[dict]:
	headers = inat_auth.headers(username)
	return matcher_logic.fetch_observations(
		username,
		cache_dir,
		requests_module=requests,
		time_module=time,
		cache_ttl_s=CACHE_TTL_S,
		per_page=per_page,
		progress_cb=progress_cb,
		cancel_check=cancel_check,
		request_headers=headers,
		use_cache=headers is None,
	)


def observation_privacy_report(observations: list[dict], *, authenticated: bool) -> dict:
	report = {
		"total": len(observations), "authenticated": authenticated,
		"restricted_locations": 0, "obscured_locations": 0, "private_locations": 0,
		"missing_coordinates": 0, "missing_datetime": 0, "skipped_photo_observations": 0,
		"restricted_missing_datetime": 0, "private_locations_available": 0,
	}
	for obs in observations:
		privacy = {obs.get("geoprivacy"), obs.get("taxon_geoprivacy")}
		restricted = bool(obs.get("obscured") or privacy & {"obscured", "private"})
		report["restricted_locations"] += int(restricted)
		report["obscured_locations"] += int("private" not in privacy and restricted)
		report["private_locations"] += int("private" in privacy)
		report["private_locations_available"] += int(bool(authenticated and obs.get("private_location")))
		lat, lon = _location_parts(with_private_location(obs) if authenticated else obs)
		report["missing_coordinates"] += int(
			lat is None or lon is None or not math.isfinite(lat) or not math.isfinite(lon)
			or not -90 <= lat <= 90 or not -180 <= lon <= 180
		)
		if parse_observation_datetime(obs.get("time_observed_at")) is None:
			report["missing_datetime"] += 1
			report["skipped_photo_observations"] += int(bool(obs.get("photos")))
			report["restricted_missing_datetime"] += int(restricted)
	return report


def selected_preview_observations(username: str) -> list[dict]:
	with inat_auth.lock:
		headers = inat_auth.headers(username)
		if headers is None:
			return preview_observations(username)
		cached = inat_auth.samples
		if cached is None or time.monotonic() - cached[0] > 900:
			samples = [
				{**entry, "observation": with_private_location(entry["observation"])}
				for entry in preview_observations(username, request_headers=headers)
			]
			cached = (time.monotonic(), samples)
			inat_auth.samples = cached
		return deepcopy(cached[1])


def observation_photo_url(photo: dict, size: str = "medium") -> str | None:
	return matcher_logic.observation_photo_url(photo, size=size)


def cache_observation_photo(photo: dict, cache_dir: Path) -> Path:
	return matcher_logic.cache_observation_photo(photo, cache_dir, requests_module=requests)


def extract_exif_datetime(path: Path) -> str | None:
	return indexer_logic.extract_exif_datetime(path, pyexiv2)


def index_local_photos(
	root_path: Path,
	progress_cb=None,
	cancel_check=None,
	group_versions: bool = True,
	ignore_hidden_files: bool = True,
	photo_metadata_cache: PhotoMetadataCache | None = None,
) -> tuple[list[LocalPhoto], dict[float, list[LocalPhoto]], dict[float, list[LocalPhoto]], dict[int, LocalPhoto]]:
	return indexer_logic.index_local_photos(
		root_path,
		local_photo_cls=LocalPhoto,
		read_xmp_fn=read_embedded_xmp,
		parse_observation_id_from_xmp_fn=parse_observation_id_from_xmp,
		extract_exif_datetime_fn=extract_exif_datetime,
		logger=LOGGER,
		progress_cb=progress_cb,
		cancel_check=cancel_check,
		group_versions=group_versions,
		ignore_hidden_files=ignore_hidden_files,
		photo_metadata_cache=photo_metadata_cache,
	)


CV_IMAGE_CACHE_SIZE = 48


@lru_cache(maxsize=CV_IMAGE_CACHE_SIZE)
def _load_cv_image_cached(path_str: str, _mtime_ns: int, _size: int, max_edge: int):
	return matcher_logic.load_cv_image(Path(path_str), cv2_module=cv2, max_edge=max_edge)


def clear_cv_image_cache() -> None:
	_load_cv_image_cached.cache_clear()


def load_cv_image(path: Path, max_edge: int = 1024):
	"""Load and downscale an image for CV scoring, reusing recent decodes of unchanged files."""
	try:
		stat = os.stat(path)
	except OSError:
		return matcher_logic.load_cv_image(path, cv2_module=cv2, max_edge=max_edge)
	return _load_cv_image_cached(str(path), stat.st_mtime_ns, stat.st_size, max_edge)


def orb_similarity(a, b) -> float:
	return matcher_logic.orb_similarity(a, b, cv2_module=cv2)


def histogram_similarity(a, b) -> float:
	return matcher_logic.histogram_similarity(a, b, cv2_module=cv2)


def edge_overlap_similarity(a, b) -> float:
	return matcher_logic.edge_overlap_similarity(a, b, cv2_module=cv2)


def pair_similarity_score(obs_img, local_img) -> float:
	return matcher_logic.pair_similarity_score(obs_img, local_img, cv2_module=cv2)


# Bump CV_SCORE_ALGORITHM_REVISION whenever image loading or pair scoring changes, so
# cached pair scores computed by the old algorithm are discarded.
CV_SCORE_ALGORITHM_REVISION = 2
CV_SCORE_MAX_EDGE = 1024


def cv_score_algorithm_version() -> str:
	return f"r{CV_SCORE_ALGORITHM_REVISION}:edge{CV_SCORE_MAX_EDGE}:cv2-{getattr(cv2, '__version__', 'unknown')}"


def score_candidates(
	observation_photo_paths: list[Path],
	candidates: list[LocalPhoto],
	score_cache: CVScoreCache | None = None,
) -> list[tuple[int, float]]:
	return matcher_logic.score_candidates(
		observation_photo_paths,
		candidates,
		load_cv_image_fn=lambda path: load_cv_image(path, max_edge=CV_SCORE_MAX_EDGE),
		pair_similarity_score_fn=pair_similarity_score,
		score_cache=score_cache,
	)


def score_candidates_per_photo(
	observation_photo_paths: list[Path],
	candidates: list[LocalPhoto],
	score_cache: CVScoreCache | None = None,
) -> list[list[tuple[int, float]]]:
	return matcher_logic.score_candidates_per_photo(
		observation_photo_paths,
		candidates,
		load_cv_image_fn=lambda path: load_cv_image(path, max_edge=CV_SCORE_MAX_EDGE),
		pair_similarity_score_fn=pair_similarity_score,
		score_cache=score_cache,
	)


def score_candidate_batches(
	jobs: list[tuple[list[Path], list[LocalPhoto]]],
	score_cache: CVScoreCache | None = None,
	**kwargs,
) -> list[list[list[tuple[int, float]]]]:
	return matcher_logic.score_candidate_batches(
		jobs,
		load_cv_image_fn=lambda path: load_cv_image(path, max_edge=CV_SCORE_MAX_EDGE),
		pair_similarity_score_fn=pair_similarity_score,
		score_cache=score_cache,
		**kwargs,
	)


def build_observation_records(raw_observations: list[dict]) -> tuple[list[ObservationData], dict[int, dict]]:
	return matcher_logic.build_observation_records(
		raw_observations,
		parse_observation_datetime_fn=parse_observation_datetime,
		build_observation_metadata_xmp_fn=build_observation_metadata_xmp,
		build_observation_template_context_fn=build_observation_template_context,
		observation_data_cls=ObservationData,
	)


def observation_visual_state(match: MatchResult) -> str:
	if match.has_existing_tag:
		return "tagged"
	if match.status in matcher_logic.PHOTOLESS_STATUSES and not match.selected_photo_ids:
		return "audio-only" if match.status == matcher_logic.AUDIO_ONLY_STATUS else "no-photos"
	if not match.selected_photo_ids:
		return "ambiguous"
	if match.status in {"auto-exact", "auto-minute"}:
		return "exact-untagged"
	if match.selected_photo_ids:
		return "fuzzy"
	return "ambiguous"


def needs_manual_attention(match: MatchResult) -> bool:
	return observation_visual_state(match) == "ambiguous"


TREE_STATUS_ORDER = ["actionable", "unmatchable", "photoless", "fuzzy", "exact", "tagged"]


def tree_status_key(match: MatchResult) -> str:
	state = observation_visual_state(match)
	if state in {"audio-only", "no-photos"}:
		return "photoless"
	if not match.candidate_photo_ids:
		return "unmatchable"
	if not match.selected_photo_ids and not match.has_existing_tag:
		return "actionable"
	if state == "ambiguous":
		return "actionable"
	if state == "exact-untagged":
		return "exact"
	return state


def empty_tree_status_counts() -> dict[str, int]:
	return {status: 0 for status in TREE_STATUS_ORDER}


def tree_status_summary(match: MatchResult) -> dict[str, object]:
	status_key = tree_status_key(match)
	status_counts = empty_tree_status_counts()
	status_counts[status_key] = 1
	matchable = bool(match.candidate_photo_ids) and status_key != "photoless"
	return {
		"observation_count": 1,
		"matchable_count": 1 if matchable else 0,
		"unmatchable_count": 0 if matchable else 1,
		"actionable_count": 1 if status_key == "actionable" else 0,
		"status_counts": status_counts,
	}


def merge_tree_status_summary(target: dict[str, object], source: dict[str, object]) -> None:
	target["observation_count"] += int(source["observation_count"])
	target["matchable_count"] += int(source["matchable_count"])
	target["unmatchable_count"] += int(source["unmatchable_count"])
	target["actionable_count"] += int(source["actionable_count"])
	for status in TREE_STATUS_ORDER:
		target["status_counts"][status] += int(source["status_counts"][status])


def expand_with_version_siblings(selected_ids: set[int], by_id: dict[int, LocalPhoto]) -> set[int]:
	expanded: set[int] = set()
	for photo_id in selected_ids:
		photo = by_id.get(photo_id)
		if photo and photo.version_siblings:
			expanded.update(photo.version_siblings)
		else:
			expanded.add(photo_id)
	return expanded


def best_cv_scores_by_group(match: MatchResult, by_id: dict[int, LocalPhoto]) -> list[tuple[str, float, int]]:
	return matcher_logic.best_cv_scores_by_group(match, by_id)


def enforce_unique_selected_groups(matches: list[MatchResult], by_id: dict[int, LocalPhoto]) -> None:
	matcher_logic.enforce_unique_selected_groups(matches, by_id)


LIBRARY_SEARCH_TOP_N = 12
# Below this a whole-library candidate is noise (unrelated photos typically score 0.15-0.27).
LIBRARY_SEARCH_MIN_SCORE = 0.2
LIBRARY_SEARCH_BATCH = 64


def _idle_library_search_status() -> dict:
	return {
		"obs_id": None,
		"running": False,
		"finished": False,
		"cancelled": False,
		"error": None,
		"phase": "idle",
		"message": "",
		"current": 0,
		"total": 0,
		"candidates": [],
	}


class LibrarySearchCancelled(Exception):
	pass


class MatcherState:
	def __init__(self) -> None:
		self.lock = threading.RLock()
		self.ready = False
		self.username = ""
		self.root_path = Path(".")
		self.cache_dir = DEFAULT_CACHE_DIR
		self.local_photos_by_id: dict[int, LocalPhoto] = {}
		self.obs_photo_by_id: dict[int, dict] = {}
		self.matches: list[MatchResult] = []
		self.matches_by_obs_id: dict[int, MatchResult] = {}
		self.refresh_dirty_photo_ids: set[int] = set()
		self.analysis_thread: threading.Thread | None = None
		self.analysis_cancel_event = threading.Event()
		self.group_versions: bool = True
		self.privacy_report: dict | None = None
		self.analysis_status = {
			"running": False,
			"phase": "idle",
			"message": "Idle",
			"current": 0,
			"total": 0,
			"finished": False,
			"cancelled": False,
			"error": None,
			"events": [],
		}
		self.library_search_thread: threading.Thread | None = None
		self.library_search_cancel_event = threading.Event()
		self.library_search_status: dict = _idle_library_search_status()

	def _append_event(self, phase: str, message: str) -> None:
		events = self.analysis_status["events"]
		events.append({"time": dt.datetime.now().isoformat(timespec="seconds"), "phase": phase, "message": message})
		if len(events) > 200:
			del events[: len(events) - 200]

	def _log_event(self, phase: str, message: str, current: int | None = None, total: int | None = None) -> None:
		"""Record an event in the analysis log without changing the visible progress bar."""
		with self.lock:
			self._append_event(phase, message)

	def _set_progress(self, phase: str, message: str, current: int | None = None, total: int | None = None) -> None:
		with self.lock:
			self.analysis_status["phase"] = phase
			self.analysis_status["message"] = message
			if current is not None:
				self.analysis_status["current"] = current
			if total is not None:
				self.analysis_status["total"] = total
			self._append_event(phase, message)

	def _cancel_check(self) -> None:
		if self.analysis_cancel_event.is_set():
			raise AnalysisCancelled()

	def start_analysis(self, username: str, path: Path, ignore_hidden_files: bool = True) -> dict:
		with self.lock:
			if self.analysis_status["running"]:
				return {"started": False, "running": True}

			# A library search uses the previous analysis' photos, so stop it.
			self.library_search_cancel_event.set()
			self.analysis_cancel_event.clear()
			self.analysis_status = {
				"running": True,
				"phase": "startup",
				"message": "Starting analysis",
				"current": 0,
				"total": 0,
				"finished": False,
				"cancelled": False,
				"error": None,
				"events": [],
			}
			self._append_event("startup", "Starting analysis")

		thread = threading.Thread(target=self._run_analysis_worker, args=(username, path, ignore_hidden_files), daemon=True)
		self.analysis_thread = thread
		thread.start()
		return {"started": True, "running": True}

	def cancel_analysis(self) -> dict:
		with self.lock:
			running = self.analysis_status["running"]
			if not running:
				return {"cancel_requested": False, "running": False}
			self.analysis_cancel_event.set()
			self._append_event("cancel", "Cancellation requested")
			self.analysis_status["message"] = "Cancellation requested"
			return {"cancel_requested": True, "running": True}

	def _photo_owners_locked(self) -> dict[str, set[int]]:
		"""Map each local photo group to the observations that selected or tagged it."""
		owners: dict[str, set[int]] = defaultdict(set)
		for match in self.matches:
			for photo_id in match.selected_photo_ids:
				owners[matcher_logic._group_key(photo_id, self.local_photos_by_id)].add(match.observation.obs_id)
		for photo in self.local_photos_by_id.values():
			if photo.existing_observation_id is not None:
				owners[matcher_logic._group_key(photo.id, self.local_photos_by_id)].add(photo.existing_observation_id)
		return owners

	def start_library_search(self, obs_id: int) -> dict:
		with self.lock:
			self.require_ready()
			if self.analysis_status["running"]:
				raise HTTPException(status_code=409, detail="Wait for the analysis to finish before searching the library")
			match = self.matches_by_obs_id.get(obs_id)
			if match is None:
				raise HTTPException(status_code=404, detail="Observation not found")
			if not match.observation.photos:
				raise HTTPException(status_code=400, detail="This observation has no photos to compare")
			self.library_search_cancel_event.set()
			previous = self.library_search_thread
		if previous is not None and previous.is_alive():
			previous.join(timeout=10)
		with self.lock:
			cancel_event = threading.Event()
			self.library_search_cancel_event = cancel_event
			self.library_search_status = {
				**_idle_library_search_status(),
				"obs_id": obs_id,
				"running": True,
				"phase": "prepare",
				"message": "Starting library search",
			}
			# Photos another observation already uses are just clutter here, so they aren't searched at all.
			claimed_groups = {group for group, owner_ids in self._photo_owners_locked().items() if owner_ids - {obs_id}}
			photos = [
				photo
				for photo in self.local_photos_by_id.values()
				if matcher_logic._group_key(photo.id, self.local_photos_by_id) not in claimed_groups
			]
			observation = match.observation
		thread = threading.Thread(
			target=self._run_library_search,
			args=(observation, photos, cancel_event),
			name="library-search",
			daemon=True,
		)
		self.library_search_thread = thread
		thread.start()
		return self.library_search_state()

	def library_search_state(self) -> dict:
		with self.lock:
			return deepcopy(self.library_search_status)

	def cancel_library_search(self) -> dict:
		with self.lock:
			self.library_search_cancel_event.set()
			return deepcopy(self.library_search_status)

	def _library_candidate_payload(self, photo: LocalPhoto, score: float, obs_ts: float) -> dict:
		photo_ids = {photo.id}
		if self.group_versions:
			photo_ids.update(photo.version_siblings)
		return {
			"photo_id": photo.id,
			"photo_ids": sorted(photo_ids),
			"name": photo.path.name,
			"path": str(photo.path),
			"url": f"/api/image/local/{photo.id}",
			"score": float(score),
			"taken": photo.exif_datetime_raw,
			"offset": matcher_logic.format_time_offset(photo.timestamp - obs_ts),
		}

	def _run_library_search(self, observation: ObservationData, photos: list[LocalPhoto], cancel_event: threading.Event) -> None:
		obs_id = observation.obs_id
		obs_ts = observation.observed_at.timestamp()

		def check() -> None:
			if cancel_event.is_set():
				raise LibrarySearchCancelled()

		def update(**fields) -> None:
			with self.lock:
				# A newer search replaces the event; this stale worker must not overwrite its status.
				if self.library_search_cancel_event is cancel_event:
					self.library_search_status.update(fields)

		try:
			update(message="Downloading the observation's photos")
			obs_paths: list[Path] = []
			for photo in observation.photos:
				try:
					obs_paths.append(cache_observation_photo({**photo, "observation_id": obs_id}, self.cache_dir))
				except Exception as exc:
					LOGGER.warning("Library search: couldn't fetch photo %s of obs %s: %s", photo.get("id"), obs_id, exc)
			if not obs_paths:
				raise RuntimeError("Couldn't download any of the observation's photos")
			obs_aspects = [
				aspect
				for aspect in (matcher_logic.aspect_ratio(matcher_logic.jpeg_dimensions(path)) for path in obs_paths)
				if aspect
			]

			representatives = matcher_logic.library_search_representatives(photos)
			total = len(representatives)
			update(
				phase="prioritise",
				message=f"Reading the shape of {total} unused local photos to decide which to compare first",
				current=0,
				total=total,
			)
			keyed: list[tuple[float, LocalPhoto]] = []
			with PhotoMetadataCache(self.cache_dir / PHOTO_METADATA_CACHE_FILENAME) as dims_cache:
				known_dims = dims_cache.load_dimensions()
				new_dims: list[tuple] = []

				def _dimensions(photo: LocalPhoto) -> tuple[tuple[int, int] | None, tuple | None]:
					try:
						mtime = os.stat(photo.path).st_mtime
					except OSError:
						return None, None
					cached = known_dims.get(str(photo.path))
					if cached is not None and cached[0] == mtime:
						return ((cached[1], cached[2]) if cached[1] else None), None
					dims = matcher_logic.jpeg_dimensions(photo.path)
					return dims, (photo.path, mtime, *(dims or (None, None)))

				# Header reads are tiny and latency-bound, so many in flight at once helps.
				try:
					with ThreadPoolExecutor(max_workers=16, thread_name_prefix="library-search-shape") as shape_executor:
						for start in range(0, total, 500):
							check()
							chunk = representatives[start : start + 500]
							for photo, (photo_dims, new_row) in zip(chunk, shape_executor.map(_dimensions, chunk)):
								if new_row is not None:
									new_dims.append(new_row)
								local_aspect = matcher_logic.aspect_ratio(photo_dims)
								keyed.append((matcher_logic.library_search_priority(photo.timestamp - obs_ts, local_aspect, obs_aspects), photo))
							update(current=start + len(chunk))
				finally:
					# Keep whatever was read, even if the search was stopped part-way.
					if new_dims:
						dims_cache.put_dimensions(new_dims)
			keyed.sort(key=lambda item: item[0])
			ordered = [photo for _priority, photo in keyed]

			update(phase="compare", message=f"Comparing with {total} unused local photos, most likely first", current=0, total=total)
			top: list[tuple[float, LocalPhoto]] = []
			cv_score_cache = CVScoreCache(self.cache_dir / CV_SCORE_CACHE_FILENAME, cv_score_algorithm_version())
			with cv_score_cache, ThreadPoolExecutor(max_workers=matcher_logic.DEFAULT_CV_MAX_WORKERS, thread_name_prefix="library-search") as executor:
				def _score(photo: LocalPhoto) -> tuple[LocalPhoto, float]:
					if cancel_event.is_set():
						return photo, 0.0
					scored = score_candidates(obs_paths, [photo], score_cache=cv_score_cache)
					return photo, (scored[0][1] if scored else 0.0)

				for start in range(0, total, LIBRARY_SEARCH_BATCH):
					check()
					batch = ordered[start : start + LIBRARY_SEARCH_BATCH]
					for photo, score in executor.map(_score, batch):
						if score >= LIBRARY_SEARCH_MIN_SCORE:
							top.append((score, photo))
					check()
					top.sort(key=lambda item: item[0], reverse=True)
					del top[LIBRARY_SEARCH_TOP_N:]
					done = start + len(batch)
					update(
						current=done,
						message=f"Compared {done} of {total} unused local photos, most likely first",
						candidates=[self._library_candidate_payload(photo, score, obs_ts) for score, photo in top],
					)
			found = len(top)
			update(
				running=False,
				finished=True,
				phase="done",
				message=f"Compared all {total} unused local photos; {found} possible match{'es' if found != 1 else ''}",
			)
		except LibrarySearchCancelled:
			update(running=False, finished=True, cancelled=True, phase="cancelled", message="Search stopped")
		except Exception as exc:
			LOGGER.exception("Library search for obs %s failed", obs_id)
			update(running=False, finished=True, error=str(exc), phase="error", message=f"Search failed: {exc}")

	def accept_library_search_candidate(self, obs_id: int, photo_id: int) -> dict:
		with self.lock:
			self.require_ready()
			match = self.matches_by_obs_id.get(obs_id)
			if match is None:
				raise HTTPException(status_code=404, detail="Observation not found")
			photo = self.local_photos_by_id.get(photo_id)
			if photo is None:
				raise HTTPException(status_code=404, detail="Photo not found")
			photo_ids = {photo_id}
			if self.group_versions:
				photo_ids.update(pid for pid in photo.version_siblings if pid in self.local_photos_by_id)

			# The user picked this photo explicitly, so it moves from any other observation.
			released_from: list[int] = []
			for other in self.matches:
				if other is not match and other.selected_photo_ids & photo_ids:
					other.selected_photo_ids -= photo_ids
					released_from.append(other.observation.obs_id)

			score = 0.0
			if self.library_search_status.get("obs_id") == obs_id:
				for candidate in self.library_search_status.get("candidates", []):
					if candidate["photo_id"] == photo_id:
						score = float(candidate["score"])
				self.library_search_cancel_event.set()
			for pid in sorted(photo_ids):
				if pid not in match.candidate_photo_ids:
					match.candidate_photo_ids.append(pid)
			scores = dict(match.scored_candidates)
			for pid in photo_ids:
				scores[pid] = max(scores.get(pid, 0.0), score)
			match.scored_candidates = sorted(scores.items(), key=lambda item: item[1], reverse=True)
			match.selected_photo_ids = set(match.selected_photo_ids) | photo_ids
			match.status = "manual-search"
			match.detail = "Picked from a whole-library search"
			return {
				"obs_id": obs_id,
				"selected_photo_ids": sorted(match.selected_photo_ids),
				"released_from": released_from,
				**self.summary(),
			}

	def analysis_progress(self) -> dict:
		with self.lock:
			status = deepcopy(self.analysis_status)
			if status["finished"]:
				status["summary"] = self.summary() if self.ready else None
			return status

	def _run_analysis_worker(self, username: str, path: Path, ignore_hidden_files: bool) -> None:
		try:
			self.analyze(username, path, ignore_hidden_files=ignore_hidden_files)
			with self.lock:
				self.analysis_status["running"] = False
				self.analysis_status["finished"] = True
				self.analysis_status["phase"] = "done"
				self.analysis_status["message"] = "Analysis complete"
				self._append_event("done", "Analysis complete")
		except AnalysisCancelled:
			with self.lock:
				self.analysis_status["running"] = False
				self.analysis_status["finished"] = True
				self.analysis_status["cancelled"] = True
				self.analysis_status["phase"] = "cancelled"
				self.analysis_status["message"] = "Analysis cancelled"
				self._append_event("cancelled", "Analysis cancelled")
		except Exception as exc:
			with self.lock:
				self.analysis_status["running"] = False
				self.analysis_status["finished"] = True
				self.analysis_status["error"] = str(exc)
				self.analysis_status["phase"] = "error"
				self.analysis_status["message"] = "Analysis failed"
				self._append_event("error", f"Analysis failed: {exc}")

	def analyze(self, username: str, path: Path, ignore_hidden_files: bool = True) -> dict:
		# The iNaturalist fetch is mostly network and cache I/O, so it runs in a
		# background thread while local photos are indexed. Its messages go to the
		# event log only so the progress bar keeps tracking indexing.
		# Return objects frozen by a previous analysis to normal garbage collection.
		gc.unfreeze()
		self._set_progress("fetch", "Fetching iNaturalist observations in the background")
		fetch_stop = threading.Event()
		fetch_result: dict[str, object] = {}

		def _fetch_cancel_check() -> None:
			if fetch_stop.is_set():
				raise AnalysisCancelled()
			self._cancel_check()

		def _fetch_worker() -> None:
			try:
				fetch_result["observations"] = fetch_observations(
					username,
					self.cache_dir,
					progress_cb=self._log_event,
					cancel_check=_fetch_cancel_check,
				)
			except BaseException as exc:  # re-raised on the analysis thread
				fetch_result["error"] = exc

		fetch_thread = threading.Thread(target=_fetch_worker, name="inat-fetch", daemon=True)
		fetch_thread.start()
		try:
			photo_cache_db = self.cache_dir / PHOTO_METADATA_CACHE_FILENAME
			with PhotoMetadataCache(photo_cache_db) as photo_metadata_cache:
				_local_photos, by_second, by_minute, by_id = index_local_photos(
					path,
					progress_cb=self._set_progress,
					cancel_check=self._cancel_check,
					group_versions=self.group_versions,
					ignore_hidden_files=ignore_hidden_files,
					photo_metadata_cache=photo_metadata_cache,
				)
			self._cancel_check()
			if fetch_thread.is_alive():
				self._set_progress("fetch", "Waiting for iNaturalist observation fetch to finish")
			while fetch_thread.is_alive():
				fetch_thread.join(timeout=0.2)
				self._cancel_check()
		finally:
			# Normally already finished; on cancel/error don't block on an in-flight
			# request; the worker exits at its next cancel check.
			fetch_stop.set()
			fetch_thread.join(timeout=2)
		if "error" in fetch_result:
			raise fetch_result["error"]
		raw_observations = fetch_result["observations"]
		authenticated = inat_auth.status()["connected"]
		privacy_report = observation_privacy_report(raw_observations, authenticated=authenticated)
		if authenticated:
			raw_observations = [with_private_location(obs) for obs in raw_observations]
		# The observation data is millions of small long-lived objects. Freezing moves
		# them out of the cyclic GC's generations so later collections don't keep
		# re-traversing them (they're acyclic, so refcounting still frees them).
		gc.freeze()
		self._set_progress("fetch", f"Preparing {len(raw_observations)} observations")
		observations, obs_photo_by_id = build_observation_records(raw_observations)
		self._set_progress("fetch", f"Loaded {len(observations)} observations")
		refresh_config = load_config()

		def _metadata_refresh_check(photo: LocalPhoto, observation: ObservationData) -> tuple[bool, list[str]]:
			return metadata_refresh_needed_with_details(
				photo.path,
				observation,
				embedded=photo.embedded_xmp,
				config=refresh_config,
			)

		self._set_progress("match", "Starting datetime and CV matching", current=0, total=len(observations))
		cv_score_cache = CVScoreCache(self.cache_dir / CV_SCORE_CACHE_FILENAME, cv_score_algorithm_version())
		try:
			with cv_score_cache:
				matches, refresh_dirty_photo_ids = matcher_logic.match_observations(
					observations,
					by_second,
					by_minute,
					by_id,
					match_result_cls=MatchResult,
					group_versions=self.group_versions,
					cache_dir=self.cache_dir,
					cache_observation_photo_fn=cache_observation_photo,
					score_candidates_fn=lambda paths, candidates: score_candidates(paths, candidates, score_cache=cv_score_cache),
					score_candidates_per_photo_fn=lambda paths, candidates: score_candidates_per_photo(
						paths, candidates, score_cache=cv_score_cache
					),
					score_candidate_batches_fn=lambda jobs, **kwargs: score_candidate_batches(
						jobs, score_cache=cv_score_cache, **kwargs
					),
					expand_with_version_siblings_fn=expand_with_version_siblings,
					metadata_refresh_check_fn=_metadata_refresh_check,
					cv_score_threshold=CV_SCORE_THRESHOLD,
					cv_margin_threshold=CV_MARGIN_THRESHOLD,
					progress_cb=self._set_progress,
					cancel_check=self._cancel_check,
				)
		finally:
			clear_cv_image_cache()
		if cv_score_cache.hits or cv_score_cache.misses:
			self._log_event(
				"cv",
				f"CV score cache: {cv_score_cache.hits} reused, {cv_score_cache.misses} computed",
			)

		with self.lock:
			self.ready = True
			self.username = username
			self.root_path = path
			self.local_photos_by_id = by_id
			self.obs_photo_by_id = obs_photo_by_id
			self.refresh_dirty_photo_ids = refresh_dirty_photo_ids
			self.matches = matches
			self.matches_by_obs_id = {match.observation.obs_id: match for match in matches}
			self.privacy_report = privacy_report
		self._set_progress("match", "Matching complete", current=len(observations), total=len(observations))

		return self.summary()

	def cache_info(self) -> dict:
		entries = cache_admin.describe_caches(self.cache_dir)
		return {
			"cache_dir": str(self.cache_dir),
			"total_bytes": sum(int(entry["size_bytes"]) for entry in entries),
			"caches": entries,
		}

	def clear_caches(self, name: str) -> dict:
		if name != "all" and name not in cache_admin.CACHE_NAMES:
			raise HTTPException(status_code=404, detail=f"Unknown cache: {name}")
		with self.lock:
			# Holding the lock also stops an analysis from starting mid-clear.
			if self.analysis_status["running"]:
				raise HTTPException(status_code=409, detail="Caches can't be cleared while an analysis is running")
			if name == "all":
				freed = cache_admin.clear_all_caches(self.cache_dir)
			else:
				freed = cache_admin.clear_cache(self.cache_dir, name)
		LOGGER.info("Cleared cache %s (%s bytes)", name, freed)
		return {"cleared": name, "freed_bytes": freed, **self.cache_info()}

	def summary(self) -> dict:
		with self.lock:
			total = len(self.matches)
			attention = sum(1 for m in self.matches if needs_manual_attention(m))
			matched = total - attention
			return {"total": total, "matched": matched, "needs_attention": attention, "privacy": self.privacy_report}

	def _refresh_diff_for_photo(self, photo: LocalPhoto, config: AppConfig) -> list[dict[str, object]]:
		"""Structured diff for a photo already tagged with an observation whose metadata is stale."""
		match = self.matches_by_obs_id.get(photo.existing_observation_id) if photo.existing_observation_id else None
		if match is None:
			return []
		embedded = photo.embedded_xmp
		if embedded is None:
			try:
				embedded = read_embedded_xmp(photo.path)
			except Exception as exc:
				LOGGER.warning("Could not read XMP for %s: %s", photo.path, exc)
				return []
		expected = _effective_metadata_xmp(match.observation, config)
		return _public_metadata_diff(_metadata_diff_for_photo(photo.path, embedded, expected))

	def _build_grouped_candidates(self, match: MatchResult) -> list[dict]:
		score_map = {photo_id: score for photo_id, score in match.scored_candidates}
		config: AppConfig | None = None

		version_groups_dict: dict[str, list[int]] = {}
		for photo_id in match.candidate_photo_ids:
			photo = self.local_photos_by_id.get(photo_id)
			if photo is None:
				continue
			vg = photo.version_group or str(photo_id)
			if vg not in version_groups_dict:
				version_groups_dict[vg] = []
			version_groups_dict[vg].append(photo_id)

		candidates = []
		for _vg, photo_ids in version_groups_dict.items():
			photos_in_group = [self.local_photos_by_id[pid] for pid in photo_ids if pid in self.local_photos_by_id]
			if not photos_in_group:
				continue

			photos_in_group.sort(key=lambda p: p.version_number, reverse=True)
			highest_photo = photos_in_group[0]

			if len(photos_in_group) > 1:
				names = [p.path.name for p in photos_in_group]
				name_display = " & ".join(names)
			else:
				name_display = highest_photo.path.name

			is_selected = any(pid in match.selected_photo_ids for pid in photo_ids)
			dirty_photo_ids = sorted([pid for pid in photo_ids if pid in self.refresh_dirty_photo_ids])

			# Versions usually need identical updates; show each distinct diff once.
			metadata_diffs: list[dict[str, object]] = []
			diff_by_signature: dict[str, dict[str, object]] = {}
			for pid in dirty_photo_ids:
				photo = self.local_photos_by_id.get(pid)
				if photo is None:
					continue
				if config is None:
					config = load_config()
				diff = self._refresh_diff_for_photo(photo, config)
				signature = json.dumps(diff, sort_keys=True)
				entry = diff_by_signature.get(signature)
				if entry is None:
					entry = {"names": [], "diff": diff}
					diff_by_signature[signature] = entry
					metadata_diffs.append(entry)
				entry["names"].append(photo.path.name)

			candidates.append(
				{
					"photo_id": highest_photo.id,
					"photo_ids": sorted(photo_ids),
					"dirty_photo_ids": dirty_photo_ids,
					"dirty": bool(dirty_photo_ids),
					"metadata_diffs": metadata_diffs,
					"name": name_display,
					"path": str(highest_photo.path),
					"score": float(score_map.get(highest_photo.id, 0.0)),
					"selected": is_selected,
					"url": f"/api/image/local/{highest_photo.id}",
				}
			)

		return sorted(candidates, key=lambda c: c["score"], reverse=True)

	def require_ready(self) -> None:
		if not self.ready:
			raise HTTPException(status_code=400, detail="No analysis loaded. Start a session first.")

	def tree(self) -> dict:
		with self.lock:
			self.require_ready()
			years: dict[int, dict] = {}
			for match in self.matches:
				when = match.observation.observed_at
				y, m, d = when.year, when.month, when.day
				stats = tree_status_summary(match)
				year = years.setdefault(
					y,
					{
						"year": y,
						"observation_count": 0,
						"matchable_count": 0,
						"unmatchable_count": 0,
						"actionable_count": 0,
						"status_counts": empty_tree_status_counts(),
						"months": {},
					},
				)
				month = year["months"].setdefault(
					m,
					{
						"month": m,
						"observation_count": 0,
						"matchable_count": 0,
						"unmatchable_count": 0,
						"actionable_count": 0,
						"status_counts": empty_tree_status_counts(),
						"days": {},
					},
				)
				day = month["days"].setdefault(
					d,
					{
						"day": d,
						"observation_count": 0,
						"matchable_count": 0,
						"unmatchable_count": 0,
						"actionable_count": 0,
						"status_counts": empty_tree_status_counts(),
					},
				)

				merge_tree_status_summary(year, stats)
				merge_tree_status_summary(month, stats)
				merge_tree_status_summary(day, stats)

			payload_years = []
			for year_key in sorted(years.keys()):
				y = years[year_key]
				payload_months = []
				for month_key in sorted(y["months"].keys()):
					m = y["months"][month_key]
					payload_days = []
					for day_key in sorted(m["days"].keys()):
						payload_days.append(m["days"][day_key])
					m["days"] = payload_days
					payload_months.append(m)
				y["months"] = payload_months
				payload_years.append(y)

			return {"years": payload_years, **self.summary()}

	def day_detail(self, year: int, month: int, day: int) -> dict:
		with self.lock:
			self.require_ready()
			rows = []
			for match in self.matches:
				when = match.observation.observed_at
				if when.year != year or when.month != month or when.day != day:
					continue

				obs_photos = []
				for photo in match.observation.photos:
					if "id" not in photo:
						continue
					obs_photos.append(
						{
							"photo_id": int(photo["id"]),
							"url": f"/api/image/observation/{int(photo['id'])}",
						}
					)

				candidates = self._build_grouped_candidates(match)

				rows.append(
					{
						"obs_id": match.observation.obs_id,
						"time": when.strftime("%H:%M:%S"),
						"status": match.status,
						"detail": match.detail,
						"state": observation_visual_state(match),
						"uri": match.observation.uri,
						"observation_photos": obs_photos,
						"candidates": sorted(candidates, key=lambda c: c["score"], reverse=True),
					}
				)

			rows.sort(key=lambda row: row["time"])
			return {
				"year": year,
				"month": month,
				"day": day,
				"rows": rows,
				"observation_count": len(rows),
			}

	def selection_state(self) -> dict:
		"""Return full-dataset selection state used by the save button."""
		with self.lock:
			self.require_ready()
			rows = []
			for match in self.matches:
				dirty_for_obs = [
					photo_id
					for photo_id in match.candidate_photo_ids
					if photo_id in self.refresh_dirty_photo_ids
				]
				rows.append(
					{
						"obs_id": match.observation.obs_id,
						"selected_photo_ids": sorted(match.selected_photo_ids),
						"dirty_photo_ids": sorted(dirty_for_obs),
					}
				)
			return {"rows": rows}

	def observation_detail(self, obs_id: int) -> dict:
		with self.lock:
			self.require_ready()
			match = self.matches_by_obs_id.get(obs_id)
			if match is None:
				raise HTTPException(status_code=404, detail="Observation not found")

			obs_photos = []
			for photo in match.observation.photos:
				if "id" not in photo:
					continue
				obs_photos.append(
					{
						"photo_id": int(photo["id"]),
						"url": f"/api/image/observation/{int(photo['id'])}",
					}
				)

			candidates = self._build_grouped_candidates(match)

			return {
				"obs_id": match.observation.obs_id,
				"observed_at": match.observation.observed_at.isoformat(),
				"uri": match.observation.uri,
				"state": observation_visual_state(match),
				"status": match.status,
				"detail": match.detail,
				"observation_photos": obs_photos,
				"candidates": candidates,
			}

	def update_selection(self, obs_id: int, selected_photo_ids: list[int]) -> dict:
		with self.lock:
			self.require_ready()
			match = self.matches_by_obs_id.get(obs_id)
			if match is None:
				raise HTTPException(status_code=404, detail="Observation not found")
			
			# Expand selection to include all version siblings
			expanded_selection = set(selected_photo_ids)
			if self.group_versions:
				for photo_id in selected_photo_ids:
					photo = self.local_photos_by_id.get(photo_id)
					if photo and photo.version_siblings:
						expanded_selection.update(photo.version_siblings)
			
			valid = set(match.candidate_photo_ids)
			invalid = expanded_selection - valid
			if invalid:
				raise HTTPException(status_code=400, detail=f"Invalid photo IDs for observation: {sorted(invalid)}")
			match.selected_photo_ids = expanded_selection
			return {"obs_id": obs_id, "selected_count": len(match.selected_photo_ids), **self.summary()}

	def save_selected(self) -> dict:
		with self.lock:
			self.require_ready()
			plan = self._build_save_plan_locked()
		if LOGGER.isEnabledFor(logging.INFO):
			LOGGER.info("save_selected: planned operations=%s", len(plan))

		success = 0
		refresh_count = 0
		new_count = 0
		remove_count = 0
		failures: list[dict] = []
		for item in plan:
			photo_id = item["photo_id"]
			obs_id = item["obs_id"]
			photo = self.local_photos_by_id.get(photo_id)
			if photo is None:
				continue

			try:
				if item["reason"] == "remove":
					if LOGGER.isEnabledFor(logging.DEBUG):
						LOGGER.debug("Removing observation metadata: photo_id=%s path=%s", photo_id, photo.path)
					remove_observation_metadata(photo.path)
					success += 1
					remove_count += 1
					photo.existing_observation_id = None
				else:
					match = self.matches_by_obs_id.get(obs_id)
					if match is None:
						continue
					if LOGGER.isEnabledFor(logging.DEBUG):
						LOGGER.debug(
							"Saving observation metadata: photo_id=%s obs_id=%s path=%s reason=%s",
							photo_id,
							obs_id,
							photo.path,
							item["reason"],
						)
					write_observation_metadata(photo.path, match.observation)
					success += 1
					photo.existing_observation_id = obs_id
					photo.embedded_xmp = None
					if item["reason"] == "refresh":
						refresh_count += 1
						self.refresh_dirty_photo_ids.discard(photo.id)
						photo.metadata_refresh_needed = False
					else:
						new_count += 1
			except Exception as exc:
				LOGGER.exception(
					"Failed save operation: photo_id=%s obs_id=%s path=%s reason=%s",
					photo_id,
					obs_id,
					photo.path,
					item.get("reason"),
				)
				failures.append({"photo_id": photo_id, "path": str(photo.path), "error": str(exc)})

		for match in self.matches:
			if match.selected_photo_ids:
				match.has_existing_tag = True

		if failures:
			LOGGER.error(
				"Save completed with failures: saved=%s failed=%s first_error=%s",
				success,
				len(failures),
				failures[0],
			)
		else:
			LOGGER.info(
				"Save completed: saved=%s refresh=%s new=%s removed=%s planned=%s",
				success,
				refresh_count,
				new_count,
				remove_count,
				len(plan),
			)

		return {
			"saved": success,
			"failed": len(failures),
			"failures": failures,
			"refresh_count": refresh_count,
			"new_count": new_count,
			"remove_count": remove_count,
			"planned_count": len(plan),
		}

	def _build_save_plan_locked(self) -> list[dict[str, object]]:
		"""Build a deduplicated plan of exactly which files will be written."""
		photo_to_obs: dict[int, int] = {}
		collisions: dict[int, list[int]] = {}  # photo_id -> [obs_ids]
		selected_photo_ids: set[int] = set()

		for match in self.matches:
			selected_photo_ids.update(match.selected_photo_ids)
			for photo_id in match.selected_photo_ids:
				photo = self.local_photos_by_id.get(photo_id)
				# Skip photos already correctly tagged for this observation;
				# if their metadata is stale they will be picked up via refresh_dirty_photo_ids.
				if photo is not None and photo.existing_observation_id == match.observation.obs_id:
					continue
				previous = photo_to_obs.get(photo_id)
				if previous is not None and previous != match.observation.obs_id:
					if photo_id not in collisions:
						collisions[photo_id] = [previous]
					collisions[photo_id].append(match.observation.obs_id)
				photo_to_obs[photo_id] = match.observation.obs_id

		for photo_id in self.refresh_dirty_photo_ids:
			photo = self.local_photos_by_id.get(photo_id)
			if photo is None or photo.existing_observation_id is None:
				continue
			photo_to_obs.setdefault(photo_id, photo.existing_observation_id)

		if collisions:
			conflict_details = []
			for photo_id, obs_ids in collisions.items():
				photo = self.local_photos_by_id.get(photo_id)
				obs_ids_unique = sorted(set(obs_ids))
				obs_info = []
				for obs_id in obs_ids_unique:
					match = self.matches_by_obs_id.get(obs_id)
					if match:
						datetime_str = match.observation.observed_at.isoformat()
						obs_info.append({
							"obs_id": obs_id,
							"datetime": datetime_str,
						})
				conflict_details.append({
					"photo_id": photo_id,
					"photo_name": photo.path.name if photo else "unknown",
					"obs_ids": obs_info,
				})
			raise HTTPException(
				status_code=400,
				detail={
					"message": "Some photos are assigned to multiple observations",
					"conflicts": conflict_details,
				},
			)

		# Expand to version siblings and deduplicate by photo_id so each file is written once.
		final_photo_to_obs: dict[int, int] = {}
		for photo_id, obs_id in photo_to_obs.items():
			photo = self.local_photos_by_id.get(photo_id)
			if photo is None:
				continue
			target_ids = [photo_id]
			if self.group_versions and photo.version_siblings:
				target_ids = [pid for pid in photo.version_siblings if pid in self.local_photos_by_id]
			for target_id in target_ids:
				previous = final_photo_to_obs.get(target_id)
				if previous is None:
					final_photo_to_obs[target_id] = obs_id
				elif previous != obs_id:
					raise HTTPException(status_code=400, detail="Conflicting save plan detected for photo versions")

		plan = []
		for target_id, obs_id in sorted(final_photo_to_obs.items()):
			reason = "refresh" if target_id in self.refresh_dirty_photo_ids else "new"
			photo = self.local_photos_by_id.get(target_id)
			match = self.matches_by_obs_id.get(obs_id)
			when = match.observation.observed_at if match is not None else None

			plan.append(
				{
					"photo_id": target_id,
					"obs_id": obs_id,
					"reason": reason,
					"path": str(photo.path) if photo else "",
					"year": when.year if when is not None else None,
					"month": when.month if when is not None else None,
					"day": when.day if when is not None else None,
				}
			)
		for photo in sorted(self.local_photos_by_id.values(), key=lambda p: p.id):
			if photo.existing_observation_id is None:
				continue
			if photo.id in selected_photo_ids:
				continue
			if photo.id in final_photo_to_obs:
				continue
			match = self.matches_by_obs_id.get(photo.existing_observation_id)
			when = match.observation.observed_at if match is not None else None
			plan.append(
				{
					"photo_id": photo.id,
					"obs_id": photo.existing_observation_id,
					"reason": "remove",
					"path": str(photo.path),
					"year": when.year if when is not None else None,
					"month": when.month if when is not None else None,
					"day": when.day if when is not None else None,
				}
			)
		return plan

	def save_preview(self) -> dict:
		with self.lock:
			self.require_ready()
			plan = self._build_save_plan_locked()
			refresh_count = sum(1 for item in plan if item["reason"] == "refresh")
			new_count = sum(1 for item in plan if item["reason"] == "new")
			remove_count = sum(1 for item in plan if item["reason"] == "remove")
			return {
				"total": len(plan),
				"refresh_count": refresh_count,
				"new_count": new_count,
				"remove_count": remove_count,
				"files": plan,
			}

	def pending_changes(self) -> dict:
		"""Save plan with per-file metadata diffs, grouped by observation.

		Files of one observation that would receive an identical diff are merged
		into one group so the UI shows each distinct change once.
		"""
		with self.lock:
			self.require_ready()
			plan = self._build_save_plan_locked()
			config = load_config()
			expected_by_obs: dict[int, dict[str, object]] = {}
			photo_cache_db = self.cache_dir / PHOTO_METADATA_CACHE_FILENAME
			photo_cache: PhotoMetadataCache | None = None

			def embedded_for(photo: LocalPhoto) -> dict[str, object]:
				if photo.embedded_xmp is not None:
					return photo.embedded_xmp
				if photo_cache is not None:
					try:
						cached = photo_cache.get(photo.path, os.stat(photo.path).st_mtime)
					except OSError:
						cached = None
					if cached is not None:
						return cached[1]
				try:
					return read_embedded_xmp(photo.path)
				except Exception as exc:
					LOGGER.warning("Could not read XMP for pending-changes preview of %s: %s", photo.path, exc)
					return {}

			observations: dict[int, dict[str, object]] = {}
			groups_by_signature: dict[tuple[int, str], dict[str, object]] = {}
			with ExitStack() as stack:
				if photo_cache_db.exists():
					photo_cache = stack.enter_context(PhotoMetadataCache(photo_cache_db))
				for item in plan:
					obs_id = int(item["obs_id"])
					photo = self.local_photos_by_id.get(int(item["photo_id"]))
					match = self.matches_by_obs_id.get(obs_id)
					reason = str(item["reason"])
					skipped = False
					diff: list[dict[str, object]] = []
					if photo is not None:
						embedded = embedded_for(photo)
						if reason == "remove":
							removal = compute_removal_diff(embedded)
							skipped = removal is None
							diff = removal or []
						elif match is not None:
							expected = expected_by_obs.get(obs_id)
							if expected is None:
								expected = _effective_metadata_xmp(match.observation, config)
								expected_by_obs[obs_id] = expected
							diff = _public_metadata_diff(_metadata_diff_for_photo(photo.path, embedded, expected))

					obs_entry = observations.get(obs_id)
					if obs_entry is None:
						context = match.observation.template_context if match is not None else {}
						obs_entry = {
							"obs_id": obs_id,
							"name": context.get("name"),
							"common_name": context.get("common_name"),
							"uri": match.observation.uri if match is not None else None,
							"year": item["year"],
							"month": item["month"],
							"day": item["day"],
							"groups": [],
						}
						observations[obs_id] = obs_entry
					signature = json.dumps([reason, skipped, diff], sort_keys=True)
					group = groups_by_signature.get((obs_id, signature))
					if group is None:
						group = {"reason": reason, "skipped": skipped, "diff": diff, "files": []}
						groups_by_signature[(obs_id, signature)] = group
						obs_entry["groups"].append(group)
					group["files"].append({"photo_id": item["photo_id"], "path": item["path"]})

			return {
				"total": len(plan),
				"refresh_count": sum(1 for item in plan if item["reason"] == "refresh"),
				"new_count": sum(1 for item in plan if item["reason"] == "new"),
				"remove_count": sum(1 for item in plan if item["reason"] == "remove"),
				"observations": [observations[obs_id] for obs_id in sorted(observations)],
			}

	def local_image_path(self, photo_id: int) -> Path:
		with self.lock:
			self.require_ready()
			photo = self.local_photos_by_id.get(photo_id)
			if photo is None:
				raise HTTPException(status_code=404, detail="Local photo not found")
			return photo.path

	def observation_image_path(self, observation_photo_id: int) -> Path:
		with self.lock:
			self.require_ready()
			photo = self.obs_photo_by_id.get(observation_photo_id)
			if photo is None:
				raise HTTPException(status_code=404, detail="Observation photo not found")
			if parse_observation_id(photo.get("observation_id")) is None:
				# Backstop for older in-memory records without observation_id.
				for match in self.matches:
					for obs_photo in match.observation.photos:
						if parse_observation_id(obs_photo.get("id")) == observation_photo_id:
							photo = {**photo, "observation_id": match.observation.obs_id}
							break
					if parse_observation_id(photo.get("observation_id")) is not None:
						break
		try:
			return cache_observation_photo(photo, self.cache_dir)
		except Exception as exc:
			raise HTTPException(status_code=500, detail=str(exc)) from exc


state = MatcherState()
app = FastAPI(title="inat2exif web app")
app.mount("/web", StaticFiles(directory=str(STATIC_DIR), html=True), name="web")


@app.middleware("http")
async def private_response_headers(request: Request, call_next):
	response = await call_next(request)
	if request.url.path.startswith("/api/"):
		response.headers["Cache-Control"] = "no-store"
		response.headers["Referrer-Policy"] = "no-referrer"
	return response


@app.get("/")
def index_redirect():
	return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/session/start")
def api_start_session(payload: StartSessionRequest | None = None):
	config = load_config()
	username = payload.username.strip() if payload and payload.username else config.username.strip()
	path_value = payload.path.strip() if payload and payload.path else config.path.strip()

	if not username:
		raise HTTPException(status_code=400, detail="Username is required in config")
	if not path_value:
		raise HTTPException(status_code=400, detail="Photos path is required in config")

	root = Path(path_value).expanduser().resolve()
	if not root.exists() or not root.is_dir():
		raise HTTPException(status_code=400, detail="Path does not exist or is not a directory")
	with state.lock:
		try:
			inat_auth.headers(username)
		except AuthenticationError as exc:
			raise HTTPException(status_code=409, detail=str(exc)) from exc
		return state.start_analysis(username, root, ignore_hidden_files=config.ignore_hidden_files)


@app.post("/api/session/cancel")
def api_cancel_session():
	return state.cancel_analysis()


@app.get("/api/session/status")
def api_session_status():
	return state.analysis_progress()


@app.get("/api/defaults")
def api_defaults():
	# Backward-compatible endpoint name; values are sourced from persisted config.
	return load_config()


@app.get("/api/tree")
def api_tree():
	return state.tree()


@app.get("/api/observation/{obs_id}")
def api_observation(obs_id: int):
	return state.observation_detail(obs_id)


@app.get("/api/day/{year}/{month}/{day}")
def api_day_detail(year: int, month: int, day: int):
	return state.day_detail(year, month, day)


@app.get("/api/selection-state")
def api_selection_state():
	return state.selection_state()


@app.post("/api/observation/{obs_id}/selection")
def api_set_selection(obs_id: int, payload: SelectionRequest):
	return state.update_selection(obs_id, payload.selected_photo_ids)


@app.post("/api/observation/{obs_id}/library-search")
def api_start_library_search(obs_id: int):
	return state.start_library_search(obs_id)


@app.post("/api/observation/{obs_id}/library-search/accept")
def api_accept_library_search(obs_id: int, payload: LibrarySearchAcceptRequest):
	return state.accept_library_search_candidate(obs_id, payload.photo_id)


@app.get("/api/library-search")
def api_library_search_status():
	return state.library_search_state()


@app.post("/api/library-search/cancel")
def api_cancel_library_search():
	return state.cancel_library_search()


@app.post("/api/save")
def api_save():
	with state.lock:
		return state.save_selected()


@app.get("/api/save-preview")
def api_save_preview():
	return state.save_preview()


@app.get("/api/pending-changes")
def api_pending_changes():
	return state.pending_changes()


@app.get("/api/config")
def api_get_config():
	"""Return the current config, reading fresh from disk on every call."""
	return load_config()


@app.put("/api/config")
def api_put_config(payload: AppConfig):
	"""Replace the config with the supplied payload and return it."""
	save_config(payload)
	return payload


@app.get("/api/config/status")
def api_config_status():
	config = load_config()
	return {"ready": CONFIG_PATH.is_file() and bool(config.username.strip() and config.path.strip())}


class OAuthSetupRequest(BaseModel):
	client_id: str = ""
	client_secret: str = ""
	remember_credentials: bool = False


def _local_auth_request(request: Request, *, mutation: bool = False) -> str:
	origin = str(request.base_url).rstrip("/")
	if urlsplit(origin).hostname not in {"127.0.0.1", "localhost", "::1"}:
		raise HTTPException(status_code=400, detail="OAuth is available only through a loopback address on this computer")
	if mutation and request.headers.get("origin") != origin:
		raise HTTPException(status_code=403, detail="Sign-in changes must come from this app's page")
	return origin


def _require_auth_idle() -> None:
	if state.analysis_status["running"] or state.library_search_status["running"]:
		raise HTTPException(status_code=409, detail="Finish or cancel the current task before changing sign-in")


def _invalidate_auth_results() -> None:
	state.ready = False
	state.matches = []
	state.matches_by_obs_id = {}
	state.obs_photo_by_id = {}
	state.local_photos_by_id = {}
	state.refresh_dirty_photo_ids = set()
	state.privacy_report = None
	state.library_search_status = _idle_library_search_status()
	state.analysis_status.update(finished=False, error=None, events=[], phase="idle", message="Sign-in changed; run auto-match again")


@app.get("/api/auth/status")
def api_auth_status(request: Request):
	origin = str(request.base_url).rstrip("/")
	available = urlsplit(origin).hostname in {"127.0.0.1", "localhost", "::1"}
	storage = credential_store(OAUTH_CREDENTIALS_PATH)
	credentials = None
	storage_error = None
	if available and storage.supported():
		try:
			credentials = storage.load()
		except CredentialStorageError as exc:
			storage_error = str(exc)
			LOGGER.warning("Could not read remembered OAuth application credentials: %s", exc)
	return {**inat_auth.status(), "available": available,
		"redirect_uri": origin + "/api/auth/callback" if available else None,
		"credentials_saved": credentials is not None, "client_id": credentials[0] if credentials else "",
		"credential_storage_supported": storage.supported(),
		"credential_storage_error": storage_error,
		"credential_storage_path": storage.location if available else None}


@app.post("/api/auth/start")
def api_auth_start(request: Request, payload: OAuthSetupRequest):
	origin = _local_auth_request(request, mutation=True)
	with state.lock:
		_require_auth_idle()
		try:
			storage = credential_store(OAUTH_CREDENTIALS_PATH)
			client_id, client_secret = payload.client_id.strip(), payload.client_secret.strip()
			if not client_secret and storage.supported():
				saved = storage.load()
				if saved and (not client_id or client_id == saved[0]):
					client_id, client_secret = saved
			if not client_id or not client_secret:
				raise AuthenticationError("Enter the registered application's client ID and client secret")
			if payload.remember_credentials:
				storage.save(client_id, client_secret)
			elif storage.supported():
				storage.forget()
			oauth_state, url = inat_auth.begin(client_id, client_secret, origin + "/api/auth/callback")
		except (AuthenticationError, CredentialStorageError) as exc:
			LOGGER.warning("Could not prepare OAuth sign-in: %s", exc)
			raise HTTPException(status_code=400, detail=str(exc)) from exc
	response = JSONResponse({"url": url})
	response.set_cookie("inat_oauth_state", oauth_state, max_age=600, httponly=True,
		samesite="lax", secure=request.url.scheme == "https", path="/api/auth")
	response.headers["Cache-Control"] = "no-store"
	return response


@app.post("/api/auth/forget-credentials")
def api_auth_forget_credentials(request: Request):
	_local_auth_request(request, mutation=True)
	with state.lock:
		_require_auth_idle()
		try:
			credential_store(OAUTH_CREDENTIALS_PATH).forget()
		except CredentialStorageError as exc:
			LOGGER.warning("Could not forget OAuth application credentials: %s", exc)
			raise HTTPException(status_code=400, detail=str(exc)) from exc
		with inat_auth.lock:
			inat_auth.pending = None
	return {"credentials_saved": False}


@app.get("/api/auth/callback")
def api_auth_callback(request: Request, code: str = "", error: str = ""):
	_local_auth_request(request)
	# Remove authorization codes from the access-log URL after parsing the callback.
	oauth_state = request.query_params.get("state", "")
	request.scope["query_string"] = b""
	with state.lock:
		try:
			_require_auth_idle()
			inat_auth.finish(oauth_state, request.cookies.get("inat_oauth_state", ""), code, bool(error))
		except (AuthenticationError, HTTPException) as exc:
			inat_auth.error = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
			LOGGER.warning("iNaturalist sign-in did not complete")
		else:
			_invalidate_auth_results()
	response = RedirectResponse("/", status_code=303)
	response.delete_cookie("inat_oauth_state", path="/api/auth")
	response.headers["Cache-Control"] = "no-store"
	response.headers["Referrer-Policy"] = "no-referrer"
	return response


@app.post("/api/auth/disconnect")
def api_auth_disconnect(request: Request):
	_local_auth_request(request, mutation=True)
	with state.lock:
		_require_auth_idle()
		inat_auth.disconnect()
		_invalidate_auth_results()
	return {"connected": False}


@app.get("/api/privacy-report")
def api_privacy_report(username: str):
	if not username.strip():
		raise HTTPException(status_code=400, detail="Enter an iNaturalist username first")
	try:
		observations = matcher_logic.fetch_observations(
			username.strip(), state.cache_dir, requests_module=requests, time_module=time,
			cache_ttl_s=CACHE_TTL_S, per_page=OBS_PER_PAGE, use_cache=False,
		)
	except (requests.RequestException, ValueError):
		LOGGER.warning("Could not check public observation privacy")
		raise HTTPException(status_code=502, detail="Could not check public observations. Please try again.") from None
	return observation_privacy_report(observations, authenticated=False)


@app.get("/api/folders")
def api_folders(path: str = "", show_hidden: bool = False):
	"""Browse directories on the app's computer, without uploading any files."""
	try:
		directory = Path(path).expanduser().resolve() if path.strip() else Path.home()
		if not directory.is_dir():
			raise HTTPException(status_code=400, detail="Choose an existing folder on the computer running inat2exif")
		folders = sorted(
			(entry for entry in directory.iterdir() if (show_hidden or not entry.name.startswith(".")) and entry.is_dir()),
			key=lambda entry: entry.name.casefold(),
		)
	except (OSError, RuntimeError, ValueError) as exc:
		LOGGER.warning("Could not browse folder %s: %s", path, exc)
		raise HTTPException(status_code=400, detail=f"Could not browse folder: {exc}") from exc
	return {
		"path": str(directory),
		"parent": str(directory.parent) if directory.parent != directory else None,
		"folders": [{"name": entry.name, "path": str(entry)} for entry in folders],
	}


@app.get("/api/template-samples")
def api_template_samples(username: str = ""):
	try:
		samples = selected_preview_observations(username)
	except (requests.RequestException, ValueError) as exc:
		LOGGER.warning("Could not load preview observations for %s: %s", username, exc)
		raise HTTPException(status_code=502, detail=f"Could not load iNaturalist preview observations: {exc}") from exc
	return {"samples": [
		{
			"obs_id": entry["observation"]["id"],
			"name": _get_nested(entry["observation"], "taxon", "name") or entry["observation"].get("species_guess") or "Unidentified observation",
			"uri": entry["observation"].get("uri"),
			"reasons": entry["reasons"],
			"comments_count": entry["observation"].get("comments_count", 0),
			"faves_count": entry["observation"].get("faves_count", 0),
		}
		for entry in samples
	]}


@app.get("/api/cache")
def api_cache_info():
	return state.cache_info()


@app.delete("/api/cache/{name}")
def api_clear_cache(name: str):
	"""Clear one cache by name, or every cache when name is "all"."""
	return state.clear_caches(name)


@app.get("/api/template-fields")
def api_template_fields():
	context = build_sample_template_context()
	sample = load_sample_observation()
	fields, source = build_template_field_catalog()
	return {
		"fields": fields,
		"source": source,
		"sample_obs_id": sample.get("id") if isinstance(sample, dict) else None,
		"sample_uri": context.get("uri"),
	}


@app.post("/api/template-preview")
def api_template_preview(payload: TemplatePreviewRequest):
	context = build_sample_template_context()
	if payload.sample_obs_id is not None:
		try:
			samples = selected_preview_observations(payload.username)
		except (requests.RequestException, ValueError) as exc:
			LOGGER.warning("Could not load selected preview observation: %s", exc)
			raise HTTPException(status_code=502, detail=f"Could not load preview observation: {exc}") from exc
		obs = next((entry["observation"] for entry in samples if entry["observation"]["id"] == payload.sample_obs_id), None)
		if obs is None:
			raise HTTPException(status_code=400, detail="The selected preview observation is unavailable; reload the preview samples")
		observed = parse_observation_datetime(obs.get("time_observed_at")) or dt.datetime.now(dt.UTC)
		context = build_observation_template_context(obs, obs["id"], obs.get("uri") or f"https://www.inaturalist.org/observations/{obs['id']}", observed)
	if not context:
		raise HTTPException(status_code=404, detail="No sample observation data available for preview")
	try:
		title = _render_observation_template(payload.title_template, context) if payload.title_template.strip() else ""
		caption = _render_observation_template(payload.caption_template, context) if payload.caption_template.strip() else ""
	except Exception as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from exc
	return {
		"title": title,
		"caption": caption,
		"sample_obs_id": context.get("obs_id"),
		"sample_uri": context.get("uri"),
	}


_shutdown_handler: Callable[[], None] | None = None


def set_shutdown_handler(handler: Callable[[], None] | None) -> None:
	global _shutdown_handler
	_shutdown_handler = handler


@app.get("/api/app-info")
def api_app_info():
	return {"app": "inat2exif", "version": __version__, "quit_available": _shutdown_handler is not None}


@app.post("/api/app/quit")
def api_app_quit(request: Request):
	if request.headers.get("origin") != str(request.base_url).rstrip("/"):
		raise HTTPException(status_code=403, detail="Quit requests must come from this app's page")
	if _shutdown_handler is None:
		raise HTTPException(status_code=409, detail="This server was not started by the inat2exif launcher; stop it where it was started")
	if not state.lock.acquire(timeout=2):
		raise HTTPException(status_code=409, detail="Wait for the current save to finish before quitting")
	try:
		if state.analysis_status["running"] or state.library_search_status["running"]:
			raise HTTPException(status_code=409, detail="Cancel or finish the current task before quitting")
		LOGGER.info("Quit requested from the app")
		_shutdown_handler()
	finally:
		state.lock.release()
	return {"quitting": True}


@app.get("/api/image/local/{photo_id}")
def api_image_local(photo_id: int):
	path = state.local_image_path(photo_id)
	return FileResponse(path)


@app.get("/api/image/observation/{photo_id}")
def api_image_observation(photo_id: int):
	path = state.observation_image_path(photo_id)
	return FileResponse(path)


def configure(*, config_path: Path | None = None, cache_dir: Path | None = None, group_versions: bool = True) -> None:
	"""Apply launcher options before the server starts."""
	global CONFIG_PATH
	if config_path is not None:
		CONFIG_PATH = config_path.expanduser().resolve()
	if cache_dir is not None:
		state.cache_dir = cache_dir.expanduser().resolve()
	state.group_versions = group_versions
	LOGGER.info("Using config file: %s", CONFIG_PATH)
	LOGGER.info("Using cache directory: %s", state.cache_dir)
