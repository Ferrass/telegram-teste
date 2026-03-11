# Telegram Post Scheduler — Backend

A production-ready FastAPI backend for scheduling and analysing posts to Telegram channels using the **MTProto** client API (Telethon), not the Bot API.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        FastAPI (port 8000)                        │
│  /auth  │  /telegram  │  /posts  │  /analytics  │  /health       │
└──────────────────────────┬───────────────────────────────────────┘
                           │ SQLAlchemy (asyncpg)
                           ▼
                    ┌─────────────┐
                    │  PostgreSQL │
                    └─────────────┘
                           ▲
               ┌───────────┴────────────┐
               │    Worker Process       │
               │  • Scheduler  (60 s)   │
               │  • Metrics   (30 min)  │
               └────────────────────────┘
```

### Module responsibilities

| File | Purpose |
|---|---|
| `app/config.py` | Pydantic-settings — reads `.env` |
| `app/database.py` | Async SQLAlchemy engine & session |
| `app/models.py` | ORM models (users, accounts, channels, posts, metrics) |
| `app/telegram_client.py` | Telethon client factory + session encryption |
| `app/auth_service.py` | JWT auth + Telegram MTProto login flow |
| `app/channel_service.py` | Admin-channel discovery via Telethon |
| `app/post_service.py` | Post CRUD |
| `app/metrics_service.py` | View/forward collection from Telegram |
| `app/worker.py` | Long-running scheduler + metrics worker |
| `app/main.py` | FastAPI app, all routes |

---

## Prerequisites

- Python 3.12+
- PostgreSQL 14+
- Telegram API credentials from [https://my.telegram.org/apps](https://my.telegram.org/apps)

---

## Quick-start (local)

### 1. Clone and install

```bash
git clone <repo>
cd telegram-scheduler
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Generate security keys

```bash
python generate_keys.py
```

Copy the output into your `.env` file.

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in:
#   DATABASE_URL
#   SECRET_KEY          (from step 2)
#   ENCRYPTION_KEY      (from step 2)
#   TELEGRAM_API_ID     (from my.telegram.org)
#   TELEGRAM_API_HASH   (from my.telegram.org)
```

### 4. Start PostgreSQL and create the database

```bash
psql -U postgres -c "CREATE DATABASE telegram_scheduler;"
```

### 5. Run database migrations

```bash
# Auto-create tables (development shortcut)
python -c "import asyncio; from app.database import init_db; asyncio.run(init_db())"

# OR use Alembic (recommended for production)
alembic upgrade head
```

### 6. Start the API server

```bash
uvicorn app.main:app --reload --port 8000
```

### 7. Start the worker (separate terminal)

```bash
python -m app.worker
```

API docs: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## Docker Compose (recommended)

```bash
cp .env.example .env   # fill in API credentials and keys
docker compose up --build
```

This starts three containers: `db`, `api`, `worker`.

---

## API Reference

### Auth

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/auth/register` | Register with email + password |
| `POST` | `/auth/login` | Get JWT (OAuth2 password form) |

### Telegram login

| Method | Path | Body | Description |
|--------|------|------|-------------|
| `POST` | `/telegram/send-code` | `{"phone_number": "+44..."}` | Send SMS code |
| `POST` | `/telegram/verify-code` | `{"phone_number", "code", "password?"}` | Complete login, store session |

### Channels

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/telegram/channels` | List cached admin channels |
| `GET` | `/telegram/channels?sync=true` | Refresh from Telegram and return |

### Posts

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/posts/schedule` | Schedule a new post |
| `GET` | `/posts` | List all posts (filter by `?status_filter=scheduled`) |
| `DELETE` | `/posts/{id}` | Cancel a scheduled post |

### Analytics

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/analytics/posts` | Sent posts with views & forwards |

---

## Telegram login flow (detailed)

```
Client                         API                          Telegram
  │                             │                               │
  │  POST /telegram/send-code   │                               │
  │ ─────────────────────────►  │  send_code_request(phone)     │
  │                             │ ─────────────────────────────►│
  │                             │                     SMS code  │
  │  POST /telegram/verify-code │                               │
  │ ─────────────────────────►  │  sign_in(phone, code)         │
  │                             │ ─────────────────────────────►│
  │                             │  StringSession (encrypted)    │
  │                             │  stored in telegram_accounts  │
  │         { "detail": "ok" }  │                               │
  │ ◄─────────────────────────  │                               │
```

If 2FA is enabled, pass `"password"` in the verify-code request body.

---

## Security

| Concern | Approach |
|---------|----------|
| Telegram sessions | AES-256 Fernet encryption at rest |
| User passwords | bcrypt hashing (passlib) |
| API access | JWT Bearer tokens (python-jose) |
| Ownership | Every DB query filters by `user_id` |
| Secrets | Never returned to frontend; only stored encrypted |

### Generating a valid ENCRYPTION_KEY

```python
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
```

---

## Worker internals

### Scheduler (every 60 s)

1. Query `scheduled_posts` where `status = scheduled AND scheduled_time <= now`
2. For each post, load the owner's decrypted Telethon session
3. Call `send_message()` or `send_file()` on the target channel
4. Update `status → sent`, store `telegram_message_id`
5. On failure: `status → failed`

### Metrics collector (every 30 min)

1. Query all `status = sent` posts with a `telegram_message_id`
2. For each post, call `GetMessagesRequest` via Telethon
3. Extract `views` and `forwards`
4. Update `scheduled_posts` row
5. Insert snapshot into `post_metrics_history`

---

## Database schema

```sql
-- users
CREATE TABLE users (
    id            SERIAL PRIMARY KEY,
    email         VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- telegram_accounts
CREATE TABLE telegram_accounts (
    id                       SERIAL PRIMARY KEY,
    user_id                  INT REFERENCES users(id) ON DELETE CASCADE,
    phone_number             VARCHAR(20) NOT NULL,
    session_string_encrypted TEXT NOT NULL,
    connected_at             TIMESTAMPTZ DEFAULT NOW(),
    last_used_at             TIMESTAMPTZ
);

-- channels
CREATE TABLE channels (
    id           SERIAL PRIMARY KEY,
    user_id      INT REFERENCES users(id) ON DELETE CASCADE,
    channel_id   BIGINT NOT NULL,
    channel_name VARCHAR(255) NOT NULL,
    username     VARCHAR(255),
    member_count INT DEFAULT 0
);

-- scheduled_posts
CREATE TABLE scheduled_posts (
    id                  SERIAL PRIMARY KEY,
    user_id             INT REFERENCES users(id) ON DELETE CASCADE,
    channel_id          INT REFERENCES channels(id) ON DELETE CASCADE,
    message             TEXT NOT NULL,
    media_url           VARCHAR(2048),
    scheduled_time      TIMESTAMPTZ NOT NULL,
    status              VARCHAR(20) DEFAULT 'scheduled',
    telegram_message_id BIGINT,
    views               INT DEFAULT 0,
    forwards            INT DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- post_metrics_history
CREATE TABLE post_metrics_history (
    id          SERIAL PRIMARY KEY,
    post_id     INT REFERENCES scheduled_posts(id) ON DELETE CASCADE,
    views       INT DEFAULT 0,
    forwards    INT DEFAULT 0,
    captured_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Error handling

| Telegram Error | HTTP response |
|----------------|---------------|
| `FloodWaitError` | `429` with wait time in detail |
| `PhoneCodeInvalidError` | `400` Invalid code |
| `PhoneCodeExpiredError` | `400` Code expired |
| `SessionPasswordNeededError` | `400` asking for 2FA password |
| No session in DB | `400` not connected |

---

## Production checklist

- [ ] Set `APP_ENV=production` (disables SQL echo)
- [ ] Use a strong random `SECRET_KEY` and `ENCRYPTION_KEY`
- [ ] Put the API behind nginx / a reverse proxy with TLS
- [ ] Replace in-memory `_pending_clients` with Redis (for multi-process deployments)
- [ ] Run Alembic migrations instead of `init_db()`
- [ ] Set up log shipping (Datadog, Loki, etc.)
- [ ] Configure DB connection pooling (PgBouncer)
- [ ] Restrict CORS `allow_origins` to your frontend domain
