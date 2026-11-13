"""css2layout: a web page, as a browser lays it out, into DayZ .layout files that follow a resize.

    python -m dayz_mcp.css2layout <config.json> [page ...] [--demo] [--reuse] [--icons]

The page is a mockup written in HTML and CSS (any framework: what counts is what the browser
draws). Chrome lays it out at its design size and again one, two and three steps larger; from
the four measurements this tool writes

* `<layout>.layout`: every box, run of text, icon, button, edit box and list of the page as
  engine widgets at the measured places, in the measured colours, in the project's fonts;
* one `.layout` per row template (an element marked `data-dz-row`): what a script creates
  into a list, once per record;
* in every widget's `userID`, how it follows its parent's size (what the two other sizes
  showed): stays, moves with the right or bottom edge, stretches, takes a share. A small
  script (data/css2layout_flex.c.txt, written into the project with its own class prefix)
  applies it when a window is resized: the page is adaptive without a line of layout code;
* with `--demo`, `_demo_<layout>.layout`: the same page with the mockup's own sample texts
  and rows in place, for `ui_preview` -- the picture to hold against the mockup. Never
  packed: delete `_demo_*.layout` before a build (`--clean-demo`);
* `<report>/<layout>.txt`: the widgets a script can reach by name, the captions that have no
  stringtable key yet, the fonts and CSS the engine has no answer for, and where the
  browser re-flows the page as it grows. A page grows with its window from its design size;
  below it the page is not laid out again (a window clips it). Where the browser re-flows
  on the way up (a text unwraps, a box moves up a row) the page keeps its designed shape:
  the rule is what two of the three steps agree on.

What the page says about itself, in `data-dz` attributes (the mockup's own markup). A named
box keeps its drawn parts reachable: its ground is `<Name>Bg`, its frame `<Name>Edge`, a bar
an inset shadow drew `<Name>Bar`, a one-sided border `<Name>Line`. A pill has round ends
beside each of the two: `<Name>BgL`, `<Name>BgR`, `<Name>EdgeL`, `<Name>EdgeR` -- the script
half's `Paint(root, "<Name>Bg", colour)` colours a part with its ends:

    data-dz="Name"          the element's widget is named Name (a button, a panel, an edit box)
    data-dz-text="Name"     the element's text is a widget named Name, filled by script: empty
                            in the real layout, the sample text in the demo
    data-dz-label="Name"    the element's text is a widget named Name and keeps its caption
                            (a script only recolours it)
    data-dz-row="file"      a record of a list: the element becomes the row template
                            `file.layout` (the richest instance wins); instances are left out
                            of the real layout and kept in the demo
    data-dz-kind="canvas"   an empty CanvasWidget of that rectangle (a script draws there)
    data-dz-kind="scroll"   a scroll area even if the sample content fits
    data-dz-kind="stack"    a list as tall as its rows, with no scroll of its own (its rows
                            are the element's `data-dz-row` children; what scrolls is around it)
    data-dz-kind="frame"    a plain container, kept even if it draws nothing
    data-dz-kind="button"   a button, whatever the tag
    data-dz-kind="hit"      an unseen panel that takes the pointer (a drag handle, a grip)
    data-dz-icon="Name"     the element's first sign (a glyph drawn as an icon) is named Name
    data-dz-hidden          hidden in the real layout (a strip a script shows), shown in the demo
    data-dz-empty           the element's content is left out (a host other layouts go into)
    data-dz-skip            the element and its content are left out
    data-dz-align="right"   a text's alignment when the page cannot tell (left, center, right)

The config (JSON; `extends` names another config to lay this one over):

    {
      "serve": "<folder>",            the pages are served from here (they load each other)
      "base": "http://127.0.0.1:8931",  ... or are served already
      "chrome": "<chrome.exe>",       found in the usual places when left out
      "step": [240, 160],             the step of the two larger sizes
      "out": "<folder>",              where the .layout files go
      "prefix": "MyMod/gui/layouts",  that folder's path in the pbo (the report names files by it)
      "report": "<folder>",           where the reports and the measurements go
      "strings": {"files": ["<stringtable.csv>"], "column": "ukrainian", "fold": {"І": "I"},
                  "pending": ["<strings.pending.json>"]},   {"STR_KEY": ["original", "english"]}:
                                              keys that are not in the stringtable yet
      "fonts": [{"family": "Fira Sans", "weight": [0, 449], "face": "MyMod/gui/fonts/sans28",
                 "sizes": {"13": 14}, "ratio": 1.1,
                 "wrap": {"face": "MyMod/gui/fonts/sdf_sans32", "sizes": {"13": 14}, "dy": -1},
                 "edit": {"14": "MyMod/gui/fonts/field16"},
                 "track": [{"from": 1, "face": "MyMod/gui/fonts/sans28t"}]}],
      "glyphs": {"●": {"icon": "dot", "scale": 0.62}},   characters a font lacks, drawn as icons
      "icons": {"set": "myset", "png": "<folder>", "names": {"<key>": "search"}, "max": 48,
                "disc": "disc"},                a full round sprite of the set: with it a box
                                                whose corners are rounded all the way (a pill)
                                                gets round ends; without it, square ones
      "scrollbar": [0.24, 0.24, 0.23, 1],
      "pages": {"overview": {"page": "Overview.html", "size": [1360, 812],
                             "layout": "mymod_overview", "root": "PaneOverview", "auto": "ov"}}
    }

A page's own keys: `page` (the file), `size` (its design size), `layout` (the .layout's
name), `root` (its root widget's name), `auto` (the stem of the names the tool makes up),
`hidden` (the root starts hidden: its window shows it), `priority` (the root's priority, 100
when left out so a page lies over the ground of what it is created into; false leaves the
line out), `selector` (a CSS selector of the page's box when it is not the first box of the
design size).

Engine facts this leans on (the dayz-modding skill, gui-layouts.md): exact positions and sizes
are layout units, a unit is screen height / 1080 pixels; a panel clips its children; the UI
blends in linear light, so a see-through colour is written as the opaque colour it comes to;
a TextWidget takes a bitmap font at a whole "exact text size"; wrapped text needs an SDF face;
an edit box draws a bitmap face at its own pixel size; a scrollbar is 10 units, drawn over
exact-size content, and only while the content is taller than the view.
"""
from __future__ import annotations

import csv
import functools
import hashlib
import html
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = HERE / "data" / "css2layout_probe.html"
FLEX_TEMPLATE = HERE / "data" / "css2layout_flex.c.txt"
PANEL_STYLE = "rover_sim_colorable"
GENERATED = "// GENERATED by dayz-mcp css2layout from "
CHROMES = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
           r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
           "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]
FLEX_FLAG = 0x10000
TWELFTHS = 12


class ConvertError(ValueError):
    pass


# ----------------------------------------------------------------------------- config

def _absolute(cfg: dict, here: Path) -> None:
    """A config's paths are its own: made absolute before another config is laid over it."""
    def fix(v):
        p = Path(v)
        return str(p if p.is_absolute() else (here / p).resolve())
    for k in ("serve", "out", "report"):
        if isinstance(cfg.get(k), str):
            cfg[k] = fix(cfg[k])
    if isinstance(cfg.get("runtime"), dict) and cfg["runtime"].get("out"):
        cfg["runtime"] = dict(cfg["runtime"], out=fix(cfg["runtime"]["out"]))
    if isinstance(cfg.get("strings"), dict):
        st = dict(cfg["strings"])
        for k in ("files", "pending"):
            if st.get(k):
                st[k] = [fix(f) for f in st[k]]
        cfg["strings"] = st
    if isinstance(cfg.get("icons"), dict) and cfg["icons"].get("png"):
        cfg["icons"] = dict(cfg["icons"], png=fix(cfg["icons"]["png"]))


def load_config(path: Path) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    _absolute(cfg, Path(path).parent.resolve())
    base = cfg.get("extends")
    if base:
        parent = load_config((Path(path).parent / base).resolve())
        merged = dict(parent)
        for k, v in cfg.items():
            if k == "extends":
                continue
            if isinstance(v, dict) and isinstance(merged.get(k), dict) and k in ("glyphs", "icons", "strings"):
                m = dict(merged[k])
                m.update(v)
                merged[k] = m
            else:
                merged[k] = v
        cfg = merged
    cfg["_dir"] = str(Path(path).parent.resolve())
    return cfg


def cfg_path(cfg: dict, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (Path(cfg["_dir"]) / p).resolve()


# ----------------------------------------------------------------------------- measuring

class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(folder: Path):
    """Serve `folder` on a free local port; returns (base url, stop())."""
    handler = functools.partial(_Quiet, directory=str(folder))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}", httpd.shutdown


def find_chrome(cfg: dict) -> str:
    if cfg.get("chrome"):
        return cfg["chrome"]
    for c in CHROMES:
        if os.path.exists(c):
            return c
    raise ConvertError("no Chrome found: name it in the config's `chrome`")


def measure(cfg: dict, page: dict, base: str, work: Path) -> dict:
    """Run the probe on one page; the measurement is the probe's JSON."""
    w, h = page.get("size", [1360, 812])
    dw, dh = cfg.get("step", [240, 160])
    glyphs = "".join(cfg.get("glyphs", {}).keys())
    from urllib.parse import quote
    url = (f"{base}/css2layout_probe.html?page={quote(page['page'])}&w={w}&h={h}&dw={dw}&dh={dh}"
           f"&glyphs={quote(glyphs)}")
    if page.get("selector"):
        url += "&root=" + quote(page["selector"])
    import tempfile
    profile = tempfile.mkdtemp(prefix="css2layout_chrome_")
    cmd = [find_chrome(cfg), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
           "--user-data-dir=" + profile, "--force-device-scale-factor=1",
           f"--window-size={w + 3 * dw + 60},{h + 3 * dh + 260}", "--virtual-time-budget=15000", "--dump-dom", url]
    try:
        run = subprocess.run(cmd, capture_output=True, timeout=240)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    dom = run.stdout.decode("utf-8", "replace")
    m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
    if not m or not m.group(1).strip():
        raise ConvertError(f"{page['page']}: the probe wrote nothing ({run.stderr.decode('utf-8', 'replace')[-300:]})")
    return json.loads(html.unescape(m.group(1)))


# ----------------------------------------------------------------------------- geometry

def rnd(v: float) -> int:
    """Half up: 263.5 -> 264 on every edge, so neighbours keep touching."""
    import math
    return int(math.floor(v + 0.5))


SIZES = "abcd"


class R3:
    """One rectangle at the measured sizes: a (design), then b, c, d -- one, two and three
    steps larger."""

    __slots__ = ("a", "b", "c", "d")

    def __init__(self, a, b=None, c=None, d=None):
        self.a = [float(v) for v in a]
        self.b = [float(v) for v in (b if b is not None else a)]
        self.c = [float(v) for v in (c if c is not None else self.b)]
        self.d = [float(v) for v in (d if d is not None else self.c)]

    @staticmethod
    def of(fn) -> "R3":
        """fn(which) -> a rectangle, for which in a, b, c, d."""
        return R3(fn("a"), fn("b"), fn("c"), fn("d"))

    def each(self, fn) -> "R3":
        return R3(fn(self.a), fn(self.b), fn(self.c), fn(self.d))

    def inset(self, left, top, right, bottom) -> "R3":
        return self.each(lambda r: [r[0] + left, r[1] + top, r[2] - left - right, r[3] - top - bottom])

    def side(self, which: str, width: float) -> "R3":
        """The strip `width` thick along one side, inside the rectangle."""
        def cut(r):
            if which == "t":
                return [r[0], r[1], r[2], width]
            if which == "b":
                return [r[0], r[1] + r[3] - width, r[2], width]
            if which == "l":
                return [r[0], r[1], width, r[3]]
            return [r[0] + r[2] - width, r[1], width, r[3]]
        return self.each(cut)

    def copy(self) -> "R3":
        return R3(self.a, self.b, self.c, self.d)


# ----------------------------------------------------------------------------- widgets

class Wd:
    """One widget to write: class, name, where it is at the three sizes, what it holds."""

    def __init__(self, cls: str, name: str, rect: R3):
        self.cls = cls
        self.name = name
        self.rect = rect
        self.props: list[tuple[str, str]] = []
        self.kids: list["Wd"] = []
        self.color: str | None = None
        self.ignore: bool | None = True
        self.hidden_real = False        # visible 0 in the real layout
        self.row_inst = False           # an instance of a row: the demo only
        self.text: str | None = None    # a text widget's caption
        self.dyn = False                # ... filled by script: empty in the real layout
        self.flex = True
        self.note = ""
        self.spill = False              # may reach outside its parent (hoisted out of clipping)

    def set(self, key: str, value) -> "Wd":
        self.props.append((key, str(value)))
        return self


def fmt(v: float) -> str:
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return ("%.3f" % v).rstrip("0").rstrip(".")


def key(name: str) -> str:
    return f'"{name}"' if " " in name else name


def color_text(hx: str, alpha: float = 1.0) -> str:
    r, g, b = int(hx[1:3], 16), int(hx[3:5], 16), int(hx[5:7], 16)
    return "%s %s %s %s" % (fmt(round(r / 255, 4)), fmt(round(g / 255, 4)), fmt(round(b / 255, 4)), fmt(alpha))


def blend(hx: str, under: str, k: float) -> str:
    """`hx` at opacity k over `under`, as the browser mixes them."""
    a = [int(hx[i:i + 2], 16) for i in (1, 3, 5)]
    u = [int(under[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join("%02x" % rnd(a[i] * k + u[i] * (1 - k)) for i in range(3))


# ----------------------------------------------------------------------------- the converter

class Report:
    def __init__(self):
        self.named: list[str] = []
        self.rows: list[str] = []
        self.captions: dict[str, int] = {}
        self.fonts: dict[str, int] = {}
        self.dropped: dict[str, int] = {}
        self.notes: list[str] = []
        self.flex_worst = 0.0
        self.flex_bad: list[str] = []
        self.unnamed_inputs = 0
        self.unnamed_buttons = 0
        self.widgets = 0

    def drop(self, what: str) -> None:
        self.dropped[what] = self.dropped.get(what, 0) + 1


class Strings:
    """Captions to stringtable keys: an exact match in one language column."""

    def __init__(self, cfg: dict):
        self.by_text: dict[str, str] = {}
        st = cfg.get("strings") or {}
        self.fold = st.get("fold") or {}
        column = st.get("column", "original")
        for f in st.get("files", []):
            path = cfg_path(cfg, f)
            if not path.exists():
                continue
            with open(path, encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.reader(fh))
            if not rows:
                continue
            head = [h.strip().lower() for h in rows[0]]
            if column.lower() not in head:
                continue
            ci = head.index(column.lower())
            for row in rows[1:]:
                if len(row) <= ci or not row[0].strip():
                    continue
                self.by_text.setdefault(self.norm(row[ci]), row[0].strip())
        for f in st.get("pending", []):
            path = cfg_path(cfg, f)
            if not path.exists():
                continue
            which = 1 if column.lower() == "english" else 0
            for k, pair in json.loads(path.read_text(encoding="utf-8")).items():
                if isinstance(pair, list) and len(pair) > which:
                    self.by_text.setdefault(self.norm(pair[which]), k)

    def norm(self, text: str) -> str:
        t = " ".join(text.replace("\u00a0", " ").split())
        for a, b in self.fold.items():
            t = t.replace(a, b)
        return t

    def key_of(self, text: str) -> str | None:
        k = self.by_text.get(self.norm(text))
        return "#" + k if k else None


class Icons:
    """The page's small drawings: one sprite per distinct svg."""

    def __init__(self, cfg: dict):
        ic = cfg.get("icons") or {}
        self.set = ic.get("set", "")
        self.names: dict[str, str] = dict(ic.get("names") or {})
        self.max = ic.get("max", 48)
        self.found: dict[str, str] = {}

    @staticmethod
    def key_of(svg: str) -> tuple[str, str]:
        body = re.sub(r"\s+", " ", svg)
        body = re.sub(r' (width|height|style|class|aria-hidden|data-[\w-]+)="[^"]*"', "", body)
        return hashlib.md5(body.encode("utf-8")).hexdigest()[:8], body

    def name_of(self, svg: str) -> str:
        k, body = self.key_of(svg)
        self.found[k] = body
        return self.names.get(k, "i_" + k)


class Converter:
    def __init__(self, cfg: dict, page_name: str, page: dict, probe: dict, strings: Strings, icons: Icons):
        self.cfg = cfg
        self.page_name = page_name
        self.page = page
        self.probe = probe
        self.strings = strings
        self.icons = icons
        self.rep = Report()
        self.a = probe["a"]
        self.b = probe["b"]
        self.c = probe["c"]
        self.d = probe.get("d") or probe["c"]
        self.reflows = 0
        self.names: set[str] = set()
        self.auto = page.get("auto", "w")
        self.auto_n = 0
        self.rows: dict[str, list[dict]] = {}       # row file -> its instances (nodes)
        self.row_hosts: dict[str, str] = {}
        self.scroll_color = cfg.get("scrollbar")
        if probe.get("warn"):
            self.rep.notes.append(probe["warn"])

    # ---- small helpers

    def r3(self, node: dict) -> R3:
        i = node["i"]
        return R3(self.a[i], self.b[i], self.c[i], self.d[i])

    def claim(self, name: str, pool: set[str] | None = None) -> str:
        pool = self.names if pool is None else pool
        if name in pool:
            n = 2
            while f"{name}_{n}" in pool:
                n += 1
            self.rep.notes.append(f"the name {name} is used twice in one file: the second became {name}_{n}")
            name = f"{name}_{n}"
        pool.add(name)
        return name

    def fresh(self, pool: set[str] | None = None, stem: str = "") -> str:
        pool = self.names if pool is None else pool
        while True:
            self.auto_n += 1
            name = f"{stem or self.auto}{self.auto_n}"
            if name not in pool:
                pool.add(name)
                return name

    def col(self, c: dict | None, fades: list) -> str | None:
        if not c:
            return None
        hx = c.get("on") or c["c"]
        for k, under in fades:
            hx = blend(hx, under, k)
        return hx

    # ---- fonts

    def font_rule(self, f: dict) -> dict | None:
        fam = f.get("fam", "").lower()
        w = int(f.get("w", 400))
        best = None
        for rule in self.cfg.get("fonts", []):
            fams = rule.get("family")
            fams = [fams] if isinstance(fams, str) else (fams or [])
            if fam not in [x.lower() for x in fams]:
                continue
            lo, hi = rule.get("weight", [0, 1000])
            if lo <= w <= hi:
                return rule
            best = best or rule
        return best

    def font_props(self, wd: Wd, f: dict, wrapped: bool = False) -> float:
        """Write a text widget's font; returns the size written (0 when the face is fixed)."""
        rule = self.font_rule(f)
        px = f.get("px", 14)
        label = "%s %s %gpx%s" % (f.get("fam", "?"), f.get("w", 400), px, " wrapped" if wrapped else "")
        if rule is None:
            self.rep.fonts[label + "  (NO RULE: the first font of the config)"] = self.rep.fonts.get(label, 0) + 1
            rules = self.cfg.get("fonts") or [{}]
            rule = rules[0]
        src = rule
        if wrapped and rule.get("wrap"):
            src = rule["wrap"]
        elif wrapped:
            self.rep.fonts[label + "  (no wrap face: drawn with the line face)"] = 1
        # one family and weight may be several atlases, by the size on screen
        for part in src.get("by") or []:
            if "upto" not in part or px <= part["upto"]:
                src = dict(src, **part)
                break
        face = src.get("face", "gui/fonts/sdf_MetronBook24")
        if not wrapped and f.get("ls"):
            for t in sorted(rule.get("track") or [], key=lambda t: -t.get("from", 0)):
                if f["ls"] >= t.get("from", 0) and t.get("min", 0) <= px <= t.get("max", 999):
                    face = t["face"]
                    break
            else:
                self.rep.drop("letter-spacing %gpx (no tracked face for %s %s)" % (f["ls"], f.get("fam"), f.get("w")))
        sizes = src.get("sizes") or {}
        ipx = str(int(px)) if float(px).is_integer() else str(px)
        if ipx in sizes:
            size = sizes[ipx]
        else:
            size = rnd(px * src.get("ratio", rule.get("ratio", 1.12)))
            if sizes:
                self.rep.fonts[label + f"  (no size in the table: {size} by ratio)"] = 1
        wd.set("font", f'"{face}"').set(key("exact text"), "1").set(key("exact text size"), fmt(size))
        return size

    # ---- decoration

    def decor(self, n: dict, fades: list) -> dict:
        """What a box draws: its ground, its border, the bars an inset shadow stands for."""
        d = {"bg": self.col(n.get("bg"), fades), "uniform": None, "lines": [], "bars": []}
        bd = n.get("bd") or []
        if bd:
            cols = {(b["w"], self.col(b["col"], fades)) for b in bd}
            if len(bd) == 4 and len(cols) == 1:
                d["uniform"] = (bd[0]["w"], self.col(bd[0]["col"], fades))
            else:
                for b in bd:
                    d["lines"].append((b["s"], b["w"], self.col(b["col"], fades)))
            if any(b["st"] not in ("solid",) for b in bd):
                self.rep.drop("border style %s (drawn solid)" % "/".join(sorted({b["st"] for b in bd})))
        sh = n.get("sh")
        if sh:
            for m in re.finditer(r"(rgba?\([^)]*\))\s+(-?[\d.]+)px\s+(-?[\d.]+)px\s+([\d.]+)px\s+(-?[\d.]+)px(\s+inset)?", sh):
                colr, x, y, blur, spread, inset = m.groups()
                x, y, blur, spread = float(x), float(y), float(blur), float(spread)
                p = [float(v) for v in re.findall(r"[\d.]+", colr)]
                hx = "#%02x%02x%02x" % (rnd(p[0]), rnd(p[1]), rnd(p[2]))
                if len(p) > 3 and p[3] < 1:
                    hx = blend(hx, n.get("eb", "#000000"), p[3])
                for k, under in fades:
                    hx = blend(hx, under, k)
                if blur == 0 and inset and (x or y) and not spread:
                    if x:
                        d["bars"].append(("l" if x > 0 else "r", abs(x), hx))
                    if y:
                        d["bars"].append(("t" if y > 0 else "b", abs(y), hx))
                elif blur == 0 and spread > 0 and not x and not y:
                    for s in "trbl":
                        d["bars"].append((s, spread, hx))
                else:
                    self.rep.drop("box-shadow (a soft shadow)")
        if n.get("rad"):
            r_ = self.a[n["i"]]
            if not ((self.cfg.get("icons") or {}).get("disc") and n["rad"] >= r_[3] / 2 - 1 and 6 <= r_[3] <= 48):
                self.rep.drop("border-radius")
        if n.get("bgi"):
            self.rep.drop("background-image")
        return d

    def panel(self, name: str, rect: R3, hx: str) -> Wd:
        w = Wd("PanelWidgetClass", name, rect)
        w.color = hx
        w.set("style", PANEL_STYLE)
        return w

    # ---- emission

    def emit(self, n: dict, st: dict) -> list[Wd]:
        # what the page keeps off its own box (a label for screen readers at -9999) is not
        # drawn -- but what a scrolling area holds past the page's edge is its content, not
        # something kept off the page: under a scrolling ancestor nothing is left out
        r = self.a[n["i"]]
        page = self.a[self.probe["tree"]["i"]]
        if st.get("mode") != "row" and not st.get("scrolled") and (
                r[0] + r[2] < page[0] - 1 or r[1] + r[3] < page[1] - 1
                or r[0] > page[0] + page[2] + 1 or r[1] > page[1] + page[3] + 1):
            self.rep.notes.append("left out, it lies off the page: %s at %d,%d" % (
                ('"%s"' % n["text"][:40]) if "text" in n else n.get("t", "?"), r[0], r[1]))
            return []
        if "text" in n:
            return self.emit_text(n, st)
        return self.emit_el(n, st)

    def emit_kids(self, n: dict, st: dict) -> list[Wd]:
        out: list[Wd] = []
        for k in n.get("k") or []:
            out += self.emit(k, dict(st, parent_el=n, anc=st["anc"] + [n]))
        return out

    def emit_el(self, n: dict, st: dict) -> list[Wd]:
        dz = n.get("dz") or {}
        if "skip" in dz:
            return []
        pool = st["pool"]
        tag = n["t"]
        rect = self.r3(n)
        fades = st["fades"]
        if n.get("op") is not None:
            fades = fades + [(n["op"], st["under"])]
            st = dict(st, fades=fades)
        st = dict(st, under=n.get("eb", st["under"]))
        as_row = bool(st.get("as_row"))
        if "row" in dz and not as_row:
            self.rows.setdefault(dz["row"], []).append(n)
            if st.get("mode") != "demo":
                return []
            inst = self.emit_el(n, dict(st, as_row=True))
            for w in inst:
                w.row_inst = True
            return inst
        if as_row:
            st = dict(st, as_row=False)
        if tag == "svg":
            return self.emit_svg(n, st)
        if tag in ("input", "textarea"):
            return self.emit_input(n, st)
        if tag == "select":
            return self.emit_select(n, st)

        d = self.decor(n, fades)
        # A fieldset draws its border from the middle of its legend down and breaks it under
        # the legend. The browser's box of the fieldset starts at the legend's top: the
        # border is drawn lower, and the legend gets the ground it stands on, so the line
        # stops under its words.
        drect = rect
        if tag == "fieldset":
            lg = next((k for k in (n.get("k") or []) if k.get("t") == "legend"), None)
            if lg is not None and (d["uniform"] or d["lines"]):
                la = self.a[lg["i"]]
                drop = (la[1] + la[3] / 2) - rect.a[1]
                if drop > 0.5:
                    drect = rect.inset(0, rnd(drop), 0, 0)
                    if not lg.get("bg"):
                        lg["bg"] = {"c": n.get("eb", st["under"])}
        kind = dz.get("kind", "")
        has_field = self.holds(n, ("input", "textarea", "select"))
        is_btn = kind == "button" or tag == "button" or (tag == "a" and n.get("click")) or \
            (n.get("click") and not st["in_click"] and not has_field and tag not in ("label",))
        if has_field and tag != "button" and kind != "button":
            is_btn = False
        if st["in_click"]:
            is_btn = False
        of = n.get("of") or ["visible", "visible"]
        sc = n.get("sc") or [0, 0, 0, 0]
        row_kids = [k for k in (n.get("k") or []) if "t" in k and "row" in (k.get("dz") or {})]
        scrolls = of[1] in ("auto", "scroll") and (sc[1] > sc[3] + 1)
        if of[1] in ("auto", "scroll") or of[0] in ("auto", "scroll"):
            st = dict(st, scrolled=True)
        is_scroll = kind == "scroll" or scrolls or (of[1] in ("auto", "scroll") and bool(row_kids))
        clips = of[1] in ("hidden", "clip") or of[0] in ("hidden", "clip")
        decorated = bool(d["bg"] or d["uniform"] or d["lines"] or d["bars"])
        named = dz.get("name")
        need_box = decorated or is_btn or is_scroll or clips or named or kind in ("frame", "canvas") or "hidden" in dz \
            or "empty" in dz or as_row
        if rnd(rect.a[2]) <= 0 or rnd(rect.a[3]) <= 0:
            if not (n.get("k")):
                return []
            need_box = False
        if not need_box:
            return self.emit_kids(n, st)

        if kind == "stack":
            first = self.r3(row_kids[0]) if row_kids else rect
            spacer = Wd("WrapSpacerWidgetClass", self.claim(named, pool) if named else self.fresh(pool),
                        R3.of(lambda which: [getattr(first, which)[0], getattr(rect, which)[1], getattr(first, which)[2], 0]))
            spacer.ignore = None
            gaps = []
            for p_, q_ in zip(row_kids, row_kids[1:]):
                ra_, rb_ = self.a[p_["i"]], self.a[q_["i"]]
                gaps.append(round(rb_[1] - (ra_[1] + ra_[3]), 1))
            gap = max(set(gaps), key=gaps.count) if gaps else 0
            spacer.set("Padding", fmt(max(0, gap))).set("Margin", "0").set(key("Size To Content V"), "1")
            files = []
            for k in row_kids:
                if k["dz"]["row"] not in files:
                    files.append(k["dz"]["row"])
                self.row_hosts.setdefault(k["dz"]["row"], spacer.name)
            self.rep.named.append(f"{spacer.name}  WrapSpacer: the rows go here ({', '.join(files)}); it is as tall as its "
                                  f"rows and scrolls with what holds it")
            for k in row_kids:
                spacer.kids += self.emit_el(k, dict(st, parent_el=n, anc=st["anc"] + [n]))
            return [spacer]

        if kind == "hit":
            hit = Wd("PanelWidgetClass", self.claim(named, pool) if named else self.fresh(pool), rect)
            hit.ignore = None
            hit.set("color", "0 0 0 0").set("style", PANEL_STYLE)
            if named:
                self.rep.named.append(f"{hit.name}  Panel (unseen, takes the pointer)")
            return [hit]

        if kind == "canvas":
            cv = Wd("CanvasWidgetClass", self.claim(named, pool) if named else self.fresh(pool), rect)
            cv.ignore = None
            if named:
                self.rep.named.append(f"{cv.name}  CanvasWidget")
            return self.frame_decor(cv, d, rect, pool, wrap=True)

        if is_btn:
            box = Wd("ButtonWidgetClass", self.claim(named, pool) if named else self.fresh(pool), rect)
            box.ignore = None
            box.set("text", '""')
            self.fill_decor(box, d, drect, pool, n, box.name if named else "")
            if named:
                parts = [k.name for k in box.kids]
                self.rep.named.append(f"{box.name}  ButtonWidget" + (f" (parts: {', '.join(parts)})" if parts else ""))
            elif not as_row and st.get("mode") != "row":
                self.rep.unnamed_buttons += 1
            inner = dict(st, in_click=True)
        else:
            if (named and decorated) or (decorated and drect is not rect):
                # a named box keeps one shape whatever it draws: a frame holding its parts
                # (and so does a fieldset, whose border is not its own box)
                box = Wd("FrameWidgetClass", self.claim(named, pool) if named else self.fresh(pool), rect)
                self.fill_decor(box, d, drect, pool, n, box.name if named else "")
            elif decorated and (n.get("rad") or 0) >= rect.a[3] / 2 - 1 and (self.cfg.get("icons") or {}).get("disc") \
                    and 6 <= rect.a[3] <= 48:
                box = Wd("FrameWidgetClass", self.fresh(pool), rect)
                self.fill_decor(box, d, rect, pool, n)
            elif d["uniform"]:
                box = self.panel(self.claim(named, pool) if named else self.fresh(pool), rect, d["uniform"][1])
                bw = d["uniform"][0]
                fill = self.panel(self.fresh(pool), rect.inset(bw, bw, bw, bw), d["bg"] or n.get("eb", st["under"]))
                box.kids.append(fill)
                d = dict(d, uniform=None, bg=None)
                self.fill_decor(box, d, rect, pool, n)
            elif d["bg"]:
                box = self.panel(self.claim(named, pool) if named else self.fresh(pool), rect, d["bg"])
                d = dict(d, bg=None)
                self.fill_decor(box, d, rect, pool, n)
            else:
                box = Wd("FrameWidgetClass", self.claim(named, pool) if named else self.fresh(pool), rect)
                self.fill_decor(box, d, rect, pool, n)
            if named:
                parts = [k.name for k in box.kids if k.name.startswith(box.name) and k.name != box.name]
                self.rep.named.append(f"{box.name}  {'Panel' if box.cls.startswith('Panel') else 'Frame'}"
                                      + (f" (parts: {', '.join(parts)})" if parts else ""))
            inner = st
        if "hidden" in dz:
            box.hidden_real = True
        if clips or is_scroll:
            box.set("clipchildren", "1")

        if "empty" in dz:
            return [box]

        if is_scroll:
            box.kids += self.emit_scroll(n, dict(inner, scrolled=True), rect, row_kids, named, pool)
        else:
            box.kids += self.emit_kids(n, inner)
        return [box]

    def holds(self, n: dict, tags: tuple) -> bool:
        for k in n.get("k") or []:
            if "t" in k and (k["t"] in tags or self.holds(k, tags)):
                return True
        return False

    def fill_decor(self, box: Wd, d: dict, rect: R3, pool: set, n: dict, stem: str = "") -> None:
        """The ground, the border lines and the bars of a box, as its first children. With a
        stem (the box has a name) the parts are named after it, so a script can repaint them."""
        def part(suffix: str) -> str:
            if stem and stem + suffix not in pool:
                return self.claim(stem + suffix, pool)
            return self.fresh(pool)
        disc = (self.cfg.get("icons") or {}).get("disc")
        rad = n.get("rad") or 0
        h = rect.a[3]
        pill = bool(disc and self.icons.set and rad >= h / 2 - 1 and 6 <= h <= 48 and rect.a[2] > h
                    and not d["lines"] and not d["bars"])

        def ground(suffix: str, r: R3, hx: str) -> None:
            """One filled shape of the box: a panel, or for a pill a middle and two round ends."""
            if not pill:
                box.kids.append(self.panel(part(suffix), r, hx))
                return
            rr = r.a[3] / 2
            box.kids.append(self.panel(part(suffix), r.inset(rr, 0, rr, 0), hx))
            for side in ("L", "R"):
                def clip_at(q, side=side):
                    hh = q[3] / 2
                    return [q[0] if side == "L" else q[0] + q[2] - hh, q[1], hh, q[3]]

                def disc_at(q, side=side):
                    hh = q[3] / 2
                    return [q[0] if side == "L" else q[0] + q[2] - q[3], q[1], q[3], q[3]]
                clip = Wd("FrameWidgetClass", self.fresh(pool), r.each(clip_at))
                clip.set("clipchildren", "1")
                end = Wd("ImageWidgetClass", part(suffix + side), r.each(disc_at))
                end.color = hx
                end.set("image0", f'"set:{self.icons.set} image:{disc}"').set("mode", "blend").set(key("src alpha"), "1")
                clip.kids.append(end)
                box.kids.append(clip)

        if d["uniform"]:
            bw, hx = d["uniform"]
            ground("Edge", rect.copy(), hx)
            ground("Bg", rect.inset(bw, bw, bw, bw), d["bg"] or n.get("eb", "#000000"))
        elif d["bg"]:
            ground("Bg", rect.copy(), d["bg"])
        for side, w, hx in d["lines"]:
            if hx:
                box.kids.append(self.panel(part("Line"), rect.side(side, w), hx))
        for side, w, hx in d["bars"]:
            box.kids.append(self.panel(part("Bar"), rect.side(side, w), hx))

    def frame_decor(self, inner: Wd, d: dict, rect: R3, pool: set, wrap: bool) -> list[Wd]:
        """A widget that cannot hold decoration itself (a canvas): its decoration around it."""
        if not (d["bg"] or d["uniform"] or d["lines"] or d["bars"]):
            return [inner]
        box = Wd("FrameWidgetClass", self.fresh(pool), rect.copy())
        self.fill_decor(box, d, rect, pool, {})
        if d["uniform"]:
            bw = d["uniform"][0]
            inner.rect = rect.inset(bw, bw, bw, bw)
        box.kids.append(inner)
        return [box]

    def emit_scroll(self, n: dict, st: dict, rect: R3, row_kids: list, named: str | None, pool: set) -> list[Wd]:
        """What a scrolling element holds: a list of rows (a spacer the script fills) or
        whatever the page put there, on a frame as tall as the content."""
        pad = n.get("pad") or [0, 0, 0, 0]
        bws = {b["s"]: b["w"] for b in (n.get("bd") or [])}
        inner = rect.inset(bws.get("l", 0), bws.get("t", 0), bws.get("r", 0), bws.get("b", 0))
        out: list[Wd] = []
        el_kids = [k for k in (n.get("k") or []) if "t" in k]
        only_rows = bool(row_kids) and len(row_kids) == len(el_kids)
        if only_rows:
            first = self.r3(row_kids[0])
            def view_at(which):
                fr, ir = getattr(first, which), getattr(inner, which)
                return [fr[0], ir[1] + pad[0], fr[2], ir[3] - pad[0] - pad[2]]
            view = R3.of(view_at)
            host_name = self.claim(named + "Rows", pool) if named else self.fresh(pool)
            scroll = Wd("ScrollWidgetClass", self.claim(named + "Scroll", pool) if named else self.fresh(pool), view)
            scroll.ignore = None
            self.scroll_props(scroll)
            spacer_rect = view.each(lambda r: [r[0], r[1], r[2], 0])
            spacer = Wd("WrapSpacerWidgetClass", host_name, spacer_rect)
            spacer.ignore = None
            gaps = []
            for p, q in zip(row_kids, row_kids[1:]):
                ra, rb_ = self.a[p["i"]], self.a[q["i"]]
                gaps.append(round(rb_[1] - (ra[1] + ra[3]), 1))
            gap = max(set(gaps), key=gaps.count) if gaps else 0
            if gaps and len(set(gaps)) > 1:
                self.rep.notes.append(f"list {host_name}: the rows stand {sorted(set(gaps))} apart; the spacer keeps {gap}")
            spacer.set("Padding", fmt(max(0, gap))).set("Margin", "0").set(key("Size To Content V"), "1")
            spacer.flex = True
            files = []
            for k in row_kids:
                f = k["dz"]["row"]
                if f not in files:
                    files.append(f)
                self.row_hosts.setdefault(f, host_name)
            self.rep.named.append(f"{host_name}  WrapSpacer: the list's rows go here ({', '.join(files)}); "
                                  f"its ScrollWidget is {scroll.name}")
            for k in row_kids:
                spacer.kids += self.emit_el(k, st)
            scroll.kids.append(spacer)
            out.append(scroll)
            return out
        sc = n.get("sc") or [0, 0, 0, 0]
        scroll = Wd("ScrollWidgetClass", self.claim(named + "Scroll", pool) if named else self.fresh(pool), inner)
        scroll.ignore = None
        self.scroll_props(scroll)
        content_h = max(sc[1], sc[3])
        body = Wd("FrameWidgetClass", self.claim(named + "Body", pool) if named else self.fresh(pool),
                  inner.each(lambda r: [r[0], r[1], r[2], max(content_h, r[3])]))
        body.ignore = None
        if named:
            self.rep.named.append(f"{body.name}  Frame: the scrolled content ({content_h} units tall in the sample); "
                                  f"its ScrollWidget is {scroll.name}")
        body.kids += self.emit_kids(n, st)
        scroll.kids.append(body)
        return [scroll]

    def scroll_props(self, scroll: Wd) -> None:
        scroll.set("clipchildren", "1").set(key("Scrollbar V"), "1")
        if self.scroll_color:
            scroll.color = None
            scroll.set("style", "blank")
            scroll.set("color", " ".join(fmt(v) for v in self.scroll_color))

    def emit_svg(self, n: dict, st: dict) -> list[Wd]:
        dz = n.get("dz") or {}
        rect = self.r3(n)
        pool = st["pool"]
        if max(rect.a[2], rect.a[3]) > self.icons.max or dz.get("kind") == "canvas":
            if dz.get("kind") == "canvas" or dz.get("name"):
                cv = Wd("CanvasWidgetClass", self.claim(dz.get("name") or "", pool) if dz.get("name") else self.fresh(pool), rect)
                cv.ignore = None
                if dz.get("name"):
                    self.rep.named.append(f"{cv.name}  CanvasWidget (a drawing of the mockup: a script draws it)")
                return [cv]
            self.rep.notes.append("a drawing %d x %d at %d,%d is left out: mark it data-dz-kind=canvas with a name, "
                                  "and a script draws it" % (rect.a[2], rect.a[3], rect.a[0], rect.a[1]))
            return []
        if not self.icons.set:
            self.rep.drop("svg icon (no icons.set in the config)")
            return []
        name = self.icons.name_of(n["svg"])
        w = Wd("ImageWidgetClass", self.claim(dz["name"], pool) if dz.get("name") else self.fresh(pool), rect)
        w.color = self.col(n.get("col"), st["fades"]) or "#ffffff"
        w.set("image0", f'"set:{self.icons.set} image:{name}"').set("mode", "blend").set(key("src alpha"), "1")
        if dz.get("name"):
            self.rep.named.append(f"{w.name}  ImageWidget (icon {name})")
        return [w]

    def emit_input(self, n: dict, st: dict) -> list[Wd]:
        dz = n.get("dz") or {}
        pool = st["pool"]
        rect = self.r3(n)
        inp = n["inp"]
        f = n["f"]
        out: list[Wd] = []
        d = self.decor(n, st["fades"])
        pad = n.get("pad") or [0, 0, 0, 0]
        bw = d["uniform"][0] if d["uniform"] else 0
        holder: Wd | None = None
        if d["bg"] or d["uniform"] or d["lines"] or d["bars"]:
            holder = Wd("FrameWidgetClass", self.fresh(pool), rect.copy())
            self.fill_decor(holder, d, rect, pool, n)
            out.append(holder)
        if inp.get("type") in ("checkbox", "radio"):
            # the browser draws these itself: a framed box, the mark inside while it is on
            name = self.claim(dz["name"], pool) if dz.get("name") else self.fresh(pool, "tick")
            box = Wd("ButtonWidgetClass", name, rect)
            box.ignore = None
            box.set("text", '""')
            on = bool(inp.get("on"))
            marks = self.cfg.get("marks") or {}
            edge = marks.get("on" if on else "off", "#a09f99")
            fill = marks.get("fill_on" if on else "fill", n.get("eb", st["under"]))
            # its frame and its ground, in that order (a script that colours them by place
            # keeps working), named when the box is: <Name>Edge, <Name>Bg
            def part(suffix: str) -> str:
                if dz.get("name") and name + suffix not in pool:
                    return self.claim(name + suffix, pool)
                return self.fresh(pool)
            disc = (self.cfg.get("icons") or {}).get("disc")
            if inp["type"] == "radio" and disc and self.icons.set:
                for suffix, r_, hx in (("Edge", rect.copy(), edge), ("Bg", rect.inset(1, 1, 1, 1), fill)):
                    round_ = Wd("ImageWidgetClass", part(suffix), r_)
                    round_.color = hx
                    round_.set("image0", f'"set:{self.icons.set} image:{disc}"').set("mode", "blend").set(key("src alpha"), "1")
                    box.kids.append(round_)
            else:
                box.kids.append(self.panel(part("Edge"), rect.copy(), edge))
                box.kids.append(self.panel(part("Bg"), rect.inset(1, 1, 1, 1), fill))
            glyph = (self.cfg.get("glyphs") or {}).get("\u2713" if inp["type"] == "checkbox" else "\u25cf")
            if glyph and self.icons.set:
                mark = Wd("ImageWidgetClass", self.claim(name + "Mark", pool), rect.inset(2, 2, 2, 2))
                mark.color = marks.get("mark", "#140a02")
                mark.set("image0", f'"set:{self.icons.set} image:{glyph["icon"]}"').set("mode", "blend").set(key("src alpha"), "1")
                if not on:
                    mark.hidden_real = True
                    mark.props.append(("_demo_hidden", "1"))
                box.kids.append(mark)
            if dz.get("name"):
                self.rep.named.append(f"{name}  ButtonWidget (a {inp['type']}; parts: {name}Edge, {name}Bg); its mark is "
                                      f"{name}Mark: show it when on")
            else:
                self.rep.unnamed_inputs += 1
            return out + [box]
        if inp.get("type") == "range":
            name = self.claim(dz["name"], pool) if dz.get("name") else self.fresh(pool, "slide")
            box = Wd("FrameWidgetClass", name, rect)
            marks = self.cfg.get("marks") or {}
            mid = rect.each(lambda r: [r[0], r[1] + r[3] / 2 - 2, r[2], 4])
            box.kids.append(self.panel(self.claim(name + "Track", pool), mid, marks.get("track", "#3c3c3a")))
            try:
                lo, hi, val = float(inp.get("min") or 0), float(inp.get("max") or 100), float(inp.get("value") or 0)
                part = 0.0 if hi <= lo else max(0.0, min(1.0, (val - lo) / (hi - lo)))
            except ValueError:
                part = 0.5
            box.kids.append(self.panel(self.claim(name + "Fill", pool),
                                       rect.each(lambda r: [r[0], r[1] + r[3] / 2 - 2, r[2] * part, 4]), marks.get("on", "#e8782b")))
            knob = Wd("ButtonWidgetClass", self.claim(name + "Knob", pool),
                      rect.each(lambda r: [r[0] + (r[2] - 14) * part, r[1] + r[3] / 2 - 7, 14, 14]))
            knob.ignore = None
            knob.set("text", '""')
            knob.kids.append(self.panel(self.fresh(pool), knob.rect.copy(), marks.get("on", "#e8782b")))
            box.kids.append(knob)
            self.rep.named.append(f"{name}  Frame (a slider {inp.get('min')}..{inp.get('max')}): {name}Track, {name}Fill and "
                                  f"the button {name}Knob -- the script moves the knob and sizes the fill")
            return out + [box]
        multi = n["t"] == "textarea"
        inner = rect.inset(bw + pad[3], bw + (pad[0] if multi else 0), bw + pad[1], bw + (pad[2] if multi else 0))
        name = self.claim(dz["name"], pool) if dz.get("name") else self.fresh(pool, "edit")
        edit = Wd("MultilineEditBoxWidgetClass" if multi else "EditBoxWidgetClass", name, inner)
        edit.ignore = None
        edit.color = self.col(f.get("col"), st["fades"]) or "#ffffff"
        edit.set("style", "Default")
        rule = self.font_rule(f) or {}
        px = f.get("px", 14)
        ipx = str(int(px)) if float(px).is_integer() else str(px)
        face = (rule.get("edit") or {}).get(ipx)
        if face:
            edit.set("font", f'"{face}"').set(key("exact text"), "1")
        else:
            self.font_props(edit, f)
            self.rep.fonts["edit box %s %gpx  (no edit face of that size: an edit box draws a bitmap face at its own size)"
                           % (f.get("fam"), px)] = 1
        if multi:
            lh = f.get("lh") or px * 1.3
            edit.set("lines", fmt(max(1, int(inner.a[3] // lh))))
        halign = {"right": "right", "end": "right", "center": "center"}.get(f.get("ta", ""), "")
        if halign and not multi:
            edit.set(key("text halign"), halign)
        edit.text = inp.get("value", "")
        edit.dyn = True
        if dz.get("name"):
            self.rep.named.append(f"{name}  {'MultilineEditBox' if multi else 'EditBox'}")
        else:
            self.rep.unnamed_inputs += 1
        target = holder.kids if holder else out
        if inp.get("ph") and not multi:
            ph = Wd("TextWidgetClass", self.claim(name + "Ph", pool), inner.copy())
            ph.color = self.col(inp.get("phc") or {"c": "#5c5a58"}, st["fades"])
            self.font_props(ph, f)
            ph.set(key("text halign"), halign or "left").set(key("text valign"), "center")
            ph.text = self.caption(inp["ph"], f)
            ph.note = "placeholder"
            if inp.get("value"):
                ph.hidden_real = False
                ph.props.append(("_demo_hidden", "1"))
            target.append(ph)
        target.append(edit)
        return out

    def emit_select(self, n: dict, st: dict) -> list[Wd]:
        dz = n.get("dz") or {}
        pool = st["pool"]
        rect = self.r3(n)
        f = n["f"]
        d = self.decor(n, st["fades"])
        name = self.claim(dz["name"], pool) if dz.get("name") else self.fresh(pool, "pick")
        box = Wd("ButtonWidgetClass", name, rect)
        box.ignore = None
        box.set("text", '""')
        self.fill_decor(box, d, rect, pool, n)
        pad = n.get("pad") or [0, 8, 0, 8]
        bw = d["uniform"][0] if d["uniform"] else 0
        label = Wd("TextWidgetClass", self.claim(name + "Text", pool), rect.inset(bw + max(pad[3], 4), 0, bw + 22, 0))
        label.color = self.col(f.get("col"), st["fades"]) or "#ffffff"
        self.font_props(label, f)
        label.set(key("text halign"), "left").set(key("text valign"), "center")
        label.text = n["inp"].get("value", "")
        label.dyn = True
        box.kids.append(label)
        caret = (self.cfg.get("glyphs") or {}).get("▾")
        if caret and self.icons.set:
            side = max(8.0, f.get("px", 14) * 0.75)
            cr = rect.each(lambda r: [r[0] + r[2] - bw - 8 - side, r[1] + (r[3] - side) / 2, side, side])
            arrow = Wd("ImageWidgetClass", self.fresh(pool), cr)
            arrow.color = self.col(f.get("col"), st["fades"]) or "#ffffff"
            arrow.set("image0", f'"set:{self.icons.set} image:{caret["icon"]}"').set("mode", "blend").set(key("src alpha"), "1")
            box.kids.append(arrow)
        self.rep.named.append(f"{name}  ButtonWidget (a select: the script cycles or lists its options "
                              f"{json.dumps(n['inp'].get('opts', []), ensure_ascii=False)[:120]}); its caption is {label.name}")
        return [box]

    def caption(self, text: str, f: dict) -> str:
        """A static caption as the layout holds it: a stringtable key when one says exactly this."""
        t = text.replace("\u00a0", " ")
        if f.get("tt") == "uppercase":
            t = t.upper()
        elif f.get("tt") == "lowercase":
            t = t.lower()
        k = self.strings.key_of(t)
        if k:
            return k
        if "#" in t:
            # in a layout's text a # starts a stringtable key wherever it stands: the rest of
            # the word is looked up and, not being a key, drawn without the sign (or not at all)
            note = f'a "#" in the caption «{t}» will not be drawn as written: give the caption a stringtable key'
            if note not in self.rep.notes:
                self.rep.notes.append(note)
        if re.search(r"[^\W\d_]", t) or "#" in t:
            self.rep.captions[t] = self.rep.captions.get(t, 0) + 1
        return t

    def emit_text(self, k: dict, st: dict) -> list[Wd]:
        pool = st["pool"]
        f = k["f"]
        lines = k["lines"]
        rect = self.r3(k)
        parent = st["parent_el"]
        owner = self.text_owner(k, st)
        dz = (owner.get("dz") or {}) if owner else {}
        col = self.col(f.get("col"), st["fades"]) or "#ffffff"
        if k.get("g"):
            g = (self.cfg.get("glyphs") or {}).get(k["g"])
            if g and self.icons.set:
                px = f.get("px", 14)
                side = px * g.get("scale", 0.7)
                dy = px * g.get("dy", 0.0)
                gr = rect.each(lambda r: [r[0] + (r[2] - side) / 2, r[1] + (r[3] - side) / 2 + dy, side, side])
                iname = dz.get("icon")
                first = iname and self.first_run(owner, True) is k
                w = Wd("ImageWidgetClass", self.claim(iname, pool) if first else self.fresh(pool), gr)
                w.color = col
                w.set("image0", f'"set:{self.icons.set} image:{g["icon"]}"').set("mode", "blend").set(key("src alpha"), "1")
                if first:
                    self.rep.named.append(f"{w.name}  ImageWidget (the sign {k['g']}: icon {g['icon']})")
                return [w]
        dyn_name = dz.get("text") if owner and self.first_run(owner, False) is k else None
        label_name = dz.get("label") if owner and not dyn_name and self.first_run(owner, False) is k else None
        wrapped = len(lines) > 1
        if wrapped:
            w = Wd("RichTextWidgetClass", self.claim(dyn_name or label_name, pool) if (dyn_name or label_name) else self.fresh(pool), rect)
            size = self.font_props(w, f, wrapped=True)
            # the paragraph takes its container's width: the engine breaks the lines itself
            room = self.room_of(k, st)
            rule = self.font_rule(f) or {}
            dy = (rule.get("wrap") or {}).get("dy", 0)

            def para(which):
                tr = getattr(rect, which)
                hi = self.limits(room, which)[1] if room else tr[0] + tr[2]
                return [tr[0], tr[1] + dy, max(tr[2], hi - tr[0]), tr[3] + 4]
            w.rect = R3.of(para)
            w.set("wrap", "1").set(key("size to text h"), "0").set(key("size to text v"), "0")
            w.set(key("text halign"), {"center": "center", "right": "right", "end": "right"}.get(f.get("ta", ""), "left"))
            w.set(key("text valign"), "top")
        else:
            w = Wd("TextWidgetClass", self.claim(dyn_name or label_name, pool) if (dyn_name or label_name) else self.fresh(pool), rect)
            self.font_props(w, f)
            # a text a script fills is not bounded by its own sign (the script places the sign)
            room = self.room_of(k, st, own_signs=not dyn_name)
            align = self.align_of(k, room, rect, dz, st)
            w.rect = self.text_rect(k, room, rect, align)
            w.set(key("text halign"), align).set(key("text valign"), "center")
        w.color = col
        text = k["text"]
        if '"' in text:
            text = text.replace('"', "'")
            self.rep.drop('a double quote in a text (written as an apostrophe: the engine hangs on it)')
        if dyn_name:
            w.dyn = True
            w.text = text.upper() if f.get("tt") == "uppercase" else text
            self.rep.named.append(f"{w.name}  {'RichText' if wrapped else 'TextWidget'} (filled by script)")
        else:
            w.text = self.caption(text, f)
            if label_name:
                self.rep.named.append(f"{w.name}  {'RichText' if wrapped else 'TextWidget'} (a caption: recolour only)")
        if f.get("td"):
            self.rep.drop("text-decoration " + f["td"])
        return [w]

    def text_owner(self, k: dict, st: dict) -> dict | None:
        """The element whose marks (data-dz-text, -icon, -align) speak for this run: the
        nearest ancestor that carries one of them, reached through bare wrappers only."""
        for el in reversed(st["anc"]):
            dz = el.get("dz") or {}
            if "text" in dz or "label" in dz or "icon" in dz or "align" in dz:
                return el
            if dz or el.get("bg") or el.get("bd"):
                return None
        return None

    def first_run(self, el: dict, glyph: bool) -> dict | None:
        """The first run of text under an element: the first glyph, or the first plain run."""
        for x in el.get("k") or []:
            if "text" in x:
                if bool(x.get("g")) == glyph:
                    return x
            elif "t" in x:
                got = self.first_run(x, glyph)
                if got is not None:
                    return got
        return None

    def content_box(self, el: dict) -> R3:
        pad = el.get("pad") or [0, 0, 0, 0]
        bws = {b["s"]: b["w"] for b in (el.get("bd") or [])}
        return self.r3(el).inset(pad[3] + bws.get("l", 0), pad[0] + bws.get("t", 0),
                                 pad[1] + bws.get("r", 0), pad[2] + bws.get("b", 0))

    def boxed(self, n: dict) -> bool:
        """Whether an element becomes a container of its own (so a text inside it stays inside)."""
        dz = n.get("dz") or {}
        of = n.get("of") or ["visible", "visible"]
        # not `click`: the pointer cursor is inherited by every span inside a button
        holds = any(k in dz for k in ("name", "kind", "row", "hidden", "empty"))
        return bool(n.get("bg") or n.get("bd") or n.get("sh") or holds or n["t"] in ("button", "a", "label")
                    or of[0] != "visible" or of[1] != "visible")

    def room_of(self, k: dict, st: dict, own_signs: bool = True):
        """What bounds a line of text on its left and right: the nearest things on the same
        line, else the content box of the nearest container. Returns (box element, left
        neighbour, right neighbour); the neighbours may be None."""
        line = self.a[k["i"]]
        y0, y1 = line[1], line[1] + line[3]
        child = k
        left = right = None
        box_el = None
        for el in reversed(st["anc"]):
            box_el = el
            clash = False
            for x in el.get("k") or []:
                if x is child:
                    continue
                if not own_signs and x.get("g") and child is k:
                    continue
                r = self.a[x["i"]]
                if r[2] <= 0 or r[3] <= 0 or r[1] + r[3] <= y0 + 1 or r[1] >= y1 - 1:
                    continue
                if r[0] >= line[0] + line[2] - 1:
                    if right is None or r[0] < self.a[right["i"]][0]:
                        right = x
                elif r[0] + r[2] <= line[0] + 1:
                    if left is None or r[0] + r[2] > self.a[left["i"]][0] + self.a[left["i"]][2]:
                        left = x
                else:
                    clash = True
            if clash:
                return None
            if left is not None or right is not None or self.boxed(el):
                break
            child = el
        if box_el is None:
            return None
        return box_el, left, right

    def limits(self, room, which: str):
        box_el, left, right = room
        rects = getattr(self, which)
        box = getattr(self.content_box(box_el), which)
        lo, hi = box[0], box[0] + box[2]
        if left is not None:
            r = rects[left["i"]]
            lo = max(lo, r[0] + r[2] + 2)
        if right is not None:
            hi = min(hi, rects[right["i"]][0] - 2)
        return lo, hi

    def align_of(self, k: dict, room, rect: R3, dz: dict, st: dict | None = None) -> str:
        if dz.get("align") in ("left", "center", "right"):
            return dz["align"]
        if room is None:
            return "left"
        box_el, left, right = room
        lo, hi = self.limits(room, "a")
        lgap = rect.a[0] - lo
        rgap = hi - (rect.a[0] + rect.a[2])
        ta = k["f"].get("ta", "")
        if left is None and right is None:
            whole = self.a[box_el["i"]]
            wl = rect.a[0] - whole[0]
            wr = (whole[0] + whole[2]) - (rect.a[0] + rect.a[2])
            centred = abs(wl - wr) <= 2.0 and wl > 1.5
            if centred and (ta == "center" or box_el.get("jc") == "center" or box_el["t"] == "button"):
                return "center"
            if ta in ("right", "end") and rgap <= 2.0 and lgap > 2.0:
                return "right"
        # what the larger size showed: a text that went with its container's right edge
        box = self.content_box(box_el)
        grow = box.b[2] - box.a[2]
        if grow > 4 and right is None:
            moved = (rect.b[0] - box.b[0]) - (rect.a[0] - box.a[0])
            if abs(moved - grow) <= 1.5 and rgap <= 2.5:
                return "right"
            if left is None and abs(moved - grow / 2) <= 1.5 and abs(lgap - rgap) <= 2.0:
                return "center"
        # ... or with the right edge of the container the line lives in (a value at the end
        # of a row that stretches)
        if st is not None and right is None:
            holder = None
            for el in reversed(st["anc"]):
                if self.boxed(el):
                    holder = el
                    break
            if holder is not None:
                ha, hb = self.a[holder["i"]], self.b[holder["i"]]
                grow = hb[2] - ha[2]
                moved = (rect.b[0] - hb[0]) - (rect.a[0] - ha[0])
                if grow > 4 and abs(moved - grow) <= 1.5:
                    return "right"
        return "left"

    def text_rect(self, k: dict, room, rect: R3, align: str) -> R3:
        """A line's widget: the measured line, widened into the room its line leaves, so a
        longer caption in another language, or a longer value, still fits."""
        if room is None:
            return rect.inset(0, 0, -2, 0)

        def fit(which):
            tr = getattr(rect, which)
            lo, hi = self.limits(room, which)
            if align == "center" and room[1] is None and room[2] is None:
                whole = getattr(self, which)[room[0]["i"]]
                return [whole[0] + 1, tr[1], whole[2] - 2, tr[3]]
            if hi - lo < tr[2]:
                return [tr[0], tr[1], tr[2] + 2, tr[3]]
            if align == "center":
                return [lo, tr[1], hi - lo, tr[3]]
            if align == "right":
                return [lo, tr[1], tr[0] + tr[2] - lo, tr[3]]
            return [tr[0], tr[1], hi - tr[0], tr[3]]
        return R3.of(fit)

    # ---- the tree after emission

    @staticmethod
    def inside(r, p, slack: float = 0.6) -> bool:
        return (r[0] >= p[0] - slack and r[1] >= p[1] - slack and r[0] + r[2] <= p[0] + p[2] + slack
                and r[1] + r[3] <= p[1] + p[3] + slack)

    def spills(self, kid: Wd, parent: Wd) -> bool:
        """A panel and a frame clip their children: what reaches outside must leave."""
        if parent.cls in ("ScrollWidgetClass", "WrapSpacerWidgetClass", "ButtonWidgetClass"):
            return False
        if ("clipchildren", "1") in parent.props or kid.cls == "WrapSpacerWidgetClass":
            return False
        slack = 6.0 if kid.cls in ("TextWidgetClass", "RichTextWidgetClass") else 0.6
        return not self.inside(kid.rect.a, parent.rect.a, slack)

    def lift(self, w: Wd) -> list[Wd]:
        """Returns the children of `w` that must leave it, after the same below."""
        kept: list[Wd] = []
        for kid in w.kids:
            gone = self.lift(kid)
            kept.append(kid)
            kept += gone
        w.kids = []
        out: list[Wd] = []
        for kid in kept:
            if self.spills(kid, w):
                out.append(kid)
            else:
                w.kids.append(kid)
        return out

    # ---- rendering

    def flex_code(self, w: Wd, parent: Wd) -> int:
        """How the widget follows its parent's size, in twelfths: for each of x, y, width and
        height the share of the parent's growth it takes. Three steps were measured; where
        they disagree one of them holds a jump of the browser's own (a line of text that
        unwraps, a box that moves up a row), and the middle value is the even rule."""
        p = [getattr(parent.rect, s_) for s_ in SIZES]
        r = [getattr(w.rect, s_) for s_ in SIZES]

        def share(i: int, coord: int) -> float | None:
            by = (p[i + 1][2] - p[i][2]) if coord in (0, 2) else (p[i + 1][3] - p[i][3])
            if abs(by) < 0.5:
                return None
            if coord < 2:
                delta = (r[i + 1][coord] - p[i + 1][coord]) - (r[i][coord] - p[i][coord])
            else:
                delta = r[i + 1][coord] - r[i][coord]
            return delta / by

        ks = []
        jumped = False
        for coord in range(4):
            got = [v for v in (share(i, coord) for i in range(3)) if v is not None]
            if not got:
                ks.append(0)
                continue
            got.sort()
            mid = got[len(got) // 2]
            if got[-1] - got[0] > 0.02:
                jumped = True
            ks.append(max(0, min(TWELFTHS, rnd(mid * TWELFTHS))))
        if jumped:
            self.reflows += 1
            if len(self.rep.flex_bad) < 10:
                self.rep.flex_bad.append("%s %s" % (w.cls.replace("WidgetClass", ""), w.name))
        kx, ky, kw, kh = ks
        if not (kx or ky or kw or kh):
            return 0
        return FLEX_FLAG | kx | (ky << 4) | (kw << 8) | (kh << 12)

    def render(self, w: Wd, parent: Wd | None, demo: bool, depth: int, index: int, out: list[str], top: bool = False) -> None:
        if w.row_inst and not demo:
            return
        pad = " " * depth
        ra = w.rect.a
        if parent is None:
            x, y = 0, 0
            x0, y0 = rnd(ra[0]), rnd(ra[1])
        else:
            pa = parent.rect.a
            x0, y0 = rnd(ra[0]), rnd(ra[1])
            x, y = x0 - rnd(pa[0]), y0 - rnd(pa[1])
        width = rnd(ra[0] + ra[2]) - x0
        height = rnd(ra[1] + ra[3]) - y0
        if w.cls == "WrapSpacerWidgetClass":
            height = 0
        elif width <= 0 or height <= 0:
            return
        self.rep.widgets += 1
        out.append(f"{pad}{w.cls} {w.name} {{")
        p = pad + " "
        hidden = (w.hidden_real and not demo) or (top and self.page.get("hidden") and not demo)
        if demo and ("_demo_hidden", "1") in w.props:
            hidden = True
        out.append(f"{p}visible {'0' if hidden else '1'}")
        # A frame takes the pointer unless told not to, and a frame inside a button then
        # swallows the click meant for the button (measured 2026-11-13: a named badge in a
        # row). No widget written here needs a frame's own pointer, and a widget that
        # ignores it does not hide its children from it: every frame but the page's root
        # ignores the pointer.
        if (w.ignore and w.cls != "FrameWidgetClass") or (w.cls == "FrameWidgetClass" and parent is not None):
            out.append(f"{p}ignorepointer 1")
        if w.color:
            out.append(f"{p}color {color_text(w.color)}")
        out.append(f"{p}position {fmt(x)} {fmt(y)}")
        out.append(f"{p}size {fmt(width)} {fmt(height)}")
        out.append(f"{p}hexactpos 1")
        out.append(f"{p}vexactpos 1")
        out.append(f"{p}hexactsize 1")
        out.append(f"{p}vexactsize 1")
        if parent is not None:
            out.append(f"{p}priority {index}")
        elif not top:
            # a row goes into a container that draws its own ground: over it
            out.append(f"{p}priority 100")
        elif self.page.get("priority", 100) is not False:
            # a page likewise; "priority": false leaves the line out (a window whose
            # order its host keeps)
            out.append(f"{p}priority {self.page.get('priority', 100)}")
        if parent is not None and w.flex:
            code = self.flex_code(w, parent)
            if code:
                out.append(f"{p}userID {code}")
        elif parent is None:
            out.append(f"{p}userID {FLEX_FLAG}")
        if w.text is not None:
            text = "" if (w.dyn and not demo) else w.text
            # a double quote or a line break inside a value hangs the engine's layout parser
            text = " ".join(text.replace('"', "'").split())
            out.append(f'{p}text "{text}"')
        for k, v in w.props:
            if k.startswith("_"):
                continue
            out.append(f"{p}{k} {v}")
        kids = [k for k in w.kids if demo or not k.row_inst]
        if kids:
            out.append(f"{p}{{")
            i = 0
            for kid in kids:
                i += 1
                self.render(kid, w, demo, depth + 2, i, out)
            out.append(f"{p}}}")
        out.append(f"{pad}}}")

    # ---- the page

    def build(self, mode: str) -> Wd:
        self.names = set()
        self.auto_n = 0
        tree = self.probe["tree"]
        self.rows = {}
        st = {"pool": self.names, "fades": [], "under": tree.get("eb", "#000000"), "in_click": False,
              "parent_el": None, "anc": [], "mode": mode}
        root_name = self.claim(self.page.get("root") or (self.page["layout"] + "_root"))
        root = Wd("FrameWidgetClass", root_name, self.r3(tree))
        root.ignore = None
        root.flex = False
        d = self.decor(tree, [])
        self.fill_decor(root, d, root.rect, self.names, tree)
        root.kids += self.emit_kids(tree, st)
        for extra in self.lift(root):
            root.kids.append(extra)
        return root

    def row_template(self, file: str, insts: list[dict]) -> tuple[Wd, set[str]]:
        """The richest instance of a row, as a file of its own."""
        def weight(n):
            own = 1 + (1 if n.get("bg") else 0) + len(n.get("bd") or []) + (1 if n.get("sh") else 0)
            return own + sum(weight(k) for k in (n.get("k") or []) if "t" in k) + sum(1 for k in (n.get("k") or []) if "text" in k)
        best = max(insts, key=weight)
        pool: set[str] = set()
        self.auto_n = 0
        st = {"pool": pool, "fades": [], "under": best.get("eb", "#000000"), "in_click": False, "parent_el": None,
              "anc": [], "mode": "row", "as_row": True}
        dz = dict(best.get("dz") or {})
        root_name = dz.get("name") or file
        node = dict(best)
        node["dz"] = dict(dz, name=root_name)
        made = self.emit_el(node, st)
        if len(made) != 1:
            raise ConvertError(f"row {file}: the element came out as {len(made)} widgets, not one")
        root = made[0]
        root.flex = False
        root.hidden_real = False
        for extra in self.lift(root):
            root.kids.append(extra)
        return root, pool

    def convert(self, demo: bool) -> dict[str, str]:
        files: dict[str, str] = {}
        head = GENERATED + f"{self.page['page']} -- edit the page and convert again, not this file."
        root = self.build("real")
        named = list(self.rep.named)
        lines: list[str] = []
        self.render(root, None, False, 0, 1, lines, top=True)
        files[self.page["layout"] + ".layout"] = head + "\n" + "\n".join(lines) + "\n"
        row_files = dict(self.rows)
        for file, insts in row_files.items():
            rep_before = len(self.rep.named)
            troot, _pool = self.row_template(file, insts)
            row_named = self.rep.named[rep_before:]
            del self.rep.named[rep_before:]
            lines = []
            self.render(troot, None, False, 0, 1, lines)
            files[file + ".layout"] = head + "\n" + "\n".join(lines) + "\n"
            host = self.row_hosts.get(file, "")
            self.rep.rows.append("%s.layout  root %s (%s), %d in the sample, %g x %g units%s\n      %s" % (
                file, troot.name, troot.cls.replace("WidgetClass", ""), len(insts),
                rnd(troot.rect.a[0] + troot.rect.a[2]) - rnd(troot.rect.a[0]),
                rnd(troot.rect.a[1] + troot.rect.a[3]) - rnd(troot.rect.a[1]),
                f", list {host}" if host else ", placed by script",
                "\n      ".join(row_named) if row_named else "(no named parts)"))
        if demo:
            keep = (self.rep.named, self.rep.captions, self.rep.fonts, self.rep.dropped, self.rep.notes, self.rep.widgets,
                    self.rep.flex_bad, self.rep.unnamed_buttons, self.rep.unnamed_inputs, self.rep.rows)
            self.rep = Report()
            reflows = self.reflows
            droot = self.build("demo")
            lines = []
            self.render(droot, None, True, 0, 1, lines, top=True)
            files["_demo_" + self.page["layout"] + ".layout"] = "\n".join(lines) + "\n"
            worst = self.rep.flex_worst
            self.reflows = reflows
            self.rep = Report()
            (self.rep.named, self.rep.captions, self.rep.fonts, self.rep.dropped, self.rep.notes, self.rep.widgets,
             self.rep.flex_bad, self.rep.unnamed_buttons, self.rep.unnamed_inputs, self.rep.rows) = keep
            self.rep.flex_worst = max(worst, self.rep.flex_worst)
        self.rep.named = named
        return files

    def report_text(self, files: dict[str, str]) -> str:
        r = self.rep
        prefix = self.cfg.get("prefix", "")
        out = [f"{self.page_name}: {self.page['page']} -> {prefix}/{self.page['layout']}.layout",
               f"design size {self.probe['size'][0]} x {self.probe['size'][1]}, measured again one, two and three steps of {self.probe['step'][0]} x {self.probe['step'][1]} larger",
               f"widgets: {r.widgets}; {self.reflows} of them the browser re-flows as the page grows (the rule keeps "
               f"them as designed)", ""]
        out.append("FILES")
        for f in files:
            out.append(f"  {prefix}/{f}")
        out.append("")
        out.append("NAMED WIDGETS OF THE PAGE (FindAnyWidget)")
        out += ["  " + x for x in r.named] or ["  (none: mark the page with data-dz)"]
        out.append("")
        if r.rows:
            out.append("ROW TEMPLATES")
            out += ["  " + x for x in r.rows]
            out.append("")
        if r.unnamed_buttons or r.unnamed_inputs:
            out.append(f"WITHOUT A NAME: {r.unnamed_buttons} button(s), {r.unnamed_inputs} edit box(es) -- a script cannot reach them")
            out.append("")
        if r.captions:
            out.append("CAPTIONS WITH NO STRINGTABLE KEY (written into the layout as they are)")
            for t, n in sorted(r.captions.items()):
                out.append(f"  {t}" + (f"  x{n}" if n > 1 else ""))
            out.append("")
        if r.fonts:
            out.append("FONTS")
            out += ["  " + x for x in sorted(r.fonts)]
            out.append("")
        if r.dropped:
            out.append("CSS THE ENGINE HAS NO ANSWER FOR")
            for t, n in sorted(r.dropped.items()):
                out.append(f"  {t}  x{n}")
            out.append("")
        if r.flex_bad:
            out.append("RESIZE: WHERE THE BROWSER RE-FLOWS (a text unwraps, a box changes its row); the first of them")
            out += ["  " + x for x in r.flex_bad]
            out.append("")
        if r.notes:
            out.append("NOTES")
            out += ["  " + x for x in r.notes]
            out.append("")
        return "\n".join(out)


# ----------------------------------------------------------------------------- icons

def render_icons(cfg: dict, found: dict[str, str], names: dict[str, str], base: str, served: Path, work: Path) -> list[str]:
    """Draw every svg white on clear, 64 x 64 a piece, into the config's icons.png folder."""
    ic = cfg.get("icons") or {}
    if not ic.get("png") or not found:
        return []
    from PIL import Image
    out_dir = cfg_path(cfg, ic["png"])
    out_dir.mkdir(parents=True, exist_ok=True)
    cell = 64
    keys = sorted(found)
    cols = 8
    rows = (len(keys) + cols - 1) // cols
    body = []
    for i, k in enumerate(keys):
        svg = re.sub(r"<svg", f'<svg width="{cell - 8}" height="{cell - 8}" style="position:absolute;left:{(i % cols) * cell + 4}px;'
                              f'top:{(i // cols) * cell + 4}px;color:#fff"', found[k], count=1)
        body.append(svg)
    page = ("<!doctype html><meta charset='utf-8'><body style='margin:0;background:transparent;color:#fff'>"
            + "".join(body) + "</body>")
    (served / "css2layout_icons.html").write_text(page, encoding="utf-8")
    shot = work / "icons.png"
    if shot.exists():
        shot.unlink()
    import tempfile
    profile = tempfile.mkdtemp(prefix="css2layout_chrome_")
    cmd = [find_chrome(cfg), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
           "--user-data-dir=" + profile, "--force-device-scale-factor=1",
           "--default-background-color=00000000", f"--window-size={cols * cell},{rows * cell}",
           "--virtual-time-budget=3000", "--screenshot=" + str(shot), base + "/css2layout_icons.html"]
    try:
        subprocess.run(cmd, capture_output=True, timeout=120)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if not shot.exists():
        raise ConvertError("the icons were not drawn: Chrome wrote no picture")
    sheet = Image.open(shot).convert("RGBA")
    written = []
    for i, k in enumerate(keys):
        tile = sheet.crop(((i % cols) * cell, (i // cols) * cell, (i % cols + 1) * cell, (i // cols + 1) * cell))
        # white, with the drawing in alpha: the widget's colour tints it
        alpha = tile.split()[3]
        lum = tile.convert("L")
        from PIL import ImageChops
        a = ImageChops.multiply(alpha, lum)
        white = Image.new("RGBA", tile.size, (255, 255, 255, 0))
        white.putalpha(a)
        name = names.get(k, "i_" + k)
        white.save(out_dir / (name + ".png"))
        written.append(name)
    return written


# ----------------------------------------------------------------------------- the runtime script

def write_runtime(cfg: dict) -> str | None:
    rt = cfg.get("runtime")
    if not rt:
        return None
    text = FLEX_TEMPLATE.read_text(encoding="utf-8").replace("MyMod_", rt.get("prefix", "MyMod_"))
    path = cfg_path(cfg, rt["out"])
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return str(path)
    return None


# ----------------------------------------------------------------------------- running it

def run(config_path, pages: list[str] | None = None, demo: bool = False, reuse: bool = False,
        icons: bool = False, clean_demo: bool = False) -> dict:
    """Convert the pages a config names (all of them when `pages` is empty). Returns what
    was done: per page the files, how many changed, the widgets, the captions without a key;
    raises ConvertError on what stops it. `reuse` takes the last measurement of a page
    instead of asking the browser again; `icons` draws the pages' svg icons as PNGs;
    `clean_demo` removes the `_demo_*.layout` files first (alone: only that)."""
    cfg = load_config(Path(config_path).resolve())
    all_pages = cfg.get("pages") or {}
    todo = list(pages or all_pages)
    out_dir = cfg_path(cfg, cfg["out"])
    report_dir = cfg_path(cfg, cfg.get("report", "css2layout-report"))
    report_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {"pages": {}, "out": str(out_dir), "report": str(report_dir)}
    if clean_demo:
        removed = 0
        for f in out_dir.glob("_demo_*.layout"):
            f.unlink()
            removed += 1
        result["demo_removed"] = removed
        if not pages and not demo and not icons and not reuse:
            return result
    stop = None
    served = None
    if cfg.get("serve"):
        served = cfg_path(cfg, cfg["serve"])
        shutil.copyfile(PROBE, served / "css2layout_probe.html")
        base, stop = serve(served)
    else:
        base = cfg.get("base", "http://127.0.0.1:8000")
    strings = Strings(cfg)
    icon_set = Icons(cfg)
    try:
        for name in todo:
            if name not in all_pages:
                raise ConvertError(f"{name}: no such page in the config (it has: {', '.join(all_pages)})")
            page = all_pages[name]
            cache = report_dir / (page["layout"] + ".measure.json")
            if reuse and cache.exists():
                probe = json.loads(cache.read_text(encoding="utf-8"))
            else:
                probe = measure(cfg, page, base, report_dir)
                cache.write_text(json.dumps(probe, ensure_ascii=False), encoding="utf-8")
            conv = Converter(cfg, name, page, probe, strings, icon_set)
            files = conv.convert(demo)
            out_dir.mkdir(parents=True, exist_ok=True)
            changed = 0
            for fname, text in files.items():
                target = out_dir / fname
                old = target.read_text(encoding="utf-8") if target.exists() else None
                if old != text:
                    target.write_text(text, encoding="utf-8", newline="\n")
                    changed += 1
            (report_dir / (page["layout"] + ".txt")).write_text(conv.report_text(files), encoding="utf-8", newline="\n")
            result["pages"][name] = {"files": list(files), "written": changed, "widgets": conv.rep.widgets,
                                     "reflowed": conv.reflows, "captions_without_key": len(conv.rep.captions),
                                     "unnamed_buttons": conv.rep.unnamed_buttons, "unnamed_inputs": conv.rep.unnamed_inputs,
                                     "report": str(report_dir / (page["layout"] + ".txt"))}
        if icons and served is not None:
            result["icons"] = render_icons(cfg, icon_set.found, icon_set.names, base, served, report_dir)
        elif icon_set.found:
            (report_dir / "icons.json").write_text(json.dumps(icon_set.found, ensure_ascii=False, indent=1), encoding="utf-8")
        rt = write_runtime(cfg)
        if rt:
            result["runtime"] = rt
    finally:
        if stop:
            stop()
        if served is not None and icons:
            try:
                (served / "css2layout_icons.html").unlink()
            except OSError:
                pass
    return result


# ----------------------------------------------------------------------------- the command line

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    try:
        res = run(args[0], args[1:], demo="--demo" in flags, reuse="--reuse" in flags,
                  icons="--icons" in flags, clean_demo="--clean-demo" in flags)
    except ConvertError as e:
        print("css2layout:", e)
        return 1
    if "demo_removed" in res:
        print(f"removed {res['demo_removed']} demo layout(s) from {res['out']}")
    for name, pg in res["pages"].items():
        print(f"{name}: {len(pg['files'])} file(s), {pg['written']} written, {pg['widgets']} widgets, "
              f"{pg['reflowed']} re-flowed by the browser, {pg['captions_without_key']} caption(s) without a key")
    if "icons" in res:
        print(f"icons: {len(res['icons'])} drawn")
    if "runtime" in res:
        print("runtime script written:", res["runtime"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
