---
name: sign-macos
description: Sign and notarize the music-chop (dv2mv) macOS .app and .dmg for distribution. Use when producing a distributable signed/notarized macOS build, or when an `xcrun notarytool` submission comes back `Invalid` and needs diagnosis + a re-sign. Auto-detects the Developer ID identity from the keychain, so no Team ID is hardcoded.
---

# Sign + notarize the dv2mv macOS build

End-to-end recipe for a Gatekeeper-clean `dist/dv2mv.app` + `dist/dv2mv-<ver>.dmg`.
The build pipeline lives in `packaging/build_macos.sh`; this skill drives it and
handles the failure modes we hit the slow way the first time.

## Preconditions (verify, don't assume)

1. **Developer ID Application cert is installed** (cert + private key in the login keychain):
   ```bash
   security find-identity -v -p codesigning | grep "Developer ID Application"
   ```
   If empty: create it in Xcode → Settings → Accounts → (team) → Manage Certificates → + → **Developer ID Application**. This needs the **$99 Apple Developer Program** and an **Account Holder / Admin** role — the plain "Developer" role cannot create Developer ID certs.

2. **A notarytool keychain profile exists** (stores Apple ID + app-specific password + Team ID so creds never appear on a command line or in this skill):
   ```bash
   xcrun notarytool history --keychain-profile dv2mv-notary >/dev/null 2>&1 && echo OK
   ```
   If missing: create an app-specific password at appleid.apple.com, then
   `xcrun notarytool store-credentials dv2mv-notary --apple-id <email> --team-id <TEAMID> --password <app-specific-pw>`.

3. **Build deps**: `command -v pyinstaller` (else `pip install -r packaging/requirements-build.txt`), plus `ffmpeg`/`ffprobe` on PATH (the spec bundles them; `rubberband` is optional but included if present).

## Derive the signing identity (no hardcoding)

```bash
DEVELOPER_ID="$(security find-identity -v -p codesigning \
  | sed -n 's/.*"\(Developer ID Application: .*\)"/\1/p' | head -1)"
echo "Signing as: $DEVELOPER_ID"
```

## Avoid the keychain-prompt trap (one-time per machine)

`codesign` signs *dozens* of nested binaries; clicking **Allow** grants one file at
a time → endless prompts, and even **Always Allow** can keep re-prompting. Grant
Apple's tools persistent access to the key once:
```bash
security set-key-partition-list -S apple-tool:,apple: -s ~/Library/Keychains/login.keychain-db
# prompts for the login (Mac) password in the terminal; add `-k '<pw>'` if it errors instead of prompting
```

## Build, sign, notarize

```bash
cd ~/code/dv2mv
DEVELOPER_ID="$DEVELOPER_ID" NOTARY_PROFILE=dv2mv-notary bash packaging/build_macos.sh
```
The script: builds the icns → PyInstaller freeze → codesign (hardened runtime +
`packaging/entitlements.plist` for numba JIT and the bundled dylibs) → dmg →
`notarytool submit --wait` → `stapler staple` → `spctl` verdict.

**Run the long wait in the background** so a killed local poller doesn't lose the
verdict — Apple keeps processing server-side regardless. If you only need to wait
on an already-submitted id: `xcrun notarytool wait <id> --keychain-profile dv2mv-notary`.

## If notarization comes back `Invalid`

This is normal and diagnosable — do NOT just retry. Pull the per-file log:
```bash
xcrun notarytool log <submission-id> --keychain-profile dv2mv-notary
```

**The bug we hit (and the #1 thing to check):** the codesign step must sign
**every Mach-O by file *type*, not by extension.** Extensionless CLI tools
(ffmpeg / ffprobe / rubberband under `Contents/Frameworks/bin/`) are missed by a
`*.dylib`/`*.so` glob and get rejected with: *not signed with a valid Developer ID
/ no secure timestamp / hardened runtime not enabled*. The fixed loop in
`build_macos.sh` is:
```bash
find "$APP/Contents" -type f -print0 | while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then
    codesign --force --timestamp --options runtime -s "$DEVELOPER_ID" "$f"
  fi
done
```

**Re-sign the EXISTING app — don't rebuild.** When only signing was wrong the
bundle is fine; skip the slow PyInstaller run:
```bash
APP="dist/dv2mv.app"
# 1. re-sign all nested Mach-O (loop above)
# 2. re-sign the app bundle WITH entitlements:
codesign --force --timestamp --options runtime --entitlements packaging/entitlements.plist -s "$DEVELOPER_ID" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
# 3. rebuild the dmg around it, then re-submit with notarytool (wait in background)
```

## Success looks like

- `notarytool` status: **Accepted**
- `stapler staple` succeeds on both `.dmg` and `.app`
- `spctl -a -vvv <app>` → **accepted**, `source=Notarized Developer ID`

## After a green light

Commit only `packaging/build_macos.sh` (explicit `git add`, never `-A` in this
repo) to `main` and push to sourcehut (`git@git.sr.ht:~kevin_griffin/music-chop`),
which triggers builds.sr.ht CI. The signed `.dmg` is a `dist/` build artifact
(gitignored) — it is not committed; ship it via a release if distribution is wanted.
