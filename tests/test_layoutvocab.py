from pathlib import Path

from dayz_mcp.layoutvocab import build_vocab, load_vocab


def test_the_vocabulary_counts_classes_and_keys(tmp_path):
    (tmp_path / "a.layout").write_text(
        'FrameWidgetClass A {\n size 1 1\n "exact text" 1\n {\n  TextWidgetClass T {\n   size 1 1\n  }\n }\n}\n',
        encoding="utf-8",
    )
    (tmp_path / "broken.layout").write_text("FrameWidgetClass B {\n", encoding="utf-8")
    vocab = build_vocab(tmp_path, build="test")
    assert vocab["build"] == "test"
    assert vocab["files"] == 2
    assert vocab["unparsed"] == ["broken.layout"]
    assert vocab["classes"] == {"FrameWidgetClass": 1, "TextWidgetClass": 1}
    assert vocab["keys"] == {"size": 2, "exact text": 1}


def test_the_shipped_vocabulary_knows_the_common_widgets_and_keys():
    vocab = load_vocab()
    for cls in ("FrameWidgetClass", "PanelWidgetClass", "TextWidgetClass", "ButtonWidgetClass",
                "EditBoxWidgetClass", "ScrollWidgetClass", "WrapSpacerWidgetClass",
                "GridSpacerWidgetClass", "ItemPreviewWidgetClass"):
        assert cls in vocab["classes"], cls
    for key in ("size", "position", "hexactsize", "exact text", "exact text size",
                "text halign", "Size To Content V", "Scrollbar V", "style", "scriptclass"):
        assert key in vocab["keys"], key
    # The BUILD is data, not behaviour: pinned to its exact digits, this test
    # failed the day the vocabulary was regenerated against the next DayZ
    # build -- for no behavioural reason. What has to hold is that the file
    # says WHICH build it was generated from, and that the generator parsed
    # every layout it was given.
    assert vocab["build"].isdigit()
    assert vocab["unparsed"] == []
