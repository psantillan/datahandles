"""The fixture of the two-resource agent task: a git repository and a local web site.

The repository documents where its status page is. The status page links to the release notes,
and the release notes list the newest version. The README of the repository names an older
version, so an answer from the repository alone is wrong. The correct answer needs both resources.
"""
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

QUESTION = ("What is the newest released version of the shop service? The repository tells you where its status page is. "
            "The status page links to the release notes. Give the version number.")
ANSWER, DECOY = "3.4.1", "3.3.0"

PAGES = {
    "/status": "<title>Shop service status</title><h1>Shop service</h1><p>All systems are working.</p>"
               "<p><a href='/releases'>Release notes</a> | <a href='/incidents'>Incidents</a></p>",
    "/releases": "<title>Release notes</title><h1>Release notes</h1>"
                 "<h2>3.4.1 (2026-11-02)</h2><p>Fix the rounding of tax totals.</p>"
                 "<h2>3.4.0 (2026-10-20)</h2><p>Add gift cards.</p>"
                 "<h2>3.3.0 (2026-09-01)</h2><p>Add the order history page.</p>",
    "/incidents": "<title>Incidents</title><p>No incidents in the last 90 days.</p>",
}


def serve_site() -> tuple[ThreadingHTTPServer, str]:
    """Start the web site on a free local port. Return the server and its base URL."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGES.get(self.path)
            self.send_response(200 if body else 404)
            data = (body or "<title>Not found</title>").encode()
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def make_repo(path: Path, site: str) -> Path:
    """Make the repository in `path` with three commits."""
    def git(*args):
        subprocess.run(["git", "-C", str(path), "-c", "user.name=Shop Team", "-c", "user.email=shop@example.com", *args],
                       check=True, capture_output=True)

    def write(rel, text):
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)

    path.mkdir(parents=True)
    git("init", "-q", "-b", "main")
    write("README.md", f"# Shop service\n\nThe shop service takes orders and payments. Version {DECOY}.\n\n"
                       "How we run it in production: docs/OPERATIONS.md\n")
    write("src/shop/orders.py", "def total(items):\n    return sum(i.price * i.count for i in items)\n")
    git("add", ".")
    git("commit", "-q", "-m", "Add the order service")
    write("docs/OPERATIONS.md", "# Operations\n\nDeploys run from the main branch every weekday.\n\n"
                                f"Status page: {site}/old-status\n")
    git("add", ".")
    git("commit", "-q", "-m", "Add the operations notes")
    write("docs/OPERATIONS.md", "# Operations\n\nDeploys run from the main branch every weekday.\n\n"
                                f"Status page: {site}/status\n\nOn-call: see the team calendar.\n")
    git("commit", "-q", "-am", "Move the status page")
    return path
