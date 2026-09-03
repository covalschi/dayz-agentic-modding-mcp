// A layout shown on demand, for looking at it: the eyes of the layout work.
//
// The previewed file is created under a host of the bridge's own (see
// dzmcp_preview_host.layout), walked on the NEXT tick -- a widget measured
// before its first layout pass reports zeros -- and removed by ui_unload or
// by the next ui_load. The HUD is hidden while a preview is up so a screenshot
// shows the layout and nothing else, and shown again on unload.
//
// One statement per line, as everywhere in this mod.

// One fixture operation, as the JSON carries it. Defaults in the constructor:
// the deserializer leaves an absent member at whatever the constructor set.
class DZMCP_FixtureOp
{
    string op;
    string layout;
    string into;
    int count;
    string name;
    int nth;
    string text;
    string color;

    void DZMCP_FixtureOp()
    {
        op = "";
        layout = "";
        into = "";
        count = 1;
        name = "";
        nth = 1;
        text = "";
        color = "";
    }
}

class DZMCP_Fixture
{
    ref array<ref DZMCP_FixtureOp> ops;

    void DZMCP_Fixture()
    {
        ops = new array<ref DZMCP_FixtureOp>();
    }
}

class DZMCP_Preview
{
    static const string HOST_LAYOUT = "DZMCP_Bridge/gui/layouts/dzmcp_preview_host.layout";

    protected Widget m_Root;
    protected Widget m_Host;
    protected Widget m_Loaded;
    protected string m_Layout;
    protected bool m_HudHidden;

    void DZMCP_Preview()
    {
        m_Root = null;
        m_Host = null;
        m_Loaded = null;
        m_Layout = "";
        m_HudHidden = false;
    }

    bool IsLoaded()
    {
        return m_Loaded != null;
    }

    Widget LoadedRoot()
    {
        return m_Loaded;
    }

    string LayoutPath()
    {
        return m_Layout;
    }

    // "x y w h" of the host in screen pixels, "" when nothing is loaded.
    string HostRect()
    {
        if (!m_Host)
            return "";
        float x;
        float y;
        float w;
        float h;
        m_Host.GetScreenPos(x, y);
        m_Host.GetScreenSize(w, h);
        return "" + Math.Round(x) + " " + Math.Round(y) + " " + Math.Round(w) + " " + Math.Round(h);
    }

    bool Load(string layout, string hostSpec, out string why)
    {
        why = "";
        Unload();
        WorkspaceWidget workspace = GetGame().GetWorkspace();
        if (!workspace)
        {
            why = "this client has no workspace";
            return false;
        }
        m_Root = workspace.CreateWidgets(HOST_LAYOUT);
        if (!m_Root)
        {
            why = "the bridge's own host layout did not load: " + HOST_LAYOUT;
            return false;
        }
        if (!PickHost(hostSpec, why))
        {
            Unload();
            return false;
        }
        m_Loaded = workspace.CreateWidgets(layout, m_Host);
        if (!m_Loaded)
        {
            why = "nothing loaded from '" + layout + "' -- the path is relative to the pbo prefix with forward slashes, and CreateWidgets returns nothing for a file it cannot find";
            Unload();
            return false;
        }
        m_Layout = layout;
        HideHud();
        return true;
    }

    bool ApplyFixture(string json, out string why)
    {
        why = "";
        if (!m_Loaded)
        {
            why = "no layout is loaded to apply a fixture to";
            return false;
        }
        DZMCP_Fixture fixture = new DZMCP_Fixture();
        JsonSerializer reader = new JsonSerializer();
        string parseError;
        if (!reader.ReadFromString(fixture, json, parseError))
        {
            why = "the fixture does not parse: " + parseError;
            return false;
        }
        for (int i = 0; i < fixture.ops.Count(); i++)
        {
            DZMCP_FixtureOp op = fixture.ops.Get(i);
            if (!op)
            {
                why = "fixture op " + i + " is not an object";
                return false;
            }
            string opWhy;
            if (!ApplyOp(op, opWhy))
            {
                why = "fixture op " + i + " (" + op.op + "): " + opWhy;
                return false;
            }
        }
        DZMCP_Ui.UpdateSpacers(m_Loaded);
        return true;
    }

    protected bool ApplyOp(DZMCP_FixtureOp op, out string why)
    {
        why = "";
        if (op.op == "add")
            return OpAdd(op, why);
        if (op.op == "text")
            return OpText(op, why);
        if (op.op == "show")
            return OpShow(op, true, why);
        if (op.op == "hide")
            return OpShow(op, false, why);
        if (op.op == "color")
            return OpColor(op, why);
        why = "unknown op '" + op.op + "'; this build knows add, text, show, hide, color";
        return false;
    }

    protected Widget Target(DZMCP_FixtureOp op, out string why)
    {
        why = "";
        if (op.name == "")
        {
            why = "needs a name";
            return null;
        }
        Widget node = DZMCP_Ui.FindNth(m_Loaded, op.name, op.nth);
        if (!node)
            why = "no widget named '" + op.name + "' (occurrence " + op.nth + ") under the loaded layout";
        return node;
    }

    protected bool OpAdd(DZMCP_FixtureOp op, out string why)
    {
        why = "";
        if (op.layout == "")
        {
            why = "add needs a layout";
            return false;
        }
        if (op.count < 1)
        {
            why = "add: count must be at least 1, not " + op.count;
            return false;
        }
        if (op.count > 500)
        {
            why = "add: count must be at most 500, not " + op.count;
            return false;
        }
        Widget into = m_Loaded;
        if (op.into != "")
        {
            into = DZMCP_Ui.FindNth(m_Loaded, op.into, 1);
            if (!into)
            {
                why = "no container named '" + op.into + "' under the loaded layout";
                return false;
            }
        }
        for (int i = 0; i < op.count; i++)
        {
            Widget made = GetGame().GetWorkspace().CreateWidgets(op.layout, into);
            if (!made)
            {
                why = "nothing loaded from '" + op.layout + "' on copy " + (i + 1);
                return false;
            }
        }
        return true;
    }

    protected bool OpText(DZMCP_FixtureOp op, out string why)
    {
        Widget node = Target(op, why);
        if (!node)
            return false;
        TextWidget text;
        if (Class.CastTo(text, node))
        {
            text.SetText(op.text);
            return true;
        }
        EditBoxWidget edit;
        if (Class.CastTo(edit, node))
        {
            edit.SetText(op.text);
            return true;
        }
        ButtonWidget button;
        if (Class.CastTo(button, node))
        {
            button.SetText(op.text);
            return true;
        }
        why = "'" + op.name + "' is a " + node.ClassName() + ", which carries no text";
        return false;
    }

    protected bool OpShow(DZMCP_FixtureOp op, bool show, out string why)
    {
        Widget node = Target(op, why);
        if (!node)
            return false;
        node.Show(show);
        return true;
    }

    protected bool OpColor(DZMCP_FixtureOp op, out string why)
    {
        Widget node = Target(op, why);
        if (!node)
            return false;
        array<string> parts = new array<string>();
        op.color.Split(" ", parts);
        if (parts.Count() != 4)
        {
            why = "color must be four fractions 0..1 'a r g b', not '" + op.color + "'";
            return false;
        }
        // Mirrors DZMCP_World.TextToPos: a native ToFloat() on garbage
        // silently answers 0, which is a legal fraction and therefore
        // indistinguishable from a real one -- so every token is checked
        // numeric, and in range, before any of them is trusted.
        for (int i = 0; i < parts.Count(); i++)
        {
            string part = parts.Get(i);
            if (!DZMCP_World.IsNumeric(part))
            {
                why = "color must be four fractions 0..1 'a r g b', not '" + op.color + "'";
                return false;
            }
            float value = part.ToFloat();
            if (value < 0 || value > 1)
            {
                why = "color must be four fractions 0..1 'a r g b', not '" + op.color + "'";
                return false;
            }
        }
        node.SetColor(ARGBF(parts.Get(0).ToFloat(), parts.Get(1).ToFloat(), parts.Get(2).ToFloat(), parts.Get(3).ToFloat()));
        return true;
    }

    void Unload()
    {
        if (m_Root)
            m_Root.Unlink();
        m_Root = null;
        m_Host = null;
        m_Loaded = null;
        m_Layout = "";
        if (m_HudHidden)
        {
            ShowHud(true);
            m_HudHidden = false;
        }
    }

    // Empty spec: the whole screen. "w h": a centred frame of that many layout
    // units, so proportional children lay out as on a screen of that size.
    protected bool PickHost(string spec, out string why)
    {
        Widget fill = m_Root.FindAnyWidget("HostFill");
        Widget fixed = m_Root.FindAnyWidget("HostFixed");
        if (!fill || !fixed)
        {
            why = "the host layout lacks HostFill or HostFixed";
            return false;
        }
        if (spec == "")
        {
            m_Host = fill;
            return true;
        }
        array<string> parts = new array<string>();
        spec.Split(" ", parts);
        if (parts.Count() != 2)
        {
            why = "host must be two numbers like '1306 518', not '" + spec + "'";
            return false;
        }
        float w = parts.Get(0).ToFloat();
        float h = parts.Get(1).ToFloat();
        if (w <= 0 || h <= 0)
        {
            why = "host must be two positive numbers, not '" + spec + "'";
            return false;
        }
        fill.Show(false);
        // SetSize takes LAYOUT UNITS -- HostFixed's hexactsize/vexactsize
        // flags are what turn them into screen pixels. SetScreenSize takes
        // pixels directly, so calling it here with a layout-unit "w h" spec
        // sized the host wrong (measured 2026-09-03: a "1306 518" host came
        // back 1306x518 PIXELS, not layout units).
        fixed.SetSize(w, h);
        fixed.Show(true);
        m_Host = fixed;
        return true;
    }

    protected void HideHud()
    {
        ShowHud(false);
        m_HudHidden = true;
    }

    protected void ShowHud(bool show)
    {
        if (!GetGame() || !GetGame().GetMission())
            return;
        Hud hud = GetGame().GetMission().GetHud();
        if (hud)
            hud.Show(show);
    }
}
