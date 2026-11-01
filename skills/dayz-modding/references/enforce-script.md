# Enforce Script — language reference (DayZ)

Verified against the official wiki (Enforce Script Syntax) and the 1.29 game scripts
(`scripts.pbo` 124708); sections dated later say which build they were measured on, and the
2026-11-01 audit re-checked the cited vanilla lines against 1.30.164200 (`scripts.pbo` 127662).
Enforce Script ≈ typed C-family with single inheritance, no GC, refcounted objects.
Files end in `.c`, compiled per-module-tier VM at game start (or on `-filePatching` reload).

## What does NOT exist (never generate these)

Ternary `?:` (use if/else) · `do...while` (while + break) · try/catch/finally (guard
clauses + bool/out returns) · lambdas/closures (named methods, ScriptInvoker,
`ScriptCaller.Create(fn)`) · operator overloading · namespaces · interfaces ·
`#include` · nested classes · string interpolation (`string.Format`, max %1..%9).

Method overloading DOES compile, by parameter count and by parameter type (checked
2026-11-01): CF's `CF_ModStorage.Write(bool/int/float/vector/string)` (CF_ModStorage.c:156-176),
and vanilla itself — `PlayerBase.AddAction(typename, out TInputActionMap)` /
`AddAction(typename)` (1.30 playerbase.c:1817/:1838), `SetActions(out TInputActionMap)` /
`SetActions()` (:1695/:1747). Constructor overloading is unverified.

Does exist (surprises): `switch` **falls through like C** (break mandatory) · `sealed`
classes · `auto` inference · short-circuit `&&`/`||` · `notnull` parameter modifier ·
`event` keyword · `thread` coroutines with `Sleep(ms)` (crashes outside threaded
context; stop via `KillThread(owner, "FnName")`).

Compile traps: redeclaring the same variable name in **sibling if/else-if blocks** is a
compile error ("Redeclaration of variable") — declare once before the chain · default
parameter values must be constants — literals, NULL, named constants or enum values
(vanilla: `= UAMaxDistances.DEFAULT` in cctcursor.c:11, `= RoadSurfaceDetection.LEGACY`
in game.c:1177) — never a call (no `int x = GetValue()`) · `string` is a
value type (copied on assign/pass).

## Types

Primitives: `int` (32-bit signed), `float`, `bool`, `string`, `vector` (3 floats:
`"1 0.5 2"` / `Vector(x,y,z)`, components `v[0]`), `typename`, `void`, `class`, `auto`.
Defaults: 0 / 0.0 / false / "" / zero vector / null.

```c
int a = 5;
float b = 5.5;
vector pos = "7500 300 7500";
string s = string.Format("%1 has %2 points", name, points);   // %1..%9 placeholders
auto x = new map<string, int>();     // type inference
```

Casting: `int i = 3.7;` truncates; object casts via `MyClass.Cast(obj)` (returns null on
mismatch — the standard safe-cast) or `MyClass c; Class.CastTo(c, obj);`.
Constants: `int.MAX/MIN`, `float.MAX/LOWEST`, `string.Empty`, `vector.Zero/Up/Aside/Forward`.
Trap: `1 < int.MIN` evaluates TRUE — don't rely on integer-limit comparisons.
Coordinates: pos[0]=X east, pos[1]=Y altitude, pos[2]=Z north; `vector.DistanceSq` for
cheap radius checks.
Cross product: `a * b` between two vectors (vanilla `vector.Perpend()` is
`return value * vector.Up;`, documented as the cross product, enconvert.c:222-225). `vector`
has no `Cross()` method: none is declared in `1_core/proto/enconvert.c` of 1.29 (125372) or
1.30.164200, and no vanilla script calls one.

## Classes

```c
class Animal
{
    protected int m_Health;             // public by default; private/protected available
    void Animal(int health)             // constructor = class name
    {
        m_Health = health;
    }
    void ~Animal();                     // destructor — NOT guaranteed on process kill
    void MakeSound();                   // "virtual" by default
}

class Dog : Animal                      // single inheritance; ':' or 'extends'
{
    override void MakeSound()           // 'override' keyword is required
    {
        Print("Wof!");
    }
}
```

- `new Dog(100)` allocates; without a strong ref it is refcounted away.
- `static` members: `ClassName.Member`. `const` for constants.
- Method overloading by parameter count or type compiles (see "What does NOT exist" above);
  constructor overloading is unverified — prefer default args there:
  `void Foo(int a, int b = 0)`.
- `out` / `inout` parameter modifiers pass by reference:
  `bool FindPlayer(string id, out PlayerBase player)`.

## An `if` condition must fit on one line (verified 2026-08-26 and 2026-08-30)

```c
// BOTH of these fail: "Expected ')', not a '||'"
if (a() ||
    b())            { ... }

if (a()
 || b())            { ... }

// Write one check per line instead
if (a()) return false;
if (b()) return false;
```

The parser does not join a condition across lines in either spelling. Measured twice: on
2026-08-26 with the error above, and on 2026-08-30 (a diag boot) a 4-clause condition
broken after the second `||` (`if (a || b ||\n    c || d)`) failed the "World" module
compile with a plain `Syntax error` naming that line. Fix: keep the condition on one line,
or precompute (`bool hit = a || b; if (!hit) hit = c || d; if (hit) ...`). Vanilla never
splits a boolean expression across lines: no 1.30 script line starts or ends with `||`/`&&`.
(Unverified: an older note says a leading-`+` continuation of a string expression is
silently dropped rather than refused; no measurement of it is recorded in this skill.)

## A field initialiser on a base class breaks private access in derived classes (verified 2026-08-26)

> **Suspect as a general rule (audit 2026-11-01): contradicted by vanilla; re-measure.**
> 1.30 `ItemBase` carries initialisers (itembase.c:40 `float m_StoreLoadedQuantity =
> float.LOWEST;`, :49 `bool m_CanPlayImpactSound = true;`) and its subclasses call their own
> `private` methods and compile: `EasterEgg` (easteregg.c:70 calls `private void Capture` at
> :157), `Edible_Base` (edible_base.c:1019 calls `private void PrintNutritionsData` at :1071).
> What the bisection below proves is narrower — a `string` initialiser on that mod's base
> class — and the cause may lie elsewhere. Keep avoiding initialisers on mod base classes
> until it is re-measured, but do not read the rule as a language law.

Isolated by bisection on a live stand, one field at a time, four boots.

```c
class Base
{
    protected string m_Foo;        // compiles
    protected string m_Foo = "";   // DOES NOT COMPILE -- see below
}

class Leaf : Base
{
    override void OnThing() { Helper(); }
    private void Helper() {}
}
```

With the initialiser present, EVERY class deriving from `Base` loses access
to its OWN `private` methods:

```
SCRIPT (E): @"...leaf.c,54": Method Helper is private
```

Two things make this expensive to diagnose:

- **The error names the derived files, never the base.** The file actually
  at fault does not appear in the error list at all. Four unrelated files
  reported errors on lines nobody had edited.
- **The same initialiser is fine on a leaf class**, so the pattern appears
  elsewhere in the same codebase and looks proven.

Enforce zero-initialises members anyway -- `string` starts `""`, `int`
starts `0`, object refs start `null` -- so the initialiser buys nothing on
a base class. Declare bare, and assign in a method if a non-zero default is
needed.

Rule: **no field initialisers on any class that is inherited from.**

## modded classes (the modding mechanism)

```c
modded class PluginRecipesManagerBase
{
    override void RegisterRecipies()
    {
        super.RegisterRecipies();       // call the vanilla (or previous mod's) version
        RegisterRecipe(new MyRecipe);
    }
}
```

- `modded class X { }` injects a subclass that *replaces* X everywhere — `new X()` yields
  the modded version. Multiple mods chain: each `modded` inherits from the previous.
- Inside a modded class you may access `private` members of the original.
- `super` = previous implementation in the chain. Omitting `super` calls is the #1 mod
  compatibility bug.
- **`modded class X extends Y` compiles cleanly but the class NEVER joins the modded
  chain** — the base is implicit; never write an inheritance clause on modded. (#1 cause
  of "my override isn't running".)
- (unmeasured) Engine-backed classes may refuse `modded class`: `Widget` (`class Widget:
  Managed`, enwidgets.c:107, all proto native) and `UIManager` (`class UIManager` with no
  base, uimanager.c:1, all proto native). `UIScriptedMenu` is a script class
  (`extends UIMenuPanel`, uiscriptedmenu.c:66) — whether it can be modded is not measured.
  Safest: mod a concrete script class (`modded class InventoryMenu`, `modded class IngameHud`).
- **`EntityAI` cannot be modded either** (measured 2026-09-26, retail server): a
  `modded class EntityAI { int m_X; }` in 3_Game is refused at boot with
  `Engine class 'EntityAI' cannot be modded`, although `entityai.c` has a script body.
  To put a field on "everything that can stand in a cargo", `modded class ItemBase` is
  enough: `Weapon_Base` descends from it through `InventoryItemSuper` (measured the same
  day — declaring the field on `Weapon_Base` as well fails with
  `Multiple declaration of variable`). Read it through `ItemBase.Cast(e)`. One
  `modded class X` per class per mod: add the field to the reopening you already have.
- `#ifndef NO_GUI ... #endif` guards client-only STATEMENTS and MEMBERS (NO_GUI is defined
  on server builds, 1_core/defines.c:29). Vanilla 1.30 has 171 such guards: none around a
  class declaration, 11 around members (missionserver.c:59-62 guards three fields), 160
  around statements inside function bodies (missionserver.c:110); 1.29 had 51 (0 / 4 / 47).
  Vanilla GUI classes compile on the server unguarded (`class IngameHud extends Hud`,
  ingamehud.c:1; inventorynew/inventory.c has no guard at all), so being a GUI class is not
  what breaks a server compile — touching a NO_GUI-only symbol is (e.g. MissionServer's
  `m_ControlDisabledMode`). Guard those members/statements; wrapping a whole modded class
  is legal but not vanilla practice. The guarded half is compiled only by clients: check it
  with `client_compile_check` — a server boot never sees errors in it.
- You can add new members and methods; other files see them if declared in the same tier.

## Memory: ref / autoptr; members weak by default, locals strong

```c
class Registry
{
    ref array<ref Sample> m_Samples;    // strong: Registry owns array, array owns Samples
    PlayerBase m_LastUser;              // weak: does NOT keep the player alive
}

void Fn()
{
    autoptr FileHandleHelper h = new FileHandleHelper();  // deleted at scope exit
    Thing t = new Thing();              // a plain local already holds it until scope exit
}
```

- MEMBER object variables are **weak** unless marked `ref`. Objects with zero strong refs
  are destroyed; weak pointers then read as null.
- LOCAL variables are strong without `ref`: vanilla news containers into plain locals and
  uses them afterwards (1.30 areadamage.c:174 `array<vector> raycast_sources = new
  array<vector>;`; fifteen `ScriptRPC rpc = new ScriptRPC();` locals). `ref` on a local is
  redundant but legal: vanilla runs one in `PlayerBase.AddAction` on every server
  (playerbase.c:1828-1832, `ref array<ActionBase_Basic> action_array` later assigned
  `new array<ActionBase_Basic>`; also areadamage.c:175, 41 such locals in 1.30). House style
  here: no `ref` on locals (see the 2026-09-06 section below and its note).
- Entities in the world (ItemBase, PlayerBase, …) are owned by the engine — never hold
  `ref` to them (dangling entity = crashes); store them weak and null-check, or store
  identifiers instead.
- Circular strong refs leak. Break cycles with weak backpointers.
- `delete obj;` forces destruction.
- (community-reported, unverified) classes inheriting `Managed` are script-only and their
  weak refs auto-null on delete; non-Managed weak refs may dangle. Layout `scriptclass`
  handlers MUST inherit Managed regardless (see gui-layouts.md).

## Templates (generics)

```c
class Wrapper<Class T>
{
    T m_Value;
    void Set(T v) { m_Value = v; }
}
Wrapper<string> w = new Wrapper<string>();

// stock generics used everywhere:
array<int>; array<ref MyClass>; map<string, ref MyClass>; set<string>;
Param1<int>; Param2<string, float>; ...                  // RPC payload tuples
JsonFileLoader<MySettingsClass>;                          // static helpers per T
```

## Containers

```c
array<string> names = {};            // or: array<string> names = new array<string>;
names.Insert("Sidorovich");
int n = names.Count();
string first = names.Get(0);         // or names[0]
names.Remove(0);                     // WARNING: UNORDERED — swaps with last, truncates
names.RemoveOrdered(0);              // preserves order — use when order matters
int idx = names.Find("Sidorovich");  // -1 if absent

map<string, int> points = new map<string, int>();
points.Insert("uid123", 10);         // does NOT update an existing key (returns false)
points.Set("uid123", 20);            // insert-or-update — use for upserts
int p;
if (points.Find("uid123", p)) { }    // safe lookup
points.Get("uid123");                // null/0 if missing
points.Contains("uid123");
points.Remove("uid123");

foreach (string name2 : names) { Print(name2); }
foreach (int i2, string v2 : names) { }          // index + value
foreach (string k, int v : points) { }           // map: key + value
```

- More array API: `IsValidIndex(i)`, `Sort(true)` = descending, `GetRandomElement()` /
  `GetRandomIndex()` (UNIFORM: `Math.RandomInt(0, Count())`, enscript.c:567-571 — build any
  weighted drop roll yourself), `Reserve(n)`, `Resize(n)`, `InsertAll(other)`,
  `Copy(other)`.
- Static arrays `int arr[5]` are fixed-size; out-of-bounds is UB (no runtime check).
  Trap: assigning a complex expression directly into a static-array element can segfault —
  compute into a local first (community-reported).
- Aliases: `TStringArray`, `TIntArray`, `TFloatArray`, `TStringMap`, etc.
- Never remove while foreach-iterating — collect indices, then `RemoveOrdered` in reverse.

## Enums

```c
enum EResearchTier
{
    BASIC = 0,
    ADVANCED,           // 1
    EXPERIMENTAL        // 2
}
enum EExtended : EResearchTier { SECRET }   // continues numbering
```

Enums are ints; `typename.EnumToString(EResearchTier, val)` for debug printing.
Extending vanilla enums (`ERPCs`, `eModifiers`, `EPlayerStats_current`, `eAgents`)
continues base numbering — two mods doing it collide silently; pick high sentinel values
(and never extend `EPlayerStats_current` at all: KK_SkillFramework does, and any server may
run it — it did on one RP server, snapshot 2026-07-31).

## typename & reflection

```c
typename t = PlayerBase;                 // or obj.Type()
string cls = obj.ClassName();            // config classname of entities
obj.IsInherited(ItemBase);               // SCRIPT class hierarchy — fast, on Class
GetGame().IsKindOf(cls, "Inventory_Base"); // CONFIG (CfgVehicles) hierarchy — for
                                         // admin-JSON classname matching (base classes)
MyAction a = MyAction.Cast(action);      // safe downcast

// spawn by string classname (entities): GetGame().CreateObjectEx / player.SpawnEntity...
// instantiate script class by typename: t.Spawn()
// generic field reflection (settings validators):
// EnScript.GetClassVar(obj, "m_Field", 0, outVal) / SetClassVar (silent on wrong name);
// t.GetVariableCount()/GetVariableName(i)/GetVariableType(i)
```

Rule: JSON config classnames (admins write CfgVehicles base classes) → `IsKindOf`;
server-side script type gates → `IsInherited`.

## Events / ScriptInvoker

```c
class ResearchManager
{
    static ref ScriptInvoker SI_OnPointsChanged = new ScriptInvoker();
}
ResearchManager.SI_OnPointsChanged.Insert(OnPointsChangedHandler);  // subscribe
ResearchManager.SI_OnPointsChanged.Invoke(playerId, newTotal);      // fire
ResearchManager.SI_OnPointsChanged.Remove(OnPointsChangedHandler);
```

- Every Insert needs a matching Remove (double Insert = handler fires twice; one Remove
  removes one).
- Static invokers have no stable teardown order — guard at shutdown:
  `if (ResearchManager.SI_OnPointsChanged) ResearchManager.SI_OnPointsChanged.Remove(cb);`
- Statics survive mission restarts — full lifecycle contract: patterns-cookbook.md §14.

## Timers & deferred calls

```c
// one-shot deferred call (MILLISECONDS):
GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(Finish, 3000, false /*repeat*/, param1);
GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).Remove(Finish);       // MANDATORY in dtor

// by-name variant is a DIFFERENT method (verified 2_gamelib/tools.c:63):
// proto void CallLaterByName(Class obj, string fnName, int delay = 0, bool repeat = false, Param params = NULL)
// CallLater(this, "Method", ...) is a StarDZ-doc error — object+string args = ByName only.

// repeating Timer (SECONDS! must be held by ref):
ref Timer m_Tick = new Timer();
m_Tick.Run(5.0, this, "OnTick", null, true);   // every 5 s, looped
```

Categories (verified vanilla usage): `CALL_CATEGORY_SYSTEM` (survives mission lifecycle),
`CALL_CATEGORY_GAMEPLAY` (mission gameplay logic — vanilla MissionServer uses it; "Runs unless
ingame menu is opened", 3_game/tools/tools.c:10, so a client callback there stalls while the
pause menu is up), `CALL_CATEGORY_GUI` (UI). (community-reported) CallLater stores due time in a 32-bit
float — after ~4.5 h uptime callbacks drift/stall/burst; for long processes persist
absolute timestamps and reconcile (see persistence-networking.md §6).

## Strings, math, random

```c
// ToLower/ToUpper/Replace/TrimInPlace MUTATE IN PLACE and return int (verified enstring.c:171,
// :319); Trim() is the exception: `proto string Trim()` returns a trimmed copy (enstring.c:304):
string k = cls;
k.ToLower();                     // string x = s.ToLower();  <-- type bug, never compiles right
s2.Length(); s2.Contains("Lab"); s2.Substring(0, 3); s2.IndexOf("a"); s2.LastIndexOf("a");
// NO string.Compare(): it does not exist (a call fails to compile, measured 2026-09-19); order strings
// by hand -- ToAscii() per character (enstring.c:86) with Length()/Substring().
s2.Split(" ", tokens); string.Join(", ", arr); num.ToStringLen(5);  // zero-pad
int v3 = s2.ToInt(); float f3 = s2.ToFloat(); "1.0 2.0 3.0".ToVector();

Math.RandomFloat01();            // 0..1 — chance rolls
Math.RandomInt(0, 10);           // 0..9 (max EXCLUSIVE)
Math.RandomIntInclusive(1, 10);  // both ends inclusive
Math.RandomBool(); Math.NormalizeAngle(deg);
Math.Clamp(x, min, max); Math.Round(x); Math.AbsFloat(x);
```

Roll a configured chance server-side: `if (Math.RandomFloat01() < cfg.Chance) { ... }`.
String compare is case-sensitive (`==`); normalize config classnames with the
copy-then-ToLower idiom above (engine classnames are case-insensitive in configs but
not in string maps!).

## Logging

```c
Print("value: " + x);                          // script.log in profile dir
PrintFormat("Processed %1 into %2", inCls, outCls);
Error("Config invalid: " + path);              // red, with callstack
ErrorEx("soft error", ErrorExSeverity.WARNING);
```

Logs land in the `-profiles=<dir>` folder (`script_*.log` / crash RPTs). Tell the user
which log to check after every test plan. Caveat (community-reported): compile errors
for undefined classes/name conflicts can report the WRONG location (last parsed file at
EOF) — search for the symbol instead of trusting file:line; `crash_*.log` files contain
script exceptions, not real crashes.

**`Formula too complex`: one expression has a complexity ceiling (measured 2026-08-07,
2026-09-26, 2026-10-31; the three notes merged here 2026-11-01).** A single expression built
from many chained `+` operators — the "one big boot-summary `Print`/log line" idiom — fails
with `Formula too complex` on that exact line, immediately followed by a cascade of
unrelated-looking errors (`Incompatible parameter`, `Bad type 'JsonFileLoader'`, …) on every
later line of the same class — parser recovery, not real errors — and the whole module
refuses to compile.

| Measured | Expression | Result |
|---|---|---|
| 2026-08-07 (1.29) | boot-summary string `"a=" + x + " b=" + y + ...`, 16 terms (15 `+`) | compiled |
| 2026-08-07 (1.29) | the same plus one clause, 18 terms (17 `+`) | failed |
| 2026-09-26 (1.29, client) | `Note("a" + b + ": " + c.GetType() + ...)`, 14 terms with calls | failed |
| 2026-10-31 | 23 and 27 operands | failed; 11-12 operands compiled |
| vanilla 1.30 | hand_events.c:696 (13 terms, 6 of them static calls; retail); transport.c:1122 (15 plain terms, under `#ifdef DIAG_DEVELOPER`) | compile |

So the limit is on the expression's complexity, not on the string, and calls weigh more than
plain terms. Rule: keep each expression to about 10 terms and build long strings in steps
(`string s = a + b + ...; s += c + d + ...; Print(s);`) — same final string, no behaviour
change. Only a script compiler reaches this line: packing (FileBank, `mod_build`) does not
compile-check, so it surfaces only in `client_compile_check` or a boot — and code under
`#ifndef NO_GUI` only in `client_compile_check`, because the server never compiles that half.

A multi-line `if` condition is a syntax error: see "An `if` condition must fit on one line"
near the top of this file (the 2026-08-30 measurement is merged there).

## Preprocessor & platform gates

Engine defines (existence flags only — no value substitution; `#else` works). Source:
`1_core/defines.c`, identical in 1.29 and 1.30 (a documentation file; the engine adds them):

| Define | Active when |
|---|---|
| `SERVER` | dedicated server only — "Only defined when CGame.IsDedicatedServer equals true" (:106, :115); NOT on an offline/listen host |
| `NO_GUI`, `NO_GUI_INGAME` | server builds (:29, :34) — guard client-only statements/members (see "modded classes") |
| `DIAG` | Diag builds, i.e. DayZDiag (:77) |
| `DEVELOPER` / `RELEASE` | exactly one is always active; `DEVELOPER` is the internal build, `RELEASE` "includes Diag" (:80-87) — so DayZDiag is RELEASE + DIAG, not DEVELOPER |
| `DIAG_DEVELOPER` | DIAG or DEVELOPER (:92) — the define that is on in DayZDiag |
| `PLATFORM_WINDOWS` / `_LINUX` / `_XBOX` / `_PS4` / `_CONSOLE` | platform |
| `BUILD_EXPERIMENTAL` | experimental branch |

Custom symbols: `defines[] = {"MYMOD"};` inside your CfgMods class, beside `dependencies[]`
(not inside `class defs`; see mod-structure.md);
(community-reported) since DayZ ~1.26 every CfgMods class name is also auto-registered
as a define — the mechanism for optional soft-dependencies
(`#ifdef DAYZEXPANSIONAI ... #endif`). Mods are also seen gating on CfgPatches names —
verify which symbol a given target mod exports before relying on it.
Trap (community-reported): an EMPTY `#ifdef X ... #endif` block (even comment-only) can
segfault — keep at least one statement.
For "dedicated server or not", vanilla's own documentation prefers the define: `SERVER`
"Should be preferred over using CGame.IsDedicatedServer when possible" (defines.c:113). The
runtime checks remain the tool for everything a define cannot express (listen/offline host,
client, multiplayer):

```c
if (GetGame().IsDedicatedServer()) { }        // pure server
if (GetGame().IsServer())          { }        // server (true also in SP host)
if (GetGame().IsClient())          { }        // connected client
if (GetGame().IsMultiplayer())     { }
```

(community-reported) IsClient/IsServer are unreliable DURING LOAD (client can report
IsServer=true while loading) — in constructors/Init paths gate on
`g_Game.IsDedicatedServer()` / `!g_Game.IsDedicatedServer()` instead; that is what the
verified settings examples already do. Contexts: dedicated = IsServer T / IsClient F;
client = F/T; listen server = BOTH true (hides races — always test on dedicated).

Operator-precedence trap: `if (a & b == b)` parses as `a & (b == b)` — always
parenthesize bitwise ops. A `switch` whose final `return` sits in `default:` still
errors "function must return a value" — move the return after the switch.

## File IO (for $profile configs)

```c
if (!FileExist("$profile:MyMod"))
    MakeDirectory("$profile:MyMod");           // NOT recursive — create each level
FileHandle fh = OpenFile("$profile:MyMod\\log.txt", FileMode.APPEND);
FPrintln(fh, "line");
CloseFile(fh);
```

Prefer `JsonFileLoader<T>` for structured config — full file-IO sharp edges (FindFile
returns pathless names, CopyFile backups, LoadData/MakeData): persistence-networking.md §5.

## `%` is integer-only; in a float context it does not compile (measured 2026-09-12)

`a % b` works on `int` operands in an `int` context (`(want - lo) % stride`,
`idx = i % grid`). Inside an expression the compiler types as `float` — the
arguments of `Vector(float, float, float)`, arithmetic with a float — the operands
are promoted first and the operator is gone: `Vector(i % 8, 0, i / 8)` fails at
script load with `Unknown operator '%'`. Compute into `int` locals, then use them:

```c
int col = i % 8;
int row = i / 8;
vector pos = at + Vector(col, 0, row);
```

## Known limitations / gotchas

- No garbage collector; forgetting `ref` on containers-of-objects = objects vanish.
- No multiple inheritance, no interfaces (use base classes / duck typing via typename).
- No exceptions — return bools + `out` params; `Error()` for loud failures.
- Constructor overloading is unverified (method overloading compiles, see the top of this
  file); `modded` classes get no custom constructor args.
- Numbers: int overflows silently; float compare with tolerance.
- Script compile errors appear at game/Workbench start — always give the user the
  expected "compile clean" checkpoint before testing gameplay; fix the FIRST
  `SCRIPT (E)` error, the rest cascade.

## `ref` on a LOCAL variable: the 2026-09-06 crash, not reproduced 2026-11-01

> **Not reproduced (re-measured 2026-11-01, DayZ 1.30.164200, a stand mod).** The exact
> declaration below, with a `ref array<string>` local beside it and `string` locals read
> before and after both, ran 100,000 times on the server and 100,000 times on the client at
> mission start, then 2,000 times every 10 s for minutes: no fault, correct results. Vanilla
> does the same in `PlayerBase.AddAction` on every server (1.30 playerbase.c:1828-1832; 41
> `ref` locals in 1.30). So a `ref` local is redundant, not fatal; the crash below was most
> likely the wandering heap corruption measured on 2026-09-01 (the compound-boolean field
> write, further down), whose crash site "is never the culprit". No `ref` on locals stays the
> house style.

```c
// the 2026-09-06 suspect; ran clean 100,000 times on 2026-11-01
ref map<string, bool> online = new map<string, bool>();

// The house style everywhere else, and the one that works
map<string, bool> online = new map<string, bool>();
```

`ref` belongs on CLASS MEMBERS and on container ELEMENT types
(`array<ref Foo>`); a local variable already owns what it holds. Marking one
`ref` compiled without a word and then faulted the server inside an unrelated
static two lines BEFORE the map was ever touched — `0F B6 03 / 48 FF C3 /
84 C0 / 75 F6`, a strlen walking freed memory, reading a `string` local that
had been valid one statement earlier. Removing the `ref` fixed it with no
other change (a factions stand mod, the diag RPT of 2026-09-06).

Recognising it: the crash lands in whatever function happens to read a local
next, so it looks exactly like the wandering heap-corruption crash below — but
the cure is in the DECLARATION, not in the code that faulted.

## Returning a container from a non-`ref` function destroys it (verified 2026-08-02)

There is no GC — lifetime is refcounting. A container created inside a function and returned
by a **non-`ref`** return type has no owner at the moment of return, so it dies immediately
and the caller gets garbage:

```c
// BROKEN — the array is destroyed before the caller can iterate it
array<string> ThingsFor(string key)
{
    if (nothingApplies)
        return new array<string>();   // <-- no owner
    return m_SomeMember;              // fine: the member owns it
}
```

Fix: keep an empty instance as a member and return that.

```c
protected ref array<string> m_Empty = new array<string>();
...
    if (nothingApplies)
        return m_Empty;
```

Same trap with `out`/return of maps and any `ref`-managed object. Rule of thumb: a function
returning a container must return something that is **already owned** — a member, or a
parameter the caller owns.

Fix: the CALLER creates it and passes it in — `void Read(Pda pda, Book book)`.

## NEVER assign a compound `&&` / `||` expression straight into an object FIELD — it corrupts the native heap (measured 2026-09-01)

```c
// KILLS THE SERVER — measured, one line, reproducible on demand
row.Reach = window && e.Index >= lo && e.Index <= hi;

// SAFE — compute into a local, put the finished value in the field
bool reach = false;
if (window)
{
    if (e.Index >= lo && e.Index <= hi)
        reach = true;
}
row.Reach = reach;
```

`row` is a script object on the heap; `window`, `lo`, `hi` are ordinary locals. Writing the
chained boolean directly into the `bool` field corrupts the heap. The same expression
computed into a `bool` local and then stored is fine.

Isolated by halving on a deterministic repro (one button press killed a dedicated server
within a second). Everything else in the same loop was proved innocent, one step per server
run: `new` of the row object; `row.Name = e.Name` (string copied out of a `ref array<ref T>`
element); `row.Index = e.Index`; a 10 000-call `proto` grid scan plus a float write
(`row.MHz = Grid.MHzAt(...)`); `list.Items.Insert(row)`; `JsonFileLoader.MakeData` over the
non-empty array; and the CF `SendRPC` that shipped the identical 63-byte document. Each of
those ran clean. Re-enabling only the line above brought the crash straight back.

A relative of this was already measured in the same project on 2026-08-30: the Enforce parser
broke on a multi-line `if` condition ending in a trailing `||`. Treat compound boolean
expressions as fragile in general — evaluate them into a local first.

### How this failure presents (worth recognising for ANY heap corruption)

- `0xC0000005` reading address **0x24**, faulting inside ntdll's heap code (nearest exported
  symbol `RtlSizeHeap`). Faulting bytes `45 0F B7 49 24` = `movzx r9d,[r9+0x24]`, right after
  `xor ecx,[rip+…]` / `test cx,cx` / `jz` — the block header failed its checksum and the code
  followed a null pointer taken out of the corrupted header.
- Sometimes `0xC0000374` (STATUS_HEAP_CORRUPTION) in `RtlFreeHeap` / `free_base` instead.
- **The crash site wanders and is never the culprit.** Across nine crashed sessions it landed
  in `JsonFileLoader.MakeData`, in `JsonFileLoader.LoadData`, in `Print()` inside a logger's
  `Dbg()`, in `ErrorModuleHandler.OnEvent`, and with no script frame at all. Each is merely
  the next operation to touch the heap. **Do not bisect toward the crash line** — bisect the
  work that ran before it, by disabling halves.
- Latency from damage to crash ranged from ~1 s to 110 s depending on allocation traffic.
- An empty container makes the loop body never run, so **"empty works, one element kills it"
  says only that the loop body is guilty** — it is NOT evidence that the element's type,
  nesting, or serialisation form is at fault. Chasing the shape (nested `ref array` vs.
  standalone document, engine JSON vs. a hand-rolled format) cost a full day and changed
  nothing.
- Script-level probes still help, but read them correctly: every probe firing means the
  corruption happened *somewhere in that stretch*, not that the stretch is innocent.

### The same construct also lies QUIETLY — no crash, just the wrong answer (measured 2026-09-06)

```c
// WRONG — the field gets FALSE while both operands are TRUE
st.MeLeader = mine && MyMod_Roles.IsLeader(acting);

// RIGHT
st.MeLeader = false;
if (mine)
    st.MeLeader = MyMod_Roles.IsLeader(acting);
```

A factions stand mod, script log of 2026-09-06. `mine` is a `bool`
local, `MyMod_Roles.IsLeader` a static returning `bool`, `st` a script object on
the heap. A probe printed on the very next line, calling the same static a
second time:

```
PROBE state acting=765611980... uid=765611980... mine=true lead=true me=false
```

Both operands true, the field false. Split into the `if`, the identical probe
prints `me=true`. Nothing crashed, nothing was logged, and the only visible
symptom was a leader's own faction screen refusing to admit he was the leader.

So the 2026-09-01 rule is not only about heap corruption: **an `&&` whose right
operand is a CALL, assigned straight into an object field, can also simply
evaluate wrong.** Compute into a local, or branch.

## Debug logging must read the flag from the right side

A helper like `Log.Dbg()` that checks a SERVER-side config service prints nothing on the
client, because that service does not exist there — silently, forever, no matter what the
admin toggles. If a mod logs on both sides, the helper must consult the client's own synced
copy of the flag as well. This is worth stating because the failure looks like the logged
event never happened: an entire debugging cycle can be spent chasing "the RPC never arrives"
when the arrival log line simply could not print.

### `out` is a KEYWORD — never a variable name

```c
string out = "";        // Broken expression (missing ';'?)
```

The error names neither the word nor the reason; it is reported at the
DECLARATION (measured 2026-09-20; an earlier note here said "at the line that uses
the variable" — the 2026-09-20 measurement wins). `out` is the
parameter modifier (`void F(out string why)`), so the parser sees a modifier
where an expression belongs.

Cost this twice in one session on the same file. Anything that reads like an
"output" local — `out`, and check before reaching for `in`, `ref`, `proto`,
`owned`, `private`, `class` — wants a different name. `line`, `text`,
`result` all work.

## Strings are byte arrays: Substring can cut a UTF-8 character in half

`string.Length()` and `Substring()` count BYTES, and player text is UTF-8 —
every Cyrillic letter is two bytes. A blind `s.Substring(0, MAX)` length cap
halves the budget for Cyrillic ("Схрон біля Доброго" is 33 bytes) and, worse,
can land mid-character, leaving a lead byte with no continuation — undecodable;
Discord renders U+FFFD. Measured on a stand, in a mod's marker names.

Safe clip: cut at the byte cap, then walk back over UTF-8 continuation bytes
(`10xxxxxx`) to the character start. Byte access is `s.Get(i)` (one-byte
string, enstring.c:448) + `.ToAscii()` (int of first byte, enstring.c:86):

```c
static string Clip(string s, int maxBytes)
{
    if (s.Length() <= maxBytes)
        return s;
    int cut = maxBytes;
    while (cut > 0 && (s.Get(cut).ToAscii() & 0xC0) == 0x80)
        cut--;
    return s.Substring(0, cut);
}
```

## Substring OUTPUT is silently capped at 8191 bytes (measured 2026-08-28)

Measured with a probe on a stand (1.29 DIAG server):

- `s.Substring(off, n)` returns AT MOST **8191 bytes** (2^13 - 1; an 8 KiB
  buffer with a NUL). `Substring(0, 8191)` -> 8191, `Substring(0, 8192)` ->
  8191, `Substring(0, 65536)` -> 8191. No error anywhere.
- The INPUT may be huge and the offset deep: `Substring(500000, 900)` on a
  1 MiB string returns the correct 900 bytes. `s.Get(1000000)` also reads
  correctly. Only the OUTPUT length is capped.
- Concatenation is NOT capped: `s = s + s` doubled a string to 1 MiB, and
  `+=` joined 8000+8000 to 16000. Building big strings is fine; carving big
  pieces out of them is not.

Consequence: never materialise "the rest of the string"
(`s = s.Substring(cut, s.Length() - cut)`) in a loop over a long string --
past ~9 KiB the remainder silently loses its tail on the first iteration.
Walk an OFFSET and take pieces small enough to be under the cap instead.

## A script method named `Step` is never executed (measured 2026-09-02, build 1.29 retail client)

`class MyMod_PageRadio : MyMod_Page { private void Step(int dir) { ... } }` and a call
`Step(1)` from the same class: the call returned immediately, NOT ONE statement of the
body ran (an `Info` print as the very first line never appeared), and nothing was written
to the script log -- no compile error, no runtime error. Renaming the method to
`StepFreq` (and nothing else) made the identical body run. The likely cause is vanilla
`class Step` (2_gamelib/tests/testingframework.c:125, the `[Step(EStage.Setup)]` attribute
class): an unqualified `Step(1)` resolves to that class, not to the method — the same
collision that makes a FIELD named `Step` fail with "already used as type name" (SKILL.md
iron rules). The compiler does not warn. Do not name methods (or fields) after vanilla
classes; when a button "does nothing" with no log line at all, suspect the method NAME
before the body.

## Numeric promotion: int + float is float32 (measured 2026-09-20)

`int a = 212027504; float d = 10.0; int b = a + d;` gives `b = 212027520`, not
`212027514`: the sum is computed in float32 (24-bit mantissa), which cannot
represent integers above 16 777 216 exactly, and at ~2^28 the spacing is 16.
Anything built on calendar seconds (a station deadline, a project end, a
cooldown) drifts by 0..16 s per operation and the drift looks like a slow
timer. Convert the float first (`int t = d;` truncates), then add ints.
Comparisons (`now >= end`) and differences (`end - now`) between two ints are
exact; the trap is only the mixed-type expression.

## A long `+` chain is "Formula too complex" (measured 2026-09-26)

Merged 2026-11-01 into the "`Formula too complex`" table under "Logging" above (this
measurement: 14 terms with calls failed on the client; the guarded `#ifndef NO_GUI` half is
compiled only by `client_compile_check`).

## Two calls of one function in a single argument list: the second overwrites the first (measured 2026-09-26)

`Describe(e, Handle(box, e), ParentHandle(e), r)` where `ParentHandle` itself calls
`Handle(box, parent)` reached `Describe` with the PARENT's number in the first slot for every
nested item, and the right number for every root item (there `ParentHandle` returns 0 before
calling anything). The engine appears to hand a function's return value back through a slot
that the same function's next call reuses before the outer call has consumed it. Rule: never
put two calls of the same function, directly or through a callee, into one argument list;
look each value up into a local first (`int h = Handle(...); int ph = ParentHandle(e);
Describe(e, h, ph, r);`). The same shape was fine in a loop that computed the parent's number
into a local one statement earlier, which is how the fault hid for a week.

## An instance method that shares its name with a static method of the calling class is never reached (measured 2026-09-27)

Inside `static void Step(int budget)` of class A, `job.Step(budget)` on an object of class B
(whose `int Step(int)` is an ordinary method) compiled, ran every frame for thirty thousand
frames and never once executed B's Step: no side effect, no log line, the job's counters
untouched, no script error. Renaming either side (`Drive` / `Take`) made the identical code
work at once. Rule: when a class has a static method named X, do not call `obj.X()` from that
class on any object -- give the other method a different name. The same-name trap does not
need `super`, overloading or inheritance to bite.

