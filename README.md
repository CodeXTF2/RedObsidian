# RedObsidian

A minimal Flask collaboration app with a multi-project system, CLI-based user/project management, login, realtime markdown pages, a shared graph workspace, and a standalone timeline.

## Run Locally (Development)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage_users.py create yourusername yourpassword
python run.py
```

Open `http://127.0.0.1:5000`.

## Run in Production

RedObsidian uses Flask-SocketIO for realtime collaboration, which requires an asynchronous worker class when run in production.

**Cross-Platform (Windows / Linux / macOS):**
Because Gunicorn does not support Windows natively, the simplest cross-platform production setup uses Gevent's built-in WSGI server. Run the `wsgi.py` file directly:
```bash
python wsgi.py
```
*Note: Under the hood, `Flask-SocketIO` detects `gevent` and automatically starts a production-ready WSGI server with WebSocket support listening on `0.0.0.0:5000`.*

**On Linux/macOS (Alternative using Gunicorn):**
```bash
gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 --bind 0.0.0.0:5000 wsgi:app
```


## User Management

Account creation is handled via the command line:

- **Create a user:** `python manage_users.py create <username> <password>`
- **List users:** `python manage_users.py list`
- **Delete a user:** `python manage_users.py delete <username>`

## Project Management

RedObsidian supports multiple concurrent projects. Manage them via `manage_projects.py`:

- **List projects:** `python manage_projects.py list`
- **Export a project:** `python manage_projects.py export <project_name> <output_file.zip> --password <password>`
- **Import a project:** `python manage_projects.py import <input_file.zip> --password <password> [--name <new_name>]`

You can also create and switch projects directly in the web UI via the **Projects** navigation link.

## Security Notes

- **Authentication Required:** All routes and assets require a valid login.
- **Project Isolation:** Data and uploads are strictly isolated between projects.
- **Uploads:** Files are stored in `instance/uploads/<project_id>/` and served through an authenticated route.
- **Secret Key:** Set `REDOBSIDIAN_SECRET_KEY` in the environment. If it is not set, a random key is generated on first start and stored in `instance/secret_key` (keep that file private; deleting it logs everyone out).
- **CSRF:** All forms and state-changing API calls require a CSRF token (Flask-WTF). Session cookies use `SameSite=Lax`; set `REDOBSIDIAN_SECURE_COOKIES=1` when serving over HTTPS.
- **Realtime origin check:** Websocket connections are accepted from the same origin only. Behind a reverse proxy that changes the host, list the public origin(s) in `REDOBSIDIAN_ALLOWED_ORIGINS` (comma-separated, e.g. `https://notes.example.com`).
- **Deleting:** Only a project's creator can delete it from the web UI. Deleted pages can be restored with Undo; they are purged permanently after 30 days.
- **Offline use:** All frontend assets (including the Socket.IO client) are served locally; no CDN access is needed.
