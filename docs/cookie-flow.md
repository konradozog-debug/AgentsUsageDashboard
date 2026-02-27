# Cookie Flow

## How Firefox Stores Cookies

Firefox stores cookies in a SQLite file: `cookies.sqlite` inside the profile directory.

Path in the dashboard container (volume `firefox_data` mounted as `/firefox`):
```
/firefox/.mozilla/firefox/<profile>/cookies.sqlite
```

Table: `moz_cookies` — columns: `name`, `value`, `host`, `path`, `expiry`, `lastAccessed`, etc.

## Profile Auto-Detection

Search for the profile directory in order:
1. `*.default-release` — default profile in newer Firefox versions
2. `*.default` — fallback
3. First folder containing `cookies.sqlite`

## WAL Lock — Why We Copy

Firefox uses SQLite in WAL (Write-Ahead Logging) mode. The file is locked during writes. Direct reads from another process may:
- Return incomplete data
- Throw `database is locked`

### Safe Reading Procedure

1. **Copy all three files** to `/tmp`:
   - `cookies.sqlite`
   - `cookies.sqlite-wal`
   - `cookies.sqlite-shm`

   All three are **required**. Without `-wal` and `-shm` the data may be inconsistent or empty.

2. Use `shutil.copy2()` — preserves metadata.

3. Read from the copy:
   ```sql
   SELECT name, value FROM moz_cookies
   WHERE host LIKE '%{domain}%'
   ORDER BY lastAccessed DESC
   ```

4. Delete temp files after reading.

## Key Cookies per Service

| Service | Domain | Key Cookie | Usage |
|---------|--------|------------|-------|
| OpenAI Codex | chatgpt.com | `__Secure-next-auth.session-token` | Full cookie string → exchange for Bearer token |
| Kimi Code | kimi.com | `kimi-auth` | Directly used as Bearer JWT |
| Claude | claude.ai | `sessionKey` | Full cookie string + custom headers |

## What Happens When a Session Expires

- Backend receives an auth error (401/403) from the API
- Cache keeps the last valid data + `error` field with a message
- Dashboard shows stale data with a visual indicator
- User needs to open Firefox GUI (:5800) and log in again
- Endpoint `/api/cookies` (DEBUG=true) allows checking whether cookies are present
