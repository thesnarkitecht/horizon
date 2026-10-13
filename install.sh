#!/bin/sh
# curl -fsSL https://raw.githubusercontent.com/thesnarkitecht/horizon/main/install.sh | sh
# Builds ~/Applications/Horizon.app. Needs Xcode Command Line Tools (xcode-select --install).
set -eu
SRC="${HORIZON_SRC:-https://raw.githubusercontent.com/thesnarkitecht/horizon/main}"
command -v swiftc >/dev/null 2>&1 || { echo "run 'xcode-select --install' first, then re-run this"; exit 1; }
A="$HOME/Applications/Horizon.app/Contents"
mkdir -p "$A/MacOS" "$A/Resources"
for f in horizon.py ui.html; do curl -fsSL "$SRC/$f" -o "$A/Resources/$f"; done
curl -fsSL "$SRC/app/Horizon.swift" -o "$A/Resources/Horizon.swift"
swiftc -O "$A/Resources/Horizon.swift" -o "$A/MacOS/Horizon"
rm "$A/Resources/Horizon.swift"
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
echo "installed ~/Applications/Horizon.app"
command -v container >/dev/null 2>&1 || echo "also needed: brew install container && container system start"
command -v mlx_lm.server >/dev/null 2>&1 || echo "also needed on every Mac: pip install mlx-lm"
command -v macmon >/dev/null 2>&1 || echo "for GPU stats on every Mac: brew install macmon"
