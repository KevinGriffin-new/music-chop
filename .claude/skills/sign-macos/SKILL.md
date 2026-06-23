---
name: sign-macos
description: Sign and notarize a macOS app bundle (.app) + its .dmg for Developer ID distribution outside the Mac App Store. Use when producing a distributable signed/notarized Mac build, or when an `xcrun notarytool` submission comes back `Invalid` and needs diagnosis + a re-sign. Auto-detects the Developer ID identity from the keychain, so no Team ID is hardcoded. The recipe is project-agnostic; an "Applying it here" footer maps it onto this repo.
---

# Sign + notarize a macOS app (Developer ID)

End-to-end recipe for a Gatekeeper-clean, signed-and-notarized `.app` + `.dmg`
distributable *outside* the Mac App Store. Project-agnostic — the **"Applying it
here"** footer maps each step onto this repo. Throughout: `$APP` is your built
`.app`, `$DMG` your disk image, `<notary-profile>` your stored notarytool creds.

## Preconditions (verify, don't assume)

1. **Developer ID Application cert installed** (cert + private key in login keychain):
   ```bash
   security find-identity -v -p codesigning | grep "Developer ID Application"
   ```
   If empty: Xcode → Settings → Accounts → (team) → Manage Certificates → + →
   **Developer ID Application**. Needs the **$99 Apple Developer Program** and an
   **Account Holder / Admin** role — the plain "Developer" role can't create
   Developer ID certs. The cert is useless without its private key, so create it
   on (or export it to) the build machine.

2. **A notarytool keychain profile exists** (stores Apple ID + app-specific
   password + Team ID, so credentials never hit a command line or this file):
   ```bash
   xcrun notarytool history --keychain-profile <notary-profile> >/dev/null 2>&1 && echo OK
   ```
   If missing: make an app-specific password at appleid.apple.com, then
   `xcrun notarytool store-credentials <notary-profile> --apple-id <email> --team-id <TEAMID> --password <app-specific-pw>`.

3. **A built `.app` bundle** from your freezer (PyInstaller, py2app, Xcode,
   electron-builder, …) with all third-party binaries already inside it.

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
# prompts for the login (Mac) password; add `-k '<pw>'` if it errors instead of prompting
```

## Sign every Mach-O — by *type*, not extension

THE bug that fails notarization most often: signing only `*.dylib`/`*.so` misses
**extensionless Mach-O executables** (bundled CLIs, helper tools) under
`$APP/Contents/…`. They ship with their original signatures and get rejected —
*not signed with a valid Developer ID / no secure timestamp / hardened runtime not
enabled*. Match by file **type**:
```bash
find "$APP/Contents" -type f -print0 | while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then
    codesign --force --timestamp --options runtime -s "$DEVELOPER_ID" "$f"
  fi
done
# then the bundle itself, with the entitlements your app needs:
codesign --force --timestamp --options runtime --entitlements <entitlements.plist> -s "$DEVELOPER_ID" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
```
Entitlements are app-specific: `com.apple.security.cs.allow-jit` +
`allow-unsigned-executable-memory` for JIT runtimes (numba, V8, LuaJIT, …);
`disable-library-validation` to load third-party-signed bundled dylibs.

## Notarize → log → staple

```bash
xcrun notarytool submit "$DMG" --keychain-profile <notary-profile> --wait
xcrun stapler staple "$DMG" && xcrun stapler staple "$APP"
spctl -a -vvv "$APP"   # want: accepted, source=Notarized Developer ID
```
**Run the wait in the background** so a killed local poller doesn't lose the
verdict — Apple processes server-side regardless. Resume any submission with
`xcrun notarytool wait <id> --keychain-profile <notary-profile>`.

**Always pull the full log**, even on success — it surfaces the *exact* cause of an
`Invalid` (per-file signing errors, or status codes like `7000` = team not
configured for notarization) instead of a bare verdict:
```bash
xcrun notarytool log <submission-id> --keychain-profile <notary-profile>
```

## If it comes back `Invalid`

Normal and diagnosable — do NOT just retry. Pull the log (above), fix the cause
(usually the Mach-O-by-type signing), then **re-sign the EXISTING bundle — don't
rebuild.** When only signing was wrong the bundle is fine, so skip the slow freeze:
re-run the sign loop, re-sign the `.app` with entitlements, rebuild the `.dmg`
around it, resubmit.

## GOTCHA: a new Developer ID's first submissions are slow

A *first* submission from a freshly-created Developer ID is routinely held by Apple
for in-depth analysis — stuck `In Progress` for **hours** (forum-confirmed; the
status page still shows Notary as up). It is NOT a package problem; your build is
signed correctly. Wait it out (or `notarytool wait` with a long `--timeout`); don't
resubmit repeatedly — extra submissions just join the same slow queue.

## Success looks like

- `notarytool` status **Accepted**; `stapler staple` succeeds on both `.dmg` and `.app`
- `spctl -a -vvv "$APP"` → **accepted**, `source=Notarized Developer ID`

---

## Applying it here (music-chop)

- Repo `~/code/dv2mv`; **`packaging/build_macos.sh`** runs the whole pipeline (icns
  → PyInstaller freeze → sign-all-Mach-O → dmg → notarize+log → staple), gated on
  env vars:
  ```bash
  cd ~/code/dv2mv
  DEVELOPER_ID="$DEVELOPER_ID" NOTARY_PROFILE=dv2mv-notary bash packaging/build_macos.sh
  ```
- Notary profile **`dv2mv-notary`**; app `dist/dv2mv.app` → `dist/dv2mv-<ver>.dmg`.
- The extensionless CLIs the by-type signing catches: `ffmpeg`, `ffprobe`,
  `rubberband` (under `Contents/Frameworks/bin/`).
- Entitlements `packaging/entitlements.plist`: `allow-jit` +
  `allow-unsigned-executable-memory` (numba) + `disable-library-validation`
  (bundled ffmpeg/rubberband dylibs). Build deps:
  `pip install -r packaging/requirements-build.txt`.
- After `Accepted`: commit only `packaging/build_macos.sh` (explicit `git add`,
  never `-A`) to `main`; the push fans out to both remotes (sr.ht canonical +
  GitHub mirror) and triggers CI. The `.dmg` is a gitignored `dist/` artifact —
  ship it via a release, don't commit it.
