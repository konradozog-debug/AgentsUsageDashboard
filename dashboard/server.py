import os
import shutil
import sqlite3
import threading
import time
import logging
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, send_file
from curl_cffi import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "300"))
DEBUG_MODE = os.environ.get("DEBUG", "false").lower() == "true"
FIREFOX_BASE = Path("/firefox/.mozilla/firefox")
REQUEST_TIMEOUT = 15
FALLBACK_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:145.0) Gecko/20100101 Firefox/145.0"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent-stats")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Thread-safe cache
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_cache = {
    "codex": None,
    "kimi": None,
    "claude": None,
    "zai": None,
    "last_fetch": None,
    "next_refresh_at": None,
}
_fetch_lock = threading.Lock()

# Claude org_id — stable, cached after first lookup
_claude_org_id = None

# ---------------------------------------------------------------------------
# Firefox profile & cookie reader
# ---------------------------------------------------------------------------
def _find_profile():
    """Auto-detect Firefox profile directory."""
    firefox_root = Path("/firefox")
    if not firefox_root.exists():
        return None

    # jlesage/firefox stores profile at /config/profile/ → mounted as /firefox/profile/
    jlesage = firefox_root / "profile"
    if (jlesage / "cookies.sqlite").exists():
        return jlesage

    # Standard Mozilla path: /firefox/.mozilla/firefox/<profile>/
    if FIREFOX_BASE.exists():
        profiles = [p for p in FIREFOX_BASE.iterdir() if p.is_dir()]
        for pattern in ["*.default-release", "*.default"]:
            for p in profiles:
                if p.match(pattern) and (p / "cookies.sqlite").exists():
                    return p
        for p in profiles:
            if (p / "cookies.sqlite").exists():
                return p

    # Fallback: search recursively
    for db in firefox_root.rglob("cookies.sqlite"):
        return db.parent

    return None


def _get_firefox_ua():
    """Auto-detect Firefox User-Agent from profile. Needed for Cloudflare cf_clearance."""
    profile = _find_profile()
    if not profile:
        return FALLBACK_UA
    # Try compatibility.ini for version
    compat = profile / "compatibility.ini"
    if compat.exists():
        try:
            for line in compat.read_text(errors="ignore").splitlines():
                if line.startswith("LastVersion="):
                    ver = line.split("=", 1)[1].split("_")[0]
                    return f"Mozilla/5.0 (X11; Linux x86_64; rv:{ver}) Gecko/20100101 Firefox/{ver}"
        except Exception:
            pass
    # Try prefs.js for override
    prefs = profile / "prefs.js"
    if prefs.exists():
        try:
            for line in prefs.read_text(errors="ignore").splitlines():
                if "general.useragent.override" in line:
                    # user_pref("general.useragent.override", "...");
                    ua = line.split('"')[3] if line.count('"') >= 4 else None
                    if ua:
                        return ua
        except Exception:
            pass
    return FALLBACK_UA


FIREFOX_UA = None  # resolved lazily on first fetch


def _ua():
    """Get Firefox UA (lazy init)."""
    global FIREFOX_UA
    if FIREFOX_UA is None:
        FIREFOX_UA = _get_firefox_ua()
        log.info("Using User-Agent: %s", FIREFOX_UA[:60])
    return FIREFOX_UA


def _copy_sqlite(src_path, tmp_name):
    """Copy a SQLite DB + WAL + SHM to /tmp for safe reading. Returns tmp path or None."""
    if not src_path.exists():
        return None
    tmp_dir = Path(f"/tmp/{tmp_name}")
    tmp_dir.mkdir(exist_ok=True)
    tmp_db = tmp_dir / src_path.name
    try:
        shutil.copy2(src_path, tmp_db)
        for suffix in ["-wal", "-shm"]:
            wal = src_path.parent / f"{src_path.name}{suffix}"
            if wal.exists():
                shutil.copy2(wal, tmp_dir / f"{src_path.name}{suffix}")
    except (OSError, IOError) as e:
        log.warning("Failed to copy %s: %s", src_path.name, e)
        return None
    return tmp_db


def _cleanup_tmp(tmp_db):
    """Remove temp SQLite files."""
    if not tmp_db:
        return
    for f in tmp_db.parent.iterdir():
        try:
            f.unlink()
        except OSError:
            pass


def _read_cookies(domain):
    """Read cookies for a domain from Firefox cookies.sqlite."""
    profile = _find_profile()
    if not profile:
        return []
    tmp_db = _copy_sqlite(profile / "cookies.sqlite", "cookie_read")
    if not tmp_db:
        return []

    cookies = []
    try:
        conn = sqlite3.connect(str(tmp_db))
        cur = conn.execute(
            "SELECT name, value FROM moz_cookies "
            "WHERE host LIKE ? ORDER BY lastAccessed DESC",
            (f"%{domain}%",),
        )
        cookies = cur.fetchall()
        conn.close()
    except sqlite3.Error as e:
        log.warning("SQLite error for %s: %s", domain, e)
    finally:
        _cleanup_tmp(tmp_db)
    return cookies


def _read_localstorage(domain, key_prefix):
    """Read a value from Firefox localStorage.

    Firefox 79+ uses LSNG: per-origin SQLite in storage/default/<origin>/ls/data.sqlite
    Older Firefox uses webappsstore.sqlite.
    """
    profile = _find_profile()
    if not profile:
        return None

    # --- Strategy 1: LSNG (Firefox 79+) ---
    # Origin dirs use format: https+++z.ai  or  https+++www.kimi.com
    storage_default = profile / "storage" / "default"
    if storage_default.is_dir():
        for origin_dir in storage_default.iterdir():
            if not origin_dir.is_dir() or domain not in origin_dir.name:
                continue
            ls_db = origin_dir / "ls" / "data.sqlite"
            if not ls_db.exists():
                continue
            tmp_db = _copy_sqlite(ls_db, f"lsng_{domain}")
            if not tmp_db:
                continue
            try:
                conn = sqlite3.connect(str(tmp_db))
                cur = conn.execute(
                    "SELECT key, value FROM data WHERE key LIKE ? LIMIT 1",
                    (f"{key_prefix}%",),
                )
                row = cur.fetchone()
                if row:
                    val = row[1]
                    # LSNG stores values as blobs — decode if bytes
                    if isinstance(val, bytes):
                        val = val.decode("utf-8", errors="replace")
                    log.info("LSNG localStorage found: origin=%s key=%s len=%d", origin_dir.name, row[0], len(val))
                    conn.close()
                    return val
                else:
                    # Debug: show what keys exist
                    try:
                        cur2 = conn.execute("SELECT key FROM data LIMIT 10")
                        keys = [r[0] for r in cur2.fetchall()]
                        log.info("LSNG keys for %s: %s", origin_dir.name, keys)
                    except Exception:
                        pass
                conn.close()
            except sqlite3.Error as e:
                log.warning("LSNG error for %s: %s", origin_dir.name, e)
            finally:
                _cleanup_tmp(tmp_db)

    # --- Strategy 2: Legacy webappsstore.sqlite ---
    tmp_db = _copy_sqlite(profile / "webappsstore.sqlite", "ls_read")
    if not tmp_db:
        log.info("localStorage: no LSNG match and no webappsstore.sqlite for %s", domain)
        return None

    value = None
    try:
        conn = sqlite3.connect(str(tmp_db))
        try:
            cur = conn.execute(
                "SELECT key, value FROM webappsstore2 "
                "WHERE originKey LIKE ? AND key LIKE ? "
                "ORDER BY key ASC LIMIT 1",
                (f"%{domain}%", f"{key_prefix}%"),
            )
            row = cur.fetchone()
        except sqlite3.OperationalError:
            cur = conn.execute(
                "SELECT key, value FROM webappsstore2 "
                "WHERE scope LIKE ? AND key LIKE ? "
                "ORDER BY key ASC LIMIT 1",
                (f"%{domain}%", f"{key_prefix}%"),
            )
            row = cur.fetchone()
        if row:
            log.info("legacy localStorage found: domain=%s key=%s len=%d", domain, row[0], len(row[1]))
            value = row[1]
        else:
            log.info("legacy localStorage empty for %s", domain)
        conn.close()
    except sqlite3.Error as e:
        log.warning("localStorage error for %s/%s: %s", domain, key_prefix, e)
    finally:
        _cleanup_tmp(tmp_db)
    return value


def _cookie_string(domain):
    """Build full Cookie header string for a domain."""
    cookies = _read_cookies(domain)
    seen = set()
    parts = []
    for name, value in cookies:
        if name not in seen:
            seen.add(name)
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _cookie_value(domain, name):
    """Get a single cookie value by name."""
    for cname, cvalue in _read_cookies(domain):
        if cname == name:
            return cvalue
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _remaining_from_iso(iso_str):
    """Seconds remaining until an ISO-8601 reset timestamp."""
    if not iso_str or not isinstance(iso_str, str):
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError, AttributeError):
        return None


def _remaining_from_unix(ts):
    """Seconds remaining until a Unix epoch reset timestamp."""
    if not ts:
        return None
    try:
        ts = float(ts)
        return max(0, int(ts - datetime.now(timezone.utc).timestamp()))
    except (ValueError, TypeError):
        return None


def _unix_to_iso(ts):
    """Convert Unix epoch timestamp to ISO-8601 string for frontend."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


# ---------------------------------------------------------------------------
# HTTP helpers — curl_cffi with Firefox TLS fingerprint
# ---------------------------------------------------------------------------
def _get(url, **kwargs):
    """GET with Firefox TLS impersonation and error logging."""
    kwargs.setdefault("impersonate", "firefox")
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    if "headers" in kwargs:
        kwargs["headers"].setdefault("User-Agent", _ua())
    else:
        kwargs["headers"] = {"User-Agent": _ua()}
    r = requests.get(url, **kwargs)
    if r.status_code >= 400:
        log.warning("HTTP %d GET %s — %s", r.status_code, url.split("?")[0], r.text[:300])
    return r


def _post(url, **kwargs):
    """POST with Firefox TLS impersonation and error logging."""
    kwargs.setdefault("impersonate", "firefox")
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    if "headers" in kwargs:
        kwargs["headers"].setdefault("User-Agent", _ua())
    else:
        kwargs["headers"] = {"User-Agent": _ua()}
    r = requests.post(url, **kwargs)
    if r.status_code >= 400:
        log.warning("HTTP %d POST %s — %s", r.status_code, url.split("?")[0], r.text[:300])
    return r


# ---------------------------------------------------------------------------
# API fetchers
# ---------------------------------------------------------------------------
def fetch_codex():
    cookie_str = _cookie_string("chatgpt.com")
    if not cookie_str:
        return {"status": "offline", "error": "No cookies for chatgpt.com"}

    # Step 1 — cookies → Bearer token
    r = _get(
        "https://chatgpt.com/api/auth/session",
        headers={"Cookie": cookie_str},
    )
    r.raise_for_status()
    token = r.json().get("accessToken")
    if not token:
        return {"status": "error", "error": "No accessToken in session response"}

    auth = {"Authorization": f"Bearer {token}"}

    # Step 2 — usage
    r = _get(
        "https://chatgpt.com/backend-api/wham/usage",
        headers=auth,
    )
    r.raise_for_status()
    usage = r.json()
    log.info("codex raw keys: %s", list(usage.keys()) if isinstance(usage, dict) else type(usage).__name__)

    # Parse rate_limit structure (field names from CodexBar/OpenAI API)
    rl = usage.get("rate_limit") or {}
    log.info("codex rate_limit: %s", str(rl)[:500])
    pw = rl.get("primary_window") or {}
    sw = rl.get("secondary_window") or {}

    # Field name varies: "used_percent" (CodexBar) or "usage_percent" (older API)
    session_pct = pw.get("used_percent") or pw.get("usage_percent", 0)
    weekly_pct = sw.get("used_percent") or sw.get("usage_percent", 0)

    # reset_at is a Unix epoch timestamp (integer), convert for frontend
    pw_reset_ts = pw.get("reset_at")
    sw_reset_ts = sw.get("reset_at")

    result = {
        "status": "ok",
        "plan": usage.get("plan_type", usage.get("plan", "unknown")),
        # "email" field intentionally omitted — PII
        "limit_reached": rl.get("limit_reached", False),
        "session": {
            "usage_pct": session_pct,
            "reset_at": _unix_to_iso(pw_reset_ts),
            "remaining_seconds": _remaining_from_unix(pw_reset_ts) or 0,
        },
        "weekly": {
            "usage_pct": weekly_pct,
            "reset_at": _unix_to_iso(sw_reset_ts),
            "remaining_seconds": _remaining_from_unix(sw_reset_ts) or 0,
        },
        "credits": usage.get("credits"),
        "error": None,
    }

    # Step 3 — daily breakdown (best-effort)
    try:
        r = _get(
            "https://chatgpt.com/backend-api/wham/usage/daily-token-usage-breakdown",
            headers=auth,
        )
        r.raise_for_status()
        result["daily_breakdown"] = r.json().get("data", [])
    except Exception:
        result["daily_breakdown"] = []

    return result


def fetch_kimi():
    token = _cookie_value("kimi.com", "kimi-auth")
    if not token:
        return {"status": "offline", "error": "No kimi-auth cookie found"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Cookie": f"kimi-auth={token}",
        "Content-Type": "application/json",
        "Origin": "https://www.kimi.com",
        "Referer": "https://www.kimi.com/code/console",
        "Accept": "*/*",
        "connect-protocol-version": "1",
        "x-msh-platform": "web",
        "x-language": "en-US",
    }

    # Connect protocol: scope is a repeated field → must be an array
    url = "https://www.kimi.com/apiv2/kimi.gateway.billing.v1.BillingService/GetUsages"
    r = _post(url, headers=headers, json={"scope": ["FEATURE_CODING"]})
    if r.status_code != 200:
        log.warning("kimi GetUsages %d — %s", r.status_code, r.text[:300])
        return {"status": "error", "error": f"GetUsages HTTP {r.status_code}"}

    resp = r.json()
    log.info("kimi usages response keys: %s", list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__)
    usages = resp.get("usages", []) if isinstance(resp, dict) else []
    if not usages:
        log.info("kimi raw response: %s", str(resp)[:500])
        return {"status": "error", "error": "Empty usages response"}

    u = usages[0]
    det = u.get("detail", {})
    limit_val = int(det.get("limit", 1))
    used_val = int(det.get("used", 0))
    weekly_pct = round((used_val / limit_val) * 100, 1) if limit_val else 0

    # Rate limit (5-min window)
    rate = {"usage_pct": 0, "reset_at": None, "remaining_seconds": 0}
    limits = u.get("limits", [])
    if limits:
        rd = limits[0].get("detail", {})
        rl_limit = int(rd.get("limit", 1))
        rl_rem = int(rd.get("remaining", rl_limit))
        rl_used = rl_limit - rl_rem
        rate = {
            "usage_pct": round((rl_used / rl_limit) * 100, 1) if rl_limit else 0,
            "reset_at": rd.get("resetTime"),
            "remaining_seconds": _remaining_from_iso(rd.get("resetTime")) or 300,
        }

    result = {
        "status": "ok",
        "plan": "unknown",
        "session": rate,
        "weekly": {
            "usage_pct": weekly_pct,
            "used": used_val,
            "limit": limit_val,
            "remaining": int(det.get("remaining", 0)),
            "reset_at": det.get("resetTime"),
            "remaining_seconds": _remaining_from_iso(det.get("resetTime")),
        },
        "error": None,
    }

    # Subscription — correct path: kimi.gateway.order.v1
    try:
        r2 = _post(
            "https://www.kimi.com/apiv2/kimi.gateway.order.v1.SubscriptionService/GetSubscription",
            headers=headers,
            json={},
        )
        r2.raise_for_status()
        sub = r2.json()
        # Plan name is in subscription.goods.title (e.g. "Allegretto")
        goods = sub.get("subscription", {}).get("goods", {})
        if isinstance(goods, dict) and goods.get("title"):
            result["plan"] = goods["title"]
            log.info("kimi plan: %s", goods["title"])
    except Exception as e:
        log.warning("kimi GetSubscription failed: %s", e)

    return result


def fetch_claude():
    global _claude_org_id

    cookie_str = _cookie_string("claude.ai")
    if not cookie_str:
        return {"status": "offline", "error": "No cookies for claude.ai"}

    headers = {
        "Cookie": cookie_str,
        "anthropic-client-platform": "web_claude.ai",
        "anthropic-client-version": "1.0.0",
    }

    # Step 1 — org_id (cached). Prefer org with "chat" capability (personal account)
    if not _claude_org_id:
        r = _get("https://claude.ai/api/organizations", headers=headers)
        r.raise_for_status()
        orgs = r.json()
        log.info("claude orgs type=%s len=%s", type(orgs).__name__, len(orgs) if isinstance(orgs, list) else "?")
        if isinstance(orgs, list) and orgs:
            # Prefer org with "chat" capability (personal usage, not API-only)
            for org in orgs:
                if not isinstance(org, dict):
                    continue
                caps = org.get("capabilities", [])
                if "chat" in caps:
                    _claude_org_id = org.get("uuid")
                    log.info("claude selected org %s (has 'chat' cap)", _claude_org_id)
                    break
            if not _claude_org_id:
                first = orgs[0] if isinstance(orgs[0], dict) else {}
                _claude_org_id = first.get("uuid")
                log.info("claude fallback to first org %s", _claude_org_id)
        elif isinstance(orgs, dict):
            _claude_org_id = orgs.get("uuid") or (orgs.get("data", [{}])[0].get("uuid") if orgs.get("data") else None)
        if not _claude_org_id:
            log.warning("claude orgs response: %s", str(orgs)[:300])
            return {"status": "error", "error": "No organizations found"}

    # Step 2 — usage
    r = _get(
        f"https://claude.ai/api/organizations/{_claude_org_id}/usage",
        headers=headers,
    )
    r.raise_for_status()
    usage = r.json()
    if not isinstance(usage, dict):
        log.warning("claude usage unexpected type: %s — %s", type(usage).__name__, str(usage)[:200])
        return {"status": "error", "error": f"Unexpected usage response: {type(usage).__name__}"}
    log.info("claude usage keys: %s", list(usage.keys()))

    # Parse with explicit type checks — any field may be None, a number, or unexpected type
    def _dict(val):
        return val if isinstance(val, dict) else {}

    try:
        fh = _dict(usage.get("five_hour"))
        sd = _dict(usage.get("seven_day"))

        return {
            "status": "ok",
            "plan": "Pro",
            "session": {
                "usage_pct": fh.get("utilization", 0) or 0,
                "reset_at": fh.get("resets_at"),
                "remaining_seconds": _remaining_from_iso(fh.get("resets_at")),
            },
            "weekly": {
                "usage_pct": sd.get("utilization", 0) or 0,
                "reset_at": sd.get("resets_at"),
                "remaining_seconds": _remaining_from_iso(sd.get("resets_at")),
            },
            "models": {
                "sonnet": _dict(usage.get("seven_day_sonnet")).get("utilization", 0) or 0,
                "opus": _dict(usage.get("seven_day_opus")).get("utilization", 0) or 0,
            },
            "error": None,
        }
    except Exception as e:
        log.error("claude parse error: %s\n%s", e, traceback.format_exc())
        log.error("claude raw usage: %s", str(usage)[:800])
        return {"status": "error", "error": str(e)}


def _zai_jwt(api_key: str) -> str:
    """Generate a JWT token from Z-AI API key (format: id.secret)."""
    import jwt as pyjwt
    parts = api_key.split(".", 1)
    if len(parts) != 2:
        return api_key  # Not in id.secret format, use as-is
    kid, secret = parts
    now_ms = int(time.time() * 1000)
    payload = {
        "api_key": kid,
        "exp": now_ms + 3600 * 1000,
        "timestamp": now_ms,
    }
    return pyjwt.encode(payload, secret, algorithm="HS256",
                        headers={"alg": "HS256", "sign_type": "SIGN"})


def fetch_zai():
    """Fetch Z (z.ai) usage data. Uses API key from env var ZAI_API_KEY."""
    api_key = os.environ.get("ZAI_API_KEY", "").strip()
    from_env = bool(api_key)
    ls_token = None

    if not api_key:
        # Fallback: try localStorage token (use as-is, do NOT generate JWT)
        ls_token = _read_localstorage("z.ai", "token")
        if ls_token:
            ls_token = ls_token.strip().strip('"').strip("'")
            log.info("z.ai using localStorage token, len=%d", len(ls_token))

    if not api_key and not ls_token:
        return {"status": "offline", "error": "No ZAI_API_KEY env var and no token in localStorage"}

    # ENV var: generate JWT from id.secret format API key
    # localStorage: use raw token as-is (it's already a session JWT)
    if from_env:
        try:
            token = _zai_jwt(api_key)
            log.info("z.ai generated JWT from API key (env)")
        except Exception as e:
            log.warning("z.ai JWT generation failed (%s), using raw key", e)
            token = api_key
    else:
        token = ls_token
        log.info("z.ai using raw localStorage token (no JWT generation)")

    auth = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://chat.z.ai",
        "Referer": "https://chat.z.ai/",
    }

    # Quota limits
    r = _get("https://api.z.ai/api/monitor/usage/quota/limit", headers=auth)
    log.info("z.ai quota response: %d — %s", r.status_code, r.text[:300])
    if r.status_code >= 400:
        # Try alternate endpoint
        r = _get("https://chat.z.ai/api/monitor/usage/quota/limit", headers=auth)
        log.info("z.ai alt quota response: %d — %s", r.status_code, r.text[:300])
    r.raise_for_status()
    body = r.json()

    if not body.get("success"):
        return {"status": "error", "error": body.get("msg", "API error")}

    data = body.get("data", {})
    level = data.get("level", "unknown")
    limits = data.get("limits", [])
    log.info("z.ai limits raw: %s", limits)

    # Z-AI limits format:
    #   type=TIME_LIMIT  (session/hourly)  — has usage, currentValue, remaining, percentage
    #   type=TOKENS_LIMIT unit=3 number=5  (5-hour window) — has percentage
    #   type=TOKENS_LIMIT unit=6 number=1  (weekly)        — has percentage, nextResetTime
    # percentage is directly the usage percent (0-100)
    # nextResetTime is Unix ms timestamp

    session_pct = 0
    weekly_pct = 0
    session_reset = None
    weekly_reset = None

    for lim in limits:
        pct = lim.get("percentage", 0)
        ltype = lim.get("type", "")
        unit = lim.get("unit", 0)
        reset_ms = lim.get("nextResetTime")

        # Convert Unix ms timestamp to remaining seconds
        remaining_sec = None
        if reset_ms:
            remaining_sec = max(0, int((reset_ms / 1000) - time.time()))

        if ltype == "TOKENS_LIMIT" and unit == 6:
            # Weekly token limit
            weekly_pct = pct
            weekly_reset = remaining_sec
        elif ltype == "TOKENS_LIMIT" and unit == 3:
            # 5-hour window
            session_pct = pct
            session_reset = remaining_sec
        elif ltype == "TIME_LIMIT":
            # Hourly request limit — use as session if no 5h window yet
            if session_pct == 0:
                session_pct = pct
                session_reset = remaining_sec

    return {
        "status": "ok",
        "plan": level,
        "session": {
            "usage_pct": session_pct,
            "reset_at": None,
            "remaining_seconds": session_reset,
        },
        "weekly": {
            "usage_pct": weekly_pct,
            "reset_at": None,
            "remaining_seconds": weekly_reset,
        },
        "error": None,
    }


# ---------------------------------------------------------------------------
# Fetch orchestration
# ---------------------------------------------------------------------------
FETCHERS = [
    ("codex", fetch_codex),
    ("kimi", fetch_kimi),
    ("claude", fetch_claude),
    ("zai", fetch_zai),
]


def _do_fetch():
    """Fetch all agents, update cache. Skips if already running."""
    if not _fetch_lock.acquire(blocking=False):
        return
    try:
        results = {}
        statuses = []

        for name, fetcher in FETCHERS:
            try:
                data = fetcher()
                if data.get("status") == "ok":
                    data["last_success"] = datetime.now(timezone.utc).isoformat()
                results[name] = data
                st = "OK" if data.get("status") == "ok" else f"ERR {data.get('error', '?')}"
                statuses.append(f"{name}: {st}")
            except Exception as e:
                with _lock:
                    prev = _cache.get(name)
                if prev and prev.get("last_success"):
                    prev = dict(prev)
                    prev["status"] = "stale"
                    prev["error"] = str(e)
                    results[name] = prev
                else:
                    results[name] = {"status": "error", "error": str(e)}
                statuses.append(f"{name}: ERR {e}")

        now = datetime.now(timezone.utc)
        results["last_fetch"] = now.isoformat()
        results["next_refresh_at"] = (now + timedelta(seconds=REFRESH_INTERVAL)).isoformat()

        with _lock:
            _cache.update(results)

        log.info(" | ".join(statuses))
    finally:
        _fetch_lock.release()


def _background_loop():
    while True:
        _do_fetch()
        time.sleep(REFRESH_INTERVAL)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_file("dashboard.html")


@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify(dict(_cache))


@app.route("/api/refresh")
def api_refresh():
    _do_fetch()
    with _lock:
        return jsonify(dict(_cache))


@app.route("/api/cookies")
def api_cookies():
    if not DEBUG_MODE:
        return jsonify({"error": "Debug mode disabled"}), 403

    domains = {
        "chatgpt.com": "__Secure-next-auth.session-token",
        "kimi.com": "kimi-auth",
        "claude.ai": "sessionKey",
    }
    result = {}
    for domain, key_cookie in domains.items():
        cookies = _read_cookies(domain)
        names = [c[0] for c in cookies]
        # For chatgpt: token can be chunked as .0, .1 etc.
        key_found = key_cookie in names or any(n.startswith(key_cookie + ".") for n in names)
        result[domain] = {
            "total": len(cookies),
            "key_present": key_found,
            "names": names,
        }
    # z.ai uses localStorage
    zai_token = _read_localstorage("z.ai", "z-ai-open")
    result["z.ai"] = {
        "source": "localStorage",
        "key_present": zai_token is not None,
        "token_length": len(zai_token) if zai_token else 0,
    }
    result["profile"] = str(_find_profile())
    result["user_agent"] = _ua()
    return jsonify(result)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
_thread = threading.Thread(target=_background_loop, daemon=True)
_thread.start()
log.info("Agent Stats started — refresh every %ds, debug=%s", REFRESH_INTERVAL, "on" if DEBUG_MODE else "off")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8777, debug=True, use_reloader=False)
