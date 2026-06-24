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

VER="0.1.1"
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
    # Match Mach-O binaries by *type*, not extension: the bundled CLI tools
    # (ffmpeg/ffprobe/rubberband under Frameworks/bin) are extensionless
    # executables, so a *.dylib/*.so glob misses them and notarization rejects
    # the app ("not signed with a valid Developer ID / no secure timestamp /
    # hardened runtime not enabled"). `file` + a Mach-O test catches them all.
    find "$APP/Contents" -type f -print0 \
        | while IFS= read -r -d '' f; do
            if file -b "$f" | grep -q "Mach-O"; then
                codesign --force --timestamp --options runtime \
                    -s "$DEVELOPER_ID" "$f"
            fi
          done
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

# Sign the dmg *container* too (separate from notarizing it), so `spctl -t open`
# blesses the disk image, not just the app inside. A notarized-but-unsigned dmg
# still mounts and the app verifies as Notarized Developer ID, but the container
# reports "no usable signature" — this removes that. Must run BEFORE notarize.
if [[ -n "${DEVELOPER_ID:-}" ]]; then
    echo "==> codesign dmg (container)"
    codesign --force --timestamp -s "$DEVELOPER_ID" "$DMG"
fi

# ── 5. notarize + staple (gated on creds) ───────────────────────────────────
# Always capture the submission id and pull the FULL notary log, so a failure
# surfaces the exact cause — per-file signing errors (the real bug here was three
# bundled CLIs left unsigned), or a status code like 7000 (team not configured
# for notarization) — instead of a bare "Invalid" or a silent hang. Staple only
# on Accepted; otherwise print the log and fail loud. Override the wait limit
# with NOTARY_TIMEOUT (e.g. 90m); a new Developer ID's first apps are often held
# for in-depth analysis and can take hours.
if [[ -n "${DEVELOPER_ID:-}" && -n "${NOTARY_PROFILE:-}" ]]; then
    echo "==> notarize ($NOTARY_PROFILE)"
    SUB_JSON="$(xcrun notarytool submit "$DMG" \
        --keychain-profile "$NOTARY_PROFILE" --output-format json)"
    SUB_ID="$(printf '%s' "$SUB_JSON" | plutil -extract id raw -o - -)"
    echo "    submission id: $SUB_ID"

    echo "==> waiting for verdict (timeout ${NOTARY_TIMEOUT:-45m})"
    xcrun notarytool wait "$SUB_ID" \
        --keychain-profile "$NOTARY_PROFILE" \
        --timeout "${NOTARY_TIMEOUT:-45m}" || true

    SUB_STATUS="$(xcrun notarytool info "$SUB_ID" \
        --keychain-profile "$NOTARY_PROFILE" --output-format json \
        | plutil -extract status raw -o - -)"
    echo "    status: $SUB_STATUS"

    echo "==> notary log ($SUB_ID)"
    xcrun notarytool log "$SUB_ID" --keychain-profile "$NOTARY_PROFILE" \
        || echo "    (log not available yet — submission still In Progress)"

    if [[ "$SUB_STATUS" == "Accepted" ]]; then
        xcrun stapler staple "$DMG"
        xcrun stapler staple "$APP"
        spctl -a -vvv "$APP" || true
        echo "==> notarized + stapled"
    else
        {
            echo "build: notarization status='$SUB_STATUS' (not Accepted)."
            echo "       See the notary log above for the exact cause."
            echo "       'In Progress' = Apple-side delay (common for a new Developer"
            echo "       ID's first apps), not a package problem; the dmg is already"
            echo "       signed. Pick the verdict back up later with:"
            echo "         xcrun notarytool wait $SUB_ID --keychain-profile $NOTARY_PROFILE"
        } >&2
        exit 1
    fi
else
    echo "==> skip notarize (set DEVELOPER_ID + NOTARY_PROFILE to enable)"
fi

echo "==> done: $APP  and  $DMG"
