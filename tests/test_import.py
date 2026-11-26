import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDY_DOWNLOADER_HOME", str(tmp_path / "home"))
    for m in [m for m in sys.modules if m.startswith("handy_downloader")]:
        del sys.modules[m]
    return tmp_path


def make_camera_jpg(p: Path, when: str, make="Apple", model="iPhone 15 Pro"):
    img = Image.new("RGB", (4032, 3024), "red")
    exif = Image.Exif()
    exif[0x010F], exif[0x0110] = make, model
    exif.get_ifd(0x8769)[0x9003] = when
    img.save(p, exif=exif)


def test_classify_and_sort(env):
    from handy_downloader.catalog import Catalog
    from handy_downloader.importer import Job, run_import, DEFAULT_CONFIG
    from handy_downloader.sources import FolderSource

    phone = env / "phone" / "DCIM" / "100APPLE"
    phone.mkdir(parents=True)
    make_camera_jpg(phone / "IMG_0001.JPG", "2025:01:14 10:20:30")
    make_camera_jpg(phone / "IMG_0002.JPG", "2025:02:03 08:00:00")
    Image.new("RGB", (1179, 2556), "blue").save(phone / "IMG_0003.PNG")           # Screenshot-Größe
    Image.new("RGB", (6000, 4000), "green").save(phone / "IMG-20250305-WA0007.jpg")  # WhatsApp
    Image.new("RGB", (800, 600), "white").save(phone / "download.jpg")
    os.utime(phone / "download.jpg", (datetime(2024, 12, 24).timestamp(),) * 2)
    (phone / "IMG_0004.MOV").write_bytes(b"\x00" * 100)
    os.utime(phone / "IMG_0004.MOV", (datetime(2025, 2, 10).timestamp(),) * 2)
    (phone / "notes.txt").write_text("kein Bild")

    target = env / "Bilder"
    cfg = dict(DEFAULT_CONFIG, target=str(target))
    cat = Catalog(env / "k.sqlite")
    job = run_import(FolderSource(phone.parent), cfg, cat, Job())

    assert job.state == "done" and job.imported == 6 and job.errors == 0
    shot = datetime.fromtimestamp((phone / "IMG_0003.PNG").stat().st_mtime)
    files = sorted(str(p.relative_to(target)) for p in target.rglob("*") if p.is_file())
    assert files == sorted([
        "2024/12.24/Extern/download.jpg",
        "2025/01.25/Fotos/IMG_0001.JPG",
        "2025/02.25/Fotos/IMG_0002.JPG",
        "2025/02.25/Videos/IMG_0004.MOV",
        "2025/03.25/Extern/IMG-20250305-WA0007.jpg",
        f"{shot:%Y/%m.%y}/Screenshots/IMG_0003.PNG",
    ])

    # zweiter Lauf: nichts doppelt
    job2 = run_import(FolderSource(phone.parent), cfg, cat, Job())
    assert job2.imported == 0

    total, rows = cat.search(category="video")
    assert total == 1 and rows[0]["orig_name"] == "IMG_0004.MOV"
    total, _ = cat.search(min_mp=8)
    assert total == 3  # zwei Kamerafotos (12 MP) + WhatsApp-Bild (24 MP)
    total, rows = cat.search(category="kamera")
    assert total == 2 and rows[0]["camera"] == "Apple iPhone 15 Pro"


def test_month_styles(env):
    from handy_downloader.media import month_folder
    dt = datetime(2025, 3, 7)
    assert month_folder(dt) == "03.25"
    assert month_folder(dt, "M.YY") == "3.25"
    assert month_folder(dt, "MM Monat") == "03 März"


def test_filename_dates(env):
    from handy_downloader.media import date_from_filename
    assert date_from_filename("PXL_20250103_142530123.jpg") == datetime(2025, 1, 3, 14, 25, 30)
    assert date_from_filename("IMG-20250105-WA0001.jpg") == datetime(2025, 1, 5)
    assert date_from_filename("Screenshot 2024-11-02 at 09.10.11.png") == datetime(2024, 11, 2, 9, 10, 11)
    assert date_from_filename("IMG_0001.HEIC") is None


def test_web_api(env):
    from handy_downloader import app as appmod
    c = appmod.app.test_client()
    assert c.get("/").status_code == 200
    s = c.get("/api/status").get_json()
    assert "devices" in s and s["config"]["month_style"] == "MM.YY"
    assert c.post("/api/config", json={"month_style": "M.YY"}).get_json()["config"]["month_style"] == "M.YY"
    # Fremde Webseiten (kein JSON) werden abgewiesen
    assert c.post("/api/cancel", data="x", content_type="text/plain").status_code == 415
    assert c.get("/api/media?type=video").get_json()["total"] == 0


def test_delete_from_device_only_when_backup_verified(env):
    from handy_downloader.catalog import Catalog
    from handy_downloader.importer import Job, run_import, run_delete, DEFAULT_CONFIG
    from handy_downloader.sources import FolderSource

    card = env / "card" / "DCIM"
    card.mkdir(parents=True)
    make_camera_jpg(card / "A.JPG", "2025:01:01 10:00:00")
    make_camera_jpg(card / "B.JPG", "2025:01:02 10:00:00", model="iPhone 14")
    (card / "C.MOV").write_bytes(b"\x01" * 5000)
    src = FolderSource(card, kind="volume")
    cat = Catalog(env / "k.sqlite")
    cfg = dict(DEFAULT_CONFIG, target=str(env / "Bilder"))
    assert run_import(src, cfg, cat, Job()).imported == 3

    files = {Path(r["remote_path"]).name: r for r in cat.phone_files(src.id)}
    assert set(files) == {"A.JPG", "B.JPG", "C.MOV"}

    # Kopie von B auf dem Mac wird nachträglich verändert → B darf NICHT gelöscht werden
    Path(files["B.JPG"]["path"]).write_bytes(b"kaputt")
    rows = cat.phone_rows(src.id, [r["rid"] for r in files.values()])
    job = run_delete(src, rows, cat, Job())

    assert job.state == "done" and job.deleted == 2 and job.errors == 1
    assert not (card / "A.JPG").exists() and not (card / "C.MOV").exists()
    assert (card / "B.JPG").exists()
    assert job.freed == files["A.JPG"]["remote_size"] + 5000
    assert [Path(r["remote_path"]).name for r in cat.phone_files(src.id)] == ["B.JPG"]
    # Mac-Kopien sind unverändert da
    assert Path(files["A.JPG"]["path"]).exists() and Path(files["C.MOV"]["path"]).exists()


def test_old_catalog_gets_migrated(env):
    import sqlite3
    db = env / "alt.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE seen (device TEXT, remote_path TEXT, size INTEGER, mtime REAL, "
                "PRIMARY KEY (device, remote_path, size))")
    con.execute("INSERT INTO seen VALUES ('x','/a',1,0)")
    con.commit(); con.close()
    from handy_downloader.catalog import Catalog
    cat = Catalog(db)
    assert cat.was_seen("x", "/a", 1)
    assert cat.phone_files("x") == []  # alte Einträge ohne Prüfsumme werden nie zum Löschen angeboten


def test_android_delete_command(env):
    """Simuliertes adb: führt den Shell-Befehl lokal aus (prüft Quoting & Erfolgserkennung)."""
    from handy_downloader.sources import AndroidSource, RemoteFile
    fake = env / "adb"
    fake.write_text('#!/bin/sh\n# adb -s SERIAL shell CMD\nshift 3\nexec sh -c "$1"\n')
    fake.chmod(0o755)
    d = env / "sdcard" / "DCIM"
    d.mkdir(parents=True)
    tricky = d / "Johannes' Bild $HOME.jpg"
    tricky.write_bytes(b"x")
    src = AndroidSource(str(fake), "SER", "Pixel")
    res = src.delete([RemoteFile(str(tricky), tricky.name, 1, 0), RemoteFile(str(d / "weg.jpg"), "weg.jpg", 1, 0)])
    assert res == {str(tricky): None, str(d / "weg.jpg"): "nicht gefunden"}
    assert not tricky.exists()


def test_iphone_with_async_library(env, monkeypatch):
    """Simuliert die neue, asynchrone pymobiledevice3: Verbindungen sind an EINE Ereignisschleife gebunden."""
    import asyncio
    import types

    files = {"/DCIM/100APPLE/IMG_0001.JPG": None, "/DCIM/100APPLE/IMG_0002.MOV": b"\x00" * 3000}
    jpg = env / "src.jpg"
    make_camera_jpg(jpg, "2025:04:05 06:07:08")
    files["/DCIM/100APPLE/IMG_0001.JPG"] = jpg.read_bytes()

    class Bound:
        def __init__(self):
            self.loop = asyncio.get_running_loop()  # wie echte asyncio-Verbindungen

        def check(self):
            assert asyncio.get_running_loop() is self.loop, "Verbindung in fremder Ereignisschleife benutzt"

    class Lockdown(Bound):
        short_info = {"DeviceName": "Johannes' iPhone"}
        display_name = "iPhone 15 Pro"

        async def close(self):
            self.check()

    async def create_using_usbmux(serial=None, autopair=True):
        return Lockdown()

    async def list_devices():
        return [types.SimpleNamespace(serial="UDID1", connection_type="USB")]

    class AfcService(Bound):
        def __init__(self, lockdown):
            super().__init__()
            self.open, self.handles = False, {}

        async def __aenter__(self):
            self.check(); self.open = True; return self

        async def __aexit__(self, *a):
            self.check(); self.open = False

        async def listdir(self, d):
            self.check(); assert self.open
            if d == "/DCIM":
                return ["100APPLE"]
            return sorted(p.rsplit("/", 1)[1] for p in files if p.startswith(d + "/"))

        async def stat(self, p):
            self.check()
            if p in files:
                return {"st_ifmt": "S_IFREG", "st_size": len(files[p]), "st_mtime": datetime(2025, 4, 5)}
            return {"st_ifmt": "S_IFDIR", "st_size": 0, "st_mtime": datetime(2025, 1, 1)}

        async def fopen(self, p, mode="r"):
            self.check(); self.handles[1] = [files[p], 0]; return 1

        async def fread(self, h, n):
            self.check(); data, pos = self.handles[h]; self.handles[h][1] = pos + n; return data[pos:pos + n]

        async def fclose(self, h):
            self.check(); self.handles.pop(h)

    mods = {
        "pymobiledevice3": types.ModuleType("pymobiledevice3"),
        "pymobiledevice3.lockdown": types.SimpleNamespace(create_using_usbmux=create_using_usbmux),
        "pymobiledevice3.usbmux": types.SimpleNamespace(list_devices=list_devices),
        "pymobiledevice3.services": types.ModuleType("pymobiledevice3.services"),
        "pymobiledevice3.services.afc": types.SimpleNamespace(AfcService=AfcService),
    }
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)

    from handy_downloader.sources import detect_iphones
    from handy_downloader.catalog import Catalog
    from handy_downloader.importer import Job, run_import, DEFAULT_CONFIG

    devs = detect_iphones()
    assert [(d.kind, d.name) for d in devs] == [("iphone", "Johannes' iPhone")]
    cfg = dict(DEFAULT_CONFIG, target=str(env / "Bilder"))
    job = run_import(devs[0], cfg, Catalog(env / "k.sqlite"), Job())
    assert (job.state, job.imported, job.errors) == ("done", 2, 0), job.message
    assert (env / "Bilder/2025/04.25/Fotos/IMG_0001.JPG").read_bytes() == files["/DCIM/100APPLE/IMG_0001.JPG"]
    assert (env / "Bilder/2025/04.25/Videos/IMG_0002.MOV").stat().st_size == 3000
    assert devs[0]._afc is None  # Verbindung wurde wieder geschlossen


def test_overview_and_selective_import(env):
    from handy_downloader.catalog import Catalog
    from handy_downloader.importer import Job, run_import, run_scan, DEFAULT_CONFIG
    from handy_downloader.sources import FolderSource

    dcim = env / "Handy" / "DCIM" / "100APPLE"
    dcim.mkdir(parents=True)
    make_camera_jpg(dcim / "IMG_0001.JPG", "2025:05:01 10:00:00")
    Image.new("RGB", (1179, 2556), "blue").save(dcim / "IMG_0002.PNG")          # Screenshot
    (dcim / "IMG_0003.MOV").write_bytes(b"\x00" * 7000)
    (dcim / "IMG_0004.MP4").write_bytes(b"\x00" * 3000)
    src = FolderSource(env / "Handy" / "DCIM", kind="volume")
    cat = Catalog(env / "k.sqlite")
    cfg = dict(DEFAULT_CONFIG, target=str(env / "Bilder"))

    # 1. Nur nachsehen – nichts wird heruntergeladen
    scan = run_scan(src, cat, Job()).scan
    assert scan["video"] == {"n": 2, "bytes": 10000, "new_n": 2, "new_bytes": 10000}
    assert scan["foto"]["n"] == 1 and scan["screenshot"]["n"] == 1
    assert not (env / "Bilder").exists() or not any(p.is_file() for p in (env / "Bilder").rglob("*"))

    # 2. Nur Videos importieren
    job = run_import(src, cfg, cat, Job(), only=["video"])
    assert (job.imported, job.only) == (2, ["video"])
    assert {p.suffix for p in (env / "Bilder").rglob("*") if p.is_file()} == {".MOV", ".MP4"}
    assert job.scan["video"]["new_n"] == 0 and job.scan["foto"]["new_n"] == 1  # Übersicht aktualisiert

    # 3. Später den Rest – Videos werden nicht doppelt geholt
    job = run_import(src, cfg, cat, Job())
    assert job.imported == 2 and all(v["new_n"] == 0 for v in job.scan.values())

    f = cat.facets()
    assert f["sizes"]["video"] == 10000 and f["categories"] == {"video": 2, "kamera": 1, "screenshot": 1}
    assert f["sizes"]["kamera"] == (dcim / "IMG_0001.JPG").stat().st_size


def test_pause_resume_and_stop(env):
    import threading
    import time
    from handy_downloader.catalog import Catalog
    from handy_downloader.importer import Job, run_import, DEFAULT_CONFIG
    from handy_downloader.sources import FolderSource

    class SlowPhone(FolderSource):
        def fetch(self, rf, dest):
            time.sleep(0.1)
            super().fetch(rf, dest)

    dcim = env / "Handy" / "DCIM"
    dcim.mkdir(parents=True)
    for i in range(8):
        (dcim / f"IMG_{i:04d}.MOV").write_bytes(bytes([i]) * 1000)
    src = SlowPhone(dcim, kind="volume")
    cat = Catalog(env / "k.sqlite")
    cfg = dict(DEFAULT_CONFIG, target=str(env / "Bilder"))
    job = Job()
    t = threading.Thread(target=run_import, args=(src, cfg, cat, job))
    t.start()

    while job.done < 2:
        time.sleep(0.02)
    job.paused = True
    time.sleep(0.3)                     # laufende Datei wird noch fertig
    stand = job.done
    time.sleep(0.5)
    assert job.done == stand and job.state == "importing"   # pausiert: nichts passiert

    job.paused = False                   # Fortsetzen
    while job.done < stand + 2:
        time.sleep(0.02)
    job.cancel = True                    # Stoppen
    t.join(5)
    assert job.state == "done" and job.stopped and 0 < job.imported < 8
    assert "übrig" in job.message

    # Später: Rest holen, nichts doppelt
    rest = run_import(src, cfg, cat, Job())
    assert rest.imported == 8 - job.imported and rest.duplicates == 0
    assert len([p for p in (env / "Bilder").rglob("*") if p.is_file()]) == 8
