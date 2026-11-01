# Toolchain: DayZ Tools, packing, signing, testing, server

The Windows-side pipeline. Claude Code, running locally, can execute the
CLI tools directly via PowerShell (BankRev, CfgConvert, DSUtils, FileBank); the user drives
GUI apps (Workbench, the game, Publisher). **With the `dayz` MCP, packing goes through its
`mod_build` (FileBank + DSSignFile).** Addon Builder is the generic path; its CLI
failed headless in the setup measured (see "Local test server" below).

Tools live in `<DayZ Tools>\Bin\` (Steam: `steamapps\common\DayZ Tools\Bin\`):
AddonBuilder, Binarize, BinMake, CeEditor, CfgConvert, DsUtils, ImageToPAA, Launcher,
NavMeshGenerator, ObjectBuilder, PboUtils (BankRev/FileBank), Publisher, TerrainBuilder,
TerrainProcessor, Workbench, WorkDrive.

## One-time setup

1. Steam → DayZ Tools → launch once (registers paths).
2. **Work drive**: DayZ Tools → Settings → assign `P:\` (WorkDrive tool).
3. **Extract Game Data**: DayZ Tools → Tools → Extract Game Data → wait (long). Unpacks
   vanilla data+scripts to `P:\` so Workbench/Addon Builder can reference them.
   (For scripts only, extracting `dta\scripts.pbo` with BankRev is enough.)
4. **Mount P:\ headlessly** (P:\ is a plain `subst` and does NOT survive reboots): resolve
   the work-drive folder from DayZ Tools `settings.ini` (`[ProjectDrive] path`) or registry
   `HKLM\SOFTWARE\WOW6432Node\Bohemia Interactive\DayZ Tools` → `Path`, then
   `subst P: <folder>`; `subst P: /D` unmounts. Restores the environment after a reboot
   without opening DayZ Tools. Junction convention (no admin needed): `mklink /J P:\Mods
   "<DayZ>\!Workshop"` and `mklink /J P:\<ModName> <source dir>` so Addon Builder and the
   engine read a single source of truth.

## Iteration loop (fastest known)

With the `dayz` MCP: sources live in the mod's repository, and
`mod_build` packs them with FileBank and signs them with DSSignFile
(`project_open` -> `mod_lint` -> `mod_build` -> `server_start` -> `log_verdict`). FileBank
does not binarize and does not parse `config.cpp`. The generic loop, for a machine with a
working Addon Builder:

1. Sources at `<work>\MyMod\` (or `P:\MyMod`): `config.cpp` + `scripts\...`.
2. Pack once with **Addon Builder**: source dir `MyMod`, output `<work>\@MyMod\addons\`.
   Options → set "Path to project folder" so paths resolve. Binarize is not needed
   for pure-script mods (configs still get rapified — that's fine).
3. Copy/keep an **unpacked** mirror of the pbo contents next to the game and launch with
   `-filePatching` → edit scripts → restart game only (no repack). Requirements:
   packed pbo exists AND unpacked tree matches the prefix layout AND
   `allowFilePatching = 1;` in serverDZ.cfg.
4. Repack (and later sign) before distributing anything.

### Addon Builder CLI (headless)

The generic headless form (invocation from Agentic-Z build.py). **It failed headless in the
setup measured** ("Error during Steam initialization", 2026-07-31, see below); in such a
setup use `mod_build` or FileBank:

```bat
AddonBuilder.exe <sourceDir> <outputDir> -prefix=<ModName> -temp=<tempDir> -include=<include.lst> [-clear]
```

- `include.lst` = semicolon-separated extension patterns, one line:
  `*.c;*.h;*.hpp;*.cpp;*.bin;*.xml;*.json;*.layout;*.p3d;*.paa;*.edds;*.rvmat;*.imageset;*.png;*.ptc;*.emat`
- `-clear` wipes the output dir first.
- **Stale-PBO gotcha**: AddonBuilder can exit 0 WITHOUT producing a new PBO (nothing
  matched the include list / malformed include.lst), leaving a stale PBO from a prior run.
  After every build verify the output `.pbo` exists AND its mtime is newer than build
  start — else fail loudly.

### What -filePatching actually hot-reloads

| Hot-reloads (restart game only) | Requires PBO rebuild |
|---|---|
| existing `.c` scripts (restart the game) | any `config.cpp` change |
| `.layout` files — re-read on EVERY `CreateWidgets`, no restart; loose tree must live under `<game dir>/<prefix>/`, NOT inside `@Mod` (measured 2026-09-03, gui-layouts.md §23a) | NEW `.c` files (not previously in the PBO) |
| `.paa` textures, `.ogg` sounds | new model/texture references, `model.cfg` changes |

Corollary: "mysterious stale config" during iteration = config.cpp never hot-reloads.

## Local test server (adjust paths)

`serverDZ.cfg` minimum for testing:

```
hostname = "local test";
password = ""; passwordAdmin = "adm";
maxPlayers = 4;
verifySignatures = 0;        // 2 in production!
BattlEye = 0;                // diag build needs BE off
allowFilePatching = 1;
instanceId = 1;              // REQUIRED (verified 1.29 diag): without it the server
                             // self-terminates ("[Server] :: termination in: 10")
                             // with NO error in RPT, right after World-module compile
storageAutoFix = 1;
steamQueryPort = 27016;
class Missions               // the mission template is read ONLY from here, as in the
{                            // stock DayZ Server serverDZ.cfg
    class DayZ
    {
        template = "dayzOffline.chernarusplus";   // mission folder in .\mpmissions
    };
};
```

Verified 2026-07-31 (1.29.163451 diag + official server):
- **The minimal test cfg of that day HUNG the server forever**: boot stalled after the
  World-module compile / CfgVehicles scan, burning one core with stable memory, mission
  never loaded, no error anywhere (reproduced on BOTH DayZDiag -server and
  DayZServer_x64). That cfg had `template = ...;`
  at TOP LEVEL, with no `class Missions` -- and that is the cause. Re-measured 2026-11-01
  (DayZDiag 1.30.164200 `-server`, no mods, eight settings plus the mission): with `template`
  inside `class Missions { class DayZ { ... } }` the server loaded the Mission module and
  initialised the CE about 25 s after start; the same lines with `template` at top level
  stalled after the World module -- no Mission module, no error, for 150 s, as in July; and a
  cfg with no `class Missions` plus `-mission=<absolute path>` stalled the same way, so
  `-mission` does not stand in for the cfg's mission on a server. The template resolves
  against `mpmissions\` beside the EXECUTABLE, not the working directory (`$CurrentDir` in
  the logs is the exe's folder): started from another folder that held its own
  `mpmissions`, the server still loaded `<game>\mpmissions\<template>` and created
  `storage_<instanceId>` in it -- point a test cfg's `instanceId` at a number no stand uses. What worked then and still
  works: start from the shipped `serverDZ.cfg` of the free official "DayZ Server" Steam
  app, editing only what you need (verifySignatures = 0; BattlEye = 0;
  allowFilePatching = 1;); build every stand cfg that way. With it the same
  modded boot reached mission + mod init in ~15 seconds.
- `instanceId` specifically: without it the server self-terminates
  ("[Server] :: termination in: 10") with NO error in RPT.
- Official DayZ Server app (free) ships
  `mpmissions\dayzOffline.{chernarusplus,enoch,sakhal,nasdara}` (nasdara since 1.30) and
  needs no Steam session — best headless test target. The CLIENT install has NO
  `mpmissions\` — for DayZDiag -server, clone BohemiaInteractive/DayZ-Central-Economy (or
  copy the DayZ Server's missions) and junction it
  (`New-Item -ItemType Junction DayZ\mpmissions -Target <dir>`).
- `-mission=<absolute path>` is UNRESOLVED: it failed on 2026-07-31, but that
  test ran with the template-less cfg above, so it proves nothing. The `dayz` MCP never
  passes `-mission`; its stands rely on the `mpmissions\` junction.
- AddonBuilder CLI failed headless in the setup measured ("Error during Steam initialization",
  `[API loaded no]`) even with Steam running — pack with
  `PboUtils\FileBank.exe -dst <outDir> -property prefix=<Prefix> <srcDir>` instead
  (no Steam dependency; fine for script/config mods, no binarization).
- Mission-module compile errors appear ONLY after the mission actually starts — a boot
  that dies pre-mission has NOT yet compiled 5_Mission code.

Launch lines (from the DayZ install dir):

```bat
:: server (diag build shares the client install):
DayZDiag_x64.exe -server -config=serverDZ.cfg -port=2302 -profiles=P:\test-profile ^
  "-mod=<DayZ>\!Workshop\@CF;<work>\@MyMod" -filePatching

:: client:
DayZDiag_x64.exe -connect=127.0.0.1 -port=2302 "-mod=...same list..." -filePatching
```

- Without `-mission` the engine looks for `mpmissions\` next to the binary (which the game
  install lacks -- hence the junction above).
- On 1.30, a hand-started stand that loads a project's own bridge mod (a stand-tool pbo
  that adds a `world_exec` verb to the `dayz` MCP's bridge) needs
  `-serverMod=@DZMCP_Bridge` too (`server_start` passes it when the profile lists
  `@DZMCP_Bridge` under `mods.server_only` in `dayz-mcp.local.toml`).
- Agentic-Z's per-instance pattern passes `-mission` (unresolved, see above):

```bat
DayZDiag_x64.exe -server -config=<inst>\serverDZ.cfg -profiles=<inst>\Profiles ^
  -mission=<inst>\mission -mod=@A;@B -filePatching -port=2302
DayZDiag_x64.exe -profiles=<clientLogs> -mod=... -connect=127.0.0.1 -port=2302 ^
  -filePatching -window -x=1920 -y=1080
```

- Per-instance layout `.server\<instance>\{mission, serverDZ.cfg, Profiles}` keeps test
  missions inside the project (not the game install) and stops variants of one map from
  sharing state. Copy the mission template once from the free DayZ Server install (Steam
  app 223350, uninstallable afterwards) into `.server\<inst>\mission\`. With
  -filePatching, edits to the mission copy (init.c, db\types.xml, cfggameplay.json) go
  live on next launch.
- Client launched with -filePatching against a server missing `allowFilePatching = 1;`
  fails to connect with error `0x00020005` ("The server does not support the client's
  current filePatching setting").
- Dedicated `DayZServer_x64.exe` (separate Steam app 223350) behaves the same; retail
  `DayZ_x64.exe` works for clients but has no diag/debug features.
  (community-reported, unverified): retail `DayZ_x64.exe` AND `DayZServer_x64.exe` may
  block past the loading screen when -filePatching is enabled — only DayZDiag_x64.exe
  works in both roles for filePatching iteration. Test before relying on retail
  binaries + -filePatching.
- Optional, local COT testing only (a stand may use VPPAdminTools instead): COT writes
  per-player permission files only after first spawn — launch → spawn → quit, then add
  `"admin"` to `Roles[]` in `PermissionsFramework\Players\<steamid>.json` and flip
  trailing ` 0` → ` 2` in `Permissions\<steamid>.txt`, relaunch. Never touch
  `Roles\everyone.txt` (grants every connecting player admin).
- `-servermod=` = server-only mods (no client requirement) — admin tools etc. Anything
  adding client-visible items/actions/UI must be in `-mod=` on BOTH sides.
- Logs: `-profiles` dir → `script_*.log` (Print output, compile errors), `*.RPT` (crashes).
  First checkpoint after any script change: server+client boot with **zero script errors**
  in script log.

### The retail client writes NO script lines to its .RPT (verified 2026-08-31, 1.29)

Measured on a live retail client joined to a dedicated stand: the whole
session's `DayZ_x64_*.RPT` contained **zero** `SCRIPT` lines -- engine lines
only (`[Inputs] Loading inputs default ...`, animation errors, the module
list). Every `Print()` a mod made on that client is simply absent, and no
`script_*.log` is produced either.

So on retail you are BLIND client-side. A client-half bug leaves no trace
anywhere, and "nothing in the log" means "no log", not "no problem". Two
consequences worth planning around:

* put the diagnostic on the SERVER side of any client/server exchange -- log
  the arrival of the request, not the departure. A missing arrival line is
  then real evidence rather than silence about silence.
* to read a client's own script output at all, use the diag client
  (`client_compile_check` and the diag stand), where the log receivers are
  compiled in.

### Reading a crash minidump without a debugger (verified 2026-08-31)

The engine's own `crash_*.log` names frames by NEAREST EXPORTED SYMBOL, so a
fault inside one module is routinely printed under a name from another --
`[RtlSizeHeap]: ??? addr:...` for something that is not RtlSizeHeap, and
`Fault address: ... Unknown module` for an address the engine failed to
attribute at all. Do not reason from those names.

The minidump next to it answers properly and needs no debugger installed. Its
`ModuleList` stream is every loaded image with base and size, so the faulting
module is a lookup, not a guess. Struct offsets that matter (x64):

| structure | field | offset |
|---|---|---|
| `MINIDUMP_MODULE` (108 bytes) | base / size / name RVA | 0 / 8 / **20** |
| `MINIDUMP_THREAD` (48 bytes) | id / stack start / stack size / stack RVA | 0 / **24** / 32 / 36 |
| `MINIDUMP_EXCEPTION_STREAM` | code / address / params / context loc | +8 / +24 / +40 / +160 |
| `CONTEXT_AMD64` | Rbx / Rsp / R9 / R15 / Rip | 0x90 / 0x98 / 0xC0 / 0xF0 / 0xF8 |

Getting the module name RVA wrong (16 instead of 20) reads a version field as
a pointer and blows up loudly, which is the good case.

Also read the `Fault code bytes` line in the RPT: it is the actual
instruction. `movzx r9d, word ptr [r9+24h]` preceded by
`xor ecx,eax / xor ecx,[cookie] / test cx,cx / je` is ntdll's HEAP ENTRY
COOKIE CHECK -- i.e. heap corruption, detected during an unrelated
allocation. The crash site then says nothing about the culprit: it is where
the damage was noticed, not where it was done.

### The client build must match the server build (verified 2026-08-31, 1.29)

DayZ ships two clients and they are not interchangeable at connect time:

| stand | binary | client that can join |
|---|---|---|
| dedicated retail (`DayZServer` install) | `DayZServer_x64.exe` | `DayZ_x64.exe` (retail) |
| diag stand (game install) | `DayZDiag_x64.exe` | `DayZDiag_x64.exe` (diag) |

Joining a retail dedicated server with the diag client is refused by the engine,
not by the mod:

```
CONNECTING FAILED (0x00020017)
Client is using Diag exe while the server is not.
```

The trap is what this looks like from a harness. The error is a client-side
modal dialog; the server logs nothing, and any readiness check that waits on a
player count just sits there. The failure therefore presents as "the client
started, the bridge is publishing, and nobody ever joined" -- which sends the
reader to mods, ports and profiles, none of which are wrong.

Consequences worth knowing before choosing a stand:

- A server-side NATIVE patch (a proxy DLL beside the server binary) forces the
  dedicated retail stand, because that is the binary being patched -- and that
  in turn forces a retail client, for the whole session.
- **The retail client must be launched through `DayZ_BE.exe`, whatever the
  server config says about BattlEye.** Measured 2026-08-31: with `BattlEye = 0;`
  in serverDZ.cfg -- so nothing on the SERVER asks for it -- a directly launched
  `DayZ_x64.exe` still loaded the world, was refused at connect with a BattlEye
  message, and never appeared in the server's player count. The same build
  through `DayZ_BE.exe -exe DayZ_x64.exe <args>` joined the same stand. The
  server-side setting and the client-side requirement are separate things, and
  reading the first as governing the second costs a full boot to discover.
- That launcher starts the game as a SEPARATE process and keeps running beside
  it, so automation that records the pid it spawned holds the LAUNCHER's --
  and then has no window to capture, no window to focus, no process to stop and
  the wrong .RPT to judge. Adopt the game instead: snapshot the pids with the
  game's image before launching, and take the one that appears after.
- `-profiles` is honoured by both builds, so key bindings can be inspected. Note
  that `<preset>` blocks in a mod's `inputs.xml` apply to a FRESH profile only:
  an existing profile that predates the mod simply has no binding for its
  inputs, and the key does nothing with nothing said anywhere. Testing a new
  input is easiest under a new `-name=`.

## Workbench script debugging (verified workflow)

Verified on 1.29. The 1.30 Workbench (WorkbenchApp.exe of 2026-10-21) changed its script API
(`Workbench.GetModule/OpenModule/CloseModule` take a typename, new `1_core/workbenchapi/`,
persistence-networking.md "DayZ 1.30 Update 2") -- re-check this workflow and any plugin.

1. `workbenchApp.exe -mod=P:\MyModUnpacked` (Workbench → Options → Source Data = extracted
   game data location on first run).
2. Run `DayZDiag_x64.exe ... -mod=<packed list incl. MyMod>` — Workbench auto-attaches
   (popup bottom-right).
3. Script Editor: F9 breakpoints (red dot; red-circle-! = line never executes),
   yellow arrow = paused, Debug menu = stepping. Print output appears in Workbench console.
4. Packed and unpacked mod contents must be identical or breakpoints won't bind.

Not re-measured on 1.30 (2026-11-01): attaching needs the Workbench GUI, which no
scripted stand run drives.

## If it doesn't work (troubleshooting)

| Error / symptom | Fix |
|---|---|
| `Redeclaration of variable X` | same name declared in sibling if/else-if blocks — declare once before the chain |
| `Serializer ... mismatch` | OnStoreSave/OnStoreLoad write/read order or count differs |
| `Cannot register cfg class` | duplicate CfgPatches class name across PBOs |
| `Addon requires addon X` | requiredAddons entry missing/wrong — names are case-sensitive |
| Mod silently absent, zero script errors | MISSPELLED `requiredAddons` entry silently skips your whole PBO — the only evidence is in the `.RPT`, NOT the script log |
| Wall of `SCRIPT (E):` errors | fix the FIRST one only; the rest usually cascade from it |
| mod.cpp `picture`/`logo` not showing | only `.edds`/`.paa`/`.tga` accepted; PNG/JPG silently ignored |

Log locations: client → `%localappdata%\DayZ\` (`.RPT`, script logs); server → the
`-profiles` dir; admin log `.ADM`; crash dumps `.mdmp`. Extra launch params worth adding
while testing: `-scriptDebug=true`, `-doLogs`, `-adminLog`, `-freezeCheck` (logs script
freezes).

## Editor tooling (catch errors before packing)

- **VSCode extensions**: "EnScriptSyntax" (Risage — autocomplete with inheritance-chain
  detection, DayZ 1.27) or "DayZ Enforce Script" (WalaceWilker — CF-aware); "jm-dayztools"
  (Jacob-Mango, the CF author) integrates DayZ Tools.
- **enforce-script-lsp** (github.com/devz-tools/enforce-script-lsp, GPL-3.0, Rust) — real
  diagnostics/go-to-definition; point its workspace at mod sources + extracted
  game-scripts so completion matches the build you run (1.30.164200 now; its
  `dta\scripts.pbo` header says `version=127662`). Early-stage; builds with cargo.
- **dayz-ce-schema** (rvost, VSCode) — schema validation + classname autocomplete for
  types.xml/events.xml/cfgeconomycore/cfggameplay.json etc. Precedent: publish a JSON
  Schema for a mod's admin configs so admins get the same free validation.
- Mikero's **pboProject** (mikero.bytex.digital, freemium) — strict packer that catches
  errors Addon Builder silently passes; upgrade path when the mod grows.
  **PBO Manager / pboman3** (AGPL, GitHub) — Explorer-integrated pbo GUI.
  **RaG PBO Builder** — one-tool pack+sign+preflight pipeline worth mirroring.
- Optional semantic search: DayZ-n-Chill/Agentic-Z ships a `dayz-rag` MCP server (vector
  search over vanilla scripts/layouts/configs + Bohemia wiki, ~34k chunks). Costs: Python
  required (3.13 works), a Voyage AI API key required for EVERY
  query (never fully offline), ~207 MB prebuilt index on the repo's GitHub releases. A
  BankRev-extract + Grep flow already covers exact-symbol lookup — revisit if
  layout-by-meaning search over vanilla GUI becomes useful.

## Signing (before ANY public/server distribution)

DSUtils (in Tools\Bin\DsUtils):

```bat
DSCreateKey MyKey            :: → MyKey.biprivatekey (SECRET, keep offline) + MyKey.bikey
DSSignFile MyKey.biprivatekey @MyMod\addons\MyMod.pbo   :: → MyMod.pbo.MyKey.bisign
```

Ship `MyKey.bikey` in `@MyMod\keys\`. Server owners copy .bikey into the server's `keys\`
folder; with `verifySignatures = 2` clients without valid signatures are kicked.
Re-sign EVERY pbo after EVERY repack. GUI DSUtils also exists (drag folder, sign all).

## Publishing (Steam Workshop)

DayZ Tools → Publisher: point at `@MyMod` content, fill title/description/preview,
publish; it generates `meta.cpp` with the workshop id. Updates = same tool, same item.
Users then subscribe; server hosts reference `!Workshop\@MyMod`.

**Measured 2026-09-19 -- Publisher has NO command line.** `Publisher.exe` and
`Publisher.Core.dll` carry only WPF dialog strings (UTF-16/ASCII scan); it is a .NET 4.5
GUI over `SteamLayerWrap.dll` (C++/CLI, no exports) + 32-bit `steam_api.dll`, with
`steam_appid.txt` = 221100. The headless path is **ISteamUGC through the RUNNING Steam
client, via the game's own `steam_api64.dll`** (`<DayZ>\steam_api64.dll`, 1069
exports incl. the flat C API; `STEAMUGC_INTERFACE_VERSION017`, accessors
`SteamAPI_SteamUGC_v017` / `SteamAPI_SteamUtils_v010`). From a plain Python process with
env `SteamAppId=221100` + `SteamGameId=221100` (no `steam_appid.txt` needed)
`SteamAPI_Init()` returns true, `ISteamUtils::GetAppID` reads back 221100. Result structs
under Windows `pack(8)`: `CreateItemResult_t` 24 B (`PublishedFileId` @8, legal flag @16),
`SubmitItemUpdateResult_t` 16 B (flag @4, id @8); callback ids 3403 / 3404
(`k_iSteamUGCCallbacks` = 3400). `SetItemTags` gained a 4th arg (`bAllowAdminTags`) after
v017 -- pass `false` always, x64 ABI ignores an extra arg. Create/Submit are async
`SteamAPICall_t`s: poll `ISteamUtils::IsAPICallCompleted` + `GetAPICallResult`, no
callback classes needed. `GetItemUpdateProgress` statuses: 1 preparing config,
2 preparing content, 3 uploading content, 4 uploading preview, 5 committing.
Publisher's `meta.cpp` AS DELIVERED to subscribers (`steamapps\workshop\content\221100\<id>\`,
read 2026-09-19): CRLF line ends and a 4th line `timestamp = <int64>;` (e.g.
`5250938868662592531`, opaque); a committed 3-line LF copy of `meta.cpp` (no timestamp) is
accepted by Publisher for an update, so the 4th line is not needed.
`timestamp` decodes as .NET `DateTime.ToBinary()` with the UTC kind bit -- the moment Publisher
wrote the file (5250938868662592531 -> 2026-09-17T23:37:03Z, 15 s before that item's
`time_updated`). First live MCP update 2026-09-19: an unchanged 0.55 MB mod took 10 s end to
end, Steam moved only the one changed file (per-file dedup), and the public listing's
`time_updated` / `file_size` matched within seconds. In Claude Code's auto permission mode the
publish call is refused by the classifier ("Real-World Transactions") unless
`mcp__dayz__workshop_publish` is in `permissions.allow` of `.claude/settings.local.json`
(works without a session restart, measured 2026-09-19).
`workshop_publish(mod, description=..., tags=[...], content=False)` edits the LISTING only
(no files sent, ~1 s; 13 items done 2026-09-19). Measured: tags appear in the keyless
`GetPublishedFileDetails` API at once, descriptions stay EMPTY there for a long while although
the item's page renders them immediately -- verify descriptions on the page, not the API.
"A long while" has no end in sight: one item's description was still empty in the API eight
days after it was set (2026-10-05). The check that works: fetch
`steamcommunity.com/sharedfiles/filedetails/?id=<id>`, take the text of
`<div class="workshopItemDescription" id="highlightContent">`, and compare it with the BBCode
file's text after dropping markup, ALL whitespace and `https://` on both sides (Steam joins
what the tags separated and spells a link's scheme out). Compare what was sent too: the job's
`spec.json` against the file, byte for byte.
A CHANGE NOTE WITH CYRILLIC killed the uploader before MCP commit 9bf1abc (2026-10-05): its
log line went to a redirected stdout in the ANSI code page (`'charmap' codec can't encode`),
after the content was set and before the submit, so nothing reached Steam and the job failed
in 0.3 s. Descriptions were never affected -- they are not logged.
`SetItemTags` REPLACES the whole tag list; DayZ's allowed Workshop tags (browse page 2026-09-19):
Type `Mod`/`Server`; Mod Type `Animation` `Character` `Economy` `Environment` `Equipment`
`Mechanics` `Sound` `Props` `Terrain` `Vehicle` `Weapon`; Meta `Dependency`/`Collection`;
Required DLC `Badlands`/`Frostline`. Descriptions take Steam BBCode (`[h1]`, `[b]`, `[url=]`).
**A DESCRIPTION IS LIMITED TO 8000 BYTES, NOT CHARACTERS** (measured 2026-09-24 and
2026-09-26). Cyrillic costs two bytes a character in UTF-8, so a bilingual listing hits the
ceiling at roughly 6000 characters: 6398 chars of mixed English and Ukrainian are 8424
bytes, 7514 chars were 9849 bytes, and Steam refuses them with `SubmitItemUpdate:
InvalidParam` -- after the content has already been uploaded -- naming no number and no
field; 7861 bytes went through. Measure `len(text.encode('utf-8'))` before publishing. The
same call with `content=True` succeeded in the same minute, so a failure here does NOT mean
the upload is broken.
The parameter is `description` and takes the TEXT ITSELF, not a path: an unknown argument
like `description_file` is dropped in silence and the job reports success while the listing
is untouched (measured 2026-09-24 -- the files went up, the text did not). Read the job's
`spec.json` when in doubt: it prints what was really sent.
Workshop ids of dependencies: CF 1559212036, VPPAdminTools 1828439124.

The `dayz` MCP does exactly this (2026-09-19): `workshop_publish(mod, changenote, title,
description, preview, previews, visibility, tags, content=True, requires, remove_requires)`
-> job (`previews` = extra screenshot-strip images; `requires` / `remove_requires` = Workshop
ids added to / taken off the item's Required Items; `content=False` = listing only); UPDATE when `@Mod/meta.cpp` carries
`publishedid`, CREATE otherwise (title required, new item **private** by default,
`meta.cpp` written into the folder BEFORE the content goes up, Publisher's own three
lines); refuses an unbuilt folder, a junction inside it, an unreadable `meta.cpp`, a
preview > 1 MB, a running build. `workshop_status(mod)` = folder vs. keyless
`GetPublishedFileDetails` read-back (private items are invisible there) with `stale`.
The uploader runs as its own process (`python -m dayz_mcp.steamugc spec.json result.json`)
so the account shows "in game" only for the upload. Steam client must be running and
logged in; the MCP never sees credentials. SteamCMD `workshop_build_item` would work too
but needs its own password + Steam Guard login -- rejected for that reason.

## Mission-side files (server content config)

Mission = `mpmissions\<template>\`. Relevant to mods:

- `cfgeconomycore.xml` — registers custom CE folders/files (see items-and-processing.md §CE)
  and `<classes>` rootclasses definitions.
- `db\types.xml` (+ modded additions) — loot table entries. Ship your own example
  types.xml (nominal=0) in the mod root so admins opt in — standard practice.
- `cfgspawnabletypes.xml` — spawn presets (attachments/cargo chances).
- `init.c` — mission entry; prefer `modded class MissionServer` in your pbo instead
  (survives mission swaps).
- `cfggameplay.json` — engine-exposed gameplay tuning (schema = `3_Game/CfgGameplayDataJson.c`);
  enable via `enableCfgGameplayFile = 1;` in serverDZ.cfg.
- DayZ Editor Loader (if server runs it) spawns `$mission:\EditorFiles\*.dze` object
  placements at start — the admin path for placing static mod objects.

## PBO inspection & unpacking

- **BankRev.exe** (official, `Tools\Bin\PboUtils`): extracts any normal PBO —
  `& "...\PboUtils\BankRev.exe" -f <outDir> <file.pbo>`. It writes into a SUBFOLDER named
  after the pbo FILE, not into `<outDir>` itself and not after the prefix: `Renamed.pbo` with
  prefix `probe\deep\` unpacked to `<outDir>\Renamed\...` (measured 2026-11-01). Add
  `-prefix` to unpack under the prefix path instead (`<outDir>\probe\deep\...`). Either way it
  also writes `<outDir>\<name>.txt` with the pbo properties (`prefix=probe\deep\`). Refuses
  obfuscated PBOs.
- **CfgConvert.exe**: binarized config.bin → text:
  `& "...\CfgConvert\CfgConvert.exe" -txt -dst out.cpp config.bin`.
- **scripts/ScanPbo.ps1** (bundled with this skill): obfuscation-tolerant scanner —
  parses the PBO header itself, decompresses Cprs entries with the verified
  distance-based LZSS (byte-identical to BankRev), saves entries matching marker
  substrings / config.* names / `\0raP` signature / printable-ratio > 0.83, and writes a
  `_listing.txt` of ALL entries. Entries over 8 MB (stored size) are listed but never
  extracted, not even with `-All`; `-All` saves every other entry. Use when BankRev
  refuses ("Obfuscated PBO") or yields junk (Mikero/KGB/"PBO Tools" packers).
- **Header trick**: the file-name table of any pbo is plaintext at the start of the file —
  parse it up to the empty-name terminator entry and ASCII-decode to map a multi-GB pack
  without extraction. Its size grows with the entry count: `dta\scripts.pbo`'s table is
  ~229 KB (3076 entries, 1.30), so a fixed 64 KB read truncates it.
- **scripts/extract_pbo.py**: portable Python fallback (Python 3.13 works); extracts
  every entry, decoys included, with the same LZSS.

### Judging a model without opening Object Builder

You cannot render a `.p3d` from the CLI, but you can answer the two questions that
usually decide whether a model is usable, and both are cheap:

- **What does it look like?** Extract the `_co.paa` the p3d references and convert it
  with `& "...\Bin\ImageToPAA\ImageToPAA.exe" in.paa out.png` — **ImageToPAA converts
  both directions**, so you can Read the PNG and see the albedo atlas. Damage, rust and
  cracked screens are usually painted into the atlas, so this catches "the vanilla PC
  monitor is smashed" without booting the game. Caveat: when an atlas contains several
  variants of the same part, only the UVs decide which one the mesh shows — the atlas
  tells you a clean variant *exists*, not that this model uses it.
- **Does it have collision?** Dump printable strings from the p3d: named selections
  `component01`, `component02`, … come from the Geometry LOD. No `componentNN` means no
  Geometry LOD, so `physLayer` will not give the object collision no matter what you set
  (verified: `lab_petri_dish.p3d` has none, `PC.p3d` and `radar_panel.p3d` do).
  **Caveat found re-running this check on small, compressed item p3d (a mod project,
  2026-08-07):** a bare "does `componentNN` appear at all" grep gives false positives
  there — `lab_petri_dish.p3d` itself (the file cited above as having *no* Geometry LOD)
  turned up one `component01` hit, the same single-occurrence signal every donor
  candidate in `gear_medical.pbo` produced. Contrast the confirmed-working case:
  `PC.p3d` gives a real multi-component chain (`component01`..`06`, 8 raw hits), not a
  lone match. So for small item p3d, a single `componentNN` hit is not proof either way —
  a real LOD chain (several numbered components) is a much stronger signal than presence
  alone, but for items the strongest check is sidestepping the p3d entirely: decompile the
  owning vanilla config (`CfgConvert.exe -txt` on the PBO's `config.bin`) and confirm the
  candidate's `model=` path belongs to a real, currently-shipping `Inventory_Base`/
  `Edible_Base` class — one players have picked up/carried/dropped for years is stronger
  physics evidence than any string grep. This trick likely still holds for the
  *structural* donors it was verified on (`PC.p3d`, `radar_panel.p3d`) — don't extend the
  bare-presence form of it to item p3d without the config cross-check.
- Textures a model references are also plaintext inside the p3d — grep it for `.paa` /
  `.rvmat` to find which atlas to convert, and for `hiddenSelections` in the *config*
  to know whether you could retexture it at all.
- Steam Workshop metadata without page scraping (pages rate-limit at ~6 hits):
  `POST https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/`
  with `itemcount=N&publishedfileids[i]=<id>` (keyless) → title, description,
  subscriptions, time_updated.

## Production checklist

1. Version bump in mod.cpp/CfgMods + changelog.
2. Repack all PBOs → re-sign all PBOs → verify .bisign names match key.
3. Test on clean profile with `verifySignatures = 2`, BattlEye on, no -filePatching.
4. Push to Workshop; server host updates mod + copies new .bikey if key rotated.
5. Keep `.biprivatekey` out of any shared folder/repo — leaked key = impersonation.

## Window-capture screenshots can miss short-lived HUD overlays

Measured live 2026-08-30 (a mod's toast, diag client, windowed): a script-driven HUD
pane that the engine reported as shown for a full 8 s (state-change log both sides) and
that the human at the monitor SAW was absent from every window capture taken inside
that interval (PrintWindow-style capture; timestamps correlated via the script log's
creation time ≈ `GetGame().GetTime()` zero). Persistent HUD widgets (minimap, vanilla
HUD) capture fine in the same shots. Verify transient overlays by engine-side logging
(arm/show/hide transitions) or human eyes — not by window captures.

## Loose prefix folders in the game root leak into a retail client (measured 2026-09-09)

A folder in the game directory named like a pbo prefix (`DayZ\MyMod_PDA` -- a junction to a
source tree, placed there for `-filePatching`) is consulted by the RETAIL client even when it
runs WITHOUT `-filePatching`: the client compiled the Workshop pbo's old `MyMod_PdaModule.c` (its
line 1235 still named `MyMod_RadiationReading`) yet could not see the type, whose file
`MyMod_PdaRadiation.c` had just been deleted from the loose tree -- so the file LIST came from the
folder and the contents from the pbo. Any drift between the loose tree and the pbo a player
loads ends in "Can't compile World script module" on that player's machine. Rule: no prefix
junctions in the game root while anyone plays with Workshop copies; create them for a
file-patched run and remove them after (the `dayz` MCP's `mod_build` recreates them on demand).

## Server performance: one instance is one core (measured 2026-09-22)

Sampled `TotalProcessorTime` of every thread of a retail `DayZServer_x64.exe` twice,
30 s apart, on a hybrid-core desktop CPU (8 P + 8 E cores / 24 logical, 64 GB). Mission
`dayzOffline.chernarusplus`, mods CF + VPP + a full mod stack, **zero players**
(a mod's meter printed `players=0` for the previous hour), CE in idle mode
(`IdleModeStartup=1`), uptime 3.1 h:

| | |
|---|---|
| process CPU in a 30 010 ms window | 29 766 ms = **1.00 core** |
| hottest of its 34 threads | 28 172 ms = **93.9 % of one core** |
| next hottest | 656 ms = 2.2 % of a core (11 threads used any CPU) |
| memory | 1.13 GB working set, **5.42 GB private** |
| sustained | 11 253 s CPU over 11 220 s uptime — 1.00 core, flat |

So the simulation is a single thread, and an instance occupies a core **whether or not
anyone is playing**. Capacity planning is `instances <= cores`, not `instances <= load`.
Nothing is pinned by default (affinity `0xFFFFFF`, priority Normal), so on a hybrid-core
CPU the 100 %-busy thread can be parked on an E-core — pin live instances to P-cores.

What the binary actually supports (string table of `DayZServer_x64.exe`, same date):

- CLI: `limitFPS`, `cpucount`, `instance`, `filePatching`, `bandwidthAlg`, `publicIP`,
  `publicAddress`, `pid`. **No `exThreads`** — that is an Arma-era parameter.
- serverDZ.cfg: `multithreadedReplication`, **`entitySimulationMultiThreaded`**
  (the binary says `multithreadedAnimations` is deprecated in its favour — it appears in
  no public config template), `entitySimulationPerformingCatchup`, `simulatedPlayersBatch`,
  `networkRangeClose/Near/Far/DistantEffect`, `speedhackDetection`, `shotValidation`,
  `motdInterval`, `loginQueueConcurrentPlayers/MaxPlayers`.
- `dayzsetting.xml` in the server folder carries the Enfusion job system
  (`<jobsystem globalqueue= threadqueue=><pc maxcores= reservedcores=/>`); community docs
  say `multithreadedReplication`'s worker count derives from those two numbers. The
  shipped value in the measured install is `maxcores="2" reservedcores="1"`.
- The tick target is a fixed 30 Hz; 25-30 is healthy, 15-25 players feel desync, below 15
  is unplayable.

Server FPS is readable from script: `proto native float GetFps()` on `CGame`
(`3_game/global/game.c:867`, 1.30.164200), plus `World.GetServerFpsWarningThreshold()` /
`GetServerFpsCriticalThreshold()` (`3_game/global/world.c:202,207`). Log it next to the
player count for a permanent per-instance health number.

Cross-instance play (moving a character between servers) has no engine support: there is
no redirect/migrate proto. The only working shape is snapshot → RPC with the target
address → `g_Game.Connect(menu, ip, port, password)` from the main menu after
`DisconnectSessionForce`.

### binarize.exe walks the whole DRIVE of its working directory (measured 2026-10-05, rev.163709)

Before it prints `Binarize started`, binarize enumerates every directory of the drive its
working directory is on, from the drive's root down. Seen in the live process: one busy
thread in `FindNextFileW` / `NtQueryDirectoryFile`, a handle open on the drive's root and one on every
level of whatever folder it had reached (a scratch tree of unpacked mods at that moment),
about 57,000 directory operations a second, nearly all kernel time, 15 MB of memory, nothing
read. So the run time is the size of the DISK, not of the model:

- from a repository on a large data drive (`<repo>\...\build\model-root`, through
  `asset_build` as it was then): 510 s, 522 s, 540 s and 538 s for four models of 332 to
  3716 triangles;
- the same models and a fifth, same root reached as `subst <letter>: <root>`, working
  directory `<letter>:\`, source named on the letter, output left on the big drive: 0.0 to 0.1 s
  each, repeated a dozen times, ODOLs of the same size to within three bytes (rebuilds are
  never byte-identical, and one rebuild in five came out 230 bytes larger with the same
  LODs and the same strings);
- another project's small model, one run after the other: 650.8 s from its folder on
  the big drive, 0.1 s from a letter -- same size, same verdict (C1, C3, C4 pass);
- the very first run from a letter took 28 s. No later one did, the old script included.
  Not explained; do not quote 28 s as the cost of a build.

The walk goes through junctions: with the whole unpacked `dz` (103,359 files) junctioned
into the root a run from a letter took 0.5 s instead of 0.1 s. From a folder on the big
drive the same difference drowns (510 s with all of `dz` linked in, 522 s with the one
folder the models name). Two binarize processes at once did not disturb each other (a
letter run finished in 0.1 s while another was eight minutes into its walk of the big drive).

On 2026-09-26 two attempts were given up after 10 and 12 minutes and recorded here as
"never finishes". That was the same walk on a fuller disk, cut short -- not a hang.

**The `dayz` MCP does the substitution itself since 2026-10-05** (`asset_build`, module
`assets/drive.py`, commit e9355de): it maps a free letter (Z downwards, never P) onto
`build.project_root` for the run, starts binarize from it and removes the letter when the
process is gone. `asset-build.json` carries `cwd`, the directory the tool really ran from.
Every run maps a letter of its own and removes only its own, so a build killed together
with the server leaves its letter until the next logon (`subst Z: /D`). With no free letter
the build runs from the folder as before and says so in the job summary. A server that was
already running keeps the old code until it is restarted.

What follows for anything that starts binarize by hand: give it a root on a SMALL drive --
the project drive DayZ Tools assume (`P:`), or a letter substituted for the run -- never a
folder deep in a big disk, and link into that root only what the models name. A tool that
wants the letter has to pass it through untouched: Python's `Path.resolve()` turns a
substituted path back into the real one (measured: `Path("<letter>:\\").resolve()` gave the
folder). Whether a letter is free is a question for `GetLogicalDrives`, not for
`os.path.exists` -- a card reader without a card holds a letter no path exists on -- and
`subst` has the last word: exit code 1 with `Drive already SUBSTed` for a substituted
letter, `Invalid parameter - C:` for a real drive, `Path not found` for a missing folder
(measured). From PowerShell, hand `Start-Process` the arguments as ONE string with its own
quotes;
an array element with a space ("DayZ Tools") is not quoted and splits `-binpath` in two,
and binarize then exits at once having built nothing. Wait on the process handle with a
ceiling, never on end-of-stream. An MLOD is still worth checking with a reader first (the
Arma 3 Object Builder add-on's `a3ob.import_p3d`, or a small MLOD reader script), since a
run costs minutes when the root is not small.
A root whose prefix folder is itself a junction is refused by `asset_build` ("the source is
not inside the project root"): the containment check resolves the link. Junctions deeper
down (`data`, the vanilla folder) are fine.

## Workshop description limit is 8000 BYTES of UTF-8

Merged into "Publishing (Steam Workshop)" above (measurements of 2026-09-24 and 2026-09-26).

## Radio frequency proxy and the Linux server -- measured on 1.29.163709

**Build note.** Everything from here to the end of this file was measured on DayZ
1.29.163709 (2026-10-05). 1.30 changed the frequency lookup: nine vanilla channels instead of
eight, a new leaf shape and new offsets, and the Windows proxy needed a second shape -- see
persistence-networking.md, "DayZ 1.30 Experimental 1.30.164014: the radio frequency lookup
changed" and "DayZ 1.30 Update 2, 1.30.164200". The byte patterns, offsets and "vanilla gives
8" below are 1.29 values. A Linux stand built from the 1.29 server still holds 1.29 content;
rebuilding it from a 1.30 Windows server would pair 1.30 content with the 1.29 Linux binary,
which the same-build rule below forbids.

### A proxy DLL beside the Windows server under Wine (measured 2026-10-05, Wine 9.0, Ubuntu 24.04 WSL2)

A native `hid.dll` placed beside `DayZServer_x64.exe` (the server imports HID.DLL statically) is
loaded by Wine 9.0 WITHOUT any override: `WINEDEBUG=+loaddll` shows `Z:\...\HID.DLL ... native`,
then `C:\Windows\System32\hid.dll ... builtin` for the export forwards (linker forwarders with an
absolute System32 path resolve in Wine's loader). `WINEDLLOVERRIDES="hid=n,b"` gives the same;
`hid=n` loads the proxy but refuses the builtin behind the forwards (`err:module:
find_forwarded_export module not found for forward 'C:\Windows\System32\hid.…'`). The proxy's own
Win32 use (CreateFileW, GetCommandLineW, VirtualProtect on the host image, a byte-pattern scan of
the exe's .text) behaves as on Windows. Whether the default prefers the application's copy is a
per-DLL property in Wine (the builtin's prefer-native flag), so other DLL names (winhttp, dinput8,
version) can still need the explicit `n,b`. Wine comes from `apt-get install --no-install-recommends
wine wine64`; the 64-bit server runs with the wine32 warning ignored.
The same test through Proton (GE-Proton11-7, Wine 11.0 Staging, 2026-10-05), run outside Steam as
`STEAM_COMPAT_DATA_PATH=<dir> STEAM_COMPAT_CLIENT_INSTALL_PATH=<dir> python3 <proton>/proton run
DayZServer_x64.exe ...`: identical answers. No override and `hid=n,b` load the proxy as native and
the System32 builtin behind it; `hid=n` additionally breaks Proton's own bridge
(`lsteamclient.dll` -> `XINPUT1_3.dll` -> `hid.dll` not found). GE-Proton unpacks into
`GE-Proton11-7-x86_64/`, its binary is `files/bin/wine` (no `wine64`), and a pre-set `WINEDEBUG` /
`WINEDLLOVERRIDES` in the environment is passed through by the launcher.

### Redirecting a tiny engine function: do not clobber registers (measured 2026-10-05)

A leaf of three instructions is transparent to the compiler that built its callers: GCC at -O2
(`-fipa-ra`) keeps caller values in every register the leaf does not write, across the call. A
detour of the usual shape, `mov rax, imm64; jmp rax` into a C function, clobbers rax and then
whatever the calling convention allows. Measured on a stand-in for the DayZ frequency lookup built
with `gcc -O2 -no-pie -fno-pic`: the patch applied, nothing crashed, and every result printed as
0.000 because the caller had `eax = 1` (printf's vector-argument count) set BEFORE the call. The
robust form: a 5-byte `E9 rel32` at the site (no register touched) to a relay page mapped within
2 GiB (`mmap` with `MAP_FIXED_NOREPLACE` at 64 KiB steps around the site) holding
`FF 25 00000000 <abs64>`, entering an assembly stub that saves flags, rax rcx rdx rsi rdi r8-r11
and xmm1-xmm15, calls the C code, restores, and returns with only xmm0 changed. A Linux preload
library does this and its test checks all 24 registers. The Windows DLL
still uses the 12-byte form and worked on 1.29: MSVC callers were not observed to
rely on volatile registers across that call (1.30 needed a second code shape, see the build note
above). Also: build a preload library without `dladdr` (find your own
path with `dl_iterate_phdr`) and the newest glibc symbol it needs stays at 2.14.

### The native Linux server (measured 2026-10-05: `DayZServer` 1.29.163709, Ubuntu 24.04 under WSL2)

**Getting it.** App 223350 has three depots: 223351 shared content (addons, dta, keys, missions,
`battleye/` with BOTH `BEServer_x64.dll` and `beserver_x64.so`; 3.6 GB), 223353 Windows binaries,
223352 Linux binaries -- three files, 78 MB: `DayZServer`, `libsteam_api.so`, `steamclient.so`.
Anonymous steamcmd answers `missing license for depot`; an account that owns the game downloads
it from the Steam client console (`steam://open/console`, then `download_depot 223350 223352`)
into `steamapps\content\app_223350\depot_223352`. A working Linux server = those three files +
the content folders of a Windows server OF THE SAME BUILD (`addons dta keys battleye sakhal
mpmissions dayzsetting.xml steam_appid.txt`) copied onto a Linux filesystem.

**The binary.** ELF `ET_EXEC` (not PIE) at 0x400000, built with GCC 7.5.0 on Ubuntu 18.04 (its
`.comment`). One `R E` segment holds .text AND .rodata (0x400000..0x24AE000), one RW segment
starts at 0x26AD000, with a 2 MiB hole between them; the heap sits far above at a random address.
Script defines on Linux: `SERVER_FOR_WINDOWS, SERVER, PLATFORM_LINUX, NO_GUI, NO_GUI_INGAME` --
`SERVER_FOR_WINDOWS` is set on the Linux server too (presumably it names the clients' platform),
so a platform check has to use `PLATFORM_LINUX`.

**Launch and boot.** `./DayZServer -config=serverDZ.cfg -port=2402 -profiles=profiles
"-mod=@CF;@MyMod_Core;@MyMod_Radio" -dologs -adminlog -freezecheck`. With `BattlEye = 1`
and `verifySignatures = 2`: `BattlEye Server: Initialized (v1.220, DayZ 1.29.163709)`, `SUCCESS:
SteamGameServer_Init(...)`, the mods' ready lines 22 s after start, `Mission read.` at 40 s (fresh
Chernarus, empty storage), rss about 5 GB, 31 threads, UDP on the port, port+2 and steamQueryPort.
Mixed-case mod folders and pbo names (`@MyMod_Radio/addons/MyMod_Radio.pbo`) load from ext4
as they are. SIGINT in the main loop is a clean stop in 12-19 s (`--- Termination successfully
completed ---`, exit status 255); a SIGINT sent during start-up (7 s in) is ignored and NOT
remembered, so a supervisor has to send it again or kill. Without game data the bare binary prints
`No entry '.CfgVoiceTypes'.` and segfaults (it then tries to run `./CrashReporter`, which the
depot does not ship).

**Script paths.** `$profile:MyMod\\X.json` (backslashes) lands in `<profiles>/MyMod/X.json`:
paths given by script are translated, case is kept as written, and under umask 022 files come
out 0640 and directories 0755. The engine's own input export is NOT translated: it writes a file literally
named `profiles/users/Survivor\Server.core.xml` and fails to parse it on every later boot
(`Cannot read inputs cfg ... parser error`). That is vanilla noise, as is `ANIMATION (E): Can't
load @Mod/Anims/cfg/skeletons.anim.xml` for every mod.

**Preloading a library.** `LD_PRELOAD=/abs/path/lib.so ./DayZServer ...` works with BattlEye on.
Set it on the server's own command line rather than exporting it: every process started with it
in the environment loads the library too (`timeout`, `env`, the launching shell), so a preload
library needs a gate on `/proc/self/exe`. The frequency lookup on Linux is at +0x15BA5F0:
`48 8D 05 rel32` (lea rax,[rip+table]), `83 E6 07` (and esi,7), `F3 0F 10 04 B0` (movss
xmm0,[rax+rsi*4]), `C3`; table at +0x1FCAD00. The mask comes AFTER the lea, the reverse of the
Windows build (`8B C2 48 8D 0D rel32 83 E0 07 F3 0F 10 04 81 C3`), and it is the only place in
the executable segment whose four bytes address the table, relative or absolute. A preload library
that finds it through the table instead of through bytes was right on first contact. `mprotect` to RW and back to RX on the
server's text works (the page becomes a private copy, visible as its own line in
`/proc/<pid>/maps`), and the mod's probe then reads 400 frequencies where vanilla gives 8 (1.29;
1.30 has 9). Cost of
the register-saving stub: two million `SetFrequencyByIndex` calls take 82 ns each patched against
52 ns vanilla; `GetTunedFrequency` is 32 ns either way (cached, it never reaches the lookup).

**A WSL stand.** The Linux server runs from a Linux filesystem (inside the WSL distro), with
content copied from a Windows server of the same build, minus its storage. WSL in NAT mode keeps
its ports apart from a Windows stand's, so the two never meet. A boot script that starts the
server with or without the library, waits for the mod's ready line in a script log born in that
boot, holds for a set time, stops it and prints both logs makes the comparison repeatable.
From Git Bash prefix a `wsl.exe` call with `MSYS_NO_PATHCONV=1`. A full twelve-folder stack
(CF, VPPAdminTools, a core mod, feature mods and their glue pbos) booted this way in a profile
of its own on 2026-10-05 with 0 script errors; VPP writes its own profile tree. A bare folder
(binary + library only, no game data) is enough for a two-second self-test of the library, if
the library carries one.
