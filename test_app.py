"""Unit and route tests for TubeArchivist Lite."""

import tempfile
import unittest
import io
from pathlib import Path
from unittest.mock import patch

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
    search_media,
    set_media_favorite,
    set_media_tags,
    subscriptions_opml,
)
from library import (
    add_ignored,
    cancel_download,
    claim_next_download,
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
    set_media_watched,
    update_media_progress,
    update_settings,
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
    def test_advanced_settings_are_mapped_to_yt_dlp(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "library.db"
            download_dir = root / "downloads"
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
            ):
                options = yt_dlp_download_options(1, {})

            self.assertIn("height<=480", options["format"])
            self.assertEqual(options["subtitleslangs"], ["en", "fr"])
            self.assertTrue(options["writeinfojson"])
            self.assertTrue(options["getcomments"])
            self.assertEqual(options["ratelimit"], 64 * 1024)
            self.assertEqual(options["cookiefile"], str(cookie_path))
            self.assertEqual(
                options["postprocessors"][0]["categories"],
                ["sponsor", "selfpromo"],
            )
            self.assertEqual(
                options["outtmpl"],
                str(download_dir / "%(title)s [%(id)s].%(ext)s"),
            )

    def test_defaults_validation_and_quality_formats(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "library.db"
            initialize_database(database_path)
            defaults = get_settings(database_path)
            self.assertEqual(defaults["collection_limit"], 100)
            self.assertEqual(defaults["download_quality"], "1080")
            self.assertEqual(defaults["page_size"], 24)
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
        self.assertIn("height<=1080", quality_format("1080"))
        self.assertEqual(quality_format("best"), "bestvideo+bestaudio/best")
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
                    data={"collection_limit": "75", "download_quality": "1440"},
                )
                self.assertEqual(response.status_code, 303)
                saved = get_settings(database_path)
                self.assertEqual(saved["collection_limit"], 75)
                self.assertEqual(saved["download_quality"], "1440")
                invalid = client.post(
                    "/settings",
                    data={"collection_limit": "10001", "download_quality": "1080"},
                )
                self.assertEqual(invalid.status_code, 400)
                self.assertIn(b"between 1 and 10,000", invalid.data)

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
                    headers={"Range": "bytes=2-5"},
                )
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.data, b"st-v")
                self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
                self.assertEqual(response.headers["Accept-Ranges"], "bytes")
                response.close()
                self.assertEqual(client.get("/api/media/999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
