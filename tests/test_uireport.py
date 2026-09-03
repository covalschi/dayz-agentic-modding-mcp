import json
from pathlib import Path

from dayz_mcp.uireport import render_html, write_report

NODES = [{"path": "", "class": "FrameWidget", "name": "Root", "visible": True, "shown": True,
          "rect": "100 50 400 300", "depth": 0, "text": "", "text_size": None},
         {"path": "0", "class": "TextWidget", "name": "Label", "visible": True, "shown": True,
          "rect": "110 60 200 20", "depth": 1, "text": "", "text_size": (240, 20)}]
ISSUES = [{"rule": "text_overflow", "severity": "error", "path": "0", "name": "Label",
           "cls": "TextWidget", "detail": "240x20 in 200x20", "other": ""}]
META = {"layout": "a/b.layout", "host": (100, 50, 400, 300), "window": "3840x1600", "emulated": False}


def test_the_report_is_one_file_with_boxes_relative_to_the_host():
    html = render_html("shot.png", NODES, ISSUES, ["a note"], META)
    assert "<img" in html and 'src="shot.png"' in html
    # the label's box is placed relative to the host origin, not the screen
    assert 'data-path="0"' in html
    assert "left:10px" in html and "top:10px" in html and "width:200px" in html
    assert "text_overflow" in html and "a note" in html
    assert "http" not in html.split("<body")[-1]  # no external resources


def test_write_report_writes_the_three_files(tmp_path):
    report = write_report(tmp_path, "shot.png", NODES, ISSUES, [], META)
    assert report == tmp_path / "report.html"
    assert json.loads((tmp_path / "nodes.json").read_text(encoding="utf-8"))[1]["name"] == "Label"
    assert json.loads((tmp_path / "issues.json").read_text(encoding="utf-8"))[0]["rule"] == "text_overflow"
    assert "Label" in report.read_text(encoding="utf-8")


from dayz_mcp.uireport import render_gallery


def test_the_gallery_links_every_entry_with_its_counts():
    html = render_gallery([
        {"name": "chat", "size": "3840x1600", "ok": True, "report": "../preview-chat-1/report.html",
         "shot": "../preview-chat-1/shot.png", "issues": {"error": 2, "warn": 1}, "error": ""},
        {"name": "map", "size": "3840x1600", "ok": False, "report": "", "shot": "", "issues": {}, "error": "no layout"},
    ])
    assert "chat" in html and "2 errors" in html and 'href="../preview-chat-1/report.html"' in html
    assert "no layout" in html
