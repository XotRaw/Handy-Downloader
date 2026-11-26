"""Import-Ablauf: holen → prüfen (Duplikat?) → einordnen → in Jahr/Monat/Typ-Ordner verschieben."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .catalog import Catalog
from .media import QUICK_LABELS, analyze, quick_category, target_dir
from .sources import RemoteFile, Source

APP_DIR = Path(os.environ.get("HANDY_DOWNLOADER_HOME", Path.home() / "Library/Application Support/HandyDownloader"))
CONFIG_FILE = APP_DIR / "config.json"

DEFAULT_CONFIG = {
    "target": str(Path.home() / "Pictures" / "Handy-Import"),
    "month_style": "MM.YY",       # 01.25 | M.YY → 1.25 | "MM Monat" → 01 Januar
    "split_by_type": True,        # Unterordner Fotos / Videos / Screenshots / Extern
    "auto_import": True,          # beim Anstecken automatisch importieren
    "include": ["kamera", "extern", "screenshot", "video"],
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 1
    while True:
        cand = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not cand.exists():
            return cand
        i += 1


@dataclass
class Job:
    source: str = ""
    kind: str = "import"          # import | delete
    state: str = "idle"           # idle | scanning | importing | verifying | deleting | done | error
    total: int = 0
    done: int = 0
    imported: int = 0
    skipped: int = 0
    duplicates: int = 0
    errors: int = 0
    deleted: int = 0
    freed: int = 0                # Bytes, die auf dem Handy frei geworden sind
    current: str = ""
    message: str = ""
    by_category: dict = field(default_factory=dict)
    only: list = field(default_factory=list)   # leer = alles importieren
    scan: dict = field(default_factory=dict)   # Übersicht: Typ → Anzahl/Größe (gesamt + neu)
    started: float = 0.0
    cancel: bool = False

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("cancel")
        return d


def summarize(source: Source, files: list, catalog: Catalog) -> dict:
    """Je Typ: wie viele Dateien/Bytes liegen auf dem Gerät, und wie viele davon fehlen noch auf dem Mac."""
    out = {k: {"n": 0, "bytes": 0, "new_n": 0, "new_bytes": 0} for k in QUICK_LABELS}
    for rf in files:
        c = out[quick_category(rf.path)]
        c["n"] += 1
        c["bytes"] += rf.size or 0
        if not catalog.was_seen(source.id, rf.path, rf.size):
            c["new_n"] += 1
            c["new_bytes"] += rf.size or 0
    return out


def run_scan(source: Source, catalog: Catalog, job: Job) -> Job:
    """Nur nachsehen, was auf dem Gerät liegt – nichts herunterladen."""
    job.__dict__.update(Job(source=source.name, kind="scan", state="scanning", started=time.time()).__dict__)
    try:
        files = []
        for rf in source.list_files():
            files.append(rf)
            job.done += 1
            if job.cancel:
                break
        job.scan = summarize(source, files, catalog)
        job.state = "done"
    except Exception as e:
        job.state, job.message = "error", str(e)
    finally:
        try:
            source.close()
        except Exception:
            pass
    return job


def run_import(source: Source, cfg: dict, catalog: Catalog, job: Job, only: list | None = None) -> Job:
    root = Path(cfg["target"]).expanduser()
    staging = root / ".handy-tmp"
    staging.mkdir(parents=True, exist_ok=True)
    only = [o for o in (only or []) if o in QUICK_LABELS]
    job.__dict__.update(Job(source=source.name, state="scanning", started=time.time(), only=only).__dict__)
    all_files: list = []
    try:
        all_files = list(source.list_files())
        files = [rf for rf in all_files
                 if (not only or quick_category(rf.path) in only)
                 and not catalog.was_seen(source.id, rf.path, rf.size)]
        job.total, job.state = len(files), "importing"
        for rf in files:
            if job.cancel:
                job.message = "Abgebrochen"
                break
            job.current = rf.name
            tmp = staging / f"{time.time_ns()}_{rf.name}"
            try:
                source.fetch(rf, tmp)
                digest = sha1_of(tmp)
                if catalog.has_hash(digest):
                    job.duplicates += 1
                    tmp.unlink(missing_ok=True)
                    catalog.mark_seen(source.id, rf.path, rf.size, rf.mtime, digest)
                    continue
                info = analyze(tmp, fallback_mtime=rf.mtime, original_name=rf.name)
                if info.category not in cfg.get("include", DEFAULT_CONFIG["include"]):
                    job.skipped += 1
                    tmp.unlink(missing_ok=True)
                    continue
                dest_dir = target_dir(root, info, cfg["month_style"], cfg["split_by_type"])
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = unique_path(dest_dir / rf.name)
                shutil.move(str(tmp), dest)
                ts = info.taken_at.timestamp()
                os.utime(dest, (ts, ts))
                catalog.add(sha1=digest, path=str(dest), orig_name=rf.name, device=source.name,
                            category=info.category, taken_at=info.taken_at.isoformat(timespec="seconds"),
                            year=info.taken_at.year, month=info.taken_at.month, width=info.width,
                            height=info.height, megapixel=info.megapixel, camera=info.camera,
                            size=dest.stat().st_size, date_source=info.date_source)
                catalog.mark_seen(source.id, rf.path, rf.size, rf.mtime, digest)
                job.imported += 1
                job.by_category[info.category] = job.by_category.get(info.category, 0) + 1
            except Exception as e:  # einzelne Datei kaputt → weitermachen
                job.errors += 1
                job.message = f"{rf.name}: {e}"
                tmp.unlink(missing_ok=True)
            finally:
                job.done += 1
        job.state = "done"
        job.current = ""
    except Exception as e:
        job.state, job.message = "error", str(e)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if all_files:
            job.scan = summarize(source, all_files, catalog)  # aktualisierte Übersicht nach dem Import
        try:
            source.close()
        except Exception:
            pass
    return job


def run_delete(source: Source, rows: list[dict], catalog: Catalog, job: Job) -> Job:
    """Dateien vom Handy löschen – aber nur, wenn die Kopie auf dem Mac nachweislich identisch ist."""
    job.__dict__.update(Job(source=source.name, kind="delete", state="verifying",
                            total=len(rows), started=time.time()).__dict__)
    try:
        safe: list[tuple[dict, RemoteFile]] = []
        for r in rows:  # 1. Sicherung prüfen
            if job.cancel:
                job.message = "Abgebrochen – nichts gelöscht"
                job.state = "done"
                return job
            job.current = Path(r["remote_path"]).name
            local = Path(r["path"])
            if not r.get("sha1") or not local.exists() or sha1_of(local) != r["sha1"]:
                job.errors += 1
                job.message = f"{job.current}: Kopie auf dem Mac fehlt oder ist verändert – bleibt auf dem Handy"
            else:
                safe.append((r, RemoteFile(r["remote_path"], job.current, r["remote_size"], r["mtime"])))
            job.done += 1
        if job.cancel:
            job.message, job.state = "Abgebrochen – nichts gelöscht", "done"
            return job
        # 2. Löschen
        job.state, job.done, job.current = "deleting", 0, ""
        results = source.delete([rf for _, rf in safe])
        ok_rids = []
        for r, rf in safe:
            err = results.get(rf.path, "nicht bearbeitet")
            if err is None or err == "nicht gefunden":   # gelöscht oder schon vorher weg
                ok_rids.append(r["rid"])
                if err is None:
                    job.deleted += 1
                    job.freed += rf.size or 0
            else:
                job.errors += 1
                job.message = f"{rf.name}: {err}"
            job.done += 1
        catalog.mark_deleted(ok_rids)
        job.state = "done"
    except Exception as e:
        job.state, job.message = "error", str(e)
    return job


class ImportManager:
    """Hält genau einen laufenden Import und startet ihn im Hintergrund."""

    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.job = Job()
        self.scans: dict[str, dict] = {}   # Geräte-ID → letzte Übersicht
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _remember_scan(self, source: Source) -> None:
        if self.job.scan:
            self.scans[source.id] = {"cats": self.job.scan, "at": time.time()}

    def start(self, source: Source, cfg: dict, on_done=None, only: list | None = None) -> bool:
        if self.busy:
            return False

        def work():
            run_import(source, cfg, self.catalog, self.job, only=only)
            self._remember_scan(source)
            if on_done:
                on_done(self.job)

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        return True

    def start_scan(self, source: Source) -> bool:
        if self.busy:
            return False

        def work():
            run_scan(source, self.catalog, self.job)
            self._remember_scan(source)

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        return True

    def start_delete(self, source: Source, rows: list[dict], on_done=None) -> bool:
        if self.busy:
            return False

        def work():
            run_delete(source, rows, self.catalog, self.job)
            self.scans.pop(source.id, None)  # Übersicht ist veraltet – neu prüfen
            if on_done:
                on_done(self.job)

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        self.job.cancel = True
