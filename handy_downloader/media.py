"""Metadaten lesen, Aufnahmedatum bestimmen und Dateien einordnen.

Kategorien:
    kamera      – Foto, das mit einer Kamera (Handy) aufgenommen wurde (EXIF Hersteller/Modell vorhanden)
    extern      – Bild ohne Kamera-Daten: aus dem Internet geladen, per WhatsApp/Telegram erhalten, bearbeitet …
    screenshot  – Bildschirmfoto
    video       – Videos (egal ob selbst aufgenommen oder empfangen)
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PHOTO_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".webp", ".dng", ".raw", ".tif", ".tiff", ".bmp"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".webm"}
MEDIA_EXT = PHOTO_EXT | VIDEO_EXT

CATEGORY_LABELS = {
    "kamera": "Kamera-Fotos",
    "extern": "Extern & Downloads",
    "screenshot": "Screenshots",
    "video": "Videos",
}
# Ordnernamen im Monatsordner
CATEGORY_FOLDERS = {
    "kamera": "Fotos",
    "extern": "Extern",
    "screenshot": "Screenshots",
    "video": "Videos",
}

try:  # HEIC-Unterstützung (iPhone)
    from pillow_heif import register_heif_opener  # type: ignore

    register_heif_opener()
except Exception:  # pragma: no cover - optional
    pass

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore

# EXIF-Tags
_TAG_MAKE, _TAG_MODEL, _TAG_SOFTWARE, _TAG_DATETIME = 0x010F, 0x0110, 0x0131, 0x0132
_TAG_DATETIME_ORIGINAL, _TAG_USER_COMMENT = 0x9003, 0x9286
_EXIF_IFD = 0x8769

_SCREENSHOT_NAME = re.compile(r"(screenshot|bildschirmfoto|screen[ _-]?shot|scrnshot|capture d)", re.I)
_MESSENGER_NAME = re.compile(r"(-WA\d+|whatsapp|telegram|signal-\d)", re.I)
# 20250103_142530, 2025-01-03 14.25.30, IMG-20250103-WA0001, PXL_20250103_142530123
_NAME_DATE = re.compile(r"(?<!\d)(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?:[ _T.-]?(?:at |um )?([01]\d|2[0-3])[-_.]?([0-5]\d)[-_.]?([0-5]\d))?")


@dataclass
class MediaInfo:
    category: str
    taken_at: datetime
    date_source: str  # exif | video | dateiname | datei
    width: int | None = None
    height: int | None = None
    camera: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def megapixel(self) -> float | None:
        if self.width and self.height:
            return round(self.width * self.height / 1_000_000, 1)
        return None


# Grobe Einteilung VOR dem Herunterladen (nur anhand von Name/Ordner), für „nur Videos importieren“ usw.
# Ob ein Foto selbst aufgenommen oder extern ist, steht erst nach dem Herunterladen fest (EXIF).
QUICK_LABELS = {"foto": "Fotos", "video": "Videos", "screenshot": "Screenshots"}
QUICK_OF = {"kamera": "foto", "extern": "foto", "screenshot": "screenshot", "video": "video"}


def quick_category(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    low = "/" + path.lower().replace("\\", "/")
    if is_video(name):
        return "video"
    if _SCREENSHOT_NAME.search(name) or "/screenshots/" in low or "/screenshot/" in low:
        return "screenshot"
    if name.lower().endswith(".png") and "/dcim/" in low:
        return "screenshot"  # das iPhone legt Screenshots als PNG im Kamera-Ordner ab
    return "foto"


def is_media(name: str) -> bool:
    return Path(name).suffix.lower() in MEDIA_EXT


def is_video(name: str) -> bool:
    return Path(name).suffix.lower() in VIDEO_EXT


def _parse_exif_date(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    value = str(value).strip().strip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            dt = datetime.strptime(value[:19], fmt)
            if dt.year > 1990:
                return dt
        except ValueError:
            continue
    return None


def date_from_filename(name: str) -> datetime | None:
    m = _NAME_DATE.search(Path(name).stem)
    if not m:
        return None
    y, mo, d, h, mi, s = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0))
    except ValueError:
        return None


def _read_image(path: Path) -> dict:
    out: dict = {}
    if Image is None:
        return out
    try:
        with Image.open(path) as img:
            out["width"], out["height"] = img.size
            out["format"] = img.format
            exif = img.getexif()
            if exif:
                sub = {}
                try:
                    sub = exif.get_ifd(_EXIF_IFD) or {}
                except Exception:
                    pass
                out["make"] = (exif.get(_TAG_MAKE) or "").strip().strip("\x00") or None
                out["model"] = (exif.get(_TAG_MODEL) or "").strip().strip("\x00") or None
                out["software"] = (exif.get(_TAG_SOFTWARE) or "").strip().strip("\x00") or None
                out["date"] = _parse_exif_date(sub.get(_TAG_DATETIME_ORIGINAL)) or _parse_exif_date(exif.get(_TAG_DATETIME))
                comment = sub.get(_TAG_USER_COMMENT)
                if isinstance(comment, bytes):
                    comment = comment.decode(errors="ignore")
                out["comment"] = comment or ""
            # PNG-Screenshots vom iPhone tragen das im XMP / Info-Feld
            info_text = " ".join(str(v) for v in (img.info or {}).values() if isinstance(v, (str, bytes)))
            out["info"] = info_text[:2000]
    except Exception:
        pass
    return out


def _read_video(path: Path) -> dict:
    out: dict = {}
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            res = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                capture_output=True, text=True, timeout=20,
            )
            data = json.loads(res.stdout or "{}")
            tags = (data.get("format") or {}).get("tags") or {}
            for s in data.get("streams", []):
                if s.get("codec_type") == "video":
                    out["width"], out["height"] = s.get("width"), s.get("height")
                    break
            created = tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time")
            if created:
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if dt.tzinfo:
                        dt = dt.astimezone().replace(tzinfo=None)
                    if dt.year > 1990:
                        out["date"] = dt
                except ValueError:
                    pass
            out["make"] = tags.get("com.apple.quicktime.make") or tags.get("com.android.manufacturer")
            out["model"] = tags.get("com.apple.quicktime.model") or tags.get("com.android.model")
            return out
        except Exception:
            pass
    mdls = shutil.which("mdls")  # macOS Spotlight als Ausweichlösung
    if mdls:
        try:
            res = subprocess.run([mdls, "-name", "kMDItemContentCreationDate", "-name", "kMDItemPixelWidth",
                                  "-name", "kMDItemPixelHeight", "-raw", str(path)],
                                 capture_output=True, text=True, timeout=10)
            parts = res.stdout.split("\x00")
            if parts and parts[0] and parts[0] != "(null)":
                dt = datetime.strptime(parts[0][:25], "%Y-%m-%d %H:%M:%S %z").astimezone().replace(tzinfo=None)
                out["date"] = dt
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                out["width"], out["height"] = int(parts[1]), int(parts[2])
        except Exception:
            pass
    return out


def analyze(path: Path, fallback_mtime: float | None = None, original_name: str | None = None) -> MediaInfo:
    """Datei untersuchen. `original_name` = Name auf dem Handy (für Datums-/Typ-Erkennung)."""
    name = original_name or path.name
    meta = _read_video(path) if is_video(name) else _read_image(path)

    # --- Datum ---------------------------------------------------------
    taken, source = meta.get("date"), "exif" if not is_video(name) else "video"
    if not taken:
        taken, source = date_from_filename(name), "dateiname"
    if not taken:
        ts = fallback_mtime if fallback_mtime is not None else path.stat().st_mtime
        taken, source = datetime.fromtimestamp(ts), "datei"

    # --- Kategorie -----------------------------------------------------
    camera = " ".join(x for x in (meta.get("make"), meta.get("model")) if x) or None
    if camera and meta.get("model") and meta.get("make") and meta["model"].lower().startswith(meta["make"].lower()):
        camera = meta["model"]
    hint = f"{meta.get('comment', '')} {meta.get('info', '')}".lower()

    if is_video(name):
        category = "video"
    elif _SCREENSHOT_NAME.search(name) or "screenshot" in hint:
        category = "screenshot"
    elif camera and not _MESSENGER_NAME.search(name):
        category = "kamera"
    elif (path.suffix.lower() == ".png" and not camera and meta.get("width")
          and _looks_like_phone_screen(meta["width"], meta["height"])):
        category = "screenshot"
    else:
        category = "extern"

    return MediaInfo(
        category=category, taken_at=taken, date_source=source,
        width=meta.get("width"), height=meta.get("height"), camera=camera,
        extra={"software": meta.get("software")},
    )


def _looks_like_phone_screen(w: int, h: int) -> bool:
    lo, hi = sorted((w, h))
    return lo >= 640 and 1.7 <= hi / lo <= 2.4


MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
             "September", "Oktober", "November", "Dezember"]


def month_folder(dt: datetime, style: str = "MM.YY") -> str:
    """Name des Monatsordners. Stile: 'MM.YY' → 01.25, 'M.YY' → 1.25, 'MM Monat' → 01 Januar."""
    if style == "M.YY":
        return f"{dt.month}.{dt:%y}"
    if style == "MM Monat":
        return f"{dt.month:02d} {MONTHS_DE[dt.month - 1]}"
    return f"{dt:%m}.{dt:%y}"


def target_dir(root: Path, info: MediaInfo, month_style: str = "MM.YY", split_by_type: bool = True) -> Path:
    d = root / f"{info.taken_at.year}" / month_folder(info.taken_at, month_style)
    if split_by_type:
        d = d / CATEGORY_FOLDERS[info.category]
    return d
