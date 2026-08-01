"""Unit and route tests for TubeArchivist Lite."""

import tempfile
import unittest
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
    video_identity,
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
    def test_defaults_validation_and_quality_formats(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "library.db"
            initialize_database(database_path)
            self.assertEqual(
                get_settings(database_path),
                {"collection_limit": 100, "download_quality": "1080"},
            )
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


class FlaskEndpointTests(unittest.TestCase):
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
                self.assertEqual(
                    get_settings(database_path),
                    {"collection_limit": 75, "download_quality": "1440"},
                )
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
