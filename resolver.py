"""YouTube stream resolution, validation, caching, and bounded prefetching."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import logging
import os
import random
import socket
import subprocess
import sys
import threading
import time


POT_PROVIDER_HOST = "127.0.0.1"
POT_PROVIDER_PORT = 4416
STREAM_PROBE_BYTES = 1024
MAX_RESOLVE_ATTEMPTS = 5
VIDEO_RESOLVE_TIMEOUT = 90
PLAYLIST_CACHE_TTL = 86400
STREAM_URL_TTL = 18000
CACHE_PATH = Path.home() / ".cache" / "pi_kiosk" / "player_cache.json"

_playlist_cache = {}
_prefetch_cache = {}
_prefetch_futures = {}
_cache_lock = threading.RLock()
_cache_file_lock = threading.Lock()
_prefetch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prefetch")
_task_context = threading.local()
_initialized = False
log = logging.getLogger(__name__)


def _is_fresh(entry, ttl):
    return bool(entry and time.time() - entry.get("ts", 0) < ttl)


def save_persistent_cache():
    """Persist cache snapshots atomically across app restarts."""
    with _cache_lock:
        data = {
            "playlists": dict(_playlist_cache),
            "streams": dict(_prefetch_cache),
        }

    with _cache_file_lock:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp_path = CACHE_PATH.with_suffix(".tmp")
            temp_path.write_text(json.dumps(data))
            os.replace(temp_path, CACHE_PATH)
        except OSError as exc:
            log.warning("Could not save persistent cache: %s", exc)


def load_persistent_cache():
    """Restore only entries that are still safe to serve."""
    try:
        data = json.loads(CACHE_PATH.read_text())
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        log.warning("Ignoring invalid persistent cache: %s", exc)
        return

    playlists = {
        key: value
        for key, value in (data.get("playlists") or {}).items()
        if _is_fresh(value, PLAYLIST_CACHE_TTL) and value.get("ids")
    }
    streams = {
        key: value
        for key, value in (data.get("streams") or {}).items()
        if _is_fresh(value, STREAM_URL_TTL) and value.get("url")
    }
    with _cache_lock:
        _playlist_cache.update(playlists)
        _prefetch_cache.update(streams)
    log.info("Restored %d playlists and %d ready streams", len(playlists), len(streams))


def run_ytdlp(*args, timeout=45):
    """Run yt-dlp from the active venv and return its standard output."""
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-progress",
        "--no-playlist-reverse",
        "--js-runtimes",
        "node",
        "--extractor-args",
        "youtube:player_client=mweb",
        *args,
    ]
    if os.name == "posix" and getattr(_task_context, "background", False):
        cmd = ["nice", "-n", "10", *cmd]

    started = time.monotonic()
    target = args[-1] if args else "request"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("yt-dlp timed out after %.1fs for %s", time.monotonic() - started, target)
        raise

    log.info("yt-dlp completed in %.1fs for %s", time.monotonic() - started, target)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "yt-dlp failed")
    for line in result.stderr.strip().splitlines():
        log.info("yt-dlp: %s", line)
    return result.stdout.strip()


def validate_stream_url(url, http_headers=None):
    """Probe a tiny byte range so rejected media URLs never reach Chromium."""
    headers = dict(http_headers or {})
    headers["Range"] = f"bytes=0-{STREAM_PROBE_BYTES - 1}"
    request = Request(url, headers=headers)

    try:
        with urlopen(request, timeout=15) as response:
            status = getattr(response, "status", 200)
            if status not in (200, 206):
                raise RuntimeError(f"media probe returned HTTP {status}")
            if not response.read(1):
                raise RuntimeError("media probe returned no data")
    except HTTPError as exc:
        raise RuntimeError(f"media probe returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"media probe failed: {exc}") from exc


def find_english_subtitle(info):
    """Return the best manual or automatic English VTT track."""
    for source_name in ("subtitles", "automatic_captions"):
        source = info.get(source_name) or {}
        language_codes = sorted(
            (code for code in source if code.lower() == "en" or code.lower().startswith("en-")),
            key=lambda code: (
                code.lower() != "en",
                code.lower() != "en-orig",
                code.lower(),
            ),
        )
        for language_code in language_codes:
            for entry in source.get(language_code) or []:
                if entry.get("ext") == "vtt" and entry.get("url"):
                    return entry["url"], source_name, language_code
    return "", "", ""


def get_playlist_video_ids(playlist_id):
    """Return playlist video IDs, caching them for 24 hours."""
    with _cache_lock:
        cached = _playlist_cache.get(playlist_id)
        if _is_fresh(cached, PLAYLIST_CACHE_TTL):
            return cached["ids"]

    log.info("Fetching playlist %s", playlist_id)
    output = run_ytdlp(
        "--flat-playlist",
        "--print",
        "%(id)s",
        f"https://www.youtube.com/playlist?list={playlist_id}",
        timeout=60,
    )
    ids = [line.strip() for line in output.splitlines() if line.strip()]
    log.info("Playlist %s: %d videos", playlist_id, len(ids))
    with _cache_lock:
        _playlist_cache[playlist_id] = {"ids": ids, "ts": time.time()}
    save_persistent_cache()
    return ids


def resolve_stream_url(video_id):
    """Resolve one video into a validated direct URL, title, and subtitle URL."""
    started = time.monotonic()
    log.info("Resolving stream for %s", video_id)
    output = run_ytdlp(
        "-f",
        "best[height<=480][ext=mp4]/best[height<=480]/best[ext=mp4]/best",
        "-j",
        f"https://www.youtube.com/watch?v={video_id}",
        timeout=VIDEO_RESOLVE_TIMEOUT,
    )
    info = json.loads(output)
    url = info.get("url") or (info.get("requested_formats") or [{}])[0].get("url", "")
    if not url:
        raise RuntimeError("yt-dlp returned no stream URL")

    validate_stream_url(url, info.get("http_headers"))
    log.info("Validated %s format %s", video_id, info.get("format_id", "unknown"))
    subtitle_url, subtitle_source, subtitle_language = find_english_subtitle(info)
    if subtitle_url:
        log.info("Selected %s language %s for %s", subtitle_source, subtitle_language, video_id)
    else:
        log.info("No English subtitles found for %s", video_id)
    log.info("Resolved %s in %.1fs", video_id, time.monotonic() - started)
    return url, info.get("title", ""), subtitle_url


def pick_and_resolve(playlist_id, exclude_id=None):
    """Pick and validate a random playlist video, retrying rejected streams."""
    ids = get_playlist_video_ids(playlist_id)
    if not ids:
        raise RuntimeError("Empty playlist")

    available = [video_id for video_id in ids if video_id != exclude_id] or ids
    candidates = random.sample(available, min(MAX_RESOLVE_ATTEMPTS, len(available)))
    last_error = None
    for video_id in candidates:
        try:
            url, title, subtitle_url = resolve_stream_url(video_id)
            return {
                "url": url,
                "video_id": video_id,
                "title": title,
                "subtitle_url": subtitle_url,
            }
        except Exception as exc:
            last_error = exc
            log.warning("Rejected %s: %s", video_id, exc)
    raise RuntimeError(f"No playable stream after {len(candidates)} attempts: {last_error}")


def _prefetch_next(playlist_id, exclude_id=None):
    _task_context.background = True
    try:
        result = pick_and_resolve(playlist_id, exclude_id)
        with _cache_lock:
            _prefetch_cache[playlist_id] = {**result, "ts": time.time()}
        save_persistent_cache()
        log.info("Prefetched %s for playlist %s", result["video_id"], playlist_id)
        return result
    except Exception as exc:
        log.warning("Prefetch failed for %s: %s", playlist_id, exc)
        return None
    finally:
        _task_context.background = False
        with _cache_lock:
            _prefetch_futures.pop(playlist_id, None)


def schedule_prefetch(playlist_id, exclude_id=None):
    """Start one deduplicated prefetch without overloading the Pi."""
    with _cache_lock:
        cached = _prefetch_cache.get(playlist_id)
        if _is_fresh(cached, STREAM_URL_TTL):
            return False
        _prefetch_cache.pop(playlist_id, None)

        active = {key: future for key, future in _prefetch_futures.items() if not future.done()}
        _prefetch_futures.clear()
        _prefetch_futures.update(active)
        if playlist_id in active:
            return False
        if active:
            log.info("Prefetch busy; skipped playlist %s", playlist_id)
            return False

        _prefetch_futures[playlist_id] = _prefetch_executor.submit(
            _prefetch_next, playlist_id, exclude_id
        )
    log.info("Scheduled prefetch for playlist %s", playlist_id)
    return True


def wait_for_prefetch(playlist_id):
    """Join matching in-flight work instead of launching duplicate yt-dlp work."""
    with _cache_lock:
        future = _prefetch_futures.get(playlist_id)
    if not future:
        return get_prefetched(playlist_id)
    log.info("Waiting for in-flight prefetch for playlist %s", playlist_id)
    future.result()
    return get_prefetched(playlist_id)


def get_prefetched(playlist_id):
    """Return and consume a prefetched URL if it is still valid."""
    with _cache_lock:
        cached = _prefetch_cache.pop(playlist_id, None)
    if cached:
        save_persistent_cache()
    return cached if _is_fresh(cached, STREAM_URL_TTL) else None


def get_any_prefetched():
    """Consume any warm stream to make shuffle startup immediate."""
    with _cache_lock:
        valid = [
            (playlist_id, value)
            for playlist_id, value in _prefetch_cache.items()
            if _is_fresh(value, STREAM_URL_TTL)
        ]
        if not valid:
            return None
        playlist_id, result = random.choice(valid)
        del _prefetch_cache[playlist_id]
    save_persistent_cache()
    return {**result, "playlist_id": playlist_id}


def check_dependencies():
    """Fail startup early when the YouTube resolution stack is unavailable."""
    try:
        subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError("yt-dlp is unavailable in the active Python environment") from exc

    try:
        node = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
        if int(node.lstrip("v").split(".", 1)[0]) < 22:
            raise RuntimeError(f"Node.js 22+ is required; found {node}")
        log.info("Using Node %s", node)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as exc:
        raise RuntimeError("Node.js 22+ is required for YouTube challenge solving") from exc

    try:
        with socket.create_connection((POT_PROVIDER_HOST, POT_PROVIDER_PORT), timeout=3):
            log.info("PO token provider ready on %s:%d", POT_PROVIDER_HOST, POT_PROVIDER_PORT)
    except OSError as exc:
        raise RuntimeError(
            f"PO token provider is unavailable on {POT_PROVIDER_HOST}:{POT_PROVIDER_PORT}"
        ) from exc


def initialize():
    """Initialize the resolver exactly once during application startup."""
    global _initialized
    if _initialized:
        return
    check_dependencies()
    load_persistent_cache()
    _initialized = True
