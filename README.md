# TubeArchivist Lite

The deliberately small Flask version. It has a recent-download home page,
normalized channels, a durable FIFO download queue, watched status, and local
video playback. SQLite stores media metadata, channel records, playback
percentage, and every queue state transition. Channel cards open filtered video
pages. Playback below 5% is stored as 0%, partial videos resume, and reaching
95% marks a video watched. There is still no authentication, search,
subscription scheduler, or playlist editing.

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
the SQLite database is stored at `./data/library.db`. Saved videos appear in
the Home page and can be played with seeking. Queue status never polls: use the
Refresh status link on the Downloads page when you want an update.

Only expose this container on localhost. This version intentionally has no
authentication.
