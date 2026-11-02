"""Persistenter Betriebsstatus (SQLite). Keine Secrets; private Schlüssel liegen nur im Pending-Speicher."""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sqlite3
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
  resource_id TEXT PRIMARY KEY,
  namespace TEXT NOT NULL,
  service_id TEXT NOT NULL,
  stack_id TEXT NOT NULL,
  source_repo TEXT,
  npm_instance TEXT NOT NULL,
  domains TEXT NOT NULL,
  npm_host_id INTEGER,
  npm_cert_id INTEGER,
  infisical_cert_id TEXT,
  cert_fingerprint TEXT,
  cert_sans TEXT,
  cert_not_after TEXT,
  cert_issuer TEXT,
  policy_hash TEXT,
  target_commit TEXT,
  deployed_commit TEXT,
  reconciled_commit TEXT,
  manifest TEXT,
  last_result TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
  op_id TEXT PRIMARY KEY,
  resource_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  step TEXT NOT NULL,
  data TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
  resource_id TEXT NOT NULL,
  taken_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  npm_host TEXT NOT NULL
);
"""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class State:
    def __init__(self, state_dir: pathlib.Path):
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "pending").mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.dir / "pending", 0o700)
        self.db = sqlite3.connect(self.dir / "state.sqlite3", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # --- services -------------------------------------------------------------------------------
    def get(self, resource_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM services WHERE resource_id=?", (resource_id,)).fetchone()
        return dict(row) if row else None

    def all(self, namespace: str | None = None) -> list[dict]:
        if namespace:
            rows = self.db.execute("SELECT * FROM services WHERE namespace=? ORDER BY resource_id", (namespace,))
        else:
            rows = self.db.execute("SELECT * FROM services ORDER BY resource_id")
        return [dict(r) for r in rows]

    def upsert(self, resource_id: str, **fields) -> None:
        fields["updated_at"] = utcnow().isoformat()
        for k in ("domains", "cert_sans"):
            if k in fields and not isinstance(fields[k], str) and fields[k] is not None:
                fields[k] = json.dumps(list(fields[k]))
        if self.get(resource_id) is None:
            cols = ["resource_id", *fields]
            self.db.execute(f"INSERT INTO services ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [resource_id, *fields.values()])
        else:
            sets = ",".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE services SET {sets} WHERE resource_id=?", [*fields.values(), resource_id])

    def forget(self, resource_id: str) -> None:
        for op in self.db.execute("SELECT op_id FROM operations WHERE resource_id=?", (resource_id,)).fetchall():
            self.drop_pending_key(op["op_id"])
        self.db.execute("DELETE FROM operations WHERE resource_id=?", (resource_id,))
        self.db.execute("DELETE FROM services WHERE resource_id=?", (resource_id,))

    # --- operations (Wiederaufnahme unterbrochener Läufe) -----------------------------------------
    def open_op(self, resource_id: str, kind: str) -> dict | None:
        row = self.db.execute("SELECT * FROM operations WHERE resource_id=? AND kind=? AND status='open' "
                              "ORDER BY started_at DESC LIMIT 1", (resource_id, kind)).fetchone()
        if not row:
            return None
        op = dict(row)
        op["data"] = json.loads(op["data"])
        return op

    def start_op(self, resource_id: str, kind: str, data: dict | None = None) -> dict:
        now = utcnow().isoformat()
        op = {"op_id": uuid.uuid4().hex[:12], "resource_id": resource_id, "kind": kind, "step": "started",
              "data": data or {}, "status": "open", "started_at": now, "updated_at": now}
        self.db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
                        (op["op_id"], resource_id, kind, op["step"], json.dumps(op["data"]), "open", now, now))
        return op

    def step(self, op: dict, step: str, **data) -> None:
        op["step"] = step
        op["data"].update(data)
        self.db.execute("UPDATE operations SET step=?, data=?, updated_at=? WHERE op_id=?",
                        (step, json.dumps(op["data"]), utcnow().isoformat(), op["op_id"]))

    def close_op(self, op: dict, status: str) -> None:
        self.db.execute("UPDATE operations SET status=?, updated_at=? WHERE op_id=?",
                        (status, utcnow().isoformat(), op["op_id"]))
        self.drop_pending_key(op["op_id"])

    # --- Pending-Schlüssel ------------------------------------------------------------------------
    def _key_path(self, op_id: str) -> pathlib.Path:
        return self.dir / "pending" / f"{op_id}.key"

    def save_pending_key(self, op_id: str, key_pem: bytes) -> None:
        path = self._key_path(op_id)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key_pem)
            f.flush()
            os.fsync(f.fileno())

    def load_pending_key(self, op_id: str) -> bytes | None:
        path = self._key_path(op_id)
        return path.read_bytes() if path.exists() else None

    def drop_pending_key(self, op_id: str) -> None:
        self._key_path(op_id).unlink(missing_ok=True)

    # --- Snapshots vor Übernahme ------------------------------------------------------------------
    def snapshot(self, resource_id: str, reason: str, npm_host: dict) -> None:
        self.db.execute("INSERT INTO snapshots VALUES (?,?,?,?)",
                        (resource_id, utcnow().isoformat(), reason, json.dumps(npm_host, sort_keys=True)))
