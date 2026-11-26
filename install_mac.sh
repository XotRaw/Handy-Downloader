#!/bin/bash
# Installiert den Handy-Downloader auf dem Mac (einmalig ausführen).
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

if ! command -v python3 >/dev/null; then
  echo "Python 3 fehlt. Bitte zuerst 'xcode-select --install' ausführen oder Python von python.org installieren."
  exit 1
fi

echo "→ Lege Python-Umgebung an …"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt

# Doppelklick-Starter
cat > "Handy-Downloader starten.command" <<EOS
#!/bin/bash
cd "$DIR" && exec .venv/bin/python -m handy_downloader
EOS
chmod +x "Handy-Downloader starten.command"

read -r -p "Beim Anmelden automatisch im Hintergrund starten? [j/N] " ans
if [[ "$ans" =~ ^[jJyY]$ ]]; then
  PLIST="$HOME/Library/LaunchAgents/de.handy-downloader.plist"
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$PLIST" <<EOS
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>de.handy-downloader</string>
  <key>ProgramArguments</key><array>
    <string>$DIR/.venv/bin/python</string><string>-m</string><string>handy_downloader</string><string>--no-browser</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/handy-downloader.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/handy-downloader.log</string>
</dict></plist>
EOS
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "✓ Autostart eingerichtet. Oberfläche: http://127.0.0.1:8765"
fi

echo
echo "✓ Fertig. Starten per Doppelklick auf „Handy-Downloader starten.command“."
echo "  Optional: brew install ffmpeg (genaueres Video-Datum)  ·  brew install android-platform-tools (Android)"
