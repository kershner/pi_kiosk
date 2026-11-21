import io
import subprocess
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import app as web
from misc import browser_watchdog as watchdog


class HeartbeatApiTests(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        self.token_patch = patch.object(web, '_browser_watchdog_token', None)
        self.seen_patch = patch.object(web, '_browser_watchdog_seen', 0.0)
        self.token_patch.start()
        self.seen_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.addCleanup(self.seen_patch.stop)

    def arm(self):
        return self.client.post('/api/kiosk/watchdog').get_json()['token']

    def status(self, token):
        return self.client.get('/api/kiosk/watchdog', query_string={'token': token})

    def beat(self, token):
        return self.client.post('/api/kiosk/heartbeat', query_string={'token': token})

    def test_startup_grace_expires_without_a_loaded_page(self):
        with patch.object(web.time, 'monotonic', return_value=100):
            token = self.arm()
        with patch.object(web.time, 'monotonic', return_value=279):
            self.assertEqual(self.status(token).status_code, 204)
        with patch.object(web.time, 'monotonic', return_value=280):
            self.assertEqual(self.status(token).status_code, 503)

    def test_heartbeat_extends_deadline_and_reload_can_reuse_token(self):
        with patch.object(web.time, 'monotonic', return_value=100):
            token = self.arm()
        with patch.object(web.time, 'monotonic', return_value=250):
            self.assertEqual(self.beat(token).status_code, 204)
        with patch.object(web.time, 'monotonic', return_value=400):
            self.assertEqual(self.status(token).status_code, 204)
            self.assertEqual(self.beat(token).status_code, 204)

    def test_previous_browser_cannot_keep_new_browser_alive(self):
        old_token = self.arm()
        with patch.object(web.time, 'monotonic', return_value=100):
            token = self.arm()
        with patch.object(web.time, 'monotonic', return_value=279):
            self.assertEqual(self.beat(old_token).status_code, 403)
            self.assertEqual(self.beat('invalid').status_code, 403)
        with patch.object(web.time, 'monotonic', return_value=280):
            self.assertEqual(self.status(token).status_code, 503)

    def test_lan_clients_cannot_arm_or_refresh_watchdog(self):
        token = self.arm()
        for method, path in [('POST', '/api/kiosk/watchdog'),
                             ('GET', '/api/kiosk/watchdog'),
                             ('POST', '/api/kiosk/heartbeat')]:
            response = self.client.open(
                path, method=method, query_string={'token': token},
                environ_overrides={'REMOTE_ADDR': '192.168.1.20'},
            )
            self.assertEqual(response.status_code, 403)


class SupervisorTests(unittest.TestCase):
    def run_supervisor(self, checks, waits, times):
        process = Mock(pid=123)
        process.wait.side_effect = waits
        arm_response = io.BytesIO(b'{"token":"session-token"}')
        with (
            patch.object(watchdog.opener, 'open', side_effect=[arm_response, *checks]),
            patch.object(watchdog.subprocess, 'Popen', return_value=process) as popen,
            patch.object(watchdog, 'stop_browser') as stop,
            patch.object(watchdog.time, 'monotonic', side_effect=times),
            patch.object(watchdog, 'log'),
        ):
            result = watchdog.run(['chromium', '--kiosk', 'http://localhost:5020?shuffle=true'])
        stop.assert_called_once_with(process)
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertEqual(popen.call_args.args[0][-1],
                         'http://localhost:5020?shuffle=true&watchdog=session-token')
        return result

    def test_expired_page_stops_browser(self):
        stale = HTTPError(watchdog.WATCHDOG_URL, 503, 'stale', {}, None)
        self.assertEqual(self.run_supervisor(
            [stale], [subprocess.TimeoutExpired('chromium', 15)], [0],
        ), 1)

    def test_transient_backend_failure_does_not_restart_browser(self):
        self.assertEqual(self.run_supervisor(
            [URLError('offline'), io.BytesIO()],
            [subprocess.TimeoutExpired('chromium', 15),
             subprocess.TimeoutExpired('chromium', 15), 0],
            [0, 15, 30],
        ), 0)

    def test_persistent_backend_failure_stops_browser(self):
        self.assertEqual(self.run_supervisor(
            [URLError('offline')], [subprocess.TimeoutExpired('chromium', 15)], [0, 180],
        ), 1)

    def test_stubborn_process_group_is_force_killed(self):
        process = Mock(pid=123)
        with (
            patch.object(watchdog.os, 'killpg', create=True) as killpg,
            patch.object(watchdog.signal, 'SIGKILL', 9, create=True),
            patch.object(watchdog.time, 'monotonic', side_effect=[0, 11]),
        ):
            watchdog.stop_browser(process)
        self.assertEqual(killpg.call_args_list[0].args, (123, watchdog.signal.SIGTERM))
        self.assertEqual(killpg.call_args_list[-1].args, (123, 9))
        process.wait.assert_called_once()
