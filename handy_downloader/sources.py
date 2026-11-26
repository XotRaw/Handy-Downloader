"""Quellen: angeschlossene Handys und Speicher.

* iPhone/iPad   – über USB mit `pymobiledevice3` (Zugriff auf /DCIM, wie „Digitale Bilder“)
* Android       – über `adb` (USB-Debugging muss am Handy eingeschaltet sein)
* Laufwerke     – alles unter /Volumes mit einem DCIM-Ordner (SD-Karten, Kameras, manche Android-Handys)
* Ordner        – beliebiger Ordner auf dem Mac (z. B. ein AirDrop- oder Export-Ordner)
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .media import is_media


@dataclass
class RemoteFile:
    path: str      # Pfad auf der Quelle
    name: str
    size: int
    mtime: float   # Unix-Zeit


class Source:
    id: str
    name: str
    kind: str      # iphone | android | volume | folder

    def list_files(self) -> Iterator[RemoteFile]:
        raise NotImplementedError

    def fetch(self, rf: RemoteFile, dest: Path) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Verbindung zur Quelle schließen (falls nötig)."""

    def delete(self, files: list[RemoteFile]) -> dict[str, str | None]:
        """Dateien auf der Quelle löschen. Ergebnis: Pfad → None (ok) oder Fehlertext."""
        return {rf.path: "Löschen wird bei dieser Quelle nicht unterstützt" for rf in files}

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind}


# --------------------------------------------------------------------------- Ordner / Laufwerk
class FolderSource(Source):
    def __init__(self, path: Path, kind: str = "folder", name: str | None = None):
        self.root = Path(path).expanduser()
        self.kind = kind
        self.id = f"{kind}:{self.root}"
        self.name = name or self.root.name or str(self.root)

    def list_files(self) -> Iterator[RemoteFile]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith(".") or not is_media(fn):
                    continue
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                yield RemoteFile(str(p), fn, st.st_size, st.st_mtime)

    def fetch(self, rf: RemoteFile, dest: Path) -> None:
        shutil.copy2(rf.path, dest)

    def delete(self, files: list[RemoteFile]) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for rf in files:
            try:
                os.remove(rf.path)
                out[rf.path] = None
            except FileNotFoundError:
                out[rf.path] = "nicht gefunden"
            except OSError as e:
                out[rf.path] = str(e)
        return out


def detect_volumes() -> list[Source]:
    vols = Path("/Volumes")
    out: list[Source] = []
    if not vols.is_dir():
        return out
    for v in vols.iterdir():
        dcim = v / "DCIM"
        try:
            if v.is_dir() and not v.is_symlink() and dcim.is_dir():
                out.append(FolderSource(dcim, kind="volume", name=f"{v.name} (Laufwerk)"))
        except OSError:
            continue
    return out


# --------------------------------------------------------------------------- Android (adb)
ANDROID_DIRS = ["/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Download", "/sdcard/Movies",
                "/sdcard/Android/media/com.whatsapp/WhatsApp/Media"]


class AndroidSource(Source):
    kind = "android"

    def __init__(self, adb: str, serial: str, model: str):
        self.adb, self.serial = adb, serial
        self.id = f"android:{serial}"
        self.name = model or f"Android {serial}"

    def _run(self, *args: str, timeout: int = 120) -> str:
        res = subprocess.run([self.adb, "-s", self.serial, *args], capture_output=True, text=True, timeout=timeout)
        return res.stdout

    def list_files(self) -> Iterator[RemoteFile]:
        dirs = " ".join(f"'{d}'" for d in ANDROID_DIRS)
        # Ausgabe: <mtime> <größe> <pfad>
        out = self._run("shell", f"find {dirs} -type f ! -path '*/.*' -exec stat -c '%Y %s %n' {{}} + 2>/dev/null",
                        timeout=600)
        seen = set()
        for line in out.splitlines():
            parts = line.strip().split(" ", 2)
            if len(parts) != 3 or not parts[0].isdigit():
                continue
            path = parts[2]
            if path in seen or not is_media(path):
                continue
            seen.add(path)
            yield RemoteFile(path, path.rsplit("/", 1)[-1], int(parts[1]), float(parts[0]))

    def fetch(self, rf: RemoteFile, dest: Path) -> None:
        res = subprocess.run([self.adb, "-s", self.serial, "pull", "-a", rf.path, str(dest)],
                             capture_output=True, text=True, timeout=1800)
        if res.returncode != 0 or not dest.exists():
            raise IOError(res.stderr.strip() or "adb pull fehlgeschlagen")

    def delete(self, files: list[RemoteFile]) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for rf in files:
            esc = rf.path.replace("'", "'\\''")
            q, uri = f"'{esc}'", f"'file://{esc}'"
            # löschen + Galerie benachrichtigen, dann prüfen, ob die Datei wirklich weg ist
            cmd = (f"if [ ! -e {q} ]; then echo MISSING; else rm -f {q}; "
                   f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d {uri} >/dev/null 2>&1; "
                   f"if [ -e {q} ]; then echo FAIL; else echo OK; fi; fi")
            try:
                res = self._run("shell", cmd, timeout=60).strip()
            except Exception as e:
                out[rf.path] = str(e)
                continue
            out[rf.path] = None if res.endswith("OK") else "nicht gefunden" if "MISSING" in res else "Löschen fehlgeschlagen"
        return out


def _find_adb() -> str | None:
    cand = shutil.which("adb")
    if cand:
        return cand
    for p in ("/opt/homebrew/bin/adb", "/usr/local/bin/adb", str(Path.home() / "Library/Android/sdk/platform-tools/adb")):
        if Path(p).exists():
            return p
    return None


def detect_android() -> list[Source]:
    adb = _find_adb()
    if not adb:
        return []
    try:
        out = subprocess.run([adb, "devices", "-l"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    found: list[Source] = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            model = next((p.split(":", 1)[1].replace("_", " ") for p in parts if p.startswith("model:")), "")
            found.append(AndroidSource(adb, parts[0], model))
    return found


# --------------------------------------------------------------------------- iPhone (pymobiledevice3)
# Neuere pymobiledevice3-Versionen (ab ca. 5.x) sind asynchron, ältere synchron. Damit Verbindungen
# gültig bleiben, laufen alle iPhone-Aufrufe in EINER dauerhaft laufenden Ereignisschleife.
_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="iphone-loop", daemon=True).start()
        return _loop


def _sync(value, timeout: float = 600):
    """Ergebnis holen – egal ob die Bibliothek synchron oder asynchron antwortet."""
    if inspect.isawaitable(value):
        async def runner():
            return await value
        return asyncio.run_coroutine_threadsafe(runner(), _get_loop()).result(timeout)
    return value


def _call(fn, *args, **kwargs):
    """Funktion IN der Ereignisschleife aufrufen (manche Objekte müssen dort erzeugt werden)."""
    async def runner():
        res = fn(*args, **kwargs)
        return await res if inspect.isawaitable(res) else res
    return asyncio.run_coroutine_threadsafe(runner(), _get_loop()).result(600)


class IPhoneSource(Source):
    kind = "iphone"

    def __init__(self, udid: str, name: str):
        self.udid = udid
        self.id = f"iphone:{udid}"
        self.name = name or "iPhone"
        self._afc = None
        self._lockdown = None

    def _service(self):
        if self._afc is None:
            from pymobiledevice3.lockdown import create_using_usbmux  # type: ignore
            from pymobiledevice3.services.afc import AfcService  # type: ignore

            self._lockdown = _call(create_using_usbmux, serial=self.udid)
            afc = _call(AfcService, lockdown=self._lockdown)
            if hasattr(afc, "__aenter__"):  # neue Versionen: Verbindung ausdrücklich öffnen
                afc = _sync(afc.__aenter__()) or afc
            self._afc = afc
        return self._afc

    def close(self) -> None:
        afc, ld, self._afc, self._lockdown = self._afc, self._lockdown, None, None
        for obj, meth in ((afc, "__aexit__"), (afc, "close"), (ld, "close")):
            if obj is None or not hasattr(obj, meth):
                continue
            try:
                _sync(getattr(obj, meth)(None, None, None) if meth == "__aexit__" else getattr(obj, meth)(), 15)
                if meth == "__aexit__":
                    break  # schließt bereits alles von AFC
            except Exception:
                pass

    def list_files(self) -> Iterator[RemoteFile]:
        afc = self._service()
        stack = ["/DCIM"]
        while stack:
            d = stack.pop()
            try:
                entries = _sync(afc.listdir(d))
            except Exception:
                continue
            for e in sorted(entries):
                if e in (".", "..") or e.startswith("."):
                    continue
                p = f"{d}/{e}"
                try:
                    st = _sync(afc.stat(p))
                except Exception:
                    continue
                if st.get("st_ifmt") == "S_IFDIR":
                    stack.append(p)
                elif is_media(e):
                    mt = st.get("st_mtime")
                    mtime = mt.timestamp() if isinstance(mt, datetime) else float(mt or 0) / (1e9 if (mt or 0) > 1e12 else 1)
                    yield RemoteFile(p, e, int(st.get("st_size", 0)), mtime)

    def fetch(self, rf: RemoteFile, dest: Path) -> None:
        afc = self._service()
        # Stückweise lesen, damit auch große Videos nicht komplett im Speicher landen
        if hasattr(afc, "fopen") and hasattr(afc, "fread"):
            fh = _sync(afc.fopen(rf.path, "r"))
            try:
                with open(dest, "wb") as out:
                    while True:
                        chunk = _sync(afc.fread(fh, 4 * 1024 * 1024))
                        if not chunk:
                            break
                        out.write(chunk)
            finally:
                _sync(afc.fclose(fh))
        else:
            dest.write_bytes(_sync(afc.get_file_contents(rf.path)))
        if dest.stat().st_size != rf.size and rf.size:
            raise IOError(f"Unvollständig übertragen: {rf.name}")
        os.utime(dest, (rf.mtime, rf.mtime))

    def delete(self, files: list[RemoteFile]) -> dict[str, str | None]:
        """Löschen über Apples ImageCaptureCore (wie „Digitale Bilder“).

        Nicht über den direkten Dateizugriff, sonst bleiben in der Fotos-App des iPhones leere Einträge.
        Läuft in einem eigenen Prozess, weil ImageCaptureCore eine eigene Ereignisschleife braucht.
        """
        if sys.platform != "darwin":
            return {rf.path: "Löschen vom iPhone geht nur auf dem Mac" for rf in files}
        req = {"udid": self.udid, "name": self.name,
               "files": [{"key": rf.path, "folder": rf.path.rsplit("/", 2)[-2], "name": rf.name, "size": rf.size}
                         for rf in files]}
        try:
            res = subprocess.run([sys.executable, "-m", "handy_downloader.icc_delete"], input=json.dumps(req),
                                 capture_output=True, text=True, timeout=1800)
            data = json.loads(res.stdout.strip().splitlines()[-1])
        except Exception as e:
            return {rf.path: f"iPhone-Löschen fehlgeschlagen: {e}" for rf in files}
        if data.get("error"):
            return {rf.path: data["error"] for rf in files}
        return {rf.path: data["results"].get(rf.path, "nicht bearbeitet") for rf in files}



_iphone_names: dict[str, str] = {}


def detect_iphones() -> list[Source]:
    try:
        from pymobiledevice3.usbmux import list_devices  # type: ignore
        from pymobiledevice3.lockdown import create_using_usbmux  # type: ignore
    except Exception:
        return []
    out: list[Source] = []
    try:
        devices = _call(list_devices)
    except Exception:
        return []
    for dev in devices:
        if str(getattr(dev, "connection_type", "USB")).upper() != "USB":
            continue
        name = _iphone_names.get(dev.serial)
        if name is None:  # Namen nur einmal pro Gerät abfragen, nicht alle 4 Sekunden
            try:
                ld = _call(create_using_usbmux, serial=dev.serial, autopair=False)
                info = ld.short_info if isinstance(getattr(ld, "short_info", None), dict) else {}
                name = info.get("DeviceName") or ld.display_name or "iPhone"
                _iphone_names[dev.serial] = name
                if hasattr(ld, "close"):
                    _sync(ld.close(), 10)
            except Exception:
                name = "iPhone"  # noch nicht vertraut – beim Import fragt das iPhone „Vertrauen?“
        out.append(IPhoneSource(dev.serial, name))
    return out


def detect_all() -> list[Source]:
    return detect_iphones() + detect_android() + detect_volumes()
