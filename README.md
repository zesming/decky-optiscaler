# Decky OptiScaler — PBZ experiment

A fork of [Decky OptiScaler](https://github.com/danielcamilo1/decky-optiscaler) for experimenting with **Phantom Blade Zero + FSR 4.1 on Steam Deck**. It manages per-game files, configuration, launch options and restoration from Decky Loader. It is independent of the OptiScaler project.

This experimental package contains:

- Official **OptiScaler 0.9.5-pre5** core from a fixed upstream build, with the PBZ compatibility fixes.
- The stable support libraries and a verified community **FSR 4.1.1b INT8 RDNA2 fix** archive for offline setup.
- A PBZ setup profile for the main `PBZ/platform/steam/PBZ.exe`, with visible 100% output-scale and other manual requirements.
- An explicit Steam Deck / LSFG experiment which disables OptiScaler FG and leaves lsfg-vk separate.
- A native live-control bridge pinned to this exact core, with per-game capabilities and confirmed command results.
- Original-file backups, an upgrade restore point, precise launch-options merging, and Remove support for new experiment files.

The core and support libraries are a pinned repack, not an upstream full 0.9.5 release. Live control requires the matching core and bridge installed into each game. Saving an INI is separate from submitting a live configuration request; actual runtime state is reported separately, and model/provider and other startup settings still require a restart. Existing old/unknown runtime installs are read-only until explicitly upgraded in Setup. See the [live-control guide](docs/live-control.md) for connection and recovery states. Device evidence is separate from image-quality, performance and long-session claims.

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
python3 scripts/test_live.py
python3 scripts/test_live_rpc.py
python3 scripts/live_metadata.py --check
HARNESS=run.tsx node testharness/build-and-run.mjs
HARNESS=all-options.tsx node testharness/build-and-run.mjs
HARNESS=live-fields.tsx node testharness/build-and-run.mjs
HARNESS=use-config.tsx node testharness/build-and-run.mjs
HARNESS=launch-options.tsx node testharness/build-and-run.mjs
HARNESS=recovery-ui.tsx node testharness/build-and-run.mjs
python3 scripts/package.py
python3 scripts/verify_package.py 'out/Decky OptiScaler.zip'
```

The native bridge uses the fixed pre5 source references and PDB-backed profile. Rebuilding requires **Zig 0.15.2**: run `bash asi/build.sh` before packaging, then `python3 scripts/live_metadata.py --check`. Windows CI compiles and runs native behavior tests and checks that rebuilding reproduces the shipped binary metadata. Do not copy a bridge from another OptiScaler build into a game.

## Credits

- [Original Decky plugin](https://github.com/danielcamilo1/decky-optiscaler) by its original authors.
- [OptiScaler](https://github.com/optiscaler/OptiScaler) and its wiki contributors.
- [fsr4xyz](https://github.com/the3rdparty1917/fsr4xyz) community RDNA2 fix.
- [Decky Framegen](https://github.com/xXJSONDeruloXx/Decky-Framegen) and [Decky LSFG-VK](https://github.com/xXJSONDeruloXx/decky-lsfg-vk) for the upstream deployment/UI references.

This fork's changes were developed with Codex. Automated synthetic validation is reported separately from physical device testing.
