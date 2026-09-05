import unittest
import subprocess
from unittest.mock import patch
from unittest.mock import Mock

import resolver
import app as web
import catalog
from remote import extract_youtube_id


class YouTubeUrlTests(unittest.TestCase):
    def test_extracts_video_urls(self):
        self.assertEqual(
            extract_youtube_id("https://www.youtube.com/watch?v=abc123"),
            ("video", "abc123"),
        )
        self.assertEqual(
            extract_youtube_id("https://youtu.be/abc123"),
            ("video", "abc123"),
        )

    def test_playlist_takes_precedence(self):
        self.assertEqual(
            extract_youtube_id("https://www.youtube.com/watch?v=abc123&list=PLxyz"),
            ("playlist", "PLxyz"),
        )

    def test_rejects_non_youtube_urls(self):
        self.assertIsNone(extract_youtube_id("https://example.com/watch?v=abc123"))


class ResolverTests(unittest.TestCase):
    def test_ytdlp_uses_a_managed_subprocess(self):
        process = Mock()
        process.communicate.return_value = ("result\n", "")
        process.returncode = 0

        with patch.object(resolver.subprocess, "Popen", return_value=process) as popen:
            output = resolver.run_ytdlp("--version")

        self.assertEqual(output, "result")
        self.assertEqual(popen.call_count, 1)
        process.communicate.assert_called_once_with(timeout=45)

    def test_ytdlp_timeout_kills_the_process_group_on_posix(self):
        process = Mock(pid=123)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("yt-dlp", 45),
            ("", ""),
        ]

        with (
            patch.object(resolver.os, "name", "posix"),
            patch.object(resolver.subprocess, "Popen", return_value=process) as popen,
            patch.object(resolver.os, "killpg", create=True) as killpg,
            patch.object(resolver.signal, "SIGKILL", 9, create=True),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                resolver.run_ytdlp("--version")

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(123, 9)
        self.assertEqual(process.communicate.call_count, 2)

    def test_prefers_manual_english_subtitles(self):
        info = {
            "subtitles": {
                "en-US": [{"ext": "vtt", "url": "manual-us"}],
                "en": [{"ext": "vtt", "url": "manual-en"}],
            },
            "automatic_captions": {
                "en": [{"ext": "vtt", "url": "automatic-en"}],
            },
        }
        self.assertEqual(
            resolver.find_english_subtitle(info),
            ("manual-en", "subtitles", "en"),
        )

    def test_playlist_choice_excludes_current_video(self):
        with (
            patch.object(resolver, "get_playlist_video_ids", return_value=["a", "b"]),
            patch.object(resolver.random, "sample", return_value=["b"]),
            patch.object(resolver, "resolve_stream_url", return_value=("url", "title", "sub")),
        ):
            result = resolver.pick_and_resolve("playlist", exclude_id="a")

        self.assertEqual(result["video_id"], "b")
        self.assertEqual(result["subtitle_url"], "sub")

    def test_prefetched_stream_is_consumed_once(self):
        cached = {"url": "stream", "video_id": "video", "title": "Title", "ts": resolver.time.time()}
        with patch.object(resolver, "save_persistent_cache"):
            with resolver._cache_lock:
                resolver._prefetch_cache["playlist"] = cached
            self.assertEqual(resolver.get_prefetched("playlist")["url"], "stream")
            self.assertIsNone(resolver.get_prefetched("playlist"))

    def test_prefetch_queue_allows_only_one_background_job(self):
        class PendingFuture:
            def done(self):
                return False

        with resolver._cache_lock:
            resolver._prefetch_cache.clear()
            resolver._prefetch_futures.clear()
        try:
            with patch.object(resolver._prefetch_executor, "submit", return_value=PendingFuture()):
                self.assertTrue(resolver.schedule_prefetch("first"))
                self.assertFalse(resolver.schedule_prefetch("first"))
                self.assertFalse(resolver.schedule_prefetch("second"))
        finally:
            with resolver._cache_lock:
                resolver._prefetch_futures.clear()

    def test_failed_matching_prefetch_is_not_resolved_twice(self):
        future = Mock()
        future.result.return_value = None
        with resolver._cache_lock:
            resolver._prefetch_futures["playlist"] = future
        try:
            with self.assertRaisesRegex(RuntimeError, "Background stream resolution failed"):
                resolver.wait_for_prefetch("playlist")
        finally:
            with resolver._cache_lock:
                resolver._prefetch_futures.clear()


class PlayerApiTests(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()

    def test_pages_render_without_injected_route_constants(self):
        with patch.object(web, "get_categories", return_value=[]) as get_categories:
            home = self.client.get("/")
        submit = self.client.get("/submit")

        self.assertEqual(home.status_code, 200)
        self.assertIn(b"/static/js/piStuff.js", home.data)
        self.assertIn(b'id="loading-indicator"', home.data)
        self.assertIn(b'id="playback-state"', home.data)
        self.assertIn(b'id="paused-controls"', home.data)
        self.assertIn(b'data-paused-action="play"', home.data)
        self.assertNotIn(b"API_PLAY_URL", home.data)
        self.assertEqual(submit.status_code, 200)
        self.assertIn(b"/static/js/submitForm.js", submit.data)
        self.assertIn(b'id="submit-action" class="submit-action" hidden', submit.data)
        self.assertIn(b'id="submit-status" class="submit-status"', submit.data)
        self.assertNotIn(b'id="display-message"', home.data + submit.data)
        self.assertIn("no-store", home.headers["Cache-Control"])
        get_categories.assert_called_once_with(force_refresh=True)
        static = self.client.get("/static/js/piStuff.js")
        self.assertIn("no-cache", static.headers["Cache-Control"])
        static.close()

    def test_player_routes_validate_required_ids(self):
        self.assertEqual(self.client.get("/api/player/next").status_code, 400)
        self.assertEqual(self.client.get("/api/player/resolve").status_code, 400)
        self.assertEqual(self.client.get("/api/player/prefetch").status_code, 400)

    def test_resolve_route_returns_player_payload(self):
        with patch.object(resolver, "resolve_stream_url", return_value=("url", "Title", "captions")):
            response = self.client.get("/api/player/resolve?video_id=abc")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "url": "url",
            "video_id": "abc",
            "title": "Title",
            "subtitle_url": "captions",
        })

    def test_next_route_consumes_and_replenishes_prefetch(self):
        ready = {"url": "url", "video_id": "abc", "title": "Title", "subtitle_url": ""}
        with (
            patch.object(resolver, "get_prefetched", return_value=ready),
            patch.object(resolver, "wait_for_prefetch") as wait,
            patch.object(resolver, "pick_and_resolve") as resolve,
            patch.object(resolver, "schedule_prefetch") as schedule,
        ):
            response = self.client.get("/api/player/next?playlist_id=PL123")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["video_id"], "abc")
        wait.assert_not_called()
        resolve.assert_not_called()
        schedule.assert_called_once_with("PL123", "abc")


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.original_cache = catalog._cache.copy()

    def tearDown(self):
        with catalog._lock:
            catalog._cache.update(self.original_cache)

    def test_force_refresh_bypasses_fresh_cache(self):
        with catalog._lock:
            catalog._cache.update(data=[{"name": "old"}], ts=catalog.time.time())

        with patch.object(catalog, "_fetch", return_value=[{"name": "new"}]) as fetch:
            categories = catalog.get_categories(force_refresh=True)

        self.assertEqual(categories, [{"name": "new"}])
        fetch.assert_called_once_with()

    def test_force_refresh_keeps_last_good_catalog_on_failure(self):
        cached = [{"name": "available offline"}]
        with catalog._lock:
            catalog._cache.update(data=cached, ts=catalog.time.time())

        with patch.object(catalog, "_fetch", return_value=None):
            categories = catalog.get_categories(force_refresh=True)

        self.assertEqual(categories, cached)


if __name__ == "__main__":
    unittest.main()
