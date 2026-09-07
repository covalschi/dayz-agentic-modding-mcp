// Reading the client's widget tree, kept apart from the dispatcher so that
// DZMCP_ClientBridgeCore stays about the protocol and this stays about the UI.
//
// Every signature below was read in the unpacked game sources rather than
// remembered:
//   Widget.GetName()                        1_core/proto/enwidgets.c:121
//   Widget.GetTypeID()                      :124
//   Widget.GetUserID()                      :136
//   Widget.IsVisible() / IsVisibleHierarchy :138 / :139
//   Widget.GetPos/GetSize                   :151 / :152
//   Widget.GetScreenPos/GetScreenSize       :153 / :154
//   Widget.GetParent/GetChildren/GetSibling :158 / :159 / :160
//   Class.ClassName()                       1_core/proto/enscript.c:37
//   MultilineEditBoxWidget.GetText(out)     enwidgets.c:318
//   EditBoxWidget.GetText() / SetText()     :349 / :350
//   ButtonWidget.GetText(out)               :389
//   UIScriptedMenu.GetLayoutRoot()          3_game/tools/uiscriptedmenu.c:75
//   UIManager.GetMenu()                     3_game/tools/uimanager.c:59
//   Game.GetWorkspace()                     3_game/global/game.c:84
//   GetWidgetUnderCursor()                  1_core/proto/enwidgets.c:184 (global)
//
// THE ONE ENGINE LIMIT THAT SHAPES ALL OF THIS: a plain TextWidget has NO
// GetText. In the whole of enwidgets.c the method is declared exactly three
// times -- MultilineEditBoxWidget, EditBoxWidget, ButtonWidget. The label a
// mod draws its numbers into can be WRITTEN from script and never read. So
// this walker reports what a node IS and where it is, and returns a string
// only where the engine lets one be taken. What a mod's UI MEANS is a question
// for the server-side bridge, where the data is real -- and the engine, as it
// turns out, enforces that boundary for us.
//
// FORMATTING RULE, the same one the rest of this mod carries: an Enforce
// statement ends at the end of its line. One statement, one line, however long.

// One walk in progress. A holder rather than static counters: static mutable
// state on a class that two verbs can reach is a bug waiting for the second
// caller.
class DZMCP_UiWalk
{
    int total;      // how many nodes were VISITED, whatever was recorded
    int matched;    // how many of those passed the filter; == total with no filter
    int limit;      // how many may be recorded
    int maxDepth;   // how deep to go; the root is depth 0
    int offset;     // how many MATCHED nodes to skip before recording -- a page after the first
    ref array<string> lines;

    // The filter, applied DURING the walk rather than to what it recorded.
    // Empty means "do not filter on this field". Name and class are exact,
    // text is a substring -- a label's exact string is the one thing a caller
    // rarely knows in advance.
    string wantName;
    string wantClass;
    string wantText;

    void DZMCP_UiWalk()
    {
        total = 0;
        matched = 0;
        limit = 0;
        maxDepth = 0;
        offset = 0;
        lines = new array<string>();
        wantName = "";
        wantClass = "";
        wantText = "";
    }

    bool HasFilter()
    {
        return wantName != "" || wantClass != "" || wantText != "";
    }

    // How many matched, or -1 when nothing was filtered. -1 rather than the
    // visit count: with no filter the two numbers ARE the same, and
    // publishing it twice would invite a reader to compare them for a meaning
    // that is not there.
    int MatchedOrNone()
    {
        if (!HasFilter())
            return -1;
        return matched;
    }
}

class DZMCP_Ui
{
    // A ceiling on ONE listing, not on the tree. The state document is
    // rewritten every tick, and a HUD walked without a bound would make every
    // tick pay for one caller's question. The true count is reported beside
    // the list, so a page never reads as the whole interface.
    static const int NODES_MAX = 300;

    // Depth is bounded separately: a cycle in the tree (which nothing
    // prevents) would otherwise be an endless walk, and an endless walk inside
    // a 1 Hz tick is the one failure an agent cannot diagnose.
    static const int DEPTH_MAX = 32;

    // How much of one node's text travels. Long enough for a label or a field,
    // short enough that one text box cannot fill the document.
    static const int TEXT_LEN = 200;

    // The root a caller asked for, or null with `why` saying what was not
    // there.
    //
    //   "menu"       the open scripted menu's layout root
    //   "screen"     the whole workspace
    //   "workspace"  the same widget, under the name that says what it IS --
    //                the parent of every top-level window, menus and
    //                non-menus alike
    //   anything else: a WIDGET NAME, looked up under the workspace.
    //
    // The last one is why this is not a closed list. A mod may create a window
    // under the workspace root rather than as a UIScriptedMenu -- VPP's admin
    // tools do -- and then "menu" cannot see it at all while "screen" sees it
    // only as node 2300 of a walk. Its own name is the only handle a caller
    // has for it.
    static Widget Root(string which, out string why)
    {
        why = "";
        if (!GetGame())
        {
            why = "there is no game to read a widget tree from";
            return null;
        }

        if (which == "screen" || which == "workspace")
        {
            Widget workspace = GetGame().GetWorkspace();
            if (!workspace)
                why = "this client has no workspace -- there is no UI to walk";
            return workspace;
        }

        if (which != "menu")
            return Named(which, why);

        UIManager manager = GetGame().GetUIManager();
        if (!manager)
        {
            why = "this client has no UI manager";
            return null;
        }
        UIScriptedMenu menu = manager.GetMenu();
        if (!menu)
        {
            why = "no scripted menu is open, so there is no menu root to walk -- use root=screen for the whole workspace";
            return null;
        }
        Widget layout = menu.GetLayoutRoot();
        if (!layout)
            why = "the open menu has no layout root";
        return layout;
    }

    // A root addressed by the widget's own name, searched from the workspace
    // root. The first one with that name, in the walk's own order.
    static Widget Named(string name, out string why)
    {
        if (name == "")
        {
            why = "no root was named -- root is menu, screen, workspace, preview, or the name of a widget";
            return null;
        }

        Widget workspace = GetGame().GetWorkspace();
        if (!workspace)
        {
            why = "this client has no workspace, so a widget cannot be looked up by name";
            return null;
        }

        Widget found = FindNth(workspace, name, 1);
        if (!found)
            why = "no widget is named '" + name + "' anywhere under the workspace -- root is menu, screen, workspace, preview, or the name of a widget";
        return found;
    }

    // What the REAL mouse is over, from the engine's own hit test rather than
    // from a rectangle compared here. Null when the cursor is over nothing, or
    // when there is no game to ask.
    //
    // This is what lets a cursor click answer for itself. A click delivered by
    // the mouse leaves no trace in any script the bridge can reach: the widget
    // under the cursor afterwards, the open menu and the number of top-level
    // widgets are the three observations that CAN be made, and none of them is
    // this mod's opinion about what should have happened.
    static Widget UnderCursor()
    {
        if (!GetGame())
            return null;
        return GetWidgetUnderCursor();
    }

    // How many widgets hang directly off the workspace root, or -1 when there
    // is no workspace to count.
    //
    // The one number that witnesses a window which is NOT a scripted menu. A
    // mod may create a panel under the workspace root (VPP's admin tools do),
    // and then OpenMenuClass() reads identically before and after the click
    // that opened it -- while this count goes up by one.
    //
    // Bounded by the same ceiling as a listing: a sibling chain that loops
    // would otherwise spin here forever, inside the once-a-second tick.
    static int TopLevelCount()
    {
        if (!GetGame())
            return -1;
        Widget workspace = GetGame().GetWorkspace();
        if (!workspace)
            return -1;
        int count = 0;
        Widget child = workspace.GetChildren();
        while (child)
        {
            count++;
            if (count > NODES_MAX)
                break;
            child = child.GetSibling();
        }
        return count;
    }

    // The index path of `wanted` under `node`, in the SAME child/sibling order
    // Walk uses -- so a path answered here means the same node a path from a
    // listing does. False when it is not under this root at all.
    //
    // An out parameter rather than a returned string because the root's own
    // path is "", which is indistinguishable from "not found" as a return
    // value -- and the widget under the cursor really can be the root.
    static bool PathOf(Widget node, Widget wanted, string path, out string found)
    {
        if (!node)
            return false;
        if (node == wanted)
        {
            found = path;
            return true;
        }
        Widget child = node.GetChildren();
        int index = 0;
        while (child)
        {
            string childPath = "" + index;
            if (path != "")
                childPath = path + "." + index;
            if (PathOf(child, wanted, childPath, found))
                return true;
            child = child.GetSibling();
            index++;
            if (index > NODES_MAX)
                break;
        }
        return false;
    }

    // The class of the open menu, or "" when none is open. Its own name, taken
    // from the instance rather than guessed from a table, so a mod's menu
    // reports the mod's own class.
    static string OpenMenuClass()
    {
        if (!GetGame() || !GetGame().GetUIManager())
            return "";
        UIScriptedMenu menu = GetGame().GetUIManager().GetMenu();
        if (!menu)
            return "";
        return menu.ClassName();
    }

    // Does this node pass the walk's filter? True when there is no filter, so
    // an unfiltered walk records everything it visits exactly as before.
    //
    // The test is on the WIDGET, not on the line describing it. An earlier
    // version filtered the recorded lines, which meant the filter only ever
    // saw the first 300 nodes the walk kept -- a window 2300 nodes into the
    // workspace was invisible unless the caller already knew to ask for
    // offset=2300, which is knowledge the search was supposed to produce
    // (measured on the stand 2026-09-06).
    static bool Wanted(Widget node, DZMCP_UiWalk walk)
    {
        if (!walk.HasFilter())
            return true;
        if (walk.wantName != "" && node.GetName() != walk.wantName)
            return false;
        if (walk.wantClass != "" && node.ClassName() != walk.wantClass)
            return false;
        if (walk.wantText != "" && TextOf(node).IndexOf(walk.wantText) < 0)
            return false;
        return true;
    }

    // Walk depth-first from `node`, recording at most `walk.limit` MATCHING
    // nodes and counting every one it visits.
    //
    // Two counts, deliberately: `total` is what was visited and `matched` is
    // what passed the filter. They are the same number for an unfiltered walk,
    // and reporting one for the other is how a page said there was more of the
    // answer to fetch when there was not. `offset` and `limit` page over the
    // matches -- with no filter that is every visited node, exactly as before.
    //
    // Depth-first, in the order GetChildren/GetSibling hand the siblings back:
    // ascending `priority`, stable for equal values (measured 2026-09-04, skill
    // gui-layouts.md, section 24's last bullet) -- NOT the declaration order of the
    // .layout file. The order is fixed for an unchanged tree, so a path recorded
    // now means the same node in the next walk; pairing a path with its source
    // node is the eyes' job (uicheck walks the source the same way).
    static void Walk(Widget node, string path, int depth, DZMCP_UiWalk walk)
    {
        if (!node)
            return;
        if (depth > walk.maxDepth)
            return;

        walk.total++;
        if (Wanted(node, walk))
        {
            walk.matched++;
            if (walk.matched > walk.offset && walk.lines.Count() < walk.limit)
                walk.lines.Insert(Describe(node, path, depth));
        }

        Widget child = node.GetChildren();
        int index = 0;
        while (child)
        {
            string childPath = "" + index;
            if (path != "")
                childPath = path + "." + index;
            Walk(child, childPath, depth + 1, walk);
            child = child.GetSibling();
            index++;
            // A sibling chain that loops would spin here forever. Bounded by
            // the same ceiling as the whole listing: past it there is nothing
            // left to record anyway, and the total already says the tree is
            // bigger than the answer.
            if (index > NODES_MAX)
                break;
        }
    }

    // One node as one line: path, class, name, visibility, screen rectangle,
    // and text where the engine allows it to be read.
    //
    // The separator is `|`, as in the world listing: it cannot appear in a
    // widget class name, and the text is sanitised before it goes in.
    static string Describe(Widget node, string path, int depth)
    {
        float x;
        float y;
        float w;
        float h;
        node.GetScreenPos(x, y);
        node.GetScreenSize(w, h);

        string visible = "0";
        if (node.IsVisible())
            visible = "1";

        string shown = "0";
        if (node.IsVisibleHierarchy())
            shown = "1";

        string rect = "" + Math.Round(x) + " " + Math.Round(y) + " " + Math.Round(w) + " " + Math.Round(h);
        return path + "|" + node.ClassName() + "|" + DZMCP_Text.Sanitize(node.GetName(), TEXT_LEN) + "|" + visible + shown + "|" + rect + "|" + depth + "|" + TextOf(node) + "|" + Metrics(node);
    }

    // The node's text, or "" when the engine gives no way to read it.
    //
    // A PLAIN TextWidget FALLS IN THE SECOND CASE: it has SetText and no
    // GetText. Reporting "" for it is not this walker giving up, it is the
    // engine's answer, and the class name in the same line is what tells the
    // caller which of the two it is looking at.
    //
    // A PASSWORD FIELD IS REFUSED ON PURPOSE. PasswordEditBoxWidget extends
    // EditBoxWidget, so the cast below would happily read one, and this
    // document is written to a file on disk once a second. A tool that
    // harvested somebody's typed password into a JSON file as a side effect of
    // looking at the UI would be indefensible, and no caller asked for it.
    static string TextOf(Widget node)
    {
        PasswordEditBoxWidget secret;
        if (Class.CastTo(secret, node))
            return "";

        EditBoxWidget edit;
        if (Class.CastTo(edit, node))
            return DZMCP_Text.Sanitize(edit.GetText(), TEXT_LEN);

        MultilineEditBoxWidget multi;
        if (Class.CastTo(multi, node))
        {
            string many;
            multi.GetText(many);
            return DZMCP_Text.Sanitize(many, TEXT_LEN);
        }

        ButtonWidget button;
        if (Class.CastTo(button, node))
        {
            string label;
            button.GetText(label);
            return DZMCP_Text.Sanitize(label, TEXT_LEN);
        }

        return "";
    }

    // The rendered text's size in screen pixels, for widgets that derive from
    // TextWidget: labels, multiline and rich text, multiline edit boxes.
    // EditBoxWidget and ButtonWidget extend UIWidget (enwidgets.c:347, :381)
    // and have no GetTextSize, so they report "" -- absence, not zero.
    static string Metrics(Widget node)
    {
        TextWidget text;
        if (!Class.CastTo(text, node))
            return "";
        int sx = 0;
        int sy = 0;
        text.GetTextSize(sx, sy);
        return "" + sx + " " + sy;
    }

    // Resolve an index path -- "0.3.1" -- against a root. Null when any step
    // is missing, which is exactly what a path from a tree that has since
    // changed looks like.
    //
    // "" is the root itself.
    static Widget ByPath(Widget root, string path)
    {
        if (!root)
            return null;
        if (path == "")
            return root;

        array<string> steps = new array<string>();
        path.Split(".", steps);

        Widget at = root;
        for (int i = 0; i < steps.Count(); i++)
        {
            string step = steps.Get(i);
            if (step == "")
                return null;
            int wanted = step.ToInt();
            if (wanted < 0)
                return null;
            Widget child = at.GetChildren();
            int index = 0;
            while (child && index < wanted)
            {
                child = child.GetSibling();
                index++;
            }
            if (!child)
                return null;
            at = child;
        }
        return at;
    }

    // Does the node at a path still look like what the caller expected?
    //
    // The whole reason a path carries an expectation: a tree walked a minute
    // ago is not the tree in front of the mouse now, and clicking "whatever is
    // at 0.3.1 today" is how an automated run presses the wrong button and
    // reports success. Empty expectations mean "do not check", which is the
    // caller's choice and not a default.
    static bool Matches(Widget node, string expectName, string expectClass, out string why)
    {
        why = "";
        if (!node)
        {
            why = "nothing is at that path any more -- walk the tree again";
            return false;
        }
        if (expectName != "" && node.GetName() != expectName)
        {
            why = "the node at that path is named '" + node.GetName() + "', not '" + expectName + "' -- the tree changed since it was walked";
            return false;
        }
        if (expectClass != "" && node.ClassName() != expectClass)
        {
            why = "the node at that path is a " + node.ClassName() + ", not a " + expectClass + " -- the tree changed since it was walked";
            return false;
        }
        return true;
    }

    // The centre of a node's screen rectangle, for the caller that wants to
    // put a real cursor on it.
    static string CentreOf(Widget node)
    {
        float x;
        float y;
        float w;
        float h;
        node.GetScreenPos(x, y);
        node.GetScreenSize(w, h);
        return "" + Math.Round(x + w / 2) + " " + Math.Round(y + h / 2);
    }

    // The nth widget (1-based) named `name` under `root`, depth-first in
    // declaration order -- the order rows created from one template appear
    // in, which is why "the third Line" is a meaningful address.
    static Widget FindNth(Widget root, string name, int nth)
    {
        int seen = 0;
        return FindNthFrom(root, name, nth, seen);
    }

    protected static Widget FindNthFrom(Widget node, string name, int nth, inout int seen)
    {
        if (!node)
            return null;
        if (node.GetName() == name)
        {
            seen++;
            if (seen == nth)
                return node;
        }
        Widget child = node.GetChildren();
        int guard = 0;
        while (child)
        {
            Widget hit = FindNthFrom(child, name, nth, seen);
            if (hit)
                return hit;
            child = child.GetSibling();
            guard++;
            if (guard > NODES_MAX)
                break;
        }
        return null;
    }

    // Spacers lay their children out only when told (enwidgets.c:164 Update;
    // the wiki's most-reported spacer bug). Called once after a fixture.
    static void UpdateSpacers(Widget node)
    {
        if (!node)
            return;
        SpacerWidget spacer;
        if (Class.CastTo(spacer, node))
            spacer.Update();
        Widget child = node.GetChildren();
        int guard = 0;
        while (child)
        {
            UpdateSpacers(child);
            child = child.GetSibling();
            guard++;
            if (guard > NODES_MAX)
                break;
        }
    }
}
