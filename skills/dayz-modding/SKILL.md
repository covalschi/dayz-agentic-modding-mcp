---
name: dayz-modding
description: >-
  DayZ mod development with Enforce Script (Enfusion): mod structure, config.cpp,
  scripting (actions, recipes, items, UI), JSON admin configs, persistence, networking/RPC,
  PBO packing, signing, Workbench debugging, server setup and Steam Workshop publishing.
  Use this skill whenever the user mentions DayZ modding in any form — a DayZ mod, addon,
  or framework; Enforce Script / EnforceScript / Enfusion scripts; config.cpp, CfgPatches,
  CfgMods, PBO files, DayZ Tools, Workbench, DayZDiag; DayZ server configuration (types.xml,
  serverDZ.cfg); modded classes; crafting/processing/research systems for DayZ; STALKER RP
  server features; or the `dayz` MCP server's tools (project_open, mod_build, server_start,
  log_verdict, knowledge_*, client_*, ui_*, world_*, workshop_publish). Even a small
  question about DayZ item classes, actions, or server files should load this skill.
---

# DayZ Modding

Build and maintain DayZ mods with verified, real APIs. The skill was first checked against
the extracted 1.29 game scripts (`scripts.pbo` version 124708, July 2026), the official
Bohemia wiki, open-source mods (CF, DayZ Expansion, TerjeMods, BI samples) and — in the
2026-07-31 research pass — a direct extraction of a 54-mod STALKER RP server modpack
plus a 205-entry mod/ecosystem catalog. Facts measured later carry their date. The game is
now 1.30 (1.30.164200, `scripts.pbo` version 127662, stable since 2026-10-21): what 1.30
changed is in the "DayZ 1.30" sections at the end of references/persistence-networking.md,
and a claim without a date was last checked on 1.29. When in doubt, do not invent APIs —
ask the knowledge index or grep the actual game scripts (see "Ground truth" below).

## Setup

A Windows machine with Steam. Below, `<DayZ>` is the game folder
(`steamapps\common\DayZ`; scripts source of truth: `dta\scripts.pbo`) and `<DayZ Tools>`
is `steamapps\common\DayZ Tools` (`Bin\`: AddonBuilder, CfgConvert, DsUtils, PboUtils incl.
**BankRev.exe**, Publisher, Workbench, WorkDrive…). Claude can run the CLI tools; the GUI
apps (Workbench, the game, Publisher) are the user's to drive.
- With the **`dayz` MCP server** (dayz-agentic-modding-mcp) a mod repository is built,
  booted, judged and published through it:
  `project_open` -> `mod_lint` -> `mod_build` -> `server_start` -> `log_verdict`,
  then `workshop_publish` (references/toolchain.md, "Publishing"). Each repository's
  `dayz-mcp.local.toml` holds that machine's absolute paths (game, tools, stand, sibling
  mods); after moving folders rewrite them, or the stand builds from the old ones.
  Without the MCP, use the manual loop in "The dev loop".
- `<DayZ>\!Workshop` holds the Workshop mods the launcher downloaded, one `@Name` folder
  each: the place to read a dependency's PBOs.
- `scripts/ScanPbo.ps1` (PowerShell), `scripts/extract_pbo.py` (Python 3) and BankRev.exe
  are the hand-held PBO readers.

## Ground truth: reading game and mod PBOs

- First ask the `dayz` MCP knowledge index (after `project_open`; `knowledge_status` says
  whether a layer is stale): `knowledge_find` (kind class / method / constant / enum /
  config), `knowledge_show(body=True)`, `knowledge_callers`, `knowledge_overrides`. It reads
  the game's `scripts.pbo` and configs, the dependency mods and the project's own sources in
  place, and every answer names its layer's age. Unpack by hand only when it cannot answer
  (binarized config bodies, obfuscated third-party PBOs, comments and string literals).
- Normal PBOs (incl. `dta\scripts.pbo`): official extractor —
  `& "<DayZ Tools>\Bin\PboUtils\BankRev.exe" -f <outDir> <file.pbo>`
- Binarized `config.bin` → text:
  `& "<DayZ Tools>\Bin\CfgConvert\CfgConvert.exe" -txt -dst out.cpp config.bin`
- Obfuscated PBOs (BankRev refuses or yields junk — Mikero/KGB/"PBO Tools" packers):
  `powershell scripts/ScanPbo.ps1 -Pbo <file.pbo> -OutDir <dir>` — parses the header
  itself, LZSS-decompresses, filters real files by content signature. The header
  file-name table of ANY pbo is plaintext at the start of the file, up to an entry with an
  empty name (about 229 KB for `scripts.pbo`) — read it to map contents without extracting
  multi-GB packs.
- Steam Workshop metadata without scraping (pages 429 fast): keyless
  `POST https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/`
  with `itemcount=1&publishedfileids[0]=<id>`.
- Before writing any nontrivial code, look up (index or grep) the class/method you plan to
  use. Extract game scripts to a scratch dir, not into the repo; BankRev `-f <outDir>`
  writes into `<outDir>\<pbo file name without .pbo>\`, not into `<outDir>` itself.

## Mod anatomy (60-second version)

Released mod = `@MyMod/` folder:

```
@MyMod/
├── addons/            MyMod.pbo (+ MyMod.pbo.MyKey.bisign)
├── keys/              MyKey.bikey (public key, for server owners)
├── mod.cpp            menu presentation: name, picture, logo, author, version
└── meta.cpp           workshop metadata (generated by Publisher)
```

Inside the PBO: your content + `config.cpp`. Canonical minimal script mod (verbatim
pattern from Bohemia's Test_Crafting sample):

```cpp
class CfgPatches
{
    class MyMod
    {
        requiredAddons[] = {"DZ_Data", "DZ_Scripts"};  // load-order deps; add "JM_CF_Scripts" for CF
    };
};

class CfgMods
{
    class MyMod
    {
        dir = "MyMod";
        type = "mod";
        name = "My Mod";
        dependencies[] = {"Game", "World", "Mission"};
        class defs
        {
            class gameScriptModule    { value = ""; files[] = {"MyMod/scripts/3_Game"}; };
            class worldScriptModule   { value = ""; files[] = {"MyMod/scripts/4_World"}; };
            class missionScriptModule { value = ""; files[] = {"MyMod/scripts/5_Mission"}; };
        };
    };
};
```

Script tiers (each is a separate VM; lower tiers can't see higher ones):

| defs key | folder | contains | runs on |
|---|---|---|---|
| engineScriptModule | 1_Core | engine-level, proto wrappers | both |
| gameLibScriptModule | 2_GameLib | gamelib helpers | both |
| gameScriptModule | 3_Game | game/session logic, settings, RPC managers, `DayZGame` | both |
| worldScriptModule | 4_World | entities, items, actions, recipes, `PlayerBase`, `ItemBase` | both |
| missionScriptModule | 5_Mission | `MissionServer`/`MissionGameplay`, UI menus | both (`MissionServer` runs on the server, `MissionGameplay` on the client) |

Most gameplay mods live in 3_Game + 4_World + 5_Mission. New items also need `CfgVehicles`
entries (and models, or reuse vanilla/modpack `model=` paths). A starter skeleton ships in
`assets/mod-template/` — copy it as the base for any new mod.

## Core language rules (details: references/enforce-script.md)

Enforce Script is single-inheritance, typed, no GC (refcounting via `ref`/`autoptr`):

```c
modded class ActionConstructor   // an action nobody registers here is silently dropped
{
    override void RegisterActions(TTypenameArray actions)
    {
        super.RegisterActions(actions);
        actions.Insert(MyCustomAction);
    }
}

modded class PlayerBase          // injected subclass; chains across mods
{
    override void SetActions(out TInputActionMap InputActionMap)
    {
        super.SetActions(InputActionMap);        // ALWAYS call super in modded overrides
        AddAction(MyCustomAction, InputActionMap);
    }
}
```

- `modded class X` = the standard non-destructive way to extend vanilla (and other mods').
  You get access to private members of the original. Forgetting `super.X()` silently breaks
  other mods — treat it as mandatory.
- Member `ref` = strong reference (keeps object alive); a member without it is weak.
  Locals are strong already — vanilla stores `new` in plain locals and uses them
  afterwards (areadamage.c:174-175); a `ref` local is redundant but legal (PlayerBase.AddAction
  runs one on every server, playerbase.c:1828). Containers of objects: `ref array<ref Thing>`.
- Client/server: the same 3_Game/4_World code loads on both sides. Gate with
  `GetGame().IsServer()`, `GetGame().IsClient()`, `GetGame().IsMultiplayer()`,
  `GetGame().IsDedicatedServer()`, or at compile time with `#ifdef SERVER` (defined only on a
  dedicated server; defines.c prefers it to the runtime check). Authoritative logic
  (spawning, loot, points) = server only. In init/constructor paths prefer
  `g_Game.IsDedicatedServer()` — IsClient/IsServer are community-reported unreliable during
  load.

### Language iron rules (always in force — details in references/enforce-script.md)

| Rule | Consequence if ignored |
|---|---|
| NO ternary `?:`, `do..while`, try/catch, lambdas, interfaces | compile error. Overloading by parameter count and type DOES compile (vanilla `PlayerBase.AddAction`, playerbase.c:1817/1838; CF's `CF_ModStorage.Write(bool/int/float/vector/string)`, cf_modstorage.c:156-180) |
| `obj.Field = a && b && c;` — compound boolean assigned straight into an object FIELD | CORRUPTS THE NATIVE HEAP, crashes minutes later somewhere else (measured 2026-09-01). Compute into a `bool` local, then assign |
| `switch` FALLS THROUGH like C | missing `break` = wrong branch runs |
| Redeclaring a variable in sibling if/else blocks | compile error — declare before the chain |
| `s.ToLower()/ToUpper()/Replace()/TrimInPlace()` mutate IN PLACE, return int; `Trim()` is the exception: `proto string Trim()` returns the trimmed copy (enstring.c, measured 2026-09-05) | `string x = s.ToLower();` is a type bug; idiom: `string k = s; k.ToLower();` -- but `string x = s.Trim();` is correct |
| `string` has NO `Compare()` (compile error, measured 2026-09-19) | order strings by hand: `ToAscii()` per character with `Length()`/`Substring()` |
| `Length()` and `Substring()` count BYTES, not characters: `"польові".Length() == 14`, `Substring(0,1)` returns HALF a Cyrillic letter and the mangled string then draws as NOTHING past the cut, `Substring(0,2)` returns `"п"` (measured 2026-09-20 on a live client) | count and cut CHARACTERS with `LengthUtf8()` / `SubstringUtf8(startChar, len)` (enstring.c:212/:137, present in 1.29 too; vanilla clips player names with them, charactercreationmenu.c:518-520); keep the byte forms for byte caps (RPC, JSON) and cut those on UTF-8 boundaries. A widget given invalid UTF-8 silently drops the rest of the line, so the bug looks like a missing field, not a corrupt one |
| `array.Remove(i)` is UNORDERED (swap-with-last) | use `RemoveOrdered(i)` when order matters |
| `%` is int-only: inside a float-typed expression (a `Vector()` argument, float arithmetic) it fails with `Unknown operator '%'` (measured 2026-09-12) | compute `int col = i % 8;` first, then pass the int |
| `int + float` promotes to FLOAT32 (24-bit mantissa): epoch/calendar seconds (~2^28) + a float duration land on a multiple of 16 (measured 2026-09-20: `end - now = 16` for `time = 10.0`) | keep deadlines in `int` end to end: `int t = floatTime;` first, then `int end = now + t` |
| A class field named `local` (`bool local;`), a local named `out` (`string out = s;`) | `local`: `Syntax error` reported at the `class` line, not the field (measured 2026-09-19); `out`: `Broken expression (missing ';'?)` at the declaration (measured 2026-09-20). `type` is fine |
| One long expression (`"a " + x.ToString() + " b " + ...` in one statement) | `Formula too complex` at compile, and the whole module refuses. The ceiling depends on the expression, calls weigh more than plain terms: 14 parts failed (2026-09-26), 18 (2026-08-07), 23 and 27 (2026-10-31); 11-12 compiled, vanilla compiles 13-15. Keep each expression to about 10 parts; build long strings in steps: `string s = ...; s = s + ...;` |
| A class field named `Step`, `Debug` or `Hud` (`float Step;`, `bool Debug;`, `bool Hud;`) | `Variable name 'Step' already used as type name` at compile: vanilla declares `class Step`, `class Debug` and `class Hud`, and a FIELD may not share a type's name (measured 2026-09-20; `Hud` measured 2026-09-28 -- the whole World module refused to compile). Before naming wire/JSON fields, grep `dta\scripts.pbo` for `class <Name>`; `StepMHz`, `DebugLog`, `ToHud`, `Count`, `Range`, `Index` are fine |
| `map.Insert(k,v)` does NOT update existing keys | use `map.Set(k,v)` for insert-or-update |
| `modded class X extends Y` compiles but never joins the modded chain | never write extends on modded |
| `#ifndef NO_GUI` hides code from the dedicated server's compile | guard client-only statements and members, as vanilla does (1.30: 171 guards, none around a whole class; GUI classes such as `IngameHud` compile on the server unguarded). The server never compiles what sits inside — `client_compile_check` does |
| `CallLater(fnRef, ms, ...)` vs `CallLaterByName(obj, "Fn", ms, ...)` | object+string args belong to ByName only (verified tools.c:63) |
| `CreateWidgets(path)` returns null on a bad path (the script log gets `Cannot load layout`, the caller only a null) | always null-check; the path starts at your mod's prefix |
| `widget.SetHandler(h)` keeps ONE handler — last wins silently | give each widget one owner. `WidgetEventHandler.GetInstance().RegisterOnClick` is no way around it: it calls `SetHandler(this)` itself, displacing yours, and keeps one callback per widget and event (`map.Insert`: the first registration wins; widgeteventhandler.c:133-138) |
| Hand-rolled ChangeGameFocus/ShowUICursor must balance open/close | imbalance freezes player input (#1 UI bug) |
| config.cpp strings use `$STR_key`; scripts/layouts `#STR_key`; inputs.xml bare key | wrong prefix = raw key shown |

## The dev loop

With the `dayz` MCP the loop is its tools (Setup above): `mod_lint`, `mod_build`
(FileBank + DSSignFile; the AddonBuilder CLI fails headless, toolchain.md), `server_start`
on the stand, `log_verdict`, then `client_*` / `ui_*` / `world_*` on a live client and
`workshop_publish`. Without the MCP, the manual loop:

1. **Write** sources in the mod's repository (config.cpp + scripts/…).
2. **Pack**: Addon Builder (DayZ Tools GUI) — source `MyMod`, output `@MyMod\addons\`; headless:
   `PboUtils\FileBank.exe -dst <outDir> -property prefix=<Prefix> <srcDir>`.
3. **Test locally**:
   - Server: `DayZDiag_x64.exe -server -config=serverDZ.cfg -port=2302 "-mod=@CF;@MyMod" -profiles=...`
   - Client: `DayZDiag_x64.exe -connect=127.0.0.1 -port=2302 -mod=...`
   - Test servers: `verifySignatures = 0;`, diag needs `BattlEye = 0;`.
   - Fast iteration: pack once, keep an **unpacked** copy with identical prefix layout,
     launch everything with `-filePatching` (+ `allowFilePatching = 1;`) — script edits
     apply without repacking.
4. **Debug**: Workbench (`workbenchApp.exe -mod=MyUnpackedMod`) auto-attaches to a running
   `DayZDiag_x64.exe`; F9 breakpoints; `Print()` → `script*.log` in `-profiles` dir.
5. **Sign & release**: DSUtils → create key once, sign every PBO, ship public `.bikey` in
   `keys/`. Publish via Publisher -- or through the `dayz` MCP:
   `mod_build` packs and signs, `workshop_publish` uploads (`content=False` edits only the
   listing). Details: references/toolchain.md.

## Core systems — where to look

Read the reference file before writing code in that area:

- **references/patterns-cookbook.md** — START HERE for any framework work: verified
  production patterns from 60+ mods — RPC/config sync contracts, JSON config lifecycle
  (versioning/migration/validation/hot-reload), persistence options incl. the
  timers-not-persisted pitfall, the full processing-station blueprint, device patterns
  (portable EnergyManager devices, PDA-style UI singletons, twin static/portable classes),
  placement, mutant-part Skinning canon, points/skills storage, faction gating, UI
  approaches, PBO obfuscation playbook, licensing map.
- **references/mods-catalog.md** — 205-entry catalog (2026-07-31 snapshot) of a 54-mod
  STALKER RP server modpack + top workshop mods + ecosystem tools, with techniques and
  classnames per mod. Grep it by mod name; some mods have several entries.
- **references/actions.md** — interactive-object verbs: ActionInteractBase /
  ActionSingleUseBase / ActionContinuousBase + CB class, condition components,
  OnFinishProgressServer, SetActions/AddAction registration.
- **references/items-and-processing.md** — CfgVehicles items, inventory/cargo/attachments
  hooks, quantity & health, RecipeBase crafting, TurnItemIntoItem, spawning, energy
  manager, central economy (types.xml).
- **references/persistence-networking.md** — netsync variables, RPC (vanilla ScriptRPC vs
  CF), OnStoreSave/OnStoreLoad, CF_ModStorage, per-player JSON, JsonFileLoader,
  settings-sync-on-connect; radio/VoN engine internals read out of the server binary
  (frequency leaf is cached, SetFrequencyByIndex loops all players, the per-frame
  transmitter pass is O(switched-on radios × players) — the byte it checks is SwitchOn, so
  PTT gating does not shrink it; `[VON]` trace and `EnProfiler` are not on retail) — read
  from the 1.29 binary in 2026-09; 1.30 changed the frequency table (nine channels).
- **references/mod-structure.md** — full config.cpp/CfgMods reference (inputs, imageSets,
  widgetStyles, storageVersion), stringtable, load order.
- **references/gui-layouts.md** — native UI mechanics: .layout format, widget catalog,
  events, UIScriptedMenu/ShowDialog, imagesets, CanvasWidget (tree connector lines),
  ItemPreviewWidget, styles/fonts, housekeeping + performance. THE reference for native
  menus and tech-tree UIs. Example layouts: assets/layout-examples/.
- **references/toolchain.md** — P:\ drive, Workbench debugging, Addon Builder (incl.
  headless CLI), BankRev, CfgConvert, signing, Publisher, server files, troubleshooting
  tables, editor tooling (VSCode extensions, LSP), server performance (measured: one
  instance = one core even when empty; the threading knobs that exist; server FPS from
  script; no engine support for moving a character between servers).
- **references/frameworks.md** — what to reuse vs. only study: CF, Dabs, Expansion,
  TerjeMods, Zen, COT, BI samples + license map.
- **dayz-particles** (an Agentic-Z skill, agentic-z.com, installed separately) — particle
  effects: .ptc/.emat formats, vanilla ParticleList catalog, script API — device VFX.
- **StarDZ-Team/DayZ-Modding-Wiki** (GitHub; en/ + ru/, 105 chapters, CC BY-SA/MIT; worth
  a local clone) — long-form GUI/engine-API reading; this skill's measured references win
  on any conflict.
- **scripts/build_imageset.py** — icon-atlas packer:
  `python scripts/build_imageset.py <png folder> --name <set> --prefix MyMod/gui/imagesets`
  writes `<set>_ca.paa` + `<set>.imageset` (Python 3.11+ with Pillow; DayZ Tools' ImageToPAA,
  found through `--tools`, `DAYZ_TOOLS` or the registry; `--no-paa` keeps the PNG atlas).

## Patterns for processing/research gameplay

A research-tree / processing framework maps onto verified systems like this (details in
patterns-cookbook.md; real third-party classnames in mods-catalog.md):

1. **Devices** = ItemBase subclasses; portable = EnergyManager + battery slot (detector/
   PDA pattern); static = an `Inventory_Base` class carrying the structure's p3d (a
   `House`/`Land_*` object created by script is not saved and is gone after a restart,
   items-and-processing.md), or admin-designated existing statics (position-keyed config). Attachment slots as roles: Input /
   Consumables / Tool / Battery / Output, grouped via GUIInventoryAttachmentsProps.
2. **"Process X in device Y"** = ActionContinuousBase on the device (CCTObject), inputs
   checked in ActionCondition against admin JSON, work in OnFinishProgressServer
   (server-authoritative); or CP_Workbench-style background Timer with slot locking —
   in that case PERSIST remaining time (timers die on restart).
3. **Admin-configurable conversions** = JSON in `$profile:<Mod>\` via
   JsonFileLoader<T> (LoadFile overload with errorMessage), ConfigVersion + migration +
   Validate() + backup, per-entity JSON folder for rules, classnames validated with
   ConfigIsExisting, synced to clients on connect via CF RPC (whole-object Param1 works,
   but every string inside stays ≤1023 bytes; maps don't — rebuild entry-by-entry).
4. **Chains** = step N's output classname = step N+1's accepted input. Outputs spawned via
   probe + FindFreeLocationFor + LocationCreateEntity (rotation-aware) or
   TurnItemIntoItemEx lambdas preserving state.
5. **Research points** = server-side per-player/per-faction DB in `$profile:` keyed by
   identity (Hardline model: netsync int for live display, RPC-on-dirty for maps). Do NOT
   extend the PlayerStats enums — other mods do (KK_SkillFramework does: a silent id
   collision). Drop chance rolled server-side in OnFinishProgressServer.
6. **Faction gating** (Scientists/Clear Sky): ask an affiliation service that your core
   declares with an empty default implementation; an optional faction mod fills it, and the
   caller never checks whether that mod is loaded (the answer is empty, never null).
   Expansion AI (`player.eAI_GetFactionTypeID()`, faction classnames in config strings) is
   one optional provider behind `#ifdef EXPANSIONMODAI` — many servers do not run it.
   Fallbacks: worn-gear checks (SGE suits), territory flags.
7. **Mutant parts as inputs** = creatures drop parts via config `class Skinning` blocks on
   knife-skinning (WT mutants already do); for creatures without parts, modded
   ActionSkinning.OnFinishProgressServer spawns JSON-configured extras.
8. **Tech-tree UI** = native per-node .layout instantiation (Terje approach; it needs no
   Dabs or Expansion dependency). Third-party hosts:
   an Expansion Book tab (proven tab API) or a Dabs MVC ScriptViewMenu.

## Pitfalls (learned the hard way — respect these)

- Never trust the client: rewards, spawns, and point mutations belong in server-side
  callbacks only. Sensitive values (codes) return dummies on client (CodeLock pattern).
- `modded` override without `super.…()` = breaks vanilla or other mods silently.
- Renaming your `CfgMods` class after release makes CF ModStorage data UNREACHABLE — the
  class NAME is part of your save format. Read out of CF's own source 2026-09-02
  (`modstructure.c:53`, `modloader.c:40-70`, `cf_modstorageobject.c:62-75`): every block is
  tagged with `name.Hash()` and `name.Reverse().Hash()`; on load an unmatched pair becomes
  an orphan whose `GetModName()` returns `unknown<a,b>`. The bytes are NOT destroyed — CF
  rewrites orphan blocks on every save (`m_UnloadedMods`), so pulling a mod off a server
  does not wipe player data, and renaming back recovers it. But the renamed mod registers
  under new hashes and reads defaults. Also `storageVersion > 0` is REQUIRED or ModStorage
  silently does nothing.
- Timer/CallLater-driven processing is NOT persisted — server restart cancels it silently
  and can leave slots locked. Persist the REMAINING time (or a wall-clock UTC stamp), never
  a `GetGame().GetTime()` stamp: that clock restarts with the server
  (persistence-networking.md). Community report: engine timers degrade after ~4h uptime.
- RPC ids collide silently: use CF's string-keyed RPCManager; when extending vanilla enums
  (ERPCs, eModifiers, EPlayerStats_current, eAgents) pick high/sentinel values — and never
  extend EPlayerStats_current at all: KK_SkillFramework does, and any server may run it.
- `array`/`map` members holding objects need `ref`; `map<string, ref T>` does not survive
  RPC serialization — rebuild client-side.
- Strings stop at about 1023 BYTES in transport: a longer RPC string arrives as `String
  CORRUPTED`, `JsonSerializer.LoadData` cuts every string value at 1023 B, and a 1024-byte
  string in an entity's store throws on load and loses that item's whole mod blob. Chunk at
  ≤900-1000 B on UTF-8 boundaries (persistence-networking.md).
- Pure-script PBOs need correct **prefix** ($PBOPREFIX$ / folder name = paths in files[]);
  mismatch = scripts silently not loading.
- New items don't spawn until added to mission economy (types.xml via cfgeconomycore.xml).
  Ship example types.xml with nominal=0 in the mod root so admins opt in.
- A mod's own class placed in the world is swept by the central economy unless it carries a
  lifetime (`ECE_PLACE_ON_SURFACE` lacks `ECE_NOLIFETIME`): `SetLifetime(seconds)` at
  creation and again in `EEOnAfterLoad`, not `EEInit` — the engine restores the saved
  lifetime after `EEInit` (measured 2026-09-17/25, items-and-processing.md).
- Old `JsonLoadFile` keeps defaults on malformed JSON and tells only the script log
  (`Cannot load data from…`), never the caller — use `LoadFile(path, data, errorMessage)`
  and report the broken file to the admin; never crash on bad config.
- Vanilla bug T148506 (won't-fix): if a vanilla item declares `inventorySlot = "X";` as a
  STRING, a mod's `inventorySlot[] += {...}` is silently dropped — redeclare the full
  array including the original value.
- A SCRIPT MOVE into a container CREATED IN THE SAME FRAME makes the moved entity
  undeletable once it later leaves that cargo for the ground: `ObjectDelete` stops halfway
  (netid 0, location UNKNOWN, still in the spatial index, `IsPendingDeletion` forever), players
  see an unpickable ghost, a restart persists it as a real item (dupe). Creation into the
  container (`CreateEntityInCargo`) and players' juncture moves are fine; one frame between the
  creation and the move cures the server. Clients get the same ghost from ANY SYNC_MOVE of a
  container with children into a cargo: build nested trees out of LOCAL entities
  (`ECE_LOCAL`, `LocationCreateLocalEntity`), move them in `InventoryMode.LOCAL` a frame later,
  publish once with `RemoteObjectTreeCreate`, then `SetSynchDirty()` every entity of the tree
  (measured 2026-09-18, persistence-networking.md).
- Statics survive mission restarts (process may not restart) — null singletons in
  OnMissionFinish, pair every ScriptInvoker Insert with Remove, Remove pending CallLater
  in destructors. See patterns-cookbook.md §14.
- A misspelled requiredAddons entry silently skips your whole PBO — evidence only in the
  .RPT, not the script log. Fix the FIRST `SCRIPT (E)` error; the rest cascade.
- Test with `-filePatching` but ship packed+signed; `verifySignatures=2` rejects unsigned.
- Licensing: most mods forbid repacking (some obfuscate + phone home). Depend on CF
  (LGPL); study-only for CC BY-NC-ND (Expansion/Dabs/KK); adapt-with-attribution for
  Terje (CC BY-NC) and Zen (free). Check every source against your own mod's license: a
  CC BY-NC-SA mod cannot take CC BY-SA code (COT), the two share-alike terms conflict.
  Full map: patterns-cookbook.md §12.

## When asked to write actual mod code

1. Read the relevant reference file(s) above first (patterns-cookbook.md for framework
   work; mods-catalog.md for a third-party mod's classes and techniques).
2. Check `assets/mod-template/` for the skeleton; keep the module-tier layout.
3. Verify each vanilla class/method in the knowledge index or the extracted game scripts
   (BankRev on `dta\scripts.pbo` when the index cannot answer).
4. Produce: sources + config.cpp + (if new items) types.xml snippet + packing/testing
   instructions.
5. Enforce Script has no offline compiler — the game compiles it. Keep changes small and
   always end with a concrete test plan (launch line + what to check + which log shows
   success). With the `dayz` MCP, `mod_lint` (static checks before packing) and
   `client_compile_check` (the real compiler, on a client) catch errors before a stand
   boot; without it, VSCode extensions (EnScriptSyntax / DayZ Enforce Script) and
   enforce-script-lsp help — see toolchain.md.
