"""The durable directory of handles (ARCHITECTURE.md section 10). It holds metadata only, never resource data."""
import json
import sqlite3
from dataclasses import dataclass

from .adapter import ResourceRef

_DDL = """
CREATE TABLE IF NOT EXISTS handles (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, type TEXT NOT NULL, adapter TEXT NOT NULL,
  locator TEXT NOT NULL, version TEXT NOT NULL, descriptor TEXT NOT NULL, consistency TEXT NOT NULL,
  persistence TEXT NOT NULL, created_at TEXT NOT NULL, released_at TEXT, owner TEXT NOT NULL,
  parents TEXT NOT NULL, operation TEXT);
CREATE TABLE IF NOT EXISTS grants (
  principal TEXT NOT NULL, handle_id TEXT NOT NULL, capability TEXT NOT NULL,
  PRIMARY KEY (principal, handle_id, capability));
CREATE TABLE IF NOT EXISTS operations (
  seq INTEGER PRIMARY KEY, id TEXT NOT NULL, principal TEXT NOT NULL, target TEXT, capability TEXT, input TEXT,
  status TEXT NOT NULL, error_code TEXT, started_at TEXT NOT NULL, duration_ms REAL,
  bytes_transferred INTEGER, source_version TEXT, result_handle TEXT);
"""
_JSON = ("locator", "descriptor", "parents", "operation")


@dataclass
class Record:
    id: str
    kind: str
    type: str
    adapter: str
    locator: dict
    version: str
    descriptor: dict
    consistency: str
    persistence: str
    created_at: str
    released_at: str | None
    owner: str
    parents: list
    operation: dict | None

    @property
    def ref(self) -> ResourceRef:
        return ResourceRef(self.type, self.locator, self.version)


class Registry:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_DDL)

    def put(self, rec: Record) -> None:
        row = {k: json.dumps(v) if k in _JSON else v for k, v in vars(rec).items()}
        self.db.execute(f"INSERT INTO handles ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))

    def get(self, handle_id: str) -> Record | None:
        row = self.db.execute("SELECT * FROM handles WHERE id = ?", (handle_id,)).fetchone()
        return row and Record(**{k: json.loads(row[k]) if k in _JSON else row[k] for k in row.keys()})

    def named(self, name: str) -> list[str]:
        """The ids of the live resource handles with this name, newest first."""
        rows = self.db.execute(
            "SELECT id FROM handles WHERE kind = 'resource' AND released_at IS NULL"
            " AND json_extract(descriptor, '$.name') = ? ORDER BY id DESC", (name,))
        return [r[0] for r in rows]

    def mark_released(self, handle_id: str, at: str) -> None:
        self.db.execute("UPDATE handles SET released_at = ? WHERE id = ?", (at, handle_id))

    def drop_ephemeral(self) -> None:
        """An ephemeral handle belongs to one runtime process. Remove what an earlier process left."""
        self.db.execute("DELETE FROM grants WHERE handle_id IN (SELECT id FROM handles WHERE persistence = 'ephemeral')")
        self.db.execute("DELETE FROM handles WHERE persistence = 'ephemeral'")

    def grant(self, principal: str, handle_id: str, capabilities) -> None:
        self.db.executemany("INSERT OR IGNORE INTO grants VALUES (?, ?, ?)", [(principal, handle_id, c) for c in capabilities])

    def revoke(self, principal: str, handle_id: str) -> None:
        self.db.execute("DELETE FROM grants WHERE principal = ? AND handle_id = ?", (principal, handle_id))

    def grants(self, principal: str, handle_id: str) -> set:
        rows = self.db.execute("SELECT capability FROM grants WHERE principal = ? AND handle_id = ?", (principal, handle_id))
        return {r[0] for r in rows}

    def log(self, **row) -> None:
        if "input" in row:
            row["input"] = json.dumps(row["input"])
        self.db.execute(f"INSERT INTO operations ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))

    def history(self, result_handle: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM operations", ()
        if result_handle:
            q, args = q + " WHERE result_handle = ?", (result_handle,)
        return [dict(r) for r in self.db.execute(q + " ORDER BY seq", args)]
