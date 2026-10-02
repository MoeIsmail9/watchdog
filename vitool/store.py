import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from contextlib import contextmanager

from .settings import DEFAULTS, validate


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "vitool.sqlite3"
        self.database_url = os.getenv("TURSO_DATABASE_URL", "").strip()
        self.auth_token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
        if bool(self.database_url) != bool(self.auth_token):
            raise ValueError("Set both TURSO_DATABASE_URL and TURSO_AUTH_TOKEN")
        # One shared Turso connection. A new connection per query made a single scan
        # open hundreds of connections, and Turso started refusing them.
        self.remote = None
        self.remote_lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, first_seen REAL NOT NULL,
                    matched INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
                    delivered INTEGER NOT NULL DEFAULT 0, baseline INTEGER NOT NULL DEFAULT 0
                );
            """)
        if not self.database_url:
            self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        if self.database_url:
            with self.remote_lock:
                if self.remote is None:
                    import libsql
                    self.remote = libsql.connect(self.database_url, auth_token=self.auth_token)
                try:
                    with self.remote:
                        yield self.remote
                except Exception:
                    # Drop a broken connection so the next query reconnects.
                    remote, self.remote = self.remote, None
                    try:
                        remote.close()
                    except Exception:
                        pass
                    raise
            return
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def patch(self, key, changes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            value = json.loads(row[0]) if row else {}
            value.update(changes)
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def get_many(self, defaults):
        """Read several state keys in one query; missing keys get their default."""
        keys = list(defaults)
        with self.connect() as db:
            rows = db.execute(f"SELECT key, value FROM state WHERE key IN ({','.join('?' * len(keys))})",
                              tuple(keys)).fetchall()
        found = {key: json.loads(value) for key, value in rows}
        return {key: found.get(key, default) for key, default in defaults.items()}

    def set(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def settings(self):
        return validate(self.get("settings", DEFAULTS))

    def save_settings(self, value):
        result = validate(value)
        self.set("settings", result)
        return result

    def item(self, item_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        columns = ("id", "data", "first_seen", "matched", "reason", "delivered", "baseline")
        return dict(zip(columns, row)) if row else None

    def save_item(self, item, matched, reason, baseline=False):
        with self.connect() as db:
            db.execute("""INSERT INTO items (id,data,first_seen,matched,reason,baseline) VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET data=excluded.data, matched=excluded.matched, reason=excluded.reason""",
                (item["id"], json.dumps(item), time.time(), int(matched), reason, int(baseline)))

    def delivered(self, item_id):
        with self.connect() as db:
            db.execute("UPDATE items SET delivered=1 WHERE id=?", (item_id,))

    def matches(self, pending=False):
        query = "SELECT * FROM items WHERE matched=1"
        if pending:
            query += " AND delivered=0 AND baseline=0"
        query += " ORDER BY first_seen DESC LIMIT 100"
        with self.connect() as db:
            rows = db.execute(query).fetchall()
        columns = ("id", "data", "first_seen", "matched", "reason", "delivered", "baseline")
        records = [dict(zip(columns, row)) for row in rows]
        return [{**json.loads(row["data"]), **{k: row[k] for k in ["first_seen", "reason", "delivered", "baseline"]}} for row in records]
