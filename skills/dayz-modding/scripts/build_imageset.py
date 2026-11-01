# SPDX-License-Identifier: MIT
"""Pack a folder of PNG icons into one atlas and write the DayZ .imageset for it.

Each PNG in SOURCE_DIR becomes one image of the set, named after the file stem.
The icons are shelf-packed, tallest first, into the smallest power-of-two RGBA
atlas that holds them (width and height may differ) on a transparent background,
with a gutter between neighbours. Three files land in --out:

    <name>_ca.png     the atlas
    <name>.imageset   RefSize, the texture path and one Pos/Size entry per icon
    <name>_ca.paa     the atlas converted by DayZ Tools' ImageToPAA

The _ca suffix is what makes ImageToPAA keep the alpha channel. A mod still has
to list the .imageset under CfgMods > defs > imageSets > files[], or its icons
stay blank without any error. Needs Python 3.11+ and Pillow.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("error: Pillow is required: python -m pip install Pillow")

MIN_SIDE = 4  # ImageToPAA compresses in 4x4 blocks
HARD_LIMIT = 1 << 16  # only used to say how big an atlas would have to be
NAME_RE = re.compile(r"[A-Za-z0-9_]+")
TOOLS_ENV = "DAYZ_TOOLS"
IMAGE_TO_PAA = Path("Bin") / "ImageToPAA" / "ImageToPAA.exe"


class PackError(Exception):
    """A problem the user can fix; main() prints it as `error: ...` and exits 1."""


@dataclass
class Icon:
    name: str
    image: Image.Image
    x: int = 0
    y: int = 0

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height


# --- reading the icons -------------------------------------------------------


def load_icons(source: Path, skip: Path | None = None) -> list[Icon]:
    """Every .png directly inside `source` (not subfolders), as RGBA, by file name.

    `skip` is the atlas this run is about to write: when --out is the icon
    folder itself, a rerun must not pack its own previous result.
    """
    files = sorted(p for p in source.iterdir() if p.is_file() and p.suffix.lower() == ".png")
    if skip is not None:
        files = [p for p in files if p.resolve() != skip]
    if not files:
        raise PackError(f"no PNG files in {source}")

    problems: list[str] = []
    seen: dict[str, Path] = {}
    icons: list[Icon] = []
    for path in files:
        name = path.stem
        if not NAME_RE.fullmatch(name):
            problems.append(f"{path.name}: the image name '{name}' may only use letters, digits and underscore")
            continue
        if name.lower() in seen:
            problems.append(f"{path.name}: duplicate image name '{name}' (same as {seen[name.lower()].name})")
            continue
        seen[name.lower()] = path
        try:
            with Image.open(path) as opened:
                opened.load()
                rgba = opened.convert("RGBA")
        except Exception as exc:  # Pillow raises OSError, SyntaxError, ValueError... for bad files
            problems.append(f"{path.name}: cannot be read as a PNG ({exc})")
            continue
        icons.append(Icon(name, rgba))
    if problems:
        raise PackError("\n  ".join([f"{len(problems)} problem(s) in {source}:", *problems]))
    return icons


# --- packing -----------------------------------------------------------------


def _next_pow2(n: int) -> int:
    return 1 << (max(n, 1) - 1).bit_length()


def _shelf(icons: list[Icon], width: int, pad: int) -> tuple[list[tuple[int, int]], int] | None:
    """Lay `icons` (tallest first) on shelves of an atlas `width` px wide.

    Returns the (x, y) of each icon and the height used, or None when an icon
    is too wide for the atlas. `pad` px separate neighbours and also border the
    atlas, so a sprite never touches the wrapped-around opposite edge.
    """
    spots: list[tuple[int, int]] = []
    x = y = pad
    shelf_height = 0
    for icon in icons:
        if icon.width + 2 * pad > width:
            return None
        if x + icon.width + pad > width:  # shelf full: start the next one
            y += shelf_height + pad
            x = pad
            shelf_height = 0
        spots.append((x, y))
        x += icon.width + pad
        shelf_height = max(shelf_height, icon.height)
    return spots, y + shelf_height + pad


def pack(icons: list[Icon], pad: int, limit: int) -> tuple[int, int] | None:
    """Place every icon (sets its x, y) in the smallest power-of-two atlas.

    Tries each power-of-two width up to `limit`, rounds the height each needs up
    to a power of two and keeps the smallest area (ties: the squarer, then the
    narrower). Returns (width, height), or None when nothing up to `limit` fits.
    """
    order = sorted(icons, key=lambda i: (-i.height, -i.width, i.name.lower()))
    best = None
    side = max(MIN_SIDE, _next_pow2(max(i.width for i in icons) + 2 * pad))
    while side <= limit:
        laid = _shelf(order, side, pad)
        if laid is not None:
            spots, used = laid
            height = max(MIN_SIDE, _next_pow2(used))
            if height <= limit:
                key = (side * height, max(side, height), side)
                if best is None or key < best[0]:
                    best = (key, side, height, spots)
        side *= 2
    if best is None:
        return None
    _, width, height, spots = best
    for icon, (x, y) in zip(order, spots):
        icon.x, icon.y = x, y
    return width, height


def render_atlas(icons: list[Icon], size: tuple[int, int]) -> Image.Image:
    atlas = Image.new("RGBA", size, (0, 0, 0, 0))
    for icon in icons:
        atlas.paste(icon.image, (icon.x, icon.y))  # no mask: alpha is copied as it is
    return atlas


# --- the .imageset text ------------------------------------------------------


def imageset_text(name: str, texture: str, size: tuple[int, int], icons: list[Icon]) -> str:
    """The vanilla form: ImageSetClass > Textures > ImageSetTextureClass, Images > ImageSetDefClass.

    One space of indent per level, LF line ends, `Flags 0`, an empty Groups
    block; each image's block name equals its Name, as in every vanilla set.
    """
    lines = [
        "ImageSetClass {",
        f' Name "{name}"',
        f" RefSize {size[0]} {size[1]}",
        " Textures {",
        "  ImageSetTextureClass {",
        "   mpix 1",
        f'   path "{texture}"',
        "  }",
        " }",
        " Images {",
    ]
    for icon in sorted(icons, key=lambda i: i.name.lower()):
        lines += [
            f"  ImageSetDefClass {icon.name} {{",
            f'   Name "{icon.name}"',
            f"   Pos {icon.x} {icon.y}",
            f"   Size {icon.width} {icon.height}",
            "   Flags 0",
            "  }",
        ]
    lines += [" }", " Groups {", " }", "}"]
    return "\n".join(lines) + "\n"


# --- ImageToPAA --------------------------------------------------------------


def registry_tools_dirs() -> list[tuple[str, Path]]:
    """DayZ Tools install folders the Windows registry knows, with where each came from.

    The folder is the string value `path` of the key
    `Software\\Bohemia Interactive\\Dayz Tools`. A Steam install of DayZ Tools has it
    under HKCU and under HKLM, in both the native and the 32-bit view (read with
    `reg query`).
    """
    if sys.platform != "win32":
        return []
    import winreg

    subkey = r"Software\Bohemia Interactive\Dayz Tools"
    probes = (
        ("HKCU", winreg.HKEY_CURRENT_USER, 0),
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        ("HKLM (32-bit view)", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
    )
    found: list[tuple[str, Path]] = []
    for label, hive, view in probes:
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view) as key:
                value, kind = winreg.QueryValueEx(key, "path")
        except OSError:
            continue
        if kind == winreg.REG_SZ and value:
            found.append((f"registry {label}\\{subkey}", Path(value)))
    return found


def find_image_to_paa(tools_arg: str | None) -> Path:
    """ImageToPAA.exe from --tools, else $DAYZ_TOOLS, else the registry.

    An explicit --tools is the only candidate: a wrong folder there is an
    error, not a reason to quietly use another installation.
    """
    if tools_arg:
        candidates = [("--tools", Path(tools_arg))]
    else:
        candidates = []
        if os.environ.get(TOOLS_ENV):
            candidates.append((f"${TOOLS_ENV}", Path(os.environ[TOOLS_ENV])))
        candidates += registry_tools_dirs()

    misses = []
    for label, root in candidates:
        exe = root / IMAGE_TO_PAA
        if exe.is_file():
            return exe
        misses.append(f"{label}: {exe} does not exist")
    if not misses:
        misses.append(f"${TOOLS_ENV} is not set and the registry has no DayZ Tools entry")
    raise PackError(
        "ImageToPAA.exe not found:\n  "
        + "\n  ".join(misses)
        + f"\nPoint at the DayZ Tools folder (the one that holds {IMAGE_TO_PAA}) with --tools DIR "
        + f"or the {TOOLS_ENV} environment variable, or pass --no-paa to skip the conversion."
    )


def convert_to_paa(exe: Path, png: Path, paa: Path) -> None:
    if paa.exists():
        paa.unlink()  # a stale file must not pass for a fresh conversion
    try:
        done = subprocess.run(
            [str(exe), str(png), str(paa)],
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PackError(f"could not run {exe}: {exc}") from exc
    if done.returncode != 0 or not paa.is_file() or paa.stat().st_size == 0:
        detail = (done.stdout + done.stderr).strip()
        raise PackError(
            f"ImageToPAA did not produce {paa.name} (exit code {done.returncode})"
            + (f":\n{detail}" if detail else "")
        )


# --- command line ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_imageset.py",
        description="Pack a folder of PNG icons into one atlas and write its DayZ .imageset "
        "(and the .paa, through DayZ Tools' ImageToPAA).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
example:
  python build_imageset.py icons --name mymod_icons --prefix MyMod/gui/imagesets --out ../MyMod/gui/imagesets
  writes mymod_icons_ca.png, mymod_icons.imageset and mymod_icons_ca.paa; a layout or
  script then uses  set:mymod_icons image:<file stem>

image names:
  The file stem is the image name, so it may only hold letters, digits and
  underscore (the form every vanilla imageset uses). Duplicate names, a PNG that
  cannot be read, or a folder without PNG files are errors. Files that are not
  .png, and subfolders, are ignored.

packing:
  Icons go on shelves, tallest first; the atlas is the power-of-two size with the
  smallest area (ties: the squarer one). PADDING px of transparent gutter separate
  neighbours and also border the atlas. Size in the imageset is the icon without
  its gutter; RefSize is the atlas size, which DayZ requires to match the texture.

ImageToPAA (not needed with --no-paa) is looked up, first hit wins:
  1. --tools DIR     the DayZ Tools folder; given explicitly, it must hold
                     Bin\\ImageToPAA\\ImageToPAA.exe - there is no fallback
  2. the DAYZ_TOOLS environment variable, same meaning
  3. the Windows registry: the string value "path" of
     Software\\Bohemia Interactive\\Dayz Tools (HKCU, then HKLM, then the 32-bit
     view of HKLM), which is where DayZ Tools records its install folder

after the run:
  list <MOD_PATH>/<SET>.imageset in config.cpp under CfgMods > class defs >
  class imageSets > files[], and pack the .imageset and the .paa into the pbo under
  MOD_PATH. Without that registration the icons stay blank and nothing is logged.

exit status: 0 on success, 1 on any error (message on stderr), 2 for a bad command line.
""",
    )
    parser.add_argument("source_dir", metavar="SOURCE_DIR", help="folder of PNG files, one icon per file")
    parser.add_argument(
        "--name", required=True, metavar="SET",
        help="imageset name (its Name field) and the stem of the output files",
    )
    parser.add_argument(
        "--prefix", required=True, metavar="MOD_PATH",
        help="in-mod folder the .paa will live in, e.g. MyMod/gui/imagesets; "
        "the texture path becomes MOD_PATH/SET_ca.paa",
    )
    parser.add_argument("--out", metavar="DIR", help="output folder, created if missing (default: current folder)")
    parser.add_argument(
        "--padding", type=int, default=2, metavar="N",
        help="gutter in px between icons and around the atlas edge (default: 2)",
    )
    parser.add_argument(
        "--max-size", type=int, default=4096, metavar="N",
        help="largest atlas side in px, rounded down to a power of two; fails if the icons "
        "do not fit (default: 4096)",
    )
    parser.add_argument(
        "--tools", metavar="DAYZ_TOOLS_DIR",
        help="DayZ Tools install folder (see the lookup order below)",
    )
    parser.add_argument(
        "--no-paa", action="store_true",
        help="write the PNG atlas and the .imageset, but skip the ImageToPAA conversion",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    if args.padding < 0:
        raise PackError("--padding must be 0 or more")
    if args.max_size < MIN_SIDE:
        raise PackError(f"--max-size must be at least {MIN_SIDE}")
    if not NAME_RE.fullmatch(args.name):
        raise PackError(f"--name '{args.name}' may only use letters, digits and underscore")
    prefix = args.prefix.replace("\\", "/").strip("/")
    if not prefix or '"' in prefix or any(ord(ch) < 32 for ch in prefix):
        raise PackError("--prefix must be a non-empty in-mod folder such as MyMod/gui/imagesets")
    source = Path(args.source_dir)
    if not source.is_dir():
        raise PackError(f"{source} is not a folder")

    out_dir = Path(args.out) if args.out else Path.cwd()
    atlas_png = out_dir / f"{args.name}_ca.png"
    imageset = out_dir / f"{args.name}.imageset"
    atlas_paa = out_dir / f"{args.name}_ca.paa"

    converter = None if args.no_paa else find_image_to_paa(args.tools)  # fail before writing anything
    icons = load_icons(source, skip=atlas_png.resolve())

    pad = args.padding
    cap = 1 << (args.max_size.bit_length() - 1)  # atlas sides are powers of two: the largest one allowed
    for icon in icons:
        if _next_pow2(icon.width + 2 * pad) > cap or _next_pow2(icon.height + 2 * pad) > cap:
            raise PackError(
                f"{icon.name}.png is {icon.width}x{icon.height}; with a {pad} px gutter it needs "
                f"{icon.width + 2 * pad}x{icon.height + 2 * pad} px, more than the largest atlas side "
                f"{cap} (--max-size {args.max_size})"
            )
    size = pack(icons, pad, cap)
    if size is None:
        need = pack(icons, pad, HARD_LIMIT)
        hint = (
            f"they fit in {need[0]}x{need[1]}; raise --max-size to {max(need)}"
            if need else "they do not fit in any atlas"
        )
        raise PackError(
            f"{len(icons)} icons do not fit in an atlas of {cap}x{cap} or smaller "
            f"(--max-size {args.max_size}): {hint}"
        )

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        render_atlas(icons, size).save(atlas_png, format="PNG")
        imageset.write_text(
            imageset_text(args.name, f"{prefix}/{args.name}_ca.paa", size, icons),
            encoding="utf-8",
            newline="\n",
        )
    except OSError as exc:
        raise PackError(f"cannot write into {out_dir}: {exc}") from exc
    print(f"packed {len(icons)} icons into a {size[0]}x{size[1]} RGBA atlas ({pad} px gutter)")
    print(f"  wrote {atlas_png}")
    print(f"  wrote {imageset}")

    if converter is None:
        print("  skipped the .paa (--no-paa)")
    else:
        convert_to_paa(converter, atlas_png.resolve(), atlas_paa.resolve())
        print(f"  wrote {atlas_paa}  (ImageToPAA: {converter})")
    print(f'register {prefix}/{args.name}.imageset in config.cpp (CfgMods > defs > imageSets > files[]); '
          f'use it as "set:{args.name} image:<icon>"')


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # a Windows console must not die on a path it cannot print
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        run(args)
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
