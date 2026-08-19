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
import subprocess
import threading
import random
import json
import time
import sys
import socket

PORT = 8765
POT_PROVIDER_HOST = "127.0.0.1"
POT_PROVIDER_PORT = 4416
STREAM_PROBE_BYTES = 1024
MAX_RESOLVE_ATTEMPTS = 5

# Playlist video ID cache: playlist_id → {ids: [...], ts: float}
_playlist_cache = {}
_playlist_lock = threading.Lock()
PLAYLIST_CACHE_TTL = 3600  # 1 hour

# Pre-fetched next stream URL cache: playlist_id → {url, video_id, ts}
_prefetch_cache = {}
_prefetch_lock = threading.Lock()
STREAM_URL_TTL = 18000  # 5 hours (YouTube URLs expire ~6h)


def log(msg):
    print(f"[player_server] {msg}", flush=True)


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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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

    return ids


def resolve_stream_url(video_id):
    """Get a direct stream URL, title, and English subtitle URL for a single video.
    Uses -j (dump JSON) which reliably returns both the selected format's
    stream URL and the video title in a single yt-dlp call."""
    log(f"Resolving stream for {video_id}...")
    output = run_ytdlp(
        "-f", "best[height<=480][ext=mp4]/best[height<=480]/best[ext=mp4]/best",
        "-j",
        f"https://www.youtube.com/watch?v={video_id}",
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

    # Find English VTT subtitle URL — prefer manual captions, fall back to auto-generated
    subtitle_url = ""
    for source in (info.get("subtitles", {}), info.get("automatic_captions", {})):
        for entry in source.get("en", []):
            if entry.get("ext") == "vtt":
                subtitle_url = entry.get("url", "")
                break
        if subtitle_url:
            break

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


def prefetch_next(playlist_id, exclude_id=None):
    """Background thread: pre-resolve next video so it's ready instantly."""
    try:
        result = pick_and_resolve(playlist_id, exclude_id)
        with _prefetch_lock:
            _prefetch_cache[playlist_id] = {**result, "ts": time.time()}
        log(f"Prefetched {result['video_id']} for playlist {playlist_id}")
    except Exception as e:
        log(f"Prefetch failed for {playlist_id}: {e}")


def get_prefetched(playlist_id):
    """Return and consume a prefetched URL if still valid, else None."""
    with _prefetch_lock:
        cached = _prefetch_cache.get(playlist_id)
        if cached and time.time() - cached["ts"] < STREAM_URL_TTL:
            del _prefetch_cache[playlist_id]
            return cached
    return None


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

            if not playlist_id:
                return json_response(self, {"error": "missing playlist_id"}, 400)

            try:
                # Try prefetch cache first (instant response)
                result = get_prefetched(playlist_id)

                if result:
                    log(f"Serving prefetched {result['video_id']}")
                    # Kick off next prefetch in background
                    threading.Thread(
                        target=prefetch_next,
                        args=(playlist_id, result["video_id"]),
                        daemon=True,
                    ).start()
                    return json_response(self, result)

                # No prefetch — resolve now
                result = pick_and_resolve(playlist_id, exclude_id)

                # Kick off next prefetch immediately
                threading.Thread(
                    target=prefetch_next,
                    args=(playlist_id, result["video_id"]),
                    daemon=True,
                ).start()

                return json_response(self, result)

            except Exception as e:
                log(f"Error in /next: {e}")
                return json_response(self, {"error": str(e)}, 500)

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
                with urllib.request.urlopen(sub_url, timeout=10) as r:
                    data = r.read()
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
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    log(f"Listening on http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Stopped.")
