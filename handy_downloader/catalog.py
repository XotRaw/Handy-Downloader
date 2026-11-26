"""SQLite-Katalog aller importierten Dateien (für Suche, Filter und Duplikat-Erkennung)."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY,
    sha1        TEXT UNIQUE,
    path        TEXT NOT NULL,
    orig_name   TEXT,
    device      TEXT,
    category    TEXT,
    taken_at    TEXT,
    year        INTEGER,
    month       INTEGER,
    width       INTEGER,
    height      INTEGER,
    megapixel   REAL,
    camera      TEXT,
    size        INTEGER,
    date_source TEXT,
    imported_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS media_cat ON media(category);
CREATE INDEX IF NOT EXISTS media_date ON media(taken_at);
CREATE TABLE IF NOT EXISTS seen (
    device TEXT, remote_path TEXT, size INTEGER, mtime REAL,
    PRIMARY KEY (device, remote_path, size)
);
"""
# Spalten, die nach Version 1.0 dazukamen (für „Vom Handy löschen“)
MIGRATIONS = [
    "ALTER TABLE seen ADD COLUMN sha1 TEXT",
    "ALTER TABLE seen ADD COLUMN deleted_at TEXT",
]


class Catalog:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                self.db.execute(stmt)
            except sqlite3.OperationalError:
                pass  # Spalte gibt es schon

    # ---- Import-Hilfen
    def was_seen(self, device: str, remote_path: str, size: int) -> bool:
        with self._lock:
            return self.db.execute("SELECT 1 FROM seen WHERE device=? AND remote_path=? AND size=?",
                                   (device, remote_path, size)).fetchone() is not None

    def mark_seen(self, device: str, remote_path: str, size: int, mtime: float, sha1: str | None = None) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO seen (device, remote_path, size, mtime, sha1, deleted_at) "
                            "VALUES (?,?,?,?,?,NULL)", (device, remote_path, size, mtime, sha1))

    # ---- Handy aufräumen
    def phone_files(self, device: str) -> list[dict]:
        """Dateien, die noch auf dem Gerät liegen UND schon sicher auf dem Mac sind."""
        with self._lock:
            rows = self.db.execute(
                """SELECT s.rowid AS rid, s.remote_path, s.size AS remote_size, s.mtime, s.sha1,
                          m.id, m.path, m.category, m.taken_at, m.orig_name, m.megapixel
                   FROM seen s JOIN media m ON m.sha1 = s.sha1
                   WHERE s.device = ? AND s.deleted_at IS NULL
                   ORDER BY m.taken_at""", (device,)).fetchall()
        return [dict(r) for r in rows]

    def phone_rows(self, device: str, rids: list[int]) -> list[dict]:
        q = ",".join("?" * len(rids))
        with self._lock:
            rows = self.db.execute(
                f"""SELECT s.rowid AS rid, s.remote_path, s.size AS remote_size, s.mtime, s.sha1, m.path
                    FROM seen s JOIN media m ON m.sha1 = s.sha1
                    WHERE s.device = ? AND s.deleted_at IS NULL AND s.rowid IN ({q})""",
                (device, *rids)).fetchall()
        return [dict(r) for r in rows]

    def mark_deleted(self, rids: list[int]) -> None:
        with self._lock, self.db:
            self.db.executemany("UPDATE seen SET deleted_at = datetime('now','localtime') WHERE rowid = ?",
                                [(r,) for r in rids])

    def has_hash(self, sha1: str) -> bool:
        with self._lock:
            row = self.db.execute("SELECT path FROM media WHERE sha1=?", (sha1,)).fetchone()
        return row is not None and Path(row["path"]).exists()

    def add(self, **rec) -> int:
        cols = ", ".join(rec)
        q = ", ".join("?" for _ in rec)
        with self._lock, self.db:
            cur = self.db.execute(f"INSERT OR REPLACE INTO media ({cols}) VALUES ({q})", tuple(rec.values()))
            return cur.lastrowid

    def get(self, media_id: int):
        with self._lock:
            return self.db.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()

    # ---- Suche
    def search(self, category: str = "", year: str = "", month: str = "", q: str = "", min_mp: float = 0,
               camera: str = "", device: str = "", limit: int = 200, offset: int = 0) -> tuple[int, list[dict]]:
        where, args = [], []
        if category:
            cats = category.split(",")
            where.append(f"category IN ({','.join('?' * len(cats))})")
            args += cats
        if year:
            where.append("year=?"); args.append(int(year))
        if month:
            where.append("month=?"); args.append(int(month))
        if min_mp:
            where.append("megapixel>=?"); args.append(float(min_mp))
        if camera:
            where.append("camera=?"); args.append(camera)
        if device:
            where.append("device=?"); args.append(device)
        if q:
            where.append("(orig_name LIKE ? OR path LIKE ? OR camera LIKE ?)")
            args += [f"%{q}%"] * 3
        w = ("WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self.db.execute(f"SELECT COUNT(*) FROM media {w}", args).fetchone()[0]
            rows = self.db.execute(f"SELECT * FROM media {w} ORDER BY taken_at DESC LIMIT ? OFFSET ?",
                                   (*args, limit, offset)).fetchall()
        return total, [dict(r) for r in rows]

    def facets(self) -> dict:
        with self._lock:
            years = [dict(r) for r in self.db.execute(
                "SELECT year, COUNT(*) n FROM media GROUP BY year ORDER BY year DESC")]
            rows = self.db.execute("SELECT category, COUNT(*) n, COALESCE(SUM(size),0) b FROM media GROUP BY category").fetchall()
            cats = {r["category"]: r["n"] for r in rows}
            sizes = {r["category"]: r["b"] for r in rows}
            cams = [r["camera"] for r in self.db.execute(
                "SELECT camera, COUNT(*) n FROM media WHERE camera IS NOT NULL GROUP BY camera ORDER BY n DESC")]
            devs = [r["device"] for r in self.db.execute(
                "SELECT DISTINCT device FROM media WHERE device IS NOT NULL ORDER BY device")]
        return {"years": years, "categories": cats, "sizes": sizes, "cameras": cams, "devices": devs}

    def forget_missing(self) -> int:
        """Einträge entfernen, deren Datei im Finder gelöscht wurde."""
        with self._lock:
            rows = self.db.execute("SELECT id, path FROM media").fetchall()
        gone = [r["id"] for r in rows if not Path(r["path"]).exists()]
        if gone:
            with self._lock, self.db:
                self.db.executemany("DELETE FROM media WHERE id=?", [(i,) for i in gone])
        return len(gone)
