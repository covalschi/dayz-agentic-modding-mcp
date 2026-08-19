"""Locating the game and the tools.

Order: explicit argument -> environment variable -> Steam registry -> every Steam
library. A candidate counts only if the probe file is actually inside it, so a
leftover empty folder never wins.

Everything is injectable: the discovery logic must be testable on a machine that
has neither Steam nor the game.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

GAME_PROBE = "DayZDiag_x64.exe"
FILEBANK_REL = "Bin/PboUtils/FileBank.exe"
SIGNER_REL = "Bin/DsUtils/DSSignFile.exe"
CFGCONVERT_REL = "Bin/CfgConvert/CfgConvert.exe"
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
