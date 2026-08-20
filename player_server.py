#!/usr/bin/env python3
"""
player_server.py — Pi local stream resolver
Runs on localhost:8765, resolves YouTube playlist/video URLs via yt-dlp
so Chromium can play them natively without the heavy YouTube iframe.
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
import subprocess
import threading
import random
import json
import time
import sys
import socket
import os

PORT = 8765
POT_PROVIDER_HOST = "127.0.0.1"
POT_PROVIDER_PORT = 4416
STREAM_PROBE_BYTES = 1024
MAX_RESOLVE_ATTEMPTS = 5
VIDEO_RESOLVE_TIMEOUT = 90
MWEB_USER_AGENT = (
    "Mozilla/5.0 (iPad; CPU OS 16_7_10 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
    "Mobile/15E148 Safari/604.1"
)
SUBTITLE_REQUEST_HEADERS = {
    "User-Agent": MWEB_USER_AGENT,
    "Referer": "https://m.youtube.com/",
    "Accept": "text/vtt,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}

# Playlist video ID cache: playlist_id → {ids: [...], ts: float}
_playlist_cache = {}
_playlist_lock = threading.Lock()
PLAYLIST_CACHE_TTL = 86400  # 24 hours

# Pre-fetched next stream URL cache: playlist_id → {url, video_id, ts}
_prefetch_cache = {}
_prefetch_lock = threading.Lock()
_prefetch_inflight = set()
_prefetch_events = {}
_prefetch_slot = threading.BoundedSemaphore(1)
STREAM_URL_TTL = 18000  # 5 hours (YouTube URLs expire ~6h)
CACHE_PATH = Path.home() / ".cache" / "pi_kiosk" / "player_cache.json"
_cache_file_lock = threading.Lock()
_task_context = threading.local()


def log(msg):
    print(f"[player_server] {msg}", flush=True)


def save_persistent_cache():
    """Persist warm playlist and stream caches across server restarts."""
    with _cache_file_lock:
        with _playlist_lock:
            playlists = dict(_playlist_cache)
        with _prefetch_lock:
            streams = dict(_prefetch_cache)
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp_path = CACHE_PATH.with_suffix(".tmp")
            temp_path.write_text(json.dumps({"playlists": playlists, "streams": streams}))
            os.replace(temp_path, CACHE_PATH)
        except OSError as e:
            log(f"Could not save persistent cache: {e}")


def load_persistent_cache():
    """Restore only entries that are still safe to serve."""
    try:
        data = json.loads(CACHE_PATH.read_text())
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        log(f"Ignoring invalid persistent cache: {e}")
        return

    now = time.time()
    playlists = {
        key: value for key, value in (data.get("playlists") or {}).items()
        if now - value.get("ts", 0) < PLAYLIST_CACHE_TTL and value.get("ids")
    }
    streams = {
        key: value for key, value in (data.get("streams") or {}).items()
        if now - value.get("ts", 0) < STREAM_URL_TTL and value.get("url")
    }
    with _playlist_lock:
        _playlist_cache.update(playlists)
    with _prefetch_lock:
        _prefetch_cache.update(streams)
    log(f"Restored {len(playlists)} playlists and {len(streams)} ready streams")


def run_ytdlp(*args, timeout=45):
    """Run yt-dlp with given args, return stdout or raise on failure.
    Uses sys.executable so yt-dlp is resolved from the active venv."""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-progress",
        "--no-playlist-reverse",
        "--js-runtimes", "node",
        "--extractor-args", "youtube:player_client=mweb",
        *args,
    ]
    if os.name == "posix" and getattr(_task_context, "background", False):
        cmd = ["nice", "-n", "10", *cmd]
    started = time.monotonic()
    target = args[-1] if args else "request"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"yt-dlp timed out after {time.monotonic() - started:.1f}s for {target}")
        raise
    log(f"yt-dlp completed in {time.monotonic() - started:.1f}s for {target}")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "yt-dlp failed")
    if result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            log(f"yt-dlp: {line}")
    return result.stdout.strip()


def validate_stream_url(url, http_headers=None):
    """Fetch a tiny byte range so rejected Google Video URLs never reach Chromium."""
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
    except HTTPError as e:
        raise RuntimeError(f"media probe returned HTTP {e.code}") from e
    except (URLError, TimeoutError) as e:
        raise RuntimeError(f"media probe failed: {e}") from e


def find_english_subtitle(info):
    """Return the best English VTT URL from yt-dlp's language variants."""
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
    """Return list of video IDs for a playlist, cached for 1 hour."""
    with _playlist_lock:
        cached = _playlist_cache.get(playlist_id)
        if cached and time.time() - cached["ts"] < PLAYLIST_CACHE_TTL:
            return cached["ids"]

    log(f"Fetching playlist {playlist_id}...")
    output = run_ytdlp(
        "--flat-playlist",
        "--print", "%(id)s",
        f"https://www.youtube.com/playlist?list={playlist_id}",
        timeout=60,
    )
    ids = [line.strip() for line in output.splitlines() if line.strip()]
    log(f"Playlist {playlist_id}: {len(ids)} videos")

    with _playlist_lock:
        _playlist_cache[playlist_id] = {"ids": ids, "ts": time.time()}
    save_persistent_cache()

    return ids


def resolve_stream_url(video_id):
    """Get a direct stream URL, title, and English subtitle URL for a single video.
    Uses -j (dump JSON) which reliably returns both the selected format's
    stream URL and the video title in a single yt-dlp call."""
    started = time.monotonic()
    log(f"Resolving stream for {video_id}...")
    output = run_ytdlp(
        "-f", "best[height<=480][ext=mp4]/best[height<=480]/best[ext=mp4]/best",
        "-j",
        f"https://www.youtube.com/watch?v={video_id}",
        timeout=VIDEO_RESOLVE_TIMEOUT,
    )
    info = json.loads(output)
    title = info.get("title", "")
    # 'url' is the stream URL of the selected format; fall back to first
    # requested format if yt-dlp split into separate video+audio tracks.
    url = info.get("url") or (info.get("requested_formats") or [{}])[0].get("url", "")
    if not url:
        raise RuntimeError("yt-dlp returned no stream URL")

    validate_stream_url(url, info.get("http_headers"))
    log(f"Validated {video_id} format {info.get('format_id', 'unknown')}")

    # Prefer manual captions and accept yt-dlp's en-orig/en-US variants too.
    subtitle_url, subtitle_source, subtitle_language = find_english_subtitle(info)
    if subtitle_url:
        log(f"Selected {subtitle_source} language {subtitle_language} for {video_id}")
    else:
        log(f"No English subtitles found for {video_id}")

    log(f"Resolved {video_id} in {time.monotonic() - started:.1f}s")

    return url, title, subtitle_url


def pick_and_resolve(playlist_id, exclude_id=None):
    """Pick and validate a random playlist video, retrying rejected streams."""
    ids = get_playlist_video_ids(playlist_id)
    if not ids:
        raise RuntimeError("Empty playlist")

    available = [i for i in ids if i != exclude_id] if exclude_id else ids
    if not available:
        available = ids

    candidates = random.sample(available, min(MAX_RESOLVE_ATTEMPTS, len(available)))
    last_error = None
    for video_id in candidates:
        try:
            url, title, subtitle_url = resolve_stream_url(video_id)
            return {"url": url, "video_id": video_id, "title": title, "subtitle_url": subtitle_url}
        except Exception as e:
            last_error = e
            log(f"Rejected {video_id}: {e}")

    raise RuntimeError(
        f"No playable stream after {len(candidates)} attempts: {last_error}"
    )


def _prefetch_next(playlist_id, exclude_id=None):
    """Worker for a single bounded background prefetch."""
    _task_context.background = True
    try:
        result = pick_and_resolve(playlist_id, exclude_id)
        with _prefetch_lock:
            _prefetch_cache[playlist_id] = {**result, "ts": time.time()}
        save_persistent_cache()
        log(f"Prefetched {result['video_id']} for playlist {playlist_id}")
    except Exception as e:
        log(f"Prefetch failed for {playlist_id}: {e}")
    finally:
        _task_context.background = False
        with _prefetch_lock:
            _prefetch_inflight.discard(playlist_id)
            event = _prefetch_events.pop(playlist_id, None)
            if event:
                event.set()
        _prefetch_slot.release()


def schedule_prefetch(playlist_id, exclude_id=None):
    """Start one deduplicated prefetch without overloading the Pi."""
    with _prefetch_lock:
        cached = _prefetch_cache.get(playlist_id)
        if cached and time.time() - cached["ts"] < STREAM_URL_TTL:
            return False
        if cached:
            del _prefetch_cache[playlist_id]
        if playlist_id in _prefetch_inflight:
            return False
        if not _prefetch_slot.acquire(blocking=False):
            log(f"Prefetch busy; skipped playlist {playlist_id}")
            return False
        _prefetch_inflight.add(playlist_id)
        _prefetch_events[playlist_id] = threading.Event()

    threading.Thread(
        target=_prefetch_next,
        args=(playlist_id, exclude_id),
        daemon=True,
    ).start()
    log(f"Scheduled prefetch for playlist {playlist_id}")
    return True


def wait_for_prefetch(playlist_id):
    """Join an in-flight prefetch instead of launching duplicate yt-dlp work."""
    with _prefetch_lock:
        event = _prefetch_events.get(playlist_id)
    if not event:
        return None
    log(f"Waiting for in-flight prefetch for playlist {playlist_id}")
    event.wait()
    return get_prefetched(playlist_id)


def get_prefetched(playlist_id):
    """Return and consume a prefetched URL if still valid, else None."""
    with _prefetch_lock:
        cached = _prefetch_cache.get(playlist_id)
        if cached and time.time() - cached["ts"] < STREAM_URL_TTL:
            del _prefetch_cache[playlist_id]
        else:
            cached = None
    if cached:
        save_persistent_cache()
        return cached
    return None


def get_any_prefetched():
    """Consume any warm stream, used to make shuffle startup immediate."""
    now = time.time()
    with _prefetch_lock:
        valid = [
            (playlist_id, value)
            for playlist_id, value in _prefetch_cache.items()
            if now - value["ts"] < STREAM_URL_TTL
        ]
        if not valid:
            return None
        playlist_id, result = random.choice(valid)
        del _prefetch_cache[playlist_id]
    save_persistent_cache()
    return {**result, "playlist_id": playlist_id}


def json_response(handler, data, status=200):
    body = json.dumps(data).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", len(body))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Suppress default access log noise

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        path = parsed.path

        # GET /ping — health check
        if path == "/ping":
            return json_response(self, {"ok": True})

        # GET /next?playlist_id=xxx[&exclude=video_id]
        # Returns a stream URL for a random video from the playlist.
        # Uses prefetch cache when available, otherwise resolves on demand.
        if path == "/next":
            playlist_id = (qs.get("playlist_id") or [None])[0]
            exclude_id = (qs.get("exclude") or [None])[0]
            should_prefetch = (qs.get("prefetch") or ["1"])[0] != "0"

            if not playlist_id:
                return json_response(self, {"error": "missing playlist_id"}, 400)

            try:
                # Try prefetch cache first (instant response)
                result = get_prefetched(playlist_id)
                if not result:
                    result = wait_for_prefetch(playlist_id)

                if result:
                    log(f"Serving prefetched {result['video_id']}")
                    if should_prefetch:
                        schedule_prefetch(playlist_id, result["video_id"])
                    return json_response(self, result)

                # No prefetch — resolve now
                result = pick_and_resolve(playlist_id, exclude_id)

                if should_prefetch:
                    schedule_prefetch(playlist_id, result["video_id"])

                return json_response(self, result)

            except Exception as e:
                log(f"Error in /next: {e}")
                return json_response(self, {"error": str(e)}, 500)

        # GET /prefetch?playlist_id=xxx[&exclude=video_id]
        # Queues the playlist that shuffle mode will actually play next.
        if path == "/prefetch":
            playlist_id = (qs.get("playlist_id") or [None])[0]
            exclude_id = (qs.get("exclude") or [None])[0]
            if not playlist_id:
                return json_response(self, {"error": "missing playlist_id"}, 400)
            scheduled = schedule_prefetch(playlist_id, exclude_id)
            return json_response(self, {"ok": True, "scheduled": scheduled})

        # GET /ready — consumes any persisted warm stream for fast startup.
        if path == "/ready":
            result = get_any_prefetched()
            return json_response(self, result or {}, 200 if result else 204)

        # GET /resolve-video?video_id=xxx
        # Resolves a specific video (used when a video is submitted via QR code).
        if path == "/resolve-video":
            video_id = (qs.get("video_id") or [None])[0]
            if not video_id:
                return json_response(self, {"error": "missing video_id"}, 400)
            try:
                url, title, subtitle_url = resolve_stream_url(video_id)
                return json_response(self, {"url": url, "video_id": video_id, "title": title, "subtitle_url": subtitle_url})
            except Exception as e:
                log(f"Error in /resolve-video: {e}")
                return json_response(self, {"error": str(e)}, 500)

        # GET /proxy-subtitle?url=xxx
        # Proxies a VTT subtitle file to avoid CORS issues when loading
        # YouTube subtitle URLs from a localhost page.
        if path == "/proxy-subtitle":
            import urllib.request
            sub_url = (qs.get("url") or [None])[0]
            if not sub_url:
                self.send_response(400)
                self.end_headers()
                return
            try:
                request = urllib.request.Request(sub_url, headers=SUBTITLE_REQUEST_HEADERS)
                with urllib.request.urlopen(request, timeout=10) as r:
                    data = r.read()
                if not data.lstrip(b"\xef\xbb\xbf").startswith(b"WEBVTT"):
                    raise RuntimeError("YouTube returned a non-VTT subtitle response")
                self.send_response(200)
                self.send_header("Content-Type", "text/vtt; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", len(data))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                log(f"Subtitle proxy error: {e}")
                self.send_response(502)
                self.end_headers()
            return

        # GET /invalidate-playlist?playlist_id=xxx
        # Clears the cached video ID list for a playlist.
        if path == "/invalidate-playlist":
            playlist_id = (qs.get("playlist_id") or [None])[0]
            if playlist_id:
                with _playlist_lock:
                    _playlist_cache.pop(playlist_id, None)
            return json_response(self, {"ok": True})

        self.send_response(404)
        self.end_headers()


def check_dependencies():
    try:
        subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("ERROR: yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        sys.exit(1)

    try:
        node = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
        major = int(node.lstrip("v").split(".", 1)[0])
        if major < 22:
            raise RuntimeError(f"Node.js 22+ is required; found {node}")
        log(f"Using Node {node}")
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"ERROR: Node.js 22+ is required for YouTube challenge solving: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        with socket.create_connection((POT_PROVIDER_HOST, POT_PROVIDER_PORT), timeout=3):
            log(f"PO token provider ready on {POT_PROVIDER_HOST}:{POT_PROVIDER_PORT}")
    except OSError as e:
        print(
            f"ERROR: PO token provider is not reachable on "
            f"{POT_PROVIDER_HOST}:{POT_PROVIDER_PORT}: {e}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    check_dependencies()
    load_persistent_cache()
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    log(f"Listening on http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Stopped.")
