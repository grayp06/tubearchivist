# TubeArchivist Lite

The deliberately small Flask version. It provides the core TubeArchivist
archive workflows with one Flask process, one sleeping worker thread, SQLite,
and local files. There is no Redis, Celery, Elasticsearch, SPA runtime, or
front-end status polling.

The archive includes:

- recent videos with watched progress, favourites, tags, filters, sorting, and pagination;
- normalized channel pages plus channel and playlist subscriptions;
- manual subscription rescans that expand into the durable priority queue;
- imported YouTube playlists and editable local playlists;
- metadata, description, tag, ID, channel, and downloaded-subtitle search;
- local playback, event-driven progress saving, and 95% watched detection;
- individual/bulk cancellation, priority, retry, ignore, and delete-and-ignore;
- 480p through 4K/best quality controls, rate limiting, subtitles, info JSON,
  cookies, optional comments, and SponsorBlock removal;
- archive statistics, download-folder reconciliation, SQLite snapshots, JSON
  export, and OPML subscription import/export.

## TubeArchivist API compatibility

The `/api/` surface is a drop-in contract replacement for TubeArchivist
**v0.5.10**. It exposes the same 58 paths and HTTP methods for videos,
channels, playlists, downloads, search, watched state, settings, snapshots,
backups, statistics, users, tasks, schedules, notifications, membership,
OpenAPI schema, and interactive API documentation. List endpoints use TubeArchivist's
`data`/`paginate` envelope and resource fields retain the upstream names.

Existing clients may continue to send `Authorization: Token <token>`. The
token is available from `GET /api/appsettings/token/`, can be rotated with
`DELETE /api/appsettings/token/`, and is intentionally optional because this
edition remains a localhost-only, no-auth application. Interactive API
documentation is at <http://localhost:8088/api/docs/> and the schema is at
<http://localhost:8088/api/schema/>.

API task and schedule records are persisted in SQLite. Operations available in
this architecture execute locally; configuration-only integrations such as
membership and notification delivery retain their upstream request/response
contracts but report that no remote service is configured.

Authentication, multiple users, notification delivery, and automatic schedules are
intentionally omitted. This localhost-oriented edition keeps those out to
preserve the requested simplicity and memory profile; subscription scans and
maintenance are explicit manual actions.

The Downloads form accepts individual YouTube videos, playlists, and channels.
Playlist and channel URLs are expanded into grouped SQLite video rows; archived
or already-pending videos are skipped. Individual queued or active videos can
be cancelled. Ignoring a queue row cancels it and prevents that video from
being added again, including through later channel or playlist expansion.

Downloaded videos also have a **Delete & ignore** action. It removes the local
media file and library record but retains a small SQLite ignore entry. Ignored
videos can be allowed again from Settings.

Settings are stored in SQLite. Collection expansion defaults to 100 entries and
can be changed from 1 to 10,000. Download quality defaults to 1080p, with 480p,
720p, 1440p, 4K, and best-available presets. FFmpeg is included for merging
separate high-resolution video and audio streams.

## Run

```bash
docker compose up -d --build
```

Open <http://localhost:8088>. Downloaded files are written to `./downloads` and
the SQLite database is stored at `./data/library.db`. SQLite snapshots are
stored under `./data/snapshots`. Saved videos appear in
the Home page and can be played with seeking. Queue status never polls: use the
Refresh status link on the Downloads page when you want an update.

Only expose this container on localhost. This version intentionally has no
authentication.
