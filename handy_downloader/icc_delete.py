"""Fotos/Videos auf dem iPhone über Apples ImageCaptureCore löschen (wie im Programm „Digitale Bilder“).

Wird als eigener Prozess gestartet:  python -m handy_downloader.icc_delete  < anfrage.json
Anfrage:  {"udid": "...", "name": "...", "files": [{"key", "folder", "name", "size"}, ...]}
Antwort (letzte Zeile auf stdout):  {"results": {key: null | "Fehlertext"}, "error": null | "..."}
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

VIDEO = {".mov", ".mp4", ".m4v"}


def _spin(seconds: float = 0.1) -> None:
    from Foundation import NSDate, NSRunLoop
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))


def _wait(cond, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        _spin()
    return cond()


def main() -> None:
    req = json.loads(sys.stdin.read())
    try:
        result = delete(req)
    except Exception as e:  # pragma: no cover - nur auf dem Mac
        result = {"results": {}, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(result))


def delete(req: dict) -> dict:
    try:
        import objc
        from Foundation import NSObject
        import ImageCaptureCore as ICC
    except ImportError:
        return {"results": {}, "error": "Für das Löschen vom iPhone fehlt 'pyobjc-framework-ImageCaptureCore' "
                                         "(./install_mac.sh erneut ausführen)"}

    class Delegate(NSObject):
        def init(self):
            self = objc.super(Delegate, self).init()
            self.devices, self.ready, self.session_error, self.opened = [], False, None, False
            return self

        # ICDeviceBrowserDelegate
        def deviceBrowser_didAddDevice_moreComing_(self, browser, device, more):
            self.devices.append(device)

        def deviceBrowser_didRemoveDevice_moreGoing_(self, browser, device, more):
            pass

        # ICDeviceDelegate
        def device_didOpenSessionWithError_(self, device, error):
            self.opened, self.session_error = True, error

        def device_didCloseSessionWithError_(self, device, error):
            pass

        def didRemoveDevice_(self, device):
            pass

        # ICCameraDeviceDelegate
        def deviceDidBecomeReadyWithCompleteContentCatalog_(self, device):
            self.ready = True

        def cameraDevice_didAddItems_(self, camera, items): pass
        def cameraDevice_didRemoveItems_(self, camera, items): pass
        def cameraDevice_didRenameItems_(self, camera, items): pass
        def cameraDeviceDidChangeCapability_(self, camera): pass
        def cameraDevice_didReceiveThumbnail_forItem_error_(self, camera, thumb, item, error): pass
        def cameraDevice_didReceiveMetadata_forItem_error_(self, camera, meta, item, error): pass
        def cameraDevice_didReceivePTPEvent_(self, camera, data): pass
        def cameraDeviceDidRemoveAccessRestriction_(self, device): pass
        def cameraDeviceDidEnableAccessRestriction_(self, device): pass

    d = Delegate.alloc().init()
    browser = ICC.ICDeviceBrowser.alloc().init()
    browser.setDelegate_(d)
    browser.setBrowsedDeviceTypeMask_(ICC.ICDeviceTypeMaskCamera | ICC.ICDeviceLocationTypeMaskLocal)
    browser.start()
    _wait(lambda: d.devices, 15)
    _spin(1.0)  # weiteren Geräten kurz Zeit geben
    if not d.devices:
        return {"results": {}, "error": "iPhone wird von macOS nicht als Kamera erkannt (entsperrt? vertraut?)"}

    udid = (req.get("udid") or "").replace("-", "").lower()

    def matches(dev) -> bool:
        try:
            sn = (dev.serialNumberString() or "").replace("-", "").lower()
        except Exception:
            sn = ""
        return bool(udid) and sn == udid

    device = next((x for x in d.devices if matches(x)), None) \
        or next((x for x in d.devices if x.name() == req.get("name")), None) \
        or (d.devices[0] if len(d.devices) == 1 else None)
    if device is None:
        return {"results": {}, "error": "Mehrere Geräte angeschlossen – bitte nur das iPhone anstecken"}

    device.setDelegate_(d)
    device.requestOpenSession()
    if not _wait(lambda: d.opened, 60) or d.session_error is not None:
        return {"results": {}, "error": "Verbindung zum iPhone nicht möglich – iPhone entsperren und erneut versuchen"}
    _wait(lambda: d.ready, 600)

    try:
        can_delete = bool(device.canDeleteOneOrMoreFiles())
    except Exception:
        can_delete = True
    if not can_delete:
        device.requestCloseSession()
        return {"results": {}, "error": "Das iPhone erlaubt das Löschen nicht. Meist ist „iCloud-Fotos“ aktiv – "
                                         "dann bitte direkt in der Fotos-App am iPhone löschen."}

    # Dateien zuordnen: exakt (Ordner, Name, Größe), sonst (Ordner, Name ohne Endung, Foto/Video)
    exact, loose = {}, {}
    for f in device.mediaFiles() or []:
        name = str(f.name())
        try:
            folder = str(f.parentFolder().name())
        except Exception:
            folder = ""
        p = Path(name)
        exact[(folder, name, int(f.fileSize()))] = f
        loose.setdefault((folder, p.stem, p.suffix.lower() in VIDEO), []).append(f)

    results: dict[str, str | None] = {}
    to_delete, wanted = [], []   # wanted: (Ordner, Name, Schlüssel) – vorab merken, da gelöschte Objekte ungültig werden
    for item in req["files"]:
        p = Path(item["name"])
        f = exact.get((item["folder"], item["name"], int(item["size"])))
        if f is None:
            cands = loose.get((item["folder"], p.stem, p.suffix.lower() in VIDEO), [])
            f = cands[0] if len(cands) == 1 else None
        if f is None:
            results[item["key"]] = "nicht gefunden"
            continue
        to_delete.append(f)
        wanted.append((item["folder"], str(f.name()), item["key"]))

    if to_delete:
        failed_names: set[str] = set()
        done = {"v": False, "err": None}

        def on_failed(failed):
            for v in (failed or {}).values():
                try:
                    failed_names.add(str(v.name()))
                except Exception:
                    pass

        def on_done(result, error):
            done["v"], done["err"] = True, error

        if device.respondsToSelector_(b"requestDeleteFiles:deleteFailed:completion:"):
            device.requestDeleteFiles_deleteFailed_completion_(to_delete, on_failed, on_done)
            _wait(lambda: done["v"], 1800)
        else:  # ältere macOS-Versionen
            device.requestDeleteFiles_(to_delete)
            names = {w[1] for w in wanted}
            _wait(lambda: not names & {str(x.name()) for x in (device.mediaFiles() or [])}, 1800)
        _spin(1.0)

        remaining = set()
        for x in device.mediaFiles() or []:
            try:
                remaining.add((str(x.parentFolder().name()), str(x.name())))
            except Exception:
                remaining.add(("", str(x.name())))
        for folder, name, key in wanted:
            gone = (folder, name) not in remaining and ("", name) not in remaining
            results[key] = None if gone and name not in failed_names else "iPhone hat das Löschen abgelehnt"

    device.requestCloseSession()
    _spin(0.5)
    return {"results": results, "error": None}


if __name__ == "__main__":
    main()
