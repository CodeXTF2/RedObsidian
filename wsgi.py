import eventlet
eventlet.monkey_patch()

import sys
import os
import time
from app import create_app, socketio

app = create_app()

if __name__ == "__main__":
    print("Starting Eventlet WSGI server on http://0.0.0.0:5000...")
    print("Press Ctrl+C to quit.")
    
    # Run the server in a green thread
    server_thread = eventlet.spawn(socketio.run, app, host="0.0.0.0", port=5000, log_output=True)
    
    try:
        # The main thread sleeps in a loop, allowing it to instantly catch KeyboardInterrupt
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nShutting down server immediately...")
        os._exit(0)
