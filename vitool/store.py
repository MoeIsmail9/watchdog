import json
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

from .settings import DEFAULTS, validate


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "vitool.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, first_seen REAL NOT NULL,
                    matched INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
                    delivered INTEGER NOT NULL DEFAULT 0, baseline INTEGER NOT NULL DEFAULT 0
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
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
        return dict(row) if row else None

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
        return [{**json.loads(row["data"]), **{k: row[k] for k in ["first_seen", "reason", "delivered", "baseline"]}} for row in rows]
