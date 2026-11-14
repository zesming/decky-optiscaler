#!/usr/bin/env python3
"""Package the plugin into a Decky-installable zip.

Mirrors the layout `decky plugin build` produces: a single top-level directory
named after plugin.json's "name", holding the runtime files only.
"""

import json
import shutil
import stat
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "out"

# Files and directories that make up the installed plugin.
INCLUDE_FILES = ["plugin.json", "package.json", "main.py", "README.md", "LICENSE",
                 "docs/phantom-blade-zero.md", "docs/steam-deck-fsr4.md"]
INCLUDE_DIRS = ["dist", "py_modules", "bin", "defaults"]

EXCLUDE_NAMES = {"__pycache__", ".DS_Store", "node_modules", ".git"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".map", ".tmp"}


def keep(path: Path) -> bool:
    if any(part in EXCLUDE_NAMES for part in path.parts):
        return False
    return path.suffix not in EXCLUDE_SUFFIXES


def collect():
    entries = []
    for name in INCLUDE_FILES:
        path = ROOT / name
        if path.is_file():
            entries.append((path, Path(name)))
        else:
            print(f"  note: {name} not found, skipping")
    for name in INCLUDE_DIRS:
        base = ROOT / name
        if not base.is_dir():
            print(f"  note: {name}/ not found, skipping")
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and keep(path.relative_to(ROOT)):
                entries.append((path, path.relative_to(ROOT)))
    return entries


# Keep only the two pinned archives and the optional OptiPatcher component;
# extracted trees and the retired live-control ASI must never enter the zip.
ALLOWED_BIN_SUFFIXES = (".7z", ".asi")


def check_bin():
    """Reject extracted payloads and unexpected component types."""
    bin_dir = ROOT / "bin"
    if not bin_dir.is_dir():
        return
    strays = [
        p for p in bin_dir.iterdir()
        if p.is_dir()
        or (p.is_file() and p.suffix not in ALLOWED_BIN_SUFFIXES and p.name != ".DS_Store")
    ]
    if strays:
        sys.exit(
            "bin/ contains unexpected files: "
            + ", ".join(p.name for p in strays)
            + "\nRemove them (they are probably an extracted payload) and re-run."
        )

    asi = bin_dir / "decky_optiscaler_live.asi"
    if not asi.is_file():
        print("  Live control is disabled: this runtime has no validated Decky ASI ABI.")
    if not (bin_dir / "OptiPatcher.asi").is_file():
        print("  note: bin/OptiPatcher.asi missing -- the OptiPatcher option will be "
              "offered as unavailable. Fetch it from the OptiPatcher rolling release.")


def main():
    plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    name = plugin["name"]
    version = package.get("version", "0.0.0")
    check_bin()
    sys.path.insert(0, str(ROOT / "py_modules"))
    from optiscaler.constants import (PAYLOAD_ARCHIVE, PAYLOAD_SHA256, FSR4_BUILDS,
                                      LIVE_CONTROL_SUPPORTED, OPTIPATCHER_SHA256)
    from optiscaler.payload import sha256
    required = {PAYLOAD_ARCHIVE, "OptiPatcher.asi", FSR4_BUILDS[0]["sources"][0]["asset"]}
    actual = {p.name for p in (ROOT / "bin").iterdir() if p.is_file() and p.name != ".DS_Store"}
    if not LIVE_CONTROL_SUPPORTED and "decky_optiscaler_live.asi" in actual:
        sys.exit("The incompatible live ASI must not be shipped in this beta.")
    if actual != required:
        sys.exit(f"Unexpected bin inventory: missing={required-actual}, extra={actual-required}")
    if sha256(ROOT / "bin" / "OptiPatcher.asi") != OPTIPATCHER_SHA256:
        sys.exit("OptiPatcher does not match the pinned checksum.")
    if sha256(ROOT / "bin" / PAYLOAD_ARCHIVE) != PAYLOAD_SHA256:
        sys.exit("Runtime archive does not match the pinned checksum.")
    community = FSR4_BUILDS[0]["sources"][0]
    if sha256(ROOT / "bin" / community["asset"]) != community["archive_sha256"]:
        sys.exit("Community archive does not match the pinned checksum.")

    if not (ROOT / "dist" / "index.js").is_file():
        sys.exit("dist/index.js is missing — run `pnpm build` first.")

    OUT_DIR.mkdir(exist_ok=True)
    archive = OUT_DIR / f"{name}.zip"
    if archive.exists():
        archive.unlink()

    entries = collect()
    total = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for source, relative in entries:
            arcname = Path(name) / relative
            info = zipfile.ZipInfo(str(arcname))
            info.date_time = (2026, 1, 1, 0, 0, 0)
            executable = source.stat().st_mode & stat.S_IXUSR
            info.external_attr = (0o755 if executable else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            data = source.read_bytes()
            total += len(data)
            zf.writestr(info, data)

    size = archive.stat().st_size
    print(f"\n{archive}  (version {version})")
    print(f"  {len(entries)} files, {total / 1e6:.1f} MB uncompressed -> {size / 1e6:.1f} MB zip")

    # The bundled release dominates the size; call it out so it is not a surprise.
    payload = next((r for _, r in entries if r.parts[0] == "bin"), None)
    if payload:
        print(f"  includes {payload} (OptiScaler release)")
    return archive


if __name__ == "__main__":
    main()
