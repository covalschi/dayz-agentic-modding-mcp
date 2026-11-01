# GUI: .layout files, widgets, menus (native DayZ UI)

Native UI mechanics: layout format, widget wiring, events,
menus, imagesets, drawing. Script APIs were first verified on 1.29 (`scripts.pbo` 124708:
`1_core/proto/enwidgets.c`, `3_game/gameplay.c`, `3_game/tools/uiscriptedmenu.c` and friends)
and re-checked on 1.30.164200 on 2026-11-01: `enwidgets.c`, `uiscriptedmenu.c`, `uimanager.c`,
`widgeteventhandler.c` and `3_game/tools/tools.c` are unchanged, and script line numbers and
vanilla layout counts below are 1.30.164200's unless a section says otherwise. .layout/.imageset file-format
facts come from community documentation (not greppable in scripts.pbo) and are flagged where
unverified.

## 1. .layout file format

Brace-delimited `key value` pairs (no `=`, NOT XML). One root widget per file; children nest
in a `{ }` block after the parent's attributes. Widget type names in layouts always end in
`Class` (`TextWidgetClass`, `ButtonWidgetClass`...).

```
FrameWidgetClass MenuRoot {
 position 0 0
 size 1 1
 hexactpos 0
 vexactpos 0
 hexactsize 0
 vexactsize 0
 {
  TextWidgetClass Title {
   text "Research"
   "exact text" 1
   "exact text size" 18
  }
 }
}
```

Rules (community-documented format; exercised by the shipped example layouts):

- **Exact flags** — dual coordinate system per axis: `hexactpos` / `vexactpos` /
  `hexactsize` / `vexactsize`. `0` = proportional 0.0–1.0 of parent, `1` = pixels.
  Standard responsive pattern: proportional width + pixel height (`size 1 30, hexactsize 0,
  vexactsize 1`).
- **Alignment refs** — `halign left|center_ref|right_ref`, `valign top|center_ref|bottom_ref`
  move the position *reference point*. Center a dialog: `halign center_ref` + `valign
  center_ref` + exact pos `0 0` — NOT proportional `position 0.5 0.5` (that puts the
  top-left corner at screen center).
- **`priority`** = z-order among SIBLINGS only (children always render above parent).
  Use 998–999 for modal roots. A root that holds an `ItemPreviewWidget` can sit that high
  too if the preview's OWN priority is 201, as in vanilla's tooltip (§13, re-measured
  2026-11-01: at 201 the model drew under every root from 1 to 998; at 300+ it never drew;
  below 100 it vanished as soon as any ancestor was at 100+).
- **Multi-word attribute names MUST be quoted**: `"exact text" 1`, `"exact text size" 14`,
  `"bold text" 1`, `"text halign" center` — unquoted silently fails.
- **Colors** are 4 floats 0–1 in layouts (`color 0.09 0.09 0.09 0.9`) but `0xAARRGGBB` int
  in script `SetColor()` (enwidgets.c:144). Helpers: `ARGB(a,r,g,b)` 0–255, `ARGBF` floats.
- Negative `size` values = undefined rendering. Never.
- `"no focus" 1` also breaks gamepad/keyboard navigation to that widget.
- `scaled 1` = respect the user's HUD Size setting (affects pixel-mode dims).
- Misc: `ignorepointer 1` (clicks pass through), `clipchildren 1`, `visible`, `disabled`,
  `userID N` (readable via `GetUserID()`, enwidgets.c:136).
- **`scriptclass "MyMod_Handler"`** binds a script class to the widget: must inherit
  `Managed` and implement `OnWidgetScriptInit(Widget w)` — otherwise silently null.
  scriptclass names are GLOBAL across all loaded mods — always prefix (`MyMod_`).
  An optional `ScriptParamsClass` block (string/number pairs) goes in a separate `{ }`
  after the children block (community-reported, unverified).
- Perf ceiling: 500+ widgets in one layout tree causes measurable frame drops
  (community-reported). Keep files small; split repeating rows into their own template
  .layout and instantiate per node (see §3).

## 2. Widget catalog

Every widget has 3 identifiers: script class (`TextWidget`), layout class
(`TextWidgetClass`), TypeID constant (`TextWidgetTypeID`) for programmatic `CreateWidget`.
Script classes, all in enwidgets.c unless noted:

| Script class | Key methods (enwidgets.c) | Notes |
|---|---|---|
| `Widget` (base) | Show, Enable, SetPos/SetSize and SetScreenPos/SetScreenSize (all screen px on exact-flag widgets, measured §24/§31), SetColor/SetAlpha/SetRotation, FindAnyWidget :169, SetHandler :172, Unlink :173, SetUserData :135, SetSort, GetParent/GetChildren/GetSibling :158-160, Update :164 | root of all |
| `TextWidget` :189 | SetText, SetTextFormat, SetBold/SetItalic, SetOutline/SetShadow, GetTextSize | **NO GetText()** — cache what you SetText |
| `MultilineTextWidget` :219 | + SetLineBreakingOverride | |
| `RichTextWidget` :224 | GetContentHeight, SetContentOffset(offset, snapToLine), ElideText, GetNumLines | inline tags, see §8 |
| `HtmlWidget` (gameplay.c:316) | LoadFile(path) | extends RichTextWidget |
| `EditBoxWidget` :347 | **GetText()** :349, SetText | needs SetFocus to type, §10 |
| `MultilineEditBoxWidget` :313 | GetText(out), SetLine/GetLine, GetLinesCount | |
| `ButtonWidget` :381 | GetState/SetState, SetText, GetText(out) :389 | only widget with reliable OnClick |
| `CheckBoxWidget` :418 | IsChecked, SetChecked, SetText | |
| `SliderWidget` :358 | SetMinMax, GetCurrent/SetCurrent, SetStep | |
| `ProgressBarWidget` :377 | SetCurrent/GetCurrent, GetMin/GetMax (via SimpleProgressBarWidget) | processing progress |
| `XComboBoxWidget` :407 | AddItem, ClearAll, GetCurrentItem/SetCurrentItem | |
| `TextListboxWidget` :439 | AddItem(text, userData, column, row=-1) :442, SetItem, GetItemText(row,col,out) :454, GetItemData, SetItemColor; GetSelectedRow/SelectRow/ClearItems/EnsureVisible (BaseListboxWidget :425) | layout columns property is engine-typo **`colums`** `"Name;75;Count;25"` (see `menu_filter_list.layout`) |
| `ImageWidget` :247 | LoadImageFile(num, name, noCache=false) :257, SetImage(num) :267, SetMaskProgress | multi-state icons, §6 |
| `CanvasWidget` :341 | DrawLine, Clear — entire API | §7 |
| `ItemPreviewWidget` (gameplay.c:286) | SetItem(EntityAI), SetView, SetModelOrientation/Position, SetForceFlip | §8; since 1.30 extends `PreviewWidget` (:276), which now holds Set/GetModelOrientation/Position + ApplyToCamera |
| `PlayerPreviewWidget` (gameplay.c:305) | SetPlayer(DayZPlayer), UpdateItemInHands, Refresh | also `: PreviewWidget` since 1.30 |
| `GridSpacerWidget` / `WrapSpacerWidget` :473-479 | + AddChildAfter, SetContentAlignmentH/V (SpacerWidget) | dynamic node grids; GridSpacer overrides children's size |
| `ScrollWidget` :481 | GetContentWidth/Height, V/HScrollToPos, VScrollToPos01, VScrollToWidget, GetVScrollPos, IsScrollbarVisible | see gotchas below |
| `VideoWidget`, `RenderTargetWidget`, `RTTextureWidget` | | |
| frames and panels | — | NO script class: there is no `class FrameWidget` or `class PanelWidget` (only `FrameWidgetTypeID`); `ClassName()` says `Widget` (§23a) — hold them as `Widget` |

Gotchas:

- **TextWidget has no GetText()** — only EditBoxWidget (:349), ButtonWidget (:389, `out`
  param) and MultilineEditBoxWidget (:318). Keep displayed strings in your own state.
- **PanelWidget**: layout class `PanelWidgetClass` exists, but there is no `class
  PanelWidget` declaration anywhere in the 1.29 or 1.30.164200 scripts (grep) — cast panels to
  `Widget`. (Community-reported as an engine quirk; the wiki contradicts itself here.)
- **ScrollWidget does NOT auto-scroll** when children are added: call
  `VScrollToPos01(1.0)` / `VScrollToWidget(child)` yourself; content metrics may be stale
  the same frame you add children — defer one frame via
  `GetGame().GetCallQueue(CALL_CATEGORY_GUI).CallLater(...)` (community-reported). Give the
  scroll area `clipchildren 1`.
- `fixaspect` direction (fixwidth vs fixheight semantics) is community-reported and the
  wiki self-contradicts — test empirically before relying on it.
- **ScrollWidget scrollbar visibility follows honest content size** (measured live
  2026-08-30, in a stand mod's device UI): layout key `"Scrollbar V" 1` enables the vertical bar, but the
  engine only draws it when the content canvas is larger than the viewport. If script
  clamps the canvas (`if (want < 430) want = 430; rows.SetSize(w, want)`) the bar renders
  as a permanent full-height white thumb even for one row; dropping the clamp
  (`rows.SetSize(w, rowsY)`) makes the bar disappear whenever content fits.
- **No button style hides a button's children** (re-measured 2026-11-01, DayZ 1.30.164200,
  a stand mod at 3840x1600, a red child panel and a child TextWidget in every button).
  `DayZDefaultButton` paints a solid white plate UNDER the children -- they stay visible --
  and its own `text` is invisible on the plate (§24's "solid white box hiding the text").
  `Default`, `Empty`, no style at all, and the undefined name `DayZButton` paint no
  background: the own text and the children all show (`Empty` draws the own text in a bold
  serif face). Vanilla `dayzwidgets.styles` (1.29, 1.30) defines `Default`, `MenuDefault`,
  `Editor`, `DayZDefaultButton*`, `DayZInventoryButton*`, `Empty`, `Colorable`,
  `EmptyHighlight`, `DayZDigitButton`... -- no `DayZButton`. The 2026-08-29/30 observation
  that a `DayZButton` button hid its children was not reproduced.

### Sizing widgets from script: Set* and Get* are NOT the same units

**Superseded by later measurements:** §23a (2026-09-03), §24's last bullet (2026-09-06) and
§31 (2026-10-31) found `SetPos`/`SetSize`/`SetScreenPos`/`SetScreenSize` and their getters
ALL in screen pixels on exact-flag widgets (`GetScreenSize`/`GetSize` = 1). Convert layout units
with `s = H / 1080` (§24); the 1.48x below was that scale at 1600 rows, not a Set/Get gap.

`SetScreenSize(260, 92)` produced a widget that `GetScreenSize` reported as **385x136**
(measured live, 1.48x — the workspace UI scale). `GetTextSize` is documented as returning
**pixels** (`enwidgets.c:209`), so text measurements are in Get-space, not Set-space.
Mixing the two silently produces cards wider than the slot you computed for them, nodes
that touch, and text that overflows its box.

Do not hard-code the factor — it depends on resolution and the player's UI scale.
Calibrate once per menu open with a throwaway widget, using **two points** so the same
code also absorbs a parent-relative vs absolute difference in `SetScreenPos`:

```c
probe.SetScreenSize(200, 100); probe.GetScreenSize(w1, h1);
probe.SetScreenSize(400, 100); probe.GetScreenSize(w2, h2);
sizeK = (w2 - w1) / 200;                    // Get-пікселів на одиницю Set
probe.SetScreenPos(100, 100);  probe.GetScreenPos(x1, y1);
probe.SetScreenPos(300, 100);  probe.GetScreenPos(x2, y2);
posK = (x2 - x1) / 200;  posB = x1 - posK * 100;   // offset ловить relative-vs-absolute
probe.Unlink();
// далі працюємо в справжніх пікселях:
w.SetScreenPos((targetX - posB) / posK, (targetY - posB) / posK);
w.SetScreenSize(targetW / sizeK, targetH / sizeK);
```

Guard the results (`0.2 < k < 6`, else fall back to 1.0): a widget measured before the
first layout pass returns zeros.

### Resizing a card does NOT resize its children

Children declared with `hexactsize 1` keep their pixel size when you resize the parent
from script — the inner panel sticks out of a shrunken card and the text box keeps its
original width, so text spills. Declare card children **proportionally**
(`hexactpos 0 / vexactpos 0 / hexactsize 0 / vexactsize 0`, position and size as 0..1
fractions of the parent) and one `SetScreenSize` on the card scales the whole card.
Font size stays absolute (`"exact text size"`), so still compute how much text fits.

### ImageWidget: a correct path is not enough (white boxes / tiled backgrounds)

`LoadImageFile(int num, string name, bool noCache = false)` (enwidgets.c:257) accepts three
argument forms, all proven by vanilla usage and interchangeable in one code path:

1. **Imageset ref** `"set:<setname> image:<imagename>"` — the canonical form
   (watermark.c:15; the grammar is spelled out in a source comment at
   attachmentcategoriescontainer.c:164). `<setname>` is the imageset's `Name` field, not a
   path. A **mod's own** imageset only resolves after it is registered in CfgMods
   (`class defs { class imageSets { files[] = {...}; }; }`) — without that block a
   `set:mymod_icons ...` ref silently fails.
2. **Plain VFS texture path**, forward slashes, no leading slash, case-insensitive
   (`"gui/textures/feedback_qr.edds"`, ingamemenuxbox.c:269). Rooted at the PBO prefix, so
   your own texture is `MyMod/gui/textures/foo.edds`. `.edds` and `.paa` both work.
3. GUID-prefixed `"{<hex>}Path.edds"` (uimanager.c:253) — a Workbench resource id, useless
   in a mod's config.

The trap: **both relevant render flags are opt-in**, so a perfectly valid path still looks
broken (`enum WidgetFlags`, enwidgets.c):

- no `"stretch mode" stretch_w_h` → the texture is drawn at its native size and **wraps**,
  i.e. tiles across the widget (line 74 `STRETCH //< Stretch texture to a full size`);
- no `"src alpha" 1` → the texture is treated as opaque, so an alpha glyph renders as a
  solid rectangle (line 59 `SOURCEALPHA`).

Measured across every shipped vanilla layout in `dta/gui.pbo` (a 1.29 count by a method not
recorded here; not re-counted on 1.30): of 177 ImageWidgets bound to
a **raw texture**, 176 declare a stretch property. Of 1392 bound to an **imageset ref**, 989
declare none — imageset entries carry their own Pos/Size rect and are scaled automatically.
Hence the rule: **prefer `set:` refs for icons**; if you accept raw paths (e.g. an
admin-configurable JSON field), put `mode blend` + `"stretch mode" stretch_w_h` +
`"src alpha" 1` on the widget (`mode blend` is the third must, §12). `LoadImageFile` returns
bool — check it; it is the cheapest probe you have.

Useful verified vanilla icons for `set:dayz_gui image:` — `iconBacteria`, `iconPill`,
`iconSkull`, `icon_hammer`, `gear`, `icon_refresh`; `set:dayz_inventory image:missing` is
vanilla's own "no icon" sentinel (staticguiutils.c:11).

## 3. Creating widgets from script

```c
// From a layout file — path relative to PBO packing root, FORWARD slashes
// (a backslash spelling loads too; §32 uses it to escape the layout mix-up):
Widget root = GetGame().GetWorkspace().CreateWidgets("MyMod/gui/layouts/tree.layout", parent);
// enwidgets.c:181. Returns NULL on a bad path, and the only trace is two SCRIPT (E)
// "Cannot load layout" lines in the log (§31) — ALWAYS null-check.
TextWidget title = TextWidget.Cast(root.FindAnyWidget("Title"));   // + null-check

// Programmatic single widget (enwidgets.c:179):
Widget w = GetGame().GetWorkspace().CreateWidget(TextWidgetTypeID, 0, 0, 100, 30,
    WidgetFlags.VISIBLE | WidgetFlags.EXACTSIZE | WidgetFlags.EXACTPOS, 0xFFFFFFFF, 100, parent);
// Without WidgetFlags.EXACTSIZE the 100/30 are treated as PROPORTIONAL (100 = 100x parent!).
```

`WidgetFlags` (enwidgets.c:57): `VISIBLE, EXACTPOS/HEXACTPOS/VEXACTPOS,
EXACTSIZE/HEXACTSIZE/VEXACTSIZE, IGNOREPOINTER, NOFOCUS, CLIPCHILDREN, DRAGGABLE,
SOURCEALPHA, BLEND, ADDITIVE, STRETCH, NOFILTER, DISABLED...`

Per-node template pattern (tech-tree nodes, list rows):

```c
foreach (MyMod_TreeNode node : m_Nodes)
{
    Widget row = GetGame().GetWorkspace().CreateWidgets(
        "MyMod/gui/layouts/tree_node.layout", m_NodeContainer);  // WrapSpacerWidget
    row.SetUserData(node);                       // enwidgets.c:135 — attach payload (Class)
    row.FindAnyWidget("NodeButton").SetHandler(this);
}
m_NodeContainer.Update();   // enwidgets.c:164 — spacers need Update() after add/remove
```

`w.GetUserData(out Class data)` (:134) recovers the payload in event handlers — no separate
widget→node map needed. `SetUserID(int)`/`GetUserID()` (:136) is the lightweight variant
(see `menu_filter_list.layout`).

Cleanup: `w.Unlink()` destroys the widget AND all children (:173) — then null your refs.
When clearing children in a loop, grab `GetSibling()` BEFORE `Unlink()` (:160) or you lose
the iteration chain. Exception: a `UIScriptedMenu`'s `layoutRoot` is destroyed
automatically by the C++ side (uiscriptedmenu.c:162 comment) — don't double-manage it;
hand-rolled panels outside the menu stack must Unlink themselves.

## 4. Widget events

`ScriptedWidgetEventHandler` (enwidgets.c:656) overridables — all return bool:

```
OnClick(w,x,y,button)  OnDoubleClick  OnSelect  OnItemSelected(w,x,y,row,col,oldRow,oldCol)
OnChange(w,x,y,finished)  OnFocus / OnFocusLost  OnMouseEnter / OnMouseLeave(w,enterW,x,y)
OnMouseWheel(w,x,y,wheel)  OnMouseButtonDown / OnMouseButtonUp  OnController(w,control,value)
OnKeyDown / OnKeyUp / OnKeyPress  OnDrag / OnDragging / OnDraggingOver / OnDrop /
OnDropReceived(w,x,y,reciever)  OnResize  OnChildAdd / OnChildRemove  OnUpdate(w)
OnModalResult(w,x,y,code,result)  OnEvent(eventType,target,p0,p1)
```

- **Return true = consumed**; false propagates UP the parent chain only — never to siblings.
- `widget.SetHandler(this)` (:172) attaches **ONE** handler — a later SetHandler replaces
  it silently. The vanilla singleton `WidgetEventHandler.GetInstance()`
  (5_mission/gui/widgeteventhandler.c:22) is a central dispatcher, NOT an additive one:
  `RegisterOnClick(widget, this, "FnName")` (:133) itself calls `w.SetHandler(this)` (:135),
  displacing whatever handler the widget had, and keeps ONE (object, function) per widget
  per event in a `map` filled with `Insert` (:137), which does not overwrite an existing
  key -- so a second mod registering the same widget's click is silently ignored.
  `UnregisterWidget(widget)` (:48) drops every registration of that widget, other mods'
  included, and calls `SetHandler(NULL)` (:67). `RegisterOnMouseButtonDown` (:70) works the
  same way. A misspelled function name registers fine and never fires. (Client-side
  class — 5_Mission.)
- **OnClick fires reliably only on ButtonWidget** — for panels/images/text use
  OnMouseButtonDown/Up (community-reported behavior, matches vanilla usage).
- `OnChange` `finished` semantics: EditBox → true only on Enter (not tab/click-away);
  slider → true on release. Do heavy work only when `finished` (community-reported).
- Defer heavy work out of handlers:
  `GetGame().GetCallQueue(CALL_CATEGORY_GUI).CallLater(this.RebuildTree, 0, false);`
  — `CALL_CATEGORY_GUI = 1` "Runs always (on client)" (tools.c:9).

## 5. UIScriptedMenu lifecycle

**VERIFIED 2026-08-01 (live, cost hours — three separate traps):**

1. **`ShowScriptedMenu` is the WRONG way to open a gameplay menu.** It marks the menu as
   "created hidden" (`IsCreatedHidden()`), and `LockControls()` returns early
   (uiscriptedmenu.c:80-90, `FEATURE_CURSOR` is on in 1.29 and 1.30, defines.c:59) — no
   cursor, no focus, and clicks fall through to the world. (Note 2026-11-01: the source
   comment at uiscriptedmenu.c:20 ties `IsCreatedHidden` to creation through
   `UIManager.CreateScriptedMenu`, not to `ShowScriptedMenu`; the symptom is measured, the
   mechanism is not confirmed.) Vanilla uses it in 3 places: the two login-queue screens
   before Mission exists (dayzgame.c:1892, :1934; 1.29 :1873, :1915) and the INVENTORY --
   `MissionGameplay.ShowInventory` shows the menu it built with
   `CreateScriptedMenu(MENU_INVENTORY, null)` (missiongameplay.c:221, :1203). The other 25+
   gameplay menus and both checked third-party mods (@Trader, @Code Lock) use:
   `GetUIManager().EnterScriptedMenu(MY_ID, NULL)` + `modded class MissionGameplay`
   overriding `CreateScriptedMenu(int id)` to `new MyMenu(); menu.SetID(id);`.

   **BUT `EnterScriptedMenu` silently returns NULL when another menu owns the stack**
   (verified live 2026-08-02, cost several debugging cycles). The mission override is not
   even called — put a log line in `CreateScriptedMenu` and you will see the engine invoke it
   for vanilla ids (11 inventory, 17 chat input) and never for yours. This bites in the two
   most common cases:
   - opening from a CHAT COMMAND — the vanilla chat-input menu (`MENU_CHAT_INPUT` = 17) is
     still on the stack when the server's RPC arrives;
   - opening from inside an ACTION — the action command owns input at that moment;
   - opening from another mod's menu (e.g. a VPP admin submenu) — same thing.

   **Fix: defer by one frame.**
   ```c
   GetGame().GetCallQueue(CALL_CATEGORY_GUI).CallLater(OpenDeferred, 100, false);
   ```
   Then `EnterScriptedMenu` succeeds and brings the whole package with it: `Init()`,
   `OnShow()`, the menu stack, cursor, focus and a working `Close()`.

   The claim "custom MenuIDs must be > 46" is **WRONG** — id magnitude is irrelevant
   (774101 and 121 behave identically). Stack ownership was always the real cause.

   Fallback if the stack is still busy: build the menu yourself, as vanilla does with
   `LoginQueueStatic` (dayzgame.c:203-210 — it calls `Init()` in its own constructor):
   `new MyMenu(); m.SetID(id); m.Init(); GetUIManager().ShowScriptedMenu(m, NULL);`.
   Re-measured 2026-11-01 (DayZ 1.30.164200, a stand mod): `ShowScriptedMenu` calls `Init()`
   a SECOND time and then `OnShow()` once, synchronously, before it returns, and `GetMenu()`
   then returns the menu; `HideScriptedMenu(m)` calls `OnHide()` and `GetMenu()` goes back to
   null. So guard `Init()` against the second call (create the layout only while `layoutRoot`
   is null) and do NOT call `OnShow()`/`OnHide()` yourself -- they would run twice. The older
   note here ("`OnShow()` is NOT called for you", a menu left without data) does not hold on
   1.30.164200; vanilla's inventory, shown the same way, reaches `InventoryMenu.OnShow` too.
2. **`super.OnShow()` does NOT block player movement** — it only touches game-focus/cursor.
   Movement/camera are killed separately, and you must do it yourself:
   `GetGame().GetMission().AddActiveInputExcludes({"menu"})` +
   `AddActiveInputRestriction(EInputRestrictors.INVENTORY)` (+ hide the HUD with
   `hud.ShowHudUI(false)` and `hud.ShowQuickbarUI(false)`: context flags that survive another
   mod's later `hud.Show(true)`, which undoes `hud.Show(false)`; §31), undone in OnHide with
   `RemoveActiveInputExcludes({"menu"}, true)` (and `ShowHudUI(true)` + `ShowQuickbarUI(true)`).
   (missiongameplay.c:1299-1332 `Pause()` + :1334-1349 `Continue()` = the Pause-menu pattern.)
3. **A `PanelWidget` with only `color` renders NOTHING.** `color` is a TINT over the
   style's 9-slice images; PanelWidget has no `Default` style, and `blank`/`ColorablePanel`
   have empty images. For a solid filled panel you MUST add `style rover_sim_colorable`
   (dayzwidgets.styles:3284 — WhitePixel in all 9 slots; used 641× in vanilla layouts).
   Buttons: `style Colorable` (:2167) or `MenuDefault` (:1711). Layout `color` is R G B A.

```c
class MyMod_TreeMenu : UIScriptedMenu     // 3_game/tools/uiscriptedmenu.c:66
{
    override Widget Init()             // :163 — create widgets, return layout root
    {
        layoutRoot = GetGame().GetWorkspace().CreateWidgets("MyMod/gui/layouts/tree.layout");
        // cache child refs, SetHandler(this) on interactive widgets
        return layoutRoot;
    }

    override void Update(float timeslice)
    {
        super.Update(timeslice);
        if (GetUApi().GetInputByID(UAUIBack).LocalPress())   // ESC — vanilla logoutmenu.c:124
            Close();                                          // safe self-close, uiscriptedmenu.c UIMenuPanel:31
    }
}
// Open (trap 1 above): modded class MissionGameplay overrides CreateScriptedMenu(int id) to
//   return a new MyMod_TreeMenu with SetID(id); the caller defers by a frame, then
//   GetGame().GetUIManager().EnterScriptedMenu(MYMOD_MENU_TREE, null);        // uimanager.c:4
// Close: Close() from inside; HideScriptedMenu(menu) (uimanager.c:10) only for a
//   ShowScriptedMenu-ed menu (the fallback above).
```

**Focus balance rule (the #1 UI bug).** Base `OnShow()` calls `LockControls()` =
`ChangeGameFocus(1, <device>)` per input device + `ShowUICursor(true)`; `OnHide()` calls
`UnlockControls()` = `ChangeGameFocus(-1, ...)` (uiscriptedmenu.c:80-134, 173-192). If you
override OnShow/OnHide, call `super.` — and if you hand-roll focus for a non-menu panel,
every `ChangeGameFocus(1)` must be matched by exactly one `ChangeGameFocus(-1)` +
`ShowUICursor(false)`, or player input stays frozen after close.

Native confirmation dialogs — no layout needed (uimanager.c:45):

```c
g_Game.GetUIManager().ShowDialog("Unlock node?", "Spend 5 research points?",
    MYMOD_DIALOG_UNLOCK, DBT_YESNO, DBB_YES, DMT_QUESTION, this);
// Result arrives in the menu's OnModalResult(w, x, y, code, result): code == your id.
```

Constants (gameplay.c:619-633): `DBT_OK=0, DBT_YESNO=1, DBT_YESNOCANCEL=2`;
`DBB_NONE=0, DBB_OK=1, DBB_YES=2, DBB_NO=3, DBB_CANCEL=4`;
`DMT_NONE=0, DMT_INFO=1, DMT_WARNING=2, DMT_QUESTION=3, DMT_EXCLAMATION=4`.

## 6. Imagesets (tree-node icons)

Native `.imageset` (community-documented format):

```
ImageSetClass {
 Name "mymod_icons"
 RefSize 1024 1024
 Textures {
  ImageSetTextureClass { mpix 1  path "MyMod/gui/imagesets/mymod_icons.edds" }
 }
 Images {
  ImageSetDefClass icon_microscope { Name "icon_microscope"  Pos 0 0  Size 64 64  Flags 0 }
 }
 Groups { }
}
```

- **`RefSize` must EXACTLY match the texture's pixel dimensions** or every `Pos` is scaled
  wrong. Atlas dims power-of-two (256–4096, rectangular OK). Keep a few px gutter
  (community advice ranges 2–10 px) around each sprite to prevent bleed at non-1:1 scaling;
  `Size` = sprite dims WITHOUT gutter. `Flags`: 1 = horizontal tile, 2 = vertical tile.
- Texture is `.edds`/`.paa` (PNG → `ImageToPAA.exe` from DayZ Tools).
- **MUST register in config.cpp** `CfgMods ... class defs { class imageSets { files[] =
  {"MyMod/gui/imagesets/mymod_icons.imageset"}; }; };` — missing registration = icons
  silently blank, no error.
- Reference syntax: layout `image0 "set:mymod_icons image:icon_microscope"`; script
  `ImageWidget.LoadImageFile(0, "set:mymod_icons image:icon_locked")` (enwidgets.c:257) —
  load states into slots 0..7, switch with `SetImage(n)` (:267): locked/unlocked node icons.
- Set names are global, last-loaded PBO wins — prefix (`mymod_`).
- Prototype icons free of charge: Dabs Framework ships Font Awesome imagesets
  (solid/regular/light/thin/brands, 64px grid).

## 7. CanvasWidget — tech-tree connector lines

The only script way to draw lines/shapes. Entire API (enwidgets.c:341-345):
`DrawLine(x1, y1, x2, y2, widthPx, colorARGB)` + `Clear()`.

Contract: `Clear()` + full redraw every frame (do it in the menu's `Update()`);
coordinates are pixels relative to the canvas's own top-left; layout needs
`ignorepointer 1` so it doesn't block clicks on node buttons above it. Rectangles/circles =
line segments (36 segments ≈ smooth circle).

Tree edges: one full-size `CanvasWidgetClass` UNDER the node container + per-node layouts
on top. Anchor lines to widget centers via `node.GetScreenPos(x, y)` +
`GetScreenSize(w, h)` (enwidgets.c:153-154), then offset by the canvas's own screen pos.
World-to-screen (device overlays, not the tree): `g_Game.GetScreenPosRelative(worldPos)`
returns 0..1 coords (vanilla actiontargetscursor.c:417) — scale by canvas screen size.
Fine for dozens of lines; no batching, so don't build large production UI from it.

## 8. ItemPreviewWidget & rich text (node detail panel)

`ItemPreviewWidget` (gameplay.c:286-302; since 1.30 it extends `PreviewWidget`, :276-284,
which now declares the orientation/position calls and `ApplyToCamera`): live 3D render of an item.
`SetItem(EntityAI object)`, `GetItem()`, `SetView(int)` — pass `item.GetViewIndex()`
(vanilla inventorygrid.c:215), `SetModelOrientation(vector)`, `SetModelPosition(vector)`,
`SetForceFlipEnable/SetForceFlip`. **Needs a real EntityAI instance** — to preview a
classname from a processing rule, spawn a client-local probe:
`EntityAI probe = EntityAI.Cast(GetGame().CreateObjectEx(classname, "0 0 0", ECE_LOCAL));`
(`ECE_LOCAL = 1073741824` "create object locally", centraleconomy.c:25) and delete it when
the panel closes. Drag-rotate (vanilla pattern, inspectmenunew.c:78-101): in
`OnMouseButtonDown` → `GetMousePos(x, y)` (ensystem.c:329) +
`g_Game.GetDragQueue().Call(this, "UpdateRotation")`; callback signature
`UpdateRotation(int mouse_x, int mouse_y, bool is_dragging)`; apply delta via
`SetModelOrientation`. See `assets/layout-examples/item_preview.layout`.

`RichTextWidget` inline tags (community-confirmed vanilla usage):
`<image set="dayz_gui" name="icon_pin" scale="1.2" />`, `</br>` line break (note the
closing-tag spelling), `<h scale="0.8">heading</h>`. Keybind icon markup:
`InputUtils.GetRichtextButtonIconFromInputAction("UAUISelect", "#label", EUAINPUT_DEVICE_CONTROLLER, InputUtils.ICON_SCALE_TOOLBAR)`
(vanilla controlsxbox.c:337). Long descriptions: `ElideText(line, maxWidth, "...")`,
`GetContentHeight()` + `SetContentOffset(offset, true)` for scrolling (enwidgets.c:224-234).
`HtmlWidget.LoadFile(path)` (gameplay.c:316-318) — vanilla book system.

## 9. Styles & fonts

Layout `style` attribute; style names are global, last-loaded .styles file wins — register
custom ones via CfgMods `defs > widgetStyles` (see mod-structure.md) and prefix them.
Usable vanilla style names (community-reported, unverified — those used by the skill's shipped
examples all occur in vanilla 1.30 layouts): `Default`, `Colorable`,
`rover_sim_colorable` (tintable panel — combine with `color`/SetColor), `rover_sim_black`,
`OutlineFilled`, `MenuDefault`, `DayZNormal`,
`blank`, `Empty`. The shipped examples use `Default`, `rover_sim_colorable`, `Editor` and `Empty`.
(`Outline_1px_BlackBackground`, once listed here as a dialog-root style, is defined nowhere
in the 1.29 or 1.30 `dta/gui.pbo` -- neither in `dayzwidgets.styles` nor in any layout.)

Fonts (paths appear only in layouts, not scripts — community-reported names):
`gui/fonts/Metron`, `Metron28`, `Metron-Bold`, `Metron-Bold58`, and SDF variants
`sdf_MetronBook24`, `sdf_MetronLight24`, `sdf_MetronBook72` (numeric suffix = SDF
generation size; SDF scales crisply — render size set via `"exact text" 1` +
`"exact text size" N`). `"size to text h/v"` auto-fits widget to text. Script-side loaders
exist: `LoadWidgetStyles(filename)` / `LoadWidgetImageSet(filename)` (enwidgets.c:692-693).

## 10. Housekeeping checklist (every menu/panel)

- [ ] `super.OnShow()`/`super.OnHide()` called in overrides (focus stays balanced, §5).
- [ ] Non-menu panels: `m_Root.Unlink(); m_Root = null;` on close (menus: engine destroys
      layoutRoot, §3).
- [ ] Modal/dialog root `priority 998`–`999` so it renders above other UI -- unless the
      dialog holds an `ItemPreviewWidget` (§13: 300+ on it or an ancestor hid the model).
- [ ] `SetFocus(editbox)` (enwidgets.c:698) after opening, or the EditBox won't take typing.
- [ ] Click-outside-to-close: a full-screen transparent `ButtonWidget` (`style Empty`) under
      the dialog that takes `OnClick` -- OnClick is reliable only on buttons (§4) -- or a
      full-screen panel answering `OnMouseButtonDown`.
- [ ] Tab switching (tree / points / log): panel-manager pattern — `map<string, Widget>`
      of named panels, Show(true) one, Show(false) the rest.
- [ ] Any `modded class` touching GUI types (`IngameHud`, `Chat`, menus) wrapped in
      `#ifndef NO_GUI ... #endif` — the dedicated server compiles without the GUI symbol
      space (vanilla missionserver.c does exactly this) and crashes on `Unknown type` otherwise.
- [ ] Every `WidgetEventHandler.GetInstance().Register*` matched by `UnregisterWidget` on close.
- [ ] Dabs ScriptViewMenu users: see frameworks.md (ghost-menu trap).

## 11. Performance

- **Widget pooling**: pre-create N row widgets (`CreateWidgets` + `Show(false)`); acquire =
  Show(true) + repopulate, release = Show(false). Never destroy/recreate on list refresh.
  Thresholds (community-reported): 500+ widgets in a tree or 50+ ScrollWidget children =
  frame drops.
- **Debounce** search/filter EditBoxes ~150 ms (restartable CallLater) before rebuilding.
- **Batch refresh**: rebuild the visible subset per frame (fixed batch size), not the whole
  tree in one handler; defer from event handlers via CALL_CATEGORY_GUI CallLater (§4).
- One shared CanvasWidget for all lines (§7); no per-frame string concatenation in Update().
- Spacer containers: call `Update()` once after a batch of adds/removes, not per child.

## Example layouts (assets/layout-examples/)

Three working starting points, each with a commented script companion in its header.

- `hud_text.layout` — small HUD text overlay (no menu stack, `ignorepointer`): a title line and a
  value line at an exact pixel position; the companion creates it once from `modded class
  MissionGameplay`, updates the value, hides it while a menu is open and unlinks it on mission finish.
- `menu_filter_list.layout` — centered menu: EditBox filter + 2-column TextListbox (note the
  `colums` property) + Select/Close buttons wired via userID; the companion is a `UIScriptedMenu`
  with OnChange/OnClick/OnItemSelected, the cursor and input handling of §5 and `Close()`.
- `item_preview.layout` — item inspect panel: ItemPreviewWidgetClass with every priority under
  256 (§13) + rotate-left/right buttons (`SetModelOrientation`) + close button; the companion
  covers `SetItem`, the rotation and the cleanup.

## Item display name/description come from the ITEM, not from config

`Object.GetDisplayName()` (object.c:476) calls `NameOverride(out string)` FIRST and only
falls back to the config `displayName` when it returns false. `DescriptionOverride` is the
twin (object.c:1349). Both default to `return false`.

Every UI surface goes through `GetDisplayName()`: cargo panel
(cargocontainer.c:292), attachment slots (attachments.c:466), inspect menu
(inspectmenunew.c:168), action-target cursor (actiontargetscursor.c:759, :809).

Practical use: one item class whose visible name is computed at runtime — from a JSON
config, from a hidden field, from anything. Renaming in JSON + a config reload changes every
such item already in the world, with no server restart and no player relog.

---

## 12. ImageWidget: four traps that produce NO error line

Measured on a live client 2026-08-24 while building a PDA-style bezel for a stand mod — a
2048x1024 `.paa` device front with a transparent screen cutout, shown behind the
live widgets. Each of the four was found by a separate boot; **none of them logs
anything**, so the only symptom is the wrong picture.

### The filename suffix decides the PAA format

`ImageToPAA` reads the suffix, not the pixels:

- `_co` — **colour opaque**. Alpha is DISCARDED.
- `_ca` — **colour + alpha**. Alpha survives.

Built once as `mymod_bezel_co.paa`: the cutout came out opaque black and the
frame rendered by its alpha, i.e. light grey. Renaming to `_ca` doubled the file
(125 KB → 252 KB — DXT1 vs DXT5) and fixed it. Vanilla keeps the same
convention: `base_radio_station_co.paa` vs `base_radio_station_glass_ca.paa`.

### `mode blend` is mandatory

```
image0 "MyMod/gui/textures/thing_ca.paa"
mode blend
"src alpha" 1
```

Without `mode`, the default ignores alpha and composites additively: transparent
regions turn opaque and dark colours wash out to light grey. Vanilla always
pairs `image0` with `mode blend` for transparent art and `mode opaque` for
backgrounds (`gui/layouts/xbox/day_z_title_screen.layout`).

### `"stretch mode" stretch_w_h` is mandatory

```
"stretch mode" stretch_w_h
```

Without it the widget does **not** scale the texture — it draws it at native size and
wraps (tiles) it (§2, §31), so a texture larger than the widget looks CROPPED. A 2048x1024
texture in a 1280x640 widget shows exactly the top-left 1280x640 of the image,
so the right and bottom parts of a frame silently vanish while the top and left
draw normally. Vanilla sets it on the full-screen background image in the same
file.

### The widget `color` MULTIPLIES the texture, and the engine brightens

`color 1 1 1 1` does not mean "as authored". The engine draws the texture
noticeably lighter than it sits on disk — consistent with a double gamma
conversion. Verified by converting the `.paa` back to PNG: the frame really is
`(28,29,33)` with alpha 255 and the cutout really is alpha 0, yet it rendered
around `(185,185,185)`.

Because `color` multiplies, calibrate brightness **in the layout**, not by
regenerating the texture. For a graphite panel authored at `(28,29,33)`,
`color 0.38 0.38 0.40 1` looked right at 1600x900.

Re-measured 2026-11-01 (DayZ 1.30.164200, a stand mod, a DXT `_ca` texture of four solid
quadrants over a (64,64,64) panel): with `mode blend` -- and with no `mode` line at all --
the texture drew exactly as authored, (33,32,33), (132,130,132), (198,196,198), (198,97,33)
on screen, the same as PanelWidgets of those colours (§32 agrees). Only `mode additive`
lightened it, adding the background in linear light: 33 over 64 came out 73, 132 came out
145. So the darkening through `color` above compensated for additive blending; a texture
drawn with `mode blend` needs no correction.

### `style "DayZButton"` does not draw over its children (re-measured 2026-11-01)

An earlier note said a button with that style painted its art on top of child widgets.
Re-measured on 1.30.164200, no style does that (§2's gotchas): `DayZDefaultButton` paints a
white plate under the children, and every other style tried paints nothing. A button keeps
receiving `OnClick`, `OnMouseEnter` and `OnMouseLeave` whatever its style, so drawing the
states yourself still works.

### Bezel-behind-widgets ordering

For a device frame with live UI inside the cutout, the SCREEN goes first and the
frame goes on top with a higher `priority`. The frame's transparent cutout is
what reveals the screen. The reverse order hides the screen completely, and
there is no error to say so.

## 13. `ItemPreviewWidget`: high `priority` silently kills the 3D preview

The single loudest trap in this file, because nothing anywhere reports it.

`ItemPreviewWidget.SetItem()` succeeds, `GetItem()` returns the entity,
`GetView()` returns the index you set, the widget is `visible`/`shown` with a
sane rect in `ui_tree` — and the model is simply never drawn. No script error,
no RPT line, no blank placeholder. Just nothing.

The cause is `priority` -- the preview's own and its ancestors'. Re-measured 2026-11-01
(DayZ 1.30.164200, a stand mod, layouts created on the workspace at 3840x1600, a held item
and an apple, 74 cells; "own" is the preview widget's priority, a chain reads
root > middle frame):

| own | ancestors | model drawn |
|---|---|---|
| 201 | roots 1, 5, 50, 100, 200, 201, 202, 256, 257, 300, 600, 952, 998; 952 > 0, 100 > 0, 300 > 0 | yes, every one |
| 258-299 (nine values) | root 50 | yes |
| 100, 101, 150, 200, 255, 256, 257 | root 100 | yes |
| 100 | 150 > 5 | yes |
| 99, 100 | roots 256, 952 | no |
| 1 | roots 0, 1, 2, 3, 4, 5, 6, 8, 10, 20, 50, 99; 5 > 99 | yes |
| 1, 2, 10, 50, 99 | root 100 | no |
| 1 | roots 256, 300, 952, 998; 100 > 0, 952 > 0; 5 > 100, 5 > 150, 150 > 5 | no |
| 300, 600 | root 100 | no |
| 1, 150 | 99 > 300 | no |

No single threshold fits every cell (own 100 drew under 150 > 5 but not under a 256 root,
and a background panel made no difference), but three things held every time:

- **own 201 draws under anything.** Vanilla's inventory tooltip is exactly that shape: root
  `BackPanelWidget` at 952 > frame 0 > preview `ItemFrameWidget` at 201
  (`inventory_new/day_z_inventory_new_tooltip.layout`, filled by `itemmanager.c:574-578`).
- **own 300+ never draws**, and neither does a preview under a middle frame at 300.
- **own below 100 needs every ancestor below 100**: a root or a middle frame at 100+ hides it.

So the rule this section opened with in 2026-08 -- "the preview or ANY ancestor at 300+
kills it" -- is wrong: a 998 root over a 201 preview draws. The 2026-08 ladder below fits
the own-priority rows if its root was below 100 (the root was not recorded), and the menu
story after it fits the third rule. The `dayz` MCP's `mod_lint` check `layout-preview-priority`
and its layout generator follow this table since 2026-11-01: they refuse an own priority of 300
or more, a middle frame at 300 or more, and an own priority below 100 under an ancestor at 100
or more, and warn below 201 under an ancestor at 256 or more; vanilla's tooltip shape passes.

The 2026-08 ladder, one boot per rung, same item and same position, only the number
changed:

| `priority` | model drawn |
|---|---|
| 2, 10, 100, 150, 200, 255, 256 | yes |
| 300, 600, 900 | no |

The first symptom in a stand mod's device menu was a menu root at `priority 990`: every
preview in the whole menu stayed empty, including one placed outside the device
frame and outside every clipping parent. Dropping the root to `priority 2` made
all of them appear at once.

Vanilla's previews sit at 151 (`gui/layouts/inventory_new/medium/icon.layout`), 200
(`radial_quickbar_item_card.layout`) and 201 (the tooltip, and its `_xbox` twin). The recipe
that held under every root measured: give the preview itself `priority 201`.

### How to tell this apart from the other preview failures

`SetItem` alone is enough for a preview; `SetView(item.GetViewIndex())` is what
vanilla adds (`inventorygrid.c:215`), and `SetModelPosition` only matters for
large inspect-style panels (`inspectmenunew.c:72`). If those are in place and
you still see nothing, do NOT keep tuning them — check `priority` on the widget
and on every parent up to the layout root.

A useful control: build the same `ItemPreviewWidget` in a throwaway layout
created straight on the workspace, the way `PluginItemDiagnostic` does
(`GetGame().GetWorkspace().CreateWidgets(...)`, root at `priority 5`). If the
control draws and yours does not, the difference is in your widget tree, not in
the item.

### Which entity to preview

`SetItem` takes an `EntityAI`, not a classname, so the preview shows the real
attachments of the real item. On a client that means resolving the entity the
SERVER is talking about, not guessing: send `GetNetworkID(low, high)` from the
server and resolve it with `g_Game.GetObjectByNetworkId(low, high)`. Guessing
"whatever is in hands" desynchronises the moment the server's own lookup falls
back to the inventory.

## 14. Textures can be loaded from disk at runtime; screenshots cannot be taken

Two facts measured together on a live client, because they are usually asked
about together ("can a mod show a photo the player took?").

### `LoadImageFile` accepts `$profile:` paths — YES

```c
ImageWidget iw = ImageWidget.Cast(root.FindAnyWidget("Photo"));
bool ok = iw.LoadImageFile(0, "$profile:probe_tex.paa");  // true
iw.SetImage(0);                                           // renders
```

A `.paa` copied into the profile folder by hand loads and draws. The texture
does **not** have to be packed into a PBO, and it does not have to exist when
the mod is built. Slots are 0..7 (`LoadImageFile(int num, ...)`), so one widget
can hold eight images and switch with `SetImage`.

`MakeDirectory("$profile:MyMod")` works too, so a mod can create its own folder
and fill it.

This opens a real door: per-client image content that ships or arrives outside
the pbo. It does **not** open the door to images the client did not already
have — DayZ script has no way to download a file.

### `MakeScreenshot` writes nothing — NO

```c
MakeScreenshot("mymod_probe");                     // nothing
MakeScreenshot("$profile:MyMod/photo.dds");        // nothing
```

Both forms were called with the target directories already created (verified:
`MakeDirectory` had made them, and both existed and stayed **empty**).
`FileExist` on the explicit path returned false, and no `.dds` appeared
anywhere under the profile.

The proto is declared in `1_core/proto/proto.c` and documents a
`$profile:ScreenShotes/DATE TIME-NAME.dds` default, but from mod script in the
game client it is a no-op.

### What this means for an in-game camera

Script has no framebuffer access, and the one engine call that would have
produced an image file does nothing. So **a mod cannot photograph the live
world**. What it can do:

- show images that are already on that client's disk (`LoadImageFile`);
- ship authored images in the pbo — quest photos, evidence, stills;
- render a live 3D view with `RenderTargetWidget` + `SetWidgetWorld(w, world,
  cameraIndex)` — a viewfinder, not a still, and untested here;
- record a "photo" as *data* (position, direction, time, weather, what the
  crosshair hit) and re-present it as a framed card, using
  `ItemPreviewWidget` for the subject if the subject was an item.

## 15. The radio band table is EIGHT frequencies, not seven — and it is measurable

Every mod discussion repeats that DayZ has seven radio frequencies. That number
is nowhere in the scripts: it lives in the engine, and it can be read at
runtime.

Spawn one vanilla transmitter out of the world, walk `SetFrequencyByIndex(i)`,
read `GetTunedFrequencyIndex()` back to see whether the engine accepted the
index, and stop when it refuses or the frequency repeats:

```c
Object obj = GetGame().CreateObjectEx("PersonalRadio", "0 0 0", ECE_NOLIFETIME | ECE_LOCAL);
ItemTransmitter probe = ItemTransmitter.Cast(obj);
for (int i = 0; i < 32; i++)
{
    probe.SetFrequencyByIndex(i);
    if (probe.GetTunedFrequencyIndex() != i)
        break;                       // engine refused: that is the edge
    float hz = probe.GetTunedFrequency();
    ...
}
GetGame().ObjectDelete(obj);
```

Measured on **1.29**, dedicated server:

```
8 frequencies: 87.8  89.5  91.3  91.9  94.6  96.6  99.7  102.5
```

On 1.30 the table has NINE channels, `80.0` first (read from the 1.30 server binary --
persistence-networking.md, "DayZ 1.30 Experimental 1.30.164014").

`SetFrequencyByIndex` applies immediately — the read-back matches on the same
line, no tick needed.

This matters for any "virtual channel" scheme: the number of engine
frequencies is the number of conversations that can be on the air at once
without mixing, so it is what virtual channels have to be mapped onto. Measure
it at boot rather than hardcoding, because it is an engine table and a future
build can change it.

## 16. CF module order is NOT guaranteed — a dependent mod can boot first

`OnMissionStart` arrived in the **dependent** mod before the mod it depends on,
on a stand where the dependency is declared in `requiredAddons` and the pbo is
loaded first. The dependent's registration calls all landed on a null config
and were silently dropped; the only symptom was a `0` in its own ready line.

Do not order-couple modules. If a mod offers a registration API to others,
make that API work **before** its own load:

```c
static bool Declare(MySpec spec)
{
    if (!s_Cfg)                       // not loaded yet: queue, and say yes
    {
        if (!s_Pending) s_Pending = new array<ref MySpec>();
        s_Pending.Insert(spec);
        return true;
    }
    return Insert(spec);
}

static void ServerLoad()
{
    ...load config...
    for (int i = 0; s_Pending && i < s_Pending.Count(); i++)
        Insert(s_Pending[i]);         // admin config already won by now
    s_Pending = null;
}
```

The caller gets `true` because its declaration *was* accepted; where it lands
is the callee's problem, not the caller's.

## 17. Wrapped text is a CLASS, not an attribute — `multiline 1` is silently ignored

`multiline 1` on a `TextWidgetClass` does nothing at all. Not an error, not a
warning: the engine reads the file, builds the widget, draws one line and clips
it at the widget's width. Measured in a stand mod's account-link gate — the sentence
rendered as `...Link your account once ar` and stopped mid-word.

The attribute does not exist. Wrapping is a different widget class:

```
MultilineTextWidgetClass Why {
 size 572 46
 wrap 1                      // <- THIS is the attribute, on THIS class
 "exact text" 1
 "exact text size" 15
 "text halign" left
}
```

Counted in the 1.29 `dta/gui.pbo` (216 layouts) and again in 1.30.164200 (227 layouts):
`multiline` appears zero times as an attribute, `MultilineTextWidgetClass` 339 times, 337
of them with `wrap 1` (the other two are labels in `xbox/day_z_ingamemenu_xbox.layout`).
The vanilla reference is `layouts/day_z_connection_dialogue.layout:76`,
which also sets `"size to text v" 1` when the box should grow to fit.

With a bitmap (FNT3) font this class -- and `RichTextWidgetClass` -- IGNORES
`"exact text size"` and draws the atlas at its own pixel size on every screen;
only an SDF (FNT5) font scales there (measured 2026-10-31, §30 "Wrapped text").

`MultilineTextWidget extends TextWidget` (enwidgets.c:219), so script that
already does `TextWidget.Cast(root.FindAnyWidget("Why")).SetText(...)` keeps
working — swapping the class in the layout needs no script change.

### The root widget is declared like every other one

`Name { type SomeWidgetClass ... }` is not a form the parser accepts, and the
failure is reported as `Cannot load layout '<path>'` — the same line you get for
a file that is missing entirely, which sends you hunting the PBO instead of the
syntax. Every widget, root included, is `WidgetTypeClass Name {`:

```
PanelWidgetClass MyMod_LinkRoot {     // NOT: MyMod_LinkRoot { type PanelWidgetClass
```

Tell the two apart before unpacking anything: grep the built PBO for the layout
path. Present means the file shipped and the parser rejected it; absent means it
was never packed.

## 18. A scripted menu open at the moment of death CRASHES the client

`Exception code: C0000005 ACCESS_VIOLATION`, no script error line, no warning —
the client process is simply gone. Measured in a stand mod: the same death
(`health = 0`) crashed the client every time with the mod's gate menu open, and
was survived every time with the menu suppressed. Same build, same server, one
variable.

The engine tears the mission down on death in an order that does not wait for
scripted menus, so the two chains a menu typically runs in `OnShow`/`OnHide`
dereference freed objects:

```c
GetGame().GetMission().GetHud().Show(true);   // GetMission() OR GetHud() is null
```

Two fixes, and you want both:

1. **Never chain through `GetMission()` or `GetHud()`.** Take each into a local
   and test it. This is cheap and it is the one that stops the crash.
2. **Do not keep a menu open over a dead player.** Guard the reopen loop, and
   close the menu yourself when the player stops being playable. Vanilla guards
   its own pause menu the same way (missiongameplay.c:1314):

```c
PlayerBase p = PlayerBase.Cast(GetGame().GetPlayer());
if (!p) return false;
if (!p.IsAlive()) return false;
if (!p.IsPlayerLoaded()) return false;          // exists but not ready yet
if (GetGame().GetMission().IsPlayerRespawning()) return false;
```

### `ResetGUI()` does NOT undo `AddActiveInputExcludes`

They are unrelated calls, and the pairing the engine actually uses is in
`MissionGameplay.Continue()` (missiongameplay.c:1346):

```c
RemoveActiveInputExcludes({"menu"}, true);      // note the bForceSupress
RemoveActiveInputRestriction(EInputRestrictors.INVENTORY);
```

Skip it and the symptom is not a crash but something worse to diagnose: the
window is gone and the player cannot move, with nothing in any log.

## 19. MapWidget paints over everything -- siblings AND its own children

Measured on a stand (a stand mod's map page, 2026-08-28): a ButtonWidget
sibling with `priority 4` over a MapWidget with `priority 1` renders
NOTHING -- and nesting the button as a CHILD of the map (children normally
render above their parent, section "priority") renders nothing either. The
widgets exist, report visible/shown, receive script clicks; they are just
never painted. The map's native rendering happens after the scripted UI in
its rectangle, and no `priority` value changes that.

Consequence: nothing can be overlaid on a MapWidget. A panel that must
coexist with the map goes BESIDE it, and the map itself is resized to make
room: read the real size once with `GetSize(out w, out h)` at build time and
`SetSize(w * fraction, h)` on toggle -- measure, don't hardcode, because
layout units scale to screen units.

**The covered widget need not be a page-local sibling.** Measured on the same
stand (the same mod's map page, 2026-09-09): the device menu SHELL's own close
button (`BtnClose`, declared in the shell's layout, device rows 14-48) sits
above the per-page content host. The map page hosted inside that shell
started its own content at row 18, so the MapWidget's rectangle (rows 18-448)
covered 30 of the close button's 34 rows -- the X was invisible on that one
page while showing fine on every sibling page, because no other page's
content happens to overlap those rows. Same rule as above, one container
level up: the fix was not a `priority` change (confirmed pointless by the
2026-08-28 measurement) but moving the page's own hint-strip panel from below
the map to above it, so the map's content starts clear of the button instead
of under it.

## 20. UAInput sees raw keys while an edit box has focus

A mod input polled with `UAInput.LocalPress()` fires even when the keyboard
focus is inside an EditBoxWidget or MultilineEditBoxWidget. Measured on the
stand: a close-the-menu key bound to O closed a stand mod's device menu the moment a typed
marker description reached its first "o". The edit box consumed the
character AND the action fired -- both happen.

Guard every polled mod key that must not interrupt typing:

```c
Widget focused = GetFocus();   // proto native, enwidgets.c:702
if (focused && (EditBoxWidget.Cast(focused) || MultilineEditBoxWidget.Cast(focused)))
    return;                     // keyboard is busy with text
```

Measured again 2026-09-29, wider than one mod's menu (a stand mod's push-to-talk on T,
retail client, keys as scancodes through SendInput -- `client_type` reaches polled inputs):

- **The vanilla chat line is such an edit box too** (`ChatInputMenu.OnShow` calls
  `SetFocus(m_edit_box)`). The mod's T input fired on every "t" typed into chat. The engine
  silences only ITS OWN inputs there: vanilla `UAVoiceOverNet`, with the same T added to it
  by script as an alternative binding, stayed off (no VONManager state event) while the mod
  input fired on each letter.
- **A double-press detector latches on ordinary words.** Two presses inside 400 ms
  ("attack", "letter") set the push-to-talk latch, and the transmitter stayed open after
  the chat closed.
- **Bindings are PHYSICAL keys** (`kT` is a scancode). On the Ukrainian and Russian layouts
  the T key types "е", one of the most frequent letters, so a Latin-letter binding collides
  with Cyrillic chat far more often than an English test suggests.
- **A HELD key must not `return` on the guard** -- treat it as released, so a hold that
  focus interrupts lets go instead of freezing in its last state:
  `if (down && typing) down = false;`
- **Focus does not linger once the field is gone.** After the menu left the page whose
  MultilineEditBox had the focus, and again after the menu was closed with the focus
  still in that field, the same T fired at once -- the guard does not need its own
  visibility check (`Widget.IsVisibleHierarchy()` exists if one ever does).
- **Chat closed with Escape**: T fired again on the next press.

## 21. MapWidget never fires OnClick — a menu's click routing goes silent

A single click on a `MapWidget` produces `OnMouseButtonDown` and
`OnMouseButtonUp` through the owning `UIScriptedMenu`, and NOTHING else. The
menu's `OnClick` fires for buttons only. Measured on the stand: an
`if (w == m_Map)` branch inside `OnClick` was dead code — real mouse clicks
never reached it, while the same branch under `OnMouseButtonUp` fired every
time. Vanilla agrees: `5_mission/gui/maphandler.c` attaches a handler to the
map widget itself and listens to `OnDoubleClick`/`OnKeyDown`, never `OnClick`.

Build map clicks from the pair: remember down (x,y), compare on up, and treat
a move above a slop threshold as a drag, not a click — the map pans with the
same button, and every pan ends with an up. Compare `button == MouseState.LEFT`
on the up event (vanilla idiom, e.g. `ctobjectfollower.c:164`).

## 22. MultilineEditBox: `lines N` turns on wrap by INSERTING real \n while typing

Three facts about `MultilineEditBoxWidgetClass`, all measured on the stand:

- With `lines N` in the layout the box wraps typed text — by MUTATING the
  string: a real `\n` is inserted at the wrap point AS YOU TYPE. `GetText()`
  after typing "…the hay bales…" returned `…the hay ba\nles…`. The vanilla
  paper note saves the same artifact. Without `lines` the box does not wrap
  at all (it behaves as a tall single-line field). Text put in with
  `SetText()` is never mutated — wrap-\n comes only from typing.
- `GetLinesCount()`/`GetLine(i)` return the state of the LAST RENDER.
  `SetText()` + immediate `GetLinesCount()` in the same frame gives the old
  value — the box cannot be used as its own synchronous measuring probe.
- A hidden `TextWidget` ruler CAN measure the box's wrap geometry, but only
  with a scale correction: the edit box lays bitmap-font glyphs at NATIVE
  pixel size while `TextWidget` renders them scaled by the unit scale
  `s = screen_h / 1080` (§23a, §30). Measured at 1600x900: the same
  string was 435 px in the ruler and ~523 px in the box (ratio = 900/1080, which on
  16:9 equals the 1600/1920 first written here; on 3840x1600 the width ratio would be
  2.0 against the real 1.48). Usable threshold:
  `inner = (box_GetSize_w - 12) * (screen_h / 1080.0)` —
  the 12 is the box's interior padding in native px, bracketed live as
  8.8–14.8. A reader built on that threshold
  reclassifies each `\n` as engine-wrap (glue) or human Enter (keep) by
  whether line + next char would still fit.
- Re-measured 2026-11-01 (DayZ 1.30.164200, a stand mod's notes box 765 units wide,
  `MetronBook14`, `"exact text" 1`, clients at 1920x1080 and 3840x1600): the box breaks a
  typed line in two places. The first is a **200-character line cap** -- characters, not
  bytes (a Cyrillic line broke at 200 characters, 372 bytes), whatever the width: 200 capital
  W, 2,392 native px and twice the box, still broke at the 200th. The second is the box's
  edge -- but **only with `"limit visible" 1`** in the layout, which vanilla's paper note
  (`EditWidget`, `lines 15`) carries. Without it the box ignores its edge: the text runs on
  hidden past it (characters 106-200 of every line at 1920x1080) and breaks only at 200.
- The scale was measured directly: the same 200-character line was 1421 in the ruler at
  1080 rows and 2105 at 1600 (2105 / 1421 = 1600 / 1080); a width ratio (2.0 at 3840x1600)
  makes the threshold a third too wide and keeps every engine break as an Enter. `GetSize()`
  of the exact-size box returns screen px (1133.33 = 765 x 1600/1080). With `"limit visible"
  1` the edge break fell between `inner` and `inner + one glyph` at both sizes (1661 against
  1674, 753 against 759), so the threshold above stands -- and a reader must also glue every
  line that reached 200 characters, or the cap's breaks survive as Enters.

One more measured cap: the edit box accepts at most **512 bytes** of content —
a 1371-byte clipboard paste survived as exactly 512 bytes in `GetText()`.
Typing past the cap is silently dropped too. No layout attribute was found to
raise it; treat 512 as the practical limit of any player-typed multiline field
(the vanilla paper note lives happily inside it). Re-measured 2026-11-01 (1.30.164200):
typing is NOT capped at 512 bytes -- 279 Cyrillic characters (519 bytes) and 512 ASCII
characters with four line breaks (516 bytes) arrived whole in `GetText()`; the paste was not
re-measured.

## 23. The player equipment grid ignores `attachments[]` (measured 2026-08-28)

Adding a slot to `SurvivorBase { attachments[] += {...} }` makes the slot
REAL (drag-and-drop attaches, FindAttachment works) but the inventory
screen never draws it. The vanilla equipment grid is built from a separate
config list -- `inventory.c` reads the literal path
`CfgVehicles SurvivorBase InventoryEquipment playerSlots` and indexes only
those names (each "Slot_X" string, prefix stripped). A modded player slot
therefore needs BOTH:

```cpp
class SurvivorBase : Man
{
    attachments[] += {"MyMod_Wear"};
    class InventoryEquipment
    {
        playerSlots[] += {"Slot_MyMod_Wear"};
    };
};
```

Also: a slot without `ghostIcon` in its CfgSlots entry renders as an
unrecognizable blank; give it one of the `set:dayz_inventory image:*` icons.

## 23a. Preview harness — measured facts (2026-09-03)

(Numbered "11" until 2026-11-01, which clashed with §11 Performance; older notes that cite
"gui-layouts.md §11" for these facts mean this section.)

Measured on a diag stand with the bridge's `ui_load`/`ui_tree` (engine
rectangles, not estimates), diag client, windows 3840x1600 and 1920x1080.

- **Unit scale is `s = H / 1080` exactly.** A 100-unit pixel-exact panel is 100 px
  at 1080 rows and 148 px at 1600 rows. The "1.48 workspace scale" noted in §2
  above is 1600/1080 — a 3840x1600 window, not a mystery constant.
  On a 2.4:1 screen the virtual width is 2592 units, not 1920: everything
  proportional in width stretches, pixel-sized widgets do not.
- **`-x=W -y=H` set the client window size** (both sizes honoured, windowed).
- **`SetScreenSize(w, h)` on an exact-size FrameWidget takes real PIXELS** (asked
  1306x518, read back 1306x518 px at 1600 rows). (Reconciles the §2 note:
  `SetScreenSize` is not "layout units"; the earlier 1.48 factor was the unit
  scale of the window it was measured in.)
- **CORRECTION (2026-09-03, second gallery round): `SetSize(w, h)` on that SAME
  exact-size frame is ALSO real pixels, not layout units.** Re-measured after
  the note above shipped a fix that trusted it: asked `SetSize("1306 518")`
  again, read back 1306x518 px at 1600 rows, where 1306 layout units at that
  window's own scale are ~1934 px (1306 * 1600/1080). The exact-size flags
  (`hexactsize 1`/`vexactsize 1`) only govern how the FILE's own declared
  `size` is parsed at load; neither script `Set*` call converts a number for
  you afterwards -- there is no script-side call that takes layout units on
  a widget like this. To size one in layout units from script, measure the
  window's own scale yourself first (`GetGame().GetWorkspace().GetScreenSize`
  is valid on any tick, even the one a just-created widget still reports zero
  on -- see "a widget measured before its first layout pass" below) and
  multiply before calling either `Set*`.
- **`-filePatching` reads loose files from `<game dir>/<pbo prefix>/...`** — a
  junction `<DayZ>\MyMod -> <source tree>` works; a loose tree
  inside the built `@Mod` folder (`@Mod/<prefix>/...`) is NOT read at all, even
  after a client restart. The server config needs `allowFilePatching = 1`.
- **`CreateWidgets` re-reads the loose `.layout` on every call**: edit the file,
  reopen the menu (or `ui_load` again) and the change is there — no client
  restart. The §"What -filePatching actually hot-reloads" table in toolchain.md
  says so since 2026-09-03 (it used to say "restart game only").
- **The vertical scrollbar is 10 layout units wide, drawn OVER the content**
  (10 px at 1080, 14 px at 1600); the engine does not narrow the content, and
  the bar is drawn only while the content is taller than the viewport. Text
  inside a `ScrollWidget` must stop 10 units short of the right edge itself.
- **`Widget.ClassName()` returns `"Widget"` for BOTH `PanelWidgetClass` and
  `FrameWidgetClass`** — the engine name cannot tell a panel from a frame; only
  the source layout can. `TextWidget`, `ButtonWidget`, `EditBoxWidget`,
  `ScrollWidget`, `WrapSpacerWidget` report their own names.
- **`GetTextSize`** is non-empty for the `TextWidget` family only (Text,
  Multiline, RichText, MultilineEditBox — `EditBoxWidget` and `ButtonWidget`
  extend `UIWidget`) and scales with `s` (442x52 px at 1600 rows vs 299x36 at
  1080 for the same 24-pt string). `ButtonWidget.GetText` returns the layout's
  `text` value.
- **`WrapSpacer` with `"Size To Content V" 1`** and pixel-high children: two
  30-unit rows give 68 units (engine defaults `Margin 2` = inset on all four sides and
  `Padding 2` = gap after every child: 2+30+2+30+2+2, see §24) — identical with
  `vexactsize 0` and `vexactsize 1` on the spacer.
  Children declared `size 1` (proportional width) overhang the spacer on the
  right by the 2-unit inset (the default `Margin`) — engine behaviour, set `Margin 0`
  (vanilla sets Margin and Padding to 0) if the edge matters. The "105 613 units" runaway measured on a stand mod's
  chat page (recorded in a comment in its layout) did NOT reproduce with pixel-high
  children; its cause lies elsewhere (proportional-height children under
  Size To Content is the first suspect).
- **A widget measured before its first layout pass reports zeros** — the
  bridge's `ui_load` walks on the tick after `CreateWidgets`, and every
  rectangle was right on that tick (1 Hz).

## 24. Spacer idioms -- measured with the preview harness (2026-09-04)

Measured on a diag stand with `ui_gallery` (engine rectangles read back
through the bridge, diag client, window 3840x1600, s = 1.481). Probe layouts:
throwaway files in a scratch folder `probes/`; the numbers below are layout units.

- **Vertical list = `ScrollWidget` (clipchildren 1, "Scrollbar V" 1) ->
  `WrapSpacerWidgetClass` with `size 1 0`, `hexactsize 0`, `vexactsize 1`,
  `Padding 0`, `Margin 0`, `"Size To Content V" 1` -> rows `size 1 <h>`,
  `hexactsize 0`, `vexactsize 1`.** Ten 55-unit rows give a spacer of exactly
  550; zero rows give 0 (no runaway); three give 165. The rows are added by
  script (`CreateWidgets(row, spacer)` + `spacer.Update()`).
- **The same law holds at list length, and there is no "winding" mode.**
  Re-measured 2026-09-04 on a stand mod's PDA-style marker list, rows `size 1 42`
  (on the 345-wide draft of that panel, since narrowed -- the law is
  width-independent, so read the page itself for its current widths): 30 rows
  -> the spacer is exactly 1260 units tall, the width of a row
  (1867 px at s = 1.4815), rows at a regular 42-unit pitch, and the
  scroll clips them (6.8 viewports of content); one row -> 42 units; after
  `Unlink()`ing every row plus one `Update()` -> 0. This retires a claim
  that had stood in that page's own source: "a WrapSpacer winds its own
  size (measured 105 613 units on the chat page) and breaks the scroll",
  used there to justify stacking rows by hand with `SetPos`/`SetSize`.
  Nothing in the engine reproduces it -- a runaway spacer is the
  proportional-flag mistake (`vexactsize 0` with a pixel number -- §1's exact
  flags; first suspected in §23a), not a property of stacking.
- **A proportional-width child of a scroll with `"Scrollbar V" 1` gets the
  scroll width MINUS 10 units** (590 in a 600 scroll) whether or not the bar
  is currently drawn. So proportional rows never run under the bar; only
  pixel-sized content does (the §23a note above was measured on pixel content).
- **A `WrapSpacer` stack is a VBox**: full-width children (`size 1 h`) each
  wrap to their own line. Hidden children (`visible 0`, or `Show(false)`) take
  NO space, with or without `"Ignore invisible" 1` -- toggling a row collapses
  the stack.
- **`Margin m` = inset of the spacer on all four sides; `Padding p` = gap
  AFTER every child (including the last).** Wrap, Padding 8 / Margin 0, two
  30-rows: 30+8+30+8 = 76. Padding 0 / Margin 8: 8+30+30+8 = 76, children
  adjacent. Padding 8 / Margin 4, three rows: 4+30+8+30+8+30+8+4 = 122. The
  engine defaults are Padding 2 / Margin 2 (hence "two rows = 68" in §23a).
  Vanilla sets both to 0 and puts gaps in as widgets; do the same, it is
  exact.
- **`GridSpacerWidgetClass` with a fixed size = equal cells** (`size 184 154`,
  `Columns 3 Rows 4` -> cells of 58.7 x 36.5); every child is STRETCHED to its
  cell whatever it declares (`size 0.5` and `size 100` both become the cell).
  In a grid `Padding p` is the gap BETWEEN cells (no trailing gap: 3*58.7 + 2*4
  = 184), `Margin m` the outer inset.
- **`GridSpacer` with `"Size To Content H" 1` + `"Size To Content V" 1`,
  `Columns N Rows 1` = an HBox**: children keep their pixel widths and sit
  side by side (100+300+80 = 480 wide). A proportional child inside it
  collapses to 0 -- there is no "flexible middle column"; compute that width
  once from the row width instead.
- **`WrapSpacer` does NOT grow horizontally**: `size 0 30` with
  `"Size To Content H" 1` stays 0 wide and every child wraps to its own line.
  A wrap spacer of fixed width flows children left-to-right and wraps (four
  100-wide children in 250 -> two per line, height 60).
- **Text sizes itself**: `"size to text h" 1` / `"size to text v" 1` on
  `TextWidgetClass` with `size 0 0` gives the text's own box (117x23 for a
  16-pt string); `halign right_ref` + `position 12 0` still anchors the
  self-sized box 12 units from the parent's right edge. `MultilineTextWidgetClass`
  and `RichTextWidgetClass` with `wrap 1` + `"size to text v" 1` grow to their
  line count (three 16-pt lines = 55 units, 18.5 per line; 15-pt = 17.3).
- **Text-driven row heights compose**: a row that is itself a `WrapSpacer`
  stack (`size 1 0`, stcV) holding `[who 20][RichText wrap + size to text v]
  [gap 8]` reports 50 units for one line and 80 for three, and the list
  spacer around three such rows reports their sum (180). Setting a longer
  text from script re-wraps and pushes the rows below down. No `SetPos`,
  `SetSize` or `GetContentHeight` anywhere.
- **`GetTextSize` over-reports width by ~9 % against the self-sized box**
  (188 px reported, 174 px box, glyphs all visible), and for a WRAPPED
  multiline it reports the unwrapped width (636 px for a 593-px box). The
  harness rule `text_overflow` must not be trusted on self-sized or wrapped
  text until it reads those flags from the source.
- **Anchors**: `halign right_ref position X` puts the box X units inside the
  parent's right edge; `valign bottom_ref position 0 Y` likewise from the
  bottom; `center_ref` on both axes with `position 0 0` centres. A
  proportional `size 1 1` child with `position 10 10` keeps the full parent
  size and overflows by 10 -- "fill with an inset" needs an inset frame.
- **`fixaspect`**: `fixwidth` keeps the computed HEIGHT and derives the width
  from the ratio of the two declared numbers (`size 0.25 0.5` in 400x200 ->
  50x100; vanilla's `size 1 1` + fixwidth = a square of the height);
  `inside` = the largest box of that ratio inside the PARENT (100x200),
  `outside` = the smallest box covering the parent (1:5 -> 400x2000). Vanilla
  uses fixwidth 488 times, inside 67, outside 29 (1.30.164200; 1.29 had 433 fixwidth).
- **Edit-box styles** (screenshot): `Default` draws nothing of its own --
  vanilla puts it inside a `PanelWidgetClass` that paints the box (chat
  input); `Editor` draws a filled grey box with a light frame; `DefaultBorder`
  and `EditorProperty` draw a 1-unit border only; `ServerBrowser` and no style
  draw nothing. **Button styles**: `Editor` = framed grey box; `MenuDefault` =
  dark box with the text; `Default`, `EmptyHighlight`, no style = text only;
  `DayZDefaultButton` / `DayZDefaultButtonNoBorder` = a solid white box that
  hides the text. A button's own `text` ignores the layout `font` /
  `"exact text size"` (drawn about twice the size of the same text in a
  TextWidget) -- keep the child-Text idiom for captions.
- **Text box heights per SDF point size (measured 2026-09-04, `sdf_MetronBook24`,
  `"exact text" 1`):** 13 pt -> 18.9 units, 15 pt -> 22.3, 16 pt -> 23.0,
  18 pt -> ~26. A label box shorter than that trips `text_overflow`; the
  generator's default label height is `size + 9`, which clears all of them.
- **Text WIDTHS per point size (measured 2026-09-05 on the stand,
  `sdf_MetronBook24`, `text_size` read back through `ui_preview`, units =
  px / s):** a Ukrainian lowercase letter costs ~9.2 units at 15 pt, ~7.9 at
  13 pt and ~6.0 at 10 pt; UPPERCASE ~11.1 at 15 pt; a digit-and-space string
  (`>> 1509 3373  842 m`, 19 chars) costs 148.5 units at 13 pt and 114.1 at
  10 pt; Latin lowercase ~7.5 units at 13 pt. Two consequences worth
  remembering: **the point sizes are not proportional to each other** (13 pt
  is 86 % of 15 pt's width, 10 pt is 76 % of 13 pt's), so extrapolate only
  roughly and measure the final string; and **a byte cap is not a width cap**
  -- 64 bytes are 32 Cyrillic letters (~250 units) but 64 Latin characters
  (~480), so a row that clips by bytes must be sized for the language it is
  written in.
- **A self-sized label (`"size to text h" 1`) with an EMPTY text is 0 wide** --
  the harness reports `zero_size` (WARN) for it in fixture previews that set
  no text; expected, not a defect.
- **`ui_load` fixtures travel to the bridge as ONE JSON string value, and
  `JsonSerializer.ReadFromString` truncates a string above 1023 bytes**
  (persistence-networking.md): a fixture file must stay under ~1000 bytes
  on the wire (Cyrillic = 2 bytes per letter). `ui_preview` now serialises
  fixtures compactly and warns above 1000 bytes; splitting long fixtures in
  the bridge is not done yet.
- **A style-less `ButtonWidgetClass` flashes on hover unless an opaque child
  covers it** (measured in a stand mod's list rows, 2026-08): every clickable row keeps a
  full-size background panel (`RowBg`, `size 1 1`) as its first child for that
  reason, not for the colour alone.
- **`OnClick` fires reliably only on `ButtonWidget`.** A self-growing row
  (`WrapSpacerWidgetClass` root) that must be clickable registers
  `WidgetEventHandler.GetInstance().RegisterOnMouseButtonDown(w, this, "Fn")`
  after `CreateWidgets`; the children keep `ignorepointer 1` so the press
  reaches the row.

- **A `TextListboxWidget` holds no widgets, so no fixture can fill one**
  (measured 2026-09-05). Its chain is `TextListboxWidget` ->
  `SimpleListboxWidget` -> `BaseListboxWidget` -> `UIWidget`: it is neither a
  `TextWidget` nor an `EditBoxWidget` nor a `ButtonWidget`, so the bridge's
  `text` op refuses it, and its rows come from `AddItem`, not from child
  widgets, so `add` cannot reach them either. Every native listbox is
  therefore EMPTY in a preview or a gallery -- the state to judge in a still
  is the empty one, and a full list has to be looked at live.
- **A root positioned by a fraction is a fraction OF THE PREVIEW HOST**
  (measured 2026-09-05 on a stand mod's VPP admin window `mymod_admin.layout`,
  `at_frac 0.24 0.14`, exact size 1000x620). Previewed in a host of its own size the window landed at
  240,87 INSIDE the host and its right quarter -- the close button included --
  was cropped away; previewed in the 1920x1080 workspace the engine really
  hands it, it landed at 461,151 whole. So a fraction-positioned page is
  hosted at the size of the surface it lands in, never at its own size (a
  page positioned with plain `at` still wants its own rectangle).
- **VPP Admin Tools' own keys** (read out of `@VPPAdminTools/addons/
  VPPAdminTools.pbo`'s `<modded_inputs>` preset and confirmed in the stand
  client's `<user>.dayz_preset_User.xml`, 2026-09-05): `UAOpenAdminTools`
  = **Home**, `UAToggleAdminTools` = **End**, `UACopyPositionClipboard` = P,
  `UAToggleFreeCam` = Backspace, `UATeleportToCrosshair` = H. The key alone
  is not enough: VPP opens the menu only for a Steam64 listed in
  `<profiles>/VPPAdminTools/Permissions/SuperAdmins/SuperAdmins.txt` or in a
  group of `UserGroups/UserGroups.json`. On a stand where nobody was added both hold
  nothing but the shipped `Example User` (26561198420222028), so Home reaches
  the game (the cursor state flips) and no menu appears for anybody --
  a mod's VPP submenu cannot be looked at live there until a real Steam64 is
  written into one of those two files and the server restarted.

- **Children come back in `priority` order, not declaration order** (measured
  2026-09-04 on two files): `GetChildren`/`GetSibling` walk siblings by ascending
  `priority`, stable for equal values -- `WipeNote` was source child 44 and engine
  child 22 in a stand mod's VPP pane layout; generated `BtnFPromote` was source child 6 and
  engine child 1 in that mod's faction page layout. So matching a drawn node to its
  source by child index is wrong whenever priorities are non-monotone in source
  order -- generated layouts included, because the generator restarts `priority`
  at 0 inside every flattened `hbox`/`header`. Match by name within the parent.

- **On an exact-size/exact-pos widget EVERY position and size API is in SCREEN
  PIXELS -- `GetSize` included, so `GetScreenSize(w,h)/GetSize(w,h)` is always
  exactly 1 and is NOT a way to get the unit scale** (measured 2026-09-06 on the
  a stand mod's VPP tab strip, logged at debug level and read back
  with `ui_find`). The strip is declared `1000x28` units and the tab template
  `150x28`; at 3840x1600 `GetScreenSize` and `GetSize` BOTH answer `1481.48` for
  the strip and `GetSize` answers `222.222` for the tab (= 150 x 1600/1080), and
  at 1920x1080 both answer `1000` and `150`. `SetPos` likewise takes pixels and
  stores them verbatim: `SetPos(231.111)` -> `GetPos` = `231.111` ->
  `GetScreenPos` = `1152.71` = the parent's `921.6` plus it, with no rounding and
  no hidden factor. The cost of assuming otherwise: a gap constant kept in layout
  units and "scaled" by that always-1 ratio reached the screen unscaled -- 6 px
  instead of 6 x 1.4815 = 8.89, a tab step of 228 px where 231 was intended, and
  the defect is INVISIBLE at 1920x1080 because s = 1 there. **To convert layout
  units to pixels use the engine's own idiom** -- the global
  `GetScreenSize(out int x, out int y)` then `s = y / 1080.0` (vanilla
  `tabberprefab/tabberui.c:48`, `inventorymenu.c:72`,
  `mainmenu/tutorialsmenu.c:15`); write the division through a float, because
  Enforce's `int / int` is integer division and `1600 / 1080` is 1. After the fix
  the step measured 231 px at 3840x1600 (tabs at 922/1153/1384/1615) and 156 px
  at 1920x1080 (461/.../929) -- 156 layout units at both. Aside, measured in the
  same pass: **a script-log line is cut at 255 characters including the
  `SCRIPT       : ` prefix**, silently and mid-token, so a diagnostic that prints
  a whole layout pass must be budgeted for it.

## 25. Fonts and the Ukrainian letters (measured 2026-09-04)

A probe layout with `ІЇЄҐ іїєґ IІ iі` in every vanilla font, shot through
`ui_preview` on the stand (`dta/gui.pbo`; the engine's languagecore knows no
Ukrainian, so the atlases were never built for it):

- every SDF font (`sdf_MetronBook24/72`, `sdf_MetronLight24/30/36/42/72`)
  draws the CAPITAL Cyrillic І (U+0406) as a missing-glyph box; the bitmap
  Metron fonts (`MetronBook22`, `MetronLight22`, `Metron22`, `MetronBlack22`)
  draw it as a blank; of the probed fonts only `AmorSerifPro22` (serif) has it (the
  1.30 glyph tables, read 2026-11-01, also map U+0406 in every size of `amorserifpro`,
  `amorserifpro-bold`, `etelkatextpro` -- inked, below -- and `metronlight-italic` /
  `metronblack-italic`; whether the italics draw it was not checked);
- Ї Є Ґ (U+0407, U+0404, U+0490) and all four lowercase letters і ї є ґ render
  in every font;
- Latin capital I (U+0049) is visually identical to Cyrillic І in Metron, so
  Ukrainian UI text stores U+0049 in place of U+0406 (a stringtable
  convention); nothing else needs substitution -- text a script upper-cases
  takes the Latin I from the upper-casing, not from the data (below).

Font assets are `gui/fonts/<name>.edds` plus `<name>.fnt` (formats in §30);
the one `.fsdf` recipe in the pbo lists ASCII only and is stale. The root of
the І gap: the vanilla `metronbook.ttf` / `metronlight.ttf` in `gui.pbo` map no
U+0406 at all. CORRECTION (2026-10-31): a mod CAN build and ship its own font
with every Ukrainian letter -- §30. The Latin-I substitution stays the rule for
text drawn in the vanilla fonts.

**A name the game upper-cases: the upper-casing writes the Latin `I` for `і`; the
name keeps its Cyrillic `і`** (read from the build-125372 scripts and the 227
`.layout` files of `gui.pbo`, 2026-10-31; the box is a user report, and the
fix is not yet re-shot on a stand). The inventory upper-cases an entity's
`GetDisplayName()` in SCRIPT, never in a layout: no `.layout` attribute changes
case, and the header text is `sdf_MetronLight24`, a face without U+0406. Sites,
under `5_mission/gui/inventorynew/`: `containeditems/headers/header.c:35`,
`.../closableheader.c:56`, `containeditems/cargocontainer.c:293`,
`inherited/containerwithcargoandattachments.c:343`, `attachments.c:467`,
`containeditems/handspreview.c:124`; and the action hints,
`5_mission/gui/itemactionswidget.c:360` and `actiontargetscursor.c:992` (1.30 lines; 1.29:
attachments.c:466, itemactionswidget.c:351, actiontargetscursor.c:1031). What the
engine's own `ToUpper` makes of Cyrillic is UNMEASURED on 1.30; on 1.29 it blanked
every non-ASCII byte of a container header (a storage mod's container, stand, 2026-09-16:
"Мала скриня" read " (0/500)"). Neither that nor "ASCII only" yields a U+0406, so
the box came from a Cyrillic-aware table laid over it: that storage mod's
`MyMod_Case.Upper` sent `і` (U+0456) to `І` (U+0406), and the stand that showed the box
loads it. **The rule: whoever upper-cases Ukrainian text for the vanilla faces writes the
Latin `I` (U+0049) for `і` and `І` both**, one way -- `MyMod_Case.Lower` still sends
`І` to `і`. The upper-casing helpers of two stand mods (a storage one and a research one) do it, both
2026-10-31. A `displayName` (item, station, module, the `name` of an attachment
group) keeps the small Cyrillic `і`; a capital in it stays the Latin `I` by the
stringtable convention above. A copy with the Latin `i` is a different string, so a
search box or a sort key that compares the Cyrillic spelling with it finds nothing
or orders it apart (read from the code; a first fix stored the Latin `i` in the
data and was undone for that). Descriptions are not upper-cased and keep `і` too. A
table that serves MATCHING keeps the exact upper-casing (`MyMod_Case.UpperExact`,
`і` to `І`): a query upper-cased to the Latin `I` and compared with raw names loses
every name written in capitals with U+0406 and lights every name that holds a
Latin `I`.

Sizes and letters of two vanilla faces, read from the 1.30 `gui.pbo` files by the
authors of a stand mod's tree skins (2026-10-31; offline, not measured in the engine):

- **Metron**: the bitmap `Metron*` files have cell = HEAD size and
  em = cell / 1.138 (hhea of the vanilla `MetronBook.ttf`; on the atlases the cap
  is 0.615 of the cell), so a CSS size maps to `"exact text size"` =
  round(px x 1.14). `sdf_MetronBook24` / `sdf_MetronLight24` have em = HEAD size
  (cap 17 of 24): a `MultilineTextWidget` in them sets its lines one em -- the
  text size -- apart, so a CSS line-height above 1 needs line labels. Metron
  lacks U+0406 and U+25C6, and its bitmap files lack U+25A1 too, so a missing letter draws
  blank there; the SDF files CARRY U+25A1 and draw it for a missing letter (the box; glyph
  tables read 2026-11-01); no vanilla
  `.layout` sets a line spacing. A menu that shows config text in Metron swaps
  U+0406 for U+0049 in one place for every such text.
- **etelkatextpro**: `gui/fonts/etelkatextpro{12,14,16,22,28,48,58}`
  are FNT3 with cell = HEAD size, 558 glyphs each, WITH the Cyrillic capital І
  (U+0406, inked), Ї Є Ґ, « » · — № and no U+25A1. It is proportional, not
  monospace (the 12-px file: I 3, M 9, W 10 px). Its `.edds` atlases have NO
  mipmaps (mipcount 1, unlike a mod's own fonts from §30): take the file nearest
  the drawn size -- a big cell shrunk far may alias. Cap height .625, x-height
  .479, baseline .775 of the cell. Its capital І still wants one look on screen.

## 26. OnDoubleClick DOES reach a ButtonWidgetClass row, via the owning menu (measured 2026-09-09)

The double-click event is NOT limited to the widget classes vanilla happens to
use for it (MapWidget, TextListbox, EditBox, Panel, GridSpacer). Measured on the
retail stand with a stand mod's PDA-style menu: a list row whose ROOT is
`ButtonWidgetClass` (children all `ignorepointer 1`) receives a real
double-click, and it arrives at the owning `UIScriptedMenu.OnDoubleClick` as
`w == the row`, exactly the way `OnClick` does — no handler attached to the row
itself, no parent walk needed.

```c
override bool OnDoubleClick(Widget w, int x, int y, int button)
{
    if (button != MouseState.LEFT)                 // vanilla idiom
        return super.OnDoubleClick(w, x, y, button);
    // dispatch to the active page; w is the clicked descendant
}
```

`UIScriptedMenu` declares a plain script `OnDoubleClick`
(`3_game/tools/uiscriptedmenu.c:268`) over the engine slot in
`ScriptedWidgetEventHandler` (`1_core/proto/enwidgets.c:660`), so `override` +
`super` are both legal. Vanilla precedent for a menu-level handler catching a
descendant's double-click: `5_mission/gui/scriptconsole.c:642` (1.29: :332).

Do NOT read §21 as "buttons only get OnClick": that section is about the map
widget swallowing OnClick, and it says nothing about double-clicks. A
`ButtonWidgetClass` root therefore serves both gestures at once — single click
through `OnClick`, double through `OnDoubleClick` — and there is no need to
demote the root to `PanelWidgetClass` to make a double-click land.

One trap that IS real, and cost a rewrite: if the single-click handler rebuilds
the row widgets (Unlink + recreate), the second click of the pair lands on a
widget that no longer exists and the gesture dies. Repaint the selection in
place instead.

## 27. Imagesets: a mod registers its own (measured 2026-09-09)

A mod declares its imagesets in its own `CfgMods` entry -- `class defs { class imageSets
{ files[] = {"MyMod/gui/imagesets/my_icons.imageset"}; } }` -- each entry a pbo-prefix path
to a plain-text `ImageSetClass` file (`Name`, `RefSize`, `Textures { path }`, one
`ImageSetDefClass` per sprite: `Name`, `Pos`, `Size`, `Flags`) beside its `_ca.paa`. No script
registration and no `requiredAddons` are needed; the `.imageset` may even live in a different
pbo than the `CfgMods` entry (VPP declares from `assets.pbo`, ships the file in
`VPPAdminTools.pbo`). Any widget then loads a sprite with `LoadImageFile(0, "set:my_icons
image:sprite")` -- so a companion mod can bring a tab icon for another mod's UI without
touching that mod's atlas.

## Script-created widgets: declared sizes scale, `SetPos`/`SetSize` take pixels (measured 2026-09-20, corrected 2026-10-31)

**CORRECTED 2026-10-31 (§31): `SetPos`/`SetSize` take SCREEN PIXELS**, on script-created widgets
too -- a two-point probe at 3840x1600 moved a widget 100 px for `SetPos(100)`. The 2026-09-20
reading below holds for the widgets' declared size only; do not trust its `SetPos` sentence.

`CreateWidgets(path, parent)` into a plain panel of a scripted menu produces widgets at
the same scale as the menu's own layout: a root `size 200 64` with exact flags measured
296 x 95 px on a 1600-high screen (1.4815 px per unit, H/1080). Placing them is another
matter: `SetPos(x, y)` and `SetSize(w, h)` take screen pixels (§31, §24's last bullet), so
multiply layout units by `H / 1080` before placing grid nodes. (The 2026-09-20 run read
`SetPos(220, 84)` as landing 326 x 124 px from the parent's origin, i.e. as layout units;
§31's two-point probe overturned that.) The canvas for connector lines wants screen pixels
as well, so subtract the canvas's own `GetScreenPos` from the nodes'
`GetScreenPos`/`GetScreenSize`.

## 28. MapWidget: SetMapPos on the tick its pane is shown can miss (measured 2026-09-28)

Measured on a stand (a stand mod's HUD minimap, `SetScale` set once at creation in
an earlier tick). The minimap pane had never been visible; in one tick the script did
`pane.Show(true)` and then `map.SetMapPos(playerPos)`. The view landed about 600 m off:
100 m cells 109-111 x 115-116 on screen while the player stood at 11509 / 3368 (cells
115 x 119), with no self mark in view. It stayed there because the painter only
re-issued `SetMapPos` after the player moved 1 m. A 2 m teleport, i.e. the same call in
a later tick, centred it at once. Once `SetMapPos` was issued every tick (0.5 s), the
same show-then-position sequence was centred by the next screenshot (about 4 s later),
after the player was teleported about 9.6 km while the pane was hidden: NW Airfield in
view, self mark in the middle.

Rule: never make "position the map" depend only on change-detection. Re-issue
`SetMapPos` on every paint tick (it is cheap), and keep only the expensive work -- the
`ClearUserMarks` / `AddUserMark` rebuild -- behind the moved/changed guard. The rule
"set the scale first, then the position" that a stand mod's map page follows (`Paint()` on
the tab's first frame) is probably the same engine behaviour seen from the other side --
an inference, not measured.

## 29. Keys in a scripted menu: widget OnKeyPress vs Mission.OnKeyPress, and Enter "clicks" the hovered button (measured 2026-09-28)

Measured on both stands (diag and retail client), a stand mod's PIN pad, keys delivered as
scancodes through SendInput (the `client_type` / `client_key` path, which is what a keyboard
produces):

- `UIScriptedMenu.OnKeyPress(Widget w, x, y, key)` is a WIDGET event: the engine calls it for
  the widget that holds keyboard focus. With focus on the menu's layout root
  (`SetFocus(layoutRoot)` in `OnShow`) and again after a real mouse click on a pad button,
  digit keys never arrived there -- the pad's keyboard PIN entry was dead.
- The raw path does receive them: `DayZGame.OnKeyPress(int key)` -> `Mission.OnKeyPress(int)`
  fires for every press regardless of focus (vanilla uses it for debug bindings and
  `m_Hud.KeyPress`). A `modded class MissionGameplay` override that forwards `key` to the
  top menu (`GetUIManager().GetMenu()`) made the pad work at once, with `KeyCode.KC_1..KC_0`,
  `KC_BACK` and `KC_RETURN` / `KC_NUMPADENTER`. Keep ONE path: a second copy in the widget
  `OnKeyPress` would double every digit wherever both fire.
- ENTER ALSO CLICKS THE BUTTON UNDER THE MOUSE CURSOR. A menu opens with the cursor at the
  screen centre; on that pad it is `Key5`. One Enter on an empty pad, no digits typed,
  put a "5" into the buffer; together with the keyboard confirm this gave a stray digit on
  the next step ("repeat the new code" started with "5"). `ui_cursor` confirmed `Key5` under
  the cursor. The echo lands after the key handler in the same press. Fix used: stamp
  `GetGame().GetTime()` on every keyboard Enter and swallow every `OnClick` of the menu for
  500 ms after it -- the echo can hit OK (a second submit) or a destructive button just as
  easily as a digit.

## 30. A mod ships its own fonts: `.fnt` + `.edds` (measured 2026-10-31)

Fonts are layout-only: `font "<prefix>/<dir>/<name>"`, no extension. No script call sets a
font (the vanilla scripts have none), so a skin changes fonts with its own layout, or shows
one widget per font and hides the rest (the ZenNotes pattern). Workshop mods ship their own
fonts inside their pbo -- VPPAdminTools (InterDisplay, full Ukrainian Cyrillic),
DayZ-Expansion, SNI_Inventory, ZenNotes (handwriting for paper notes), CodeLock,
Renegade_Trader. A TTF can also be converted into `.fnt` + `.edds` by a script that writes the
formats below.

Probe layout on the stand, `ui_preview`, client 2844x1600:

- **R03** -- a mod-built FNT3 font with an LZ4 `.edds` atlas draws, every Ukrainian letter and
  « » — · № ’ included. **R09** -- the same atlas stored all-`COPY` draws too.
  **R08** -- a font with 1 118 `KERN` pairs drew "AV To Wa" tight; that the pairs are
  applied is INFERRED -- the probe had no unkerned control row of the same font.
- **R11** -- a plain single-level DDS (no `ENF1` marker, no block table) saved as
  `name.edds` ALSO draws: vanilla files carry `ENF1`, the loader does not require it.
- **R10** -- with only `name.paa` beside `name.fnt` the Cyrillic stays blank and the ASCII
  falls back to some default font: the texture is `<fnt path>` with the extension swapped
  to `edds` in code (DayZDiag RVA 0x1fdb15), never `.paa`.
- **R04 vs R03, R16 = R17** -- `"exact text" 1` + `"exact text size" S` scales the whole
  glyph CELL (the line, top of the accents to the bottom of the descenders) to S layout
  units; it is not the em size. In a `TextWidget` -- `MultilineTextWidget` and
  `RichTextWidget` ignore it for FNT3 fonts, see "Wrapped text" below. Without a size the cell draws at its own pixel height. A
  font whose cell (29 px) is taller than its HEAD size (22) drew smaller at size 22 than
  natively; a font with cell = HEAD = 22 drew the same both ways.
- **R06** -- the atlas is white with coverage in alpha and the widget `color` tints it:
  dark ink (41,31,20) on a beige panel.
- **R12-R15** -- a family (`font "dir/Name"`, `dir/Name.xml` = `<fontGenerator>` with
  `<font size="N"/>`, files `dir/NameN.fnt`, `"exact text" 0`) picked the 16-px file for a
  16-high row, the 22-px file for 22, the 22-px file again for 28 (!) and the 28-px file for
  40: the pick rule is not the obvious one, so name the size file explicitly.

Formats (vanilla 1.30 `gui.pbo`, 119 fonts; a writer built to them reproduces all 109 vanilla FNT3 files
byte for byte):

- `.fnt` is IFF: `FORM`, chunk ids and sizes big-endian, payloads little-endian, chunk order
  free. FNT3 (bitmap): `HEAD` {u32 len, name\0, u16 size, 6 zero bytes}; `GLPS` {8 junk
  bytes, u32 glyph count, u32 range count, ranges {u16 first code point, u16 count}};
  `TCRD` 12 bytes per glyph {u16 x, y, w, h, s16 pre, s16 post} -- h is the full cell
  height for every glyph, advance = pre + w + post; `KERN` 8 bytes {u16 left, u16 right,
  s32 dx in px}, sorted by (left, right), may be empty. FNT5 (SDF): ranges {u32, u16, u16
  junk}; TCRD 14 bytes {u16 x, y = corner of the glyph's CELL square, u16 w, h of the ink
  box (centred in that square), s16 bearingX, bearingY = ink top above the baseline,
  advance}; the rest of FNT5 below ("Wrapped text").
- A character the font lacks draws U+25A1 if the font has that glyph, else nothing --
  INFERRED from §25: the SDF Metron fonts carry U+25A1 and drew a box, the bitmap Metron
  fonts lack it and drew a blank; no FNT3 font of the 2026-10-31 probe missed a character.
- `.edds`: a DDS header, A8R8G8B8 for bitmap fonts (RGB white, coverage in A), then a block
  table, SMALLEST mip first, {char[4] codec, u32 size}: `COPY` = raw, or `LZ4 ` = u32 size +
  chunks {u24 compressed size, u8 flags, 0x80 = last} of 64 KiB each (vanilla chunks are
  linked; stand-alone chunks draw too). The engine's block switch also knows `LZO ` and `ZLIB`.
- DayZ Tools has a builder after all: `WorkbenchApp.exe` contains a Font Editor (GUI; a
  `.ttf`/`.otf` plus a `fontGenerator` `.xml` give `.fnt` + `.tga`, imported to `.edds`).
  A script that writes the formats above is the scriptable route.

Sharpness (simulated with the real atlases, GPU-style: bilinear when magnifying, trilinear
mips when shrinking; not an engine measurement): the engine draws a cell of N px at
S x (H / 1080) screen px. A cell of 2 x S lands at exactly half on 1920x1080 -- the sharpest
mip -- and at 0.74 on 3840x1600, magnified on neither; a cell equal to S blurs on 1600 rows,
one of 1.48 x S softens on 1080. One stand mod builds its cells at twice the face's most
common text size. MEASURED since (2026-10-31, Caveat labels at 13-23, each engine line fitted
to an ideal area-averaged rendering of the same line): a 32-px cell is NOT sharper than a
48-px one on 1920x1080 (RMS coverage error 0.107/0.092/0.074/0.052 against
0.108/0.099/0.061/0.049) and only a little closer on 3840x1600 (at 13 and 20). The
"twice the size" rule is not confirmed by the engine; a bigger cell costs nothing visible.

Measuring a shot: the engine blends UI text in LINEAR light. Coverage taken from a shot in
linear light gives an FNT3 line the ink of the ideal rendering within +-5 %; taken from the
sRGB values it reads 0.6-0.85 of it.

### Wrapped text needs an FNT5 (SDF) font (measured 2026-10-31, 1920x1080 and 3840x1600)

`MultilineTextWidget` and `RichTextWidget` IGNORE `"exact text size"` for EVERY FNT3 font,
vanilla included, and the screen scale too: they draw the atlas at its own pixel size.
Measured with `ui_preview`, rows of Text / Multiline / Rich at 18, 24, 32: vanilla
`metronbook22` drew 22 screen px a line, a mod font with a 48-px cell 48, one with 30 px 30
-- at every size, on 1920x1080 and on 3840x1600 (where the TextWidget rows grew x 1.48).
`amorserifpro22` the same. A plain `TextWidget` scales the same fonts (cell = S x H/1080).
No FNT3 field changes this -- the vanilla files behave the same -- so there is no byte fix.
FNT5 fonts scale in all three widgets (vanilla `sdf_MetronBook24` and mod-built FNT5 fonts:
line pitch S x H/1080 in Multiline and Rich). So: wrapped text = an SDF font. A 22-px
vanilla FNT3 font at size 18 only LOOKS right because 22 is close to 18.

FNT5 as the engine reads it (vanilla `sdf_*`, `robotomono_msdf_28`; a mod-built font in this
layout draws in every widget):

- `HEAD` after the u16 size: 6 zero bytes, u8 2, u32 CELL, f32 ascent (baseline below the
  line top), f32 em, u16 spread (8), u8 1, u8 1, f32 em. `sdf_MetronBook24`: size 24 and
  (2, 36, 19.0, 24.0, 8, 1, 1, 24.0); `sdf_metronbook72` CELL 93; `robotomono_msdf_28` 38.
- Every glyph sits in a CELL x CELL square with its ink box CENTRED in it; the squares stand
  in slots CELL + 2 apart from (1, 1); TCRD x, y = the square's corner. The engine samples
  the whole square around the ink: a builder that packed tight variable cells drew
  fragments of the neighbouring glyphs.
- `KERN` is present and EMPTY in all nine vanilla FNT5 files of 1.30 (the seven `sdf_metron*`,
  `sdf_notosanscjk-light28`, `robotomono_msdf_28`); its record layout is unknown,
  so an FNT5 font has no kerning.
- The texture: A8R8G8B8, A = 255, the distance in RGB with the median of the three as the
  value (MSDF), edge at 128, about 21 levels per atlas pixel (+-6 px of range). A
  single-channel field copied into R = G = B draws through the same shader.
- Scale = S / HEAD size. With f32 em = HEAD size (every vanilla SDF font) the lines are S
  apart, so with HEAD size = the line height of the face "exact text size" means what it
  means for an FNT3 font: an SDF Caveat and an FNT3 Caveat of the same line drew equal ink
  widths to the pixel at 13-23.
- One line MEASURES S x CELL / size high -- `GetTextSize`, `"size to text v"` (vanilla
  `sdf_MetronBook24`: 1.5 x S) -- and n lines (n - 1) x S plus that; the glyphs sit 2-3 px
  lower in a TextWidget than the FNT3 font's at the same S (1080).
- `GetTextSize` width = (sum of advances + 1 per glyph) x S / size: one font unit a glyph
  more than is drawn, vanilla SDF fonts too, in every layout but one -- in one probe layout
  two mod-built SDF fonts measured without the extra, reproducibly, at both resolutions;
  the trigger was not found. A live menu measured without it too (2026-10-31, 3840x1600): in
  a stand mod's tree window an opens chip sized by the menu from `GetTextSize` of
  `mymod_sdf_type32` (bold, S 13) came out 97.2 units = 9 + 11 glyphs x .583 S + 5, nothing extra,
  and a layout that took S / 32 a glyph off such widths pulled the next word onto the blank.
  So do not subtract the extra blind: measure it in the window that relies on it.

`WrapSpacerWidget` + `"Size To Content V"` over `MultilineTextWidget` children with
`"size to text v"` + `wrap 1`: the children get their wrapped height and are stacked right
(the second starts where the first ends), but the spacer keeps the SUM OF THEIR ONE-LINE
HEIGHTS (FNT3: the native cell; FNT5: S x CELL / size) and clips the rest -- FNT3 and FNT5
alike, unchanged 4 s after load (`ui_load`, then `ui_tree`). Nothing came out 0 high there.

**The vanilla `7segment` fonts have no colon.** `gui/fonts/7segment22`, `48`, `72` and `96`
carry 17 glyphs each, ` +,-./0123456789` (read from the `.fnt` files, 2026-10-31; a
stand mod's clock window found it first): a digital clock in that face draws its `:` as a picture
or in another font.

## 31. A script-placed tree: units, scroll, canvas, clipping, the pointer (measured 2026-10-31)

Measured on the stand with a stand mod's tree screen (`MyMod_TreeMenu`: nodes placed by script
in a scrolled frame, links on a CanvasWidget the size of the content), retail client, windows
3840x1600 (s = 1.48148 px per layout unit) and 1920x1080; numbers from the menu's own debug-log lines and
`ui_tree`/`ui_find`/`ui_cursor`.

- **`SetPos` and `SetSize` take SCREEN PIXELS** -- on a layout frame (two-point probe: `SetPos(0,0)`,
  a frame later `SetPos(100,0)`: the screen position moved 100 px at 1600 rows; `SetSize(100,100)`
  measured 100 px) and on widgets made by `CreateWidgets(nodeLayout, parent)` (a node cell placed
  with `SetPos(266 x 1.48148)` landed exactly 394.074 px from its parent). This settles the
  contradiction between §24's last bullet (pixels, right) and the 2026-09-20 note (layout units,
  wrong). Multiply layout units by `H / 1080` yourself; measure it once per menu with such a
  probe and log it.
- **Scroll positions are pixels.** `VScrollToPos01(1)` then `GetVScrollPos()` = 462.222, and the
  content had moved exactly 462.222 px on screen. Use the `...Pos01` calls for "home", pixels for
  drag (a real left-button drag of 300 x 200 px moved the content by exactly 300 x 200 px).
- **Both scrollbars take 10 units from the view while they show**: with a 1659 px content in a
  1212 px scroll the vertical maximum was 462 px, not 447 -- the horizontal bar's 14.8 px (10
  units) came off the visible height.
- **A CanvasWidget the size of the whole content draws to its far edge**: `size 1 1` inside the
  scrolled frame, the frame sized by script to 2180 x 1120 units -> a 3229.6 x 1659.3 px canvas;
  lines at its right end were drawn and scrolled into view. No viewport-sized fallback needed.
- **A thick `DrawLine` is a filled rectangle with flat ends**: `DrawLine(x-5, y, x+5, y, 10, c)` is
  a 10 x 10 square; a diagonal segment of length and width `a x sqrt2` is a filled diamond of
  half-diagonal `a` (an outlined diamond = a wide one in the line colour under a narrower one in
  the background colour).
- **A FrameWidget clips its children to its own box** unless the layout says `clipchildren 0` (a
  label at y = -15 in a 1-unit-high frame showed a one-pixel slice: tier numerals looked like
  dots). `ClearFlags(WidgetFlags.CLIPCHILDREN)` lifts it at runtime.
- **A FrameWidget without `ignorepointer 1` TAKES the mouse.** Node cards whose name and price sat
  in frames over the card's `NodeHit` button answered hover and click only on the rim (a user
  report); with the containers made pointer-transparent the real cursor at the card's centre is
  over the button (`ui_cursor`: `ButtonWidget 'node:...'`) and the click selects. The layout
  generator gives `panel` primitives `ignorepointer 1`, `frame` primitives not -- use transparent
  panels as containers. `SetFlags(WidgetFlags.IGNOREPOINTER)` at runtime works too.
- **A style-less ButtonWidget paints a light box under the mouse** when nothing opaque covers
  it; `style Empty` (every state's image empty, `dayzwidgets.styles`) paints nothing.
- **`MultilineTextWidget` and `RichTextWidget` ignore `"exact text size"` with a mod's FNT3 bitmap
  font** (section 30's builder): a 48-px-cell font at size 24 drew 48 units per line (five lines =
  240), a 72-px one 72; a `TextWidget` honours it with the mod's font. CORRECTED by §30 "Wrapped
  text": the vanilla FNT3 fonts do the same -- `metronbook22` at 18 only looked right here because
  22 is close to 18; only SDF (FNT5) fonts scale in these two widgets. In this probe such a widget
  inside a WrapSpacer with `"size to text v" 1` came out 0 high; §30's systematic probe found the
  spacer keeping the children's one-line heights instead -- trust §30. Wrap with an SDF font, or by
  script: `SetText(candidate)`, `Update()`, `GetTextSize()` answers in the same frame (vanilla
  `actiontargetscursor.c`, `tabberui.c` do the same), then put each line in its own label.
- **A WrapSpacer puts the child after a zero-height child BESIDE it**, not under it: a
  full-width label after a 0-high one landed at x = left + width, outside the clip. Hidden
  children still take no room.
- **An ImageWidget showing a texture without `"stretch mode"` tiles it at its own pixel size**
  (a 128x128 wood tile over the whole screen, a 16x16 dash tile along a 1-unit strip) -- so its
  size in units follows the screen height.
- **A `TextWidget` clips its own text at its box, and `"size to text h"` makes the box the sum of
  the glyph advances.** A slanted face's last letter reaches past its advance and loses its top:
  with a Caveat bitmap font (section 30) at `"exact text size" 48` the words `locked`,
  `researched`, `after all` drew as `lockea`, `researchea`, `after alı` -- identically inside a
  GridSpacer (default and `clipchildren 0`), a WrapSpacer and a plain frame with `clipchildren 0`
  (a `ui_load` probe at 3840x1600; the mid-word `l` of `all` was whole). Cyrillic too: the
  Ukrainian `невідомо` in a self-sized label at 23 lost the right side of its last `о`
  (`невідомc`, 3840x1600). Fix: a label box wider
  than the text, with whatever must hug the text sized from `GetTextSize` (it returns the text's
  own width whatever the box), e.g. a tooltip frame of text + padding that clips the wide label.
- **`CreateWidgets` on a missing layout file writes two `SCRIPT (E)` "Cannot load layout" lines
  per call**; test an optional layout with `FileExist(path)` first. `FileExist` sees files packed in
  pbos -- inferred, not probed head-on: vanilla `ParticleList.RegisterParticle` checks every
  `graphics/particles/*.ptc` with it and warns `Particle file not found!` on a miss, the game root
  has no loose `graphics/` folder, and neither the retail client nor the diag one wrote that
  warning in this session.
- **A press on an empty part of a ScrollWidget's content -- or on its bars -- arrives with the
  ScrollWidget itself as `w`** in `OnMouseButtonDown`. The tree menu skipped `w == TreeScroll` to
  leave the bars to the engine and so stopped dragging from every empty place (a 200-px drag moved
  nothing). Tell the bars by position: the bottom and right 10 units of the scroll, each only while
  its bar is drawn (the content wider / taller than the view). After that fix a 150-px drag on an
  empty place moved the content exactly 150 px, and a 100-px drag on the horizontal thumb moved it
  the native 152 px with no content drag on top.
- **A ScrollWidget does not notice its content growing until `scroll.Update()`.** A card stack that
  script had grown to 819 px inside a 690-px ScrollWidget showed no bar and the wheel did nothing;
  after `stack.Update()` then `scroll.Update()` the wheel scrolled exactly the 129 px difference.
  Update the stack, then the scroll, whenever script changes the content's height.
- **Of a ScrollWidget's bars only the colour can be styled** (survey of the vanilla `gui` files,
  2026-10-31; the colour itself is §32): the ScrollWidget styles in `dayzwidgets.styles` (`blank`,
  `dashed`, `Colorable`) define only the 9-slice frame -- no scrollbar parts, unlike the listbox
  styles' `ScrollBarTop/Center/Bottom` -- and the vanilla layouts use no scroll keys but
  `"Scrollbar V"`, `"Scrollbar H"` and `"Scrollbar V Left"`, with `style blank`, `Colorable` or none.
  Script has no `SetStyle` (only `GetStyleName`), so `style blank` must be in the layout.
- **Hide the HUD under a full-screen menu with `ShowHudUI(false)` + `ShowQuickbarUI(false)`;
  `Hud.Show(false)` holds only until anything calls `Hud.Show(true)`** (read from the 1.30 scripts
  and `day_z_hud.layout`; the symptom was seen on the stand, the fix not yet). CORRECTED
  2026-10-31: this bullet first said `Hud.Show(false)` hides only the panel widget and so leaves the
  status icons, stamina and hotbar visible -- wrong. `IngameHud.Show` toggles `m_HudPanelWidget`,
  which is the layout's `HudPanel` (`missiongameplay.c:140`), and `HudPanel` is the parent of
  `BadgesPanel`, `NotifiersPanel`, `QuickbarGrid` and `LeftHUDPanel/PlayerPanel/StaminaBar`
  (`IngameHud.Init` finds them under it, `ingamehud.c:149/166/169`, and hands `HudPanel` to
  `IngameHudVisibility.Init`, `ingamehudvisibility.c:56-64`), so `Hud.Show(false)` DOES hide them.
  The tree menu already called it in `OnShow` and the stand still showed the icons, stamina and
  hotbar under the window, so something called `Hud.Show(true)` afterwards. Such callers exist --
  VPP's `UnlockPlayerControl`, two stand mods' menus (an account-link gate and a PDA-style menu) on their close, and the
  DZMCP bridge's preview (`Load` calls `Unload()` first, which calls `hud.Show(true)`, so
  `ui_load` / `ui_unload` do) -- and which one it was is NOT measured (the preview is the
  likeliest). `ShowHudUI` / `ShowQuickbarUI` set the `MENU_OPEN` /
  `QUICKBAR_DISABLE` context flags, and `IngameHudVisibility` applies a flag by hiding the
  elements themselves (`PlayerPanel`, `BadgesPanel`, `NotifiersPanel`, `QuickbarGrid`...), which
  `HudPanel.Show(true)` does not undo. They are the pair `MapMenu`, `NoteMenu`, `InviteMenu`,
  `InspectMenuNew` and `RadialQuickbarMenu` (`OnShow`/`OnHide`) use, undone with `true` on close,
  and they leave the player's own options alone (the separate `HUD_DISABLE` and `QUICKBAR_GLOBAL`
  flags). Both are declared on the base `Hud` (`gameplay.c`), no cast needed.
- Enforce: a method called `Fog(...)` inside a menu failed with `Method 'WeatherPhenomenon' is
  private` -- `Fog` is a vanilla class and the call constructs it. Name methods away from vanilla
  class names as you would fields (`Fog`, `Shape`, `Head`, `Timer` are all classes).

## 32. Text effects, blending, scrollbars and a layout mix-up (measured 2026-10-31)

Measured on the stand with a stand mod's tree window and `ui_load` probe layouts,
retail client at 3840x1600 (UA) and 1920x1080 (EN).

- **Letter spacing does not exist for a `TextWidget`.** The layout key `"text spacing" X 0` and
  `TextWidget.SetTextSpacing(X, 0)` drew nothing: at X = 0, 7 and 20 the same word drew the same
  pixels and the same width with mod FNT3 faces (two bold Oswald cells), a mod FNT5 face (SDF
  Oswald bold) and vanilla `sdf_MetronBook24`. Spread letters with spaces (an Oswald space is
  about .23 em) -- insert them per whole letter, never by byte, because the text is UTF-8 -- or
  one label per letter.
- **Text shadows draw only on FNT5 (SDF) faces.** `"shadow size"`, `"shadow color"`,
  `"shadow opacity"`, `"shadow offset"` on an FNT3 face (size 4 / opacity .45, 8 / 1.0, 3 with
  offset 2 2) drew exactly the row without a shadow. On the SDF Oswald and on
  `sdf_MetronBook24` the same keys drew a soft halo round the glyphs: faint at 4 / .45, a strong
  glow at 8 / 1.0. A glow or shadow on bitmap text has to be a second label or a picture.
- **A mod-built FNT5 face scales like §30 says.** SDF Oswald (32-px cells) in a `TextWidget` at
  `"exact text size"` 21 and 40: one line measured about 1.37 x S, the glyphs sat about 2 px (21)
  and 4.5 px (40) lower than the FNT3 Oswald's at the same S; in a `MultilineTextWidget` it
  wrapped at the right size.
- **The UI blends in LINEAR light -- panels and pictures alike.** White panels
  (`rover_sim_colorable`) at alpha .04 / .1 / .25 / .5 over black drew 56 / 90 / 137 / 188: the
  sRGB encoding of the alpha itself. A browser mockup blends in sRGB, so its half-transparent
  layers come out different in the game: a black layer of alpha `a` darkens less (use
  `1 - (1 - a)^2.2` for the mockup's result), a light layer over a dark window brightens far
  more (solve `a = (lin(target) - lin(bg)) / (lin(fg) - lin(bg))` in linear values; on a
  #14130b window that came out about a / 6). Opaque colours are unaffected: a layer composited
  opaque at bake time matched the mockup to 1-3 levels (empty tree (31,31,27) against (32,31,24)).
- **A faint, wide light gradient draws as rings on a dark window** -- whichever way it is made.
  A glass glare (a 1200 x 540 ellipse adding at most ~20 levels to a ~15-level background) came
  out as visible concentric bands twice on the stand: with its .07 alpha baked into the picture
  (after the linear-light correction about 4/255, so only four alpha levels), and again with a
  full-range alpha ellipse weakened by the widget's colour alpha (4/255; the PAA read back all 256
  levels). The steps are the frame's own 1-level sRGB steps, some 35 units apart across the
  ellipse, with nothing to hide them -- the game does not dither. That window dropped its glare
  (2026-10-31). Note for `SetAlpha`: it REPLACES the colour's alpha (vanilla `bleedingdrop.c`
  sets `ARGB(0, r, g, b)` and then `SetAlpha`), so flickering a faint widget means
  `SetAlpha(base * k)`, not `SetAlpha(k)`.
- **DXT5 (`_ca.paa`) wrecks fine multi-colour patterns**: it keeps two colours per 4x4 block,
  and a CRT mask of 1-px red/green/blue columns came out as green dots. Keep such tiles
  single-colour with the pattern in alpha, or leave them out.
- **The scrollbar of a `ScrollWidget` is a white bar unless styled.** `color c` + `style blank`
  draws the thumb in `c` and the track in half of it (measured: thumb (68,64,47), track
  (34,32,24)), both bars -- the way the vanilla inventory scrollers do it (`color 0.502 0.502
  0.502 0.8`, `style blank`).
- **`CreateWidgets` once handed one path another file's widgets -- cause unknown.**
  `MyMod/gui/layouts/mymod_or_label.layout` came out as the content of
  `mymod_price_chip.layout` (its root `MyMod_PriceChip`, its children) when the menu created
  it, on two freshly started clients, and in one `ui_load` of that path -- while an earlier
  `ui_load` of the same path in the same session gave the right file. The pbo entry and the
  loose file both held the right text; a byte-identical copy under another name loaded right;
  another skin's pair of files with the same two row names never mixed; CRC32, FNV-1/1a,
  djb2, sdbm, Jenkins one-at-a-time and murmur3 of the two paths do not collide. Defence when
  the name cannot change: give the two files the same widgets and let the script show the right
  ones by where the row sits (whichever file arrives, the row works); when it can, rename. Look
  at the root's name in `ui_tree` -- it says which file the engine used.
- **Bold is real only on FNT5 (SDF) faces** (measured 2026-10-31 at 3840x1600, a stand mod's
  tree window, a `ui_load` probe). `"bold text" 1` / `TextWidget.SetBold(true)` on mod FNT3 faces
  (PT Mono `mymod_type30`, PT Sans Narrow `mymod_narrow28` and its Bold `mymod_narrowb36`, all at
  `"exact text size"` 24) drew exactly the regular row; on the mod FNT5 `mymod_sdf_type32` and on
  vanilla `sdf_MetronBook24` it drew a heavier weight. Bold also widens an SDF line: a PT Mono
  glyph at S 24 advanced 19.7 px regular and 20.7 px bold (1.48 px a unit: .555 S and .583 S).
  So bold text is an SDF face plus bold; a bitmap face needs a bold atlas of its own.
- **Where an SDF line sits in its box.** With `"text valign" center` the baseline of
  `mymod_sdf_type32` lay .367 S under the middle of the TextWidget (S 24 in a 34-high box: 8.8
  units), where an FNT3 face of the same S has it .3 S under the middle (its line is S high, the
  baseline at .8 S) -- the "2-3 px lower" of §30 as a rule: a label of height h in that SDF font
  has its baseline h / 2 + .367 S from its top (the font's HEAD: cell 44, ascent 25, size 32).
  Same shot; its bitmap rows landed within half a pixel of their own prediction.
- **The mix-up again, the same day (a second window).** `mymod_opens_chip.layout` built the widgets of
  `mymod_branch_row.layout` (root `MyMod_BranchRow`) for the menu, which had created branch
  rows earlier in the same paint, and for every later `ui_load` of that path in that session, in
  any letter case; a renamed copy loaded right. In a later session nothing mixed: the reverse
  order (the chip first, then a new file at the branch row's path), another skin's pair with
  the same two row names, and a file renamed with the same NTFS file id and edited in place (it
  loaded its new content -- so the cache is not keyed by the file id). An in-place edit of a
  layout under the same path IS picked up by the next `ui_load`.
- **The mix-up is ordered, not random, and the backslash spelling escapes it** (measured
  2026-10-31 on the retail 1.30 client, a stand mod's seven tree windows opened in turn: skins
  A to F and a neutral one). The wrong file is always one this client created EARLIER in the
  session, and the same order gives the same pair in every session: skins A, B, C in that order
  made C's node cell (`mymod_c_node.layout`) come back as B's branch row
  (`mymod_b_branch_row.layout`) in a Ukrainian client with `-filePatching` AND in an English
  client without it -- so it is no file-patching artifact and a player meets it too. Other
  pairs seen: `mymod_d_opens_chip` ->
  `mymod_e_cost_row`, `mymod_c_tier_label` -> `mymod_a_price_chip`, `mymod_f_tier_label`
  -> `mymod_c_opens_chip`; a window layout handed a non-window widget (the menu found no
  scroll, the window stayed empty). The pairs share no length, size or common hash. Retried in
  the same frame: the same path again, and the path lower-cased, gave the same wrong file; a
  `./` segment and a doubled `/` made the engine log `Cannot open layout` + `Cannot load
  layout` (SCRIPT (E)) and return null; the path with every `/` turned into `\` gave the RIGHT
  file. So the cache key ignores letter case but not the slash. Defence that works everywhere:
  after `CreateWidgets`, compare the root's name with the one the file should have; on a
  mismatch unlink, create again with the backslash spelling, and log it. Give every layout a
  root name that says which file it is, or the check cannot tell two files apart.
- **A PanelWidget clips its children like a FrameWidget** (a stand mod's tree window,
  2026-10-31, 3840x1600 and 1920x1080). The containers were generator `panel`s with no
  `clipchildren` key. A hairline panel lying 13.6..14.6 units down in a 14-high container, and
  one at 13.8..14.8 in another, drew nothing; one at 16.36..17.86 in a 17-high container drew
  only its first pixel. So §31's rule for frames holds for panels too: give the container room
  for every child, or `clipchildren 0`.
- **A 1-unit-thin tiled ImageWidget across the line vanished at 1920x1080** (the same window).
  Horizontal dotted and dashed rules were an `image0` 16x16 `_ca.paa` tile with no stretch mode,
  the pattern in the tile's top two texel rows, the widget 1 unit high. They drew at 3840x1600
  (1.48 px a unit) and drew nothing at 1920x1080 (1 px a unit). Their vertical twin (pattern in
  the left two texel columns, 1 unit wide) drew at both. Which row or column the engine samples
  was not measured. A tile whose pattern fills the whole cross direction (dots as full-height
  bars) does not depend on it -- that was the fix, not yet seen on the stand.
- **An ImageWidget samples its texture with WRAP, also in `"stretch mode"`** (measured 2026-10-31,
  a stand mod's tree window). A stretched torn-edge texture showed a seam along its
  border: the filter blended the opposite edge's texels in. Making the texture's last columns
  clear removed the seam. So a stretched picture's opposite edges must match, or lie where
  nothing shows -- a soft edge, a clear margin of a texel or two. The fix is the layout key
  `"clamp mode" clamp` (772 uses in the 1.30 layouts, e.g. the stretched header image of
  `inventory_new/day_z_inventory_new_tooltip.layout`; values seen: `clamp`, `wrap`, `border`).
  Measured 2026-11-01 (1.30.164200) on a 64-px texture with a red left and a blue right edge,
  stretched to 400 units at 3840x1600: with no `"clamp mode"` line, and with `"clamp mode"
  wrap`, each edge carried the opposite colour over ~5 px (half a texel); with `"clamp mode"
  clamp` both edges stayed clean. The flag `WidgetFlags.NOWRAP` "Do not do texture wrapping"
  (enwidgets.c:63) was not tried.
- **Two widgets that meet edge to edge leave a hairline at 1.48 px a unit** (same day, another window:
  the fog's fade and the opaque fog under it). Each rectangle rounds to whole pixels on its own,
  and one row of what lies beneath showed between them at 3840x1600 (about 10 % at 1080). Lay the
  later widget one unit over the earlier one where both are opaque there.
