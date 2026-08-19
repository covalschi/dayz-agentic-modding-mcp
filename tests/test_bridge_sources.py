"""Structural check on the bridge mod's sources (bridge/).

A unit test cannot compile Enforce Script -- the game is the only compiler
there is. What it CAN catch is the failure mode that otherwise shows up only
as silence at boot: config.cpp missing a required block, or a scriptModule
files[] entry pointing at a directory that does not exist. Get either wrong
and the engine registers nothing and logs nothing -- see task-1-brief.md
step 2.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"
CONFIG = BRIDGE / "config.cpp"


def _config_text() -> str:
    return CONFIG.read_text(encoding="utf-8")


def _mod_name(text: str) -> str:
    m = re.search(r"class\s+CfgPatches\s*\{\s*class\s+(\w+)", text)
    assert m, "CfgPatches does not declare a mod class"
    return m.group(1)


def test_bridge_config_exists():
    assert CONFIG.is_file(), f"{CONFIG} is missing -- the bridge mod has no config.cpp"


def test_bridge_config_declares_cfgpatches_and_cfgmods():
    text = _config_text()
    assert "CfgPatches" in text, "config.cpp does not declare CfgPatches -- the engine will not load this pbo at all"
    assert "CfgMods" in text, "config.cpp does not declare CfgMods -- no scriptModule can be registered"


def test_cfgmods_class_matches_cfgpatches_class():
    """CfgPatches and CfgMods must name the SAME class: that name is both what
    requiredAddons[] chains other mods against and the folder FileBank's
    prefix= property packs everything under."""
    text = _config_text()
    mod_name = _mod_name(text)
    _, _, after_cfgmods = text.partition("class CfgMods")
    assert after_cfgmods, "config.cpp has no CfgMods block"
    assert re.search(rf"class\s+{re.escape(mod_name)}\b", after_cfgmods), (
        f"CfgMods does not declare a class named {mod_name!r} (the CfgPatches class name) "
        "-- engine cannot associate mod metadata with the patch"
    )


def test_declared_script_module_directory_exists_with_at_least_one_c_file():
    """Every defs.*ScriptModule files[] entry in config.cpp names a PBO-internal
    path: FileBank's -property prefix=<mod name> (applied to bridge/ as a
    whole) means the real filesystem directory is that same path with the
    leading "<mod name>/" stripped. A wrong path here is exactly the typo
    this test exists to catch -- it packs fine and boots silently with the
    module simply absent."""
    text = _config_text()
    entries = re.findall(r'files\[\]\s*=\s*\{\s*"([^"]+)"\s*\}', text)
    assert entries, "config.cpp declares no scriptModule files[] entries at all"
    mod_name = _mod_name(text)

    for entry in entries:
        assert entry.startswith(mod_name + "/"), (
            f"files[] entry {entry!r} does not start with {mod_name!r}/ -- FileBank packs "
            "bridge/ whole under that prefix, so every declared path must repeat it"
        )
        rel = entry[len(mod_name) + 1:]
        module_dir = BRIDGE / rel
        assert module_dir.is_dir(), f"{module_dir} is declared in config.cpp but does not exist"
        c_files = list(module_dir.rglob("*.c"))
        assert c_files, f"{module_dir} exists but contains no .c file -- an empty module compiles to nothing"
