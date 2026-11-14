# Phantom Blade Zero on Steam Deck: experimental FSR 4.1

This build is a test package, not a verified game compatibility result. It bundles an official fixed OptiScaler **0.9.5-pre5** core and the community **FSR 4.1.1b INT8 RDNA2 fix**. Neither the maintainers' Windows result nor the plugin's synthetic tests prove Steam Deck performance or stability.

## Set up

1. Close the game. Install this Decky zip and open the game's **Setup** page.
2. The real target is the directory containing the main/big `PBZ.exe`: normally `PBZ/platform/steam`. Do not use the usual Unreal `Binaries/Win64` location. The plugin prefers the canonical folder when it exists and there is no saved or existing installation. If an older install is in the wrong folder, use **Remove** there first, then select the main executable folder in Manual setup.
3. Run setup to upgrade the game runtime. Installing a new Decky package alone does not change any game. Existing managed installs get a restore point before upgrade. Launch options are merged with existing wrappers and arguments, including your lsfg-vk setup; an unreadable launch-options field must be resolved before automatic replacement.
4. Choose the explicit **Steam Deck / LSFG experiment** action in Setup. It uses the verified community upscaler, sets `Fsr4ForceEnableInt8=1`, selects the FidelityFX backend, and disables OptiScaler FG (`Enabled=false`, `FGInput=nofg`, `FGOutput=nofg`). The archive is bundled, so this preset can be applied offline. Normal wiki reapply preserves the experiment's FG selection.
5. In the game, keep **Super Resolution Output Scale = 100%**. Change only the upscaler quality preset. Start with FSR 3.1 input, dynamic resolution off, game FG off and lsfg-vk off. First confirm the game starts and upscaling initializes; then enable your separate lsfg-vk configuration.

The setup notes stay visible even without a network connection. This package does not copy NVIDIA DLLs out of the game or edit Engine.ini. The core contains the upstream PBZ extended DLL search and input quirks.

## Verify in game

Use a recent Proton build with the VKD3D support required by FSR 4.1. Record the exact SteamOS, Mesa and Proton versions when reporting results; the plugin does not install or change them. The baseline path here is DX12 through Proton, not the experimental Vulkan-with-DX12 interop mode.

The displayed DLL identity and saved settings show **configured**, not **active**. Check `OptiScaler.log` or the OptiScaler overlay for the created FidelityFX upscaler and INT8 model. The file version alone cannot distinguish stock 4.1.1 from patched 4.1.1b, which is why the plugin checks SHA-256. Compare the same scene with all FG off, and only then with lsfg-vk on. Keep base FPS separate from generated/displayed FPS. There is no promised FPS improvement.

The Decky live ASI is deliberately unavailable: its internal memory layout is pinned to 0.9.4. This beta never installs it, sends live commands, or trusts its old heartbeat. Settings apply after restarting the game. Unknown same-name ASIs or unresolved custom plugin paths block an upgrade rather than being silently deleted.

## Restore everything

Close the game before either operation:

- **Restore previous install** restores the immutable pre-experiment snapshot: the previous core, INI bytes/comments, selected community DLL, owned components, original backup inventory, related per-game settings, and the launch options captured at that point. It removes new owned files absent from that snapshot. The restore point remains until Remove, so a failed Steam setter can be retried. After reverting to an old or unknown runtime, the new schema is read-only until you explicitly upgrade again.
- **Remove** completely removes the managed installation and newly introduced snapshot files, then restores the earliest original game files and the recorded original launch options through the existing removal prompt. Keeping the plugin's launch options is still an explicit choice; select the restore option when you want complete restoration. New experiments never replace the oldest original backups.

A failed file/configuration commit must leave the pre-operation files and manifest intact; failed operations are not presented as complete. An original INI already lost by an older plugin cannot be reconstructed: the upgrade snapshot protects the INI that actually exists now, and this distinction must not be confused with recovering a file lost before this build was installed. Unrelated game/user files are not part of the restore inventory.

## Pinned sources

- [PBZ compatibility page](https://github.com/optiscaler/OptiScaler/wiki/Phantom-Blade-Zero): requires 0.9.5+, special folder and 100% output scale; tested on Windows 11 / RX 9070XT.
- [Official build run](https://github.com/optiscaler/OptiScaler/actions/runs/43116305456), source commit `6cbd89b887f5f186c294e2fe2b3024692ea21007`, artifact `13898752558`. Its raw archive SHA-256 is `3835c3a80847bae2a9bb5d873a42e43d7cf504b172650bb54b7659b67e7fdf27`; the core PE version is `0.9.5.5`. This exact prerelease includes the PBZ fixes; this does not mean arbitrary prereleases satisfy a final-version requirement.
- [Stable 0.9.4 support bundle](https://github.com/optiscaler/OptiScaler/releases/tag/v0.9.4): the core-only artifact does not contain SDK dependencies. Its existing FFX SDK 2.2 / stock 4.1.1 and other support files are retained. DX12 export/structure compatibility was inspected; the combined Proton/game path remains unverified. This is a repacked Decky payload, not an upstream full 0.9.5 release.
- [Community 4.1.1b](https://github.com/the3rdparty1917/fsr4xyz/releases/tag/4.1.1b): archive and extracted DLL are independently hash checked. No claim of a native Navi2/FP8 model is made.

Exact hashes and sizes of every payload input are in `defaults/runtime-manifest.json`. The old ABI references remain in source for future work; the old compiled ASI is excluded from the zip.

Community reports describe 0.9.4 freezes and conflicting installation locations, often with FG involved. Those are troubleshooting leads, not Steam Deck validation. The official per-game page takes precedence over those recipes. If the experiment still hangs, restore first and share the game's OptiScaler log plus the precise environment versions; do not stack more undocumented tweaks.
