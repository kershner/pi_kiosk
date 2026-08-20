"""Fetch and cache the remotely managed playlist catalog."""

from config import CATEGORIES_REFRESH_INTERVAL, CATEGORIES_URL
import logging
import threading
import time
import requests


_cache = {"data": [], "ts": 0.0}
_lock = threading.Lock()
log = logging.getLogger(__name__)


def _fetch():
    try:
        response = requests.get(CATEGORIES_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        log.info("Fetched %d categories", len(data))
        return data
    except Exception as exc:
        log.warning("Could not fetch categories: %s", exc)
        return None


def get_categories():
    """Return the current catalog, refreshing it when stale."""
    with _lock:
        stale = time.time() - _cache["ts"] > CATEGORIES_REFRESH_INTERVAL
        current = _cache["data"]
    if not stale:
        return current

    data = _fetch()
    if data is None:
        return current
    with _lock:
        _cache.update(data=data, ts=time.time())
    return data


def start_categories_refresh_thread():
    """Warm the catalog and refresh it periodically in the background."""
    get_categories()

    def refresh_loop():
        while True:
            time.sleep(CATEGORIES_REFRESH_INTERVAL)
            get_categories()

    threading.Thread(target=refresh_loop, daemon=True, name="catalog-refresh").start()
