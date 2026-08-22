# dayz-agentic-modding-mcp

An MCP server that lets an agent build a DayZ mod, check that the client compiles,
run a test server and get a structured verdict instead of a log.

The server does the work itself: it calls FileBank, the signing tool and the
diagnostic executable directly. A project does not need its own build script.

## Why a profile

The server knows nothing about any particular mod. Everything specific lives in a
two-part profile:

* `dayz-mcp.toml` — portable, committed to the mod repository: which mods to pack,
  what a healthy boot looks like.
* `dayz-mcp.local.toml` — machine-specific, never committed: where the game, the
  tools and the test stand live.

Mixing the halves is rejected on load: that is how a repository stops building on
anyone else's machine. Start from `dayz-mcp.example.toml`.

A mod is declared once, by name: sources in `<root>/Name` by default, output
`@Name/addons/Name.pbo`. `build.sources` can redirect a mod's source elsewhere
(e.g. `"."` for a mod whose `config.cpp` sits at the repository root itself).

### The portable half — `dayz-mcp.toml`

| Key | Meaning |
|---|---|
| `project.name` | what to call this project |
| `build.mods` | one entry per mod, by name; source, pbo and `@folder` all follow from it |
| `build.sources` | where a mod's source really lives, relative to the profile (`"."` = the repository root itself) |
| `build.exclude` | what must never be packed. Listing it **replaces** the default wholesale; the default is `.git`, `*.blend`, `*.blend1`, `.gitignore`, `.gitattributes`, `README.md`, `*.ps1` |
| `build.stage` | pack a filtered copy instead of refusing when something excluded is present — the layout a root-layout mod needs |
| `build.project_root` | the directory every model path resolves against, relative to this file. It must **contain** the mod's prefix folder. Required by the model tools and by nothing else — see "The asset pipeline" |
| `build.pre_script` | a PowerShell script run before packing, for projects that generate code first |
| `expect.ready_line` | the line the mod prints when it has finished loading — see below |
| `expect.counters` | `key = value` pairs carried by that line, compared numerically |
| `expect.max_warnings` | warning budget; omit the key to disable the check |
| `expect.forbid` | substrings that make a run bad regardless of anything else |
| `expect.error_regex` | regexes marking script errors that belong to you, used by the client compile check |
| `expect.noise` | extra engine noise to ignore, on top of the built-in list |

### The machine half — `dayz-mcp.local.toml`, never committed

| Key | Meaning |
|---|---|
| `machine.game` | the game installation; discovered automatically if absent |
| `machine.tools` | DayZ Tools; discovered automatically if absent |
| `machine.blender` | the Blender **executable**, for `asset_export` only; discovered automatically if absent, and needed by nothing else |
| `machine.stand_root` | the prepared test stand. The server boots against it and its logs are read from `<stand_root>/profiles`. Defaults to `<root>/testenv` |
| `machine.config` | the server config filename inside the stand (default `serverDZ.cfg`). It is a setting because a stand can hold a config that hangs forever after world-compile and a working one under another name; it must resolve inside `stand_root` |
| `machine.port` | the port `server_start` passes to the server (default `2302`) |
| `mods.required` | mod folder names, resolved under the game's own `!Workshop` folder — `required = ["@CF"]` means `<game>/!Workshop/@CF` |
| `mods.extra` | full paths to anything else to load, for mods that do not live in `!Workshop` |
| `mods.server_only` | folder names to route to `-serverMod` instead of `-mod`. Matching is by folder name against every mod being loaded, wherever it came from — `mods.required`, `mods.extra` or the project's own `@Name` folders; everything not listed goes to `-mod`. The diagnostic client never loads server-only mods, so the client compile check drops them |

### The ready line

`server_start` finishes when `expect.ready_line` appears in a log written after
that boot began. It is the one thing the server cannot work out for itself, and
without it two things change: nothing can be waited for, so the boot job starts
the server, confirms it is still alive a moment later and finishes saying so;
and `log_verdict` has no line to read counters off, so `expect.counters` never
matches anything. Errors, crashes and the warning budget are still judged. A
profile without a ready line is supported, not broken — `project_open` says so
in its notes.

## Tools

| Tool | What it does |
|---|---|
| `project_open(path)` | read the profile, discover the game and tools, report what is missing |
| `project_status()` | current project, running server, recent jobs |
| `mod_build()` | pack and sign every declared mod; returns a job id. Refuses a second build of the same project while one is still running |
| `server_start(timeout)` | start the test server, finish when it is ready. Returns the `pid` straight away — the process is spawned before the call returns, so the very next tool already sees a running server. Refuses if the game port is already held by someone else, and refuses on the spot if the image cannot be launched. Readiness comes from `expect.ready_line` when declared, otherwise from the server binding its port; the job summary names which |
| `server_status(pulse_seconds)` | pid, whether the process is alive, whether the log is growing (sampled `pulse_seconds` apart), and how long it has been stalled |
| `server_stop(pid)` | stop the server this session started (optional pid for orphaned servers) |
| `client_compile_check(extra_mods, wait_seconds)` | run the diagnostic client and read its logs |
| `log_verdict(source, since)` | pass/fail with reasons: counters, forbidden strings, warning budget. `source` is `"server"` (the newest log in the stand) or `"client"` (the log the latest `client_compile_check` produced). `since` (an epoch timestamp, e.g. the `since` `server_start` returns) refuses a log written before the run being judged, so a stale log from an earlier boot cannot be mistaken for this one's result |
| `log_tail(source, pattern, n)` | last lines, optionally filtered; same two sources |
| `job_status(job_id)` | status of a long-running job |
| `job_wait(job_id, timeout)` | wait for a job to finish |
| `job_artifacts(job_id)` | retrieve outputs from a completed job |
| `bridge_build()` | pack the bridge mod, whose sources ship with this server (`bridge/`), not with your project; returns a job id. Built **unsigned** — see below |
| `bridge_status(window)` | is the bridge inside the running game still ticking: reports the tick number and whether it advanced over `window` seconds. Succeeds only for a tick that actually moved, or for a stand that restarted mid-sample |
| `bridge_clear(force, probe_window)` | discard the command stuck in the mailbox, naming what it threw away. Refuses while the bridge looks alive unless `force=True` |
| `world_ready(timeout)` | wait until the bridge inside the game is actually claiming commands. Call it once after the boot job finishes, before the first world command — see "server ready is not bridge ready" below |
| `world_state(class_name, radius, pos)` | snapshot of the world from the bridge's once-a-second publish: players, position, health, hands. Free with no arguments; with `class_name` it also counts objects of that class nearby (one command round trip) |
| `world_spawn(class_name, where, pos, quantity)` | create an item on the ground (with no lifetime, so it cannot vanish mid-check), in the player's hands, or in their inventory |
| `world_teleport(pos)` | move the player to `"x y z"` — the same format `world_state` reports, so a read position can be handed straight back |
| `world_set(what, value, target)` | set `health` (player or held item) or `quantity` (held item) |
| `world_delete(class_name, radius, pos)` | delete objects of one class nearby. Requires the class; never deletes a real player |
| `world_entities(class_name, radius, pos, limit)` | **which** objects are nearby, not how many: class, position, distance and health for each. A page, and it says so — the true total comes back beside the list |
| `world_time_set(hour, minute, day, month, year)` | move the world clock. Every field left at -1 keeps its current value, read back from the engine first, because the engine sets a date as five numbers at once |
| `world_weather_set(what, value, seconds, duration)` | move overcast, rain, fog, snowfall or wind. A **nudge, not a lock**: the engine keeps simulating weather afterwards, and both the tool and the mod say so |
| `world_action(action_class, target_class, subject, radius, pos)` | run a mod's own action through the engine's gate — see below |
| `world_exec(verb, args)` | the escape hatch: an arbitrary verb through the same transport, marked non-standard in every answer |
| `client_start(timeout, extra_args)` | start the game client and connect it to the stand; returns a job id. Always windowed. Finishes when the bridge reports `players >= 1` — a count, not a timer |
| `client_status()` | pid, window geometry, whether the window is minimized or in front, the background setting, the player count, and whether a virtual controller is attached |
| `client_stop()` | stop the client this session started, and unplug the virtual controller |
| `client_shot(path)` | capture the client's window to a PNG, with `lit_fraction` — the number that tells a real frame from an all-black one. No focus needed |
| `client_move(x, y, seconds)` | walk the character with the left stick. Analog, and the only tract that moves the character at all. No focus needed |
| `client_look(x, y, seconds)` | turn the camera with the right stick. No focus needed |
| `client_press(button, seconds)` | one gamepad button, from a closed table of fourteen names. No focus needed |
| `client_chat(text, color)` | put a line in chat — delivered **server-side by the bridge**, so no keyboard, no window, no focus |
| `client_type(text, submit)` | type into a client-side input field with real keystrokes. **The only tool here that takes the foreground**, and it says so in its answer |
| `client_verdict(since)` | judge the live client by its own `.RPT` — an errors-and-crashes verdict; see below |
| `ui_menu()` | what the client's interface is doing: open menu class, cursor, dialog. Free — republished every tick |
| `ui_tree(root, depth, limit)` | the client's **widget tree**: path, class, name, visibility, screen rectangle, depth and text. A page, and it says so |
| `ui_find(name, class_name, text, root)` | the same walk, filtered in the client so the whole tree never has to travel |
| `ui_click(path, expect_name, expect_class, via)` | press a widget. `via="script"` goes through the open menu's handler with no focus; `via="cursor"` puts the real mouse on its rectangle |
| `ui_text(path, text, expect_name)` | write into an edit box, and read the value back out of the widget |
| `mod_lint(mod, strict)` | judge the Enforce Script without packing or booting anything. `mod_build` runs it first and refuses on what it refuses |
| `knowledge_build(layer, full, only)` | build or refresh a layer of the API index; returns a job id. `only=[path]` re-reads exactly the files you name |
| `knowledge_status()` | what each layer holds, how old it is, and whether it still matches what is on disk |
| `knowledge_find(name, kind, owner, layer, prefix, limit)` | find a class, method, constant, enum or config class by name |
| `knowledge_show(name, ..., body)` | one declaration in full: signature, members, inheritance chain, and the source itself — read straight out of an archive if that is where it lives |
| `knowledge_overrides(name, owner, layer)` | who overrides this class or method |
| `knowledge_callers(name, kind, owner, layer)` | who **calls** this method or builds this class — every call site, with the class and method that made it |
| `asset_export(blend, mod, source, name)` | export a model out of a `.blend` into `build.project_root`, headless; returns a job id. The **optional** first step — see below |
| `asset_build(mod, source, deploy)` | binarize a mod's models from their MLOD sources, judge what came out, and only then put it in the mod; returns a job id |
| `asset_check(mod, model)` | judge the models and textures a mod already ships. Builds nothing, needs no DayZ Tools, answers in milliseconds |
| `asset_convert(source, output)` | convert one texture between `.png` and `.paa`, and judge the result |

**Three limits the engine imposes on the UI tools, none of them worked around:**
a plain `TextWidget` has `SetText` and **no** `GetText` anywhere in `enwidgets.c`, so a
label's string cannot be read at all — what a mod's interface MEANS stays a question for
the server-side bridge, where the data is real. A script-level click reaches only the open
scripted menu, because `Widget` has `SetHandler` and no `GetHandler`; `via="cursor"` is
there for everything else. And the client has to load the bridge: one pbo carries both
halves, so a profile listing it under `mods.server_only` keeps it off the client's `-mod`
line — that case is refused by name rather than answered with an empty tree.

`job_wait` is the tool meant to wait, and its `timeout` is capped at **600
seconds** however large a value is passed. Two other tools sleep: `server_status`
samples the log twice, `pulse_seconds` apart, capped at 10 seconds — that pause
is how it tells a slow boot from a hung one — and `bridge_status` samples the
bridge's tick twice, `window` apart, capped at the same 10 seconds, for the same
reason. Everything else returns immediately; work that takes minutes happens
behind a job id.

### The bridge mod

`bridge_build` packs `bridge/` **from this server's own repository** into
`@DZMCP_Bridge` beside it. It is the server's mod, not yours: one copy serves
every project, nothing is written into your repository except the job record,
and no project's signing key is used — a `-serverMod` pbo is never handed to a
client to verify, so it is built **unsigned** and its output folder is kept free
of signatures and keys.

Building it does not load it. That stays your profile's decision, because the
bridge is an extra pbo in the stand and a run without it has to remain possible.
To attach it, add two lines to `dayz-mcp.local.toml` (the same two
`bridge_build`'s job summary prints):

```toml
[mods]
extra       = ["<path printed by bridge_build>/@DZMCP_Bridge"]
server_only = ["@DZMCP_Bridge"]
```

`server_only` is what routes it to `-serverMod` instead of `-mod`. Without it
the stand boots perfectly well and `bridge_status` reports that the bridge never
wrote any state — which is true, and easy to mistake for a broken bridge.

`bridge_status` also reports the command mailbox. Inside the game only the mod
empties it, by claiming the command; on this side `bridge_clear` and
`server_start`'s pre-boot clearing do. So a command sent while the stand was
down, or before the bridge was attached, is not discarded and does not expire on
its own — it keeps blocking
every later send, and a stand booted outside these tools would pick it up.
`server_start` clears both transport files before every boot, so a server
started through this tool never runs a command from a previous session; that is
hygiene, not a substitute for knowing the command is there. The state comes back
as `stale_command`, and
`bridge_clear()` is the way out of it. Clearing is a separate tool on purpose:
throwing away a queued command is a decision, not something a status check
should do behind your back. It refuses while the bridge looks alive unless you
pass `force=True`, and either way it reports the command id it discarded.

### What `bridge_status` can tell apart

The tick alone is not enough to judge a bridge, because it restarts at 0 every
boot while the state file survives in the profile directory. Every answer
carries the channel's own verdict in `heartbeat`, and the four are genuinely
different facts:

| `state` | `heartbeat` | meaning |
|---|---|---|
| `alive` | `growing` | the tick moved within one session — the only `ok: true` liveness answer |
| `restarted` | `restarted` | a new world came up between the two samples: alive, **not** frozen, and anything sent to the old session is gone |
| `frozen` | `stalled` | the same world seen twice, not moving — a script-side problem, so `log_verdict` is the next step |
| `unknown` | `unmeasurable` | a sample could not be read (or `window=0`): no comparison was made. **Not** a diagnosis — call again |

Every answer that read a sample also carries `session_id` — the live world's
id — and a `restarted` answer carries `previous_session_id` too, so a caller can
say which world went away.

`no_server`, `stale_command`, `no_state_file`, `invalid_state`,
`unreadable_state` and `outdated_bridge` come before any of that: nothing is
running, a command is wedged, the mod is not loaded, the state document is valid
JSON with a named field wrong (it says which, and checks twice before saying it),
the file never parses at all, or it parses but predates this server's protocol
(rebuild it with `bridge_build`).

### The world commands

The world tools talk to the bridge over two JSON files in the server's
`-profiles` directory: a command mailbox (written atomically from this side,
deleted by the mod as its claim) and a state file the mod overwrites once a
second. Enforce Script has no rename, so the mod cannot write atomically —
the reader tolerates torn writes instead, and one failed read is never news.
Four facts, all measured on a live stand, decide how to use them:

**Server ready is not bridge ready.** The bridge starts claiming commands
tens of seconds *after* the server reports ready — the spread observed so far
is 18–38 seconds, and it varies boot to boot.
A command sent into that window is not rejected — it is claimed late and
completes after the caller gave up. So: `server_start`, wait for the boot job,
then **`world_ready()`**, then commands. Every world tool also refuses upfront
if the tick is not moving, naming `world_ready` as the remedy.

**Every argument value crosses the wire as a string.** The mod's parser is
strict: a JSON number anywhere in `args` rejects the whole args block. The
tools stringify numbers and booleans themselves and refuse values with no
faithful string form (lists, dicts, None). Positions travel as one string,
`"x y z"`.

**A refusal is a result.** The mod's own sentence comes back verbatim as the
error: "no player is on the server", "the class does not exist", "the action's
own Can() said no". Nobody connected is the normal state of a headless stand,
and every verb that needs a player says so instead of silently doing nothing.

**The session id protects against yesterday's command.** Every command carries
the session the bridge most recently published; the mod refuses any command
addressed to another session (or none) without executing it. A command written
while the stand was down can therefore never fire into a freshly booted world.
The tools stamp the session automatically — it only matters if you write the
mailbox by hand.

### Actions, and why there is no verb dictionary

A semantic verb like "hand in the sample" lies: in a real mod the same words
mean different things depending on which device is near, the player's faction,
and what is already unlocked. That context is not enumerable, so the bridge
does not try. `world_action` takes an action's **class name**, a target and the
held item, and asks the engine to run it through its own gate — the same one a
key press goes through. Applicability is decided by the action's own `Can()`,
and its refusal is a meaningful test result, not a tool failure. The
distinguishable answers: manager busy, player already acting, player
sprinting, unknown action class, and "the action's own `Can()` said no".
"Accepted" is not success either — the command stays running until the engine
actually releases the action, and every failure path releases the manager so
the player can still act afterwards.

### `world_exec` is the escape hatch

Anything a mod exposes that is *not* an action — "how many points in the
faction pool" — goes through `world_exec(verb, args)`: an arbitrary verb over
the same transport. Every answer is marked `non_standard`: this server does
not know the verb, does not validate it, and does not answer for what the mod
does with it. A verb the bridge build does not know comes back listing the
verbs it does. A project that needs its own verb edits **its own copy** of the
bridge's dispatcher (the comment above `KnownVerbs()` in
`bridge/scripts/5_Mission` says exactly where); there is no registration
machinery on purpose — a verb this server typed and validated would be a verb
this server answers for.

## The client: three input layers, and why there are three

The bridge reaches the **server**. What it cannot do is look at the client's
screen or act through the client — walk a character across ground, open a
menu, fill a field a mod drew. The `client_*` tools are that, and they use
three different tracts because no one of them can do the other two's work.
Every line below is a measurement against a live client, not a design
intention.

| Tract | What it does | Needs the foreground |
|---|---|---|
| the bridge (`world_*`, `client_chat`) | the world, and text into chat | no |
| a virtual gamepad, ViGEmBus (`client_move` / `look` / `press`) | movement, camera, and some interface | no |
| real keystrokes, `SendInput` (`client_type`) | text into a field that exists only on the client | **yes, and it takes it** |

**Keyboard emulation does not move the character, and window messages do
nothing at all.** `SendInput` scancodes with the foreground verified: 25 s of
forward, **0 m**. `PostMessage`/`SendMessage` WM_KEYDOWN into the main window
and its children: **0 m**, and no reaction from the menus either. The engine
reads movement from raw input and ignores emulated keys, which is why no
tool here offers a window message.

**The virtual gamepad does move it, unfocused, and it is ANALOG — the reason
it stays even where a key would do.** Measured in one run with a third-party
application holding the foreground throughout:

    stick fully forward,   10.0 s  ->  38.40 m   (3.84 m/s)
    stick at 0.3 forward,   8.0 s  ->  11.34 m   (1.42 m/s)

Same tract, same character, 2.7× the speed from stick deflection alone.
"The character is walking, not running" cannot be expressed with a key, which
knows only on and off. In the same run the character walked about 141 m of its
own accord, and the mod's own count of objects within 10 m of it went
1 → 0 → 1 as it left the spot and came back — a state change caused by
presence, which a teleport cannot produce.

**Some of the interface answers the pad, and some does not.** Measured, with
the game window behind another application the whole time: `back` opens and
closes the inventory, `start` opens the pause menu, `b` closes it — all at the
default 0.1 s tap, so a tap is long enough for the engine to latch. But `a`
moved nothing, at 0.1 s or at 0.5 s, and neither did the d-pad inside those
screens: the client did not switch to controller-navigation mode, so there was
no focused element for a confirm to act on. Treat menu **dismissal** as a
gamepad job and menu **confirmation** as unproven.

**The eyes need no focus either.** A capture is a live frame with the window
at the very bottom of the z-order (`lit_fraction` 0.9997 unfocused, 0.9997
focused in the same session). The one state that defeats them is a
**minimized** window, whose client area collapses to 0×0 — refused with a
reason rather than saved as a valid-looking empty picture.

**All of that background behaviour rests on one client setting**, `pauseMode`
(GAME → UPDATE IN BACKGROUND). At the value measured here the client keeps
drawing and simulating while unfocused, which is why the frame is live and the
stick still moves the character. At "no graphics" both would stop *silently* —
a frozen frame looks exactly like a live one. So `client_start` and
`client_status` READ that setting and warn; they never write it, because it
belongs to whoever owns the machine.

**`client_type` is the only tool that takes the screen**, and it is honest
about it: the answer carries `foreground_taken` and a sentence saying the
person at the machine could not type into their own window while it ran. It
verifies the foreground with `GetForegroundWindow` after asking for it, because
`SetForegroundWindow` returns success having done nothing when Windows refuses
— and typing blind sends the keystrokes into whatever window the person is
actually using. When the foreground cannot be had, nothing is typed and the
refusal names the process holding it.

**ViGEm is emulation of a real device and this is a test stand.** The driver is
signed and installs without a reboot, and the gamepad is a *new* device rather
than a filter over the machine's own keyboard and mouse — a filter driver was
tried here once and cost the machine's owner all keyboard and mouse input until
it was unwound by hand. None of that is a promise about anticheat on a live
server, and nothing in this phase makes one.

## The knowledge index

An agent writing a mod keeps asking the game the same questions: is there such
an API, what is it called, where is it declared, who overrides it. Answering
them meant unpacking `scripts.pbo` and sweeping the text — and every session
paid again. `knowledge_*` turns that work into a question.

It is a plain SQLite file in the project's own `.dayz-mcp/`, built by this
server out of the game, the mods a project declares and the project's own
sources. No embeddings, no external service, no key.

### Three layers, and why their rhythms differ

| Layer | Source | Goes stale when |
|---|---|---|
| `core` | the game: `dta/scripts.pbo` for the API, `Addons/*.pbo` for the item classes | the game updates |
| `deps` | the archives of the mods the profile declares, read **without unpacking them** | a dependency is updated, or the declared set changes |
| `project` | the mod's own sources, read where they lie | **every edit** |

One index built in one go would be wrong within a minute of being right: the
game moves a few times a year, a dependency a few times a month, and the
project between one agent turn and the next. So each layer is built, aged and
measured on its own, and every build is incremental — unchanged sources are
skipped by size and modification time, and `only=[path]` skips even the walk
that discovers them.

### An answer carries the age of the layer it came from

Staleness is measured, not guessed: a layer records the size and modification
time of every source it read, and that is compared against the files as they
are now.

* Every answer names the layers it used and how old each one is. An answer with
  **no** results names every layer it *searched* — "not found" is worth exactly
  as much as the layers behind it are current.
* The project layer's freshness is measured on **every** search, whether or not
  it contributed. That is the dangerous case: an agent adds a class, asks about
  it, and a layer built a minute ago says "not found" — a confident statement
  about code that exists.
* A search over a layer that was never built is **refused**, and the refusal
  names the call that builds it. "Not found" and "not looked" are different
  facts, and only one of them is safe to act on.
* Narrowing carries the same trap one level down, so an empty narrowed answer
  reports where the name *does* exist: asking `kind='class'` about a name the
  game declares only in a config gets a true "no" that reads as "the game has no
  such class".

Config classes live under `kind='config'`, not `kind='class'`. Counted in this
machine's own index of the game: 88 102 config classes against 43 595 script
declarations of every kind put together, so mixed into one kind they bury every
script answer. Separated, "does the game have an item class called X" is a
question you can ask exactly.

### What it does not answer

The index answers **what exists**: class, method, signature, where declared, who
overrides. It does not answer **what is right** — that `modded class X extends
X` compiles and silently fails to apply, that `_co` costs the alpha channel,
that `binarize` takes directories rather than files. None of that is derivable
from the sources; it was learned the hard way and lives in the modding skill and
in the mod itself. The index does not try to replace either, and it does not try
to understand what a field means or why a class is there.

### Semantic search is deliberately not here

The decision was made by measurement, not caution: every lookup that shaped this
server's earlier phases was a lookup **by name**. And an embedding index would
break the rule the rest of this server keeps — install it and it works, with no
external service and no key. The predecessor project this one deliberately did
not build on documents its knowledge layer as local and free while its code
imports a paid embedding client, fails without a key and carries hard-coded
prices. Its two search tools also hang forever, because the client behind them
was created without a timeout; hence the ceiling every search here runs under.
If exact search turns out not to be enough, semantic search is a separate phase
with one condition: the model ships **inside** the delivery.

### The measured numbers

On this machine — the game with 2810 script files, 35 installed mods, one real
project of 41 sources — through the tools, not their internals:

| Build | Result | Time |
|---|---|---|
| `core` | 2927 sources, 131 697 declarations (41 gave nothing) | 70.2 s |
| `deps`, four declared mods | 8 archives, 10 925 declarations | 0.9 s |
| `deps`, every mod installed here | 523 archives, 204 768 declarations, 3 archives unreadable and named | 139–147 s |
| `project` | 41 sources, 1196 declarations | 0.12 s |

The index on disk: 74.7 MB for a real project's three layers; 110 MB for the
523 dependency archives on their own. Those archives are 92 GB, and none of
them is unpacked.

**Call sites are what the index pays for.** The game's own scripts hold 43 579
declarations and **113 703 call sites**, and recording the second set roughly
doubles the index: measured on the game layer alone, 23.8 MB and 3.7 s to
build without them, 49.6 MB and 4.5 s with. That is the price of being able to
answer "who calls this", and it is stated here rather than discovered later on
a full disk.

| Answer | Time |
|---|---|
| `knowledge_find`, exact name | 4.2 ms end to end, of which 3.0 ms is the project walk |
| `knowledge_find`, prefix, limit 500 | 3.2 ms of query |
| `knowledge_overrides` | 4.2 ms |
| `knowledge_callers`, 23 call sites out of 113 703 | 0.38 ms of query |
| `mod_lint` on a 76-file mod | 277 ms of text checks, 7 ms of index checks |

Measured on a live stand, three boots: `world_time_set(hour=3, minute=7)` moved
the clock to `2026-09-20 03:07` and left the date where it was;
`world_weather_set("fog", 0.9, seconds=2)` took the published fog from 0.085 to
0.900 and held it; `world_entities(pos="7500 0 7500", radius=150, limit=5)`
listed 5 of 171 objects with `truncated: true`. Distances came back at 320 m
for a 150 m radius until they were made horizontal, which is what the engine's
own radius test measures.
| `knowledge_show`, a class with 400 members and its ancestry | 6.8 ms |
| `knowledge_status`, all three layers measured | 41 ms (110 ms on the first call after a build) |

Incrementality, on the real project: a full rebuild 136 ms; one edited file
found by the walk 8.8 ms (**15×**); the same file named through `only=` 5.8 ms
(**23×**). On a 2810-file tree the walk dominates and `only=` is worth far more
— but on a project of this size, 15× is what an ordinary rebuild actually buys.

The ceiling bites for real: a query measured at 77 ms, run under a 19.3 ms
ceiling, was stopped at 19.9 ms, and the connection went on answering.

## The asset pipeline

Getting a model from Blender into a mod is ten steps, and until this phase all
of them were run by hand. The value is not in launching the tools. It is that
**every tool in this chain is structurally unable to report failure**, and each
of those silences had already cost days.

Measured on the real binaries, not assumed:

| What happened | What the tool returned |
|---|---|
| `binarize` handed a file where it wanted a directory | **0**, an empty output directory, not one line of text |
| `binarize` with a material that failed to load | **0**, an ODOL of 46,190 bytes where a correct build is 58,644 |
| `binarize` handed an already-binarized model | `0xC0000005` and a **zero-length file** in the output directory, on top of whatever was there |
| the Blender exporter with its own default arguments | `FINISHED`, exit 0, a valid MLOD carrying **2 of the model's 5 LODs**, and no mention of it in 169 lines of log |

So the rule this whole namespace is built on: **the verdict is read off the
artifact, never off the tool's report.** The exit code is recorded and believed
in neither direction.

### The root is declared, not assumed

`binarize` has **no project-root option at all** — the full switch list was
enumerated against the real binary. The root is the working directory of the
process. The same command, the same input, a different directory, and out comes
a valid ODOL with plausible texture paths that the engine renders untextured,
with a success code and no complaint. The exporting Blender add-on has the same
root in a preference of its own, remembered from whatever project was open
last: on the machine this was developed on it pointed at a directory from an
unrelated session, and against a wrong root the add-on does not fail either —
it strips the drive letter, keeps the rest, and writes paths that look like
paths.

`build.project_root` is that directory, stated once in the portable half of the
profile. The server sets it as the binarizer's working directory and pushes it
into the add-on for the duration of the run, so what the add-on has stored
decides nothing (it is reported, so you can go and fix it). That is what makes
a wrong root **impossible** rather than detectable, and it is why the key is
required before anything model-shaped will run at all.

The refusals it produces happen before a process exists — measured at 0.0003 s
— and a refused build leaves the model the mod already ships byte for byte
untouched.

### Twelve checks on the artifact, and four of them refuse

`asset_check` runs them without building anything and without DayZ Tools,
because a fresh clone must be able to ask whether what it is shipping is
healthy. Four refuse: a built model is there and is an ODOL (C1), no reference
escapes the mod (C3), a material was actually inlined (C4), and nothing already
binarized is offered back to `binarize` (C10). The rest warn: dangling
references, an rvmat pointing into another mod, a transparency lost to DXT1
(C7), an animation that never reached the artifact, a `model.cfg` that is not
the one the artifact was built from, a structural fingerprint that no longer
matches what the last build deployed. Every finding says what to **do**.

C4 is the one worth knowing about. When `binarize` resolves an rvmat it copies
that material's own stage textures into the model — `fresnel`,
`#(argb,8,8,3)`, `env_land_co.paa`, `_nohq`, `_smdi` — strings no MLOD
contains. Six artifacts out of six were separated correctly by that one test,
and it found a broken model on this machine that nobody knew about.

### The Blender step is optional

`asset_export` is the only tool here that needs Blender, and everything
downstream works on a `.p3d` from anywhere — a hand export, a partner's file, a
model committed years ago. A machine with no Blender builds and ships a mod
perfectly well; the refusal says so rather than presenting it as a broken
installation. It does need the exporting add-on to be enabled in the Blender it
finds, and it never writes Blender's user preferences back (verified: the
preferences file was byte-identical after every run).

Export and build are two calls rather than one, because each half has its own
verdict and a build refused by one and allowed by the other is not a decision.

### Byte-equality is never promised

Neither half of this pipeline is reproducible, and the design says so instead
of pretending:

* **The export.** Seven exports of one unchanged source file — three from one
  session, three from another, and one made by hand in the GUI months earlier —
  gave **seven different SHA-256s** at a constant 334,032 bytes. The difference
  is the order of one internal block.
* **`binarize`.** Four runs on one unchanged input gave three different results:
  the size moved by 5 bytes and two 8-byte fragments leaked out of compressed
  regions.

So a model is never cached or compared by content hash. What is compared is a
**structural fingerprint** — the file's kind, its LOD count and its set of
names. Across all seven of those exports that fingerprint was **one value**.

### The measured numbers

One small model, on this machine, through the tools:

| Step | Result | Time |
|---|---|---|
| `asset_export` | MLOD, 334,032 B, 5 LODs, clean | **2.1 s** (about 8 s on a cold start) |
| `asset_build` | ODOL v55, 58,646 B, 4 LODs, all five C4 markers | **43.8 s** (75.6–78.7 s measured on four earlier runs) |
| `asset_check` | 1 model and 10 texture pairs judged | milliseconds |
| `asset_convert` | one PNG to DXT1, 50,764 B | 0.52 s |
| a refusal on a wrong root | before any process is started | **0.0003 s** |

Both logs are almost entirely boilerplate, and what is muted is counted rather
than dropped: Blender's 169 lines came down to **4**, and `binarize`'s 91 to
**6** — one of those six being the model's only genuine complaint.

Chained end to end, the export and the build reproduced a model that had been
made by hand months earlier: same kind, same 4 LODs, **the same 50 strings**,
and a size one byte apart.

### What none of it answers

Whether the model looks right, is scaled right, is wound right, has a
collision. Nothing outside the game answers that. C1–C12 shorten the road to
it; they do not replace it.

## Known limitations

* **Stale-pbo detection is mtime-based, not content-based.** `mod_build`
  refuses a freshly built pbo that is older than its sources -- the usual
  cause is a running server still holding the old file open, so packing
  silently produced nothing. But `git checkout` changes a file's modification
  time without changing its content, so a perfectly good pbo built right
  after switching branches can trip this check too. If `mod_build` reports
  "stale pbo" immediately after a branch switch, this is the likely reason,
  not a real packing failure -- rebuild and it will pass. A mature tool in
  this space moved to a content hash for exactly this reason; that is future
  work here, not done in this phase (see `packer.py`, `pack_one`).
* **A mod source folder is packed whole.** `mod_build` refuses to pack a mod
  whose source directory contains anything matching `build.exclude` (the
  seven-pattern default is listed above) rather than silently shipping it
  inside the published pbo. It refuses regardless of `build.exclude` when the
  source contains this server's own artifacts -- the signing keys, either half
  of the profile, the job store, the mod's previous build -- because packing
  those publishes the private signing key, and no project should have to
  configure that away. By default it does not stage a filtered copy first: a
  copy is always newer than the sources, which would permanently disable the
  stale-pbo check above *if that check measured the copy*. `build.stage = true`
  opts into copying anyway -- safe only because the stale-pbo comparison always
  measures the original source tree, never the copy. This is the layout a mod
  whose source is the repository root needs (it always contains at least
  `.git`).
* **A verdict judges the whole log, not just your mod's lines.** `log_verdict`
  reads the log of the stand it is pointed at, so a stand shared with other
  mods counts their warnings against your `expect.max_warnings` budget and
  their errors as reasons. Two projects sharing one `machine.stand_root` will
  see each other's baseline. Either give each project its own stand, or set the
  budget knowing what else is loaded. A project-scoped filter, symmetric with
  `expect.error_regex`, is the obvious refinement and is not implemented.
* **`expect.noise` cannot rescue a line that already counts as an error.**
  Classification is ordered `forbid` → crash → error → noise → warning, so a
  line containing `ERROR` or `FATAL` (or one of your `forbid` strings) is
  decided before noise is consulted. That order is deliberate -- noise matching
  first would let an innocuous substring swallow a fatal line -- but it means
  `noise` can only suppress warnings and ordinary lines, never demote an
  error-level one.
* **`client_verdict` is an errors-and-crashes verdict, not a readiness one.**
  `[expect]` describes the *server's* log: its ready line and counters are
  printed by a mod's server-side init, and `max_warnings` is a budget counted
  over that same log. A client `.RPT` contains none of it, so those three keys
  are deliberately not applied here and the answer lists them in
  `not_applied`. `forbid`, `error_regex` and `noise` are about the text of a
  log line and still apply. There is no client-side ready line to declare;
  whether the client got in is answered by the player count `client_start`
  waits on, not by its log.
* **The client tools join a stand this session did not start; `client_chat`
  cannot.** `client_start` will happily connect to whatever is already on the
  port, and says whose it is. But chat is delivered server-side, through the
  same channel as the `world_*` tools, and that channel acts only on a server
  this session started — so on a borrowed stand everything except `client_chat`
  works. The refusal names the pids holding the port rather than suggesting a
  `server_start` that would refuse them.
* **Chat is not reachable from the gamepad, and confirming a menu is not
  either.** The game binds its chat line to Enter and nothing else, and there
  is no on-screen keyboard, so text is either a bridge message (`client_chat`,
  free) or real keystrokes (`client_type`, costs the foreground).
  `client_type("", submit=True)` sends Enter alone, which is how the chat line
  is opened — and, on the evidence above, the only confirm the tool set has.
* **Every knowledge search pays a walk of the project tree.** That walk is how
  the project layer's staleness is measured on every answer, which is the one
  property the index exists for. It costs 3.0 ms on a real 41-source mod (whose
  tree holds about 1800 entries) and 21 ms on a tree of 2810 files. Caching it
  for a second or two would remove the cost and restore exactly the window of
  silence the design refuses; if it ever becomes too expensive, that trade has
  to be made deliberately, not by accident.
* **A build always goes through a job, and the job costs more than a small
  build does.** Turnaround measured at 70–95 ms against a 6 ms project rebuild:
  `job_wait` polls at 100 ms. The single shape is deliberate — a caller must
  not have to know which build blocks — and nothing forces you to wait, because
  the next search measures the layer itself.
* **The dependency layer is measured against the profile as it is now.** Add a
  mod to `mods.required` and its archives arrive as `added`; remove one and its
  archives read as `missing`. That is the requirement (the declared set is part
  of what the layer is built from) but it looks like the index went stale when
  what actually changed was the profile.
* **`core` always includes the game's configs, and that is most of its cost.**
  70 s with them against about 4 s for the scripts alone. There is no switch:
  without the configs "is there an item class called X" cannot be answered, and
  a second axis would make the staleness measurement ambiguous — the walk would
  not know whether to expect the `Addons` archives.
* **`knowledge_show` answers nearest-layer-first.** For a class a dependency
  reopens with `modded class`, the mod's declaration comes before the game's.
  That is the right order and a surprising one; pass `layer='core'` for the
  game's own.
* **Conditional compilation is indexed, not resolved.** 4.9% of the game's
  script lines sit inside `#ifdef`, including about a hundred class
  declarations. This server drives server, client and diagnostic builds, so
  there is no single correct set of defines: everything is indexed and the
  guard is recorded on the declaration. A name can therefore be reported that a
  particular build excludes — the alternative, filtering by one guess at the
  defines, would deny the existence of methods that are in the running build.
* **C12's fingerprint carries the file's size, and `binarize`'s size is not
  stable.** Rebuilding a model that nobody edited produced an artifact one byte
  larger than the shipped one, with the same kind, the same LOD count and the
  same fifty strings — and a different digest, because the size is part of it.
  So C12 can warn about a rebuild that changed nothing. It warns rather than
  refuses for exactly this reason, and the parts it is built from are reported
  beside it so the comparison can be made by hand. Splitting the digest into a
  stable half and a size is the obvious refinement and is not done.
* **A partial export warns; it does not refuse.** With the exporter's own
  default arguments a model came out carrying 2 of its 5 LODs and passing every
  other check. This server does not pass those arguments, so it should not
  happen — but an object marked as a LOD and not linked into the scene counts
  on one side of the comparison and not the other, which is a legitimate reason
  for the counts to differ, so a refusal would have false positives. Read E3.
* **The containment rule cannot see every wrong root.** It refuses a root that
  does not hold the mod's prefix folder, which is the measured failure. A root
  that *does* hold a folder of that name — a repository whose own mod directory
  is spelled like the prefix, for instance — passes it, and what catches that
  case is C10 or C3/C4 one layer down. Measured: pointed at such a root, the
  build refused, deployed nothing and left the shipped artifact untouched, but
  the refusal came from the job rather than from the call.
* **`asset_export` needs the exporting add-on enabled in Blender, and cannot
  install it.** Blender is launched with the machine owner's real preferences,
  because starting it with `--factory-startup` takes the add-on away entirely.
  Their other add-ons are kept off the search path for the run (two of the ones
  installed here reach the network as they start and are blamed for crashes),
  which Blender reports as "Add-on not loaded" in the log — that line is this
  server's own doing, not a fault.
* **A binarised config has no body to show.** `knowledge_show(body=True)` reads
  a declaration back out of the file or archive it was indexed from, but a
  `config.bin` holds the binary form while the index holds what `CfgConvert`
  made of it. The answer says so instead of returning nothing.

## Install

    python -m pip install -e ".[dev]"
    python -m pytest

Register in your MCP client:

    { "mcpServers": { "dayz": { "command": "dayz-mcp" } } }

## Licence

GPL-3.0-or-later. See `NOTICE.md`.
