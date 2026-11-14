# PBZ Steam Deck validation — 2026-11-14

Recovery implementation: `96e21ff`; PBZ identity follow-up: `509fcf7`; package `0.0.8-pbz.1`, OptiScaler `0.9.5-pre5` (core PE `0.9.5.5`), community FSR `4.1.1b` INT8 model `1`. OptiScaler FG is disabled. This is an experimental result for one device, not a general compatibility certification.

## Automated validation

- Backend selftest: 401 passed, 0 failed. Historical positive live-ASI checks are unavailable for this beta; the old ABI reference remains separately pinned and its compiled binary is excluded.
- FSR4 lifecycle: 15 passed. Recovery regressions: 58 passed.
- UI harnesses: main flow, all options, live fields, shared config queues, shell launch options, and recovery UI all passed. The shell tests execute controlled fixtures to inspect the effective Wine environment. The main and recovery UI harnesses were rerun after the PBZ identity follow-up, including a delayed wiki response and an explicitly pinned wrong-folder install.
- TypeScript, Rollup, generated schema/layout consistency, package inventory/hash checks and final-ZIP offline install/experiment/reapply/stock restore/rollback/Remove passed.
- Test runs did not change repository Git configuration.

## Device and deployment

Steam Deck OLED 1 TB; SteamOS 3.9.5 stable, build 20261023.100; kernel 7.2.7-valve; Decky Loader 3.3.1. Actual PBZ process uses Proton Experimental (prefix version 11.0-100). Mesa reports 26.1.2; the installed RADV package is a SteamOS 26.2 development build. These were inspected, not installed or upgraded by this work.

The existing OptiScaler plugin was replaced through Decky's normal authenticated install API. One active `Decky OptiScaler 0.0.8-pbz.1` instance remains. Existing LSFG-VK 0.14.4 and other plugins were left in place. Other games' files were not upgraded. The original plugin/settings, exact Steam launch options, relevant PBZ profile/configuration files and game-file hashes are backed up privately on the device.

The old saved PBZ target pointed at auxiliary Unreal executables. The tested target is the actual main `PBZ.exe` directory, `PBZ/platform/steam`. Initial installation used the captured current Steam value in the same managed transaction. The tested merge was byte-identical to that value; the launch options were not effectively changed. A persistent restore point was created before the explicit community INT8 experiment.

Package SHA-256: `e4d9455064e05cc77c444304339c5c9c5db49ebff77d0b32e2f5cf2ee2ab51c9` (73,642,857 bytes). It was extracted and checked offline before deployment.

## Runtime observations and limits

The unmodified baseline and experimental build both reached the main menu after shader compilation. Cache state differs, so startup duration is not a performance comparison. The experimental log loads the pinned community DLL, installs the INT8 support/model hooks and lists upscaler index 0 as `4.1.1b` (`F5A5CA1E01001001`). New FFX contexts were created. Earlier `Depth not exist` messages need to be interpreted against the actual input and scene; a loaded DLL or provider list alone does not prove successful per-frame upscaling.

During inspection the game menu was temporarily changed from XeSS to FSR; output scaling and other quality settings were not changed by the agent. The user then took control and requested log-only observation. No further UI input or disruptive operation is performed during that handoff. The plugin does not edit the game's external graphics profile.

After the user exited the first run and switched to Gaming Mode, the frontend loaded one instance of `0.0.8-pbz.1`, the existing Decky WebSocket and plugin RPC remained usable, and the Decky service restart count remained zero. The user then launched PBZ again. That run loaded the pinned core and community DLL, recorded INT8 mode `1`, and created two FFX upscaler contexts. No `Depth not exist` messages were present in the observed second-run log. Two startup errors remained: model selection was not found in a second module, and the NVIDIA NGX OTA updater was unavailable. These are recorded without claiming that all render paths have been verified.

The user completed a gameplay session and exited before recovery testing. Positive per-frame render/dispatch evidence and measured performance remain unverified. No FPS improvement or long-session stability claim is made.

## Physical recovery and final state

The installed plugin's real RPCs performed snapshot rollback, full Remove and reinstallation, with the game closed. Rollback restored all 38 snapshot candidate records, including file bytes, permissions and absent paths; the saved per-game preferences and exact Steam launch string matched. The immutable point remained available for retry.

Before Remove, the earliest recorded Steam launch string was restored and verified. After Remove, all three original top-level game files (499,345,640 bytes total) matched their original SHA-256, sizes and modes. No managed files, support directories, launch record, manifest or snapshot remained. The game's separate `D3D12` directory was preserved byte-for-byte with its modes; the plugin-owned directory is `D3D12_Optiscaler`. The initial verification mistakenly treated every remaining directory as residue; the corrected check distinguishes game-owned and plugin-owned paths.

The experiment was then recreated through the plugin. All 18 managed file hashes and modes, including exact INI bytes, matched the pre-test state. All plugin settings, the current Steam launch string and all 21 current profile/save files matched their pre-test values. The fresh restore point is available, and the earliest original-game preference lineage remains intact. Community `4.1.1b`, INT8 mode `1` and OptiScaler FG-off remain configured; the game is left closed.

The final package containing `509fcf7` was then installed in place. The deployed backend source hash matched the checkout, the new identity field was returned by the real RPC, and one frontend instance remained among the same 11 plugins with its existing WebSocket connected. Decky stayed active with zero service restarts. Managed files, exact INI, all plugin settings, Steam launch string and the current profile/save inventory were checked again and remained identical.

PBZ-specific rules are isolated from generic games. Supplementary regressions cover explicitly choosing the PBZ wiki entry for a renamed shortcut, a missing cached row, a game-folder alias without optional lookup names, and a fuzzy result that must not authorize the special profile. The synchronous backend identity keeps UI guards active while the wiki lookup is pending; wrong-folder installs remain removable. Ordinary games keep their FG changes even when another game has the PBZ experiment enabled. The PR is to remain a draft until the official 0.9.5 release can be repinned and validated; no automatic merge is requested.

Raw logs, screenshots, device addresses, credentials, exact user launch strings and profile/save backups are deliberately absent from this public-facing record.
