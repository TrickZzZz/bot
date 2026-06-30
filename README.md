# Account Manager

A self-hosted credential manager: a login-protected web UI for storing, editing, and bulk-importing your own account credentials, backed by a FastAPI + SQLAlchemy API.

```
account-manager/
├── backend/    FastAPI + SQLAlchemy + SQLite (CRUD, auth, bulk import)
└── frontend/   React + Vite + Tailwind + shadcn (login + dashboard)
```

## Quick start

**1. Backend**
```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python generate_secrets.py          # writes .env with required keys
export $(cat .env | xargs)
uvicorn app.main:app --reload --port 8000
```

**2. Create your login**
```bash
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"you","password":"a-strong-password"}'
```

**3. Frontend**
```bash
cd frontend
npm install
npm run dev      # http://localhost:5173
```

## How security is handled

This stores real passwords, so it's built to not be a liability:

- **Login passwords** (the `users` table) are **bcrypt-hashed** — never stored or compared in plaintext.
- **Stored account passwords** (the `accounts` table) are **encrypted at rest with Fernet** (AES-128-CBC + HMAC). They're encrypted rather than hashed because the whole point is to retrieve and display them — but they're never written to the database in plaintext.
- **Two secrets are required env vars** (`JWT_SECRET_KEY`, `ACCOUNT_ENCRYPTION_KEY`); the app refuses to boot without them, so there's no insecure default to forget about.
- **Every `/accounts` route is JWT-protected and owner-scoped** — a user can only ever see or touch rows where `owner_id` matches their own id.
- **Back up `ACCOUNT_ENCRYPTION_KEY` separately** from the database. Lose it and every stored password is permanently unrecoverable.

## Database schema

**users**
| column | type | notes |
|---|---|---|
| id | integer PK | |
| username | varchar(100) unique | |
| password_hash | varchar(255) | bcrypt |
| created_at | datetime | |

**accounts**
| column | type | notes |
|---|---|---|
| id | integer PK | |
| service_name | varchar(150) | |
| username | varchar(150) | |
| password_encrypted | varchar(500) | Fernet ciphertext |
| url | varchar(500) | nullable |
| notes | varchar(1000) | nullable |
| owner_id | integer FK → users.id | |
| created_at / updated_at | datetime | |

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/register` | Create a WebUI login |
| POST | `/auth/login` | Get a JWT |
| GET | `/auth/me` | Current user |
| GET | `/accounts?search=` | List / search your accounts |
| POST | `/accounts` | Create |
| GET | `/accounts/{id}` | Read one |
| PUT | `/accounts/{id}` | Update |
| DELETE | `/accounts/{id}` | Delete |
| POST | `/accounts/bulk-import` | Import many at once |

## Hardening before real use

- Swap SQLite for Postgres via `DATABASE_URL`.
- Put the whole thing behind HTTPS (the JWT and decrypted passwords travel over the wire).
- After creating your login, restrict or disable `/auth/register` so the UI can't mint new logins.
- Consider short-lived access tokens + refresh tokens instead of the single 60-minute JWT.
- Store secrets in a real secrets manager, not a committed `.env`.
