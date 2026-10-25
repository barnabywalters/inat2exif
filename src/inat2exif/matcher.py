import json
import math
import os
import re
import shutil
import threading
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlencode

from inat2exif.caches import OBSERVATION_STORE_FILENAME
from inat2exif.observation_store import ObservationStore
from inat2exif.utils import parse_observation_id

# OpenCV releases the GIL during decode/feature extraction, so threads scale well.
DEFAULT_CV_MAX_WORKERS = min(8, os.cpu_count() or 1)

_JPEG_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _migrate_obs_to_subdir(root_cache_dir: Path) -> None:
    """Move numbered observation directories from *root_cache_dir* into the
    ``observations/`` subdirectory if they haven't been moved yet."""
    obs_dir = root_cache_dir / "observations"
    for child in list(root_cache_dir.iterdir()):
        if child.is_dir() and child.name.isdigit() and child != obs_dir:
            dest = obs_dir / child.name
            if not dest.exists():
                obs_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(child), str(dest))
            else:
                # Merge: move any files not already present
                for item in child.iterdir():
                    target = dest / item.name
                    if not target.exists():
                        shutil.move(str(item), str(target))
                shutil.rmtree(child, ignore_errors=True)


def observation_photo_url(photo: dict, size: str = "medium") -> str | None:
    url = photo.get("url")
    if not url:
        return None
    updated = re.sub(r"/(square|small|medium|large|original)\.(jpe?g|png)$", rf"/{size}.\2", url)
    if updated == url:
        updated = url.replace("square.jpeg", f"{size}.jpeg").replace("square.jpg", f"{size}.jpg")
    return updated


def cache_observation_photo(photo: dict, cache_dir: Path, *, requests_module) -> Path:
    obs_cache_dir = cache_dir / "observations"
    obs_cache_dir.mkdir(parents=True, exist_ok=True)
    obs_id = parse_observation_id(photo.get("observation_id"))
    if obs_id is None:
        raise RuntimeError(f"Observation photo {photo.get('id')} missing observation_id")
    photo_id = parse_observation_id(photo.get("id"))
    obs_dir = obs_cache_dir / str(obs_id)
    # Each observation photo gets its own file. Older versions kept a single
    # thumbnail.jpeg per observation, which made every photo look like the first.
    photo_path = obs_dir / (f"photo_{photo_id}.jpeg" if photo_id is not None else "thumbnail.jpeg")
    obs_dir.mkdir(parents=True, exist_ok=True)
    if photo_path.exists():
        return photo_path

    url = observation_photo_url(photo, "small")
    if not url:
        raise RuntimeError(f"Observation photo {photo.get('id')} has no URL")
    response = requests_module.get(url, timeout=30)
    response.raise_for_status()
    # Write then rename so concurrent readers never see a partial file.
    tmp_path = photo_path.with_name(f".{photo_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp_path.write_bytes(response.content)
    os.replace(tmp_path, photo_path)
    if photo_id is not None:
        legacy = obs_dir / "thumbnail.jpeg"
        if legacy.exists():
            try:
                legacy.unlink()
            except OSError:
                pass
    return photo_path




def _iter_legacy_observation_files(cache_dir: Path, obs_cache_dir: Path):
    """Yield (path, observations) from older on-disk cache layouts."""
    for legacy_path in list(cache_dir.glob("observations_*.json")):
        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        if isinstance(data, list):
            yield legacy_path, [obs for obs in data if isinstance(obs, dict)]
    if obs_cache_dir.exists():
        for child in obs_cache_dir.iterdir():
            obs_path = child / "observation.json"
            if not child.is_dir() or not child.name.isdigit() or not obs_path.exists():
                continue
            try:
                data = json.loads(obs_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, ValueError):
                continue
            if isinstance(data, dict):
                yield obs_path, [data]


def migrate_legacy_observation_cache(cache_dir: Path, store: ObservationStore, *, progress_cb=None) -> int:
    """Import JSON observation caches from older layouts into *store* and delete the JSON files.

    Thumbnails live in ``observations/<obs_id>/photo_<photo_id>.jpeg``; legacy flat
    ``photo_<photo_id>.jpeg`` files are moved there.
    """
    if cache_dir.exists():
        _migrate_obs_to_subdir(cache_dir)
    obs_cache_dir = cache_dir / "observations"
    legacy_photo_files = {p.stem.removeprefix("photo_"): p for p in cache_dir.glob("photo_*.jpeg")}

    migrated = 0
    batch: list[dict] = []
    migrated_paths: list[Path] = []
    for path, observations in _iter_legacy_observation_files(cache_dir, obs_cache_dir):
        if migrated == 0 and progress_cb:
            progress_cb("fetch", "Migrating cached observations to the compressed observation store")
        for obs in observations:
            obs_id = parse_observation_id(obs.get("id"))
            photos = obs.get("photos") if isinstance(obs.get("photos"), list) else []
            first_photo = photos[0] if photos and isinstance(photos[0], dict) else None
            photo_id = parse_observation_id(first_photo.get("id")) if first_photo else None
            legacy_photo = legacy_photo_files.get(str(photo_id)) if photo_id is not None else None
            if obs_id is not None and legacy_photo and legacy_photo.exists():
                thumbnail_path = obs_cache_dir / str(obs_id) / f"photo_{photo_id}.jpeg"
                if not thumbnail_path.exists():
                    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(legacy_photo), str(thumbnail_path))
        batch.extend(observations)
        migrated_paths.append(path)
        migrated += len(observations)
        if len(batch) >= 500:
            store.upsert_many(batch)
            batch.clear()
    if batch:
        store.upsert_many(batch)

    # Only delete the JSON files once everything is safely committed.
    for path in migrated_paths:
        try:
            path.unlink()
        except OSError:
            pass
        parent = path.parent
        if parent.parent == obs_cache_dir and not any(parent.iterdir()):
            parent.rmdir()
    return migrated


def fetch_observations(
    username: str,
    cache_dir: Path,
    *,
    requests_module,
    time_module,
    cache_ttl_s: int,
    per_page: int,
    progress_cb=None,
    cancel_check=None,
    request_headers: dict | None = None,
    use_cache: bool = True,
) -> list[dict]:
    if request_headers and use_cache:
        raise ValueError("Authenticated observations must not enter the public disk cache")
    with ObservationStore(cache_dir / OBSERVATION_STORE_FILENAME) if use_cache else nullcontext() as store:
        if store is not None:
            migrate_legacy_observation_cache(cache_dir, store, progress_cb=progress_cb)
        cached_count = store.count() if store is not None else 0
        latest_cached_update = store.latest_updated_at() if store is not None else None
        observations = []

        base_params = {
            "user_id": username,
            "per_page": per_page,
        }
        if not use_cache:
            # ID cursors avoid the API's page-depth limit on large accounts.
            base_params.update(order_by="id", order="asc")
        if cached_count and latest_cached_update is not None:
            base_params["updated_since"] = latest_cached_update.isoformat().replace("+00:00", "Z")
            if progress_cb:
                progress_cb("fetch", f"Fetching observations updated since latest cached update ({cached_count} cached)")
        elif cached_count:
            if progress_cb:
                progress_cb("fetch", "Cached observations found; refreshing from iNaturalist API")
        else:
            if progress_cb:
                progress_cb("fetch", "Fetching observations from iNaturalist API")

        fetched_count = 0
        page = 1
        while True:
            if cancel_check:
                cancel_check()
            query = {**base_params, "page": page if use_cache else 1}
            url = "https://api.inaturalist.org/v1/observations?" + urlencode(query)
            if progress_cb:
                progress_cb("fetch", f"Requesting iNaturalist page {page}")
            options = {"headers": request_headers, "allow_redirects": False} if request_headers else {}
            response = requests_module.get(url, timeout=30, **options)
            response.raise_for_status()
            data = response.json()
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list) or any(not isinstance(obs, dict) or parse_observation_id(obs.get("id")) is None for obs in results):
                raise ValueError("Unexpected iNaturalist observation response")
            if not use_cache and results:
                ids = [int(obs["id"]) for obs in results]
                if ids != sorted(set(ids)) or ids[0] <= int(base_params.get("id_above", 0)):
                    raise ValueError("iNaturalist observation pagination did not advance in ID order")
            # Persist each page as it arrives so an interrupted full fetch isn't wasted.
            if store is not None:
                store.upsert_many(results)
            else:
                observations.extend(results)
            fetched_count += len(results)
            if progress_cb:
                progress_cb("fetch", f"Fetched page {page} ({len(results)} results, total {fetched_count})")
            if len(results) < per_page:
                break
            if not use_cache:
                base_params["id_above"] = int(results[-1]["id"])
            page += 1
            time_module.sleep(0.35)

        if cancel_check:
            cancel_check()
        if store is None:
            return observations
        _prune_orphan_thumbnail_dirs(cache_dir / "observations", store.ids())
        if progress_cb:
            progress_cb("fetch", f"Loading {store.count()} observations from cache")
        return store.load_all()


def _prune_orphan_thumbnail_dirs(obs_cache_dir: Path, known_obs_ids: set[int]) -> None:
    if not obs_cache_dir.exists():
        return
    for child in obs_cache_dir.iterdir():
        if child.is_dir() and child.name.isdigit() and int(child.name) not in known_obs_ids:
            shutil.rmtree(child, ignore_errors=True)


def aspect_ratio(dimensions: tuple[int, int] | None) -> float | None:
    """Orientation-independent aspect ratio (long edge / short edge)."""
    if not dimensions:
        return None
    width, height = dimensions
    if width <= 0 or height <= 0:
        return None
    return max(width, height) / min(width, height)


# A wholly different shape (e.g. 1:1 vs 3:2) counts about as much as being ~4 hours apart.
LIBRARY_SEARCH_ASPECT_WEIGHT = 4.0


def library_search_priority(time_diff_s: float, local_aspect: float | None, obs_aspects: list[float]) -> float:
    """Ordering key for a whole-library search: lower values are compared first.

    Time distance is scaled logarithmically so photos from the same day come first
    but a misdated photo years away still gets compared eventually. Aspect ratio is
    only a soft hint because observation photos are often cropped.
    """
    priority = math.log1p(abs(time_diff_s) / 3600.0)
    if local_aspect and obs_aspects:
        priority += LIBRARY_SEARCH_ASPECT_WEIGHT * min(abs(math.log(local_aspect / aspect)) for aspect in obs_aspects)
    return priority


def library_search_representatives(photos) -> list:
    """One photo per version group (the newest version, which is most like an uploaded edit)."""
    best: dict[str, object] = {}
    for photo in photos:
        key = photo.version_group or f"id:{photo.id}"
        current = best.get(key)
        if current is None or photo.version_number > current.version_number:
            best[key] = photo
    return list(best.values())


def jpeg_dimensions(path: Path) -> tuple[int, int] | None:
    """Return (width, height) from a JPEG's SOF header without decoding pixels, or None."""
    try:
        with open(path, "rb") as handle:
            if handle.read(2) != b"\xff\xd8":
                return None
            while True:
                byte = handle.read(1)
                if not byte:
                    return None
                if byte != b"\xff":
                    continue
                marker = handle.read(1)
                while marker == b"\xff":
                    marker = handle.read(1)
                if not marker:
                    return None
                code = marker[0]
                if code == 0x01 or 0xD0 <= code <= 0xD8:
                    continue
                if code in (0xD9, 0xDA):
                    return None
                length_bytes = handle.read(2)
                if len(length_bytes) < 2:
                    return None
                length = int.from_bytes(length_bytes, "big")
                if code in _JPEG_SOF_MARKERS:
                    header = handle.read(5)
                    if len(header) < 5:
                        return None
                    height = int.from_bytes(header[1:3], "big")
                    width = int.from_bytes(header[3:5], "big")
                    return (width, height) if width and height else None
                handle.seek(length - 2, os.SEEK_CUR)
    except OSError:
        return None


def _reduced_imread_flag(path: Path, *, cv2_module, max_edge: int):
    """Pick the strongest libjpeg DCT downscale that still yields at least max_edge pixels."""
    dims = jpeg_dimensions(path)
    if dims is None:
        return cv2_module.IMREAD_COLOR
    edge = max(dims)
    for factor, flag_name in ((8, "IMREAD_REDUCED_COLOR_8"), (4, "IMREAD_REDUCED_COLOR_4"), (2, "IMREAD_REDUCED_COLOR_2")):
        if math.ceil(edge / factor) >= max_edge and hasattr(cv2_module, flag_name):
            return getattr(cv2_module, flag_name)
    return cv2_module.IMREAD_COLOR


def load_cv_image(path: Path, *, cv2_module, max_edge: int = 1024):
    flag = _reduced_imread_flag(path, cv2_module=cv2_module, max_edge=max_edge)
    image = cv2_module.imread(str(path), flag)
    if image is None and flag != cv2_module.IMREAD_COLOR:
        image = cv2_module.imread(str(path), cv2_module.IMREAD_COLOR)
    if image is None:
        return None
    h, w = image.shape[:2]
    edge = max(h, w)
    if edge > max_edge:
        scale = max_edge / edge
        image = cv2_module.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2_module.INTER_AREA)
    return image


def orb_similarity(a, b, *, cv2_module) -> float:
    gray_a = cv2_module.cvtColor(a, cv2_module.COLOR_BGR2GRAY)
    gray_b = cv2_module.cvtColor(b, cv2_module.COLOR_BGR2GRAY)
    orb = cv2_module.ORB_create(nfeatures=1500)
    kp1, des1 = orb.detectAndCompute(gray_a, None)
    kp2, des2 = orb.detectAndCompute(gray_b, None)
    if des1 is None or des2 is None or not kp1 or not kp2:
        return 0.0

    matcher = cv2_module.BFMatcher(cv2_module.NORM_HAMMING, crossCheck=False)
    knn = matcher.knnMatch(des1, des2, k=2)

    good = []
    for pair in knn:
        if len(pair) < 2:
            continue
        first, second = pair
        if first.distance < 0.75 * second.distance:
            good.append(first)
    return len(good) / max(len(kp1), len(kp2), 1)


def histogram_similarity(a, b, *, cv2_module) -> float:
    hsv_a = cv2_module.cvtColor(a, cv2_module.COLOR_BGR2HSV)
    hsv_b = cv2_module.cvtColor(b, cv2_module.COLOR_BGR2HSV)
    hist_a = cv2_module.calcHist([hsv_a], [0, 1, 2], None, [8, 8, 8], [0, 180, 0, 256, 0, 256])
    hist_b = cv2_module.calcHist([hsv_b], [0, 1, 2], None, [8, 8, 8], [0, 180, 0, 256, 0, 256])
    cv2_module.normalize(hist_a, hist_a)
    cv2_module.normalize(hist_b, hist_b)
    # Compare flattened histograms: OpenCV 4.13's HISTCMP_CORREL returns wrong values
    # (often < -1 or NaN) for 3-D histograms, while the 1-D form is correct.
    corr = cv2_module.compareHist(hist_a.reshape(-1, 1), hist_b.reshape(-1, 1), cv2_module.HISTCMP_CORREL)
    if not math.isfinite(corr):
        corr = 0.0
    corr = max(-1.0, min(1.0, corr))
    return (corr + 1.0) / 2.0


def edge_overlap_similarity(a, b, *, cv2_module) -> float:
    gray_a = cv2_module.cvtColor(a, cv2_module.COLOR_BGR2GRAY)
    gray_b = cv2_module.cvtColor(b, cv2_module.COLOR_BGR2GRAY)
    edge_a = cv2_module.Canny(gray_a, 50, 150)
    edge_b = cv2_module.Canny(gray_b, 50, 150)
    union = ((edge_a > 0) | (edge_b > 0)).sum()
    if union == 0:
        return 0.0
    overlap = ((edge_a > 0) & (edge_b > 0)).sum()
    return float(overlap) / float(union)


def pair_similarity_score(obs_img, local_img, *, cv2_module) -> float:
    h, w = obs_img.shape[:2]
    resized_local = cv2_module.resize(local_img, (w, h), interpolation=cv2_module.INTER_AREA)
    score = (
        0.60 * orb_similarity(obs_img, resized_local, cv2_module=cv2_module)
        + 0.25 * histogram_similarity(obs_img, resized_local, cv2_module=cv2_module)
        + 0.15 * edge_overlap_similarity(obs_img, resized_local, cv2_module=cv2_module)
    )
    if math.isnan(score) or math.isinf(score):
        return 0.0
    return float(score)


def score_candidates_per_photo(
    observation_photo_paths,
    candidates,
    *,
    load_cv_image_fn,
    pair_similarity_score_fn,
    score_cache=None,
):
    """Score every candidate against each observation photo.

    Returns one ``[(candidate_id, score), ...]`` list per observation photo, best
    first. Each local image is decoded at most once however many observation
    photos there are, and with a ``score_cache`` unchanged pairs need no decoding.
    """
    obs_images: dict[int, object] = {}

    def _obs_image(index: int):
        if index not in obs_images:
            obs_images[index] = load_cv_image_fn(observation_photo_paths[index])
        return obs_images[index]

    per_photo: list[list[tuple[int, float]]] = [[] for _ in observation_photo_paths]
    for candidate in candidates:
        missing: list[tuple[int, object]] = []
        for index, obs_path in enumerate(observation_photo_paths):
            key = score_cache.key(obs_path, candidate.path) if score_cache is not None else None
            cached = score_cache.get(key) if score_cache is not None else None
            if cached is None:
                missing.append((index, key))
            else:
                per_photo[index].append((candidate.id, cached))
        if not missing:
            continue
        available = [(index, key) for index, key in missing if _obs_image(index) is not None]
        local_img = load_cv_image_fn(candidate.path) if available else None
        computed: set[int] = set()
        if local_img is not None:
            for index, key in available:
                score = pair_similarity_score_fn(obs_images[index], local_img)
                if score_cache is not None:
                    score_cache.put(key, score)
                per_photo[index].append((candidate.id, score))
                computed.add(index)
        for index, _key in missing:
            if index not in computed:
                per_photo[index].append((candidate.id, 0.0))
    for scored in per_photo:
        scored.sort(key=lambda item: item[1], reverse=True)
    return per_photo


def score_candidate_batches(
    jobs,
    *,
    load_cv_image_fn,
    pair_similarity_score_fn,
    score_cache=None,
    workers: int = DEFAULT_CV_MAX_WORKERS,
    progress=None,
    cancel_check=None,
):
    """Score many ``(observation_photo_paths, candidates)`` jobs, reading each local image once.

    Jobs whose candidate windows overlap share local photos; working local-photo
    first means every photo is read from disk once however many jobs want it.
    Returns, per job, the same per-photo lists as ``score_candidates_per_photo``.
    ``progress(done, total)`` counts local photos that had to be loaded.
    """
    results = [[[] for _ in paths] for paths, _candidates in jobs]
    pending: dict[str, list[tuple]] = defaultdict(list)
    for job_index, (paths, candidates) in enumerate(jobs):
        for candidate in candidates:
            for photo_index, obs_path in enumerate(paths):
                key = score_cache.key(obs_path, candidate.path) if score_cache is not None else None
                cached = score_cache.get(key) if score_cache is not None else None
                if cached is None:
                    pending[str(candidate.path)].append((job_index, photo_index, candidate.id, str(obs_path), key))
                else:
                    results[job_index][photo_index].append((candidate.id, cached))

    if pending:
        obs_paths_needed = sorted({entry[3] for entries in pending.values() for entry in entries})
        obs_images = dict(zip(obs_paths_needed, map(load_cv_image_fn, obs_paths_needed)))
        cancelled = threading.Event()

        def _work(local_path: str) -> list[tuple]:
            if cancelled.is_set():
                return []
            local_img = load_cv_image_fn(local_path)
            scored = []
            for job_index, photo_index, candidate_id, obs_path, key in pending[local_path]:
                obs_img = obs_images[obs_path]
                if local_img is None or obs_img is None:
                    score = 0.0
                else:
                    score = pair_similarity_score_fn(obs_img, local_img)
                    if score_cache is not None:
                        score_cache.put(key, score)
                scored.append((job_index, photo_index, candidate_id, score))
            return scored

        # Path order keeps neighbouring files together, which helps disk read-ahead.
        local_paths = sorted(pending)
        total = len(local_paths)
        executor = ThreadPoolExecutor(max_workers=max(1, min(workers, total)), thread_name_prefix="cv-batch")
        try:
            futures = [executor.submit(_work, local_path) for local_path in local_paths]
            for done, future in enumerate(as_completed(futures), start=1):
                if cancel_check:
                    try:
                        cancel_check()
                    except BaseException:
                        cancelled.set()
                        raise
                for job_index, photo_index, candidate_id, score in future.result():
                    results[job_index][photo_index].append((candidate_id, score))
                if progress:
                    progress(done, total)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    for per_photo in results:
        for scored in per_photo:
            scored.sort(key=lambda item: item[1], reverse=True)
    return results


def score_candidates(
    observation_photo_paths,
    candidates,
    *,
    load_cv_image_fn,
    pair_similarity_score_fn,
    score_cache=None,
):
    """Score each candidate by its best CV similarity to any observation photo."""
    per_photo = score_candidates_per_photo(
        observation_photo_paths,
        candidates,
        load_cv_image_fn=load_cv_image_fn,
        pair_similarity_score_fn=pair_similarity_score_fn,
        score_cache=score_cache,
    )
    best = {candidate.id: 0.0 for candidate in candidates}
    for scored in per_photo:
        for candidate_id, score in scored:
            best[candidate_id] = max(best[candidate_id], score)
    return sorted(best.items(), key=lambda item: item[1], reverse=True)


def build_observation_records(
    raw_observations: list[dict],
    *,
    parse_observation_datetime_fn,
    build_observation_metadata_xmp_fn,
    build_observation_template_context_fn,
    observation_data_cls,
):
    obs_records = []
    obs_photo_by_id = {}

    for obs in raw_observations:
        observed = parse_observation_datetime_fn(obs.get("time_observed_at"))
        if observed is None:
            continue
        obs_id = int(obs["id"])
        photos = obs.get("photos", [])
        for photo in photos:
            if "id" in photo:
                photo_with_obs = dict(photo)
                photo_with_obs["observation_id"] = obs_id
                obs_photo_by_id[int(photo["id"])] = photo_with_obs
        uri = obs.get("uri") or f"https://www.inaturalist.org/observations/{obs_id}"
        metadata_xmp = build_observation_metadata_xmp_fn(obs, obs_id=obs_id, uri=uri, observed=observed)
        template_context = build_observation_template_context_fn(obs, obs_id=obs_id, uri=uri, observed=observed)
        obs_records.append(
            observation_data_cls(
                obs_id=obs_id,
                observed_at=observed,
                photos=photos,
                uri=uri,
                metadata_xmp=metadata_xmp,
                template_context=template_context,
                has_sounds=bool(obs.get("sounds")),
            )
        )

    obs_records.sort(key=lambda item: item.observed_at)
    return obs_records, obs_photo_by_id


def best_cv_scores_by_group(match, by_id: dict[int, object]) -> list[tuple[str, float, int]]:
    """Return best CV scores per version group as (group_key, score, representative_photo_id)."""
    best_by_group: dict[str, tuple[float, int]] = {}
    valid_candidate_ids = set(match.candidate_photo_ids)
    for photo_id, score in match.scored_candidates:
        if photo_id not in valid_candidate_ids:
            continue
        photo = by_id.get(photo_id)
        if photo is None:
            continue
        group_key = photo.version_group or str(photo_id)
        current = best_by_group.get(group_key)
        if current is None or score > current[0]:
            best_by_group[group_key] = (score, photo_id)

    grouped = [(group_key, best_score, rep_photo_id) for group_key, (best_score, rep_photo_id) in best_by_group.items()]
    grouped.sort(key=lambda item: item[1], reverse=True)
    return grouped


def enforce_unique_selected_groups(matches: list[object], by_id: dict[int, object]) -> None:
    """Ensure no image version group is selected by more than one observation."""
    group_to_obs_ids: dict[str, set[int]] = defaultdict(set)
    obs_to_match: dict[int, object] = {match.observation.obs_id: match for match in matches}
    for match in matches:
        for photo_id in match.selected_photo_ids:
            photo = by_id.get(photo_id)
            group_key = photo.version_group if photo and photo.version_group else str(photo_id)
            group_to_obs_ids[group_key].add(match.observation.obs_id)

    for group_key, obs_ids in group_to_obs_ids.items():
        if len(obs_ids) <= 1:
            continue

        # If exactly one observation already has this photo group tagged,
        # keep it selected and clear the others. This keeps analyze/save cycles
        # idempotent when one local photo can legitimately match multiple obs.
        tagged_obs_ids: list[int] = []
        for obs_id in obs_ids:
            match = obs_to_match.get(obs_id)
            if match and match.has_existing_tag:
                tagged_obs_ids.append(obs_id)

        if len(tagged_obs_ids) == 1:
            keep_obs_id = tagged_obs_ids[0]
            for obs_id in obs_ids:
                if obs_id == keep_obs_id:
                    continue
                match = obs_to_match.get(obs_id)
                if match is None:
                    continue
                match.status = "ambiguous"
                match.selected_photo_ids = {
                    photo_id
                    for photo_id in match.selected_photo_ids
                    if not (
                        (by_id.get(photo_id) and ((by_id[photo_id].version_group or str(photo_id)) == group_key))
                        or (not by_id.get(photo_id) and str(photo_id) == group_key)
                    )
                }
            continue

        # If no unique existing tag can disambiguate, clear colliding selections.
        for obs_id in obs_ids:
            match = obs_to_match.get(obs_id)
            if match is None:
                continue
            match.status = "ambiguous"
            match.selected_photo_ids = {
                photo_id
                for photo_id in match.selected_photo_ids
                if not (
                    (by_id.get(photo_id) and ((by_id[photo_id].version_group or str(photo_id)) == group_key))
                    or (not by_id.get(photo_id) and str(photo_id) == group_key)
                )
            }


def _group_key(photo_id: int, by_id: dict[int, object]) -> str:
    photo = by_id.get(photo_id)
    return (photo.version_group if photo is not None and photo.version_group else "") or str(photo_id)


def merge_photo_scores(per_photo_scored: list[list[tuple[int, float]]]) -> list[tuple[int, float]]:
    """Combine per-observation-photo scores into one best score per local photo."""
    best: dict[int, float] = {}
    for scored in per_photo_scored:
        for photo_id, score in scored:
            if score > best.get(photo_id, -1.0):
                best[photo_id] = score
    return sorted(best.items(), key=lambda item: item[1], reverse=True)


def select_groups_per_photo(
    per_photo_scored: list[list[tuple[int, float]]],
    by_id: dict[int, object],
    *,
    score_threshold: float,
    margin_threshold: float,
) -> dict[str, int]:
    """Pick a local version group for each observation photo that clearly matches one.

    Margins are measured between version groups, so near-identical versions of the
    same photo (original + edit) don't make a clear match look ambiguous. Returns
    {group_key: representative_photo_id} for every confidently matched group.
    """
    chosen: dict[str, int] = {}
    for scored in per_photo_scored:
        best_by_group: dict[str, tuple[float, int]] = {}
        for photo_id, score in scored:
            key = _group_key(photo_id, by_id)
            current = best_by_group.get(key)
            if current is None or score > current[0]:
                best_by_group[key] = (score, photo_id)
        ranked = sorted(best_by_group.items(), key=lambda item: item[1][0], reverse=True)
        if not ranked:
            continue
        top_key, (top_score, top_photo_id) = ranked[0]
        second_score = ranked[1][1][0] if len(ranked) > 1 else 0.0
        if top_score >= score_threshold and (top_score - second_score) >= margin_threshold:
            chosen.setdefault(top_key, top_photo_id)
    return chosen


def _run_parallel_scoring(jobs, score_fn, *, workers, phase, describe, progress_cb, cancel_check):
    """Run ``score_fn(*job_args)`` for each (key, job_args) in parallel, reporting progress."""
    results: dict[object, object] = {}
    total = len(jobs)
    if not total:
        return results
    executor = ThreadPoolExecutor(max_workers=max(1, min(workers, total)), thread_name_prefix=f"{phase}-score")
    try:
        futures = {executor.submit(score_fn, *job_args): key for key, job_args in jobs}
        for done, future in enumerate(as_completed(futures), start=1):
            if cancel_check:
                cancel_check()
            results[futures[future]] = future.result()
            if progress_cb:
                progress_cb(phase, describe(done, total), current=done, total=total)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return results


def format_time_offset(seconds: float) -> str:
    sign = "+" if seconds >= 0 else "−"
    seconds = abs(int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours >= 48:
        days, hours = divmod(hours, 24)
        return f"{sign}{days}d{hours}h" if hours else f"{sign}{days}d"
    if hours and minutes:
        return f"{sign}{hours}h{minutes:02d}m"
    if hours:
        return f"{sign}{hours}h"
    if minutes:
        return f"{sign}{minutes}m"
    return f"{sign}{seconds}s"


def _offset_detail(offsets) -> str:
    labels = list(dict.fromkeys(format_time_offset(offset) for offset in sorted(offsets)))
    return f"Local photo time {', '.join(labels)} from iNaturalist time"


# Fallback tiers for observations whose time matches no local photo.
# Timezone mistakes shift the clock by whole (or half) hours but keep the minute.
TIMEZONE_SHIFT_STEP_S = 1800
TIMEZONE_SHIFT_MAX_S = 14 * 3600
NEARBY_WINDOW_S = 24 * 3600
# Measured on a real library: true matches score ~0.87 median (10th percentile ~0.79)
# while the best unrelated photo in a ±24h window peaked at ~0.27, so the wider
# tiers demand more than the same-minute tier does.
SHIFT_CV_SCORE_THRESHOLD = 0.4
SHIFT_CV_MARGIN_THRESHOLD = 0.1
NEARBY_CV_SCORE_THRESHOLD = 0.45
NEARBY_CV_MARGIN_THRESHOLD = 0.15
# How many low-confidence nearby photos to offer as suggestions per observation.
NEARBY_SUGGESTION_LIMIT = 6

AUDIO_ONLY_STATUS = "audio-only"
NO_PHOTOS_STATUS = "no-photos"
PHOTOLESS_STATUSES = frozenset({AUDIO_ONLY_STATUS, NO_PHOTOS_STATUS})


# Observations with several photos are often matched by only one of them: the
# others were taken a little later, outside the matched minute. On a real library
# 98% of those companion photos were within ±5 minutes and all within ±10.
COMPANION_WINDOW_S = 10 * 60


def _claimed_groups(matches, by_id) -> dict[str, int]:
    """Map each photo group already selected for, or tagged with, an observation to its owner."""
    claimed: dict[str, int] = {}
    for match in matches:
        for photo_id in match.selected_photo_ids:
            claimed[_group_key(photo_id, by_id)] = match.observation.obs_id
    for photo in by_id.values():
        if photo.existing_observation_id is not None:
            claimed.setdefault(_group_key(photo.id, by_id), photo.existing_observation_id)
    return claimed


def _match_companion_photos(
    matches,
    by_id,
    *,
    group_versions,
    score_jobs_fn,
    expand_with_version_siblings_fn,
    progress_cb,
    cancel_check,
) -> None:
    """Find local photos for the observation photos a matched observation hasn't got yet.

    Only observations with more photos than selected photo groups are checked. Each
    observation photo is compared with the selected photos and the unclaimed photos
    taken within COMPANION_WINDOW_S of them; one whose best match is a new group
    (by a clear margin) adds that group to the selection.
    """
    claimed = _claimed_groups(matches, by_id)
    photos_by_time = sorted(by_id.values(), key=lambda photo: photo.timestamp)
    times = [photo.timestamp for photo in photos_by_time]
    jobs = []
    for match in matches:
        if not match.selected_photo_ids or match.status in PHOTOLESS_STATUSES:
            continue
        selected_groups = {_group_key(photo_id, by_id) for photo_id in match.selected_photo_ids if photo_id in by_id}
        if len(match.observation.photos) <= len(selected_groups):
            continue
        obs_id = match.observation.obs_id
        selected_times = [by_id[photo_id].timestamp for photo_id in match.selected_photo_ids if photo_id in by_id]
        window = photos_by_time[
            bisect_left(times, min(selected_times) - COMPANION_WINDOW_S) : bisect_right(
                times, max(selected_times) + COMPANION_WINDOW_S
            )
        ]
        candidates = [photo for photo in window if claimed.get(_group_key(photo.id, by_id), obs_id) == obs_id]
        if len(candidates) > len(match.selected_photo_ids):
            jobs.append((obs_id, (match.observation, candidates)))
    if not jobs:
        return

    if progress_cb:
        progress_cb(
            "cv-companion",
            f"Looking for the other photos of {len(jobs)} matched observations among photos taken within "
            f"±{COMPANION_WINDOW_S // 60} minutes",
            current=0,
            total=len(jobs),
        )
    scores = score_jobs_fn(jobs, "cv-companion", "photos taken near matched observations")
    if cancel_check:
        cancel_check()
    matches_by_id = {match.observation.obs_id: match for match in matches}
    added_photos = 0
    added_obs = 0
    for obs_id, (_obs, candidates) in jobs:
        match = matches_by_id[obs_id]
        selected_groups = {_group_key(photo_id, by_id) for photo_id in match.selected_photo_ids if photo_id in by_id}
        per_photo = scores.get(obs_id, [])
        new_groups: set[str] = set()
        for scored in per_photo:
            # Judge each observation photo on its own, so one already covered by the
            # selection can't pull in a different photo.
            chosen = select_groups_per_photo(
                [scored],
                by_id,
                score_threshold=NEARBY_CV_SCORE_THRESHOLD,
                margin_threshold=NEARBY_CV_MARGIN_THRESHOLD,
            )
            for key in chosen:
                if key not in selected_groups and claimed.get(key, obs_id) == obs_id:
                    new_groups.add(key)
        if not new_groups:
            continue
        added = {photo.id for photo in candidates if _group_key(photo.id, by_id) in new_groups}
        if group_versions:
            added = expand_with_version_siblings_fn(added, by_id)
        _add_candidates(match, added, per_photo)
        match.selected_photo_ids = set(match.selected_photo_ids) | added
        note = (
            f"{len(new_groups)} more photo{'s' if len(new_groups) != 1 else ''} found within "
            f"±{COMPANION_WINDOW_S // 60} min of the matched one"
        )
        match.detail = f"{match.detail}; {note}" if getattr(match, "detail", None) else note
        for key in new_groups:
            claimed[key] = obs_id
        added_photos += len(new_groups)
        added_obs += 1
    if progress_cb:
        progress_cb(
            "cv-companion",
            f"Found {added_photos} more photos for {added_obs} of {len(jobs)} matched observations",
            current=len(jobs),
            total=len(jobs),
        )


def _needs_wider_search(match, cv_score_threshold: float) -> bool:
    if match.selected_photo_ids or match.has_existing_tag or not match.observation.photos:
        return False
    if match.status in PHOTOLESS_STATUSES:
        return False
    # Same-minute photos that all look unrelated are as good as no candidates.
    best = max((score for _photo_id, score in match.scored_candidates), default=0.0)
    return not match.candidate_photo_ids or best < cv_score_threshold


def _accept_wider_match(match, chosen_groups, candidates, per_photo, by_id, *, group_versions, expand_fn, status, detail):
    chosen_photos = [photo for photo in candidates if _group_key(photo.id, by_id) in chosen_groups]
    selected = {photo.id for photo in chosen_photos}
    if group_versions:
        selected = expand_fn(selected, by_id)
    _add_candidates(match, selected, per_photo)
    match.selected_photo_ids = selected
    match.status = status
    match.detail = detail


def _add_candidates(match, photo_ids, per_photo) -> None:
    scores = dict(merge_photo_scores(per_photo))
    known = set(match.candidate_photo_ids)
    for photo_id in sorted(photo_ids):
        if photo_id not in known:
            match.candidate_photo_ids.append(photo_id)
            known.add(photo_id)
    merged = dict(match.scored_candidates)
    for photo_id in photo_ids:
        if photo_id in scores:
            merged[photo_id] = max(merged.get(photo_id, 0.0), scores[photo_id])
    match.scored_candidates = sorted(merged.items(), key=lambda item: item[1], reverse=True)


def _match_with_wider_time_windows(
    matches,
    by_minute,
    by_id,
    *,
    group_versions,
    score_jobs_fn,
    expand_with_version_siblings_fn,
    cv_score_threshold,
    progress_cb,
    cancel_check,
) -> None:
    """Find photos for observations whose recorded time matched nothing useful.

    Tier 1 looks for timezone mistakes: photos at the same minute but a whole or
    half number of hours away. Tier 2 compares every photo within ±24 hours and
    auto-accepts only very clear matches, offering the best others as suggestions.
    """
    wide = [match for match in matches if _needs_wider_search(match, cv_score_threshold)]
    if not wide:
        return

    claimed = _claimed_groups(matches, by_id)

    def _available(photo, obs_id: int) -> bool:
        owner = claimed.get(_group_key(photo.id, by_id))
        return owner is None or owner == obs_id

    # Tier 1: timezone-shifted times.
    shift_jobs = []
    shift_candidates: dict[int, tuple[list, dict[int, float]]] = {}
    max_steps = TIMEZONE_SHIFT_MAX_S // TIMEZONE_SHIFT_STEP_S
    for match in wide:
        obs = match.observation
        base = obs.observed_at.replace(second=0).timestamp()
        candidates = []
        offsets: dict[int, float] = {}
        for step in range(-max_steps, max_steps + 1):
            if step == 0:
                continue
            offset = step * TIMEZONE_SHIFT_STEP_S
            for photo in by_minute.get(base + offset, ()):
                if _available(photo, obs.obs_id):
                    candidates.append(photo)
                    offsets[photo.id] = offset
        if candidates:
            shift_candidates[obs.obs_id] = (candidates, offsets)
            shift_jobs.append((obs.obs_id, (obs, candidates)))

    if shift_jobs:
        if progress_cb:
            progress_cb(
                "cv-shift",
                f"Checking {len(shift_jobs)} unmatched observations for photos with timezone-shifted times",
                current=0,
                total=len(shift_jobs),
            )
        shift_scores = score_jobs_fn(shift_jobs, "cv-shift", "timezone-shifted local photos")
        accepted = 0
        for match in wide:
            obs_id = match.observation.obs_id
            if obs_id not in shift_scores:
                continue
            candidates, offsets = shift_candidates[obs_id]
            candidates = [photo for photo in candidates if _available(photo, obs_id)]
            per_photo = [
                [(photo_id, score) for photo_id, score in scored if _available(by_id[photo_id], obs_id)]
                for scored in shift_scores[obs_id]
            ]
            chosen = select_groups_per_photo(
                per_photo,
                by_id,
                score_threshold=SHIFT_CV_SCORE_THRESHOLD,
                margin_threshold=SHIFT_CV_MARGIN_THRESHOLD,
            )
            if not chosen:
                continue
            shifts = sorted({offsets[photo_id] for photo_id in chosen.values()})
            _accept_wider_match(
                match,
                chosen,
                candidates,
                per_photo,
                by_id,
                group_versions=group_versions,
                expand_fn=expand_with_version_siblings_fn,
                status="auto-shift",
                detail=_offset_detail(shifts),
            )
            for key in chosen:
                claimed[key] = obs_id
            accepted += 1
        if progress_cb:
            progress_cb(
                "cv-shift",
                f"Matched {accepted} of {len(shift_jobs)} observations via timezone-shifted photo times",
                current=len(shift_jobs),
                total=len(shift_jobs),
            )

    # Tier 2: anything within ±24 hours.
    remaining = [match for match in wide if not match.selected_photo_ids]
    if not remaining:
        return
    photos_by_time = sorted(by_id.values(), key=lambda photo: photo.timestamp)
    times = [photo.timestamp for photo in photos_by_time]
    nearby_jobs = []
    nearby_candidates: dict[int, list] = {}
    for match in remaining:
        obs = match.observation
        t0 = obs.observed_at.timestamp()
        window = photos_by_time[bisect_left(times, t0 - NEARBY_WINDOW_S) : bisect_right(times, t0 + NEARBY_WINDOW_S)]
        candidates = [photo for photo in window if _available(photo, obs.obs_id)]
        if candidates:
            nearby_candidates[obs.obs_id] = candidates
            nearby_jobs.append((obs.obs_id, (obs, candidates)))
    if not nearby_jobs:
        return

    total_pairs = sum(len(candidates) * len(obs.photos) for _key, (obs, candidates) in nearby_jobs)
    if progress_cb:
        progress_cb(
            "cv-nearby",
            f"Comparing {len(nearby_jobs)} still-unmatched observations with photos taken within ±24h "
            f"(up to {total_pairs} image pairs)",
            current=0,
            total=len(nearby_jobs),
        )
    nearby_scores = score_jobs_fn(nearby_jobs, "cv-nearby", "local photos taken within ±24h")
    accepted = 0
    suggested = 0
    for match in remaining:
        obs = match.observation
        if obs.obs_id not in nearby_scores:
            continue
        candidates = [photo for photo in nearby_candidates[obs.obs_id] if _available(photo, obs.obs_id)]
        per_photo = [
            [(photo_id, score) for photo_id, score in scored if _available(by_id[photo_id], obs.obs_id)]
            for scored in nearby_scores[obs.obs_id]
        ]
        chosen = select_groups_per_photo(
            per_photo,
            by_id,
            score_threshold=NEARBY_CV_SCORE_THRESHOLD,
            margin_threshold=NEARBY_CV_MARGIN_THRESHOLD,
        )
        if chosen:
            offsets = sorted({by_id[photo_id].timestamp - obs.observed_at.timestamp() for photo_id in chosen.values()})
            _accept_wider_match(
                match,
                chosen,
                candidates,
                per_photo,
                by_id,
                group_versions=group_versions,
                expand_fn=expand_with_version_siblings_fn,
                status="auto-nearby",
                detail=_offset_detail(offsets),
            )
            for key in chosen:
                claimed[key] = obs.obs_id
            accepted += 1
            continue

        # Not confident enough to select anything, but plausible photos are worth showing.
        suggestion_groups: list[str] = []
        for photo_id, score in merge_photo_scores(per_photo):
            if score < cv_score_threshold or len(suggestion_groups) >= NEARBY_SUGGESTION_LIMIT:
                break
            key = _group_key(photo_id, by_id)
            if key not in suggestion_groups:
                suggestion_groups.append(key)
        if suggestion_groups:
            suggestion_ids = {photo.id for photo in candidates if _group_key(photo.id, by_id) in suggestion_groups}
            _add_candidates(match, suggestion_ids, per_photo)
            match.status = "ambiguous"
            match.detail = f"{len(suggestion_groups)} possible match(es) found within ±24h"
            suggested += 1
    if progress_cb:
        progress_cb(
            "cv-nearby",
            f"Matched {accepted} and found suggestions for {suggested} of {len(nearby_jobs)} observations "
            "using photos within ±24h",
            current=len(nearby_jobs),
            total=len(nearby_jobs),
        )


def match_observations(
    observations: list[object],
    by_second: dict[float, list[object]],
    by_minute: dict[float, list[object]],
    by_id: dict[int, object],
    *,
    match_result_cls,
    group_versions: bool,
    cache_dir: Path,
    cache_observation_photo_fn,
    score_candidates_fn,
    expand_with_version_siblings_fn,
    metadata_refresh_check_fn,
    cv_score_threshold: float,
    cv_margin_threshold: float,
    progress_cb=None,
    cancel_check=None,
    cv_max_workers: int | None = None,
    score_candidates_per_photo_fn=None,
    score_candidate_batches_fn=None,
) -> tuple[list[object], set[int]]:
    obs_count_by_second: dict[float, int] = defaultdict(int)
    obs_count_by_minute: dict[float, int] = defaultdict(int)
    tagged_photos_by_obs_id: dict[int, list[object]] = defaultdict(list)
    for photo in by_id.values():
        if photo.existing_observation_id is not None:
            tagged_photos_by_obs_id[photo.existing_observation_id].append(photo)
    for obs in observations:
        # Photo-less observations can't compete for local photos, so they mustn't
        # make an observation sharing their time look ambiguous.
        if not obs.photos:
            continue
        obs_count_by_second[obs.observed_at.timestamp()] += 1
        obs_count_by_minute[obs.observed_at.replace(second=0).timestamp()] += 1

    matches: list[object] = []
    pending_cv: list[tuple[int, object, list[object], bool]] = []
    for idx, obs in enumerate(observations, start=1):
        if cancel_check:
            cancel_check()
        if progress_cb and (idx % 20 == 0 or idx == 1 or idx == len(observations)):
            progress_cb(
                "match",
                f"Processing observation {idx}/{len(observations)}",
                current=idx,
                total=len(observations),
            )

        exact = by_second.get(obs.observed_at.timestamp(), [])
        minute = by_minute.get(obs.observed_at.replace(second=0).timestamp(), [])

        exact_groups = {photo.exif_datetime_raw for photo in exact}
        minute_groups = {photo.exif_datetime_raw for photo in minute}
        has_existing_tag = any(photo.existing_observation_id == obs.obs_id for photo in (exact or minute))

        if not obs.photos:
            # Nothing to compare against: only keep photos the user already tagged.
            nearby = exact or minute
            selected = {photo.id for photo in nearby if photo.existing_observation_id == obs.obs_id}
            if group_versions and selected:
                selected = expand_with_version_siblings_fn(selected, by_id)
            matches.append(
                match_result_cls(
                    observation=obs,
                    status=AUDIO_ONLY_STATUS if getattr(obs, "has_sounds", False) else NO_PHOTOS_STATUS,
                    candidate_photo_ids=[photo.id for photo in nearby],
                    scored_candidates=[(photo.id, 0.0) for photo in nearby],
                    has_existing_tag=has_existing_tag,
                    selected_photo_ids=selected,
                )
            )
            continue

        tagged_photos = tagged_photos_by_obs_id.get(obs.obs_id, [])
        if tagged_photos:
            selected = {photo.id for photo in tagged_photos}
            if group_versions:
                selected = expand_with_version_siblings_fn(selected, by_id)
            matches.append(
                match_result_cls(
                    observation=obs,
                    status="auto-tagged",
                    candidate_photo_ids=sorted(selected),
                    scored_candidates=[(photo_id, 1.0) for photo_id in sorted(selected)],
                    has_existing_tag=True,
                    selected_photo_ids=selected,
                    detail="Matched from existing observation metadata",
                )
            )
            continue

        # iNaturalist often stores observed_at without reliable seconds, so multiple
        # observations can collapse to the same second. In that case we must not
        # short-circuit to auto-exact; leave them for minute-level CV disambiguation.
        if exact and len(exact_groups) == 1 and obs_count_by_second[obs.observed_at.timestamp()] == 1:
            selected = {photo.id for photo in exact}
            if group_versions:
                selected = expand_with_version_siblings_fn(selected, by_id)
            matches.append(
                match_result_cls(
                    observation=obs,
                    status="auto-exact",
                    candidate_photo_ids=[photo.id for photo in exact],
                    scored_candidates=[(photo.id, 1.0) for photo in exact],
                    has_existing_tag=has_existing_tag,
                    selected_photo_ids=selected,
                )
            )
            continue

        if minute and len(minute_groups) == 1 and obs_count_by_minute[obs.observed_at.replace(second=0).timestamp()] == 1:
            selected = {photo.id for photo in minute}
            if group_versions:
                selected = expand_with_version_siblings_fn(selected, by_id)
            matches.append(
                match_result_cls(
                    observation=obs,
                    status="auto-minute",
                    candidate_photo_ids=[photo.id for photo in minute],
                    scored_candidates=[(photo.id, 1.0) for photo in minute],
                    has_existing_tag=has_existing_tag,
                    selected_photo_ids=selected,
                )
            )
            continue

        if not minute:
            matches.append(
                match_result_cls(
                    observation=obs,
                    status="no-match",
                    candidate_photo_ids=[],
                    scored_candidates=[],
                    has_existing_tag=has_existing_tag,
                    selected_photo_ids=set(),
                )
            )
            continue

        if has_existing_tag:
            tagged_photos = [p for p in minute if p.existing_observation_id == obs.obs_id]
            if tagged_photos:
                selected = {p.id for p in tagged_photos}
                if group_versions:
                    selected = expand_with_version_siblings_fn(selected, by_id)
                matches.append(
                    match_result_cls(
                        observation=obs,
                        status="auto-minute",
                        candidate_photo_ids=[photo.id for photo in minute],
                        scored_candidates=[(photo.id, 1.0) if photo.id in selected else (photo.id, 0.0) for photo in minute],
                        has_existing_tag=True,
                        selected_photo_ids=selected,
                    )
                )
                continue

        # For same-minute ambiguity (including second-collapsed observations),
        # fuzzy CV matching is the intended fallback. Scoring is deferred so all
        # CV observations can be compared in parallel after the cheap datetime pass.
        matches.append(None)
        pending_cv.append((len(matches) - 1, obs, minute, has_existing_tag))

    obs_paths_cache: dict[int, list[Path]] = {}
    obs_paths_lock = threading.Lock()

    def _observation_photo_paths(obs) -> list[Path]:
        with obs_paths_lock:
            cached = obs_paths_cache.get(obs.obs_id)
        if cached is not None:
            return cached
        paths = []
        for photo in obs.photos:
            try:
                photo_with_obs = dict(photo)
                photo_with_obs["observation_id"] = obs.obs_id
                paths.append(cache_observation_photo_fn(photo_with_obs, cache_dir))
            except Exception:
                continue
        with obs_paths_lock:
            obs_paths_cache[obs.obs_id] = paths
        return paths

    def _score_observation(obs, candidates) -> list[list[tuple[int, float]]]:
        # Score each observation photo separately: an observation's photos are
        # usually different shots, each matching a different local photo.
        paths = _observation_photo_paths(obs)
        if score_candidates_per_photo_fn is not None:
            return score_candidates_per_photo_fn(paths, candidates) if paths else []
        return [score_candidates_fn([path], candidates) for path in paths]

    workers = cv_max_workers or DEFAULT_CV_MAX_WORKERS

    def _score_jobs(jobs, phase: str, label: str) -> dict:
        """Score (key, (obs, candidates)) jobs, sharing local image reads when possible."""
        if score_candidate_batches_fn is None:
            return _run_parallel_scoring(
                jobs,
                _score_observation,
                workers=workers,
                phase=phase,
                describe=lambda done, total: f"Compared {label} for {done}/{total} unmatched observations",
                progress_cb=progress_cb,
                cancel_check=cancel_check,
            )

        def _progress(done: int, total: int) -> None:
            if progress_cb:
                progress_cb(phase, f"Compared {done}/{total} {label}", current=done, total=total)

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"{phase}-fetch") as executor:
            paths_per_job = list(executor.map(_observation_photo_paths, [obs for _key, (obs, _c) in jobs]))
        batch = [(paths, candidates) for paths, (_key, (_obs, candidates)) in zip(paths_per_job, jobs)]
        per_job = score_candidate_batches_fn(batch, workers=workers, progress=_progress, cancel_check=cancel_check)
        return {key: per_photo for (key, _job), per_photo in zip(jobs, per_job)}

    per_photo_by_index: dict[int, list[list[tuple[int, float]]]] = {}
    total_cv = len(pending_cv)
    if pending_cv:
        total_pairs = sum(len(minute) * max(1, len(obs.photos)) for _idx, obs, minute, _tag in pending_cv)
        if progress_cb:
            progress_cb(
                "cv",
                f"Comparing images for {total_cv} ambiguous observations (up to {total_pairs} image pairs)",
                current=0,
                total=total_cv,
            )
        per_photo_by_index = _run_parallel_scoring(
            [(match_index, (obs, minute)) for match_index, obs, minute, _tag in pending_cv],
            _score_observation,
            workers=workers,
            phase="cv",
            describe=lambda done, total: f"Compared images for {done}/{total} ambiguous observations",
            progress_cb=progress_cb,
            cancel_check=cancel_check,
        )

    for match_index, obs, minute, has_existing_tag in pending_cv:
        per_photo = per_photo_by_index[match_index]
        scored = merge_photo_scores(per_photo)
        selected_ids: set[int] = set()
        status = "ambiguous"

        chosen_groups = select_groups_per_photo(
            per_photo, by_id, score_threshold=cv_score_threshold, margin_threshold=cv_margin_threshold
        )
        if chosen_groups:
            chosen_times = {by_id[photo_id].exif_datetime_raw for photo_id in chosen_groups.values()}
            selected_ids = {
                p.id for p in minute if _group_key(p.id, by_id) in chosen_groups or p.exif_datetime_raw in chosen_times
            }
            if group_versions:
                selected_ids = expand_with_version_siblings_fn(selected_ids, by_id)
            status = "auto-cv"
        elif scored:
            top_score = scored[0][1]
            second_score = scored[1][1] if len(scored) > 1 else 0.0
            if top_score >= cv_score_threshold and (top_score - second_score) >= cv_margin_threshold:
                chosen_id = scored[0][0]
                chosen = by_id[chosen_id]
                selected_ids = {p.id for p in minute if p.exif_datetime_raw == chosen.exif_datetime_raw}
                if group_versions:
                    selected_ids = expand_with_version_siblings_fn(selected_ids, by_id)
                status = "auto-cv"
            elif obs.photos and obs_count_by_minute[obs.observed_at.replace(second=0).timestamp()] == 1:
                unique_groups_in_minute = {
                    (by_id[photo_id].version_group or str(photo_id))
                    for photo_id, _ in scored
                    if photo_id in by_id
                }
                min_score = min(score for _, score in scored)
                if len(unique_groups_in_minute) == len(obs.photos) and min_score >= cv_score_threshold:
                    selected_ids = {photo.id for photo in minute}
                    if group_versions:
                        selected_ids = expand_with_version_siblings_fn(selected_ids, by_id)
                    status = "auto-cv"

        matches[match_index] = match_result_cls(
            observation=obs,
            status=status,
            candidate_photo_ids=[photo.id for photo in minute],
            scored_candidates=scored,
            has_existing_tag=has_existing_tag,
            selected_photo_ids=selected_ids,
        )

    if progress_cb:
        progress_cb("resolve", "Resolving observations that share the same minute")

    observations_by_minute: dict[float, list[object]] = {}
    for match in matches:
        minute_ts = match.observation.observed_at.replace(second=0).timestamp()
        if minute_ts not in observations_by_minute:
            observations_by_minute[minute_ts] = []
        observations_by_minute[minute_ts].append(match)

    for _minute_ts, matches_in_minute in observations_by_minute.items():
        if len(matches_in_minute) <= 1:
            continue

        eligible = [
            m
            for m in matches_in_minute
            if m.status != "auto-exact"
            and m.status not in PHOTOLESS_STATUSES
            and m.candidate_photo_ids
            and not m.has_existing_tag
        ]
        if len(eligible) <= 1:
            continue

        group_scores_by_obs: dict[int, dict[str, tuple[float, int]]] = {}
        chosen_by_obs: dict[int, tuple[str, float, int]] = {}
        all_clear = True

        for match in eligible:
            grouped = best_cv_scores_by_group(match, by_id)
            if not grouped:
                all_clear = False
                break
            group_scores_by_obs[match.observation.obs_id] = {
                group_key: (score, rep_photo_id) for group_key, score, rep_photo_id in grouped
            }

            top_group, top_score, top_photo_id = grouped[0]
            second_score = grouped[1][1] if len(grouped) > 1 else 0.0
            if top_score < cv_score_threshold or (top_score - second_score) < cv_margin_threshold:
                all_clear = False
                break

            chosen_by_obs[match.observation.obs_id] = (top_group, top_score, top_photo_id)

        if all_clear:
            chosen_groups = [choice[0] for choice in chosen_by_obs.values()]
            if len(set(chosen_groups)) != len(chosen_groups):
                all_clear = False

        if all_clear:
            for obs_id, (group_key, group_score, _photo_id) in chosen_by_obs.items():
                best_other = 0.0
                for other_obs_id, other_scores in group_scores_by_obs.items():
                    if other_obs_id == obs_id:
                        continue
                    other = other_scores.get(group_key)
                    if other is not None:
                        best_other = max(best_other, other[0])
                if (group_score - best_other) < cv_margin_threshold:
                    all_clear = False
                    break

        if all_clear:
            for match in eligible:
                group_key, _score, _photo_id = chosen_by_obs[match.observation.obs_id]
                selected_ids = {
                    candidate_id
                    for candidate_id in match.candidate_photo_ids
                    if (by_id.get(candidate_id) and ((by_id[candidate_id].version_group or str(candidate_id)) == group_key))
                }
                if group_versions:
                    selected_ids = expand_with_version_siblings_fn(selected_ids, by_id)
                match.status = "auto-cv"
                match.selected_photo_ids = selected_ids
        else:
            for match in eligible:
                match.status = "ambiguous"
                match.selected_photo_ids = set()

    enforce_unique_selected_groups(matches, by_id)

    _match_with_wider_time_windows(
        matches,
        by_minute,
        by_id,
        group_versions=group_versions,
        score_jobs_fn=_score_jobs,
        expand_with_version_siblings_fn=expand_with_version_siblings_fn,
        cv_score_threshold=cv_score_threshold,
        progress_cb=progress_cb,
        cancel_check=cancel_check,
    )

    _match_companion_photos(
        matches,
        by_id,
        group_versions=group_versions,
        score_jobs_fn=_score_jobs,
        expand_with_version_siblings_fn=expand_with_version_siblings_fn,
        progress_cb=progress_cb,
        cancel_check=cancel_check,
    )

    observations_by_id = {obs.obs_id: obs for obs in observations}
    tagged_photos = [
        (photo, observations_by_id[photo.existing_observation_id])
        for photo in by_id.values()
        if photo.existing_observation_id is not None and photo.existing_observation_id in observations_by_id
    ]
    total_tagged = len(tagged_photos)
    if progress_cb:
        progress_cb(
            "refresh",
            f"Checking embedded metadata of {total_tagged} already-tagged photos against iNaturalist data",
            current=0,
            total=total_tagged,
        )

    refresh_dirty_photo_ids: set[int] = set()
    dirty_photo_ids_by_obs: dict[int, list[int]] = defaultdict(list)
    for idx, (photo, obs_record) in enumerate(tagged_photos, start=1):
        if cancel_check:
            cancel_check()
        if progress_cb and (idx % 50 == 0 or idx == total_tagged):
            progress_cb(
                "refresh",
                f"Checking embedded metadata {idx}/{total_tagged} ({len(refresh_dirty_photo_ids)} need refresh)",
                current=idx,
                total=total_tagged,
            )
        try:
            needs_refresh, mismatches = metadata_refresh_check_fn(photo, obs_record)
        except Exception:
            continue
        if needs_refresh:
            photo.metadata_refresh_needed = True
            photo.metadata_mismatches = mismatches
            refresh_dirty_photo_ids.add(photo.id)
            dirty_photo_ids_by_obs[obs_record.obs_id].append(photo.id)

    if progress_cb:
        progress_cb(
            "refresh",
            f"{len(refresh_dirty_photo_ids)} of {total_tagged} tagged photos need a metadata refresh",
            current=total_tagged,
            total=total_tagged,
        )

    for match in matches:
        obs_dirty = dirty_photo_ids_by_obs.get(match.observation.obs_id)
        if not obs_dirty:
            continue
        for photo_id in obs_dirty:
            if photo_id not in match.candidate_photo_ids:
                match.candidate_photo_ids.append(photo_id)
        match.selected_photo_ids.update(obs_dirty)

    enforce_unique_selected_groups(matches, by_id)
    return matches, refresh_dirty_photo_ids
