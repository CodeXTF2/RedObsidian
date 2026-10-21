# RedObsidian

A self-hosted reporting and collaboration platform for pentesters and red teams: a realtime Markdown vault, a
graph of linked pages, an engagement timeline, structured findings with CVSS, and report export to Word, PDF
and Markdown, with per-project access control, two-factor authentication, an audit log and encrypted
backups.

## How the app is organised

- **Projects** are engagements. Each has members with a role:
  - **Owner:** manages members and settings, exports, archives and deletes the project.
  - **Editor:** changes content.
  - **Viewer:** read-only.

  Administrators can open every project. Use the switcher at the top left to move between projects.
- Inside a project the tabs follow the engagement:
  - **Vault:** notes pages. Each page has *History* (every version, with diffs and restore) and *Create finding*.
  - **Graph:** pages as linked nodes.
  - **Timeline:** activity log with start and end times.
  - **Findings (beta, off by default):** severity, CVSS 3.1 vector and auto-score, status, affected assets, and evidence pages. A project owner turns it on under *Settings → Features*; turning it off again hides the tab and leaves findings out of reports, but keeps them.
  - **Report (beta, off by default):** cover page and narrative, then export to Word (.docx), print to PDF, or Markdown. With Findings on, each finding is included with its evidence pages and screenshots. Turned on the same way as Findings; turning it off hides the tab but keeps the report details.
  - **Files:** images and other uploads.
  - **Settings:** members, beta features, export, archive, delete, activity log.
- **Account** (user menu, top right): change password, set up two-factor authentication and recovery codes, sign out other sessions.
- **Administration** (admins only): users, all projects, security settings and the audit log.

## Security settings (all off by default)

An administrator turns these on under **Administration → Security & settings**. Every change is audit-logged.

| Setting | What it does |
|---|---|
| Password policy | Minimum length, required character classes, optional expiry |
| Require two-factor authentication | Everyone without TOTP is sent to set it up (SSO sign-ins are exempt) |
| Brute-force protection | Locks a username after N failures in a window (an IP address gets 5× the limit) |
| Idle sign-out | Ends sessions after N minutes of inactivity |
| Single sign-on (OIDC) | Entra ID, Okta, Google, Keycloak, Authentik and others; optional auto-provisioning; optional password sign-in only for admins |
| Only admins create projects | Restricts project creation and import |
| Retention | How long deleted pages, archived projects and audit entries are kept |
| Upload size | Per-file limit |
| Report template | A .docx with your styles and cover page that exports build on |

These are always on:

- CSRF protection and a strict Content-Security-Policy (no inline script).
- Uploads served as downloads unless they are raster images, sandboxed, with `nosniff`.
- Per-event permission checks on the realtime socket.
- Session revocation on password change or account disable.
- Path-traversal-safe imports.

## Quick start (development)

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python manage_users.py create yourname --admin     # prompts for a password
python run.py
```

Open `http://127.0.0.1:5000`. The first account created is always an administrator.

Run the checks with `pytest` and `ruff check .`.

## Production (Docker Compose)

The stack is Caddy (automatic TLS), the app (gunicorn with gevent), PostgreSQL and Redis, plus a nightly encrypted backup job.

```bash
cp .env.example .env          # set REDOBSIDIAN_DOMAIN, REDOBSIDIAN_SECRET_KEY, POSTGRES_PASSWORD, REDOBSIDIAN_BACKUP_PASSWORD
docker compose up -d
docker compose exec app python manage_users.py create yourname --admin
```

- Database migrations run automatically when the container starts.
- Backups land in `./backups`, AES-encrypted, keeping 14. Copy them off the host.
- Health check: `GET /healthz`.
- Scaling out: run more `app` containers. Redis fans out realtime events and Caddy keeps each browser on one container.

### Without Docker

Set `DATABASE_URL` (PostgreSQL recommended for teams) and `REDOBSIDIAN_SECRET_KEY`, then either:

- Linux: `gunicorn --worker-class gevent --workers 1 --bind 0.0.0.0:5000 wsgi:app`
- Windows or anywhere else: `python wsgi.py` (honours `HOST` and `PORT`)

Always put a TLS reverse proxy in front of it. Then set:

- `REDOBSIDIAN_SECURE_COOKIES=1`
- `REDOBSIDIAN_BEHIND_PROXY=1`, so client IPs, `https` SSO callbacks and HSTS are correct.

### Environment variables

| Variable | Purpose |
|---|---|
| `REDOBSIDIAN_SECRET_KEY` | Session signing key. If unset, one is generated in `instance/secret_key` |
| `DATABASE_URL` | e.g. `postgresql://user:pass@host/db`. Default is SQLite in `instance/` |
| `REDIS_URL` | Message queue for multiple app processes |
| `REDOBSIDIAN_SECURE_COOKIES` | `1` when served over HTTPS |
| `REDOBSIDIAN_BEHIND_PROXY` | `1` behind one reverse proxy (trusts `X-Forwarded-*`) |
| `REDOBSIDIAN_ALLOWED_ORIGINS` | Extra WebSocket origins if the proxy rewrites `Host` |
| `REDOBSIDIAN_INSTANCE_PATH` / `REDOBSIDIAN_UPLOAD_DIR` | Where data and uploads live |
| `REDOBSIDIAN_AUTO_MIGRATE` | `0` to skip migrations at start-up (default `1`) |
| `REDOBSIDIAN_MAX_REQUEST_MB` | Hard request-size cap (default 1024) |
| `REDOBSIDIAN_LOG_LEVEL` | Default `INFO`; logs go to stdout |

## Command-line tools

Passwords are always prompted for, or read from an environment variable for automation. They are never
taken as command-line arguments.

```text
python manage_users.py create <name> [--admin] [--generate] [--must-change]
python manage_users.py list | reset-password <name> | disable <name> | enable <name>
python manage_users.py make-admin <name> | remove-admin <name> | reset-2fa <name> | delete <name>

python manage_projects.py list
python manage_projects.py export <name-or-id> out.zip            # REDOBSIDIAN_EXPORT_PASSWORD or prompt
python manage_projects.py import in.zip [--name N] [--owner U]
python manage_projects.py member <name-or-id> <user> owner|editor|viewer|remove
python manage_projects.py delete <name-or-id>

python manage_backup.py backup --dir backups --keep 14            # REDOBSIDIAN_BACKUP_PASSWORD or prompt
python manage_backup.py restore backups/<file>.zip [--force]      # stop the app first
```

## Upgrading from earlier versions

Start the new version against your existing `instance/` folder. On first start it:

- Brings the old SQLite database under migrations, without data loss.
- Makes the **first account an administrator**.
- Gives every project's creator the **owner** role and every other existing account the **editor** role, so nobody loses access. Trim membership afterwards in each project's Settings.
- Signs everyone out once, because sessions are now revocable.
- Keeps old links (`/notes`, `/nodes/<id>`, `/uploads/<file>`) working by redirecting into the right project.

Back up `instance/` before upgrading.

## Development workflow

- CI (`.github/workflows/ci.yml`) runs ruff, checks that the migrations match the models, runs the tests on SQLite, runs the migrations on PostgreSQL, runs `pip-audit`, and builds and smoke-tests the Docker image.
- Schema changes: edit `app/models.py`, then `flask --app wsgi db migrate -m "describe change"`, review the file in `migrations/versions/`, and commit it.
- Frontend code is native ES modules in `app/protected_static/js/` (no build step):
  - `core.js`: shared state and helpers
  - `editor.js`, `notes.js`, `graph.js`, `timeline.js`, `vault.js`, `search.js`
  - `main.js`: wiring

  Server-rendered pages use `app/static/js/ui.js`.

## Limitations

- Notes are saved per field, with conflict detection and page history. Two people typing in the same page at the same moment get a "keep mine / load theirs" prompt rather than a live merge.
- Uploads are stored on local disk (or a mounted volume). Disk-level secure deletion depends on the storage; use an encrypted volume.
