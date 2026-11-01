# Persistence, networking & admin JSON configs

Server-authoritative patterns: sync state to clients, save state across restarts, and let
admins configure everything via JSON in the server profile. Verified against build 124708
(1.29 era), CF 1.5.5 and DayZ Expansion sources; script line citations re-checked
2026-11-01 against 1.30.164200 (`dta/scripts.pbo` version 127662) and CF 1.5.9.

## Client/server model in 30 seconds

Both client and server run the same 3_Game/4_World scripts. The server owns truth:
economy, spawning, item state, player stats. Clients render and predict. Every feature
needs an explicit answer to "how does the client learn about this?" — netsync variable,
RPC, or it doesn't need to know.

Null-context gotchas (the classic listen-server-works / dedicated-crashes class):

- `GetGame().GetPlayer()` returns NULL on a dedicated server (no local player) — iterate
  `GetGame().GetPlayers(players)` there instead. It also returns `Man`; cast to
  `PlayerBase` before using player APIs.
- `player.GetIdentity()` returns NULL in offline/SP — guard before `GetId()`.
- Three-context truth table: dedicated server IsServer=T IsClient=F; MP client IsServer=F
  IsClient=T; listen server / SP host BOTH true — which hides races and null bugs. Always
  test on a dedicated server before shipping.

## 1. Netsync variables (per-entity state → all nearby clients)

For device state everyone can see (busy/idle, progress stage, lit up):

```c
class MyMod_Microscope : ItemBase
{
    int m_ProcessingState;           // 0 idle, 1 running, 2 done

    void MyMod_Microscope()
    {
        RegisterNetSyncVariableInt("m_ProcessingState", 0, 3);   // entityai.c:2850; bounded ints pack smaller
        // also: RegisterNetSyncVariableBool / Float(min,max,precision) / BoolSignal (:2827+)
    }

    void SetProcessingState(int state)   // SERVER
    {
        m_ProcessingState = state;
        SetSynchDirty();                 // entityai.c:3064 — pushes to clients
    }

    override void OnVariablesSynchronized()   // CLIENT — called after values arrive
    {
        super.OnVariablesSynchronized();
        // update visuals/sounds based on m_ProcessingState
    }
}
```

Limits & packing:

- `RegisterNetSyncVariableFloat(name, min, max, precision)` — last param = decimal places
  kept on the wire (bandwidth).
- `SetSynchDirty()` is entity-level: one call flushes ALL changed netsync vars of that
  entity. Sync is eventual — the client does NOT have the new value right after the server
  sets it; react in `OnVariablesSynchronized()`, never poll immediately after.
- Community-reported (unverified): cap of ~32 synced variables per entity, shared across
  ALL mods modding that entity (PlayerBase on a stacked modpack is crowded), and late
  `RegisterNetSyncVariable*` calls (after construction/init) silently fail. Vanilla
  registers in constructors, as above — do the same.
- Budget trick: pack many bools into ONE `RegisterNetSyncVariableInt("m_Flags")` bitfield —
  server `m_Flags |= FLAG_X; m_Flags &= ~FLAG_Y; SetSynchDirty();`, decode bits in
  `OnVariablesSynchronized()`.

## 2. RPC (events & payloads between server and clients)

With CF as a dependency, use CF's string-keyed RPCManager and avoid int-id collisions
between mods (raw `ScriptRPC`, gameplay.c:104, is fine for mod-internal quick jobs but you
must pick globally-unique int ids; CF exists precisely to avoid that).

```c
// Registration (both sides — e.g. in a modded MissionBase / module init):
GetRPCManager().AddRPC("MyMod", "RPC_RequestState", this, SingleplayerExecutionType.Server);

// Handler signature (fixed):
void RPC_RequestState(CallType type, ParamsReadContext ctx, PlayerIdentity sender, Object target)
{
    Param1<int> data;
    if (!ctx.Read(data)) return;
    if (type == CallType.Server)
    {
        // respond only to the asking client:
        GetRPCManager().SendRPC("MyMod", "RPC_ReceiveState", new Param1<int>(m_Points), true, sender);
    }
}

// Client → server:  SendRPC(..., sendToIdentity = NULL) from client goes to server.
// Server → one client: pass that client's PlayerIdentity.  Server → all: identity NULL.
```

Payloads are `Param1..ParamN<...>` tuples; complex classes: write fields individually or
serialize to JSON string. Never trust RPC input on the server — validate sender & values.

- `guaranteed` param (the `true` in the examples): reliable delivery — use for config and
  state where a dropped packet would desync; `false` = unreliable, cheaper, only for
  frequent cosmetic updates where the next one supersedes the last.
- Payload budget (community-reported, unverified): practical limit ~64 KB per RPC; rough
  serialized sizes int/float 4 B, bool 1 B, vector 12 B, string length-prefixed UTF-8.
  MEASURED, and far lower: every single STRING in a payload must stay at or under 1023
  bytes -- a longer one makes the receiver's `ctx.Read()` throw `String CORRUPTED` while the
  sender reports nothing ("RPC strings corrupt above ~1024 bytes" below; the same cap cuts
  JSON string values and entity-storage strings). Chunk large transfers -- one RPC per
  device ruleset (or N rules per RPC), not the whole config in a single Param, and long
  strings in parts of <= 900 bytes.
- Throttle client-triggered RPCs: dirty flag + minimum send interval (~2/s), never
  per-frame.

## 3. Item persistence across server restarts (storage)

Items in the world save automatically; *your custom fields* need explicit store hooks.

Vanilla way (careful — version handling is on you; itembase.c:3194 OnStoreSave / :3093
OnStoreLoad):

```c
const int MYMOD_SAVE_VERSION = 2;   // bump when adding fields

override void OnStoreSave(ParamsWriteContext ctx)
{
    super.OnStoreSave(ctx);
    ctx.Write(MYMOD_SAVE_VERSION);   // YOUR version header — always the first field
    ctx.Write(m_ProcessingState);
    ctx.Write(m_RemainingTime);      // v2 field — appended at the END, never between
}
override bool OnStoreLoad(ParamsReadContext ctx, int version)
{
    if (!super.OnStoreLoad(ctx, version)) return false;
    int myVersion;
    if (!ctx.Read(myVersion)) return false;
    if (!ctx.Read(m_ProcessingState)) return false;
    if (myVersion >= 2 && !ctx.Read(m_RemainingTime)) return false;
    return true;
}
```

Stream rules (vanilla path):

- The engine `version` param is the ENGINE storage version, not the game version:
  `GAME_STORAGE_VERSION` (`3_game/global/game.c:5`) is 142 on 1.29 and 144 on 1.30 (CF notes
  141 for 1.27). It tells you which vanilla fields `super` read, never which of YOUR fields
  exist. Gate your fields on your OWN version header (written first, read first), as above.
- New fields go at the END of your stream only — inserting between existing fields breaks
  every old save.
- Read-order mismatch or returning false = load failed; the entity is dropped on next load
  (community-reported — the consequence is engine-side, not visible in script sources).
- Test BOTH paths before release: fresh save and upgrade-from-old-save.

CF way (safer with many mods; requires `storageVersion` in your CfgMods entry):

```cpp
class CfgMods { class MyMod { type = "mod"; storageVersion = 1; ... }; };
```
```c
override void CF_OnStoreSave(CF_ModStorageMap storage)
{
    super.CF_OnStoreSave(storage);
    auto ctx = storage["MyMod"];
    if (!ctx) return;
    ctx.Write(m_ProcessingState);
}
override bool CF_OnStoreLoad(CF_ModStorageMap storage)
{
    if (!super.CF_OnStoreLoad(storage)) return false;
    auto ctx = storage["MyMod"];
    if (!ctx) return true;
    if (!ctx.Read(m_ProcessingState)) return false;
    return true;
}
```

Old-save guard: a save written before your mod existed has no "MyMod" entry — the null
check above covers it; `storage.Contains("MyMod")` is the explicit form
(cf_modstoragemap.c:15).

Warning (CF docs): renaming your CfgMods class after release orphans stored data.
Scalars only: CF 1.5.9's `CF_ModStorage` has `Write`/`Read` for bool, int, float, vector
and string and nothing else (`cf_modstorage.c:61-180`; arrays are a TODO in
`cf_modstoragedata.c`) -- write an array as a count, then the elements. Store ids, not
object refs.

## 4. Per-player server data (points, progress)

Pattern (mirrors Expansion Hardline/Quests): JSON file per player UID in `$profile:`,
loaded on connect, mutated server-side, saved on change/disconnect, pushed to the owning
client via RPC for UI.

```c
// 3_Game — the data class (public fields = JSON fields):
class MyModPlayerData
{
    int Version = 1;
    int ResearchPoints;
    ref map<string, int> BranchProgress = new map<string, int>();   // a map in JSON: untested here, see "JSON notes"
}

// 4_World/5_Mission — manager singleton on server:
class MyModPlayerDB
{
    static const string FOLDER = "$profile:MyMod\\Players\\";
    ref map<string, ref MyModPlayerData> m_Cache = new map<string, ref MyModPlayerData>();

    MyModPlayerData Load(string uid)
    {
        MyModPlayerData data = m_Cache.Get(uid);
        if (data) return data;
        data = new MyModPlayerData();
        string path = FOLDER + uid + ".json";
        string err;
        if (FileExist(path))
            JsonFileLoader<MyModPlayerData>.LoadFile(path, data, err);   // 3_Game/tools/JsonFileLoader.c
        m_Cache.Insert(uid, data);
        return data;
    }

    void Save(string uid)
    {
        if (!FileExist("$profile:MyMod")) MakeDirectory("$profile:MyMod");
        if (!FileExist(FOLDER)) MakeDirectory(FOLDER);
        string err;
        JsonFileLoader<MyModPlayerData>.SaveFile(FOLDER + uid + ".json", m_Cache.Get(uid), err);
        if (err != "") Error("[MyMod] save failed: " + err);
    }
}
```

Wire into the mission lifecycle (verified hooks, missionServer.c):

```c
modded class MissionServer
{
    override void InvokeOnConnect(PlayerBase player, PlayerIdentity identity)   // :435
    {
        super.InvokeOnConnect(player, identity);
        MyModPlayerData d = MyMod.GetDB().Load(identity.GetId());   // GetId() = hashed steam id (gameplay.c:368)
        // push to client for UI:
        GetRPCManager().SendRPC("MyMod", "RPC_ReceivePoints", new Param1<int>(d.ResearchPoints), true, identity);
    }

    override void PlayerDisconnected(PlayerBase player, PlayerIdentity identity, string uid)  // :690
    {
        MyMod.GetDB().Save(uid);
        super.PlayerDisconnected(player, identity, uid);
    }
}
```

Faction-level (a pool shared by one faction's members): same pattern, one JSON per faction
id instead of per player; guard concurrent writes by mutating only in one manager.

## 5. Admin-editable settings ($profile JSON, synced to clients)

The Expansion-proven flow — server loads JSON (creating defaults on first run), clients
receive a copy on connect:

```c
class MyModSettings
{
    int Version = 1;
    ref array<ref MyModDeviceRule> Devices = new array<ref MyModDeviceRule>();

    [NonSerialized()]
    static ref MyModSettings s_Instance;

    static MyModSettings Get()   // valid on server after Load; on client after RPC
    {
        if (!s_Instance) { s_Instance = new MyModSettings(); s_Instance.LoadServer(); }
        return s_Instance;
    }

    void LoadServer()
    {
        if (!GetGame().IsDedicatedServer() && GetGame().IsMultiplayer()) return; // client waits for RPC
        string dir = "$profile:MyMod";
        string path = dir + "\\Settings.json";
        string err;
        if (!FileExist(dir)) MakeDirectory(dir);
        if (FileExist(path))
        {
            if (!JsonFileLoader<MyModSettings>.LoadFile(path, this, err))
                Error("[MyMod] Settings.json invalid: " + err);   // fail LOUD, keep defaults
        }
        else
        {
            WriteDefaults();
            JsonFileLoader<MyModSettings>.SaveFile(path, this, err);  // self-documenting default file
        }
        Validate();   // check classnames exist: ConfigIsExisting under CfgVehicles, CfgWeapons AND CfgMagazines
                      // (weapons and magazines are not CfgVehicles, see "Engine facts of the SQL-truth storage")
    }
}
```

Sync on connect (in `InvokeOnConnect`): serialize what clients need (usually a trimmed
subset — prompts/UI names, NOT drop chances → keeps cheating value low) and send via RPC.
Client stores it into `s_Instance`. Version field + migration keeps old JSONs working —
follow Expansion's `m_Version` bump pattern.

JSON notes: `JsonFileLoader<T>` (de)serializes public non-static fields, including nested
`ref` classes and arrays. Maps with string keys are untested here: vanilla
`1_core/proto/serializer.c` documents map support, but every production config examined for this skill
uses an array of `{key, value}` records instead (see "Persistent id is read-only"). Mark
runtime-only fields `[NonSerialized()]`.
`$profile:` = the server's `-profiles=` directory — the standard place admins expect to edit.

### File I/O sharp edges

- `MakeDirectory(name)` is NOT recursive (1_core\proto\ensystem.c:525) — it creates only
  the final segment; `MakeDirectory("$profile:MyMod\\A\\B")` fails silently if A is
  missing. Create each level (the examples above do).
- `FindFile(pattern, fileName, fileAttributes, FindFileFlags.ALL)` +
  `FindNextFile(handle, fileName, fileAttributes)` + `CloseFindFile(handle)`
  (ensystem.c:520-522) return FILENAMES only, no path — prepend the directory yourself.
  `FileAttr` is BIT FLAGS: DIRECTORY=1, HIDDEN=2, READONLY=4, INVALID=-1 (printed from
  script on 1.29 and 1.30, 2026-10-11), and FindFile does set `attr = 1` for a directory
  entry (measured 1.29), so `attr & FileAttr.DIRECTORY` tells a directory. The earlier
  note here ("plain enum 0..3, the test is always 0") was an inference from the header,
  not a measurement, and was wrong. On 1.29
  `FindFile("*", ..., FindFileFlags.ARCHIVES)` at the VFS root lists every loaded
  archive's prefix and root file (2244 entries on a 6-mod stand), and `"<prefix>/*"` one
  level deeper (measured 2026-09-20 by a string-table reader mod). 1.30.164014-164078 broke this and
  every prefixed listing ("DayZ 1.30 Experimental 1.30.164014: FindFile and FindFileFlags"
  below). 1.30.164200 lists `$profile:` directories again, but its ARCHIVES root listing
  gave 100 entries where 1.29 gave 2,200+, and `"<prefix>/*"` was not re-measured ("DayZ
  1.30 Update 2, 1.30.164200" below).
- Files PACKED IN PBOs are readable from script: `OpenFile("<prefix>/stringtable.csv",
  FileMode.READ)` opens for every mod archive AND for `dta/languagecore.pbo`
  (`languagecore/stringtable.csv`); the prefix is the PBO's `$PBOPREFIX$`, the one the
  `files[]` paths of CfgMods start with (measured 2026-09-20 on the dedicated server).
- `FGets` copes with long lines: 8198-char lines came back whole, nothing truncated, the
  loop ends on -1; an EMPTY line returns 0 (not -1), so loop on `>= 0` with a guard
  rather than the vanilla `> 0` idiom that stops at the first blank line.
- Text I/O: `OpenFile(path, FileMode.READ|WRITE|APPEND)` returns a FileHandle, 0 = fail;
  `FGets(fh, line)` returns -1 at end of file (ensystem.c:487); `FPrintln`/`FPrint` write;
  `CloseFile(fh)` in every code path.
- `CopyFile(src, dst)` (ensystem.c:531) — one-line backup of an admin config before
  regenerating it; dst must be under `$profile:`/`$saves:`. `DeleteFile` likewise works
  only under `$profile:`/`$saves:` (ensystem.c:528).
- `JsonFileLoader<T>.LoadData(jsonString, data, errorMessage)` and
  `.MakeData(data, outString, errorMessage, prettyPrint = true)`
  (3_game\tools\jsonfileloader.c:69/:85) — (de)serialize JSON strings without touching
  files; useful for JSON-over-RPC payloads.

## 6. Timers & the call queue (nothing here persists)

Timers and CallLater do NOT survive a restart — persist `Timer.GetRemaining()` via item
storage (§3) and re-arm the timer in OnStoreLoad.

- Units trap: `Timer.Run(duration, obj, "FnName", params, loop)` takes SECONDS
  (3_game\tools\tools.c:595); `CallLater(fn, delay, repeat, ...)` takes MILLISECONDS —
  mixing them = 1000x bugs.
- `TimerBase` (tools.c:219): `Pause()`/`Continue()` (:239/:247), `IsRunning()` (:264),
  `GetDuration()` (:313), `GetRemaining()` (:318). `Timer` defaults to
  CALL_CATEGORY_SYSTEM (:582).
- Categories (tools.c:8-10, vanilla comments verbatim): `CALL_CATEGORY_SYSTEM` = 0 "Runs
  always"; `CALL_CATEGORY_GUI` = 1 "Runs always (on client)"; `CALL_CATEGORY_GAMEPLAY` = 2
  "Runs unless ingame menu is opened". SYSTEM and GAMEPLAY are both legitimate for server
  logic (vanilla uses GAMEPLAY for mission gameplay timers, missionserver.c:68) — pick one,
  stay consistent, and Remove() what you schedule.
- ScriptCallQueue by-name forms (2_gamelib\tools.c): `CallByName(obj, "Fn", params)` (:59),
  `CallLaterByName(obj, "Fn", delay, repeat, params)` (:63), `Remove(fn)` (:65),
  `GetRemainingTime(fn)` (:67), `RemoveByName(obj, "Fn")` (:69),
  `GetRemainingTimeByName(obj, "Fn")` (:71). Plain `CallLater` takes a function reference,
  not object+string — see enforce-script.md.
- CRASH rule: a pending CallLater on a destroyed object = engine call on freed memory.
  Every object that schedules must `GetGame().GetCallQueue(cat).Remove(this.Fn)` in its
  destructor / cleanup path.
- `GetGame().GetUpdateQueue(cat)` is a per-frame ScriptInvoker (dayzgame.c:1238; vanilla
  PluginManager Insert/Remove at pluginmanager.c:105/:33). Every pending repeating
  CallLater is checked each frame — for many periodic jobs prefer ONE accumulator
  (`m_Acc += timeslice` in an OnUpdate) over many repeating timers.

## 7. Vanilla NotificationSystem (toasts without CF)

3_game\client\notifications\notificationsystem.c — zero-dependency "point awarded" /
"processing done" toasts:

```c
// SERVER → one player (:125); by identity (:141) — identity NULL = broadcast to all:
NotificationSystem.SendNotificationToPlayerExtended(player, 6, "Research", "+1 point");
NotificationSystem.SendNotificationToPlayerIdentityExtended(identity, 6, "Research", "+1 point");
// CLIENT-local (:215):
NotificationSystem.AddNotificationExtended(6, "Research", "settings synced");
```

Full signature: `(target, float show_time, string title_text, string detail_text = "",
string icon = "")`; icon takes imageset syntax (`"set:dayz_gui image:..."`). Max 5 stacked
(:75), default 10 s display + 3 s fade (:71-72); client ScriptInvokers
`m_OnNotificationAdded`/`m_OnNotificationRemoved` (:87-88) to restyle or mirror. Coexists
with CF's `NotificationSystem.CreateNotification` (different channel, needs CF localiser
objects) — the vanilla one is simpler for quick server toasts.

## 8. Choosing the mechanism

| Need | Use |
|---|---|
| Device visual/state for everyone nearby | netsync var + SetSynchDirty |
| One-off event to a specific player (points awarded popup) | CF RPC to identity |
| Quick toast without CF plumbing | vanilla NotificationSystem (§7) |
| Persistent item internals | CF_ModStorage (or OnStoreSave) |
| Remaining processing time across restart | persist Timer.GetRemaining() in storage, re-arm on load |
| Player/faction progress | JSON in $profile keyed by UID |
| Admin configuration | JSON in $profile + defaults autogen + validation |
| Mod-to-mod events in-process | static ScriptInvoker |

## Connect-time and netsync facts (verified live 2026-08-02)

### `InvokeOnConnect(player, identity)` — `player` CAN be NULL

It fires from two events (`ClientNew` and `ClientReady`, missionserver.c:326/:345) and on the
earlier one the character entity is not ready. Guarding only `identity` is not enough: an
unguarded `player` throws `NULL pointer to instance`, the stack unwinds, and **everything
below that line in your override never runs** — on every single connect. The symptom is
indirect: whatever the rest of the method did appears "sometimes broken", because other paths
(a client-side pull request, a later event) partially cover for it.

**Re-check the explanation (code read 2026-11-01).** The scripts do not show how vanilla
could pass a null `player`: both call sites return first when it is null
(`missionserver.c:330-334` and `:350-354` on 1.30.164200, the same guards on 1.29), and CF
1.5.9's own override dereferences `player.GetIdentity()` in its debug line before calling
`super` (CF `missionserver.c:61-65`), so a null `player` would throw inside the `super` call,
before a guard placed after it. Keep the guard, but find out what was actually null (another
mod's caller, or a different dereference) before relying on the explanation above.

```c
override void InvokeOnConnect(PlayerBase player, PlayerIdentity identity)
{
    super.InvokeOnConnect(player, identity);
    if (!identity) return;
    if (!player) return;        // REQUIRED
    ...
}
```

### Netsync variables need one explicit push after load

`RegisterNetSyncVariableInt` in the constructor is necessary but not sufficient. An entity
restored from storage in its default state never passes through any code path that calls
`SetSynchDirty()`, so a client that streams it in may judge it by an uninitialised value.
If a client-side action condition gates on that variable, prompts silently fail to appear
until something touches the entity. Push once in `EEInit` (server side).

### Worn equipment is RESTORED, not attached — `EEItemAttached` may not fire

When a character loads, attachments come back already worn. Logic hanging on
`EEItemAttached`/`EEItemDetached` therefore may not run at login, leaving a window where the
server resolves the player's state (faction, role, whatever the attachment encodes) as
"none". Symptom: the first few interactions after connecting are refused, then everything
starts working — and any slow action (typing a chat command) "fixes" it, because the window
closed on its own meanwhile. Add a delayed re-resolve:

```c
GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLaterByName(player, "MyResync", 4000, false);
```

`CallLaterByName` because the target is another object. Remove pending calls in the
destructor/`EEDelete` — an entity can die inside the delay window.

### Cross-mod calls WITHOUT a compile-time dependency

`ScriptModule.CallFunction(Class inst, string function, out void ret, void parm)` and
`CallFunctionParams` (enscript.c:146-147) invoke a method by NAME. A foreign mod can drive
your item without `requiredAddons` on you, and if your mod is absent the call just returns
false:

```c
Param2<string, float> p = new Param2<string, float>("some_key", 1.0);
GetGame().GameScript.CallFunctionParams(item, "MyMod_SetContent", null, p);
```

To be a usable contract the target method needs: a stable name, only primitive parameters
(no types from your mod), server-side execution, and validation of the incoming values.

## 9. Talking to the outside world: RestApi (measured on 1.29, 2026-08-25)

A DayZ server has **no inbound connections**. Nothing on the internet can knock on
its door, so anything that must reach the game has to be *fetched* by the game.
`RestApi` is the only way out, and it has three traps that cost a whole afternoon
between them.

```c
GetRestApi().SetOption(ERestOption.ERESTOPTION_READOPERATION, seconds);
RestContext ctx = GetRestApi().GetRestContext("https://host/base/");
ctx.SetHeader("application/json");            // Content-Type ONLY
ctx.POST(callback, "v1/route", body);         // async
string now = ctx.POST_now("v1/route", body);  // blocking
```

**An asynchronous request is killed at ten seconds, whatever you set.**
`SetOption` accepts 3..120 s for both `ERESTOPTION_READOPERATION` and
`ERESTOPTION_CONNECTION`, and neither moves the deadline for `POST`/`GET`: the
request dies at ~10 s with `OnError(errorCode == 9)`, `EREST_ERROR_TIMEOUT` (this note
said 8 until 2026-11-01; a mod's REST client handles the timeout as
`EREST_ERROR_TIMEOUT`, measured on a stand, and 8 is the dropped-context case under
`RestContext` below). The documented range does
apply to `POST_now`, which happily waits 25 s on the same address. So a long poll
must be held by **the other end for under ten seconds** — anything longer and the
server answers into a socket nobody is listening on, which from the game looks
exactly like an endpoint that never answers at all.

**A read timeout arrives through `OnError`, not `OnTimeout`.** `OnTimeout` was
never called in any measured run. For a long poll, silence is the normal case, so
the error code has to be inspected rather than reported as a failure:

```c
override void OnError(int errorCode)
{
    if (errorCode == ERestResultState.EREST_ERROR_TIMEOUT) { /* just quiet */ }
}
```

Note that `EREST_ERROR_TIMEOUT` is 9 and `EREST_ERROR_APPERROR` is 8 — the enum in
`3_game/http/restapi.c` has `EREST_ERROR` and `EREST_ERROR_CLIENTERROR` documented
as equal, but Enforce numbers them separately, so every code after `EREST_ERROR`
is one higher than the comment implies.

The full numbering, read out of `restapi.c` (build 124708): `EREST_EMPTY` 0, `PENDING` 1,
`FEEDING` 2, `SUCCESS` 3, `PROCESSED` 4, `ERROR` 5, `ERROR_CLIENTERROR` 6,
`ERROR_SERVERERROR` 7, `ERROR_APPERROR` 8, `ERROR_TIMEOUT` 9. **An HTTP 404 answer
arrives as `EREST_ERROR` (5), not as `CLIENTERROR`** (measured 2026-09-20 against a Node
server replying `404 {"error":"no such route"}`): the engine folds every non-2xx
status into the generic code, and the response body never reaches `OnSuccess`, so a
script cannot tell "no such route" from any other refusal by the code alone.
A connection refused (the service behind the URL down) arrives as `EREST_ERROR_SERVERERROR` (7),
measured the same day while that service restarted under a running server.

**The engine keeps every callback until the process ends.** The diag server's shutdown
report counts one leaked callback per request of the whole boot: N completed requests,
N + 1 `Leaked 'MyMod_RestXfer'` (the +1 in flight), eleven boots on 1.29 and 1.30.164078
(measured 2026-10-15). Dropping every script reference changes nothing, and neither does
`RestContext.reset()` before dropping the context. So a callback object per request is a
slow accumulation for the life of the server (a long poll every ~9 s is ~10 000 a day),
and so is everything it references: once the request is answered, null the callback's
body string and its reply object (keep the reply in a `ref` local while delivering, or a
one-off reply dies with the field). `DestroyRestApi()` might release them (not tried):
that API is shared with every other mod on the server, so it is not a mod's to destroy.

**`OnError` may fire several times for one request** (the engine's own doc says so,
when `RetryCount > 1`). A poll loop that re-polls from `OnError` therefore has to
claim completion exactly once, or one failed request breeds two, then four.

Two smaller things, both from the same afternoon:

* `RestContext` has a **private destructor**, so `ref RestContext` does not
  compile (`Method '~RestContext' is private`) and the error is fatal for the
  whole script module. Hold it in a plain static; `RestApi` owns it and returns
  the same context for the same URL. A context that is not held dies with its
  request: the callback gets `EREST_ERROR_APPERROR` (8) within milliseconds
  (measured on a stand).
* `SetHeader` sets the **Content-Type and nothing else**. There is no way to send
  an `Authorization` header, so a shared secret has to travel in the request body
  — which is also why such an endpoint must be https in production.

## A long string inside a JSON field arrives TRUNCATED, and the error names the wrong thing

Measured in a mod that polls an external service: a poll response carrying one item whose `Json` string
field held ~1200 bytes reached the game cut off mid-string. The game's own
`JsonFileLoader<T>.LoadData` then reported:

```
Cannot load provided JSON data (deserialization error) - check syntax:
JSON ERROR:
Missing a closing quotation mark in string.
```

Two traps in that one message.

**The second line is the useful half, and it is a SEPARATE log line.** A grep
that matches on the first line shows only "deserialization error - check
syntax", which reads as "your JSON is malformed" and sends you hunting a
serialiser bug at the source. The source was producing valid JSON every time.
Always read the line after the warning.

**"Deserialization error" is not a type mismatch here.** The shape matched the
class exactly, field for field. The bytes simply stopped arriving.

Known-good on this build: **805 bytes arrives, ~1200 does not.** Pinned
exactly on 2026-08-28 with a probe: `JsonFileLoader<T>.LoadData` truncates
every JSON STRING VALUE at **1023 bytes** (1000 -> 1000, 1023 -> 1023,
1024 -> 1023, 1500 -> 1023). `MakeData` is NOT capped (a 69 909-byte book
serialised fine and the full values were present in the JSON text) -- the
cut happens on the PARSE side, and it counts bytes, so it can land
mid-UTF-8-character. Anything that feeds JSON to the game must keep every
string value at <= 1000 bytes (UTF-8-boundary clipped).

**A third caller hits the same wall: the MCP `ui_load`/`ui_preview` bridge
command.** `dayz-agentic-modding-mcp` re-serialises a fixture file into one
JSON string and embeds it whole as `args.fixture` in the bridge's command
envelope; the client mod parses that envelope with the same engine JSON
reader, so the 1023-byte-per-string-value ceiling applies to the WHOLE
fixture blob, not to any one field inside it. Measured 2026-09-04 (the chat
page layout of a PDA mod): a 19-op fixture serialising to 2069 bytes
always failed --`JSON ERROR: Missing a name for object member.` for the
Cyrillic content, `Missing a closing quotation mark in string.` for a
same-shape ASCII fixture (same aggregate size, different byte offset for the
1023-byte cut, hence a different-looking parser error for the same cause).
Trimmed to 1020 bytes with the same op shapes, it loaded cleanly. Any UI
fixture with more than a few `text` ops on real sentences crosses this line.
Unlike the RPC and entity-storage callers above, this path has no chunking
yet -- work around it by splitting one `ui_load` into several smaller calls
against the same layout (e.g. rows in one call, header text in another), or
budget fixtures to stay well under 1000 bytes serialised.

**A fourth caller, and the one that looks safe: a JSON document nested inside a
CHUNKED RPC payload.** A framework's sync packet cut into 900-byte RPC parts and
glued before parsing has lost the transport limit -- but a mod's extra that is
itself a JSON document stored as ONE string value of that payload still meets the
1023-byte parse cut. Measured 2026-09-20 (a radio mod): eleven radio profiles
serialised into one extra (~1300 bytes) left the server intact (`extras=4, 2345 b`)
and arrived truncated; the inner `LoadData` failed, the retail client logged
nothing, and the radios stayed on vanilla 87.8. Chunking fixes the wire, not the
parser: keep every string value -- including a nested document -- under ~1000
bytes, i.e. one extra per profile/item, not one extra per list.

**A field the JSON omits, in an object the LOADER allocated, does not get its
constructor default** (measured 2026-09-05, same bridge fixture path; the
general rule is the 2026-09-06 section "An object the LOADER allocated runs NO
field initialisers" below). The fixture's ops are array elements, which the
deserializer allocates itself, so `DZMCP_FixtureOp`'s constructor (`nth = 1`)
never ran: `JsonSerializer.ReadFromString` on an op written without `"nth"`
left it reading 0, and the bridge's `FindNth` -- which counts matches from 1 --
then never matched: every op failed with
`no widget named 'MyMod_Root' (occurrence 0) under the loaded layout`,
which reads like a missing widget and is not. So a class the loader allocates
(an array element, a nested `ref` member, a `LoadData` target passed in null)
cannot carry defaults in its constructor: write every field in the JSON (what
a fixture should do -- `"nth": 1` on every `text`/`show`/`hide`/`color`
op), or treat zero as "absent" only in the same call as the parse -- read
later, an unassigned member is raw memory, not a reliable 0 (2026-09-06).

**Design around it, do not measure to the edge.** Split the payload into
several items and make the receiving side additive, so the pieces merge with
no reassembly step:

```js
// external service: one roster, several items
for (const part of roles.rosterParts()) items.push({Kind: 'roster', Json: JSON.stringify(part)})
```

```c
// game: ApplyRoster adds and updates, never deletes -- so N parts == the whole
```

Anything that carries user text through the same channel inherits this:
a long enough chat message, a note body, a quest description. Budget each
item well under 805 bytes rather than assuming the wire is transparent.

## A JSON member the class does not declare is IGNORED, not an error

Measured in a framework mod: a field was deleted from a config class while eight
live entries of its `Entries.json` still carried the key. The file loaded normally --
`entries=8` in the boot line, no quarantine, no warning -- and the stale key
was left sitting in the file untouched.

This matters when removing a field, because the alternative would be ugly: a
strict reader would fail the load, the loader would quarantine the admin's
table and replace it with shipped defaults, and his edits would be gone
from disk with only a `.bad.json` copy and a log line to say so.

So a field can be dropped from a config class without a version bump and
without a migration. The key stays in existing files as dead text until
something else rewrites them (a version bump, a quarantine, or any code path
that saves), and then it silently disappears.

The reverse case -- a field the class declares and the JSON lacks -- loads
without an error too, and what the field then holds depends on WHO ALLOCATED
the object. In an object your code made with `new` and handed to `LoadFile` /
`LoadData`, a plain field keeps its constructor default. In an object the
loader allocated -- a nested `ref` member, an array element, a target passed
in null -- no initialiser ran and the field is unassigned memory (the next two
sections, measured 2026-09-04 and 2026-09-06). Both directions are forgiving,
which is why `Validate()` earning its keep matters: nothing else will tell you
a field is missing.

## An absent `ref` member comes back ALLOCATED and ZEROED — its field initialisers are not applied (measured 2026-09-04)

Probe on a diag stand (2026-09-04). A view class
with a single member, `ref MyMod_Limits Section`, where
`MyMod_Limits` declares `int InviteTtlSeconds = 120`, was pointed with
`JsonFileLoader<T>.LoadFile` at two copies of a foreign config — one with a
`"Section": { "InviteTtlSeconds": 300 }` section sitting in the MIDDLE of keys
the class does not declare, one without the section at all:

```
PROBE A-section-present: loadfile=ok alloc=yes ttl=300
PROBE B-section-absent:  loadfile=ok alloc=yes ttl=0
```

Two facts, and the second is the trap:

- **A known key standing behind unknown ones is read normally.** Skipping
  unknown members does not disturb the parse of the members that follow, so a
  view class declaring only the part you want is a legitimate way to read one
  section out of somebody else's file. No text slicing needed.
- **A `ref` member the JSON does not mention is returned NON-NULL with zeroed
  fields**, and the nested class's field initialiser (`= 120`) is NOT applied:
  the loader allocated that member, so no initialiser ran.
  So `if (!obj.Member)` cannot answer "was this read?" — it is always false.
  Distinguish "read" from "not read" by a value the file cannot legally hold:
  zero works when `Validate()` enforces a positive floor -- but ONLY in the same
  call as the parse. "Zeroed" was the freshly allocated page; the 2026-09-06
  measurement (next section) found such unassigned members reading garbage once
  the heap moved. Test in the loader, then copy into a `new`-made object.

`LoadFile` returns success in both cases, which is correct (nothing was
malformed) and is exactly why the zeroed object is easy to mistake for read
data. A migration that trusted the null check silently replaced an admin's
`120` with `0`, which the floor clamp then "repaired" to `10`.

## An object the LOADER allocated runs NO field initialisers — every member it did not assign is raw memory (measured 2026-09-06)

The general form of the `ref` fact above, and nastier, because the field looks
right at first. It is **not about empty strings**: absent and empty are the
same case — the deserializer assigns only the members it found a value for,
and every other one keeps whatever was at that offset. Sweep for the READ, not
for `"": ` in the payload.

Factions-mod stand. An external service sends `"FRank": ""` inside a projection;
the receiving class declares `string FRank = "";`. Immediately after
`JsonFileLoader<T>.LoadData` the field reads EMPTY — the log line built one
statement later printed `frank=""`, and the snapshot written to the player
file recorded `"SeenFRank": ""`. Minutes later, from the same cached object,
the same field handed back `3` on one boot and `$<U+FFFD>` on the next, and
`if (v.FRank != "")` was TRUE while `"x:" + v.FRank` printed `x:`.

The field was never assigned at all, and nothing else set it either:
`JsonFileLoader<T>` is a thin wrapper over the native
`JsonSerializer.ReadFromString`, so the object is allocated NATIVELY — the
script constructor and the field initialisers (`= ""`, `= 120`,
`= new array<string>()`) never run. Right after the parse that page is still
freshly zeroed, which is why the first two reads looked correct; once the heap
moves, the same offset is somebody else's data. It is the `ref` fact above,
one level down and for every member, not just nested `ref`s.

**The fix is an idiom, not a per-field guard: copy into a script-constructed
instance, inside the sink.** While every member still reads correctly, read
each one exactly once into an object made with `new` (whose initialisers DID
run) and keep only that:

```c
override void Deliver(string json)          // in the sink, not in the reader
{
    MyMod_RoleView v;
    string err;
    if (!JsonFileLoader<MyMod_RoleView>.LoadData(json, v, err) || !v)
        return;
    MyMod_Roles.Apply(v.Copy());            // Copy() = new + field by field
}
```

One `Copy()` per class that outlives its parse, one call per sink. That
retires the whole class of bug instead of hardening one field, and `== ""` is
honest again everywhere downstream. Inside `Copy()`, guard nested `ref`
members (`if (Posts)`) and let the destination's own constructor allocate its
arrays — an absent `ref` comes back allocated and zeroed, see the section
above.

Consequences and scope:

- **A member read in the SAME CALL as the parse needs no copy** — it is still
  in the honeymoon. The copy is for anything cached, held by a screen between
  refreshes, or written to disk minutes later. Where a copy is deliberately
  absent, write down why, or the next reader moves the read out of the call.
- **Never trust `x != ""`, or `if (!obj.Member)`, on a JSON-loaded member**
  that was not copied. Testing the value against a registry or a whitelist
  hardens ONE field; the copy hardens all of them.
- The sweep pattern that finds it: *a `string`/`int`/`bool` member of a class
  that is ever the target of `LoadData`/`LoadFile`, read after the parsing call
  returns.* `Validate()` inside the loader is a partial cure only: it fixes
  what it touches, in the honeymoon, and only the fields it thinks about.
- A probe printed at parse time proves nothing. Read it again a minute later.

## RestApi and JsonFileLoader are UTF-8-clean; the `curl -d` argv is not

Measured end to end (2026-08-28): a Cyrillic string survives every product
hop -- client RPC -> JsonFileLoader Save/Load -> RestContext.POST -> a Node
server reading the body as UTF-8. What DOES mangle it is the test bench:
`curl -d '{"Name":"Сидорович"}'` from Git Bash on a CP1252 Windows converts
the argv UTF-16 -> ANSI before curl even runs, and every unmappable char
becomes a literal '?'. Nine Cyrillic letters arrive as nine 0x3F bytes and
the far side faithfully stores them -- which looks EXACTLY like an in-game
encoding bug and once cost a session of tracing the wrong suspect.

Seeding rule: never put non-ASCII in a Windows process argument. Write the
JSON to a UTF-8 file and POST with `curl --data-binary @file`, or POST from
node/python, which use wide argv.

## RPC strings corrupt above ~1024 bytes — chunk long JSON

A string serialised into a CF/engine RPC (`Param*<string,...>` through
`RPCManager.SendRPC`) survives up to ~1024 bytes. Beyond that the RECEIVER's
`ctx.Read()` throws `Virtual Machine Exception: !!! String CORRUPTED - FIX
OnStoreLoad() !!!` — the send side reports nothing. Bracketed live on the
stand: a 1011-byte JSON passed, 1386 bytes crashed the reader every time
(a notes-list payload). The root is the engine's 1 KiB string buffer: the
same **1023-byte** boundary was pinned exactly for entity-storage strings
and for JsonFileLoader parse values (2026-08-28), and ~1011 + envelope
strings is that same limit wearing an RPC coat.

The envelope pattern that survives: cut the body into ≤900-byte parts at
UTF-8 character boundaries (walk back over `10xxxxxx` continuation bytes),
send each part ahead via a dedicated `*Part` RPC carrying (message number,
part), then send the normal envelope with the remaining tail as its body. The
receiver accumulates parts per key (sender + message number) and prepends them
when the envelope lands. The SENDER hands out the number, one per long message,
pushes included; it only says "these parts and that envelope are one message"
and does not pair a request with its reply. Keying by (page, op) instead broke
as soon as two requests for the same page and operation were in flight: their
parts landed in one buffer and both bodies died (measured 2026-08-30, a
three-part config read beside a second read of the same config). Ordering is
safe: guaranteed RPCs arrive in send order on the same connection.

## Entity storage: strings die at 1024 bytes, the blob does not (measured 2026-08-28)

Probed on a diag stand (1.29 DIAG, CF v5 ModStorage, data-carrier test items with
synthetic payloads, full write -> save -> restart -> read cycles):

- A string written into an entity's store context (`ctx.Write(string)` --
  CF ModStorage and vanilla `OnStoreSave` share the engine serializer) LOADS
  back only up to **1023 bytes**. Exact boundary: 1023 loads byte-perfect,
  1024 throws `Virtual Machine Exception: !!! String CORRUPTED - FIX
  OnStoreLoad() !!!` at load time. The SAVE side never complains and the
  bytes do land in `storage_1/data/dynamic_*.bin` -- the failure is read-side.
- Blast radius of one oversized string: the ENTIRE mod blob of that one item
  is lost (every field the mod stored -- a handheld-device mod would lose pin, markers, all),
  the item itself SURVIVES with defaults, and neighbouring items load
  untouched. The world keeps booting; the exceptions just pile up in the log.
- The per-entity blob has no practical ceiling: with strings cut into
  <= 1000-byte pieces (int count + N strings; reassembly by concatenation,
  which is uncapped), **1 MiB on a single item survived byte-exact** through
  save + restart, zero exceptions. The "~80 KB ModStorage limit" folklore did
  not reproduce.
- Legacy-format compatibility trick: CF v5 entries are TYPED and a failed
  `ctx.Read(out int)` does NOT advance the cursor -- so a reader can probe
  "new chunked format?" with `Read(int)` and fall back to reading the old
  single string on refusal.

## Storage saves: no players, no autosave; WM_CLOSE saves, taskkill /F loses (measured 2026-08-28)

On the DIAG server with zero players connected, `storage_1` was NOT written
for 12+ minutes after entities changed -- there is no idle periodic autosave
to count on. Two reliable ways to get changes to disk:

- **Graceful console close**: `(Get-Process -Id $pid).CloseMainWindow()` on
  the server console window. The server saves the world (every
  `dynamic_*.bin` rewritten) and exits within ~3 s. This is the way to end a
  persistence test cycle.
- A **forced kill** (`taskkill /F`) discards everything since the last save --
  useful deliberately, to throw away junk writes; fatal to a persistence
  measurement if used by accident.
- Re-measured on the RETAIL server 2026-09-19: `CloseMainWindow()` on the console
  window (title `DayZ Console version (64bit) ...`, attributed to the server's own
  pid) saved every `dynamic_*.bin` and exited in 4.9 s with zero players; a box
  spawned that minute was there after the next boot. Since that day the MCP's
  `server_stop` does exactly this (WM_CLOSE, 30 s, then the kill) and `force=True`
  is the deliberate kill.

## Retail DayZServer forces BattlEye regardless of `BattlEye = 0` (observed 2026-08-28)

`serverDZ.cfg` with `BattlEye = 0;` disables BE on the DIAG server, but the
retail `DayZServer_x64.exe` (app 223350, 1.29) runs BE anyway: a client that
launched with BattlEye off passes the whole login queue and is then kicked
with `BattlEye kicked player ... 'Game restart required'`. Config was
verified read (hostname from the same file applied). Practical rule: DIAG
server = BE genuinely off, clients may disable it; RETAIL server = both
sides must run BE, the config flag is vestigial. Also: a retail server
started from a Steam library folder is adopted by Steam and gracefully
terminated when the user's game session for that app ends -- do not expect
it to outlive the player's client on the same machine.

## Radio / VoN engine internals (measured 2026-09-09 → 12)

Read out of the retail `DayZServer_x64.exe` (16 965 176 B, ImageBase 0x140000000)
while chasing "server FPS high, other players freeze" on a 60-player RP server.
Addresses are for that build; a game update moves them (a patch has to find its
target by code shape, not by address).
The native Linux server carries the same table and the same lookup in another instruction
order (measured 2026-10-05, patched through `LD_PRELOAD`): toolchain.md, "The native Linux
server".

- **The tuned frequency is CACHED on the item's native VoN state.** The 18-byte leaf
  `table[index & 7]` (+0x502D10; that is 1.29 with its eight channels -- 1.30 has nine and
  a `table[index mod 9]` leaf, see "DayZ 1.30 Experimental 1.30.164014: the radio frequency
  lookup changed") runs only from SetFrequencyByIndex / SetNextChannel /
  SetPrevChannel / the transmitter network-state serializer, and every caller stores
  the result (`movss [state+0x1c], xmm0`); GetTunedFrequency reads the cache. So a
  patched leaf costs per TUNE, not per frame — measured: 2 000 000 SetFrequencyByIndex
  = 104 ms with a patched leaf vs 102 ms vanilla (~1 ns/call of difference); the
  bare proto crossing (GetTunedFrequency) is ~23 ns.
- **SetFrequencyByIndex loops over ALL online players.** Before recomputing it calls
  +0x502EA0 ("[VON]: Remove turned off transmitter"), which walks the engine's player
  list and removes the transmitter from each player's map. Cheap once, O(players) each
  time — never in a loop with people online (a radio mod's band probe does 5281 of them at
  boot, when the list is empty).
- **Per-player audibility is a hash map keyed by the frequency float** — FNV-1a over
  its four bytes, open addressing, 32-byte slots (Add +0x9A4570, Contains +0x9A4FB0,
  Remove +0x9A69A0). O(1): more distinct frequencies do not slow it, and nothing
  iterates it per frame — only the router and the remove pass touch it (checked via
  every `lea reg,[reg+0xD8]` in .text).
- **The per-frame transmitter pass is O(switched-on radios × players).** +0x873150
  walks the transmitter list and calls vtable slot 0x848 (+0x503070) per radio; that
  returns at once unless state byte +0x10 is set, otherwise loops every online player:
  GetPlayerPos, distance against `VoNRanges[voice level]` with 1 m hysteresis,
  Add/Remove. This first reading (2026-09-09 → 12) took +0x10 for `EnableBroadcast` and
  called the pass O(broadcasting radios × players). The later reading ("VoN routing"
  below, 2026-09-25, and the 2026-09-26 note that the router only registers radios whose
  SwitchOn byte is set) has +0x10 = SwitchOn and +0x13 = `EnableBroadcast`, which is
  checked on speech (+0x9A6040), not in this pass; it supersedes the first. So every radio
  that is ON is in the pass, and a PTT that toggles only `EnableBroadcast` (as a modded
  handheld radio does) does not take it out.
- **The engine's `[VON]` trace is compiled out on retail AND Diag.** Every `[VON]:`
  site logs at level 4 and the sink (+0x6A4D0 retail, +0x90490 Diag) begins
  `cmp ecx,4; jbe ret`. Do not plan diagnostics around it.
- **`EnProfiler` exists only in DayZDiag_x64.exe.** None of its proto names
  (GetTimePerFunc, GetCountPerFunc, SortData, SetTimeResolution, …) are in the retail
  server binary; 1_Core declares them everywhere, the retail engine binds none. A
  shipped debug mode has to count for itself.
- **Server "FPS" is a smoothed average of the frame loop.** A one-second stall every
  few seconds barely moves it, and it never sees other threads. "Own client smooth,
  other players frozen" means the server stopped sending entity updates; the FPS number
  will not say why. To see where the time goes, sample the process
  (RIP samples of the main thread, names resolved from .pdata).
- **Script-side cost that WAS the bug (a radio mod's band grid):** a `Ready()` check
  walked the whole measured band table on every call and sat on EEInit — 5281 bands →
  1.78 ms per call, 1.9 ms of mod work per radio entity created (42 ms for 20), against
  ~0 with the vanilla eight of 1.29 (the loop exited on the first uneven gap). Fixed by caching
  the verdict once at probe time. Rule: anything derived from a measured table is
  computed once, at the measurement, never on a hot path.
- **The retail engine keeps a readable script call stack — profile script by function
  without Diag.** DumpStack reads it: global `[exe+0xF23610]` → script context;
  `[ctx+0x2C8]` → call-stack object, depth at `+0x48`, frame *i* (1-based) has the
  saved bytecode pointer at `+0x50+i*0x30` and the function descriptor at
  `+0x58+i*0x30`. Descriptor F: `[F+0x08]` bytecode start (C++ pointer for natives),
  `[F+0x40]` module, `[F+0x48]` name, `[F+0x50]` flags (bit 0x10 = script, clear =
  engine native). Module M: `[M+0x28]` name, `[M+0x68]/[M+0x70]` class table (class:
  `+0x10` name, `+0x68/+0x74` function table — a subclass repeats every inherited
  descriptor), `[M+0x78]→+0x20` code start, `[M+0x80]` debug table: `+0x00` file-name
  array, `+0x30` sorted entries `{u32 code offset, u16 line, u16 file}`, `+0x3C` count —
  this is what "Function %s, file:line" in a script crash log comes from. Paths keep the
  mod: `scripts/…` vanilla, `JM/CF/Scripts/…`, `VPPAdminTools/…`, `MyMod/…`.
  A `modded class X` renames the class it replaces to `X@<file index>#<line>` of the
  modding file (`'%s@%d#%d'`, +0x2D5E90); the mod itself keeps the plain name.
  A stack sampler built on this (called "the stack sampler" in the bullets that follow) samples the main
  thread at 200 Hz, reads this stack, reports time per mod, per function, natives
  attributed to their script caller, and stretches with an unchanged stack (= stalls).
  Offsets are for DayZServer_x64.exe 2026-08-13; a tool built on them has to refuse another build.
- **What an empty stand with a full mod stack looks like through it (2026-09-15):** main thread
  81% engine-only, 13% interpreting, 6% natives-from-script; the top script self-cost
  is vanilla `PluginManager.MainOnUpdate` (3.4%) whose `map.GetElement(i)` on the
  plugin map is 2.5% by itself — a by-index walk of a hash map, every frame.
- **VoN and the number of frequencies — closed 2026-09-15.** Nothing in the server's
  VoN scales with how many frequencies *exist*: the per-frame transmitter pass is
  O(transmitters) + O(switched-on × players) (+0x872D50 → +0x873150 → +0x503070; written
  "broadcasting" here on 2026-09-15, before the 2026-09-25 reading, see the pass above), the
  per-player audibility map is O(1) per op keyed by the float (Swiss table, tombstones,
  Add/Contains/Remove +0x9A4570/+0x9A4FB0/+0x9A69A0), and the listener table
  (+0x9AA800, `NetServer` vtable slot 12 via +0x6646F0) is a *reconcile*: the caller
  hands in the desired list, entries not in it are erased ("remove not existing
  listener"), so nothing accumulates. Every Add and Remove keys on the same cached
  float `[state+0x1c]`, so a patched leaf cannot desynchronise them. A 4-hour live
  sample (60 slots, 1.53 M main-thread samples) put all of VoN at 0.77 %, 0.635 % of it
  in the for-each-player callback +0x8735D0, with no single VoN function standing out
  the way a stall would.
- **What the same live sample says the main thread does instead:** script VM 44 %
  (interpreter alone 20.6 %), engine→script event dispatch ~2.5 % (+0x258570 walks the
  class chain `[C+0x28]` to find the `EOn*` handler, +0x257F90/+0x257E30 call it through
  a refcounted delegate), class-chain walks for `Cast`/`IsInherited` ~1.8 % (+0x2C8A90,
  no .pdata; the callers include "Unsafe down-casting, use '%s.Cast'"), Bullet physics
  queries ~1.7 % (`btCollisionWorld::objectQuerySingle` +0xAAF880, shape support
  +0xAB4120, dbvt traversal +0xAC2DA0), AI 2.7 %, UAInput ~5 %, ntdll waits 11 %.
  Whose script needs the stack sampler (above) on that server; a RIP sample
  alone cannot say.
- **`JsonFileLoader.JsonLoadFile` / `JsonSaveFile` block the main thread for as long as the disk
  and the parser take — measured on a live 60-slot server 2026-09-16:** a storage mod loading
  its boxes' JSON from `DayZGame.OnUpdate`, from `OnStoreSave` and from `CommandHandler` froze
  the whole server 0.15–2.5 s per load, 1318 times in 8 hours, 365 s in the worst hour, with
  every player seeing every other player stand still. File loads belong at mission start or
  behind a player action with a size cap, never on a per-frame or per-save path; a JSON that
  grows with the player base is a freeze that grows with the player base. Part of the cost
  may be `JsonLoadFile`'s own line-by-line string building (likely, not measured: see "JSON
  I/O is not the slow part" below).
- **Scripted lights run their full visual logic on a dedicated server:** `ScriptedLightBase.EOnFrame`
  (dancing shadows, flicker, brightness/radius fading, daylight pulse) took 26 % of the live
  main thread inclusive with 60 players' lights, and ZombFix's light "optimization" layer added
  ~16 % of its own on top of vanilla's ~20 %. A server-side mod that skips the visual handlers
  and keeps only lifetime/fade-out is the cheap win; measure with the stack sampler before
  and after.
- **VoN routing, read out of DayZServer_x64.exe 2026-09-25 (the question was whether more than
  eight frequencies can be in use at once — they can; nothing in the chain counts them).**
  Transmitter state bytes: `+0x10` SwitchOn, `+0x13` EnableBroadcast, `+0x14` EnableReceive,
  `+0x12` "some player is within voice range of this radio" (set by the router), `+0x18` index,
  `+0x1c` cached float, `+0x20` a per-radio float sent on the wire as an int. Per frame
  (+0x503070) every switched-on radio registers its frequency in the map of each player whose
  OWN voice level range (`VoNRanges[whisper/talk/shout]`, 1 m hysteresis) reaches the radio:
  the map answers "which radios can pick up this player's voice". On speech (+0x9A6040, from
  +0x6BF470) each such radio with broadcast ON is matched against every radio with receive ON
  on the same float frequency; the listener tables (+0x9AA800, per player, keyed by player id
  and the player's NET channel — "channel: %d" is the network channel, not a frequency) are
  reconciled from that. The client never sends a frequency: it starts one stream per own
  transmitter by entity id ("(Self) Start transmitter %d", VoNSystemXA2 slots 14/28 →
  +0x752130 → +0x74F410, a reconcile re-run whenever the client's radio map changes), and
  the receiving side plays streams tagged with the float ("(buffer %u) Start transmitter %f").
  Practical consequences: a PTT that opens the air by RPC loses roughly one round trip plus
  two frames at the start of every press (the client's own stream only starts once the synced
  broadcast flag arrives); a radio outside the speaker's voice-level range (on the ground, in a
  car, while whispering) never picks the voice up; a server freeze drops the voice of that
  interval for everyone, radio and direct alike.
- **The listener tables are rebuilt every frame, not on events (checked 2026-09-25):** the
  world update calls `+0x6DA400`, which walks every player and for each one whose
  transmitting flag (`player+0x27c`) is set recomputes the listener set from the current
  radio maps (`+0x6BF070` → `+0x9A6040` → `+0x9AA800`). So a listener who connects, tunes
  or switches a radio on mid-transmission is added the next frame; `+0x9AA800` shows 0 %
  in profiles only because it changes nothing when the desired list is unchanged.
- **A client's own `EnableBroadcast` never reaches the server (read 2026-09-25, DayZ_x64.exe and
  DayZServer_x64.exe 2026-08-13).** The client native (`+0x5EED90`) writes byte `+0x13` of the
  CLIENT copy and calls the generic entity-changed hook (`+0x7035C0` → `+0x6F29E0` →
  `+0x716220`, same shape as the server's `+0x66CEF0` → `+0x691670`); no VoN or net send
  follows. On the server `+0x13` is written only by its own native (`+0x5683C0`) and by the
  READ path of the ItemTransmitter serializer (`+0x568730`, slot 2 — the replication receiver,
  i.e. the client), plus the static-transmitter equivalents (`+0x766510`, `+0x7667C0`). The
  server's dispatcher of client messages (`+0x6C38A0`, NetworkServer slot 4) handles inventory
  commands, object delete, chat, admin login, reports, script user messages and disconnect —
  there is no "entity state from client" message. Delivery is gated by the server's `+0x13`
  in `+0x9A6040`. Practical use: a client may pre-open its own flag at the PTT press so its
  stream starts in the same frame; the server still decides which radio is heard.
- **A freshly created ItemTransmitter starts with its broadcast flag SET (observed 2026-09-26,
  retail 1.29, radio-mod stand).** A radio spawned on the ground and never switched on
  answered `IsBroadcasting() == true` on the server until the mod's first shut pass called
  `EnableBroadcast(false)` on it; vanilla never notices because the VoN router only registers
  radios whose SwitchOn byte is set. A mod that reads `IsBroadcasting()` as "is on the air"
  must gate it by power/`IsOn()` first, or clear the flag on creation.

## Cargo, entity creation and replication at scale (measured 2026-09-16, retail server 2026-08-13, CF + probe only)

Stand: a storage-mod probe, retail `DayZServer_x64.exe`, one
retail client windowed in the background (19 fps).

- **A cargo grid has at most 256 rows.** `itemsCargoSize[] = {10, 500}` reports 500 through
  `GetCargo().GetHeight()`, but every creation from row 256 on returns null: 2560 one-slot
  cells at width 10. Width 20 is accepted (`{20, 256}` took 5000 items). Vanilla's widest
  grid is 20 (`gear_camping`), its tallest 50 rows (SeaChest-class 10x50 is 500 cells).
- **`MissionServer.OnUpdate(timeslice)` is clamped at 0.3 s.** A frame with 724 ms of script
  work arrived as `timeslice = 0.3`. Measure frame length with `GetTickTime()` deltas between
  calls; `GetTickTime()` advances inside a frame with sub-ms resolution and `TickCount(prev)`
  ticks at 10 MHz.
- **An idle dedicated server has no frame cap**: 0.1--0.4 ms per frame (2500--13000 fps) with
  zero or one player. "N items per frame" is therefore not a rate; pace by time.
- **Creating items is cheap per item, expensive per burst.** `GameInventory.LocationCreateEntity`
  into an explicit cargo cell costs 0.07--0.15 ms per one-slot item (100 -> 13 ms,
  1000 -> 114--128 ms, 5000 -> 362 ms in ONE frame); a rifle with 3 attachments 1.6 ms
  (0.4 ms per entity). `CreateEntityInCargo` (engine searches a free cell) costs the same
  as an explicit cell (1000 -> 109 ms). A client within the bubble roughly doubles creation
  cost (5000 -> 816 ms). `ObjectDelete` 5000 -> 259 ms in one frame (383 ms with a client).
  Spread over frames (50 per frame) nothing exceeds 100 ms; a time budget per frame is the
  robust form.
- **A vanilla container's CARGO reaches a client only within about 20 m of it (measured
  2026-09-30, retail server + client, `CarTent` 10x50 full of `Paper`).** The container is on
  the client from far away; its items arrived at 11.2, 14.9, 18.3 and 19.1 m and never at
  20.3, 20.6, 20.8, 22 m or further (up to 200 m). Each container crosses the line on its
  own. The server then streams them at about 300 items per second per client (1000 items in
  3-4 s); neither side showed a longer frame while 1000 or 4000 items arrived (server frames
  0.2 ms mean, <= 21 ms longest; background client capped at 30 fps, longest frame 61-64 ms
  as when idle), and the main-thread profiler found no stall during a delivery. Received
  cargo STAYS on the client while the container is in its network range: moving 300 m away
  deleted 66 entities, not the 2000 items; a relog cleared them. Consequence for storage
  design: vanilla leaks every item to every client that walks within ~20 m, whatever the
  container's access rules.
- **A packed tent refuses script creation into its cargo** (500 of 500 `LocationCreateEntity`
  into a packed `CarTent` returned null); `TentBase.Pitch(true, true)` first, then fill. A
  vanilla barrel is 10x15 = 150 cells; the vanilla 500-cell containers are the car tent and
  the 10x50 sea-chest family.
- **A container lying in cargo refuses children from script too.** `Container_Base.
  CanReceiveItemIntoCargo` returns false when `!GetInventory().AreChildrenAccessible()`, and
  `CreateEntityInCargo`, `CreateEntityInCargoEx` and `LocationCreateEntity` + `SetCargo` all
  returned null for a bag inside a crate (0 of 1000 / 500 / 500). Fill a container BEFORE it
  goes into another container's cargo.
- **...but an ATTACHMENT keeps its children reachable, two levels deep.** The rule is one
  function, `EntityAI.AreChildrenAccessible()` (`3_game/entities/entityai.c:1685` on 1.30;
  read 2026-09-23 at :1662 on 1.29): it walks UP the hierarchy from the entity and returns
  false the moment an ancestor's location is `CARGO` or `PROXYCARGO`, while `ATTACHMENT` and `HANDS` only
  increment a counter checked against `GameConstants.INVENTORY_MAX_REACHABLE_DEPTH_ATT`,
  which is **2**. So a jacket ATTACHED to a container's `Body` slot still has usable
  pockets, and the same jacket dropped into that container's cargo grid does not -- in a
  vanilla sea chest exactly as in a modded one. **This is the design lever for any "store a
  whole kit" container: declare the vanilla character slots (`Headgear`, `Body`, `Vest`,
  `Back`, `Legs`, `Feet`, `Gloves`, `Armband`) and clothing kept there stays usable.**
- **...but CLOTHING has a second, stricter gate, and it is the one that usually bites.**
  `Clothing.CanReceiveItemIntoCargoClothingConditions` (`4_world/entities/itembase/
  clothing_base.c:66`, read 2026-09-23) is `return !hierarchyParent || hierarchyParent.
  IsMan() || SmershException(hierarchyParent);` -- **a garment accepts cargo only while it
  is loose or worn by a person.** Attached to a container it can be EMPTIED but never
  FILLED, because taking out is not gated by this method. "I can take things out of the
  jacket but not put them in" is this function, every time, and it is separate from the
  depth rule above. Vanilla widens it for its own stash in the sibling
  `CanLoadItemIntoCargoClothingConditions` (line 93): `if (parent && parent.IsInherited
  (UndergroundStash)) return true;`. A mod whose container is meant to hold a kit does the
  same with `modded class Clothing`, overriding BOTH methods (the Load twin runs when the
  server reads a saved tree back, and without it a filled garment loses its contents on the
  next restart) and calling super for every other parent. Measured working 2026-09-23:
  `MyMod_PersonalStash` accepted a move into a jacket in its `Body` slot while a vanilla
  `SeaChest` control still refused. Check the whole chain rather than the direct parent:
  pouches on a vest on the container have the VEST as their parent, and the cheapest
  correct test is `GetHierarchyRoot()`, not a counted walk.
- **A SLOT ID CAN BE NEGATIVE, so `slot >= 0` is not a test for "this is a slot".**
  `InventorySlots.GetSlotIdFromString` returns a hash: `Body` is 5119774 and `VestPouch`
  is -230399667 (measured 2026-09-23). Code that restores a saved tree and decides between
  the cargo path and the attachment path by the sign of the slot silently sends half of
  its items to the wrong place -- a jacket in Body goes home, vest pouches end up on the
  ground. Carry an explicit flag beside the id.
- **Reaching through attachments stops at two steps, on a container and on a player
  alike.** Measured 2026-09-23 with a chain of any length: container <- vest <- pouches
  gives `AreChildrenAccessible() == true` and its cargo can be filled; container <- vest
  <- holster <- pistol ATTACHES fine but answers false. So a deeper tree can be BUILT,
  SAVED and RESTORED -- only moving things into it is refused, and the same refusal
  happens on a character's own body.
- **`ECE_NOPERSISTENCY_WORLD` REALLY KEEPS A CONTAINER AND ITS WHOLE TREE OUT OF THE SAVE**
  (measured 2026-09-24, against a plain control that did come back). Items put in with
  `CreateEntityInCargo` -- a call that takes no flags of its own -- are not saved either,
  and are not dropped loose on the ground in place of their parent: after a save and a
  restart there was nothing at the spot at all. `ECE_LOCAL` on the SERVER gives network id
  `00`: the object is not registered on the network at all, rather than registered and
  withheld. So `ECE_LOCAL | ECE_NOPERSISTENCY_WORLD` is a container that no client can
  learn about and that a crash leaves no trace of -- the shape for a server-side
  authoritative box whose truth lives elsewhere.
- **A CONTAINER BUILT ON THE CLIENT WITH `ECE_LOCAL` IS FULLY USABLE, AND THE VANILLA
  INVENTORY SCREEN DRAWS IT** (measured 2026-09-24). The client's own `CreateObjectEx(cls,
  pos, ECE_LOCAL|...)` gives an entity with network id `00` that HAS a `GameInventory` and a
  `CargoBase`; `CreateEntityInCargo` fills it; the vicinity panel shows it with its real
  name, art and cell count. Nothing of it exists on the server or on any other client.
  This is how a container can be shown to ONE player: DayZ has **no per-player entity
  visibility** (everything in `3_game/global/game.c` taking a `PlayerIdentity` is a
  player-lifecycle call, a listing, or `RPC`/`RPCSingleParam` -- `RemoteObjectCreate` and
  `RemoteObjectTreeCreate` take an `Object` and nothing else), so you do not replicate
  selectively: you send the contents over an addressed RPC and let that one client build a
  mirror.
  **Moves inside such a mirror work in `InventoryMode.LOCAL` and NOT in `PREDICTIVE`**
  (measured: `TakeToDst` returned true and the item moved, versus false and no move).
  PREDICTIVE applies here and asks the server to agree, and there is no counterpart to
  agree with. The vanilla inventory screen drags PREDICTIVE, so it renders a mirror
  perfectly and cannot rearrange it -- a screen of your own issuing LOCAL moves can, with
  the engine still validating placement. A refused predictive move leaves nothing stranded.
- **Network bubble streaming is throttled at ~310 entities/s.** A client entering the bubble
  of a crate with 5000 cargo items received them over 17.8--17.9 s with no frame above 77 ms;
  the server stayed under 28 ms per frame. Leaving the bubble deleted 5000 client entities in
  ~2 s. Items CREATED next to a client are not throttled: 1000 / 2500 / 5000 created in one
  server frame reached the client in 1.1 / 2.2 / 3.0 s at 900--1700/s, no frame drops -- but
  one 5000-burst (a batched restore) stalled the client for 39.2 s in a single frame
  (`[ErrorModuleHandler] :: Error thrown: 0x00040004` in the client RPT a minute later; not
  reproduced in two further bursts). Pacing creation at 250/s (25 items every 100 ms) showed
  no stall in any run. Treat large creation bursts towards nearby clients as a risk to pace.
- **JSON I/O is not the slow part.** `JsonFileLoader.SaveFile` of 5000 flat item records:
  7 ms serialise + 10 ms write (360 KB compact, 870 KB pretty on disk); `LoadFile` of that
  file 9--14 ms on the main thread; 1000 records 2 + 2 ms. The live 60-slot server's 0.15--2.5 s
  `JsonLoadFile` freezes (VORDEK storage mod) mean far larger files or many loads per frame,
  not the loader itself. Likely, not measured: that conclusion compares two different
  loaders. `LoadFile` reads the file with one `ReadFile`; the deprecated `JsonLoadFile`
  builds its text line by line, `while (FGets(...) >= 0) file_content += line_content;`
  (`3_game/tools/jsonfileloader.c:105-130`), copying the growing string once per line --
  quadratic in the line count of a pretty-printed file. Time `JsonLoadFile` itself before
  blaming file size; switching to `LoadFile` helps either way.
- **The engine autosaves continuously while a player is connected**: `storage_1/data/
  dynamic_00N.bin` is rewritten round-robin, one file per second. A crate with 5000 items
  created at runtime survived `taskkill` + restart with all 5000 (they had been autosaved
  within a minute). This is the source of "double truth" for any virtual-storage mod: an
  open box's cargo is in the engine's save within a minute of opening.
- **A vanilla player login costs one frame of 1.8 s on the server** (and 1.7 s on the client)
  on this stand -- the yardstick for "freeze".
- **The stack sampler on a near-idle stand reports false "engine, no script" stretches**:
  with 91 % engine-only samples, 30 consecutive engine samples (150 ms at 200 Hz) happen by
  chance; the frame monitor saw no frame above 16 ms in those windows. Its script-attributed
  stretches (e.g. "MyMod_Probe.FillOne 39 %") match the frame monitor exactly.
- **Enforce refuses long string expressions**: this measurement failed at 21 terms and
  compiled 16; others failed as low as 14 (all of them in enforce-script.md, "Formula too
  complex" — calls weigh more than plain terms, keep each expression to about 10 parts).
  On 1.30 the error comes up as a MODAL "Can't compile ... Formula too complex" dialog
  (2026-10-11); build long log lines with `+=`.

## Item state round trip, nested cargo and the persistency flag (measured 2026-09-16, retail server, CF + a framework, a PDA and a radio mod)

Stand: a storage-mod probe (stock, blob save/load, inspect and nest operations).

- **`entity.OnStoreSave(ctx)` into a `FileSerializer` and `OnStoreLoad(ctx, g_Game.SaveVersion())`
  on a freshly created entity is a complete, exact round trip**, mods included. A crate of AKM
  (health 40, chambered `Bullet_762x39`, attached `Mag_AKM_30Rnd` with 17 rounds), Rag
  (quantity 3, wetness 0.25), Canteen (liquid 512 = LIQUID_WATER, quantity 500), Battery9V
  (energy 5), `MyMod_Radio_100m` (frequency index 3, stored by vanilla `TransmitterBase.OnStoreSave`)
  and `MyMod_DataCarrier_Chip` (two notes in CF_ModStorage, written through
  `g_Game.GameScript.CallFunctionParams(chip, "MyMod_WriteNotes", ret, Param2<string,int>)`) was
  serialised (7 entities, 1217 bytes, 7 ms), deleted, recreated from the file (4 ms) and
  inspected: every field identical except the canteen's environmental temperature. `SaveVersion()`
  is 142 on this build (1.29) and 144 on 1.30. A blob saved on 1.29 must be loaded with ITS
  version, so store the version beside the blob: 1.30 added `version >= 143/144` reads
  (`entityai.c:2981` construction data, `combinationlock.c:169`, `magazines.c:112`), and
  feeding a 1.29 stream 144 makes those read bytes that are not there. Order that worked
  (Expansion's): header (type, location type, slot, row,
  col, flip), children records, weapon chambers + internal magazine cartridges, `OnStoreSave`,
  magazine cartridges (`GetCartridgeAtIndex` / `ServerSetAmmoCount(0)` + `ServerStoreCartridge`),
  global + per-zone health (`GetDamageZones`, `GetHealth(zone,"Health")`), `GetLifetime`; on load
  chambers are pushed BEFORE `OnStoreLoad`, cartridges/health/lifetime after, then
  `AfterStoreLoad()`, `SetSynchDirty()`, `Weapon_Base.Synchronize()`, `EEOnAfterLoad` via
  `GetCallQueue(CALL_CATEGORY_SYSTEM).Call(...)`. The radio mod's own log confirmed
  `radio restored: MyMod_Radio_100m ... index=3` on the recreated item.
- **An `OnStoreLoad` that returns false leaves the stream position undefined**: nothing behind that
  entity in the same file can be read. Isolate blobs per entity (or per class, as Expansion does)
  if a partial load must survive. `ConfigIsExisting` before creating; a class that no longer
  exists cannot even consume its blob.
- **Nested containers restore only in the order "fill first, then insert".** A `SmallProtectorCase`
  created on the ground (`CreateObjectEx(... ECE_PLACE_ON_SURFACE|ECE_NOLIFETIME)`), filled with
  `CreateEntityInCargo`, then moved into the crate with
  `crate.GetInventory().FindFreeLocationFor(bag, FindInventoryLocationType.CARGO, dst)` +
  `GameInventory.LocationSyncMoveEntity(src, dst)` arrived with its contents (3 of 3 cases, 9 of 9
  children; the whole nested tree of 19 entities round-tripped through the blob exactly).
  Measured with no client watching -- before reusing this recipe read the next bullet (the
  bare move tells no client) and "Nested containers built by script" (2026-09-18: a move in
  the same frame as the target's creation leaves an undeletable ghost, and a SYNC_MOVE of a
  filled container leaves a client ghost; build the tree locally, announce it with
  `RemoteObjectTreeCreate`, then `SetSynchDirty()` on every entity of it, 2026-09-26).
  Backpacks refuse: `Clothing_Base.CanPutInCargoClothingConditions` allows clothing into cargo
  only when `GetNumberOfItems() == 0` (or the parent is a player / UndergroundStash) --
  `FindFreeLocationFor` returned false for all 5 filled `TaloonBag_Blue`. Vanilla rule, not a
  restriction to fight.
- **`LocationSyncMoveEntity` moves the item and tells no client** (measured 2026-09-17; it cost a
  live bug). It is the raw local move. `GameInventory.TakeToDst(InventoryMode.SERVER, src, dst)`
  does the same move AND sends the `SYNC_MOVE` command to the clients
  (`3_game/systems/inventory/inventory.c:1056-1073`).
  Restoring items into a container with the bare call succeeds server-side and leaves every client
  drawing them where it last saw them -- on the ground around the container -- until something
  forces a refresh; when the server later deletes them they simply vanish, which reads to the
  player as "the box ate my things". With no client connected the bug is invisible, which is how a
  measured, tested restore path shipped with it. Rule: **any server-side move a player may be
  looking at goes through `TakeToDst(InventoryMode.SERVER, ...)`**, with
  `TakeEntityToTargetCargoEx(InventoryMode.SERVER, cargo, item, row, col)` and
  `CreateEntityInCargo` as fallbacks (`TakeEntityToCargoEx` is `[Obsolete]` on 1.30 and fails a
  `max_warnings = 0` verdict; its exact old behaviour is spelled out in "DayZ 1.30 Update 2").
  The bare call is fine only for entities no client has ever been told about. Two exceptions
  measured later: a move into a container created in the SAME frame, and a SYNC_MOVE of a
  container that has children -- see "Nested containers built by script" (2026-09-18).
- **`ECE_DYNAMIC_PERSISTENCY` does nothing for items created in a container's cargo.** 100 `Paper`
  created with `LocationCreateEntity(loc, type, ECE_IN_INVENTORY | ECE_DYNAMIC_PERSISTENCY, RF_DEFAULT)`
  and 100 `Matchbox` without the flag were all 199 back after autosave + `taskkill` + restart (one
  Paper had been moved to the player and persisted there too). The flag is the object spawner's
  ground-object switch (`objectspawner.c`, arrows in `dayzgame.c`); the engine's save of a
  persistent container includes its whole cargo regardless. There is no script API to make an
  existing entity non-persistent -- keeping an open box's items out of `storage_1` is a policy
  problem, not a flag.
- **Weapon `inventorySlot[]` is an ARRAY in `Addons/dz.pbo`**: `Rifle_Base` = {"Shoulder","Melee"},
  `Pistol_Base` = {"Pistol"} (CfgConvert of `dz/config.bin`, lines 4033 / 4285), so a mod may
  extend it with `inventorySlot[] += {...}` without the T148506 string trap. Slot classes are
  declared as `class CfgSlots { class Slot_X { name = "X"; displayName = ...; ghostIcon = ...; } }`
  (the game registers them at boot).
- **The knowledge base of the `dayz` MCP** (`knowledge_build` core = scripts.pbo + Addons configs
  read in place, 131 697 declarations in 74 s on 1.29; 141 279 in 71 s on 1.30.164200,
  2026-10-21; `knowledge_show(body=True)`) answers script
  questions without extracting anything; only binarised config BODIES still need CfgConvert.
- **`ECE_NOPERSISTENCY_WORLD` is ignored for cargo items as well (measured 2026-09-16, run 6):**
  100 `Paper` created with `LocationCreateEntity(loc, type, ECE_IN_INVENTORY | ECE_NOPERSISTENCY_WORLD, RF_DEFAULT)`
  into a persistent crate's cargo, plus 100 unflagged `Matchbox`; one flagged Paper dropped on the
  ground by `Man.ServerDropEntity`, three Matchboxes moved to the player. After the engine's
  autosave cycle, `taskkill` and a restart: crate 99 Paper + 97 Matchbox (identical `inspect`
  hash), the dropped flagged Paper still on the ground, the player's 3 Matchboxes still on the
  player. Together with `ECE_DYNAMIC_PERSISTENCY` (run 5) this closes the flag route: the ECE
  persistency flags govern objects created in the WORLD by `CreateObjectEx`; a child of a
  persistent container is saved with its parent whatever flags it was created with.
  `ECE_NOPERSISTENCY_CHAR` was not tested.

## Measured 2026-09-16 (evening, storage mod implementation, retail 1.29)

- **`FileSerializer` write cost: ~0.17 ms per entity** when each record carries the entity's
  `OnStoreSave` blob (1430 entities = 238 ms in one frame; 61 frames of 5 ms when paced).
  Reading back with `OnStoreLoad` costs ~0.02 ms per entity without a client and ~0.13 ms
  with a client in the bubble (replication). Pace both across frames; keep the
  `FileSerializer` open across frames, it works.
- `MakeDirectory`, `CopyFile`, `DeleteFile`, `FileExist` (all `$profile:`) and `OpenFile` +
  `FPrintln` + `FGets` behave; `string.Split` DROPS empty fields; `FGets` lines may carry a
  trailing `\r` (files written by `FPrintln` are CRLF) -- `Trim()` the last field.
- **The world's persistent entities load a few frames AFTER `MissionServer.OnMissionStart`**
  (a box registered from `EEInit` was counted 0 at OnMissionStart and reconciled in
  `EEOnAfterLoad` later); anything that needs the loaded world must wait (15 s was enough).
- `OnStoreSave` of an entity runs WHILE the engine writes its autosave; do not delete a
  backing file inside it -- defer a few seconds.
- `PlayerBase.OnDisconnect()`: `GetIdentity()` is already null; keep the name from `OnConnect`.
- `UIManager.FindMenu(MENU_INVENTORY)` answers the inventory menu while it is HIDDEN; ask
  `InventoryMenu.IsOpened()` for "shown". `VicinityItemManager.GetVicinityItems()` keeps the
  last list after the screen closes.
- Entity RPC from the client (`Object.RPCSingleParam(id, Param1<bool>, true)` ->
  `OnRPC(sender, id, ctx)` on the server's copy) is the cheapest "client tells the server about
  this object" channel: the sender identity and the object come for free, no id on the wire.
- `GetGame().ObjectDelete(obj)` (a `CGame` method, `3_game/global/game.c:705`, not an
  `Object` one) of 50 root entities per frame (children included) = 3--6 ms per frame
  with a client in the bubble; 1468 deletes reached the client inside one second without a
  frame above its idle 51 ms.
- The stack sampler (200 Hz) reports back-to-back frames of 20 ms as ONE stretch
  of their sum (14 capture frames -> "232 ms freeze") and resolves frames of 5 ms; when the
  acceptance instrument is the profiler, pace work at <= 5 ms per frame and let the frame
  monitor be the second witness.
- **New action classes must be listed in `ActionConstructor.RegisterActions`** (see
  actions.md); `ItemBase.AddAction` drops unregistered ones with an error in the .RPT only.

## Measured 2026-09-16 (late night, a storage mod: placement kits and inventory UI, retail 1.29)

- **Deployable kit** = `ItemBase` with `IsBasebuildingKit()` true (the deploy action deletes it
  and `Hologram.PlaceEntity` leaves it alone), `IsDeployable()`, `GetDeployTime()` (default
  `UATimeSpent.DEFAULT_DEPLOY` = 5 s), `SetActions` adding `ActionTogglePlaceObject` +
  `ActionDeployObject`, and `OnPlacementComplete(player, position, orientation)` creating the
  real object (`CreateObjectEx(type, GetPosition(), ECE_PLACE_ON_SURFACE)` then
  `SetPosition/SetOrientation`, `HideAllSelections()`). The hologram projects the config class
  `<kit>Placing` (or `projectionTypename`), and reads `hologramMaterial` /
  `hologramMaterialPath` from THAT class (`<path>\<material>_deployable.rvmat` and
  `_undeployable.rvmat`; vanilla has them for `wooden_case`, `sea_chest`, `tent_medium`...).
  Rotation: `MissionGameplay` turns the hologram by 15 degrees on `UANextAction` /
  `UAPrevAction` while `PlacementCanBeRotated()` (default true).
- **Hologram refusals** (`Hologram.EvaluateCollision`): `floating` is true when the aim ray's
  contact point is under `minProjectionDistance` (1 m for objects under 1 m radius) or over
  twice that from the player -- aim at the ground about 1.5 m ahead; the rest are bbox,
  proxy, player, roof, base viable, garden plot, zero pos, angle, `CanBePlaced`, height, water,
  terrain, `PlayerBase.CanPlaceItem`. Retail has no `DIAG_DEVELOPER` details: a
  `modded class Hologram` re-running the checks after `super.EvaluateCollision()` and writing
  them with `ErrorEx` is the way to see them.
- **Retail client logging**: `Print` reaches no file (no script log on the client);
  `ErrorEx(text, ErrorExSeverity.WARNING)` lands in the .RPT, `INFO` does not.
- **Inventory icon shading**: the icon's "Color" panel (`icon.layout`, priority 2) sits BEHIND the
  `ItemPreviewWidget` "Render" (priority 151); the render ignores `SetColor`; a `PanelWidget`
  with `style blank` paints nothing at all -- `style rover_sim_colorable` paints its colour.
  A panel created with `CreateWidgets(layout, icon.GetMainWidget())` at priority 500 shades the
  item. Cargo icons are initialised through `Icon.InitEx`, not `Init`; `CargoContainer.
  UpdateInterval` calls every icon's `UpdateInterval` each frame, `SlotsIcon` has one too.
- (Hidden versus shown inventory menu, `FindMenu` versus `IsOpened`: see the evening list
  above.) A widget created inside `UIScriptedMenu.layoutRoot`
  of the inventory (a screen-sized frame) with `halign right_ref` and `position 40 6` lands
  in the strip above the equipment column.
- **Engine string casing**: `string.ToLower/ToUpper` turn non-ASCII bytes into spaces --
  Cyrillic must be folded with `Replace` tables. (`string.Split` dropping empty fields: see
  the evening list above.)
- **Stand facts**: a retail client sits in the pause menu after every connect ("back" leaves
  it, the pad's first press after an attach is swallowed); the right trigger is the use
  input (tap = single action, hold = continuous); `world_action` cannot drive a placement
  (the hologram lives on the client); a file the client polls once a second
  (`$profile:...` under the client profile) is the only mouse-free way into its screen.

## Nested containers built by script: the stuck deletion and the ghost (measured 2026-09-18, retail 1.29, storage probe mod)

Two separate engine faults, both reproduced on a stand with a watcher that reads
`ToDelete()`, `IsPendingDeletion()`, `IsSetForDeletion()`, `HasInventoryJunctureItem`,
`IsInventoryLocked()` and the spatial index (`GetObjectsAtPosition`) every 0.5 s.

**Server.** An entity that a script moved (`TakeToDst(InventoryMode.SERVER)` or the bare
`LocationSyncMoveEntity`) into a container created in the SAME frame can no longer be deleted
once it has left that cargo for the ground (a player's take-out and drop, or another script
move): `ObjectDelete` runs the children, then stops on the root and its direct child -- network
id 0, inventory location UNKNOWN, still returned by `GetObjectsAtPosition`, `ToDelete` and
`IsPendingDeletion` true forever. A second `ObjectDelete`, `EntityAI.Delete()`,
`RemoteObjectTreeDelete` + `ObjectDelete`, or deleting the tree deepest first do not finish it;
a player JOINING the server frees every pending one; a restart persists them as real items
(the top two levels of the tree) -- a dupe. `IsSetForDeletion` is why nobody can pick it up
(`CanBeActionTarget` false, juncture denied). Not triggered by: `CreateEntityInCargo` /
`CreateEntityInCargoEx` into the same-frame container, a player's juncture move, the same script
move into a container created one frame or more earlier, creation flags (`ECE_IN_INVENTORY` vs
`ECE_PLACE_ON_SURFACE`), `RemoteObjectTreeDelete`/`Create` around the move. The engine refuses
to create or move anything INTO a container that already sits in a cargo (`CreateEntityInCargo`
null, `FindFirstFreeLocationForNewEntity` no cell), so nested trees must be built bottom-up on
the ground and moved -- which is exactly the same-frame pattern. Cure: queue the moves and run
them from the next frame.

**Client.** A SYNC_MOVE (what `TakeToDst SERVER` sends) of a container WITH children into a
cargo leaves the client's copy in the same state whatever the delay (10 frames measured): after
the player takes it out, drops it and it is deleted, the copy stays with location 0 at its last
ground spot until a relog. A client that joins after the tree is in place gets one creation and
is fine. Cure, vanilla's own (`ReplaceItemWithNewLambdaBase`, `PlayerBase.CreateCopyOfItemInInventory`):
build the tree out of local entities (`CreateObjectEx(..., ECE_LOCAL | ...)`,
`GameInventory.LocationCreateLocalEntity(il, type, ECE_IN_INVENTORY, RF_DEFAULT)`), move them
in `InventoryMode.LOCAL` a frame later, then `GetGame().RemoteObjectTreeCreate(root)` once the
root is in place -- the clients receive the finished tree, and nothing is drawn on the ground
meanwhile. Then call `SetSynchDirty()` on every entity of the announced tree: values changed
while it was unannounced do not travel with the creation (measured 2026-09-26,
"RemoteObjectTreeCreate: the client's fresh copy starts from the config").

## File API limits that shape a store format (measured 2026-09-19, retail 1.29)

- `FileSerializer` is `Open`, `IsOpen`, `Close`, `Read`, `Write` -- no position, no seek,
  no raw bytes. A typed stream cannot step over a body it cannot parse (an `OnStoreLoad`
  of a class whose mod left the server), so one file holding many records loses every
  record behind the first unreadable one. Isolate by file: one `FileSerializer` file per
  root/record; the open goes on with the next file. Cost on a test disk: ~0.8 ms per file
  written, ~0.13 ms per file opened for reading (1400 files: 1108 ms paced write,
  196 ms read).
- `FindFile(dir + "\\*", name, attr, FindFileFlags.DIRECTORIES)` + `FindNextFile` +
  `CloseFindFile` enumerate a `$profile:` directory (1.29; for 1.30 see "DayZ 1.30 Update 2,
  1.30.164200"); a subdirectory appears as a plain name, without a slash -- tell it apart
  with `attr & FileAttr.DIRECTORY` (FindFile sets attr = 1 for a directory, measured; see
  "File I/O sharp edges"). `CopyFile` + `DeleteFile` is the only move. `DeleteFile` on an
  emptied directory returns true and removes it.

## Persistent id and the FileSerializer byte layout (measured 2026-09-19, retail 1.29, storage probe mod)

- **`EntityAI.GetPersistentID(out b1, out b2, out b3, out b4)` is valid the frame the entity is
  created** by `CreateObjectEx` on the server (four non-zero ints, e.g. `-1836652215-591616390-994303877-1955932247`
  for a `Paper`), and **the same four ints come back after a graceful stop (WM_CLOSE save) and a
  boot**. `Object.GetID()` is the session's network id and changes every boot (654396474 ->
  654377818 for the same Paper). Vanilla itself stores persistent ids only inside `OnStoreSave`
  (the energy source of an `EntityAI`, the parent of `PowerGeneratorStatic`) and resolves them with
  `g_Game.GetEntityByPersitentID(b1,b2,b3,b4)` (sic), which the entityai.c comment says is
  callable only inside `OnStoreLoad`/`AfterStoreLoad`. So a persistent id is a usable stable key
  for a store from the moment of placement, not only after the first save.
- **A `FileSerializer` file is a flat stream of raw little-endian values with no header of its
  own**: `Write(int)` = 4 bytes, `Write(float)` = 4 bytes IEEE, `Write(string)` = int32 length +
  the bytes (no terminator), `Write(bool)`/`Write(vector)` as their natural widths; the file
  begins with the first value written and ends with the last (hexdump of a store record:
  `02000000 13000000 "2026-09-19 00:09:07" 77050000 05000000 "Paper" 03000000 ffffffff ...`).
  Consequences: an outside process (Node, Python) can generate a file the engine reads with the
  matching `Read` sequence, can parse every field whose layout it knows, and can carve an opaque
  `OnStoreSave` body out of the stream by searching for a random marker written after it -- the
  engine still cannot skip a body itself (no seek), but a sidecar can rebuild the file without it.

## Engine facts of the SQL-truth storage (measured 2026-09-19, retail 1.29, a storage mod)

- **`local` is a reserved word of Enforce Script.** A class field `bool local;` makes the
  compiler report `Syntax error` at the line of the `class` keyword, not at the field. A field
  named `type` compiles (proved by a class kept in the storage probe).
- **`GetYearMonthDayUTC` gave the year as one digit** in a file name built as
  `y.ToString() + Pad2(mo) + ...` on this build (a close file came out `...-90919-044630.bin`),
  while the same call inside a `YYYY-MM-DD HH:MM:SS` stamp read 2026. When a file name must
  carry a date, build it out of the string stamp by dropping the punctuation.
- **`GetGame().ConfigIsExisting("CfgVehicles " + type)` is false for weapons and magazines**:
  they live in `CfgWeapons` and `CfgMagazines`. A "does this class exist" check has to ask all
  three roots, or every rifle counts as a vanished mod.
- A stand without players never autosaves, so a `taskkill` rolls the world back to the last
  graceful stop -- a crash test on such a stand shows the rules for a stale save, not for a
  save taken mid-transition. A live server with players autosaves every minute.

## Persistent id is read-only (measured 2026-09-22)

`proto void GetPersistentID(out int b1, out int b2, out int b3, out int b4)`
(`3_game/entities/entityai.c:3374` on 1.30, :3380 on 1.29) is the ONLY persistent-id API
in the script corpus -- a grep for `SetPersistentID` over all of `scripts.pbo` returns
nothing (still so on 164200). An entity
recreated from a snapshot (`CreateEntityInCargoEx(type, idx, row, col, flip)` and
friends) is a NEW entity with a NEW persistent id, so any mod keyed to that id must
re-key when it rebuilds an item rather than expecting the id to travel with it.

Related engine limit: `JsonFileLoader` cuts every JSON string VALUE at 1023 bytes (pinned
2026-08-28, see "A long string inside a JSON field arrives TRUNCATED"; seen again here
2026-08-30 with envelope 2925 / body 1023), so a nested document or a base64 blob cannot
travel as one string -- chunk it, or keep values as short scalars. `map<K,V>` is reported
not to survive RPC serialization (stated here without a probe; the community report is
about `map<string, ref T>`, patterns-cookbook.md / CannabisPlus), and its behaviour under
`JsonFileLoader` is untested here; vanilla `1_core/proto/serializer.c` documents map support
(its example writes a `map<string, float>`). Production mods (MapLink's UniversalAPI and
others) all use an array of `{key, value}` records instead -- and an array of
RECORDS, never several parallel arrays, which carry an index invariant nothing enforces.

## The persistence context IS a Serializer (read 2026-09-22)

`3_game/gameplay.c:15-16`:

```c
typedef Serializer ParamsReadContext;
typedef Serializer ParamsWriteContext;
```

Both "contexts" are plain typedefs of `Serializer`, and `FileSerializer : Serializer`
(`1_core/proto/serializer.c:103`, with `Open(path, FileMode)`, `Write(void)`,
`Read(void)`, `Close()`). So `entity.OnStoreSave(fileSerializer)` and
`entity.OnStoreLoad(fileSerializer, version)` are type-correct: an entity's WHOLE hidden
persistence stream -- vanilla fields, CF ModStorage blocks and any mod's direct writes --
can be dumped to a file and replayed onto another entity. `ScriptReadWriteContext`
(`3_game/gameplay.c:134`) is the in-memory equivalent (`GetWriteContext()` /
`GetReadContext()`), but nothing exposes its bytes to script, so only the file route
crosses a process boundary. Doing this outside the engine's own load phase is UNMEASURED.

Two constraints on replaying such a blob:

- **The stream is positional and schema-less.** Every mod's `OnStoreSave` appends to the
  same stream and `OnStoreLoad` reads it back in the same order. A blob written where
  mod X was loaded, replayed where it is not, desynchronises everything after X's bytes.
  Same game build (`forceSameBuild`) and the same mod set, or do not replay.
- **CF ModStorage is the self-describing exception.** A CF block is
  `{m_HashA, m_HashB, m_Version, m_Entries}` where entries are typed scalars
  (`CF_ModStorageDataType`: BOOL/INT/FLOAT/VECTOR/STRING) held in script memory
  (`CF_ModStorage.c`), written with a count and per-block hashes
  (`CF_ModStorageObject.OnStoreSave`). Blocks whose hash pair matches no loaded mod are
  kept in `m_UnloadedMods` and rewritten verbatim on every save -- CF already carries
  state for mods it does not have. That makes CF blocks the safe unit to move between
  servers with differing mod sets, extracted by driving `entity.CF_OnStoreSave(map)`
  after `_ResetStream()` and reading each `CF_ModStorage.m_Entries`.
- `CF_ModStorageModule` keeps only a list of **player UIDs**, in
  `$mission:storage_<instanceId>/communityframework/modstorageplayers.bin` (next to the
  world save, not in `$profile:`; CF 1.5.9 `cf_modstoragemodule.c:172-187`); item blocks live
  inside the entity's own record, so deleting an entity deletes its mod storage and there is
  no side table to clean.
- (VoN routing, the per-frame listener tables, a client's own `EnableBroadcast` and a fresh
  transmitter's broadcast flag -- read 2026-09-25/26 -- moved to "Radio / VoN engine
  internals" above.)
- **A modded class of a class declared in the SAME script module compiles and applies when its
  file sorts after the original inside the folder (observed 2026-09-26):** a mod's
  `5_Mission/MyMod/MyMod_FreqMenuFace.c` (`modded class MyMod_FreqMenu`) next to
  `MyMod_FreqMenu.c`; the server boot and the client load reported no script error. Files inside
  one module folder are compiled in alphabetical order, so the name has to sort later.

## A server without your mod REWRITES the world save without your entities (measured 2026-09-26)

A stand booted on the same mission with a different `-mod=` list (a storage mod absent)
loaded the world save, could not create the classes it did not know, and on its own save
(a graceful stop) wrote the world WITHOUT them: three placed containers of the absent mod
were gone at the next boot of the stand with the full mod set. Nothing in either log says so. On a shared
stand, never run a second profile against the same mission folder unless it loads every
mod whose entities the save holds -- or give it its own mission copy.

## RemoteObjectTreeCreate: the client's fresh copy starts from the config (measured 2026-09-26)

An entity taken off the network with `RemoteObjectTreeDelete`, changed while unannounced
(`SetQuantity` 6 -> 3 on a server-local container's item) and announced again with
`RemoteObjectTreeCreate` was drawn on the client with the CONFIG quantity (6) while the server
held 3, until the next change of any netsync variable (`SetCleanness`) brought the current
block and the copy read 3. A netsync int set before the FIRST announcement (`m_Cleanness` 1)
did arrive on the second creation, so it is the values changed while the entity was
unannounced that the re-announcement does not carry. Fix that measured clean: right after
`RemoteObjectTreeCreate(e)`, call `SetSynchDirty()` on every entity of the announced tree;
the next frame's sync carries quantity, wet and cleanness to the copies just made.

`Disinfected` in vanilla is `ItemBase.m_Cleanness == 1` (`SetCleanness(1)` in
recipes/disinfectitem.c:89; `Rag`, `BandageDressing` and `SewingKit` read `m_Cleanness == 1`
for "no infection", and their `OnCombine` drops a clean stack to 0 when a dirty one is merged
in). It is a netsync int (`RegisterNetSyncVariableInt("m_Cleanness",0,1)`), server-set only.


## A relog re-creates the character AND every item on it (measured 2026-09-28)

When a logout completes, vanilla `MissionServer.PlayerDisconnected` prints
`[Logout]: Player <uid> finished`, calls `player.Save()` and `GetHive().CharacterExit`,
then `HandleBody` runs `player.Delete()` on a living body that is neither unconscious nor
restrained (missionserver.c:690-748). The next login loads a NEW character from the hive,
and every item it carries is a new entity instance. So item fields that are not written by
`OnStoreSave` / `CF_OnStoreSave` are reset by an ordinary relog, not only by a server
restart. A reconnect inside the logout window (`OnClientReconnectEvent`) keeps the old
entity. That case was not measured here.

Measured on a PDA-mod stand: that mod's PIN counter (three parallel arrays on the item:
uid, fail count, time of the last fail) reached the lockout. The lockout read "299 s"; the
client was stopped, and about 45 s after it came back the correct PIN unlocked the device about
two minutes before the window ended. The server log showed `[Logout]: Player ... finished`,
the mod's own power-lost lines for the items at the delete, and fresh drain lines for the same
classes after the reconnect.

Rule: throttles, lockouts and cooldowns that must survive a relog cannot live on the item
or the player entity in memory. Either store them with the entity (CF storage; keep
remaining milliseconds, not `GetGame().GetTime()` stamps, because that clock restarts with
the server) or keep them server-side in a static map keyed by something that outlives the
entity.

## Measured 2026-10-05 (a storage mod: own models with lids and doors, retail 1.29)

- **A user animation moved on one client stays on that client.** `SetAnimationPhase(source, 1)`
  called on the client alone: that client's `GetAnimationPhase` read 1 and the model drew open,
  while the server's read 0 for the same entity -- so nobody else sees it (a locker whose doors
  open only for the player using it; vanilla does it for `PowerGenerator.UpdateFuelMeter`).
  For everybody: a netsynced bool, `SetAnimationPhase` on the server when it changes and again
  in `OnVariablesSynchronized` on the clients; both sides then read 1. The source is a
  `source = "user"` class in the config's `AnimationSources`, `animPeriod` its travel in
  seconds, and the model.cfg animations name it.
- **What the hologram repaints.** `Hologram.RefreshVisual` paints the hidden selection called
  `placing` or `inventory`, and `GetHiddenSelection` falls back to INDEX 0 when the
  projection's class has neither. So a model with one whole-LOD selection first in
  `hiddenSelections[]` is repainted whatever the selection is called, and a `<kit>Placing`
  class with NO `hiddenSelections` is not repainted at all: its hologram is the plain opaque
  model. With `_deployable` / `_undeployable.rvmat` copied from the vanilla sea chest's, the
  hologram is white where it can stand and nearly clear where it cannot; the vanilla sea
  chest's own looks the same in both states on the same client.
- **Orientation.** `CreateObjectEx(<item class>, pos, ECE_PLACE_ON_SURFACE)` gave four items
  four yaws (-152, -145, -15, 85 degrees), a `StaticObj_*` made the same way stood at 0. A box
  made by a kit takes the hologram's orientation, which is the player's yaw: the model's -Z
  side then faces the player, so a face meant to look at whoever deploys it belongs at -Z.
  The vanilla `locker_closed_v1` has its door at -Z as well.
- **Nested config classes do not merge** (the config rule; not measured in game here): a
  derived class that writes its own `class DamageSystem { class GlobalHealth { class Health {
  healthLevels[] = ...; }; }; };` REPLACES the parent's `DamageSystem`, and `hitpoints`,
  `transferToAttachmentsCoef` and `GlobalArmor` go with it (SeaChest 600, WoodenCrate 400).
  Leave the block inherited, or restate all of it. The parents' `healthLevels` name vanilla
  materials; a model that does not carry them just keeps its look when damaged.
- **What makes an item at rest solid for a character: its physics layer.** `SeaChest`,
  `WoodenCrate` and `Barrel_ColorBase` inherit `physLayer = "item_small"` from
  `Inventory_Base` (only tents, fences, watchtowers, barbed wire, camo nets, Hesco boxes,
  shelters and the flag declare `"item_large"`), and a character walks through them. Two
  ways to make one solid, both seen on a stand:
  - `physLayer = "item_large"` in the config of an `Inventory_Base` class: a locker so
    declared stopped a player character. For the whole class, every instance of it.
  - `dBodySetInteractionLayer(ent, PhxInteractionLayers.ITEM_LARGE)` after
    `dBodyIsSet(ent)`, on the server AND on every client (the client simulates its own
    character): a box so raised stopped a character and carried one standing on it. Per
    instance -- the way when an unannounced or client-local twin of the class must stay
    passable. Set from these hooks together, not told apart: `EEInit` and `EEOnAfterLoad`
    on the server; on a client `EEInit` behind `HasNetworkID()` (a local `ECE_LOCAL` copy
    has none) and `OnVariablesSynchronized`.
  **`dBodyGetInteractionLayer` is not a test of solidity**: the locker, solid by its config,
  read 0 on the server and on the client, while the raised boxes read `ITEM_LARGE`. Walk
  into the thing. The layers are bit flags: `ITEM_SMALL` = 33554432, `ITEM_LARGE` =
  67108864. Vanilla's own multi-state items use `AddProxyPhysics("<selection>")`
  (HescoBox).
- **Stand pad**: `right_thumb` toggles first and third person (the third is where a hologram
  in front of a carried kit can be seen); the right trigger held 12.5 s completes a 10 s
  deploy; `b` closes the inventory.

## DayZ 1.30 Experimental 1.30.164014: FindFile and FindFileFlags (measured 2026-10-11, Windows dedicated server, control run on retail 1.29.163709, same probe pbo)

**The FindFile behaviour below is 164014-164078 only; fixed in 164200, see "DayZ 1.30 Update
2, 1.30.164200".** The other facts in this section were measured on 164014.

- `FindFileFlags.DIRECTORIES / ARCHIVES / ALL` read `0 / 0 / 0` from script on 1.30 and
  `1 / 2 / 3` on 1.29. `1_core/proto/ensystem.c` is byte-identical between the builds: the
  NATIVE enum changed, the header did not. `FileAttr` (DIRECTORY=1, HIDDEN=2, READONLY=4,
  INVALID=-1) and `FileMode` (READ=1, WRITE=4, APPEND=8) print the same on both builds.
- On 1.30 the fourth `FindFile` argument selects ONE mounted file system by index (measured
  with int literals, 3 mods loaded): 0 = the server working directory; 1..N = the loaded mods
  in reverse `-mod` order (a `*` listing shows the pbo prefix: `MyMod_FsProbe`, `MyMod_Core`,
  `JM` for CF); then two `DZ` game-data roots and the engine core data (`bin`, `Core`,
  `graphics`, `gui`, ...); then the `-profiles` directory (7 with 3 mods), the config
  directory (`DayZ Exp.cfg`), the mission directory; indices just past the table (10..16)
  behave as "every file system"; 24 = ACCESS VIOLATION inside FindFile (crash log, minidump).
  Since every script passes 0, FindFile on 164014-164078 lists only the working directory.
- Patterns on 1.30 with the enum: `$profile:...`, `$mission:...`, `$saves:...`,
  `$CurrentDir:...` drop the prefix (`$profile:*` lists the working directory, a subpath
  returns no handle, also on an existing directory). Backslash patterns return no handle
  (1.29: fine). `mpmissions/*` (cwd-relative, forward slashes) works on both. Absolute paths,
  `..` traversal and `./x` return no handle on BOTH builds -- they never worked. Archive
  prefixes (`MyMod_Core/*`, `languagecore/*`) return no handle on 1.30 (1.29: 44 entries).
  A bare `MyProbe/sub/*` found the profile directory on 1.29 (the VFS searched several roots)
  and nothing on 1.30.
- Known-path functions are untouched on 1.30, all with `$profile:` and backslashes:
  `MakeDirectory` (still one level per call), `OpenFile` READ/WRITE, `FileExist`, `CopyFile`,
  `DeleteFile`, `JsonFileLoader` (`JsonSaveFile`/`JsonLoadFile`/`SaveFile`/`LoadFile`);
  `OpenFile` of packed files (`<prefix>/stringtable.csv`, `languagecore/stringtable.csv`) works.
- Consequence for a mod: a `$profile:` directory listing on 1.30 works only when the
  `-profiles` directory sits INSIDE the server directory and the pattern is cwd-relative with
  forward slashes (`profiles/MyMod/*.json`), which is what CF-Test's `CF.FindFileEx` does
  (Bohemia ticket DZEXP-134, reported as slated for after 1.30 stable). A `-profiles` outside
  the server directory cannot be listed at all. Probing the file-system index upward until the
  profile directory answers works, but a wrong index crashes the server -- do not ship it.
  Design for 164014-164078: keep a list of known names (an index file) and read by
  name. Superseded 2026-10-21: on 164200 a `$profile:` listing with backslashes works with a
  `-profiles` directory outside the server directory ("DayZ 1.30 Update 2").
- Other facts from the same boots: `3_game/tools/jsonfileloader.c` identical to 1.29 (still
  so on 164200); `[Obsolete]` attributes 53 -> 212 (`OnCEUpdate` -> `OnCEIterate(float
  currentTime, float elapsedTime)`; on `EntityAI` the Predictive/Local/Server forms of
  `TakeEntityToCargo`, the Predictive/Local forms of `TakeEntityToCargoEx` and the
  Predictive/Local/Server forms of `TakeEntityAsAttachmentEx` and `TakeEntityAsAttachment`
  -> the matching `...ToTargetCargo(Ex)` / `...ToTargetAttachment(Ex)` forms
  (`3_game/entities/entityai.c:4835-4907` on 164200); on `GameInventory`
  `TakeEntityToCargo`, `TakeEntityToCargoEx`, `TakeEntityAsAttachmentEx` and
  `TakeEntityAsAttachment` -> `...Target...` forms (`3_game/systems/inventory/
  inventory.c:1345-1386`); the 1.30 inventory UI calls the ToTarget forms, e.g.
  `5_mission/gui/inventorynew/inventory.c:562`); CF 1.5.9 and a framework mod compile and boot
  on 1.30 unchanged. The "Formula too complex" limit holds on 1.30 as a MODAL compile dialog
  (see "Cargo, entity creation and replication at scale").
- Vanilla 1.30 prints its own `SCRIPT (W)` lines on a clean boot (`human.c` missing
  declarations, `disinfectantspray.c` BotStateMachineData); they are not the mod's.
- `FileSerializer.Open` (the binary store API: `Open(path, FileMode)`, `Write`, `Read`,
  `Close`) works on 1.30 under `$profile:` with backslashes and with forward slashes, round
  trip verified; a missing parent directory fails the open as on every build.
- `[Obsolete]` is a COMPILE-TIME warning on every mention, executed or not: the 1.30
  compiler prints `SCRIPT (W) @"<file>,<line>": 'X' is obsolete: <message>` for the
  declaration line of an `override` of an obsolete method, for the `super.X()` call inside
  it, and for each call site of an obsolete method (`TakeEntityToCargo`,
  `TakeEntityAsAttachmentEx`). An attribute that vanilla commented out
  (`PredictiveTakeEntityToInventory`) prints nothing. A stand verdict with `max_warnings = 0`
  therefore fails on 1.30 for a mod that still overrides or calls the 1.29 inventory names;
  the Target forms (`PredictiveTakeEntityToTargetAttachment(Ex)`,
  `TakeEntityAsTargetAttachmentEx`, `TakeEntityToTargetCargo(Ex)` with a `CargoBase`) exist
  on 1.29 as well, so migrating early is safe.

## DayZ 1.30 Experimental 1.30.164014: the radio frequency lookup changed (measured 2026-10-11)

- Vanilla has NINE radio channels on 1.30, not eight: the table is `80.0, 87.8, 89.5, 91.3,
  91.9, 94.6, 96.6, 99.7, 102.5` (`.data`, rva `0x16114c0` in `DayZServer_x64.exe`
  1.30.164014; the 1.29 table of eight sits at rva `0xE55570`). On 164200 a frequency proxy
  found the same two shapes and its self-test passed ("DayZ 1.30 Update 2"); the table itself
  was not re-read there.
- Likely, not measured: vanilla `TransmitterBase` saves the channel INDEX (`OnStoreSave`
  writes `GetTunedFrequencyIndex()`, `OnStoreLoad` calls `SetFrequencyByIndex`,
  `4_world/entities/itembase/transmitterbase.c:12-33`), and 1.30 put 80.0 in front of the old
  eight, so a vanilla radio saved on 1.29 should come back one channel lower on 1.30 (87.8 ->
  80.0, 89.5 -> 87.8, ...). A mod that re-tunes from its own stored value is unaffected.
- The lookup is still one leaf with the same ABI (index in `edx`, result in `xmm0`), but
  `and eax, 7` is gone; it is `table[index mod 9]` with a magic multiply: `movsxd rcx, edx;
  movabs rax, 0xE38E38E38E38E38F; mul rcx; shr rdx, 3; lea rax, [rdx+rdx*8]; sub rcx, rax;
  test ecx, ecx; lea eax, [rcx+9]; cmovns eax, ecx; lea rcx, [rip+table]; cdqe; movss xmm0,
  [rcx+rax*4]; ret` (50 bytes at rva `0x90fa30`, 8 `call` sites; 1.29's 18-byte leaf at
  `0x502D10` had 9). The ninth caller, the transmitter initialiser that stores the frequency
  into the object (`movss [rdi+0x1C], xmm0`), has the lookup INLINED at rva `0x90f966..`.
- Consequence for a `hid.dll` frequency proxy written for 1.29's single shape: on 1.30 its
  code-shape check logs
  `NOT PATCHED: the frequency lookup's code shape is not present` and patches nothing, by
  design. To support 1.30 such a proxy needs a second shape (the leaf above, `rel32` wildcarded,
  the magic constant literal), a nine-entry vanilla table check starting at 80.0, the same
  12-byte entry redirect, and a decision about the inlined copy (it only sets a fresh
  transmitter's initial frequency; a mod can re-tune by index itself). Superseded 2026-10-21:
  a proxy now finds both 1.30 shapes in 164200 ("DayZ 1.30 Update 2").
- Method: find the table by its float32 values, then scan executable sections for any rel32
  landing in it (capstone 5.0.7 did the disassembly); a second server instance
  on its own port and profiles directory with the proxy `hid.dll` beside the exe answers in 20 s.
- The Linux 1.30 server (depot 1042422 of app 1042420, fixed base 0x400000, GCC) has the
  same change: leaf at VA `0xDEC850`, 46 bytes, index in `esi`, `movsxd rsi, esi; movabs
  rdx, 0xE38E38E38E38E38F; mov rax, rsi; mul rdx; shr rdx, 3; lea rax, [rdx+rdx*8]; sub rsi,
  rax; lea rax, [rip+table]; movsxd rdx, esi; movss xmm0, [rax+rdx*4]; ret`, eight `call`
  sites; the inlined copy at `0xDEC8CA` reads `[rbx+0x18]`, stores `[rbx+0x1C]` and has
  the caller's `mov byte [rbx+0x15], 1` between the multiplier load and the `mul`. GCC 13
  at -O2 loads the multiplier BEFORE the movsxd and at -O3 copies registers before it, so a
  finder must take the function entry from the `call rel32` targets, not from the first
  instruction it recognises (measured: a jump placed after those copies lost 3 of 24
  registers in the stand-in test). Anonymous steamcmd refuses app 1042420 ("Missing
  configuration"); a logged-in Steam client's console `download_depot 1042420 1042422` fetched the
  4-file, 33 MB Linux depot into `steamapps/content/app_1042420/depot_1042422`.
- The LINUX 1.30 server checks script-instance leaks at shutdown (SIGINT): stopping with live
  singletons it prints `SCRIPT (E): Leaked '<Class>' script instance (1x)!` per class and
  `==== Total Leaks (N)! ====`, and the Linux build then exits through the assertion
  "Script is leaking! Check log!" (`crash_<stamp>.log`, enf_scriptmodule.cpp:599). The
  retail Windows server prints nothing at all (1.29 and 1.30.164078: `~DayZGame()` and out),
  but **DayZDiag prints the same report on 1.29 and on 1.30.164078 without asserting**, so
  the diag stand is the place to read it on Windows. Measured 2026-10-15:
  - A 3_Game static holding 4_World instances (a registry of handlers that 4_World modules
    fill) is what leaked on Linux 1.30.164014 and on 1.29 diag: the World module unloads
    before Game. 1.30.164078 diag did NOT list those, so its silence proves nothing. Empty
    such registries from `modded class MissionServer { override void OnMissionFinish() {
    super.OnMissionFinish(); ...clear...; } }`: super includes CF's dispatch of every
    module's `OnMissionFinish` (CF missionserver.c:40-44), so nothing reads them afterwards.
    Done that way in a framework mod: the Linux report lost all five classes.
  - Vanilla 1.30 leaks by itself: `Leaked 'BunkerBroadcastManager' script instance (1x)!`
    (diag 1.30.164078 in a second report block, Linux 1.30.164014 alone), and the Linux
    assertion fires on that one too. A clean Linux 1.30 shutdown is out of a mod's reach.
  - Every `RestCallback` handed to `RestContext.POST/GET` stays referenced by the engine
    until the process ends: see section 9.

## DayZ 1.30 Update 2, 1.30.164200 (stable 2026-10-21; measured 2026-10-21)

Client, DayZDiag and the dedicated server are all 1.30.164200; `dta/scripts.pbo` is
byte-identical between client and server. Experimental got the same build on 2026-10-16.

- **FindFile lists `$profile:` directories again.** `FindFile("$profile:MyMod\\data\\*", ..., FindFileFlags.ALL)`
  -- a `$profile:` prefix, backslashes, and a `-profiles` directory OUTSIDE the server
  directory -- listed all 9 seeded files (1.30.164078: 0). Archives are NOT back to 1.29:
  `FindFile("*", ..., FindFileFlags.ARCHIVES)` returned 100 root entries with CF + 11 mod pbos
  loaded (1.29: about 2,200-2,900 with the stand set; 164078: 26-47, which was the server
  working directory), and `"<prefix>/*"` was not re-measured. The enum values themselves
  were not re-read. The changelog calls it a FindFile fix.
- `vector.Cross(vector)` does not exist on 164200, so a mod calling `.Cross(` does not
  compile. Neither 1.29 (`scripts.pbo` 125372) nor 164200 has it in
  `1_core/proto/enconvert.c` (the two files differ in no `Cross` line); if it existed, then
  only in 164014-164078, which nobody extracted. The cross product is the `*` operator
  between two vectors, already used so in 1.29 (`enconvert.c` rotation helpers, `axis * vec`)
  and in 1.30's new sun code (`environment.c:696`, `(forward * up).Normalized()`).
- `CCTCursor`, `CCTCursorParent` and `CCTCursorNoRuinCheck` now check distance differently ON
  THE SERVER (`player.GetInstanceType() == INSTANCETYPE_SERVER`): `Can()` returns
  `DistanceSq(target.GetPosition(), player.GetPosition()) <= (maxDist +
  target.GetCollisionRadius())^2` and skips the cursor-hit tests (the changelog's "actions at
  any distance" exploit). The client keeps the cursor test. So the server measures the
  object's ORIGIN plus its collision radius, whatever the player aims at.
- Other script API changes: `Transport.HasCrew()` (proto native) and `Transport.IsMoving()`
  added (CarScript's `IsMoving` is now an `override`); base building
  `GetSupportProvided/GetSupportRequirement(int)` became `bool ...(int, out int)` and
  `TryHandleUtilityParts(EntityAI)` became `TryQueueUtilityPartsUpdate(ConstructionBase)`;
  `NavmeshTest*` classes renamed `Land_NavmeshTest*`; `PluginInventoryRepair` is registered
  in every build (was debug-only); the Workbench script API was restructured
  (`Workbench.GetModule/OpenModule/CloseModule` take a `typename`, a new
  `1_core/workbenchapi/` folder), so Workbench plugins need a pass. The exe-level option
  `resolveFilePatchingUsingEnfusion` is renamed `loadRVFilesViaEnfusionFS` (the strings in
  the 164078 and 164200 executables).
- **VPPAdminTools did not compile on 164200 until VPP's own update the same day**
  (Workshop 1828439124, build of 2026-10-15; diag and the retail server, also with CF
  alone): `SCRIPT (E): Can't open script file:
  'VPPAdminTools/VPPNotifications/Definitions/vppnotifications/definitions.c'`, then `Can't
  compile "Engine" script module!`. Its `engineScriptModule` lists
  `VPPAdminTools/VPPNotifications/Definitions`; the Mikero-packed pbo holds
  `VPPNotifications\Definitions\VPPNotifications\Definitions.c`. Upstream issue
  VanillaPlusPlus/VPP-Admin-Tools#223. The mechanism is not established: FileBank-packed
  pbos with nested script folders (a whole mod set built that way) compile on the same build. The fix
  came with VPP's Workshop update at ~11:00Z.
- Moving off the 1.30-obsolete inventory names, exactly: `inv.TakeEntityAsAttachmentEx(mode,
  item, slot)` IS `inv.TakeEntityAsTargetAttachmentEx(mode, owner, item, slot)` (a pure
  delegation); `inv.TakeEntityToCargo(mode, item)` equals `inv.TakeEntityToTargetCargo(mode,
  owner, item)` unless `inv` is a `HumanInventory`, which overrides `TakeEntityToInventory`
  (the old path) but not the Target one; `TakeEntityToCargoEx` was `SetCargo(owner, item,
  idx, row, col, item.GetInventory().GetFlipCargo())` + `TakeToDst`, while its suggested
  replacement goes through native `SetCargoAuto(CargoBase, ...)` -- spell the old one out to
  keep behaviour. `inv.TakeEntityAsAttachment(mode, item)` is obsolete too and, like
  `TakeEntityToCargo`, takes the old `TakeEntityToInventory` path (`inventory.c:1382-1386`;
  replacement `TakeEntityAsTargetAttachment(mode, owner, item)`). The `EntityAI` wrappers
  over these -- Predictive/Local/Server `...TakeEntityToCargo`, Predictive/Local
  `...TakeEntityToCargoEx`, Predictive/Local/Server `...TakeEntityAsAttachment(Ex)` -- are
  obsolete with them (`entityai.c:4835-4907`; each names its `...ToTarget...` replacement).
  Vanilla 1.30 calls `PredictiveTakeEntityAsAttachment(Ex)` from nowhere but a
  debug bot; the inventory screen uses the ToTarget forms with the player as the target.
- A frequency proxy finds both 1.30 shapes in `DayZServer_x64.exe` 1.30.164200 at new
  offsets (leaf +0x9382A0, inlined copy +0x9381D7, table +0x16531B0); its self-test came out correct.
- Vanilla noise on a fresh-profile diag client: `CHARACTER LOAD ERROR: Item 'SSM_...'` (the
  menu scene); `set:dayz_gui image:externallink` and `PluginConfigDebugProfile is not
  Registred` were already there on 164078.

## DayZ 1.30: radio and underground, what script can reach (read 2026-10-23, 1.30.164200 scripts)

Read out of `scripts.pbo` while answering "can the radio lose range or crackle underground";
nothing here was measured live yet.

- **Range cannot be changed, only narrowed.** `range` is a CfgVehicles property the engine
  reads itself and Enforce has no ConfigSet. What a server
  script does hold is each transmitter's broadcast byte `+0x13` (`EnableBroadcast`) and receive
  byte `+0x14` (`EnableReceive`), both checked by the VoN router on the server (see "VoN
  routing" above; those offsets were read from the 1.29 server binary on 2026-09-25 and not
  re-read on 164200). So a smaller radius = the server turns reception off on the radios beyond
  it while the transmission lasts; per radio, not per link, so two simultaneous speakers on
  one frequency are heard both or neither.
- **The player's underground state reaches the server.** `PlayerBase.GetUndergroundPresence()`
  returns `EUndergroundPresence` NONE / OUTER / TRANSITIONING / FULL
  (`4_world/classes/undergroundhandlerclient.c`). The client's `UndergroundHandlerClient`
  computes it from the mission's `cfgundergroundtriggers.json` and `SetUnderground` sends it
  as `INPUT_UDT_UNDERGROUND_SYNC`; the server stores it in `OnInputUserDataProcess`
  (`playerbase.c`). Client-reported, and empty wherever the mission defines no triggers:
  stock Chernarus has none, Livonia has its bunker.
- **Depth below the terrain** is the map-independent test: `GetGame().SurfaceY(x, z)` is the
  terrain height (`SurfaceRoadY` is the one that includes roads and objects), so
  `pos[1] < SurfaceY(pos[0], pos[2]) - margin` holds for anything built under the ground,
  for a radio on the ground or in a box as well as one in hands.
- **The radio's own static is swappable, and vanilla 1.30 already does it**:
  `TransmitterBase` plays `SOUND_RADIO_TURNED_ON` as a loop while working and, new in 1.30,
  `SOUND_BUNKER_STATIC_NOISE` instead when the native `IsTunedToBunkerFrequency()` is true,
  restarting the loop from `OnVariablesSynchronized` when that changes
  (`4_world/entities/itembase/transmitterbase.c`).
- **The radio voice volume on a client** is `GetGame().GetSoundScene().SetRadioVolume(vol,
  time)` (`3_game/sound.c`): one level for all radio audio of that client, the option the
  player set is kept in `g_Game.m_volume_radio`; vanilla zeroes it on death and restores it
  from that field (`dayzplayerimplement.c`, `missiongameplay.c`).
- **No script reaches the voice audio itself**: no filter, distortion or per-speaker volume.
- **The bunker broadcast (1.30, the new map Nasdara).** `Bunker : House` buildings carry a
  `DigitalCodeLock_Bunker`; `BunkerBroadcastProxy` objects spawned from the mission's
  `cfgbunkerbroadcast.json` (`proxies[]`, 23 on Nasdara) bind to them. `BunkerBroadcastScheduler`
  picks `numberOfActive` bunkers (3), gives each a code of `lockCodeLength` digits (4), keeps the
  batch `batchLifetimeLength` game minutes (720) and, inside `periods` (17:45-06:00), every
  `reportInterval` game minutes (10) has the native `BunkerBroadcastManager`
  (`3_game/morsecodeconverter.c`) send each bunker's map grid digits and code as Morse
  (`BroadcastTextAsMorse`; dot/dash/gap durations, repetitions from `playbackSettings`). A radio
  for which the native `IsTunedToBunkerFrequency()` is true swaps its static for
  `bunkerbroadcast_staticnoise_SoundSet` (client side, `TransmitterBase.UseBunkerStaticNoise`,
  no check of the enable flag). Off unless `cfggameplay.json` sets `WorldsData` ->
  `bunkerBroadcastEnabled` (default false; only Nasdara's mission does); the config is read from
  `$mission:cfgbunkerbroadcast.json`, else `dz/worlds/<world>/ce/`. Which frequency counts is
  native and NOT read yet; 80.0 MHz, the channel 1.30 put in front of the old eight, is the
  likely one. Unverified consequences for a server with a widened band (a radio mod that adds frequencies): the
  broadcast is out of the band, and an unpatched client, computing frequencies from its own
  nine-entry table, may treat every ninth index as the bunker channel. `BunkerBroadcastManager`
  is also what leaks at shutdown, see the 1.30 section above.
- **How the Morse sounds (read 2026-10-23, no code read):** it is not voice. Three recorded
  samples ship in `nasdara/Addons/sounds_nasdara.pbo` under
  `DZ\sounds_takistan\environment\objects\MorseCode\` -- `MorseCode_Short.ogg` (dot),
  `MorseCode_Long.ogg` (dash), `MorseCode_Notification.ogg`, plus `_V1` variants and
  `MorseCode_StaticNoise.ogg` behind `bunkerbroadcast_staticnoise_SoundSet`. No CfgSoundSet names
  the dot, dash or notification; their names sit in `DayZ_x64.exe`, `DayZDiag_x64.exe` and
  `DayZServer_x64.exe` 1.30.164200 next to `BunkerBroadcastPlayer`, so the engine plays them by
  itself and script only hands over the text and the timing. Most likely the server sends text
  and timing and every client assembles the Morse from the samples on radios tuned to the bunker
  frequency; VoN carries only client microphone audio. Unconfirmed: a stand with
  `disableVoN = 1` and the broadcast on would settle it. Nothing here opens a way to put audio
  into the VoN stream; a mod's own radio sound is a server-chosen event played client-side on
  the radio, which was possible before 1.30 too.
