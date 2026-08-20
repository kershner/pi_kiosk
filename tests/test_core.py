import unittest
from unittest.mock import patch

import resolver
import app as web
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


class PlayerApiTests(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()

    def test_pages_render_without_injected_route_constants(self):
        with patch.object(web, "get_categories", return_value=[]):
            home = self.client.get("/")
        submit = self.client.get("/submit")

        self.assertEqual(home.status_code, 200)
        self.assertIn(b"/static/js/piStuff.js", home.data)
        self.assertNotIn(b"API_PLAY_URL", home.data)
        self.assertEqual(submit.status_code, 200)
        self.assertIn(b"/static/js/submitForm.js", submit.data)
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


if __name__ == "__main__":
    unittest.main()
