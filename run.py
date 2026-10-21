"""Development server (auto-reload off, debug on, localhost only). Use wsgi.py or gunicorn in production."""
from app import create_app, socketio
from app.maintenance import start_maintenance

app = create_app()
start_maintenance(app)

if __name__ == "__main__":
    socketio.run(
        app,
        host="127.0.0.1",
        port=5000,
        debug=True,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )
