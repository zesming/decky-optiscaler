# Steam Deck FSR 4.1

Use the [PBZ guide](phantom-blade-zero.md) for installation, required game settings, validation and complete restoration.

The stock support bundle contains the FidelityFX SDK 4.1.1 DLL. Its file presence does not prove the game is running FSR4 or the intended model. This beta also bundles the separately verified community **4.1.1b INT8 RDNA2 ghosting fix**, with its identity determined by SHA-256 rather than the unchanged version resource.

The new core reads `FSR.Fsr4ForceEnableInt8` as a model number: `0` disabled, `1` generic INT8, `2` native Navi2 INT8, `3` Lite. The pinned community preset uses **1**. Merely writing 2 does not add a missing native Navi2 model to an old DLL. Legacy true/false values are migrated only during runtime upgrade, preserving the rest of the INI; an explicit rollback restores the original bytes.

On Steam Deck, select **FSR 4.1.1b — Steam Deck** in **Settings → Basic** to use the bundled RDNA2 preset. Frame-generation settings remain user-controlled. Restoring the stock FSR DLL does not restore the whole setup; use **Restore previous install** for that, or **Remove** to restore the earliest original game files and recorded launch options.

The [matching live bridge](live-control.md) can apply supported per-frame settings, backends and available FFX-version changes after a confirmed game response. Model/provider selection, INT8 and loader changes still require restarting the game. FSR4 file identity and saved settings alone do not prove the active model.
