# Account Manager — Frontend (React + Vite + Tailwind + shadcn)

## Setup

```bash
cd frontend
npm install
npm run dev
```

Opens at http://localhost:5173 and expects the backend at http://localhost:8000.
To point at a different backend, set `VITE_API_BASE` in a `.env` file:

```
VITE_API_BASE=http://localhost:8000
```

## What's here

- **LoginPage** — sign in / create a vault login.
- **DashboardPage** — table of your accounts with:
  - **Create** / **Edit** / **Delete** (with confirm dialog)
  - **Search** by service or username (debounced)
  - **Show/hide** and **copy** passwords per row
  - **Bulk import** from CSV or JSON
- Auth token stored in `localStorage`; all requests send it as a Bearer header. A 401 clears the token and bounces you to login.

## Bulk import formats

**CSV** (header row required):
```
service_name,username,password,url,notes
GitHub,me@example.com,s3cret,https://github.com,work account
AWS,admin,hunter2,https://console.aws.amazon.com,
```

**JSON**:
```json
[
  { "service_name": "GitHub", "username": "me", "password": "s3cret", "url": "https://github.com" },
  { "service_name": "AWS", "username": "admin", "password": "hunter2" }
]
```

## Stack

- React 18 + Vite 5
- Tailwind CSS 3 with shadcn-style components (Button, Input, Label, Card, Dialog) built on Radix primitives
- lucide-react icons
- No router dependency — single auth gate in `App.jsx` keeps it minimal
