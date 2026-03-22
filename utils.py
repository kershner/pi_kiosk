from qrcode.image.styles.colormasks import SolidFillColorMask
from qrcode.image.styles.moduledrawers import GappedSquareModuleDrawer
from qrcode.image.styledpil import StyledPilImage
from urllib.parse import urlparse, parse_qs
from config import TOKEN_TTL, CATEGORIES_URL, CATEGORIES_REFRESH_INTERVAL
from io import BytesIO
import threading
import requests
import logging
import secrets
import base64
import socket
import time
import qrcode

log = logging.getLogger(__name__)

# ─── In-memory stores (replaces Django cache) ─────────────────────────────────

_token_store = {}   # "{token}:{device_id}" → expiry timestamp
_token_lock = threading.Lock()

_qr_cache = {}      # device_id → {"qr_code_b64": ..., "token": ..., "ts": ...}
_qr_lock = threading.Lock()

_latest_play = {}   # device_id → {"type": ..., "youtube_id": ..., "ts": ...}
_latest_lock = threading.Lock()

_search_cache = {}  # query → {"result": ..., "ts": ...}

_categories_cache = {"data": [], "ts": 0.0}
_categories_lock = threading.Lock()


# ─── Token helpers ─────────────────────────────────────────────────────────────

def _purge_expired_tokens():
    now = time.time()
    with _token_lock:
        expired = [k for k, exp in _token_store.items() if exp < now]
        for k in expired:
            del _token_store[k]


def create_token(device_id):
    token = secrets.token_urlsafe(12)
    with _token_lock:
        _token_store[f'{token}:{device_id}'] = time.time() + TOKEN_TTL
    return token


def validate_token(token, device_id):
    _purge_expired_tokens()
    with _token_lock:
        return f'{token}:{device_id}' in _token_store


# ─── Latest play store ─────────────────────────────────────────────────────────

def set_latest_play(device_id, content_type, content_id):
    with _latest_lock:
        _latest_play[device_id] = {
            'type': content_type,
            'youtube_id': content_id,
            'ts': time.time(),
        }


def get_latest_play(device_id):
    with _latest_lock:
        return _latest_play.get(device_id)


# ─── QR code ───────────────────────────────────────────────────────────────────

def get_local_ip():
    """Best-effort local IP for QR code URLs."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def get_or_create_qr_code(device_id, base_url):
    """Get cached QR code or create a new one with a fresh token.
    Mirrors Django utils.get_or_create_qr_code() but uses _qr_cache."""
    with _qr_lock:
        cached = _qr_cache.get(device_id)
        if cached and time.time() - cached['ts'] < TOKEN_TTL:
            return cached['qr_code_b64']

    token = create_token(device_id)
    submit_url = f'{base_url}/submit?token={token}&device_id={device_id}'
    qr_code_b64 = generate_qr_code(submit_url)

    with _qr_lock:
        _qr_cache[device_id] = {
            'qr_code_b64': qr_code_b64,
            'token': token,
            'ts': time.time(),
        }

    return qr_code_b64


def invalidate_qr_cache(device_id):
    """Force a new token + QR code to be generated on next request."""
    with _qr_lock:
        _qr_cache.pop(device_id, None)


def generate_qr_code(data):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(
        image_factory=StyledPilImage,
        module_drawer=GappedSquareModuleDrawer(),
        color_mask=SolidFillColorMask(
            front_color=(255, 255, 255),
            back_color=(26, 26, 26),
        ),
    ).convert('RGBA')

    buf = BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()


# ─── YouTube ID extraction ─────────────────────────────────────────────────────

def extract_youtube_id(url):
    """Extract video or playlist ID from various YouTube URL formats."""
    try:
        u = urlparse(url)
        host = (u.netloc or '').lower()
        qs = parse_qs(u.query)

        if 'list' in qs:
            return ('playlist', qs['list'][0])

        if host.endswith('youtu.be'):
            video_id = u.path.lstrip('/') or None
            return ('video', video_id) if video_id else None

        if 'youtube.com' in host:
            video_id = qs.get('v', [None])[0]
            return ('video', video_id) if video_id else None

        return None
    except Exception:
        return None


# ─── Categories ────────────────────────────────────────────────────────────────

def _fetch_categories():
    """Fetch categories + playlists from kershner.org."""
    try:
        r = requests.get(CATEGORIES_URL, timeout=10)
        r.raise_for_status()
        data = r.json()
        log.info(f'Fetched {len(data)} categories from kershner.org')
        return data
    except Exception as e:
        log.warning(f'Could not fetch categories: {e}')
        return None


def get_categories():
    """Return cached categories, refreshing if stale."""
    with _categories_lock:
        stale = time.time() - _categories_cache['ts'] > CATEGORIES_REFRESH_INTERVAL
        current = _categories_cache['data']

    if stale:
        data = _fetch_categories()
        if data is not None:
            with _categories_lock:
                _categories_cache['data'] = data
                _categories_cache['ts'] = time.time()
            return data

    return current


def start_categories_refresh_thread():
    """Fetch categories immediately on startup, then refresh hourly in background."""
    def _loop():
        while True:
            time.sleep(CATEGORIES_REFRESH_INTERVAL)
            get_categories()

    # Blocking initial fetch — ensures categories are ready before first page load
    data = _fetch_categories()
    if data is not None:
        with _categories_lock:
            _categories_cache['data'] = data
            _categories_cache['ts'] = time.time()

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
