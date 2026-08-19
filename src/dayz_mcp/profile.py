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

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import Result, fail, ok

MAIN_NAME = "dayz-mcp.toml"
LOCAL_NAME = "dayz-mcp.local.toml"


@dataclass
class BuildCfg:
    mods: list[str] = field(default_factory=list)
    pre_script: str = ""


@dataclass
class ModsCfg:
    required: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)


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

    notes: list[str] = []
    name = str(raw.get("project", {}).get("name", "")).strip()
    if not name:
        return fail("project.name is empty", hint="give the project a name")

    b = raw.get("build", {})
    build = BuildCfg(
        mods=[str(m) for m in b.get("mods", [])],
        pre_script=str(b.get("pre_script", "")),
    )
    if not build.mods:
        return fail(
            "build.mods is empty",
            hint='list the mods to pack, e.g. mods = ["MyMod"]; the source directory, '
                 "the pbo and the @folder all follow from the name",
        )
    for mod in build.mods:
        if not (root / mod).is_dir():
            return fail(
                f"source directory not found for {mod}: {root / mod}",
                hint="build.mods names directories next to the profile",
            )
    if build.pre_script and not (root / build.pre_script).exists():
        return fail(
            f"pre_script not found: {build.pre_script}",
            hint="the path is relative to the profile directory, or drop the key",
        )

    e = raw.get("expect", {})
    counters: dict[str, int] = {}
    for key, value in (e.get("counters", {}) or {}).items():
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

    mods = ModsCfg()
    machine = MachineCfg()
    local = root / LOCAL_NAME
    if local.exists():
        lraw, lerr = _read_toml(local)
        if lraw is None:
            return fail(lerr, hint=f"fix or delete {LOCAL_NAME}")
        lm = lraw.get("machine", {})
        machine = MachineCfg(
            game=str(lm.get("game", "")),
            tools=str(lm.get("tools", "")),
            stand_root=str(lm.get("stand_root", "")),
        )
        lmods = lraw.get("mods", {})
        mods = ModsCfg(
            required=[str(x) for x in lmods.get("required", [])],
            extra=[str(x) for x in lmods.get("extra", [])],
        )
    else:
        notes.append(f"no {LOCAL_NAME}: machine paths will be discovered automatically")

    own = [f"@{m}" for m in build.mods]
    return ok(Profile(name, root, build, mods, expect, machine, own, notes))
