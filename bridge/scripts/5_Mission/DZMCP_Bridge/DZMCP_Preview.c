// A layout shown on demand, for looking at it: the eyes of the layout work.
//
// The previewed file is created under a host of the bridge's own (see
// dzmcp_preview_host.layout), walked on the NEXT tick -- a widget measured
// before its first layout pass reports zeros -- and removed by ui_unload or
// by the next ui_load. The HUD is hidden while a preview is up so a screenshot
// shows the layout and nothing else, and shown again on unload.
//
// One statement per line, as everywhere in this mod.
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

    // Placeholder until the fixture task: refuse rather than pretend.
    bool ApplyFixture(string json, out string why)
    {
        why = "fixtures are not supported by this bridge build";
        return false;
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
        fixed.SetScreenSize(w, h);
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
