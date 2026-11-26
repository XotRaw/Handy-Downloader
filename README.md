# 📱 Handy-Downloader (macOS)

Handy per Kabel an den Mac anstecken → Fotos und Videos werden automatisch heruntergeladen
und sortiert abgelegt:

```
Handy-Import/
└── 2025/
    ├── 01.25/
    │   ├── Fotos/         ← mit der Handy-Kamera aufgenommen
    │   ├── Videos/
    │   ├── Screenshots/
    │   └── Extern/        ← heruntergeladen, per WhatsApp & Co. erhalten
    └── 02.25/
        └── …
```

## Was das Programm kann

- **Automatisch beim Anstecken** importieren (iPhone, Android, SD-Karte/Kamera) – mit Mitteilung oben rechts.
- **Ordner nach Jahr und Monat**: `2025/01.25`. Umstellbar auf `1.25` oder `01 Januar`.
- **Nach Art trennen**: Kamera-Foto, Extern/Download, Screenshot, Video. Erkannt wird das an den
  Kameradaten im Bild (EXIF: Hersteller/Modell). Bilder ohne Kameradaten gelten als „extern“.
- **Richtiges Datum**: Aufnahmedatum aus dem Foto bzw. Video; sonst aus dem Dateinamen
  (`IMG-20250105-WA0001.jpg`), sonst Dateidatum.
- **Keine Doppelten**: Bereits importierte Dateien werden übersprungen, auch beim nächsten Anstecken.
  Beim Import wird auf dem Handy **nichts gelöscht** – nur, wenn du es unter „Platz schaffen“ ausdrücklich auswählst.
- **Archiv-Ansicht mit Suche & Filtern**: nur Videos, nur Kamera-Fotos, nur Screenshots,
  nach Jahr/Monat, nach Kamera, nur hochauflösende Bilder (z. B. „ab 8 MP“), Suche im Dateinamen.
  Klick öffnet Bild/Video, „Im Finder zeigen“ springt zur Datei.
- **Platz auf dem Handy schaffen**: Nach dem Import Bilder/Videos antippen, die vom Handy
  gelöscht werden sollen (siehe unten).
- Zusätzlich: **beliebigen Ordner vom Mac** importieren und einsortieren (z. B. alte Fotos).

## Installation

1. Oben auf dieser Seite auf den grünen Knopf **„Code“ → „Download ZIP“** klicken.
2. Die ZIP im Downloads-Ordner doppelklicken. Den entstandenen Ordner in
   **`handy-downloader`** umbenennen und in deinen **Benutzerordner** ziehen
   (im Finder links das Häuschen).
3. Terminal öffnen (`Cmd + Leertaste`, „Terminal“, Enter), diese Zeile einfügen und Enter drücken:
   ```bash
   bash ~/handy-downloader/install_mac.sh
   ```
   Die Frage nach Autostart mit `j` beantworten, dann läuft das Programm immer im Hintergrund
   und importiert, sobald ein Handy angesteckt wird.
4. Oberfläche öffnen: **http://127.0.0.1:8765** (oder Doppelklick auf
   „Handy-Downloader starten.command“).

Standard-Zielordner ist `~/Bilder/Handy-Import` – in der Oberfläche unter „Einstellungen“ änderbar.

### iPhone
- Mit Kabel anstecken, entsperren, **„Diesem Computer vertrauen“** bestätigen.
- Wenn in den iPhone-Einstellungen *Fotos → „iPhone-Speicher optimieren“* aktiv ist, liegen
  ältere Originale nur in iCloud und nicht auf dem Gerät – die können per Kabel nicht geladen werden.
- Tipp: Damit die Fotos-App des Macs nicht parallel aufgeht, in „Digitale Bilder“ beim iPhone
  „Beim Anschließen öffnen: Kein Programm“ wählen.

### Android
- Einmalig `brew install android-platform-tools` (Homebrew: https://brew.sh).
- Am Handy die **Entwickleroptionen** einschalten (7× auf „Build-Nummer“ tippen) und
  **USB-Debugging** aktivieren. Beim Anstecken die Abfrage am Handy erlauben.
- Gelesen werden: Kamera (`DCIM`), `Pictures` (Screenshots), `Download`, `Movies`, WhatsApp-Medien.

### Optional
- `brew install ffmpeg` → genaueres Aufnahmedatum und Vorschaubilder bei Videos.

## Platz auf dem Handy schaffen

Beim angeschlossenen Gerät auf **„Platz schaffen …“** klicken. Dort erscheinen **nur Dateien, die
schon auf dem Mac gesichert sind**.

1. Filtern, z. B. **🎬 Videos**, **„älter als 3 Monate“**, **„größte zuerst“**.
2. Bilder/Videos antippen (rotes Häkchen) oder „Alle angezeigten auswählen“.
3. Unten steht, wie viel Platz frei wird → **„… vom Handy löschen“** → bestätigen.

Sicherheit:
- Vor dem Löschen wird **jede Kopie auf dem Mac noch einmal geprüft** (Prüfsumme). Fehlt die Kopie
  oder ist sie verändert, bleibt die Datei auf dem Handy.
- Die Kopien auf dem Mac werden nie angefasst.
- Auf dem Handy ist das Löschen **endgültig** (kein „Zuletzt gelöscht“).

**iPhone:** Gelöscht wird über Apples offiziellen Weg (wie im Programm „Digitale Bilder“), damit die
Fotos-App sauber bleibt. **Ist „iCloud-Fotos“ eingeschaltet, erlaubt das iPhone kein Löschen vom Mac**
– und ein Löschen würde die Fotos auch aus iCloud und von allen Geräten entfernen. In dem Fall in
der Fotos-App am iPhone löschen. Beim ersten Mal fragt macOS evtl. nach Zugriff auf Fotos/Kamera –
bitte erlauben.

**Android:** Wird direkt gelöscht (per `adb`), die Galerie wird benachrichtigt.

## Wie wird „Kamera-Foto“ und „Extern“ unterschieden?

| Art | Erkennung |
|---|---|
| 🎬 Video | Dateiendung (.mov, .mp4, …) |
| 🖥 Screenshot | Dateiname („Screenshot“, „Bildschirmfoto“) oder PNG im Handy-Bildschirmformat ohne Kameradaten |
| 📷 Kamera-Foto | Bild enthält Kamera-Hersteller/-Modell (z. B. „Apple iPhone 15 Pro“) |
| 🌐 Extern | alles andere: Downloads, WhatsApp/Telegram-Bilder, gespeicherte Bilder |

Hochwertige Downloads findest du über **Extern & Downloads** + Filter **„ab 8 MP“**.

## Technik

- Python 3, Weboberfläche mit Flask, Katalog in SQLite unter
  `~/Library/Application Support/HandyDownloader/`.
- iPhone über `pymobiledevice3` (USB, Apple-Dateidienst), Android über `adb`,
  Laufwerke mit `DCIM`-Ordner unter `/Volumes`.
- Tests: `python3 -m pytest tests`
- Autostart wieder entfernen:
  `launchctl unload ~/Library/LaunchAgents/de.handy-downloader.plist && rm ~/Library/LaunchAgents/de.handy-downloader.plist`
