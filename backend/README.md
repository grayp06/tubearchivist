# Django Setup

## Apps
The backend is split up into the following apps.

### config
Root Django App. Doesn't define any views.

- Has main `settings.py`
- Has main `urls.py` responsible for routing to other apps

### common
Functionality shared between apps.

Defines views on the root `/api/*` path. Has base views to inherit from.

- SQLite document store with search, replaces the search server (`index_store.py`, `index_query.py`, `index_schema.py`)
- SQLite key value store and queues (`state_store.py`)
- Searching
- URL parser
- Collection of helper functions

### appsettings
Responsible for functionality from the settings pages.

Defines views at `/api/appsettings/*`.

- Index setup
- Reindexing
- Snapshots
- Filesystem Scan
- Manual import

### channel
Responsible for Channel Indexing functionality.

Defines views at `/api/channel/*` path.

### download
Implements download functionality with yt-dlp.

Defines views at `/api/download/*`.

- Download videos
- Queue management
- Thumbnails
- Subscriptions

### playlist
Implements playlist functionality.

Defines views at `/api/playlist/*`.

- Index Playlists
- Manual Playlists

### stats
Builds aggregations views for the statistics dashboard.

Defines views at `/api/stats/*`.

### task
Defines and runs background tasks.

Defines views at `/api/task/*`.

- Has main `tasks.py` with all shared_task definitions
- Has the task runner and the dispatcher thread starting tasks as short lived processes
- Has the cron scheduler
- Has `CustomPeriodicTask` model
- Implements apprise notifications links
- Implements schedule functionality

### user
Implements user and auth functionality.

Defines views at `/api/config/*`.

- Defines custom `Account` model

### video
Index functionality for videos.

Defines views at `/api/video/*`.

- Index videos
- Index comments
- Index/download subtitles
- Media stream parsing
