# Feature compatibility matrix

This branch targets TubeArchivist's single-user archive workflows while keeping
the lightweight architecture explicit.

| Area | Lightweight implementation |
| --- | --- |
| Archive | Downloaded metadata, channels, video types, watched progress, favourites, tags |
| Discovery | Metadata/tag/subtitle search, filters, sorting, pagination |
| Channels | Normalized channel pages, subscription records, pause/remove/rescan |
| Playlists | YouTube playlist capture, custom playlists, add/remove/reorder |
| Queue | SQLite FIFO with priority, cancel, cancel-all, retry, ignore, history search |
| Downloading | Video/channel/playlist URLs, quality, limits, rate cap, cookies, subtitles, embedded metadata, comments, SponsorBlock |
| Playback | Local range streaming, resume position, event-driven progress, watched threshold |
| Maintenance | Folder reconciliation, per-video metadata refresh, SQLite snapshots |
| Portability | JSON archive export and OPML subscription import/export |
| Reporting | Archive size/runtime/counts, queue counts, largest channels |
| API | TubeArchivist v0.5.10 route/method and JSON contract, OpenAPI schema, optional token header |
| Storage | `/youtube/<channel>/<video>.mp4` media and TubeArchivist-compatible `/cache` artwork paths |

Deliberate differences from upstream: no authentication or multiple users, no
Redis/Celery/Elasticsearch, no notification delivery, no browser polling, no automatic
scheduler, and one download at a time. These are architectural constraints,
not unfinished UI placeholders.
