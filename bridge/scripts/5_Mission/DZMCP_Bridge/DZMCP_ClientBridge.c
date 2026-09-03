// The CLIENT half of the bridge.
//
// It is a SUBCLASS of the server half, not a copy of it. Claiming the mailbox,
// the two-stage parse, the watchdog, the terminal dwell, the error ring and
// publishing are the same machinery, hard-won on a live stand, and a second
// copy of them would be a second set of bugs to find. What differs is exactly
// four things -- where the transport files live, which verbs exist, what the
// per-tick refresh looks at, and whether the engine's action log is wanted --
// and each of those is a single overridden method.
//
// What this half can and cannot do is decided by the engine, not by us:
//
// * it can WALK the tree, and every widget in DayZ is built from the same
//   engine classes whether vanilla or a mod drew it, so the walk is universal;
// * it can READ text only from an EditBoxWidget, a MultilineEditBoxWidget or a
//   ButtonWidget. A plain TextWidget -- the label a mod draws its numbers into
//   -- has SetText and no GetText anywhere in enwidgets.c;
// * it can deliver a script-level click only to the OPEN SCRIPTED MENU, via
//   the handler UIManager.GetMenu() hands back. There is no GetHandler on
//   Widget (only SetHandler, enwidgets.c:172), so an arbitrary HUD widget's
//   own handler cannot be reached from here at all. A mod that listens on
//   OnMouseButtonDown, or reads state in Update(), will not be pressed this
//   way -- which is why the tool set also offers a real cursor click, using
//   the screen rectangle this walk reports.
//
// None of the above has been measured on a running client: the owner's
// instruction for this phase was not to run the stand. Every claim here comes
// from the game's own sources, and everything that needs a live client to
// settle is written down as a question in the spec, not assumed here.
class DZMCP_ClientBridgeCore extends DZMCP_BridgeCore
{
    protected ref DZMCP_Preview m_Preview;
    protected int m_LoadDepth;
    protected int m_LoadLimit;
    protected int m_LoadOffset;

    void DZMCP_ClientBridgeCore()
    {
        m_Preview = new DZMCP_Preview();
        m_LoadDepth = DZMCP_Ui.DEPTH_MAX;
        m_LoadLimit = DZMCP_Ui.NODES_MAX;
        m_LoadOffset = 0;
    }

    override protected string CmdPath()
    {
        return DZMCP_CLIENT_CMD_PATH;
    }

    override protected string StatePath()
    {
        return DZMCP_CLIENT_STATE_PATH;
    }

    override protected string Half()
    {
        return "client";
    }

    // The engine's action log exists for the accept/reject decision inside
    // StartDeliveredAction, which is a server path. Nothing here delivers an
    // action, so switching it on would buy nothing and cost log volume.
    override protected bool WantsActionLog()
    {
        return false;
    }

    override protected string KnownVerbs()
    {
        return "ping, ui_tree, ui_find, ui_click, ui_text, ui_load, ui_unload";
    }

    // Deliberately NOT "everything the server knows, plus UI". A client asked
    // to spawn an item would reach for GetPlayers() on a machine that has no
    // authority over anything, and the honest answer to that is the refusal
    // the base class already writes -- naming the verbs this build does know.
    override protected bool IsKnownVerb(string verb)
    {
        if (verb == "ping")
            return true;
        if (verb == "ui_tree" || verb == "ui_find" || verb == "ui_click" || verb == "ui_text")
            return true;
        return verb == "ui_load" || verb == "ui_unload";
    }

    override protected void Dispatch(string verb, string raw)
    {
        // Anything not ours goes to the base, which owns the unknown-verb
        // refusal, the two-stage parse and `ping`. The verb check there calls
        // IsKnownVerb, which is the override above -- so a server verb sent to
        // a client is refused by name rather than half-executed.
        if (verb != "ui_tree" && verb != "ui_find" && verb != "ui_click" && verb != "ui_text" && verb != "ui_load" && verb != "ui_unload")
        {
            super.Dispatch(verb, raw);
            return;
        }

        DZMCP_CommandFull full = new DZMCP_CommandFull();
        string parseError;
        if (!m_Json.ReadFromString(full, raw, parseError))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "the command could not be parsed past its id and verb -- most likely an args value" + " that is not a string: " + Excerpt(parseError));
            return;
        }

        map<string, string> args = full.args;

        if (verb == "ui_tree")
        {
            VerbUiTree(args);
            return;
        }
        if (verb == "ui_find")
        {
            VerbUiFind(args);
            return;
        }
        if (verb == "ui_click")
        {
            VerbUiClick(args);
            return;
        }
        if (verb == "ui_text")
        {
            VerbUiText(args);
            return;
        }
        if (verb == "ui_load")
        {
            VerbUiLoad(args);
            return;
        }
        VerbUiUnload(args);
    }

    // What this half publishes every tick. The base class looks at players,
    // hands and the action manager, none of which a client has authority over;
    // this looks at what the client can see for itself.
    override protected void RefreshWorld()
    {
        m_State.world.ui_menu = DZMCP_Text.Sanitize(DZMCP_Ui.OpenMenuClass(), ID_LEN);
        m_State.world.ui_host = m_Preview.HostRect();
        m_State.world.ui_cursor = -1;
        m_State.world.ui_dialog = -1;

        if (!GetGame() || !GetGame().GetUIManager())
            return;

        UIManager manager = GetGame().GetUIManager();
        if (manager.IsCursorVisible())
            m_State.world.ui_cursor = 1;
        else
            m_State.world.ui_cursor = 0;

        if (manager.IsDialogVisible())
            m_State.world.ui_dialog = 1;
        else
            m_State.world.ui_dialog = 0;
    }

    // ---- the verbs ---------------------------------------------------------

    protected bool RootNameOk(string which)
    {
        return which == "menu" || which == "screen" || which == "preview";
    }

    protected Widget ResolveRoot(string which, out string why)
    {
        why = "";
        if (which == "preview")
        {
            Widget loaded = m_Preview.LoadedRoot();
            if (!loaded)
                why = "no preview is loaded -- ui_load a layout first";
            return loaded;
        }
        return DZMCP_Ui.Root(which, why);
    }

    protected void PublishWalk(string which, DZMCP_UiWalk walk)
    {
        m_State.world.ui_root = which;
        m_State.world.ui_total = walk.total;
        m_State.world.ui_nodes.Clear();
        for (int i = 0; i < walk.lines.Count(); i++)
            m_State.world.ui_nodes.Insert(walk.lines.Get(i));
    }

    // ui_tree: the widget tree, as a page.
    //
    //   root   "menu" (the open scripted menu, the default) or "screen"
    //   depth  how deep to go; the root is depth 0
    //   limit  how many nodes to RECORD. The number VISITED is reported whole
    //          either way, so a page never reads as the whole interface.
    //   offset how many visited nodes to skip before recording -- a page
    //          after the first
    protected void VerbUiTree(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|root|depth|limit|offset|", "root, depth, limit, offset"))
            return;

        string which = ArgOr(args, "root", "menu");
        if (!RootNameOk(which))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_tree: root must be menu, screen or preview, not " + Excerpt(which));
            return;
        }

        string why;
        Widget root = ResolveRoot(which, why);
        if (!root)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_tree: " + why);
            return;
        }

        DZMCP_UiWalk walk = new DZMCP_UiWalk();
        walk.maxDepth = ReadBoundedInt(args, "depth", DZMCP_Ui.DEPTH_MAX, 1, DZMCP_Ui.DEPTH_MAX);
        walk.limit = ReadBoundedInt(args, "limit", DZMCP_Ui.NODES_MAX, 1, DZMCP_Ui.NODES_MAX);
        walk.offset = ReadBoundedInt(args, "offset", 0, 0, 100000);

        DZMCP_Ui.Walk(root, "", 0, walk);

        PublishWalk(which, walk);

        FinishCommand(DZMCP_STATUS_DONE, "listed " + walk.lines.Count() + " of " + walk.total + " widget(s) under the " + which + " root");
    }

    // ui_find: the same walk, filtered.
    //
    // Filtered HERE rather than by the caller, because the caller would have
    // to receive the whole tree to filter it -- and the whole tree is exactly
    // what the page limit exists to avoid sending.
    //
    // Exact, case-sensitive matching on name and class; `text` is a substring,
    // because a label's text is the one field nobody knows exactly in advance.
    protected void VerbUiFind(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|root|name|class|text|depth|limit|offset|", "root, name, class, text, depth, limit, offset"))
            return;

        string which = ArgOr(args, "root", "menu");
        if (!RootNameOk(which))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_find: root must be menu, screen or preview, not " + Excerpt(which));
            return;
        }

        string wantName = ArgOr(args, "name", "");
        string wantClass = ArgOr(args, "class", "");
        string wantText = ArgOr(args, "text", "");
        if (wantName == "" && wantClass == "" && wantText == "")
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_find needs at least one of name, class or text -- with none of them it is ui_tree");
            return;
        }

        string why;
        Widget root = ResolveRoot(which, why);
        if (!root)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_find: " + why);
            return;
        }

        DZMCP_UiWalk walk = new DZMCP_UiWalk();
        walk.maxDepth = ReadBoundedInt(args, "depth", DZMCP_Ui.DEPTH_MAX, 1, DZMCP_Ui.DEPTH_MAX);
        // The walk itself is unfiltered and bounded by the node ceiling; the
        // filter runs over its lines. Filtering inside the walk would make the
        // "visited" count mean something different from ui_tree's, and two
        // counts with one name is how a number stops being comparable.
        walk.limit = DZMCP_Ui.NODES_MAX;
        walk.offset = ReadBoundedInt(args, "offset", 0, 0, 100000);
        DZMCP_Ui.Walk(root, "", 0, walk);

        int limit = ReadBoundedInt(args, "limit", DZMCP_Ui.NODES_MAX, 1, DZMCP_Ui.NODES_MAX);
        m_State.world.ui_root = which;
        m_State.world.ui_total = walk.total;
        m_State.world.ui_nodes.Clear();

        int kept = 0;
        for (int i = 0; i < walk.lines.Count(); i++)
        {
            string line = walk.lines.Get(i);
            if (!LineMatches(line, wantName, wantClass, wantText))
                continue;
            kept++;
            if (m_State.world.ui_nodes.Count() < limit)
                m_State.world.ui_nodes.Insert(line);
        }

        FinishCommand(DZMCP_STATUS_DONE, "matched " + kept + " widget(s) of " + walk.total + " walked under the " + which + " root; listed " + m_State.world.ui_nodes.Count());
    }

    // ui_click: press a widget through the open menu's own handler.
    //
    //   path          index path from the root, as ui_tree reports it
    //   expect_name   what the caller believes is there
    //   expect_class  likewise
    //   button        mouse button number, 0 by default
    //
    // The expectation is the point. A tree walked a minute ago is not the tree
    // in front of the mouse now, and pressing "whatever is at 0.3.1 today" is
    // how an automated run presses the wrong button and reports success. Both
    // expectations are optional, because sometimes the caller genuinely means
    // "whatever is there" -- but that is then the caller's own decision, taken
    // in the open.
    protected void VerbUiClick(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|root|path|expect_name|expect_class|button|deliver|", "root, path, expect_name, expect_class, button, deliver"))
            return;

        string which = ArgOr(args, "root", "menu");
        if (!RootNameOk(which))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_click: root must be menu, screen or preview, not " + Excerpt(which));
            return;
        }

        if (!HasArg(args, "path"))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_click needs a path argument -- take one from ui_tree or ui_find");
            return;
        }

        string why;
        Widget root = ResolveRoot(which, why);
        if (!root)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_click: " + why);
            return;
        }

        string path = args.Get("path");
        Widget node = DZMCP_Ui.ByPath(root, path);
        if (!DZMCP_Ui.Matches(node, ArgOr(args, "expect_name", ""), ArgOr(args, "expect_class", ""), why))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_click: " + why);
            return;
        }

        // deliver=none: check the path and report where the node IS, without
        // pressing anything. That is how the real-cursor tract asks "is this
        // still the widget I meant, and where is it on screen" -- one
        // validation path for both tracts, rather than a second verb that
        // could drift out of step with this one.
        string deliver = ArgOr(args, "deliver", "handler");
        if (deliver != "handler" && deliver != "none")
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_click: deliver must be handler or none, not " + Excerpt(deliver));
            return;
        }
        if (deliver == "none")
        {
            // The node goes into the document in the SAME shape every listing
            // uses, not into the sentence. A caller that had to read a
            // rectangle out of prose would be parsing an error message, and
            // error messages are the one thing in this protocol allowed to be
            // reworded.
            m_State.world.ui_root = which;
            m_State.world.ui_total = 1;
            m_State.world.ui_nodes.Clear();
            m_State.world.ui_nodes.Insert(DZMCP_Ui.Describe(node, path, 0));
            FinishCommand(DZMCP_STATUS_DONE, "found " + node.ClassName() + " '" + node.GetName() + "' at " + path + "; centre " + DZMCP_Ui.CentreOf(node) + "; nothing was pressed");
            return;
        }

        // The handler is the open menu's, and there is no other. Widget has
        // SetHandler and no GetHandler, so an arbitrary widget's own handler
        // cannot be reached from script -- this is not a shortcut, it is the
        // whole of what the engine offers.
        if (!GetGame() || !GetGame().GetUIManager())
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_click: this client has no UI manager");
            return;
        }
        UIScriptedMenu menu = GetGame().GetUIManager().GetMenu();
        if (!menu)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_click: no scripted menu is open, so there is no handler to deliver a click to -- click it with the real cursor instead, using the rectangle ui_tree reports");
            return;
        }

        int button = ReadBoundedInt(args, "button", 0, 0, 7);
        float x;
        float y;
        node.GetScreenPos(x, y);

        bool handled = menu.OnClick(node, Math.Round(x), Math.Round(y), button);
        if (handled)
        {
            FinishCommand(DZMCP_STATUS_DONE, "clicked " + node.ClassName() + " '" + node.GetName() + "' at " + path + "; the menu's handler took it");
            return;
        }

        // NOT a failure of this tool, and said so. A handler that returns false
        // means the menu did not act on the click -- which is a real answer
        // about the mod, and the caller decides whether to try the cursor.
        FinishCommand(DZMCP_STATUS_DONE, "delivered a click to " + node.ClassName() + " '" + node.GetName() + "' at " + path + ", and the menu's handler did NOT take it (OnClick returned false) -- the widget may handle a different event, or none: try the real cursor at " + DZMCP_Ui.CentreOf(node));
    }

    // ui_text: write into an edit box.
    //
    // Only an EditBoxWidget (and what extends it) can be written this way. A
    // plain TextWidget has SetText too, but writing a mod's label from the
    // outside would change what the player sees without changing anything the
    // mod believes -- a lie drawn on the screen -- so it is refused rather than
    // quietly allowed.
    protected void VerbUiText(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|root|path|text|expect_name|expect_class|", "root, path, text, expect_name, expect_class"))
            return;

        string which = ArgOr(args, "root", "menu");
        if (which != "menu" && which != "screen")
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_text: root must be menu or screen, not " + Excerpt(which));
            return;
        }
        if (!HasArg(args, "path"))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_text needs a path argument -- take one from ui_tree or ui_find");
            return;
        }
        if (!HasArg(args, "text"))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_text needs a text argument");
            return;
        }

        string why;
        Widget root = DZMCP_Ui.Root(which, why);
        if (!root)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_text: " + why);
            return;
        }

        Widget node = DZMCP_Ui.ByPath(root, args.Get("path"));
        if (!DZMCP_Ui.Matches(node, ArgOr(args, "expect_name", ""), ArgOr(args, "expect_class", ""), why))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_text: " + why);
            return;
        }

        EditBoxWidget box;
        if (!Class.CastTo(box, node))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_text: " + node.ClassName() + " is not an edit box -- only a field the player could type into may be written");
            return;
        }

        box.SetText(args.Get("text"));

        // Read back rather than reported. SetText is native and returns
        // nothing, so "it was set" is otherwise this tool's own claim about
        // itself -- the same trap as trusting binarize's exit code.
        string now = box.GetText();
        FinishCommand(DZMCP_STATUS_DONE, "set " + node.ClassName() + " '" + node.GetName() + "'; it now reads " + Excerpt(now));
    }

    // ui_load: show a layout file under the preview host and list it.
    //
    //   layout   path relative to the pbo prefix, forward slashes (required)
    //   host     "w h" in layout units, or empty for the whole screen
    //   fixture  JSON of operations to populate it (next task)
    //   depth, limit, offset  as for ui_tree
    //
    // Answers on the NEXT tick: a widget measured before its first layout
    // pass reports zero rectangles (measured), so the walk waits one.
    protected void VerbUiLoad(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|layout|host|fixture|depth|limit|offset|", "layout, host, fixture, depth, limit, offset"))
            return;
        string layout = ArgOr(args, "layout", "");
        if (layout == "")
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_load needs a layout argument -- a path relative to the pbo prefix, like MyMod/gui/layouts/x.layout");
            return;
        }
        string why;
        if (!m_Preview.Load(layout, ArgOr(args, "host", ""), why))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_load: " + why);
            return;
        }
        string fixture = ArgOr(args, "fixture", "");
        if (fixture != "" && !m_Preview.ApplyFixture(fixture, why))
        {
            m_Preview.Unload();
            FinishCommand(DZMCP_STATUS_FAILED, "ui_load: " + why);
            return;
        }
        m_LoadDepth = ReadBoundedInt(args, "depth", DZMCP_Ui.DEPTH_MAX, 1, DZMCP_Ui.DEPTH_MAX);
        m_LoadLimit = ReadBoundedInt(args, "limit", DZMCP_Ui.NODES_MAX, 1, DZMCP_Ui.NODES_MAX);
        m_LoadOffset = ReadBoundedInt(args, "offset", 0, 0, 100000);
        DeferCompletion(1);
    }

    override protected void CompleteDeferred()
    {
        if (m_CmdVerb != "ui_load")
        {
            super.CompleteDeferred();
            return;
        }
        Widget root = m_Preview.LoadedRoot();
        if (!root)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "ui_load: the preview vanished before it could be walked");
            return;
        }
        DZMCP_UiWalk walk = new DZMCP_UiWalk();
        walk.maxDepth = m_LoadDepth;
        walk.limit = m_LoadLimit;
        walk.offset = m_LoadOffset;
        DZMCP_Ui.Walk(root, "", 0, walk);
        PublishWalk("preview", walk);
        FinishCommand(DZMCP_STATUS_DONE, "loaded " + m_Preview.LayoutPath() + " under a host of " + m_Preview.HostRect() + " (scale " + m_Preview.ScaleText() + "); listed " + walk.lines.Count() + " of " + walk.total + " widget(s)");
    }

    // ui_unload: remove the preview and give the HUD back.
    protected void VerbUiUnload(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|", "(none)"))
            return;
        if (!m_Preview.IsLoaded())
        {
            FinishCommand(DZMCP_STATUS_DONE, "nothing was loaded, so nothing was removed");
            return;
        }
        string was = m_Preview.LayoutPath();
        m_Preview.Unload();
        FinishCommand(DZMCP_STATUS_DONE, "removed the preview of " + was);
    }

    // ---- shared argument reading ------------------------------------------

    // One bounded integer argument. Out-of-range is CLAMPED rather than
    // refused: these are page sizes and depths, where the caller's intent
    // ("as much as you can") is clear and a refusal would only cost a round
    // trip. A value that is not a number at all is a different matter and is
    // refused by name.
    protected int ReadBoundedInt(map<string, string> args, string key, int fallback, int low, int high)
    {
        if (!HasArg(args, key))
            return fallback;
        string text = args.Get(key);
        if (!DZMCP_World.IsNumeric(text))
            return fallback;
        int value = Math.Round(text.ToFloat());
        if (value < low)
            return low;
        if (value > high)
            return high;
        return value;
    }

    // Does one described node match the filter? Split on the same separator
    // the description was built with, so there is one definition of the shape
    // and not two.
    //
    // Fields: path | class | name | visibility | rect | depth | text | text size
    //
    // SIX FIELDS IS A WHOLE LINE, not a truncated one, and demanding seven
    // made this verb answer "matched 0" to every question ever asked of it.
    // The description ends with the text field, most widgets have no text of
    // their own -- the label is a child TextWidget -- so most lines end in a
    // trailing separator with nothing after it, and Split gives back six
    // parts. Measured on a live client 2026-08-31: 505 nodes walked, 0
    // matched, for a name ui_tree had just printed.
    //
    // It failed in the worst possible direction: not an error, an empty
    // result. "That widget is not on screen" and "I dropped every line before
    // looking" read identically to the caller.
    protected bool LineMatches(string line, string wantName, string wantClass, string wantText)
    {
        array<string> parts = new array<string>();
        line.Split("|", parts);
        if (parts.Count() < 6)
            return false;

        // Absent because the widget has none -- the same thing as empty.
        string text = "";
        if (parts.Count() >= 7)
            text = parts.Get(6);

        if (wantClass != "" && parts.Get(1) != wantClass)
            return false;
        if (wantName != "" && parts.Get(2) != wantName)
            return false;
        if (wantText != "" && text.IndexOf(wantText) < 0)
            return false;
        return true;
    }
}
