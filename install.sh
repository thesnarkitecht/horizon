#!/bin/sh
# curl -fsSL https://raw.githubusercontent.com/thesnarkitecht/horizon/main/install.sh | sh
set -eu
SRC="${HORIZON_SRC:-https://raw.githubusercontent.com/thesnarkitecht/horizon/main}"
BIN="$HOME/.horizon/bin"
mkdir -p "$BIN" "$HOME/.local/bin"
for f in horizon.py ui.html app/Horizon.swift; do curl -fsSL "$SRC/$f" -o "$BIN/$(basename "$f")"; done
chmod +x "$BIN/horizon.py"
ln -sf "$BIN/horizon.py" "$HOME/.local/bin/horizon"
echo "installed: horizon -> ~/.local/bin/horizon"

if command -v swiftc >/dev/null 2>&1; then
  A="$HOME/Applications/Horizon.app/Contents"
  mkdir -p "$A/MacOS"
  swiftc -O "$BIN/Horizon.swift" -o "$A/MacOS/Horizon"
  cat > "$A/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>Horizon</string>
  <key>CFBundleIdentifier</key><string>local.horizon</string>
  <key>CFBundleName</key><string>Horizon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>26.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
  echo "installed: ~/Applications/Horizon.app"
else
  echo "no swiftc: run 'xcode-select --install' and re-run to build Horizon.app (or use 'horizon app')"
fi

command -v container >/dev/null 2>&1 || echo "next: brew install container && container system start"
command -v mlx_lm.server >/dev/null 2>&1 || echo "next: pip install mlx-lm   (on every Mac you pair)"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "add ~/.local/bin to your PATH" ;; esac
