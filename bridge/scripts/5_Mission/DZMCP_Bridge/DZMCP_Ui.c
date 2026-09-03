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
    int limit;      // how many may be recorded
    int maxDepth;   // how deep to go; the root is depth 0
    int offset;     // how many visited nodes to skip before recording -- a page after the first
    ref array<string> lines;

    void DZMCP_UiWalk()
    {
        total = 0;
        limit = 0;
        maxDepth = 0;
        offset = 0;
        lines = new array<string>();
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

    // The root a caller asked for, or null with `why` saying which one was
    // missing. "menu" is the open scripted menu's layout root; "screen" is the
    // whole workspace.
    static Widget Root(string which, out string why)
    {
        why = "";
        if (!GetGame())
        {
            why = "there is no game to read a widget tree from";
            return null;
        }

        if (which == "screen")
        {
            Widget workspace = GetGame().GetWorkspace();
            if (!workspace)
                why = "this client has no workspace -- there is no UI to walk";
            return workspace;
        }

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

    // Walk depth-first from `node`, recording at most `walk.limit` nodes and
    // counting every one it visits.
    //
    // Depth-first and in declaration order, so a path recorded now means the
    // same node in the next walk of an unchanged tree. That is the whole basis
    // of addressing a widget by path, and it is why the order is not "whatever
    // the engine hands back".
    static void Walk(Widget node, string path, int depth, DZMCP_UiWalk walk)
    {
        if (!node)
            return;
        if (depth > walk.maxDepth)
            return;

        walk.total++;
        if (walk.total > walk.offset && walk.lines.Count() < walk.limit)
            walk.lines.Insert(Describe(node, path, depth));

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
