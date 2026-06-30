# Account Manager — Backend (FastAPI)

## Setup

```bash
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Generate JWT_SECRET_KEY and ACCOUNT_ENCRYPTION_KEY into .env
python generate_secrets.py

# Load .env (or use python-dotenv / your shell's env loading)
export $(cat .env | xargs)   # Linux/Mac
```

## Run

```bash
uvicorn app.main:app --reload --port 8000
```

API docs at http://localhost:8000/docs

## Create your first login user

```bash
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username": "you", "password": "a-strong-password-here"}'
```

After the first user exists, consider removing/restricting `/auth/register`
in `routers_auth.py` (e.g. gate it behind an env flag) so the WebUI can't be
used to create arbitrary new logins.

## Database schema

**users** (WebUI login)
| column        | type         | notes                      |
|---------------|--------------|-----------------------------|
| id            | integer PK   |                              |
| username      | varchar(100) | unique                      |
| password_hash | varchar(255) | bcrypt hash, never plaintext |
| created_at    | datetime     |                              |

**accounts** (the registered accounts being managed)
| column             | type          | notes                                   |
|---------------------|---------------|------------------------------------------|
| id                  | integer PK    |                                            |
| service_name        | varchar(150)  | e.g. "GitHub", "AWS Console"               |
| username            | varchar(150)  | account username/email                    |
| password_encrypted  | varchar(500)  | Fernet-encrypted, never plaintext         |
| url                 | varchar(500)  | optional                                  |
| notes               | varchar(1000) | optional                                  |
| owner_id            | integer FK    | references users.id, scopes data per user |
| created_at          | datetime      |                                            |
| updated_at          | datetime      |                                            |

## Security notes

- Login passwords are hashed with bcrypt (`passlib`) — never stored or compared in plaintext.
- Stored account passwords are encrypted with Fernet (AES-128-CBC + HMAC) using `ACCOUNT_ENCRYPTION_KEY` — not hashed, since they must be decryptable to display/copy them in the UI.
- Both secrets are required env vars; the app refuses to start without them, so nothing falls back to an insecure default.
- All `/accounts` routes require a valid JWT bearer token and are scoped to `owner_id == current_user.id` — one user cannot read or modify another user's accounts.
- Losing `ACCOUNT_ENCRYPTION_KEY` makes all stored account passwords permanently unrecoverable — back it up somewhere safe (e.g. a secrets manager), separately from the database file.
- For production: switch `DATABASE_URL` to Postgres, put this behind HTTPS, and consider short-lived access tokens + refresh tokens instead of the single 60-minute JWT used here.
