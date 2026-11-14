"""Small, source-backed exceptions which must also work without the wiki cache."""

from pathlib import Path
from .constants import OPTISCALER_VERSION
from .wiki import normalize

PBZ_WIKI = "https://github.com/optiscaler/OptiScaler/wiki/Phantom-Blade-Zero"
PBZ_STEPS = [
    "Install beside the main PBZ.exe in PBZ/platform/steam (not the usual Unreal Binaries folder).",
    "Keep the game's Super Resolution Output Scale at 100%; change only the upscaler preset.",
    "For the Steam Deck / LSFG experiment, turn off frame generation in the game and in OptiScaler; configure lsfg-vk separately.",
    "The wiki was tested on Windows 11 / RX 9070XT. Steam Deck / Proton operation is experimental and must be verified in game.",
]


def is_pbz(name):
    value = str(name or "").strip()
    return value == "影之刃零" or normalize(value) in {"phantombladezero", "pbz"}


def target_for(game_path, name=None):
    root = Path(game_path)
    if not (is_pbz(name) or is_pbz(root.name)):
        return None
    candidates = [root / "platform" / "steam", root / "PBZ" / "platform" / "steam"]
    if root.name.lower() == "steam" and root.parent.name.lower() == "platform":
        candidates.append(root)  # Non-Steam shortcuts can already point beside PBZ.exe.
    for candidate in candidates:
        if not candidate.is_dir() or not candidate.resolve().is_relative_to(root.resolve()):
            continue
        if any(p.is_file() and p.name.lower() == "pbz.exe" for p in candidate.iterdir()):
            return str(candidate)
    return None


def recommendation(result, name, extra_names=None):
    """Retain online detail, filling critical PBZ instructions from a reviewed pin."""
    if not any(is_pbz(n) for n in [name, (result or {}).get("game"), *(extra_names or [])]):
        return result
    result = dict(result or {})
    detail = dict(result.get("detail") or {})
    detail.setdefault("Known Issues", "Requires OptiScaler 0.9.5+ and the non-standard PBZ/platform/steam installation folder.")
    detail.setdefault("Notes", "Keep Super Resolution Output Scale at 100%; only change the upscaler preset.")
    detail.setdefault("FG Inputs", "DLSSG via SL")
    result.update(matched=True, game="Phantom Blade Zero", filename="dxgi.dll",
                  filename_source="reviewed wiki profile", wiki_url=PBZ_WIKI,
                  compatibility=result.get("compatibility") or "✅",
                  detail=detail, detail_pending=False, required_version="0.9.5",
                  required_target="PBZ/platform/steam", manual_steps=list(PBZ_STEPS))
    return result


def constrain(plan, result):
    if not is_pbz((result or {}).get("game")):
        return plan
    # This precise pre5 build contains the PBZ ExtendedSearch and input fixes;
    # this is a capability pin, not a claim that every prerelease >= a final.
    supported = OPTISCALER_VERSION == "0.9.5-pre5"
    plan.update(required_version="0.9.5", required_target="PBZ/platform/steam",
                manual_steps=list(PBZ_STEPS), runtime_supported=supported,
                blocked_reason=None if supported else "Phantom Blade Zero requires the pinned 0.9.5+ compatibility fixes. Upgrade this plugin first.")
    return plan
