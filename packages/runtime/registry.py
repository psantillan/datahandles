"""The durable directory of handles (ARCHITECTURE.md section 10). It holds metadata only, never resource data.

It also holds the job table and the worker table, so job state lives outside each worker process.
One lock makes each method safe to call from the job thread and from transport threads.
"""
import json
import sqlite3
import threading
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
CREATE TABLE IF NOT EXISTS jobs (
  seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, handle_id TEXT NOT NULL, principal TEXT NOT NULL,
  operation TEXT NOT NULL, capability TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL,
  cancel INTEGER NOT NULL, submitted_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS node_runs (
  principal TEXT NOT NULL, key TEXT NOT NULL, result TEXT NOT NULL, at TEXT NOT NULL,
  PRIMARY KEY (principal, key));
CREATE TABLE IF NOT EXISTS approvals (
  seq INTEGER PRIMARY KEY, key TEXT NOT NULL, node TEXT NOT NULL, approver TEXT NOT NULL, at TEXT NOT NULL,
  used_at TEXT, used_by TEXT);
CREATE TABLE IF NOT EXISTS workers (
  id TEXT PRIMARY KEY, url TEXT NOT NULL, key TEXT NOT NULL, info TEXT NOT NULL, registered_at TEXT NOT NULL);
"""
_JSON = ("locator", "descriptor", "parents", "operation")
_JOB_JSON = ("operation", "capability", "result")


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


def _job(row) -> dict | None:
    return row and {k: json.loads(row[k]) if k in _JOB_JSON and row[k] is not None else row[k] for k in row.keys()}


class Registry:
    def __init__(self, path):
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_DDL)

    def _all(self, sql: str, args=()) -> list:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def _insert(self, table: str, row: dict) -> None:
        self._all(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))

    # ---- handles ------------------------------------------------------------------------------

    def put(self, rec: Record) -> None:
        self._insert("handles", {k: json.dumps(v) if k in _JSON else v for k, v in vars(rec).items()})

    def get(self, handle_id: str) -> Record | None:
        rows = self._all("SELECT * FROM handles WHERE id = ?", (handle_id,))
        return Record(**{k: json.loads(rows[0][k]) if k in _JSON else rows[0][k] for k in rows[0].keys()}) if rows else None

    def named(self, name: str) -> list[str]:
        """The ids of the live resource handles with this name, newest first."""
        return [r[0] for r in self._all(
            "SELECT id FROM handles WHERE kind = 'resource' AND released_at IS NULL"
            " AND json_extract(descriptor, '$.name') = ? ORDER BY id DESC", (name,))]

    def live_ids(self, kind: str | None = None) -> list[str]:
        """The ids of the handles that are not released, oldest first. `kind` limits the list to one handle kind."""
        return [r[0] for r in self._all(
            "SELECT id FROM handles WHERE released_at IS NULL AND (? IS NULL OR kind = ?) ORDER BY id", (kind, kind))]

    def mark_released(self, handle_id: str, at: str) -> None:
        self._all("UPDATE handles SET released_at = ? WHERE id = ?", (at, handle_id))

    def drop_ephemeral(self) -> None:
        """An ephemeral handle belongs to one runtime process. Remove what an earlier process left."""
        with self._lock:
            self._all("DELETE FROM grants WHERE handle_id IN (SELECT id FROM handles WHERE persistence = 'ephemeral')")
            self._all("DELETE FROM handles WHERE persistence = 'ephemeral'")
            self._all("DELETE FROM jobs WHERE handle_id NOT IN (SELECT id FROM handles)")

    # ---- grants -------------------------------------------------------------------------------

    def grant(self, principal: str, handle_id: str, capabilities) -> None:
        with self._lock:
            self.db.executemany("INSERT OR IGNORE INTO grants VALUES (?, ?, ?)", [(principal, handle_id, c) for c in capabilities])

    def revoke(self, principal: str, handle_id: str) -> None:
        self._all("DELETE FROM grants WHERE principal = ? AND handle_id = ?", (principal, handle_id))

    def grants(self, principal: str, handle_id: str) -> set:
        return {r[0] for r in self._all("SELECT capability FROM grants WHERE principal = ? AND handle_id = ?", (principal, handle_id))}

    # ---- operation log ------------------------------------------------------------------------

    def log(self, **row) -> None:
        if "input" in row:
            row["input"] = json.dumps(row["input"])
        self._insert("operations", row)

    def history(self, result_handle: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM operations", ()
        if result_handle:
            q, args = q + " WHERE result_handle = ?", (result_handle,)
        return [dict(r) for r in self._all(q + " ORDER BY seq", args)]

    # ---- jobs ---------------------------------------------------------------------------------

    def add_job(self, **row) -> None:
        """Store a job in the state `created`. The job thread does not take it before `queue_job`."""
        self._insert("jobs", {**{k: json.dumps(v) if k in _JOB_JSON else v for k, v in row.items()},
                              "handle_id": "", "state": "created", "attempts": 0, "cancel": 0})

    def queue_job(self, job_id: str, handle_id: str) -> None:
        self._all("UPDATE jobs SET state = 'queued', handle_id = ? WHERE id = ? AND state = 'created'", (handle_id, job_id))

    def job(self, job_id: str) -> dict | None:
        rows = self._all("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return _job(rows[0]) if rows else None

    def jobs(self, state: str) -> list[dict]:
        return [_job(r) for r in self._all("SELECT * FROM jobs WHERE state = ? ORDER BY seq", (state,))]

    def start_job(self, at: str) -> dict | None:
        """Move the oldest queued job to `running` and return it. Each call counts as one attempt."""
        with self._lock:
            rows = self._all("SELECT id FROM jobs WHERE state = 'queued' ORDER BY seq LIMIT 1")
            if not rows:
                return None
            self._all("UPDATE jobs SET state = 'running', attempts = attempts + 1, started_at = ? WHERE id = ?", (at, rows[0][0]))
            return self.job(rows[0][0])

    def end_job(self, job_id: str, attempt: int, state: str, at: str, result: dict) -> str | None:
        """End one attempt of a running job. Return the state that the job gets, or None if this attempt is not the current one.

        A terminal state never changes. A job with a cancel mark ends as `cancelled` and keeps no result.
        """
        with self._lock:
            job = self.job(job_id)
            if job is None or job["state"] != "running" or job["attempts"] != attempt:
                return None
            if job["cancel"]:
                state, result = "cancelled", None
            self._all("UPDATE jobs SET state = ?, finished_at = ?, result = ? WHERE id = ?", (state, at, result and json.dumps(result), job_id))
            return state

    def requeue_job(self, job_id: str) -> None:
        """Put a job that a stopped runtime left in `running` back in the queue."""
        self._all("UPDATE jobs SET state = 'queued', started_at = NULL WHERE id = ? AND state = 'running'", (job_id,))

    def cancel_job(self, job_id: str, at: str) -> None:
        """A queued job ends now. A running job gets a mark, and its output is discarded when the adapter returns."""
        with self._lock:
            self._all("UPDATE jobs SET state = 'cancelled', finished_at = ? WHERE id = ? AND state = 'queued'", (at, job_id))
            self._all("UPDATE jobs SET cancel = 1 WHERE id = ? AND state = 'running'", (job_id,))

    # ---- graph nodes --------------------------------------------------------------------------

    def node_run(self, principal: str, key: str) -> dict | None:
        """The OperationResult of the last completed run of a graph node with this key, for this principal."""
        rows = self._all("SELECT result FROM node_runs WHERE principal = ? AND key = ?", (principal, key))
        return json.loads(rows[0][0]) if rows else None

    def put_node_run(self, principal: str, key: str, result: dict, at: str) -> None:
        self._all("INSERT OR REPLACE INTO node_runs VALUES (?, ?, ?, ?)", (principal, key, json.dumps(result), at))

    def add_approval(self, key: str, node: str, approver: str, at: str) -> None:
        self._insert("approvals", {"key": key, "node": node, "approver": approver, "at": at})

    def take_approval(self, key: str, operation_id: str, at: str) -> dict | None:
        """Use one unused approval for this key and return it. An approval allows one run."""
        with self._lock:
            rows = self._all("SELECT * FROM approvals WHERE key = ? AND used_at IS NULL ORDER BY seq LIMIT 1", (key,))
            if not rows:
                return None
            self._all("UPDATE approvals SET used_at = ?, used_by = ? WHERE seq = ?", (at, operation_id, rows[0]["seq"]))
            return dict(rows[0])

    def approvals(self, key: str) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM approvals WHERE key = ? ORDER BY seq", (key,))]

    # ---- workers ------------------------------------------------------------------------------

    def put_worker(self, worker_id: str, url: str, key: str, info: dict, at: str) -> None:
        self._all("INSERT OR REPLACE INTO workers VALUES (?, ?, ?, ?, ?)", (worker_id, url, key, json.dumps(info), at))

    def worker(self, worker_id: str) -> dict | None:
        rows = self._all("SELECT * FROM workers WHERE id = ?", (worker_id,))
        return {**dict(rows[0]), "info": json.loads(rows[0]["info"])} if rows else None
