# Architecture

## Data Flow

```
┌──────────────────────────────────────────────────────────┐
│  Docker Host                                              │
│                                                           │
│  ┌─────────────┐    shared volume     ┌────────────────┐  │
│  │   Firefox    │   firefox_data      │   Dashboard     │  │
│  │  (noVNC)     │──────────────────→  │  (Python)       │  │
│  │  :5800       │   cookies.sqlite    │  :8777          │  │
│  └─────────────┘                      └───────┬────────┘  │
│        ↑                                      │           │
│   user logs in                                │           │
│   to 4 services                               ↓           │
│                                        ┌───────────────┐  │
│                                        │  External APIs │  │
│                                        │  chatgpt.com   │  │
│                                        │  kimi.com      │  │
│                                        │  claude.ai     │  │
│                                        │  z.ai          │  │
│                                        └───────────────┘  │
└──────────────────────────────────────────────────────────┘
```

## Components

### Firefox (jlesage/firefox)
- Browser container accessible via noVNC on port 5800
- User logs in once to chatgpt.com, kimi.com, claude.ai
- Firefox keeps sessions alive (cookies in SQLite)
- Volume `firefox_data` mounted as `/config`

### Dashboard (Python/Flask/Gunicorn)
- Mounts `firefox_data` as `/firefox` (read-only)
- **Background thread** (every REFRESH_INTERVAL seconds):
  1. Copies cookies.sqlite (+WAL/SHM) to /tmp
  2. Reads cookies per domain from the SQLite copy
  3. Queries each service's API
  4. Saves results to cache (with thread lock)
  5. Cleans up temp files
- **HTTP (gunicorn, 1 worker, 2 threads)**:
  - `GET /` → dashboard.html
  - `GET /api/data` → JSON from cache
  - `GET /api/refresh` → force fetch
  - `GET /api/cookies` → debug (only when DEBUG=true)

### Dashboard HTML
- Single-page, dark theme, zero frameworks
- Polls `/api/data` with interval based on `next_refresh_at`

## Shared Storage

```
firefox_data volume:
  Firefox writes → /config/.mozilla/firefox/<profile>/cookies.sqlite
  Dashboard reads → /firefox/.mozilla/firefox/<profile>/cookies.sqlite (ro)
```
