from gevent import monkey

monkey.patch_all()

import os  # noqa: E402

from app import create_app, socketio  # noqa: E402
from app.maintenance import start_maintenance  # noqa: E402

app = create_app()
start_maintenance(app)

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    print(f"Starting RedObsidian on http://{host}:{port} (put a TLS reverse proxy in front of it)")
    # Flask-SocketIO serves the app with gevent's WSGI server and simple-websocket for WebSockets
    socketio.run(app, host=host, port=port, log_output=True)
