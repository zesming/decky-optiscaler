# Decky OptiScaler — PBZ experiment

A fork of [Decky OptiScaler](https://github.com/danielcamilo1/decky-optiscaler) for experimenting with **Phantom Blade Zero + FSR 4.1 on Steam Deck**. It manages per-game files, configuration, launch options and restoration from Decky Loader. It is independent of the OptiScaler project.

This experimental package contains:

- Official **OptiScaler 0.9.5-pre5** core from a fixed upstream build, with the PBZ compatibility fixes.
- The stable support libraries and a verified community **FSR 4.1.1b INT8 RDNA2 fix** archive for offline setup.
- A PBZ setup profile for the main `PBZ/platform/steam/PBZ.exe`, with visible 100% output-scale and other manual requirements.
- An explicit Steam Deck / LSFG experiment which disables OptiScaler FG and leaves lsfg-vk separate.
- Original-file backups, an upgrade restore point, precise launch-options merging, and Remove support for new experiment files.

**Steam Deck gameplay, image quality and performance still require device testing.** The core and support libraries are a pinned repack, not an upstream full 0.9.5 release. The bundled old live ASI has been removed because its memory layout does not match the new runtime. Settings apply after restarting the game. Existing old/unknown runtime installs are read-only until explicitly upgraded in Setup.

See [PBZ setup, verification and complete recovery](docs/phantom-blade-zero.md) before testing. The existing [FSR 4 guide](docs/steam-deck-fsr4.md) explains the stock/community distinction.

## Install and recover

Install the generated zip through Decky Developer mode's **Install Plugin from URL**, or unpack its single `Decky OptiScaler/` directory into `~/homebrew/plugins/` and restart Decky Loader. Then open the game's Setup; updating Decky alone does not upgrade games.

Close the game before installation, upgrade, binary changes, rollback or removal. On an existing managed install, Setup saves a pre-experiment restore point. **Restore previous install** restores that state, including the captured launch options and related per-game preferences. **Remove** restores the earliest original game files and offers the original launch options. Use the restore choice for full removal. Failed Steam writes remain visible and retryable. Originals lost before this build cannot be recreated.

## Build and validate

Requires Node 22, pnpm 10, Python 3.11+ and a BCJ2-capable `7z`, `7zz`, `7za`, `7zr` or `bsdtar` (SteamOS includes supported extractors).

```sh
pnpm install --frozen-lockfile
python3 scripts/generate_schema.py
pnpm exec tsc --noEmit
pnpm build
python3 scripts/selftest.py
python3 scripts/test_fsr4.py
python3 scripts/test_recovery.py
HARNESS=run.tsx node testharness/build-and-run.mjs
HARNESS=all-options.tsx node testharness/build-and-run.mjs
HARNESS=live-fields.tsx node testharness/build-and-run.mjs
HARNESS=use-config.tsx node testharness/build-and-run.mjs
HARNESS=launch-options.tsx node testharness/build-and-run.mjs
HARNESS=recovery-ui.tsx node testharness/build-and-run.mjs
python3 scripts/package.py
python3 scripts/verify_package.py 'out/Decky OptiScaler.zip'
```

The archived 0.9.4 ASI references are retained for independent development; `asi/build.sh` is not a supported way to enable live control for this beta. Do not copy an old ASI into the upgraded game.

## Credits

- [Original Decky plugin](https://github.com/danielcamilo1/decky-optiscaler) by its original authors.
- [OptiScaler](https://github.com/optiscaler/OptiScaler) and its wiki contributors.
- [fsr4xyz](https://github.com/the3rdparty1917/fsr4xyz) community RDNA2 fix.
- [Decky Framegen](https://github.com/xXJSONDeruloXx/Decky-Framegen) and [Decky LSFG-VK](https://github.com/xXJSONDeruloXx/decky-lsfg-vk) for the upstream deployment/UI references.

This fork's changes were developed with Codex. Automated synthetic validation is reported separately from physical device testing.
