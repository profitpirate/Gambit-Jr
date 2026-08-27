from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def start_health_server(port: int, status: Callable[[], dict]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in {"/health", "/status"}:
                self.send_response(404)
                self.end_headers()
                return
            try:
                payload = status()
                code = 200
            except (
                AttributeError,
                KeyError,
                OSError,
                RuntimeError,
                sqlite3.Error,
                TypeError,
                ValueError,
            ) as exc:
                payload = {"status": "error", "error": str(exc)}
                code = 503
            body = json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="health-server", daemon=True).start()
    return server
