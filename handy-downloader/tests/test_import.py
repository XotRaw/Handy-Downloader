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
