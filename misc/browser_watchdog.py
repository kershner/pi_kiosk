"""Run kiosk Chromium and stop its process group when page heartbeats expire."""

import json
import os
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler


WATCHDOG_URL = 'http://127.0.0.1:5020/api/kiosk/watchdog'
CHECK_INTERVAL = 15
CONNECTION_TIMEOUT = 180
# Local health checks must not go through a configured HTTP proxy.
opener = build_opener(ProxyHandler({}))


def log(message):
    print(f'[browser-watchdog] {time.strftime("%Y-%m-%d %H:%M:%S")} {message}', flush=True)


def stop_browser(process):
    # Chromium's renderer children can outlive the browser parent. They all
    # inherit the private process group created by start_new_session below.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        process.poll()  # Reap the parent before checking for remaining children.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.25)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run(command):
    with opener.open(Request(WATCHDOG_URL, method='POST'), timeout=5) as response:
        token = json.load(response)['token']
    # xinitrc supplies the kiosk URL as the final argument.
    command = list(command)
    separator = '&' if '?' in command[-1] else '?'
    command[-1] += separator + urlencode({'watchdog': token})
    process = subprocess.Popen(command, start_new_session=True)
    log(f'Started Chromium PID {process.pid}; heartbeat timeout 180s')
    last_contact = time.monotonic()
    try:
        while True:
            try:
                result = process.wait(timeout=CHECK_INTERVAL)
                log(f'Chromium exited with status {result}')
                return result
            except subprocess.TimeoutExpired:
                pass
            try:
                with opener.open(WATCHDOG_URL + '?' + urlencode({'token': token}), timeout=5):
                    last_contact = time.monotonic()
            except HTTPError as exc:
                if exc.code == 503:
                    log('Page heartbeat expired; restarting Chromium')
                    return 1
                if time.monotonic() - last_contact >= CONNECTION_TIMEOUT:
                    log(f'Watchdog HTTP {exc.code} persisted for 180s; restarting Chromium')
                    return 1
            except (URLError, TimeoutError, OSError):
                if time.monotonic() - last_contact >= CONNECTION_TIMEOUT:
                    log('Backend unreachable for 180s; restarting Chromium')
                    return 1
    finally:
        stop_browser(process)


def interrupted(signum, frame):
    raise SystemExit(128 + signum)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        sys.exit(run(sys.argv[1:]))
    except (URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
        log(f'Could not start browser watchdog: {exc}')
        sys.exit(1)
