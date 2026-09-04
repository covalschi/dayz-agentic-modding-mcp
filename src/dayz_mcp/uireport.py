"""The human half of ui_preview: one HTML file, no external resources, the
screenshot with every widget's rectangle drawn over it and the issues beside
it. Dark, so it does not glare next to the game."""
from __future__ import annotations

import html
import json
from pathlib import Path

from .uigeom import parse_rect

_STYLE = """
body{margin:0;background:#141416;color:#d8d8dc;font:14px system-ui,sans-serif;display:flex;height:100vh}
#stage{position:relative;overflow:auto;flex:1;background:#0b0b0c}
#stage img{display:block;image-rendering:pixelated}
.box{position:absolute;box-sizing:border-box;border:1px solid rgba(120,160,255,.25);pointer-events:none}
.box.error{border:2px solid #ff5a3c;background:rgba(255,90,60,.12)}
.box.warn{border:2px solid #ffc83c;background:rgba(255,200,60,.10)}
.box.hot{border:2px solid #ffffff;background:rgba(255,255,255,.15)}
#side{width:380px;overflow:auto;border-left:1px solid #2a2a2e;padding:12px}
#side h1{font-size:15px;margin:0 0 8px}
#side .meta{color:#8a8a92;font-size:12px;margin-bottom:12px;white-space:pre-wrap}
.issue{padding:6px 8px;border-radius:4px;margin-bottom:6px;cursor:pointer;border-left:4px solid #555}
.issue.error{border-left-color:#ff5a3c}.issue.warn{border-left-color:#ffc83c}
.issue b{display:block}.issue small{color:#9a9aa2}
.note{color:#ffc83c;font-size:12px;margin:8px 0}
"""

_SCRIPT = """
document.querySelectorAll('.issue').forEach(el=>{el.addEventListener('click',()=>{
 document.querySelectorAll('.box.hot').forEach(b=>b.classList.remove('hot'));
 const box=document.querySelector('.box[data-path="'+el.dataset.path+'"]');
 if(box){box.classList.add('hot');box.scrollIntoView({block:'center',inline:'center'});}
 const other=el.dataset.other?document.querySelector('.box[data-path="'+el.dataset.other+'"]'):null;
 if(other)other.classList.add('hot');
});});
"""


def render_html(shot: str | None, nodes: list[dict], issues: list[dict], notes: list[str], meta: dict) -> str:
    host = meta.get("host") or (0, 0, 0, 0)
    hx, hy = int(host[0]), int(host[1])
    # The PNG beside this report is the CLAMPED crop (crop_bgra clamps its
    # own origin to the captured frame, never negative), so a box has to be
    # placed against that same clamped origin -- not against a host
    # rectangle that can start off-frame, to the left of or above it.
    ox, oy = max(0, hx), max(0, hy)
    worst = {}
    for issue in issues:
        worst[issue["path"]] = "error" if issue["severity"] == "error" or worst.get(issue["path"]) == "error" else "warn"
    boxes = []
    for n in nodes:
        rect = parse_rect(n.get("rect", ""))
        if not rect or not n.get("shown", True):
            continue
        x, y, w, h = rect
        cls = worst.get(n["path"], "")
        boxes.append(f'<div class="box {cls}" data-path="{html.escape(n["path"])}" '
                     f'title="{html.escape(n["class"])} {html.escape(n["name"])}" '
                     f'style="left:{x - ox}px;top:{y - oy}px;width:{w}px;height:{h}px"></div>')
    items = []
    for issue in issues:
        items.append(f'<div class="issue {issue["severity"]}" data-path="{html.escape(issue["path"])}" '
                     f'data-other="{html.escape(issue.get("other", ""))}"><b>{html.escape(issue["rule"])} '
                     f'&middot; {html.escape(issue["name"])}</b>{html.escape(issue["detail"])}'
                     f'<small>{html.escape(issue["cls"])} at {html.escape(issue["path"])}</small></div>')
    note_items = "".join(f'<div class="note">{html.escape(n)}</div>' for n in notes)
    picture = f'<img src="{html.escape(shot)}" alt="screenshot">' if shot else '<div class="note">no screenshot</div>'
    meta_text = "\n".join(f"{k}: {v}" for k, v in meta.items())
    counts = f"{sum(1 for i in issues if i['severity'] == 'error')} errors, {sum(1 for i in issues if i['severity'] == 'warn')} warnings, {len(nodes)} nodes"
    return (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{html.escape(str(meta.get('layout', 'preview')))}</title>"
            f"<style>{_STYLE}</style></head><body>"
            f'<div id="stage">{picture}{"".join(boxes)}</div>'
            f'<div id="side"><h1>{html.escape(str(meta.get("layout", "preview")))}</h1>'
            f'<div class="meta">{html.escape(meta_text)}\n{counts}</div>{note_items}{"".join(items)}</div>'
            f"<script>{_SCRIPT}</script></body></html>")


def write_report(out_dir: Path, shot: str | None, nodes: list[dict], issues: list[dict],
                 notes: list[str], meta: dict) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "nodes.json").write_text(json.dumps(nodes, indent=1, ensure_ascii=False), encoding="utf-8")
    (out_dir / "issues.json").write_text(json.dumps(issues, indent=1, ensure_ascii=False), encoding="utf-8")
    report = out_dir / "report.html"
    report.write_text(render_html(shot, nodes, issues, notes, meta), encoding="utf-8")
    return report


def render_gallery(entries: list[dict]) -> str:
    cards = []
    for e in entries:
        counts = e.get("issues") or {}
        summary = f"{counts.get('error', 0)} errors, {counts.get('warn', 0)} warnings" if e.get("ok") else html.escape(e.get("error", "failed"))
        picture = f'<a href="{html.escape(e["report"])}"><img src="{html.escape(e["shot"])}" alt=""></a>' if e.get("shot") else ""
        retried = ' <span class="retried">retried</span>' if e.get("retried") else ""
        # The language sits beside the size in the same <small> tag rather
        # than in one of its own -- absent entirely when the entry carries
        # none, so a gallery run with no `langs` looks exactly as it always
        # has instead of growing an empty label on every card.
        label = html.escape(e.get("size", ""))
        lang = e.get("language") or ""
        if lang:
            label += " &middot; " + html.escape(lang)
        cards.append(f'<div class="card"><h2>{html.escape(e["name"])} <small>{label}</small>{retried}</h2>'
                     f'{picture}<p>{summary}</p></div>')
    style = ("body{margin:0;padding:16px;background:#141416;color:#d8d8dc;font:14px system-ui,sans-serif}"
             ".card{display:inline-block;vertical-align:top;width:460px;margin:0 12px 16px 0;background:#1c1c20;padding:10px;border-radius:6px}"
             ".card img{width:100%;display:block;background:#000}.card h2{font-size:14px;margin:0 0 6px}.card small{color:#8a8a92}"
             ".card .retried{color:#ffc83c}")
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>ui gallery</title><style>{style}</style></head>'
            f'<body>{"".join(cards)}</body></html>')
