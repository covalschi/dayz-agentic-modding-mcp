"""Locating the game, the tools and Blender.

Order: explicit argument -> environment variable -> Steam registry -> every Steam
library. A candidate counts only if the probe file is actually inside it, so a
leftover empty folder never wins.

Everything is injectable: the discovery logic must be testable on a machine that
has neither Steam nor the game.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

GAME_PROBE = "DayZDiag_x64.exe"
FILEBANK_REL = "Bin/PboUtils/FileBank.exe"
SIGNER_REL = "Bin/DsUtils/DSSignFile.exe"
CFGCONVERT_REL = "Bin/CfgConvert/CfgConvert.exe"
BANKREV_REL = "Bin/PboUtils/BankRev.exe"
IMAGETOPAA_REL = "Bin/ImageToPAA/ImageToPAA.exe"
BINARIZE_REL = "Bin/Binarize/binarize.exe"
#: What `-binpath=` wants: the folder that CONTAINS a `bin` directory holding
#: the main config, NOT that `bin` directory itself. Measured, because the
#: difference is the entire effect -- pointed at `.../Binarize/bin` the switch
#: changed not one line of a real build's log, and pointed at `.../Binarize` it
#: removed 25 of 112.
BINARIZE_BINPATH_REL = "Bin/Binarize"
TOOLS_PROBE = FILEBANK_REL


def pick(candidates, probe: str, exists=os.path.exists) -> str | None:
    for c in candidates:
        if not c:
            continue
        if exists(str(Path(c) / probe)):
            return str(c)
    return None


def _registry_steam_path() -> str | None:
    try:
        import winreg  # noqa: PLC0415 - Windows only, imported lazily
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            return str(winreg.QueryValueEx(key, "SteamPath")[0]).replace("/", "\\")
    except OSError:
        return None


def steam_libraries(registry=_registry_steam_path, read_text=None) -> list[str]:
    if read_text is None:
        def read_text(p):  # noqa: ANN001
            return Path(p).read_text(encoding="utf-8", errors="ignore")

    roots: list[str] = []
    base = registry()
    if base:
        roots.append(base)
    for guess in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if guess:
            roots.append(str(Path(guess) / "Steam"))

    libs = list(roots)
    for root in roots:
        vdf = Path(root) / "steamapps" / "libraryfolders.vdf"
        try:
            text = read_text(str(vdf))
        except OSError:
            continue
        for m in re.finditer(r'"path"\s*"([^"]+)"', text):
            libs.append(m.group(1).replace("\\\\", "\\"))
    return libs


def _candidates(explicit: str, env_name: str, leaf: str) -> list[str]:
    out = [explicit, os.environ.get(env_name, "")]
    out += [str(Path(lib) / "steamapps" / "common" / leaf) for lib in steam_libraries()]
    return out


def find_game(explicit: str = "") -> str | None:
    return pick(_candidates(explicit, "DAYZ_ROOT", "DayZ"), GAME_PROBE)


def find_tools(explicit: str = "") -> str | None:
    return pick(_candidates(explicit, "DAYZ_TOOLS", "DayZ Tools"), TOOLS_PROBE)


#: Blender's executable, and the folder its installers put versioned installs in.
BLENDER_LEAF = "blender.exe" if os.name == "nt" else "blender"
BLENDER_VENDOR_DIR = "Blender Foundation"
_BLENDER_VERSION = re.compile(r"(\d+)\.(\d+)")


def _version_key(name: str) -> tuple[int, int]:
    """"Blender 5.2" -> (5, 2), so a machine with several installs gets the
    newest rather than the lexicographically last -- "Blender 10.0" sorts
    before "Blender 5.2" as text, and that is the wrong install."""
    matched = _BLENDER_VERSION.search(name)
    return (int(matched.group(1)), int(matched.group(2))) if matched else (0, 0)


def blender_candidates(explicit: str = "", which=shutil.which) -> list[str]:
    """Every place Blender might be, best first.

    Unlike the game and the tools, Blender is not a Steam application here and
    has no registry key this server may rely on, so the search is: what the
    profile declares, what the environment declares, what is on PATH, and then
    the versioned install folders, newest first.
    """
    out = [explicit, os.environ.get("BLENDER_EXE", "")]
    on_path = which("blender")
    if on_path:
        out.append(str(on_path))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        vendor = Path(base) / BLENDER_VENDOR_DIR if base else None
        if vendor is None or not vendor.is_dir():
            continue
        installs = [d for d in vendor.iterdir() if d.is_dir()]
        for install in sorted(installs, key=lambda d: _version_key(d.name), reverse=True):
            out.append(str(install / BLENDER_LEAF))
    return out


def find_blender(explicit: str = "", exists=os.path.isfile, which=shutil.which) -> str | None:
    """The Blender executable, or None. The path to the EXE, not to a folder:
    unlike DayZ Tools there is no stable layout under an install root worth
    probing for, and the thing every caller wants is the program itself."""
    for candidate in blender_candidates(explicit, which):
        if candidate and exists(candidate):
            return candidate
    return None
