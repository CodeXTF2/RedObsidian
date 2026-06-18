from gevent import monkey
monkey.patch_all()

from app import create_app, socketio

app = create_app()

if __name__ == "__main__":
    print("Starting Gevent WSGI server on http://0.0.0.0:5000...")
    print("Press Ctrl+C to quit.")
    
    # Flask-SocketIO natively wraps the app in a Gevent WSGIServer + WebSocketHandler
    # when async_mode="gevent" is specified.
    socketio.run(app, host="0.0.0.0", port=5000, log_output=True)
