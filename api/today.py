"""Live availability for the Today view (Nabeel's review, Oct 19: "update it every 15 minutes").
GET /api/today -> {"as_of", "today", "thresholds", "capacity"} computed now from GHL + Close (build_data.live_today).
Behind the site password (middleware.js covers /api/today). The page calls it when Today is opened and every 15 minutes
while it stays open. A warm function instance reuses its answer for 60 s (on Hobby each instance serves one request
at a time, so simultaneous requests may each compute; that's fine at 2 users).
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

for _p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"), os.path.join(os.getcwd(), "scripts")):
    sys.path.insert(0, _p)
import build_data  # noqa: E402

_cache = {"at": 0.0, "body": None}
_lock = threading.Lock()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            with _lock:
                if _cache["body"] is None or time.time() - _cache["at"] > 60:
                    _cache["body"] = json.dumps(build_data.live_today()).encode()
                    _cache["at"] = time.time()
                body, status = _cache["body"], 200
        except (Exception, SystemExit) as ex:   # GHL/Close down: the page keeps the morning data and says so
            body, status = json.dumps({"error": type(ex).__name__}).encode(), 502
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
