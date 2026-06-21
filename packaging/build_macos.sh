#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Build dv2mv.app and a .dmg on macOS.
#
#   bash packaging/build_macos.sh
#
# Today (no Apple Developer cert): produces an ad-hoc-signed dist/dv2mv.app and
# dist/dv2mv-<ver>.dmg you can run locally. The Developer ID + notarization
# steps are switched on by env vars once the membership lands — no edits needed:
#
#   DEVELOPER_ID="Developer ID Application: Your Name (TEAMID)" \
#   NOTARY_PROFILE=dv2mv-notary \                # from `xcrun notarytool store-credentials`
#   bash packaging/build_macos.sh
#
set -euo pipefail

VER="0.1.0"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # packaging/
ROOT="$(cd "$HERE/.." && pwd)"                          # repo root
cd "$ROOT"

# ── 0. preflight ────────────────────────────────────────────────────────────
[[ "$(uname)" == "Darwin" ]] || { echo "build: macOS only." >&2; exit 1; }
command -v pyinstaller >/dev/null || {
    echo "build: pyinstaller not found. Run:" >&2
    echo "       pip install -r packaging/requirements-build.txt" >&2
    exit 1; }

# ── 1. app icon: assets/icons/dv2mv-256.png → packaging/dv2mv.icns ──────────
SRC_PNG="assets/icons/dv2mv-256.png"
ICNS="$HERE/dv2mv.icns"
if [[ -f "$SRC_PNG" ]]; then
    echo "==> building $ICNS"
    ICONSET="$(mktemp -d)/dv2mv.iconset"
    mkdir -p "$ICONSET"
    # Source is 256px; generate the sizes we can (upscaling the @2x of 256 is
    # acceptable for a first release — replace with a 1024px master later).
    for spec in "16:16x16" "32:16x16@2x" "32:32x32" "64:32x32@2x" \
                "128:128x128" "256:128x128@2x" "256:256x256" "512:256x256@2x"; do
        px="${spec%%:*}"; tag="${spec##*:}"
        sips -z "$px" "$px" "$SRC_PNG" --out "$ICONSET/icon_${tag}.png" >/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$ICNS"
    rm -rf "$(dirname "$ICONSET")"
else
    echo "==> WARN: $SRC_PNG missing; building without a custom icon" >&2
fi

# ── 2. freeze ───────────────────────────────────────────────────────────────
echo "==> pyinstaller (clean)"
rm -rf build dist
pyinstaller --noconfirm packaging/dv2mv.spec
APP="dist/dv2mv.app"
[[ -d "$APP" ]] || { echo "build: $APP not produced." >&2; exit 1; }

# ── 3. codesign ─────────────────────────────────────────────────────────────
if [[ -n "${DEVELOPER_ID:-}" ]]; then
    echo "==> codesign (Developer ID, hardened runtime)"
    # Sign nested code first, then the app, with the JIT/library entitlements.
    find "$APP/Contents" \( -name "*.dylib" -o -name "*.so" \) -print0 \
        | xargs -0 -I{} codesign --force --timestamp --options runtime \
            -s "$DEVELOPER_ID" {} || true
    codesign --force --timestamp --options runtime \
        --entitlements packaging/entitlements.plist \
        -s "$DEVELOPER_ID" "$APP"
    codesign --verify --deep --strict --verbose=2 "$APP"
else
    echo "==> codesign (ad-hoc — local use only, no Developer ID set)"
    codesign --force --deep -s - "$APP"
fi

# ── 4. dmg (drag-to-Applications) ───────────────────────────────────────────
echo "==> dmg"
DMG="dist/dv2mv-${VER}.dmg"
STAGE="$(mktemp -d)/dv2mv"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "dv2mv ${VER}" -srcfolder "$STAGE" \
    -ov -format UDZO "$DMG" >/dev/null
rm -rf "$(dirname "$STAGE")"
echo "==> built $DMG"

# ── 5. notarize + staple (gated on creds) ───────────────────────────────────
if [[ -n "${DEVELOPER_ID:-}" && -n "${NOTARY_PROFILE:-}" ]]; then
    echo "==> notarize ($NOTARY_PROFILE)"
    xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG"
    xcrun stapler staple "$APP"
    spctl -a -vvv "$APP" || true
    echo "==> notarized + stapled"
else
    echo "==> skip notarize (set DEVELOPER_ID + NOTARY_PROFILE to enable)"
fi

echo "==> done: $APP  and  $DMG"
