"""Handy-Downloader – Weboberfläche + Hintergrund-Wächter.

Start:  python3 -m handy_downloader          (öffnet http://127.0.0.1:8765 im Browser)
        python3 -m handy_downloader --no-browser   (z. B. beim Autostart)
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

from .catalog import Catalog
from .importer import APP_DIR, ImportManager, load_config, save_config
from .media import is_video
from .sources import FolderSource, Source, detect_all

PORT = 8765
THUMB_DIR = APP_DIR / "thumbs"

app = Flask(__name__)
catalog = Catalog(APP_DIR / "katalog.sqlite")
manager = ImportManager(catalog)
_devices: dict[str, Source] = {}
_dev_lock = threading.Lock()
_watch_error = ""


def notify(title: str, text: str) -> None:
    if sys.platform == "darwin":
        t = text.replace('"', "'")
        subprocess.run(["osascript", "-e", f'display notification "{t}" with title "{title}"'], check=False)


def _gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e8 else f"{n / 1e6:.0f} MB"


def _import_finished(job) -> None:
    if job.kind == "delete":
        txt = f"{job.deleted} Dateien gelöscht, {_gb(job.freed)} frei"
        notify("Handy-Downloader", f"{job.source}: {txt}" + (f", {job.errors} nicht gelöscht" if job.errors else ""))
        return
    parts = [f"{job.imported} neu"]
    if job.duplicates:
        parts.append(f"{job.duplicates} schon vorhanden")
    if job.errors:
        parts.append(f"{job.errors} Fehler")
    notify("Handy-Downloader", f"{job.source}: " + ", ".join(parts))


def watcher() -> None:
    """Prüft alle paar Sekunden, ob ein Handy/Laufwerk angesteckt wurde."""
    global _watch_error
    while True:
        try:
            found = {s.id: s for s in detect_all()}
            _watch_error = ""
        except Exception as e:  # pragma: no cover
            found, _watch_error = {}, str(e)
        with _dev_lock:
            new = [s for sid, s in found.items() if sid not in _devices]
            _devices.clear()
            _devices.update(found)
        cfg = load_config()
        for s in new:
            if cfg.get("auto_import") and not manager.busy:
                notify("Handy-Downloader", f"{s.name} erkannt – Import startet …")
                manager.start(s, cfg, on_done=_import_finished)
            else:
                notify("Handy-Downloader", f"{s.name} erkannt")
        time.sleep(4)


# --------------------------------------------------------------------------- Seiten & API
@app.before_request
def only_local():
    """Schutz: nur lokale Aufrufe, und Aktionen nur per JSON (verhindert Fremd-Webseiten-Anfragen)."""
    host = (request.host or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost"):
        abort(403)
    if request.method == "POST" and not request.is_json:
        abort(415)


@app.get("/")
def index():
    return send_file(Path(__file__).parent / "templates" / "index.html")


@app.get("/api/status")
def status():
    with _dev_lock:
        devs = [s.as_dict() for s in _devices.values()]
    return jsonify(devices=devs, job=manager.job.as_dict(), busy=manager.busy,
                   config=load_config(), facets=catalog.facets(), watch_error=_watch_error,
                   tools={"pymobiledevice3": _has_module("pymobiledevice3"), "adb": bool(shutil.which("adb")),
                          "ffprobe": bool(shutil.which("ffprobe"))})


def _has_module(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


@app.post("/api/import")
def start_import():
    data = (request.get_json(silent=True) or {})
    cfg = load_config()
    if data.get("folder"):
        p = Path(data["folder"]).expanduser()
        if not p.is_dir():
            return jsonify(ok=False, error="Ordner nicht gefunden"), 400
        src: Source | None = FolderSource(p)
    else:
        with _dev_lock:
            src = _devices.get(data.get("source", ""))
    if not src:
        return jsonify(ok=False, error="Gerät nicht mehr verbunden"), 404
    ok = manager.start(src, cfg, on_done=_import_finished)
    return jsonify(ok=ok, error=None if ok else "Es läuft bereits ein Import")


@app.get("/api/phone-files")
def phone_files():
    """Alles, was auf dem Gerät liegt und schon geprüft auf dem Mac gesichert ist."""
    sid = request.args.get("source", "")
    rows = catalog.phone_files(sid)
    for r in rows:
        r["video"] = is_video(r["path"])
        r["backup_ok"] = Path(r["path"]).exists()
    return jsonify(items=rows)


@app.post("/api/phone-delete")
def phone_delete():
    data = request.get_json(silent=True) or {}
    with _dev_lock:
        src = _devices.get(data.get("source", ""))
    if not src:
        return jsonify(ok=False, error="Gerät nicht mehr verbunden"), 404
    rids = [int(x) for x in data.get("ids", [])]
    if not rids:
        return jsonify(ok=False, error="Nichts ausgewählt"), 400
    rows = catalog.phone_rows(src.id, rids)
    ok = manager.start_delete(src, rows, on_done=_import_finished)
    return jsonify(ok=ok, error=None if ok else "Es läuft gerade ein anderer Vorgang")


@app.post("/api/cancel")
def cancel():
    manager.cancel()
    return jsonify(ok=True)


@app.post("/api/config")
def set_config():
    cfg = load_config()
    data = (request.get_json(silent=True) or {})
    for k in ("target", "month_style", "split_by_type", "auto_import", "include"):
        if k in data:
            cfg[k] = data[k]
    save_config(cfg)
    return jsonify(ok=True, config=cfg)


@app.post("/api/choose-folder")
def choose_folder():
    """Öffnet auf dem Mac den Finder-Dialog „Ordner wählen“."""
    if sys.platform != "darwin":
        return jsonify(path=None)
    prompt = (request.get_json(silent=True) or {}).get("prompt", "Ordner wählen")
    res = subprocess.run(["osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")'],
                         capture_output=True, text=True)
    return jsonify(path=res.stdout.strip() or None)


@app.get("/api/media")
def media():
    a = request.args
    total, rows = catalog.search(category=a.get("type", ""), year=a.get("year", ""), month=a.get("month", ""),
                                 q=a.get("q", "").strip(), min_mp=float(a.get("min_mp") or 0),
                                 camera=a.get("camera", ""), device=a.get("device", ""),
                                 limit=int(a.get("limit", 120)), offset=int(a.get("offset", 0)))
    for r in rows:
        r["video"] = is_video(r["path"])
        r["exists"] = Path(r["path"]).exists()
    return jsonify(total=total, items=rows)


@app.post("/api/cleanup")
def cleanup():
    return jsonify(removed=catalog.forget_missing())


@app.post("/api/reveal/<int:mid>")
def reveal(mid: int):
    row = catalog.get(mid) or abort(404)
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", row["path"]], check=False)
    return jsonify(ok=True)


@app.post("/api/open/<int:mid>")
def open_file(mid: int):
    row = catalog.get(mid) or abort(404)
    if sys.platform == "darwin":
        subprocess.run(["open", row["path"]], check=False)
    return jsonify(ok=True)


@app.get("/file/<int:mid>")
def file(mid: int):
    row = catalog.get(mid) or abort(404)
    p = Path(row["path"])
    if not p.exists():
        abort(404)
    return send_file(p, conditional=True)


@app.get("/thumb/<int:mid>")
def thumb(mid: int):
    row = catalog.get(mid) or abort(404)
    src = Path(row["path"])
    if not src.exists():
        abort(404)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    out = THUMB_DIR / (hashlib.sha1(str(src).encode()).hexdigest() + ".jpg")
    if not out.exists() and not make_thumb(src, out):
        abort(404)
    return send_file(out, max_age=86400)


def make_thumb(src: Path, out: Path, size: int = 360) -> bool:
    if not is_video(src.name):
        try:
            from PIL import Image, ImageOps
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((size, size))
                im.convert("RGB").save(out, "JPEG", quality=80)
            return True
        except Exception:
            pass
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg and is_video(src.name):
        subprocess.run([ffmpeg, "-y", "-loglevel", "quiet", "-ss", "1", "-i", str(src), "-frames:v", "1",
                        "-vf", f"scale={size}:-2", str(out)], timeout=30)
        if out.exists():
            return True
    if shutil.which("qlmanage"):  # macOS Quick Look kann HEIC und Videos
        tmpdir = out.parent / "ql"
        tmpdir.mkdir(exist_ok=True)
        subprocess.run(["qlmanage", "-t", "-s", str(size), "-o", str(tmpdir), str(src)],
                       capture_output=True, timeout=30)
        produced = tmpdir / (src.name + ".png")
        if produced.exists():
            try:
                from PIL import Image
                with Image.open(produced) as im:
                    im.convert("RGB").save(out, "JPEG", quality=80)
            except Exception:
                shutil.move(str(produced), out)
            produced.unlink(missing_ok=True)
            return out.exists()
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Handy-Downloader")
    ap.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch öffnen")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    threading.Thread(target=watcher, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}"
    print(f"Handy-Downloader läuft: {url}  (Beenden mit Ctrl+C)")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
