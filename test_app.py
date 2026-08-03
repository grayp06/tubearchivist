"""Unit and route tests for TubeArchivist Lite."""

import tempfile
import unittest
import io
from pathlib import Path
from unittest.mock import patch
from PIL import Image

from app import (
    APP,
    collection_entry_url,
    collection_kind,
    collection_probe_url,
    enqueue_url,
    expand_collection,
    looks_like_single_video,
    quality_format,
    validate_url,
    validate_settings,
    validate_advanced_settings,
    video_identity,
    youtube_operation,
    yt_dlp_download_options,
)
from features import (
    add_playlist_media,
    add_subscription,
    archive_stats,
    create_playlist,
    create_snapshot,
    export_archive,
    list_playlist_media,
    list_playlists,
    list_subscriptions,
    prioritize_download,
    queue_subscription_scan,
    rescan_filesystem,
    retry_download,
    save_external_playlist,
    search_media,
    set_media_favorite,
    set_media_tags,
    subscriptions_opml,
    initialize_features,
)
from library import (
    add_ignored,
    cancel_download,
    claim_next_download,
    connect,
    defer_youtube_downloads,
    delete_media,
    delete_media_and_ignore,
    enqueue_collection_items,
    enqueue_download,
    get_media,
    get_settings,
    initialize_database,
    is_download_cancelled,
    is_ignored,
    list_channels,
    list_downloads,
    list_ignored,
    list_media,
    recover_interrupted_downloads,
    remove_ignored,
    save_media,
    seconds_until_next_download,
    seconds_until_youtube_available,
    set_media_watched,
    update_media_progress,
    update_settings,
)
from storage import (
    archive_download,
    cache_channel_art,
    cache_playlist_thumbnail,
    cache_video_thumbnail,
    channel_art_relative_path,
    media_relative_path,
    migrate_storage_layout,
    playlist_thumb_relative_path,
    video_thumb_relative_path,
)


class ValidateUrlTests(unittest.TestCase):
    def test_accepts_http_and_https(self):
        self.assertEqual(
            validate_url(" https://youtu.be/jNQXAC9IVRw "),
            "https://youtu.be/jNQXAC9IVRw",
        )
        self.assertEqual(
            validate_url("http://example.com/video"),
            "http://example.com/video",
        )

    def test_rejects_empty_or_non_http_urls(self):
        for value in (None, "", "not a url", "file:///etc/passwd"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_url(value)

    def test_distinguishes_video_playlist_and_channel_urls(self):
        self.assertTrue(
            looks_like_single_video("https://www.youtube.com/watch?v=video-id")
        )
        self.assertTrue(looks_like_single_video("https://youtu.be/video-id"))
        self.assertFalse(
            looks_like_single_video("https://www.youtube.com/playlist?list=list-id")
        )
        self.assertFalse(
            looks_like_single_video(
                "https://www.youtube.com/watch?v=video-id&list=list-id"
            )
        )
        self.assertEqual(
            collection_kind("https://www.youtube.com/playlist?list=list-id"),
            "playlist",
        )
        self.assertEqual(
            collection_kind("https://www.youtube.com/@example/videos"),
            "channel",
        )
        self.assertEqual(
            collection_probe_url("https://www.youtube.com/@example"),
            "https://www.youtube.com/@example/videos",
        )
        self.assertEqual(
            collection_probe_url("https://www.youtube.com/@example/videos"),
            "https://www.youtube.com/@example/videos",
        )
        self.assertEqual(
            collection_entry_url(
                {"id": "video-id", "extractor_key": "Youtube"}
            ),
            "https://www.youtube.com/watch?v=video-id",
        )
        self.assertEqual(
            video_identity("https://youtu.be/video-id?t=30"),
            ("Youtube", "video-id"),
        )
        self.assertEqual(
            video_identity("https://www.youtube.com/shorts/video-id"),
            ("Youtube", "video-id"),
        )


class LibraryTests(unittest.TestCase):
    def test_saves_and_updates_download_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "data" / "library.db"
            file_path = root / "video.mp4"
            file_path.write_bytes(b"test-video")
            info = {
                "id": "video-id",
                "extractor_key": "Youtube",
                "title": "Original title",
                "channel": "Example channel",
                "channel_id": "channel-id",
                "channel_url": "https://youtube.com/channel/channel-id",
                "channel_thumbnail": "https://example.com/channel.jpg",
                "duration": 19,
                "upload_date": "20050424",
                "description": "Description",
                "thumbnail": "https://example.com/thumb.jpg",
                "webpage_url": "https://youtu.be/video-id",
                "ext": "mp4",
                "nested": {"metadata": True},
            }

            initialize_database(database_path)
            first_id = save_media(
                database_path,
                "https://youtu.be/video-id",
                info,
                file_path,
            )
            info["title"] = "Updated title"
            second_id = save_media(
                database_path,
                "https://youtube.com/watch?v=video-id",
                info,
                file_path,
            )

            records = list_media(database_path, include_metadata=True)
            self.assertEqual(first_id, second_id)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["title"], "Updated title")
            self.assertEqual(records[0]["file_path"], str(file_path.resolve()))
            self.assertEqual(records[0]["file_size"], len(b"test-video"))
            self.assertTrue(records[0]["metadata"]["nested"]["metadata"])
            record = get_media(database_path, first_id)
            self.assertEqual(record["video_id"], "video-id")
            self.assertIsNotNone(record["channel_db_id"])
            self.assertNotIn("metadata_json", record)
            self.assertIsNone(get_media(database_path, first_id + 1))
            channels = list_channels(database_path)
            self.assertEqual(len(channels), 1)
            self.assertEqual(channels[0]["video_count"], 1)
            self.assertEqual(
                channels[0]["thumbnail_url"],
                "https://example.com/channel.jpg",
            )
            self.assertTrue(set_media_watched(database_path, first_id, True))
            self.assertEqual(get_media(database_path, first_id)["watched"], 1)
            self.assertEqual(get_media(database_path, first_id)["watch_progress"], 100)

    def test_metadata_refresh_upgrades_reconciled_file_in_place(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "library.db"
            file_path = root / "Example [video-id].mp4"
            file_path.write_bytes(b"video")
            (root / "Example [video-id].en.vtt").write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nsearchable captions\n",
                encoding="utf-8",
            )
            initialize_database(database_path)

            reconciled_id = save_media(
                database_path,
                "https://www.youtube.com/watch?v=video-id",
                {
                    "id": "video-id",
                    "extractor": "Filesystem",
                    "title": "Example",
                    "webpage_url": "https://www.youtube.com/watch?v=video-id",
                },
                file_path,
            )
            set_media_favorite(database_path, reconciled_id, True)
            set_media_tags(database_path, reconciled_id, "keep-me")
            update_media_progress(database_path, reconciled_id, 50)

            refreshed_id = save_media(
                database_path,
                "https://www.youtube.com/watch?v=video-id",
                {
                    "id": "video-id",
                    "extractor_key": "Youtube",
                    "title": "Refreshed Example",
                    "channel": "Example Channel",
                    "channel_id": "channel-id",
                    "webpage_url": "https://www.youtube.com/watch?v=video-id",
                },
                file_path,
            )

            self.assertEqual(refreshed_id, reconciled_id)
            self.assertEqual(len(list_media(database_path)), 1)
            refreshed = get_media(database_path, reconciled_id)
            self.assertEqual(refreshed["extractor"], "Youtube")
            self.assertEqual(refreshed["title"], "Refreshed Example")
            self.assertEqual(refreshed["favorite"], 1)
            self.assertEqual(refreshed["watch_progress"], 50)
            self.assertEqual(refreshed["tags"], "keep-me")
            self.assertIn("searchable captions", refreshed["subtitle_text"])

    def test_watch_progress_normalizes_thresholds_and_stays_watched(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "library.db"
            file_path = root / "video.mp4"
            file_path.write_bytes(b"video")
            initialize_database(database_path)
            media_id = save_media(
                database_path,
                "https://example.com/video",
                {"id": "progress", "title": "Progress", "extractor": "test"},
                file_path,
            )

            self.assertEqual(
                update_media_progress(database_path, media_id, 4.99)["watch_progress"],
                0,
            )
            partial = update_media_progress(database_path, media_id, 42.345)
            self.assertEqual(partial["watch_progress"], 42.34)
            self.assertEqual(partial["watched"], 0)
            complete = update_media_progress(database_path, media_id, 95)
            self.assertEqual(complete["watch_progress"], 100)
            self.assertEqual(complete["watched"], 1)
            replayed = update_media_progress(database_path, media_id, 2)
            self.assertEqual(replayed["watch_progress"], 0)
            self.assertEqual(replayed["watched"], 1)
class DownloadQueueTests(unittest.TestCase):
    def test_queued_and_active_video_rows_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "library.db"
            initialize_database(database_path)
            queued = enqueue_download(
                database_path,
                "https://youtu.be/queued",
                kind="video",
            )
            self.assertTrue(cancel_download(database_path, queued["id"]))
            self.assertTrue(is_download_cancelled(database_path, queued["id"]))
            self.assertIsNone(claim_next_download(database_path))

            active = enqueue_download(
                database_path,
                "https://youtu.be/active",
                kind="video",
            )
            self.assertEqual(claim_next_download(database_path)["id"], active["id"])
            self.assertTrue(cancel_download(database_path, active["id"]))
            self.assertTrue(is_download_cancelled(database_path, active["id"]))
            self.assertFalse(cancel_download(database_path, active["id"]))

    def test_queue_is_fifo_persistent_and_recovers_active_rows(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "library.db"
            initialize_database(database_path)
            first = enqueue_download(database_path, "https://example.com/one")
            second = enqueue_download(database_path, "https://example.com/two")
            claimed = claim_next_download(database_path)
            self.assertEqual(claimed["id"], first["id"])
            third = enqueue_download(database_path, "https://example.com/three")
            self.assertIsNone(claim_next_download(database_path))

            downloads = list_downloads(database_path)
            by_id = {item["id"]: item for item in downloads}
            self.assertEqual(by_id[first["id"]]["status"], "downloading")
            self.assertEqual(by_id[second["id"]]["queue_position"], 1)
            self.assertEqual(by_id[third["id"]]["queue_position"], 2)

            self.assertEqual(recover_interrupted_downloads(database_path), 1)
            self.assertEqual(claim_next_download(database_path)["id"], first["id"])

    def test_collection_expands_to_unique_unarchived_video_rows(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "library.db"
            archived_file = root / "archived.mp4"
            archived_file.write_bytes(b"archived")
            initialize_database(database_path)
            save_media(
                database_path,
                "https://www.youtube.com/watch?v=archived",
                {
                    "id": "archived",
                    "title": "Already archived",
                    "extractor_key": "Youtube",
                    "webpage_url": "https://www.youtube.com/watch?v=archived",
                },
                archived_file,
            )
            parent = enqueue_download(
                database_path,
                "https://www.youtube.com/playlist?list=test",
            )
            items = [
                {
                    "source_url": "https://www.youtube.com/watch?v=archived",
                    "source_position": 1,
                    "title": "Already archived",
                },
                {
                    "source_url": "https://www.youtube.com/watch?v=new-video",
                    "source_position": 2,
                    "title": "New video",
                },
                {
                    "source_url": "https://www.youtube.com/watch?v=new-video",
                    "source_position": 3,
                    "title": "Duplicate entry",
                },
            ]
            self.assertEqual(
                enqueue_collection_items(database_path, parent["id"], items),
                1,
            )
            children = [
                item
                for item in list_downloads(database_path)
                if item["parent_id"] == parent["id"]
            ]
            self.assertEqual(len(children), 1)
            self.assertEqual(children[0]["kind"], "video")
            self.assertEqual(children[0]["source_position"], 2)

    def test_ignore_list_blocks_collection_identity_and_can_be_removed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "library.db"
            initialize_database(database_path)
            add_ignored(
                database_path,
                "https://youtu.be/ignored",
                "Youtube",
                "ignored",
                "Ignored title",
            )
            self.assertTrue(
                is_ignored(
                    database_path,
                    "https://www.youtube.com/watch?v=ignored",
                    "Youtube",
                    "ignored",
                )
            )
            parent = enqueue_download(
                database_path,
                "https://www.youtube.com/playlist?list=test",
            )
            inserted = enqueue_collection_items(
                database_path,
                parent["id"],
                [
                    {
                        "source_url": "https://www.youtube.com/watch?v=ignored",
                        "extractor": "Youtube",
                        "video_id": "ignored",
                        "title": "Ignored title",
                    }
                ],
            )
            self.assertEqual(inserted, 0)
            ignored = list_ignored(database_path)
            self.assertEqual(len(ignored), 1)
            self.assertTrue(remove_ignored(database_path, ignored[0]["id"]))
            self.assertFalse(
                is_ignored(database_path, "https://youtu.be/ignored", "Youtube", "ignored")
            )

    def test_downloaded_media_can_be_deleted_from_db_and_ignored(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "library.db"
            file_path = root / "video.mp4"
            file_path.write_bytes(b"video")
            initialize_database(database_path)
            media_id = save_media(
                database_path,
                "https://youtu.be/delete-me",
                {
                    "id": "delete-me",
                    "extractor_key": "Youtube",
                    "title": "Delete me",
                    "webpage_url": "https://www.youtube.com/watch?v=delete-me",
                },
                file_path,
            )
            deleted = delete_media_and_ignore(database_path, media_id)
            self.assertEqual(deleted["video_id"], "delete-me")
            self.assertIsNone(get_media(database_path, media_id))
            self.assertTrue(
                is_ignored(
                    database_path,
                    "https://youtu.be/delete-me",
                    "Youtube",
                    "delete-me",
                )
            )

    def test_downloaded_media_can_be_deleted_without_being_ignored(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "library.db"
            initialize_database(database_path)
            file_path = root / "video.mp4"
            file_path.write_bytes(b"video")
            media_id = save_media(
                database_path,
                "https://youtu.be/delete-again",
                {
                    "id": "delete-again",
                    "extractor_key": "Youtube",
                    "title": "Delete and allow again",
                    "webpage_url": "https://www.youtube.com/watch?v=delete-again",
                },
                file_path,
            )
            deleted = delete_media(database_path, media_id)
            self.assertEqual(deleted["video_id"], "delete-again")
            self.assertIsNone(get_media(database_path, media_id))
            self.assertFalse(
                is_ignored(
                    database_path,
                    "https://youtu.be/delete-again",
                    "Youtube",
                    "delete-again",
                )
            )

    def test_collection_expansion_records_parent_type_and_count(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "library.db"
            initialize_database(database_path)
            parent = enqueue_download(
                database_path,
                "https://www.youtube.com/@example/videos",
            )
            info = {
                "_type": "playlist",
                "title": "Example channel - Videos",
                "entries": [
                    {
                        "id": "one",
                        "title": "One",
                        "extractor_key": "Youtube",
                    },
                    {
                        "id": "two",
                        "title": "Two",
                        "extractor_key": "Youtube",
                    },
                ],
            }
            enqueue_collection_items(
                database_path,
                parent["id"],
                [
                    {
                        "source_url": "https://www.youtube.com/watch?v=one",
                        "source_position": 1,
                        "title": "One",
                    }
                ],
            )
            with patch("app.DATABASE_PATH", database_path):
                expand_collection(parent, info)
            downloads = list_downloads(database_path)
            parent_row = next(item for item in downloads if item["id"] == parent["id"])
            children = [item for item in downloads if item["parent_id"] == parent["id"]]
            self.assertEqual(parent_row["kind"], "channel")
            self.assertEqual(parent_row["status"], "complete")
            self.assertEqual(parent_row["item_count"], 2)
            self.assertEqual([item["title"] for item in children], ["One", "Two"])

    def test_global_youtube_delay_paces_subscription_and_manual_downloads(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "library.db"
            initialize_database(database_path)
            update_settings(database_path, {"subscription_download_delay": 120})
            subscription = add_subscription(
                database_path,
                "https://www.youtube.com/@delayed/videos",
                "channel",
                "Delayed channel",
            )
            self.assertEqual(queue_subscription_scan(database_path), 1)
            parent = claim_next_download(database_path)
            self.assertEqual(parent["subscription_id"], subscription["id"])
            with patch("app.DATABASE_PATH", database_path):
                expand_collection(
                    parent,
                    {
                        "_type": "playlist",
                        "title": "Delayed channel - Videos",
                        "entries": [
                            {
                                "id": "delayed-video",
                                "title": "Delayed video",
                                "extractor_key": "Youtube",
                            }
                        ],
                    },
                )

            child = next(
                item
                for item in list_downloads(database_path)
                if item["parent_id"] == parent["id"]
            )
            self.assertEqual(child["subscription_id"], subscription["id"])
            manual = enqueue_download(
                database_path,
                "https://youtu.be/manual-too",
                kind="video",
            )
            defer_youtube_downloads(database_path, 120)
            queued = {
                item["id"]: item
                for item in list_downloads(database_path)
                if item["status"] == "queued"
            }
            self.assertIsNotNone(queued[child["id"]]["available_at"])
            self.assertIsNotNone(queued[manual["id"]]["available_at"])
            wait_seconds = seconds_until_next_download(database_path)
            self.assertGreater(wait_seconds, 0)
            self.assertLessEqual(wait_seconds, 120)
            self.assertIsNone(claim_next_download(database_path))

            defer_youtube_downloads(database_path, 0)
            self.assertEqual(claim_next_download(database_path)["id"], child["id"])
            with connect(database_path) as connection:
                connection.execute(
                    "UPDATE downloads SET status='complete' WHERE id=?",
                    (child["id"],),
                )
            defer_youtube_downloads(database_path, 120)
            self.assertIsNone(claim_next_download(database_path))
            defer_youtube_downloads(database_path, 0)
            self.assertEqual(claim_next_download(database_path)["id"], manual["id"])

    def test_collection_expansion_respects_persistent_limit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "library.db"
            initialize_database(database_path)
            update_settings(database_path, {"collection_limit": 2})
            enqueue_download(
                database_path,
                "https://www.youtube.com/watch?v=1",
                kind="video",
                title="Already pending",
            )
            parent = enqueue_download(
                database_path,
                "https://www.youtube.com/playlist?list=limited",
            )
            info = {
                "_type": "playlist",
                "title": "Limited playlist",
                "entries": [
                    {"id": str(index), "title": str(index), "extractor_key": "Youtube"}
                    for index in range(1, 5)
                ],
            }
            with patch("app.DATABASE_PATH", database_path):
                expand_collection(parent, info)
            rows = list_downloads(database_path)
            parent_row = next(item for item in rows if item["id"] == parent["id"])
            children = [item for item in rows if item["parent_id"] == parent["id"]]
            self.assertEqual(parent_row["item_count"], 2)
            self.assertEqual(len(children), 2)
            self.assertEqual(
                [item["source_url"] for item in children],
                [
                    "https://www.youtube.com/watch?v=2",
                    "https://www.youtube.com/watch?v=3",
                ],
            )


class SettingsTests(unittest.TestCase):
    def test_direct_youtube_operation_persists_global_cooldown(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "library.db"
            initialize_database(database_path)
            update_settings(database_path, {"subscription_download_delay": 30})
            with patch("app.DATABASE_PATH", database_path), youtube_operation():
                pass
            self.assertGreater(seconds_until_youtube_available(database_path), 0)
            manual = enqueue_download(
                database_path,
                "https://youtu.be/cooldown-manual",
                kind="video",
            )
            self.assertIsNotNone(manual["available_at"])
            self.assertIsNone(claim_next_download(database_path))

    def test_advanced_settings_are_mapped_to_yt_dlp(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "library.db"
            download_dir = root / "downloads"
            cache_dir = root / "cache"
            cookie_path = root / "cookies.txt"
            download_dir.mkdir()
            cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            initialize_database(database_path)
            update_settings(
                database_path,
                {
                    "download_quality": "480",
                    "download_subtitles": 1,
                    "subtitle_languages": "en,fr",
                    "write_info_json": 1,
                    "download_comments": 1,
                    "sponsorblock_categories": "sponsor,selfpromo",
                    "rate_limit": "64",
                    "output_template": "%(title)s [%(id)s].%(ext)s",
                    "cookie_path": str(cookie_path),
                },
            )
            with (
                patch("app.DATABASE_PATH", database_path),
                patch("app.DOWNLOAD_DIR", download_dir),
                patch("app.CACHE_DIR", cache_dir),
            ):
                options = yt_dlp_download_options(1, {})

            self.assertIn("height<=480", options["format"])
            self.assertIn("vcodec^=avc1", options["format"])
            self.assertEqual(options["format_sort"][0], "vcodec:h264")
            self.assertEqual(options["subtitleslangs"], ["en", "fr"])
            self.assertNotIn("writeinfojson", options)
            self.assertTrue(options["getcomments"])
            self.assertEqual(options["ratelimit"], 64 * 1024)
            self.assertEqual(options["cookiefile"], str(cookie_path))
            self.assertEqual(
                options["postprocessors"][1]["categories"],
                ["sponsor", "selfpromo"],
            )
            self.assertEqual(options["postprocessors"][0]["key"], "FFmpegMetadata")
            self.assertEqual(
                options["outtmpl"],
                str(cache_dir / "download" / "%(id)s.mp4"),
            )

    def test_defaults_validation_and_quality_formats(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "library.db"
            initialize_database(database_path)
            defaults = get_settings(database_path)
            self.assertEqual(defaults["collection_limit"], 100)
            self.assertEqual(defaults["download_quality"], "1080")
            self.assertEqual(defaults["page_size"], 24)
            self.assertEqual(defaults["subscription_download_delay"], 60)
            self.assertTrue(defaults["write_info_json"])
            update_settings(
                database_path,
                {"collection_limit": 250, "download_quality": "720"},
            )
            self.assertEqual(get_settings(database_path)["collection_limit"], 250)
            self.assertEqual(get_settings(database_path)["download_quality"], "720")
        self.assertEqual(
            validate_settings("100", "1080"),
            {"collection_limit": 100, "download_quality": "1080"},
        )
        with self.assertRaises(ValueError):
            validate_settings("0", "1080")
        with self.assertRaises(ValueError):
            validate_settings("100", "invalid")
        selector_1080 = quality_format("1080")
        selector_best = quality_format("best")
        self.assertIn("height<=1080", selector_1080)
        self.assertEqual(selector_1080.count("vcodec^=avc1"), 2)
        self.assertEqual(selector_best.count("vcodec^=avc1"), 2)
        self.assertIn("acodec^=mp4a", selector_best)
        self.assertNotIn("bestvideo+bestaudio", selector_best)
        advanced = validate_advanced_settings(
            {
                "collection_limit": "50",
                "download_quality": "720",
                "page_size": "30",
                "download_subtitles": "1",
                "subtitle_languages": "en,fr",
                "write_info_json": "1",
                "sponsorblock_categories": "sponsor",
                "rate_limit": "512",
                "output_template": "%(title)s [%(id)s].%(ext)s",
                "cookie_path": "/data/cookies.txt",
            }
        )
        self.assertEqual(advanced["page_size"], 30)
        self.assertEqual(advanced["download_subtitles"], 1)
        self.assertEqual(advanced["rate_limit"], "512")


class ArchiveFeatureTests(unittest.TestCase):
    def _save_video(self, root: Path, database_path: Path, video_id: str, title: str) -> int:
        path = root / f"{title} [{video_id}].mp4"
        path.write_bytes(video_id.encode())
        return save_media(
            database_path,
            f"https://youtu.be/{video_id}",
            {
                "id": video_id,
                "extractor_key": "Youtube",
                "title": title,
                "description": f"Description for {title}",
                "channel": "Feature Channel",
                "channel_id": "feature-channel",
                "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
            },
            path,
        )

    def test_subscriptions_queue_once_and_export_opml(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "library.db"
            initialize_database(database_path)
            add_subscription(
                database_path,
                "https://www.youtube.com/@feature/videos",
                "channel",
                "Feature channel",
            )
            self.assertEqual(queue_subscription_scan(database_path), 1)
            self.assertEqual(queue_subscription_scan(database_path), 0)
            self.assertEqual(len(list_subscriptions(database_path)), 1)
            opml = subscriptions_opml(database_path)
            self.assertIn(b"Feature channel", opml)
            self.assertIn(b"youtube.com/@feature", opml)

    def test_custom_playlists_search_favorites_and_tags(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "library.db"
            initialize_database(database_path)
            first = self._save_video(root, database_path, "one", "Alpha archive")
            self._save_video(root, database_path, "two", "Beta archive")
            playlist = create_playlist(database_path, "My picks", "Local list")
            self.assertTrue(add_playlist_media(database_path, playlist["id"], first))
            self.assertEqual(len(list_playlist_media(database_path, playlist["id"])), 1)
            self.assertEqual(list_playlists(database_path)[0]["video_count"], 1)
            self.assertTrue(set_media_favorite(database_path, first, True))
            self.assertTrue(set_media_tags(database_path, first, "science, favourite"))
            result = search_media(database_path, query="science", favorite=True)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["title"], "Alpha archive")
            self.assertIn("science", result["items"][0]["tags"])

    def test_priority_retry_stats_snapshot_export_and_rescan(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "library.db"
            downloads = root / "downloads"
            snapshots = root / "snapshots"
            downloads.mkdir()
            initialize_database(database_path)
            first = enqueue_download(database_path, "https://youtu.be/first", kind="video")
            second = enqueue_download(database_path, "https://youtu.be/second", kind="video")
            self.assertTrue(prioritize_download(database_path, second["id"]))
            self.assertEqual(claim_next_download(database_path)["id"], second["id"])
            self.assertTrue(cancel_download(database_path, second["id"]))
            self.assertTrue(retry_download(database_path, second["id"]))
            orphan = downloads / "Recovered [orphan-id].mp4"
            orphan.write_bytes(b"orphan")
            scan = rescan_filesystem(database_path, downloads)
            self.assertEqual(scan["added"], 1)
            duplicate = downloads / "Duplicate [orphan-id].mp4"
            duplicate.write_bytes(b"duplicate")
            second_scan = rescan_filesystem(database_path, downloads)
            self.assertEqual(second_scan["added"], 0)
            self.assertEqual(
                get_media(database_path, 1)["file_path"], str(orphan.resolve())
            )
            self.assertEqual(archive_stats(database_path)["videos"], 1)
            snapshot = create_snapshot(database_path, snapshots)
            self.assertTrue(snapshot.is_file())
            exported = export_archive(database_path)
            self.assertEqual(exported["version"], 1)
            self.assertEqual(len(exported["media"]), 1)


class FlaskEndpointTests(unittest.TestCase):
    def test_diagnostic_and_queue_apis(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "library.db"
            initialize_database(database_path)
            with patch("app.DATABASE_PATH", database_path):
                client = APP.test_client()
                health = client.get("/api/health")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.get_json(), {"status": "ok"})
                self.assertEqual(client.get("/api/jobs").get_json(), [])
                self.assertEqual(client.get("/api/library").get_json(), [])

                invalid = client.post("/api/downloads", json={})
                self.assertEqual(invalid.status_code, 400)
                queued = client.post(
                    "/api/downloads", json={"url": "https://youtu.be/api-video"}
                )
                self.assertEqual(queued.status_code, 202)
                self.assertEqual(queued.get_json()["status"], "queued")
                self.assertEqual(len(client.get("/api/jobs").get_json()), 1)

                api_missing = client.get("/api/not-found")
                self.assertEqual(api_missing.status_code, 404)
                self.assertEqual(api_missing.get_json(), {"error": "Not found."})
                page_missing = client.get("/not-found")
                self.assertEqual(page_missing.status_code, 404)
                self.assertIn(b"Not found", page_missing.data)

    def test_archive_feature_pages_and_actions_are_server_rendered(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "library.db"
            download_dir = root / "downloads"
            snapshot_dir = root / "snapshots"
            download_dir.mkdir()
            initialize_database(database_path)
            file_path = download_dir / "Feature [feature-id].mp4"
            file_path.write_bytes(b"feature")
            media_id = save_media(
                database_path,
                "https://youtu.be/feature-id",
                {
                    "id": "feature-id",
                    "extractor_key": "Youtube",
                    "title": "Searchable feature video",
                    "channel": "Feature channel",
                    "channel_id": "feature-channel",
                },
                file_path,
            )
            with (
                patch("app.DATABASE_PATH", database_path),
                patch("app.DOWNLOAD_DIR", download_dir),
                patch("app.SNAPSHOT_DIR", snapshot_dir),
            ):
                client = APP.test_client()
                self.assertEqual(
                    client.post(
                        "/subscriptions",
                        data={"url": "https://www.youtube.com/@feature", "name": "Feature"},
                    ).status_code,
                    303,
                )
                self.assertEqual(
                    client.post(
                        "/playlists",
                        data={"name": "Watch later", "description": "Test"},
                    ).status_code,
                    303,
                )
                playlist_id = list_playlists(database_path)[0]["id"]
                self.assertEqual(
                    client.post(f"/playlists/{playlist_id}/videos/{media_id}").status_code,
                    303,
                )
                self.assertEqual(
                    client.post(
                        f"/videos/{media_id}/tags", data={"tags": "search-tag"}
                    ).status_code,
                    303,
                )
                self.assertEqual(
                    client.post(
                        f"/videos/{media_id}/favorite",
                        data={"favorite": "1", "next": "/"},
                    ).status_code,
                    303,
                )
                for path, expected in (
                    ("/search?q=search-tag", b"Searchable feature video"),
                    ("/playlists", b"Watch later"),
                    (f"/playlists/{playlist_id}", b"Searchable feature video"),
                    ("/stats", b"Archive statistics"),
                    ("/settings", b"SponsorBlock"),
                ):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(expected, response.data)
                    self.assertNotIn(b"setInterval", response.data)
                self.assertEqual(client.post("/settings/snapshots").status_code, 303)
                self.assertEqual(len(list(snapshot_dir.glob("*.db"))), 1)
                exported = client.get("/settings/export.json")
                self.assertEqual(exported.status_code, 200)
                self.assertIn(b"Searchable feature video", exported.data)
                imported = client.post(
                    "/settings/subscriptions.opml",
                    data={
                        "opml": (
                            io.BytesIO(
                                b'<opml version="2.0"><body><outline text="Imported" type="channel" xmlUrl="https://www.youtube.com/@imported"/></body></opml>'
                            ),
                            "subscriptions.opml",
                        )
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(imported.status_code, 303)
                self.assertEqual(len(list_subscriptions(database_path)), 2)

    def test_queue_cancel_ignore_and_delete_ignore_routes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "library.db"
            download_dir = root / "downloads"
            download_dir.mkdir()
            initialize_database(database_path)
            with (
                patch("app.DATABASE_PATH", database_path),
                patch("app.DOWNLOAD_DIR", download_dir),
            ):
                client = APP.test_client()
                queued = enqueue_url("https://youtu.be/queue-me")
                downloads_response = client.get("/downloads")
                self.assertIn(b"Cancel", downloads_response.data)
                self.assertIn(b"Ignore", downloads_response.data)
                ignored_response = client.post(f"/downloads/{queued['id']}/ignore")
                self.assertEqual(ignored_response.status_code, 303)
                with self.assertRaisesRegex(ValueError, "is ignored"):
                    enqueue_url("https://www.youtube.com/watch?v=queue-me")
                settings_response = client.get("/settings")
                self.assertIn(b"Allow again", settings_response.data)

                ignored_id = list_ignored(database_path)[0]["id"]
                self.assertEqual(
                    client.post(f"/settings/ignored/{ignored_id}/remove").status_code,
                    303,
                )
                self.assertFalse(
                    is_ignored(database_path, "https://youtu.be/queue-me", "Youtube", "queue-me")
                )

                file_path = download_dir / "delete.mp4"
                file_path.write_bytes(b"video")
                sidecar_path = download_dir / "delete.info.json"
                sidecar_path.write_text("{}", encoding="utf-8")
                media_id = save_media(
                    database_path,
                    "https://youtu.be/delete-route",
                    {
                        "id": "delete-route",
                        "extractor_key": "Youtube",
                        "title": "Delete route",
                    },
                    file_path,
                )
                deleted_response = client.post(
                    f"/videos/{media_id}/delete-ignore",
                    data={"next": "/"},
                )
                self.assertEqual(deleted_response.status_code, 303)
                self.assertFalse(file_path.exists())
                self.assertFalse(sidecar_path.exists())
                self.assertIsNone(get_media(database_path, media_id))

    def test_video_and_channel_can_be_deleted_without_being_ignored(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "library.db"
            download_dir = root / "downloads"
            cache_dir = root / "cache"
            download_dir.mkdir()
            cache_dir.mkdir()
            initialize_database(database_path)
            with (
                patch("app.DATABASE_PATH", database_path),
                patch("app.DOWNLOAD_DIR", download_dir),
                patch("app.CACHE_DIR", cache_dir),
            ):
                client = APP.test_client()
                file_path = download_dir / "delete-only.mp4"
                file_path.write_bytes(b"video")
                media_id = save_media(
                    database_path,
                    "https://youtu.be/delete-only",
                    {
                        "id": "delete-only",
                        "extractor_key": "Youtube",
                        "title": "Delete only route",
                    },
                    file_path,
                )
                watch_response = client.get(f"/watch/{media_id}")
                self.assertIn(b"Delete, don&#39;t ignore", watch_response.data)
                deleted_response = client.post(
                    f"/videos/{media_id}/delete",
                    data={"next": "/"},
                )
                self.assertEqual(deleted_response.status_code, 303)
                self.assertFalse(file_path.exists())
                self.assertIsNone(get_media(database_path, media_id))
                self.assertFalse(
                    is_ignored(
                        database_path,
                        "https://youtu.be/delete-only",
                        "Youtube",
                        "delete-only",
                    )
                )

                channel_files = []
                for video_id in ("channel-one", "channel-two"):
                    channel_file = download_dir / f"{video_id}.mp4"
                    channel_file.write_bytes(b"video")
                    channel_files.append(channel_file)
                    save_media(
                        database_path,
                        f"https://youtu.be/{video_id}",
                        {
                            "id": video_id,
                            "extractor_key": "Youtube",
                            "title": video_id,
                            "channel_id": "UC-delete-only",
                            "channel": "Delete Only Channel",
                        },
                        channel_file,
                    )
                channel = list_channels(database_path)[0]
                channel_response = client.get(f"/channels/{channel['id']}")
                self.assertIn(b"Delete, don&#39;t ignore", channel_response.data)
                deleted_channel_response = client.post(
                    f"/channels/{channel['id']}/delete"
                )
                self.assertEqual(deleted_channel_response.status_code, 303)
                self.assertFalse(any(path.exists() for path in channel_files))
                self.assertEqual(list_channels(database_path), [])
                for video_id in ("channel-one", "channel-two"):
                    self.assertFalse(
                        is_ignored(
                            database_path,
                            f"https://youtu.be/{video_id}",
                            "Youtube",
                            video_id,
                        )
                    )

    def test_pages_are_server_rendered_without_polling(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "library.db"
            initialize_database(database_path)
            enqueue_download(database_path, "https://example.com/queued")
            with patch("app.DATABASE_PATH", database_path):
                client = APP.test_client()
                for path, expected in (
                    ("/", b"Recently downloaded"),
                    ("/channels", b"Channels"),
                    ("/downloads", b"https://example.com/queued"),
                    ("/settings", b"Video quality"),
                ):
                    with self.subTest(path=path):
                        response = client.get(path)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(expected, response.data)
                        self.assertNotIn(b"setInterval", response.data)

    def test_settings_form_persists_valid_values_and_rejects_invalid_limit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "library.db"
            initialize_database(database_path)
            with patch("app.DATABASE_PATH", database_path):
                client = APP.test_client()
                response = client.post(
                    "/settings",
                    data={
                        "collection_limit": "75",
                        "download_quality": "1440",
                        "subscription_download_delay": "300",
                    },
                )
                self.assertEqual(response.status_code, 303)
                saved = get_settings(database_path)
                self.assertEqual(saved["collection_limit"], 75)
                self.assertEqual(saved["download_quality"], "1440")
                self.assertEqual(saved["subscription_download_delay"], 300)
                invalid = client.post(
                    "/settings",
                    data={"collection_limit": "10001", "download_quality": "1080"},
                )
                self.assertEqual(invalid.status_code, 400)
                self.assertIn(b"between 1 and 10,000", invalid.data)
                invalid_delay = client.post(
                    "/settings",
                    data={
                        "collection_limit": "100",
                        "download_quality": "1080",
                        "subscription_download_delay": "86401",
                    },
                )
                self.assertEqual(invalid_delay.status_code, 400)
                self.assertIn(b"between 0 and 86,400 seconds", invalid_delay.data)

    def test_channel_page_filters_videos_and_progress_api_persists(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "library.db"
            download_dir = root / "downloads"
            download_dir.mkdir()
            initialize_database(database_path)
            first_file = download_dir / "first.mp4"
            second_file = download_dir / "second.mp4"
            first_file.write_bytes(b"first")
            second_file.write_bytes(b"second")
            first_id = save_media(
                database_path,
                "https://example.com/first",
                {
                    "id": "first",
                    "title": "First channel video",
                    "extractor_key": "Youtube",
                    "channel": "First channel",
                    "channel_id": "channel-one",
                },
                first_file,
            )
            save_media(
                database_path,
                "https://example.com/second",
                {
                    "id": "second",
                    "title": "Second channel video",
                    "extractor_key": "Youtube",
                    "channel": "Second channel",
                    "channel_id": "channel-two",
                },
                second_file,
            )
            channel_id = get_media(database_path, first_id)["channel_db_id"]

            with patch("app.DATABASE_PATH", database_path):
                client = APP.test_client()
                channels_response = client.get("/channels")
                self.assertIn(f'href="/channels/{channel_id}"'.encode(), channels_response.data)
                channel_response = client.get(f"/channels/{channel_id}")
                self.assertIn(b"First channel video", channel_response.data)
                self.assertNotIn(b"Second channel video", channel_response.data)
                watch_response = client.get(f"/watch/{first_id}")
                self.assertIn(b"timeupdate", watch_response.data)
                self.assertIn(b'crossorigin="use-credentials"', watch_response.data)
                self.assertIn(b"webkit-playsinline", watch_response.data)
                self.assertIn(
                    f'src="/api/media/{first_id}"'.encode(), watch_response.data
                )
                self.assertNotIn(b"setInterval", watch_response.data)

                partial = client.post(
                    f"/api/media/{first_id}/progress",
                    json={"current_time": 50, "duration": 100},
                )
                self.assertEqual(partial.status_code, 200)
                self.assertEqual(partial.get_json()["watch_progress"], 50)
                complete = client.post(
                    f"/api/media/{first_id}/progress",
                    json={"current_time": 95, "duration": 100},
                )
                self.assertEqual(complete.get_json()["watch_progress"], 100)
                self.assertEqual(complete.get_json()["watched"], 1)

    def test_media_route_supports_ranges_and_confines_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            download_dir = root / "downloads"
            download_dir.mkdir()
            database_path = root / "data" / "library.db"
            file_path = download_dir / "video.mp4"
            file_path.write_bytes(b"test-video")
            info = {
                "id": "video-id",
                "extractor_key": "Youtube",
                "title": "Test video",
                "webpage_url": "https://youtu.be/video-id",
                "ext": "mp4",
            }
            initialize_database(database_path)
            media_id = save_media(database_path, info["webpage_url"], info, file_path)

            with (
                patch("app.DATABASE_PATH", database_path),
                patch("app.DOWNLOAD_DIR", download_dir),
            ):
                client = APP.test_client()
                response = client.get(
                    f"/api/media/{media_id}",
                    headers={
                        "Range": "bytes=2-5",
                        "Origin": "https://localhost",
                    },
                )
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.data, b"st-v")
                self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
                self.assertEqual(response.headers["Accept-Ranges"], "bytes")
                self.assertEqual(
                    response.headers["Access-Control-Allow-Origin"],
                    "https://localhost",
                )
                self.assertEqual(
                    response.headers["Access-Control-Allow-Credentials"], "true"
                )
                self.assertIn("no-transform", response.headers["Cache-Control"])
                response.close()
                self.assertEqual(client.get("/api/media/999").status_code, 404)


class StorageLayoutTests(unittest.TestCase):
    def test_tubearchivist_paths_and_cached_jpegs(self):
        self.assertEqual(
            media_relative_path("UC-channel", "video_ID"),
            Path("UC-channel/video_ID.mp4"),
        )
        self.assertEqual(
            video_thumb_relative_path("Abc123"), Path("videos/a/Abc123.jpg")
        )
        self.assertEqual(
            channel_art_relative_path("UC-channel", "thumb"),
            Path("channels/UC-channel_thumb.jpg"),
        )
        self.assertEqual(
            playlist_thumb_relative_path("PL-list"),
            Path("playlists/PL-list.jpg"),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_dir = root / "cache"
            source = root / "source.png"
            Image.new("RGB", (400, 400), "red").save(source)
            video = cache_video_thumbnail(cache_dir, "Abc123", source)
            playlist = cache_playlist_thumbnail(cache_dir, "PL-list", source)
            channel = cache_channel_art(
                cache_dir, "UC-channel", source, source, source
            )
            with Image.open(video) as image:
                self.assertEqual(image.format, "JPEG")
                # Match TubeArchivist's float crop coordinates exactly.
                self.assertEqual(image.size, (400, 224))
            with Image.open(playlist) as image:
                self.assertEqual(image.size, (336, 189))
            self.assertTrue(channel["thumb"].is_file())
            self.assertTrue(channel["banner"].is_file())
            self.assertTrue(channel["tvart"].is_file())

    def test_legacy_media_migration_moves_media_and_sidecars(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "data" / "library.db"
            media_dir = root / "youtube"
            cache_dir = root / "cache"
            media_dir.mkdir()
            old_media = media_dir / "Legacy title [legacy-id].mp4"
            old_subtitle = media_dir / "Legacy title [legacy-id].en.vtt"
            old_media.write_bytes(b"media")
            old_subtitle.write_text("WEBVTT", encoding="utf-8")
            initialize_database(database_path)
            media_id = save_media(
                database_path,
                "https://www.youtube.com/watch?v=legacy-id",
                {
                    "id": "legacy-id",
                    "extractor_key": "Youtube",
                    "title": "Legacy title",
                    "channel": "Legacy channel",
                    "channel_id": "UC-legacy",
                },
                old_media,
            )
            result = migrate_storage_layout(database_path, media_dir, cache_dir)
            target = media_dir / "UC-legacy" / "legacy-id.mp4"
            self.assertEqual(result, {"moved": 1, "missing": 0})
            self.assertTrue(target.is_file())
            self.assertTrue((target.parent / "legacy-id.en.vtt").is_file())
            self.assertFalse(old_media.exists())
            self.assertEqual(get_media(database_path, media_id)["file_path"], str(target.resolve()))
            self.assertEqual(
                migrate_storage_layout(database_path, media_dir, cache_dir),
                {"moved": 0, "missing": 0},
            )

    def test_cache_route_serves_only_cached_artwork(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_dir = Path(temporary_directory)
            source = cache_dir / "source.png"
            Image.new("RGB", (320, 180), "blue").save(source)
            target = cache_video_thumbnail(cache_dir, "route-id", source)
            with patch("app.CACHE_DIR", cache_dir):
                client = APP.test_client()
                response = client.get("/cache/videos/r/route-id.jpg")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "image/jpeg")
                response.close()
                self.assertEqual(client.get("/cache/../source.png").status_code, 404)
            self.assertTrue(target.is_file())


class TubeArchivistApiCompatibilityTests(unittest.TestCase):
    """Exercise the public TubeArchivist v0.5.10 API contract end to end."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "data" / "library.db"
        self.download_dir = self.root / "downloads"
        self.snapshot_dir = self.root / "snapshots"
        self.cache_dir = self.root / "cache"
        self.download_dir.mkdir()
        initialize_database(self.database_path)
        initialize_features(self.database_path)
        update_settings(
            self.database_path,
            {"cookie_path": str(self.root / "cookies.txt"), "page_size": 24},
        )
        (self.download_dir / "api-channel").mkdir()
        self.video_path = self.download_dir / "api-channel" / "api-video.mp4"
        self.video_path.write_bytes(b"api-video-data")
        self.media_id = save_media(
            self.database_path,
            "https://www.youtube.com/watch?v=api-video",
            {
                "id": "api-video",
                "extractor_key": "Youtube",
                "title": "API contract video",
                "channel": "API channel",
                "channel_id": "api-channel",
                "channel_url": "https://www.youtube.com/channel/api-channel",
                "channel_thumbnail": "https://example.com/channel.jpg",
                "duration": 100,
                "upload_date": "20260801",
                "description": "A searchable API description",
                "thumbnail": "https://example.com/video.jpg",
                "webpage_url": "https://www.youtube.com/watch?v=api-video",
                "ext": "mp4",
                "view_count": 10,
                "like_count": 2,
                "categories": ["Education"],
                "comments": [],
            },
            self.video_path,
        )
        set_media_tags(self.database_path, self.media_id, "contract, api")
        self.playlist = create_playlist(self.database_path, "API playlist")
        add_playlist_media(self.database_path, self.playlist["id"], self.media_id)
        artwork_source = self.root / "artwork.png"
        Image.new("RGB", (320, 180), "green").save(artwork_source)
        cache_video_thumbnail(self.cache_dir, "api-video", artwork_source)
        cache_channel_art(
            self.cache_dir,
            "api-channel",
            artwork_source,
            artwork_source,
            artwork_source,
        )
        cache_playlist_thumbnail(
            self.cache_dir, self.playlist["playlist_id"], artwork_source
        )
        enqueue_download(
            self.database_path,
            "https://www.youtube.com/watch?v=queued-video",
            kind="video",
        )
        add_ignored(
            self.database_path,
            "https://www.youtube.com/watch?v=ignored-video",
            "Youtube",
            "ignored-video",
            "Ignored video",
        )
        self.patchers = [
            patch("app.DATABASE_PATH", self.database_path),
            patch("app.DOWNLOAD_DIR", self.download_dir),
            patch("app.SNAPSHOT_DIR", self.snapshot_dir),
            patch("app.CACHE_DIR", self.cache_dir),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.client = APP.test_client()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def assert_success(self, response):
        self.assertLess(response.status_code, 400, response.get_data(as_text=True))

    def test_route_and_method_inventory_matches_v0510(self):
        expected = {
            "/api/ping/": {"GET"},
            "/api/refresh/": {"GET", "POST"},
            "/api/watched/": {"POST"},
            "/api/search/": {"GET"},
            "/api/notification/": {"GET"},
            "/api/health/": {"GET"},
            "/api/video/": {"GET"},
            "/api/video/<video_id>/": {"GET", "DELETE"},
            "/api/video/<video_id>/nav/": {"GET"},
            "/api/video/<video_id>/progress/": {"POST", "DELETE"},
            "/api/video/<video_id>/comment/": {"GET"},
            "/api/video/<video_id>/similar/": {"GET"},
            "/api/channel/": {"GET", "POST"},
            "/api/channel/search/": {"GET"},
            "/api/channel/<channel_id>/": {"GET", "POST", "DELETE"},
            "/api/channel/<channel_id>/aggs/": {"GET"},
            "/api/channel/<channel_id>/nav/": {"GET"},
            "/api/playlist/": {"GET", "POST"},
            "/api/playlist/custom/": {"POST"},
            "/api/playlist/custom/<playlist_id>/": {"POST"},
            "/api/playlist/<playlist_id>/": {"GET", "POST", "DELETE"},
            "/api/download/": {"GET", "POST", "PATCH", "DELETE"},
            "/api/download/aggs/": {"GET"},
            "/api/download/<video_id>/": {"GET", "POST", "DELETE"},
            "/api/appsettings/config/": {"GET", "POST"},
            "/api/appsettings/snapshot/": {"GET", "POST"},
            "/api/appsettings/snapshot/<snapshot_id>/": {"GET", "POST", "DELETE"},
            "/api/appsettings/backup/": {"GET", "POST"},
            "/api/appsettings/backup/<path:filename>/": {"GET", "POST", "DELETE"},
            "/api/appsettings/cookie/": {"GET", "POST", "PUT", "DELETE"},
            "/api/appsettings/token/": {"GET", "DELETE"},
            "/api/appsettings/rescan-filesystem/": {"POST"},
            "/api/appsettings/manual-import/": {"POST"},
            "/api/appsettings/membership/profile/": {"GET"},
            "/api/appsettings/membership/sync/": {"POST"},
            "/api/appsettings/membership/token/": {"GET", "POST", "DELETE"},
            "/api/stats/video/": {"GET"},
            "/api/stats/channel/": {"GET"},
            "/api/stats/playlist/": {"GET"},
            "/api/stats/download/": {"GET"},
            "/api/stats/watch/": {"GET"},
            "/api/stats/downloadhist/": {"GET"},
            "/api/stats/biggestchannels/": {"GET"},
            "/api/task/by-name/": {"GET"},
            "/api/task/by-name/<task_name>/": {"GET", "POST"},
            "/api/task/by-id/<task_id>/": {"GET", "POST"},
            "/api/task/schedule/": {"GET"},
            "/api/task/schedule/<task_name>/": {"GET", "POST", "DELETE"},
            "/api/task/notification/": {"GET", "POST", "DELETE"},
            "/api/task/notification/test/": {"POST"},
            "/api/user/login/": {"POST"},
            "/api/user/logout/": {"POST"},
            "/api/user/account/": {"GET"},
            "/api/user/me/": {"GET", "POST"},
            "/api/schema/": {"GET"},
            "/api/docs/": {"GET"},
        }
        actual = {
            rule.rule: rule.methods - {"HEAD", "OPTIONS"}
            for rule in APP.url_map.iter_rules()
            if rule.rule.startswith("/api/")
            and rule.rule not in {
                "/api/health",
                "/api/jobs",
                "/api/library",
                "/api/downloads",
                "/api/media/<int:media_id>",
                "/api/media/<int:media_id>/progress",
            }
        }
        self.assertEqual(actual, expected)

    def test_archive_read_contracts(self):
        ping = self.client.get("/api/ping/", headers={"Authorization": "Token anything"})
        self.assertEqual(ping.get_json()["version"], "v0.5.10")
        self.assertEqual(self.client.get("/api/health/").get_json(), "OK")

        videos = self.client.get("/api/video/?type=videos&watch=unwatched")
        self.assert_success(videos)
        video_payload = videos.get_json()
        self.assertEqual(set(video_payload), {"data", "paginate"})
        self.assertEqual(video_payload["data"][0]["youtube_id"], "api-video")
        self.assertEqual(
            video_payload["data"][0]["media_url"],
            "/youtube/api-channel/api-video.mp4",
        )
        self.assertEqual(
            video_payload["data"][0]["vid_thumb_url"],
            "/cache/videos/a/api-video.jpg",
        )
        self.assertTrue(
            {"active", "channel", "media_url", "player", "stats", "streams", "tags"}
            <= set(video_payload["data"][0])
        )
        for path in (
            "/api/video/api-video/",
            "/api/video/api-video/nav/",
            "/api/video/api-video/comment/",
            "/api/video/api-video/similar/",
            "/api/channel/",
            "/api/channel/search/?q=api-channel",
            "/api/channel/api-channel/",
            "/api/channel/api-channel/aggs/",
            "/api/channel/api-channel/nav/",
            "/api/playlist/",
            f"/api/playlist/{self.playlist['playlist_id']}/",
            "/api/download/",
            "/api/download/queued-video/",
            "/api/download/aggs/?filter=pending",
            "/api/search/?query=contract",
            "/api/search/?query=video:API",
            "/api/notification/",
            "/api/refresh/",
        ):
            with self.subTest(path=path):
                self.assert_success(self.client.get(path))

        media = self.client.get(
            f"/youtube/api-channel/{self.video_path.name}",
            headers={"Range": "bytes=0-2"},
        )
        self.assertEqual(media.status_code, 206)
        self.assertEqual(media.data, b"api")
        media.close()

    def test_settings_stats_user_and_task_contracts(self):
        read_paths = [
            "/api/appsettings/config/",
            "/api/appsettings/cookie/",
            "/api/appsettings/token/",
            "/api/appsettings/snapshot/",
            "/api/appsettings/backup/",
            "/api/stats/video/",
            "/api/stats/channel/",
            "/api/stats/playlist/",
            "/api/stats/download/",
            "/api/stats/watch/",
            "/api/stats/downloadhist/",
            "/api/stats/biggestchannels/?order=media_size",
            "/api/user/account/",
            "/api/user/me/",
            "/api/task/by-name/",
            "/api/task/schedule/",
            "/api/task/notification/",
            "/api/appsettings/membership/token/",
            "/api/schema/",
            "/api/docs/",
        ]
        for path in read_paths:
            with self.subTest(path=path):
                self.assert_success(self.client.get(path))

        schema = self.client.get("/api/schema/").get_json()
        self.assertIn("/api/video/{video_id}/", schema["paths"])
        self.assertNotIn("/api/video/<video_id>/", schema["paths"])
        self.assertNotIn("/api/downloads", schema["paths"])
        self.assertNotIn("/media/{channel_id}/{filename}", schema["paths"])

        config = self.client.post(
            "/api/appsettings/config/",
            json={
                "subscriptions": {"channel_size": 50},
                "downloads": {"add_metadata": False, "sleep_interval": 15},
            },
        )
        self.assertEqual(config.get_json()["subscriptions"]["channel_size"], 50)
        self.assertEqual(config.get_json()["downloads"]["sleep_interval"], 15)
        self.assertEqual(get_settings(self.database_path)["collection_limit"], 50)
        self.assertEqual(
            get_settings(self.database_path)["subscription_download_delay"], 15
        )
        invalid_sleep = self.client.post(
            "/api/appsettings/config/",
            json={"downloads": {"sleep_interval": -1}},
        )
        self.assertEqual(invalid_sleep.status_code, 400)
        self.assertEqual(
            get_settings(self.database_path)["subscription_download_delay"], 15
        )
        self.assert_success(
            self.client.put(
                "/api/appsettings/cookie/",
                json={"cookie": "# Netscape HTTP Cookie File\n"},
            )
        )
        self.assertEqual(self.client.delete("/api/appsettings/cookie/").status_code, 204)

        snapshot = self.client.post("/api/appsettings/snapshot/").get_json()["snapshot_name"]
        self.assert_success(self.client.get(f"/api/appsettings/snapshot/{snapshot}/"))
        self.assert_success(self.client.post(f"/api/appsettings/snapshot/{snapshot}/"))
        backup = self.client.post("/api/appsettings/backup/")
        self.assert_success(backup)
        backup_name = self.client.get("/api/appsettings/backup/").get_json()[0]["filename"]
        self.assert_success(self.client.get(f"/api/appsettings/backup/{backup_name}/"))
        self.assert_success(self.client.post(f"/api/appsettings/backup/{backup_name}/"))

        flags = {"ignore_error": True, "prefer_local": True}
        self.assert_success(self.client.post("/api/appsettings/rescan-filesystem/", json=flags))
        self.assert_success(self.client.post("/api/appsettings/manual-import/", json=flags))
        self.assert_success(self.client.post("/api/user/login/", json={"username": "x", "password": "y"}))
        self.assert_success(self.client.post("/api/user/logout/"))
        self.assert_success(self.client.post("/api/user/me/", json={"grid_items": 5}))
        self.assert_success(self.client.post("/api/appsettings/membership/token/", json={"token": "member"}))
        self.assertEqual(self.client.get("/api/appsettings/membership/profile/").status_code, 400)
        self.assert_success(self.client.post("/api/appsettings/membership/sync/"))

        task = self.client.post("/api/task/by-name/download_pending/", json={}).get_json()
        task_id = task["task_id"]
        self.assert_success(self.client.get("/api/task/by-name/download_pending/"))
        self.assert_success(self.client.get(f"/api/task/by-id/{task_id}/"))
        self.assertEqual(
            self.client.post(f"/api/task/by-id/{task_id}/", json={"command": "stop"}).status_code,
            204,
        )
        self.assert_success(
            self.client.post(
                "/api/task/schedule/run_backup/",
                json={"schedule": "0 0 * * *", "config": {}},
            )
        )
        self.assert_success(self.client.get("/api/task/schedule/run_backup/"))
        self.assert_success(
            self.client.post(
                "/api/task/notification/",
                json={"task_name": "run_backup", "url": "json://example"},
            )
        )
        self.assert_success(
            self.client.post(
                "/api/task/notification/test/",
                json={"task_name": "run_backup", "url": "json://example"},
            )
        )

    def test_mutating_archive_and_queue_contracts(self):
        progress = self.client.post("/api/video/api-video/progress/", json={"position": 50})
        self.assertEqual(progress.get_json()["progress"], 50)
        self.assertEqual(self.client.delete("/api/video/api-video/progress/").status_code, 204)
        self.assert_success(self.client.post("/api/watched/", json={"id": "api-video", "is_watched": True}))
        self.assert_success(
            self.client.post(
                "/api/channel/api-channel/", json={"channel_subscribed": True}
            )
        )
        self.assert_success(
            self.client.post(
                "/api/channel/",
                json={"data": [{"channel_id": "api-channel", "channel_subscribed": False}]},
            )
        )
        custom = self.client.post("/api/playlist/custom/", json={"playlist_name": "Second"})
        custom_id = custom.get_json()["playlist_id"]
        self.assert_success(
            self.client.post(
                f"/api/playlist/custom/{custom_id}/",
                json={"action": "create", "video_id": "api-video"},
            )
        )
        self.assertEqual(self.client.delete(f"/api/playlist/{custom_id}/").status_code, 204)
        self.assert_success(
            self.client.post(
                "/api/playlist/",
                json={"data": [{"playlist_id": "PL-contract", "playlist_subscribed": True}]},
            )
        )
        self.assert_success(
            self.client.post(
                "/api/download/",
                json={"data": [{"youtube_id": "new-video", "status": "pending"}]},
            )
        )
        self.assert_success(
            self.client.post("/api/download/new-video/", json={"status": "priority"})
        )
        self.assertEqual(self.client.delete("/api/download/new-video/").status_code, 204)
        self.assertEqual(
            self.client.patch("/api/download/?filter=ignore", json={"status": "pending"}).status_code,
            204,
        )

    def test_delete_and_remaining_method_contracts(self):
        regular_id = "PL-regular"
        save_external_playlist(
            self.database_path,
            f"https://www.youtube.com/playlist?list={regular_id}",
            {
                "id": regular_id,
                "extractor_key": "YoutubePlaylist",
                "title": "Regular playlist",
            },
        )
        self.assert_success(
            self.client.post(
                f"/api/playlist/{regular_id}/",
                json={"playlist_subscribed": True, "playlist_sort_order": "bottom"},
            )
        )

        self.assert_success(self.client.post("/api/appsettings/cookie/"))
        self.assertEqual(self.client.delete("/api/appsettings/token/").status_code, 204)
        self.assertEqual(
            self.client.delete("/api/appsettings/membership/token/").status_code, 204
        )

        first_snapshot = self.client.post("/api/appsettings/snapshot/").get_json()[
            "snapshot_name"
        ]
        self.assertEqual(
            self.client.delete(f"/api/appsettings/snapshot/{first_snapshot}/").status_code,
            204,
        )
        self.client.post("/api/appsettings/backup/")
        backup_name = self.client.get("/api/appsettings/backup/").get_json()[0][
            "filename"
        ]
        self.assertEqual(
            self.client.delete(f"/api/appsettings/backup/{backup_name}/").status_code,
            204,
        )

        self.assert_success(
            self.client.post(
                "/api/task/schedule/run_backup/",
                json={"schedule": "0 1 * * *", "config": {}},
            )
        )
        self.assertEqual(
            self.client.delete("/api/task/schedule/run_backup/").status_code, 204
        )
        notification = {"task_name": "run_backup", "url": "json://delete-me"}
        self.assert_success(self.client.post("/api/task/notification/", json=notification))
        self.assertEqual(
            self.client.delete("/api/task/notification/", json=notification).status_code,
            204,
        )

        self.assertEqual(
            self.client.delete("/api/download/?filter=pending").status_code, 204
        )
        self.assertEqual(
            self.client.delete(f"/api/playlist/{regular_id}/").status_code, 204
        )
        self.assertEqual(self.client.delete("/api/video/api-video/").status_code, 204)
        self.assertEqual(
            self.client.delete("/api/channel/api-channel/").status_code, 204
        )


if __name__ == "__main__":
    unittest.main()
