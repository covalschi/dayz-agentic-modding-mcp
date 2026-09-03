"""What the game's own layouts are made of: every widget class and every
property key found in the shipped `.layout` files, with counts.

Built once from an unpacked `dta/gui.pbo` and committed as data, so the
linter answers "is this a real class" from the game's files rather than from
a hand-written list -- and so the answer carries the build it was read from.

    python -m dayz_mcp.layoutvocab <unpacked gui.pbo>/gui/layouts src/dayz_mcp/data/layout-vocab.json --build 124708
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from importlib import resources
from pathlib import Path

from .layoutparse import LayoutSyntaxError, parse_layout

DATA_FILE = "layout-vocab.json"


def build_vocab(layout_dir: Path, build: str) -> dict:
    classes: Counter[str] = Counter()
    keys: Counter[str] = Counter()
    unparsed: list[str] = []
    files = sorted(Path(layout_dir).rglob("*.layout"))
    for path in files:
        try:
            root = parse_layout(path.read_text(encoding="utf-8", errors="replace"))
        except LayoutSyntaxError:
            unparsed.append(path.name)
            continue
        for _path, node in root.walk():
            classes[node.cls] += 1
            for prop in node.props:
                keys[prop.key] += 1
    return {
        "build": build,
        "files": len(files),
        "unparsed": unparsed,
        "classes": dict(sorted(classes.items())),
        "keys": dict(sorted(keys.items())),
    }


def load_vocab() -> dict:
    text = resources.files("dayz_mcp").joinpath("data", DATA_FILE).read_text(encoding="utf-8")
    return json.loads(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build the layout vocabulary from unpacked vanilla layouts")
    parser.add_argument("layout_dir")
    parser.add_argument("out")
    parser.add_argument("--build", required=True, help="game build the layouts came from, e.g. 124708")
    args = parser.parse_args(argv)
    vocab = build_vocab(Path(args.layout_dir), args.build)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(vocab, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{vocab['files']} files, {len(vocab['classes'])} classes, {len(vocab['keys'])} keys, "
          f"{len(vocab['unparsed'])} unparsed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
