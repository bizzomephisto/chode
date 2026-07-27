"""
Production web server for PETEY Control Panel.
Uses Waitress (production WSGI) instead of Flask's dev server.
"""
from waitress import serve
from web.app import app

if __name__ == "__main__":
    print("[WEB] Starting PETEY Control Panel on http://0.0.0.0:5000")
    print("[WEB] Press Ctrl+C to stop.")
    serve(app, host="0.0.0.0", port=5000, threads=4)
