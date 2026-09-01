import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import collect_data


def make_match(match_id, category="football", date_ms=1, title=None):
    return {
        "id": match_id,
        "title": title or match_id,
        "category": category,
        "date": date_ms,
        "poster": None,
        "teams": {},
        "sources": [],
    }


class ConfigurationTests(unittest.TestCase):
    def test_empty_environment_values_use_default_base_url(self):
        with patch.dict(
            os.environ,
            {"STREAM_SITE_URL": "", "BASE_URL": ""},
            clear=False,
        ):
            self.assertEqual(
                collect_data.resolve_base_url(), collect_data.DEFAULT_BASE_URL
            )

    def test_invalid_base_url_is_rejected(self):
        with (
            patch.dict(
                os.environ,
                {"STREAM_SITE_URL": "not-a-url", "BASE_URL": ""},
                clear=False,
            ),
            self.assertRaises(collect_data.CollectorError),
        ):
            collect_data.resolve_base_url()


class ClassificationTests(unittest.TestCase):
    def test_live_api_is_authoritative(self):
        match = make_match("live-id", date_ms=100)
        self.assertEqual(collect_data.classify_match(match, {"live-id"}, 1000), "live")

    def test_future_and_negative_sentinel_are_upcoming(self):
        self.assertEqual(
            collect_data.classify_match(
                make_match("future", date_ms=2000), set(), 1000
            ),
            "upcoming",
        )
        self.assertEqual(
            collect_data.classify_match(
                make_match("sentinel", date_ms=-3600000), set(), 1000
            ),
            "upcoming",
        )

    def test_past_non_live_match_is_ended_not_upcoming(self):
        self.assertEqual(
            collect_data.classify_match(make_match("ended", date_ms=500), set(), 1000),
            "ended",
        )

    def test_zero_date_is_24_7_live(self):
        self.assertEqual(
            collect_data.classify_match(make_match("always", date_ms=0), set(), 1000),
            "live",
        )

    def test_invalid_date_is_rejected(self):
        with self.assertRaises(collect_data.CollectorError):
            collect_data.classify_match(
                make_match("bad", date_ms="tomorrow"), set(), 1000
            )


class TimeAndOrderingTests(unittest.TestCase):
    def test_positive_timestamp_is_converted_to_bangladesh_time(self):
        utc_time = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        timestamp_ms = int(utc_time.timestamp() * 1000)
        self.assertEqual(
            collect_data.format_bd_time(timestamp_ms),
            "01 Sep 2026, 06:00 AM (BD Time)",
        )

    def test_upcoming_matches_are_sorted_by_epoch_not_display_text(self):
        midnight = int(
            datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        ten_pm = int(
            datetime(2026, 9, 2, 16, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        processed = collect_data.process_upcoming_matches(
            [
                make_match("ten-pm", date_ms=ten_pm),
                make_match("midnight", date_ms=midnight),
            ]
        )
        self.assertEqual([item["id"] for item in processed], ["midnight", "ten-pm"])

    def test_undated_sentinel_is_sorted_after_concrete_times(self):
        future = int(datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp() * 1000)
        processed = collect_data.process_upcoming_matches(
            [
                make_match("unknown", category="cricket", date_ms=-3600000),
                make_match("dated", category="cricket", date_ms=future),
            ]
        )
        self.assertEqual([item["id"] for item in processed], ["dated", "unknown"])


class StreamAndOutputTests(unittest.TestCase):
    def test_stream_entries_are_deduplicated_by_embed_input(self):
        entries = [
            {"embedUrl": "https://embed.st/embed/source/one/1"},
            {"embedUrl": "https://embed.st/embed/source/one/1"},
            {"embedUrl": "https://embed.st/embed/source/two/1"},
            {"embedUrl": ""},
        ]
        result = collect_data.deduplicate_stream_entries(entries)
        self.assertEqual(len(result), 2)

    def test_only_direct_media_urls_are_publishable(self):
        self.assertTrue(
            collect_data.is_direct_media_url("https://cdn.example/live/main.m3u8")
        )
        self.assertTrue(
            collect_data.is_direct_media_url("https://cdn.example/live/manifest.mpd")
        )
        self.assertFalse(
            collect_data.is_direct_media_url("https://embed.st/embed/source/id/1")
        )
        self.assertFalse(
            collect_data.is_direct_media_url("https://embed.st/embed/source/playlist")
        )

    def test_bundle_has_no_embed_fields_and_consistent_totals(self):
        live_match = {
            "id": "match-1",
            "title": "Match One",
            "category": "football",
            "status": "LIVE_NOW",
            "start_time_bd": "01 Sep 2026, 06:00 AM (BD Time)",
            "start_epoch_ms": 1,
            "poster": "https://example.test/poster.webp",
            "headers": {},
            "streams": [
                {
                    "channel_name": "Main (HD)",
                    "channel_poster": "https://example.test/poster.webp",
                    "hd": True,
                    "direct_stream_url": "https://cdn.example/live/main.m3u8",
                }
            ],
        }
        bundle = collect_data.build_output_bundle([], [], [live_match], [])
        collect_data.validate_output_bundle(bundle)
        payload = json.loads(bundle["football/live.json"])
        self.assertEqual(payload["total_matches"], 1)
        self.assertEqual(payload["total_streams"], 1)
        self.assertNotIn("embedUrl", bundle["football/live.json"])

    def test_empty_live_match_is_rejected(self):
        payload = collect_data.build_json_file(
            "Football Live",
            [{"id": "empty", "status": "LIVE_NOW", "streams": []}],
            is_live=True,
        )
        bundle = {"football/live.json": json.dumps(payload)}
        with self.assertRaises(collect_data.CollectorError):
            collect_data.validate_output_bundle(bundle)

    def test_staged_writer_creates_all_expected_files(self):
        bundle = collect_data.build_output_bundle([], [], [], [])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            collect_data.write_output_bundle(root, bundle)
            self.assertEqual(
                sorted(path.relative_to(root).as_posix() for path in root.glob("*/*")),
                sorted(bundle),
            )


class FailureSafetyTests(unittest.TestCase):
    def test_required_api_failure_leaves_existing_outputs_untouched(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            existing = root / "football" / "upcoming.json"
            existing.parent.mkdir(parents=True)
            existing.write_text("last-known-good", encoding="utf-8")

            with (
                patch.object(
                    collect_data,
                    "fetch_json",
                    side_effect=collect_data.CollectorError("upstream unavailable"),
                ),
                self.assertRaises(collect_data.CollectorError),
            ):
                asyncio.run(collect_data.collect(root))

            self.assertEqual(existing.read_text(encoding="utf-8"), "last-known-good")


if __name__ == "__main__":
    unittest.main()
