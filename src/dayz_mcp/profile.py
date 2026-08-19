"""Project profile: the only place where a concrete mod is described.

Two files, deliberately:
  dayz-mcp.toml        portable, lives in the mod repository
  dayz-mcp.local.toml  machine-specific, never committed

Merge rule is strict, because a blurred one lets machine paths leak into the
portable half and the repository stops building anywhere else:
  * [project], [build], [expect]              -- portable file only
  * [machine], [mods].required, [mods].extra  -- local file only

One mod is declared once, as a name. The source directory (<root>/Name), the pbo
(Name.pbo) and the built mod folder (@Name) all follow from it.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import Result, fail, ok
from .packer import DEFAULT_EXCLUDE

MAIN_NAME = "dayz-mcp.toml"
LOCAL_NAME = "dayz-mcp.local.toml"


@dataclass
class BuildCfg:
    mods: list[str] = field(default_factory=list)
    pre_script: str = ""
    # What pack_one refuses to ship inside a mod's pbo -- see packer.py's
    # DEFAULT_EXCLUDE for why these three are the default.
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))


@dataclass
class ModsCfg:
    required: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    server_only: list[str] = field(default_factory=list)


@dataclass
class ExpectCfg:
    ready_line: str = ""
    max_warnings: int | None = None
    forbid: list[str] = field(default_factory=list)
    error_regex: list[str] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    noise: list[str] = field(default_factory=list)


@dataclass
class MachineCfg:
    game: str = ""
    tools: str = ""
    stand_root: str = ""
    port: int = 2302
    config: str = "serverDZ.cfg"


@dataclass
class Profile:
    name: str
    root: Path
    build: BuildCfg
    mods: ModsCfg
    expect: ExpectCfg
    machine: MachineCfg
    own_mod_dirs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _read_toml(path: Path) -> tuple[dict | None, str]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh), ""
    except tomllib.TOMLDecodeError as exc:
        return None, f"{path.name} is not valid TOML: {exc}"
    except OSError as exc:
        return None, f"{path.name} cannot be read: {exc}"


def load_profile(path: str | Path) -> Result:
    p = Path(path)
    main = p if p.is_file() else p / MAIN_NAME
    root = main.parent

    if not main.exists():
        return fail(
            f"no profile at {main}",
            hint=f"create {MAIN_NAME} in the mod repository root; see dayz-mcp.example.toml",
        )

    raw, err = _read_toml(main)
    if raw is None:
        return fail(err, hint="fix the syntax; TOML strings need double quotes")

    if "machine" in raw:
        return fail(
            f"[machine] found in {MAIN_NAME}",
            hint=f"machine paths belong in {LOCAL_NAME}, which is not committed",
        )

    if "mods" in raw:
        return fail(
            f"[mods] found in {MAIN_NAME}",
            hint=f"required and extra mods are machine-specific and belong in {LOCAL_NAME}",
        )

    notes: list[str] = []

    # Check [project] is a table
    proj = raw.get("project")
    if proj is not None and not isinstance(proj, dict):
        return fail(
            f"[project] must be a table, got {type(proj).__name__}",
            hint="write it as a [section] header, not a bare value",
        )
    name = str((proj or {}).get("name", "")).strip()
    if not name:
        return fail("project.name is empty", hint="give the project a name")

    # Check [build] is a table
    b = raw.get("build")
    if b is not None and not isinstance(b, dict):
        return fail(
            f"[build] must be a table, got {type(b).__name__}",
            hint="write it as a [section] header, not a bare value",
        )
    b = b or {}

    # Check build.mods is a list
    mods_val = b.get("mods", [])
    if not isinstance(mods_val, list):
        return fail(
            f"build.mods must be a list, got {type(mods_val).__name__}",
            hint='write it as mods = ["MyMod"], not a bare value',
        )

    # Check build.exclude is a list
    exclude_val = b.get("exclude", list(DEFAULT_EXCLUDE))
    if not isinstance(exclude_val, list):
        return fail(
            f"build.exclude must be a list, got {type(exclude_val).__name__}",
            hint='write it as exclude = [".git", "*.blend"], not a bare value',
        )

    build = BuildCfg(
        mods=[str(m) for m in mods_val],
        pre_script=str(b.get("pre_script", "")),
        exclude=[str(x) for x in exclude_val],
    )
    if not build.mods:
        return fail(
            "build.mods is empty",
            hint='list the mods to pack, e.g. mods = ["MyMod"]; the source directory, '
                 "the pbo and the @folder all follow from the name",
        )
    for mod in build.mods:
        mod_dir = root / mod
        if not mod_dir.is_dir():
            return fail(
                f"source directory not found for {mod}: {mod_dir}",
                hint="build.mods names directories next to the profile",
            )
        # A folder without its own config.cpp packs into a pbo the engine
        # silently ignores (CfgPatches/CfgMods never register), so nothing
        # ever tells the person who built it that it did nothing.
        if not (mod_dir / "config.cpp").is_file():
            return fail(
                f"{mod}/config.cpp not found",
                hint=f"a mod folder needs its own config.cpp to be packed into anything the "
                     f"engine will load; create {mod_dir / 'config.cpp'}, or remove {mod} "
                     "from build.mods",
            )
    if build.pre_script and not (root / build.pre_script).exists():
        return fail(
            f"pre_script not found: {build.pre_script}",
            hint="the path is relative to the profile directory, or drop the key",
        )

    # Check [expect] is a table
    e = raw.get("expect")
    if e is not None and not isinstance(e, dict):
        return fail(
            f"[expect] must be a table, got {type(e).__name__}",
            hint="write it as a [section] header, not a bare value",
        )
    e = e or {}

    # Check [expect.counters] is a table
    counters_val = e.get("counters")
    if counters_val is not None and not isinstance(counters_val, dict):
        return fail(
            f"[expect.counters] must be a table, got {type(counters_val).__name__}",
            hint="write it as a [expect.counters] section with key = value pairs",
        )

    counters: dict[str, int] = {}
    for key, value in (counters_val or {}).items():
        if not isinstance(value, int) or isinstance(value, bool):
            return fail(
                f"expect.counters.{key} must be an integer, got {value!r}",
                hint="counters are compared numerically against the ready line",
            )
        counters[key] = value

    max_warnings = e.get("max_warnings")
    if max_warnings is not None and (not isinstance(max_warnings, int) or isinstance(max_warnings, bool)):
        return fail("expect.max_warnings must be an integer", hint="drop the key to disable the check")

    expect = ExpectCfg(
        ready_line=str(e.get("ready_line", "")),
        max_warnings=max_warnings,
        forbid=[str(x) for x in e.get("forbid", [])],
        error_regex=[str(x) for x in e.get("error_regex", [])],
        counters=counters,
        noise=[str(x) for x in e.get("noise", [])],
    )
    if not expect.ready_line:
        notes.append("expect.ready_line is empty: readiness cannot be detected, only errors")

    # Validate error_regex patterns
    for i, pattern in enumerate(expect.error_regex):
        try:
            re.compile(pattern)
        except re.error as exc:
            return fail(
                f"expect.error_regex[{i}] is not a valid regular expression: {exc}",
                hint="fix the pattern, or escape the characters you meant literally",
            )

    mods = ModsCfg()
    machine = MachineCfg()
    local = root / LOCAL_NAME
    if local.exists():
        lraw, lerr = _read_toml(local)
        if lraw is None:
            return fail(lerr, hint=f"fix or delete {LOCAL_NAME}")

        # Reject portable sections in local file
        if "project" in lraw:
            return fail(
                f"[project] found in {LOCAL_NAME}",
                hint=f"[project] is portable and belongs in {MAIN_NAME}",
            )
        if "build" in lraw:
            return fail(
                f"[build] found in {LOCAL_NAME}",
                hint=f"[build] is portable and belongs in {MAIN_NAME}",
            )
        if "expect" in lraw:
            return fail(
                f"[expect] found in {LOCAL_NAME}",
                hint=f"[expect] is portable and belongs in {MAIN_NAME}",
            )

        # Check [machine] is a table
        lm = lraw.get("machine")
        if lm is not None and not isinstance(lm, dict):
            return fail(
                f"[machine] must be a table, got {type(lm).__name__}",
                hint="write it as a [section] header, not a bare value",
            )
        lm = lm or {}

        port_val = lm.get("port", 2302)
        if not isinstance(port_val, int) or isinstance(port_val, bool):
            return fail(
                f"machine.port must be an integer, got {port_val!r}",
                hint="use a numeric port, e.g. port = 2302",
            )

        config_val = lm.get("config", "serverDZ.cfg")
        if not isinstance(config_val, str):
            return fail(
                f"machine.config must be a string, got {config_val!r}",
                hint='use a filename, e.g. config = "serverDZ.cfg"',
            )

        machine = MachineCfg(
            game=str(lm.get("game", "")),
            tools=str(lm.get("tools", "")),
            stand_root=str(lm.get("stand_root", "")),
            port=port_val,
            config=config_val,
        )

        # Check [mods] is a table
        lmods = lraw.get("mods")
        if lmods is not None and not isinstance(lmods, dict):
            return fail(
                f"[mods] must be a table, got {type(lmods).__name__}",
                hint="write it as a [section] header, not a bare value",
            )
        lmods = lmods or {}
        mods = ModsCfg(
            required=[str(x) for x in lmods.get("required", [])],
            extra=[str(x) for x in lmods.get("extra", [])],
            server_only=[str(x) for x in lmods.get("server_only", [])],
        )
    else:
        notes.append(f"no {LOCAL_NAME}: machine paths will be discovered automatically")

    own = [f"@{m}" for m in build.mods]
    return ok(Profile(name, root, build, mods, expect, machine, own, notes))
