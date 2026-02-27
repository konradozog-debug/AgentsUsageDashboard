# API Endpoints

All endpoints are unofficial APIs — they may change without notice.
Last updated: 2026-02-26

---

## OpenAI Codex (chatgpt.com)

**Auth**: Cookie → Bearer token exchange

### 1. Cookie-to-Bearer token exchange

```
GET https://chatgpt.com/api/auth/session
Headers:
  Cookie: {full cookie string from chatgpt.com}
```

Key cookie: `__Secure-next-auth.session-token`

**Response:**
```json
{ "accessToken": "eyJhb..." }
```

### 2. Usage

```
GET https://chatgpt.com/backend-api/wham/usage
Headers:
  Authorization: Bearer {accessToken}
```

**Response:**
```json
{
  "plan_type": "plus",
  "rate_limit": {
    "limit_reached": false,
    "primary_window": {
      "used_percent": 42.0,
      "limit_window_seconds": 18000,
      "reset_after_seconds": 14400,
      "reset_at": 1772143992
    },
    "secondary_window": {
      "used_percent": 88,
      "limit_window_seconds": 604800,
      "reset_after_seconds": 68976,
      "reset_at": 1772206105
    }
  },
  "credits": { "balance": 0.0, "has_credits": false }
}
```

- `primary_window` = 5-hour session window
- `secondary_window` = weekly window
- `reset_at` = Unix timestamp (seconds)
- Field name varies: `used_percent` or `usage_percent` — handle both

### 3. Daily breakdown

```
GET https://chatgpt.com/backend-api/wham/usage/daily-token-usage-breakdown
Headers:
  Authorization: Bearer {accessToken}
```

**Response:**
```json
{ "units": "percent", "data": [ "...30 days..." ] }
```

---

## Kimi Code (kimi.com)

**Auth**: Cookie `kimi-auth` is directly a Bearer JWT token.

**Protocol**: Connect protocol (gRPC-Web compatible) — requires special headers.

### Required headers

```
Authorization: Bearer {kimi-auth}
Cookie: kimi-auth={kimi-auth}
Content-Type: application/json
Origin: https://www.kimi.com
Referer: https://www.kimi.com/code/console
Accept: */*
connect-protocol-version: 1
x-msh-platform: web
x-language: en-US
```

### 1. Usage (GetUsages)

```
POST https://www.kimi.com/apiv2/kimi.gateway.billing.v1.BillingService/GetUsages
Body:
  { "scope": ["FEATURE_CODING"] }
```

**IMPORTANT**: `scope` must be an **array** `["FEATURE_CODING"]`, not a string.

**Response:**
```json
{
  "usages": [{
    "scope": "FEATURE_CODING",
    "detail": {
      "limit": "100",
      "used": "16",
      "remaining": "84",
      "resetTime": "2026-03-05T16:00:00Z"
    },
    "limits": [{
      "window": { "duration": 300, "timeUnit": "TIME_UNIT_MINUTE" },
      "detail": {
        "limit": "100",
        "remaining": "100",
        "resetTime": "2026-02-26T22:10:00Z"
      }
    }]
  }]
}
```

- `detail` = weekly limit
- `limits[0]` = rate limit (5-minute window)
- Values are strings — parse to int

### 2. Subscription (GetSubscription)

```
POST https://www.kimi.com/apiv2/kimi.gateway.order.v1.SubscriptionService/GetSubscription
Body: {}
```

**IMPORTANT**: Path is `kimi.gateway.order.v1.SubscriptionService`, NOT `billing.v1.BillingService`.

**Response** (key fields):
```json
{
  "subscription": {
    "goods": {
      "title": "Allegretto"
    }
  }
}
```

- Plan name: `subscription.goods.title`

### Proto service mapping

| Service | Package |
|---------|---------|
| GetUsages | `kimi.gateway.billing.v1.BillingService` |
| GetSubscription | `kimi.gateway.order.v1.SubscriptionService` |

---

## Claude (claude.ai)

**Auth**: Full cookie string (key: `sessionKey`) + Anthropic-specific headers.

### Required headers

```
Cookie: {full cookie string}
anthropic-client-platform: web_claude.ai
anthropic-client-version: 1.0.0
```

### 1. Auto-detect org_id

```
GET https://claude.ai/api/organizations
```

**Response:**
```json
[
  { "uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", "name": "...", "capabilities": ["chat"] },
  { "uuid": "...", "name": "...", "capabilities": ["other"] }
]
```

- Select org with capability `"chat"` (personal account)
- Cache `uuid` — it does not change
- Each element may not be a dict — guard with `isinstance(org, dict)`

### 2. Usage

```
GET https://claude.ai/api/organizations/{org_id}/usage
```

**Response:**
```json
{
  "five_hour": { "resets_at": "2025-01-01T12:00:00Z", "utilization": 39.0 },
  "seven_day": { "resets_at": "2025-01-07T00:00:00Z", "utilization": 17.0 },
  "seven_day_sonnet": { "resets_at": "...", "utilization": 0.0 },
  "seven_day_opus": { "resets_at": "...", "utilization": 0.0 },
  "seven_day_cowork": { ... },
  "iguana_necktie": { ... },
  "extra_usage": null
}
```

- `five_hour` = session window (5h)
- `seven_day` = weekly (all models)
- `utilization` = usage percentage directly (0-100)
- Any value may be `None`/non-dict — guard with `isinstance(val, dict)`

---

## Z-AI (z.ai / Zhipu AI)

**Auth**: API key in `{id}.{secret}` format, wrapped in JWT.

Z-AI is the international brand of Zhipu AI (ChatGLM). Chinese counterpart: `open.bigmodel.cn`.

### Getting an API key

Portal: https://z.ai/manage-apikey/apikey-list

Key format: `{api_key_id}.{secret}` (two parts separated by a dot)

### JWT generation

API key must be wrapped in a JWT token before use:

```python
import jwt
import time

def generate_token(api_key: str) -> str:
    kid, secret = api_key.split(".", 1)
    now_ms = int(time.time() * 1000)
    payload = {
        "api_key": kid,
        "exp": now_ms + 3600 * 1000,
        "timestamp": now_ms,
    }
    return jwt.encode(payload, secret, algorithm="HS256",
                      headers={"alg": "HS256", "sign_type": "SIGN"})
```

Note: short keys (<32 bytes secret) generate `InsecureKeyLengthWarning` — can be safely ignored.

### 1. Usage / Quota

```
GET https://api.z.ai/api/monitor/usage/quota/limit
Headers:
  Authorization: Bearer {jwt_token}
  Accept: application/json, text/plain, */*
  Origin: https://chat.z.ai
  Referer: https://chat.z.ai/
```

**Response:**
```json
{
  "code": 200,
  "msg": "Operation successful",
  "success": true,
  "data": {
    "limits": [
      {
        "type": "TIME_LIMIT",
        "unit": 5,
        "number": 1,
        "usage": 1000,
        "currentValue": 0,
        "remaining": 1000,
        "percentage": 0,
        "nextResetTime": 1774185925983,
        "usageDetails": [
          {"modelCode": "search-prime", "usage": 0},
          {"modelCode": "web-reader", "usage": 0}
        ]
      },
      {
        "type": "TOKENS_LIMIT",
        "unit": 3,
        "number": 5,
        "percentage": 0
      },
      {
        "type": "TOKENS_LIMIT",
        "unit": 6,
        "number": 1,
        "percentage": 12,
        "nextResetTime": 1772371525998
      }
    ]
  }
}
```

### Limit interpretation

| type | unit | meaning |
|------|------|---------|
| `TIME_LIMIT` | 5 | Hourly request limit |
| `TOKENS_LIMIT` | 3 | 5-hour token window (session) |
| `TOKENS_LIMIT` | 6 | Weekly token window |

- `percentage` = usage percentage directly (0-100), no calculation needed
- `nextResetTime` = Unix timestamp in **milliseconds** (divide by 1000)
- `usage` in TIME_LIMIT is the *limit* (max requests), not *used* — don't confuse!
- `success: false` with `code: 401` = token expired/invalid

### Other Z-AI endpoints

| Endpoint | Description |
|----------|-------------|
| `https://api.z.ai/api/paas/v4/chat/completions` | Standard API (pay-as-you-go) |
| `https://api.z.ai/api/coding/paas/v4/chat/completions` | Coding Plan API (subscription) |

---

## Firefox cookie/localStorage reading

### Cookies (all services except Z-AI)

File: `{firefox_profile}/cookies.sqlite`, table `moz_cookies`

```sql
SELECT name, value FROM moz_cookies WHERE host LIKE '%{domain}%'
```

Copy the file before reading (Firefox may hold a lock).

### localStorage — Firefox LSNG (79+)

Firefox 79+ uses per-origin SQLite databases:

```
{firefox_profile}/storage/default/https+++{domain}/ls/data.sqlite
```

- Origin in directory name: `https+++chat.z.ai` (protocol `+++` domain)
- Table: `data`, columns: `key`, `utf16_length`, `conversion_type`, `value` (BLOB)
- Value is a UTF-8 blob, decode with `value.decode("utf-8", errors="replace")`

Fallback to legacy `webappsstore.sqlite` (Firefox <79):
```sql
SELECT key, value FROM webappsstore2 WHERE originKey LIKE '%{reversed_domain}%'
```

### curl_cffi

Uses `curl_cffi` with `impersonate="firefox"` for TLS fingerprint matching (JA3).
