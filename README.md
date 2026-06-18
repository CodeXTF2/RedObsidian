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
- **Secret Key:** Change `SECRET_KEY` in `app/__init__.py` before using outside local development.
