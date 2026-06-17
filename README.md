# RedObsidian

A minimal Flask collaboration app with secure account creation, login, realtime markdown pages, a shared graph workspace, and a standalone timeline.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

Open `http://127.0.0.1:5000`.

## Notes

- User passwords are stored with Werkzeug password hashing.
- Runtime data is stored in `instance/redteam_obsidian.sqlite`.
- `Notes` is the Obsidian-style markdown workspace with a page list, live editor, and live preview.
- Each graph node has its own full page at `/nodes/<id>` with realtime markdown editing.
- `Graph` is a full-page node map. Drag nodes to move them, drag a node handle onto another node to connect them, and right-drag empty graph space to pan.
- `Timeline` is a standalone full-page event log.
- Realtime collaboration uses Flask-SocketIO in threading mode. Timeline events, graph nodes, graph edits, node markdown edits, and node links are broadcast to connected users.
- Change `SECRET_KEY` in `app/__init__.py` before using outside local development.
