"""JSON over HTTP with the standard library only. The HTTP transport and the remote worker both use it."""
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def post(url: str, path: str, body: bytes, headers: dict) -> tuple[int, dict]:
    """POST a JSON body and return (HTTP status, JSON reply). A connection fault raises OSError or HTTPException.

    There is no timeout: a long operation holds its request open until it ends.
    """
    parts = urlsplit(url)
    connection = (http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection)(parts.netloc)
    try:
        connection.request("POST", parts.path.rstrip("/") + path, body, {"Content-Type": "application/json", **headers})
        reply = connection.getresponse()
        return reply.status, json.loads(reply.read())
    finally:
        connection.close()


def serve(answer, host: str, port: int) -> ThreadingHTTPServer:
    """Make an HTTP server. `answer(path, headers, body)` returns (HTTP status, JSON reply). Each request gets a thread.

    The caller starts it with `serve_forever`. Port 0 lets the system choose a free port (`server.server_port`).
    """
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            status, reply = answer(self.path, self.headers, body)
            data = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)
