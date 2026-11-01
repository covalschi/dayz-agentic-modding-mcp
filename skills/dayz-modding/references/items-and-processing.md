# Items, containers, recipes & processing

How to define items (science devices, samples, data carriers), control what fits inside
them, and transform items into other items or rewards. Verified against 1.29 (124708); cited
vanilla lines re-checked against 1.30.164200 (`scripts.pbo` 127662) on 2026-11-01.

## Defining a new item (config.cpp → CfgVehicles)

```cpp
class CfgPatches
{
    class MyMod_Items { requiredAddons[] = {"DZ_Data", "DZ_Gear_Tools"}; };
};

class CfgVehicles
{
    class Inventory_Base;                       // external class declare

    class MyMod_Microscope : Inventory_Base
    {
        scope = 2;                              // 2 = spawnable/public
        displayName = "$STR_mymod_microscope";  // $STR_ prefix in config.cpp! or literal text
        descriptionShort = "$STR_mymod_microscope_desc";
        model = "\dz\gear\radio\WalkieTalkie.p3d";   // reuse a vanilla model (this is PersonalRadio's) or your own .p3d
        weight = 4000;                          // grams
        itemSize[] = {3, 3};                    // inventory cells W×H
        itemsCargoSize[] = {4, 3};              // it has cargo (container) — omit if none
        canBeDigged = 0;
        rotatable = 1;
        physLayer = "item_large";
    };
};
```

Notes:
- Stringtable key prefixes differ by context (verified: production configs use
  `displayName = "$STR_PDAName";` — DLCSlots):

  | Context | Prefix |
  |---|---|
  | config.cpp (`displayName`, `descriptionShort`, ...) | `$STR_name` |
  | scripts (`m_Text`, `SetText`, ...) and .layout files | `#STR_name` |
  | inputs.xml `loc=` attribute | bare key, no prefix |

- With no custom model, reuse vanilla `model=` paths (grep the extracted game data or the
  wiki for `.p3d` paths). Retexture-only variants use `hiddenSelectionsTextures[]`.
- The script class (4_World) must match the classname:
  `class MyMod_Microscope : ItemBase { ... }` — actually inherit the script base matching
  the config base (`Inventory_Base` configs pair with `ItemBase` scripts).
- Container/furniture donors: extend the vanilla SCRIPT class matching the config parent,
  not bare `ItemBase` — vanilla behaviors live script-side. Example: barrels' open/close,
  netsync and persistence live in `class Barrel_ColorBase : DeployableContainer_Base`
  (4_World/Entities/ItemBase/Barrel_ColorBase.c:1). Locate the script class by grepping
  4_World for the config parent name; add the owning data addon to `requiredAddons[]`
  (e.g. `"DZ_Gear_Containers"` for barrels).
- Attachments-based devices: define `class DefaultSlot;`-style attachment slots via
  `attachments[] = {"MySlotName"};` + `CfgSlots` entry; then gate with
  `CanReceiveAttachment`. Cargo-based devices just need `itemsCargoSize[]`.
- Vanilla bug T148506 (community-reported, Bohemia ticket, won't-fix): if the vanilla item
  declares `inventorySlot = "X";` as a STRING, a modded `inventorySlot[] += {"MyMod_Slot"};`
  is silently dropped at config parse — no RPT error, the item just never fits the slot.
  Workaround: redeclare the full array including the original value:
  `inventorySlot[] = {"X", "MyMod_Slot"};`.

### Static container config checklist (community-reported, unverified)

Silent-failure omissions reported for custom `Container_Base`/static device configs —
canonical full pattern: vanilla WoodenCrate in `DZ\gear\camping\config.cpp`:

- A `class Cargo` block may be required even for non-cargo containers on some engine
  builds (`itemsCargoSize[] = {0,0}; openable = 0;`) — or inherit `Inventory_Base` instead.
- Without a `class GlobalArmor` block (damage classes keyed by CfgAmmo classnames; field is
  `damage =`, not `hit =`) projectile impacts may not register — "bullets pass through"
  despite a valid FireGeometry.
- `healthLevels[]` must be NESTED inside `class DamageSystem > class GlobalHealth >
  class Health`; declared at config root it parses but does nothing, and legacy
  `healthLevelValues[]` silently breaks.

## Inventory gating (what fits into the device)

Verified hooks on ItemBase (ItemBase.c:4140+ in 1.30):

```c
class MyMod_Microscope : ItemBase
{
    override bool CanReceiveItemIntoCargo(EntityAI item)
    {
        if (!super.CanReceiveItemIntoCargo(item)) return false;
        return MyModSettings.Get().IsAcceptedInput("microscope", item.GetType()); // admin config decides
    }

    override bool CanReceiveAttachment(EntityAI attachment, int slotId)
    {
        if (!super.CanReceiveAttachment(attachment, slotId)) return false;
        return MyModSettings.Get().IsAcceptedInput("microscope", attachment.GetType());
    }
}
```

`GetType()` returns the config classname string — the key admins use in JSON config.
Also available: `CanPutInCargo(parent)` / `CanPutAsAttachment(parent)` (on the *item being
inserted* side) and `CanReleaseCargo`, `CanLoadItemIntoCargo`.

## Quantity, health, wetness

```c
item.GetQuantity(); item.GetQuantityMax(); item.SetQuantity(x);
item.AddQuantity(-25);                        // consume part of a stack/liquid
item.GetHealth01("", "Health");               // 0..1
item.GetHealthLevel("");                      // 0 pristine .. 4 ruined
item.IsRuined();
item.SetHealth("", "", newHealth);
item.GetWet(); item.AddWet(delta);
```

Damage states matter for processing yield — see vanilla `CraftTannedLeather.CanDo/Do`
(`4_World/Classes/Recipes/Recipes/CraftTannedLeather.c`) which scales output by pelt health
and consumes a percentage of lime quantity. Copy that yield-math pattern for balancing.

## Transforming items

```c
// In-place replace, preserving location (hands/inventory), server-side:
MiscGameplayFunctions.TurnItemIntoItem(oldItem, "MyMod_PreparedSlide", player);
// (4_World/Static/MiscGameplayFunctions.c:456 in 1.30; Ex variant takes a lambda for fine control)

// Delete + spawn separately (PlayerBase.c lines are 1.30):
GetGame().ObjectDelete(oldItem);
player.SpawnEntityOnGroundPos("MyMod_ResearchPoint", player.GetPosition()); // PlayerBase.c:6741
player.SpawnEntityOnGroundOnCursorDir("MyMod_ResearchPoint", 1.0);          // :6801
EntityAI reward = player.CreateInInventory("MyMod_ResearchPoint");           // :6846 — returns NULL if full!
// WARNING (verified 1.29 (124708), unchanged in 1.30): CreateInInventory does NOT fall back to
// ground — the vanilla doc comment at playerbase.c:6837 is stale; the implementation returns NULL
// when FindFirstFreeLocationForNewEntity fails. Canonical fallback (miscgameplayfunctions.c:1711):
// if (!reward) reward = player.SpawnEntityOnGroundOnCursorDir("MyMod_ResearchPoint", 0.5);
// On a PLAYER the fallback is different (verified 2026-09-08, humaninventory.c:65):
// HumanInventory.CreateInInventory tries GameInventory.CreateInInventory (inventory.c:876,
// which searches CARGO | ATTACHMENT -- so a wearable may be created WORN), and when that
// returns null it calls CreateInHands. "Spawn into the inventory" can therefore land in the
// hands or on the body; read the item's InventoryLocation afterwards instead of assuming.

// Spawn piles with spread (used by vanilla crafting results):
MiscGameplayFunctions.CreateItemBasePilesDispersed("TannedLeather", posHead, posTarget,
    UAItemsSpreadRadius.NARROW, quantity, float.MAX, player);

// Spawn inside a container entity:
device.GetInventory().CreateInInventory("MyMod_SampleResidue");

// World-spawn (server): GetGame().CreateObjectEx("ClassName", pos, ECE_PLACE_ON_SURFACE);
```

All spawning/deleting is server-only. Wrap with `if (!GetGame().IsServer()) return;`.

### ECE creation flags (verified values, 3_Game/CE/CentralEconomy.c:7-41 in 1.30)

For `CreateObjectEx` / `SpawnEntity` variants; OR-combine as needed:

| Flag | Value | Effect |
|---|---|---|
| `ECE_TRACE` | 4 | trace under entity when placing |
| `ECE_CREATEPHYSICS` | 1024 | create collision envelope / physics |
| `ECE_EQUIP_ATTACHMENTS` / `ECE_EQUIP_CARGO` / `ECE_EQUIP` | 8192 / 16384 / 24576 | spawn with configured attachments / cargo / both |
| `ECE_NOSURFACEALIGN` | 262144 | do not align to terrain |
| `ECE_KEEPHEIGHT` | 524288 | keep given height, no surface trace |
| `ECE_NOLIFETIME` | 4194304 | "do not set lifetime when creating the object" (:30) — no CE despawn timer; consider for research outputs awaiting pickup |
| `ECE_NOPERSISTENCY_WORLD` / `ECE_NOPERSISTENCY_CHAR` | 8388608 / 16777216 | excluded from world / character saves |
| `ECE_DYNAMIC_PERSISTENCY` | 33554432 | spawns without persistency; enabled once a player takes it (:33) |
| `ECE_LOCAL` | 1073741824 | create object locally only (client visual — wrong side = desync) |
| composites | | `ECE_PLACE_ON_SURFACE` = 1060, `ECE_IN_INVENTORY` = 787456, `ECE_OBJECT_SWAP` = 787488 (:37-39) |

**A mod's own class placed in the world is removed by the central economy unless it is given a
lifetime** (measured 2026-09-17, cost a player's box overnight). `ECE_PLACE_ON_SURFACE` (1060)
does NOT include `ECE_NOLIFETIME` (4194304), so a deployed object keeps its config `lifetime` and
the CE cleanup takes it at the next sweep. A class absent from `types.xml` has no CE entry to
raise that value, so the object must raise it itself: `SetLifetime(seconds)` at creation AND
again after every load, because a loaded object starts its timer from the save. Renew it in
`EEOnAfterLoad` (server side), not in `EEInit`: the engine restores the saved lifetime AFTER
`EEInit` and overwrites a renewal made there (measured 2026-09-25). `EEDelete`
is the only hook that sees a world object leave; without a line there, a vanished object leaves no
trace to debug.

**`ECE_PLACE_ON_SURFACE` settles a spawn only from near the surface** (measured 2026-10-30, retail
1.30, 39 static stations of an `Inventory_Base` class on the Chernarus NW airfield, concrete at
339.0). Asked at `y = 0`, 339 m under the ground, every one stayed at exactly `y = 0`, invisible;
asked at `y = 339.03`, each settled on the surface, 0 to 7 cm up or down (339.000-339.095). So
`y = 0` does not mean "on the ground" for every class: give the real height, or take it from
`GetGame().SurfaceRoadY(x, z)` (proto native, 3_game/global/game.c:1177 in 1.30, counts roads
and runway concrete; its third parameter `RoadSurfaceDetection rsd` defaults to `LEGACY`, so
`(x, z)` still works) before the call. Yaw in `SetOrientation` is degrees clockwise from north:
`"1 0 1".VectorToAngles()` is `<45,0,0>` (1_core/proto/enconvert.c:346).

Ownership checks for `ActionCondition` ("is the input item inside THIS device?"):
`item.GetHierarchyRootPlayer()` — owning player or null (3_Game/Entities/EntityAI.c:882 in 1.30),
`item.GetHierarchyParent()` — direct container entity (:885). Both proto native.

## Crafting recipes (drag-item-onto-item crafting)

Vanilla "combine two items in inventory/hands" crafting. Good for simple conversions
that do NOT need a placed device; chains through devices should use Actions instead.

```c
// 4_World — the recipe:
class MyPreparationRecipe : RecipeBase
{
    override void Init()
    {
        m_Name = "#mymod_prepare_sample";       // shown in crafting UI
        m_IsInstaRecipe = false;
        m_AnimationLength = 1;                  // relative units
        m_AnywhereInInventory = true;

        InsertIngredient(0, "MyMod_MutantTailAny");   // ingredient slot 0 (accepts subclasses!)
        m_IngredientDestroy[0] = true;
        m_MinDamageIngredient[0] = -1; m_MaxDamageIngredient[0] = 3;

        InsertIngredient(1, "MyMod_PetriDish");
        m_IngredientDestroy[1] = false;

        AddResult("MyMod_PreparedSample");
        m_ResultSetFullQuantity[0] = true;
    }

    override bool CanDo(ItemBase ingredients[], PlayerBase player) { return true; }

    override void Do(ItemBase ingredients[], PlayerBase player, array<ItemBase> results, float specialty_weight)
    {
        // optional custom logic on completion (server); results already spawned
    }
}

// Registration — modded plugin (pattern from Bohemia's Test_Crafting sample):
modded class PluginRecipesManagerBase
{
    override void RegisterRecipies()
    {
        super.RegisterRecipies();
        RegisterRecipe(new MyPreparationRecipe);
        // UnregisterRecipe("CraftStoneKnife");  // removing vanilla recipes also possible
    }
}
```

`InsertIngredient` accepts base classes — `"Edible_Base"` matches all food. Useful for
admin-configurable frameworks: generate/check recipes dynamically instead of hardcoding.
Recipe fields: `m_MinQuantityIngredient[i]`, `m_MaxDamageIngredient[i]`,
`m_IngredientAddHealth[i]`, `m_IngredientAddQuantity[i]`, `m_IngredientUseSoftSkills[i]`,
`m_Specialty` (soft-skills rough/precise), `AddResult` + `m_ResultSetQuantity[i]`.
Access the manager at runtime: `PluginRecipesManager.Cast(GetPlugin(PluginRecipesManager))`.

Result placement — `m_ResultToInventory[i]` (RecipeBase.c:59). Vanilla recipes' own comment
(e.g. CraftArrowBone.c:53): `-2` = spawn result on the ground; `-1` = place anywhere in the
player's inventory; `>= 0` = switch position with ingredient number(value). 1.29 (124708)
reality, unchanged in 1.30 (`RecipeBase.SpawnItems`, :232-:261): `-1` → `player.GetInventory().CreateInInventory()`;
the `>= 0` swap branch is commented out (currently a no-op); every other value (incl. `-2`)
and any failed inventory placement falls through to
`SpawnEntityOnGroundRaycastDispersed(item, m_ResultSpawnDistance[i])`, `Error()` if that
fails too. Always set it explicitly per result. Actual craft duration =
`m_AnimationLength * 4.0` seconds (read 2026-11-01: recipebase.c:97 returns
`m_AnimationLength * CRAFTING_TIME_UNIT_SIZE`, and constants.c:675 sets that to 4.0).

## Powered devices (optional realism: lab needs electricity)

`ComponentEnergyManager` (3_Game/tools/Component/ComponentEnergyManager.c). Config:

```cpp
class MyMod_Centrifuge : Inventory_Base
{
    class EnergyManager
    {
        hasIcon = 1;
        energyUsagePerSecond = 0.5;
        plugType = 2;                 // 2 = PLUG_COMMON_APPLIANCE: plugs into a socket (generator, cable reel)
        // battery-powered instead: plugType = 1 (PLUG_BATTERY_SLOT) + attachmentAction = 1
        // (PLUG_THIS_INTO_ATTACHMENT: plug into the battery attached to it)
    };
};
```

Constants (3_game/tools/component/_constants.c:25-38): plugType 0 `PLUG_UNDEFINED`,
1 `PLUG_BATTERY_SLOT`, 2 `PLUG_COMMON_APPLIANCE`, 3 `PLUG_CROCODILE_PINS`, 4
`PLUG_CROCODILE_PINS_CHARGER`, 5 `PLUG_PAS_DEVICE`, 6 `PLUG_BARBED_WIRE`, 7
`PLUG_GAS_CAN_SLOT`, 8 `PLUG_VEHICLE_BATTERY_PINS`; attachmentAction 1
`PLUG_THIS_INTO_ATTACHMENT`, 2 `PLUG_ATTACHMENTS_INTO_THIS`.

Script: `item.GetCompEM()` → `.SwitchOn()` / `.SwitchOff()` / `.IsWorking()` /
`.CanWork()` (:768 in 1.30) / `.HasEnergy()`. Gate processing on `device.GetCompEM().IsWorking()`
in `ActionCondition` for "device must be powered" gameplay. Vanilla examples: grep
`GetCompEM()` usages in 4_World entities.

## Custom sounds for devices (running / finish feedback)

config.cpp — shader + set pair (sample path WITHOUT the .ogg extension):

```cpp
class CfgSoundShaders
{
    class MyMod_Hum_SoundShader
    {
        samples[] = { {"MyMod\sounds\hum_01", 1} };          // path, weight
        volume = 0.8;
        range = 50;
    };
};
class CfgSoundSets
{
    class MyMod_Hum_SoundSet
    {
        soundShaders[] = {"MyMod_Hum_SoundShader"};
        sound3DProcessingType = "character3DProcessingType";
        volumeCurve = "characterAttenuationCurve";
        spatial = 1;                                        // 3D positional
        loop = 1;
    };
};
```

- 3D (`spatial = 1`) samples must be MONO .ogg — stereo breaks spatialization
  (community-reported, unverified). Add `"DZ_Sounds_Effects"` to `requiredAddons[]`
  (community-reported).
- On any Object (verified, 3_Game/Entities/Object.c, 1.30 lines):
  `PlaySoundSet(out EffectSound snd, "MyMod_Hum_SoundSet", fadeIn, fadeOut, loop = false)` (:1247),
  `PlaySoundSetLoop(...)` (:1273), `StopSoundSet(snd)` (:1314). `PlaySoundSet`/`Loop` carry a
  built-in `!g_Game.IsDedicatedServer()` guard (:1249) and call `SetAutodestroy(true)` for you
  (:1264) — prefer them. `PlaySoundSetAtMemoryPoint(...)` (:1294) does NEITHER (1.29 and 1.30):
  it returns `SEffectManager.PlaySoundEnviroment(...)` as is, so guard it yourself and call
  `snd.SetAutodestroy(true)` like the SEffectManager calls below.
- From anywhere: `SEffectManager.PlaySound("MyMod_Hum_SoundSet", pos)` (3_Game/EffectManager.c:178),
  `PlaySoundOnObject(set, obj, ...)` (:256) — with these you MUST call
  `snd.SetAutodestroy(true)` yourself, or the effect stays registered in SEffectManager
  until mission end (leak; see comment at :41). Guard with `!GetGame().IsDedicatedServer()`.
- Looped sounds never auto-end: `StopSoundSet` on power-off and in `EEDelete`/destructor.
- Drive state audio client-side from `OnVariablesSynchronized()` off netsync device state.

## Making items spawn in the world (Central Economy)

New items exist only if the **mission** economy knows them:

1. In the mission folder, `cfgeconomycore.xml` declares extra CE folders:
   ```xml
   <ce folder="mymod_ce">
       <file name="mymod_types.xml" type="types" />
   </ce>
   ```
2. `mymod_ce/mymod_types.xml` — one `<type>` per spawnable item:
   ```xml
   <types>
       <type name="MyMod_Microscope">
           <nominal>4</nominal>          <!-- target count in world -->
           <min>2</min>
           <lifetime>28800</lifetime>    <!-- seconds until cleanup -->
           <restock>1800</restock>
           <cost>100</cost>
           <flags count_in_map="1" deloot="0"/>
           <category name="tools"/>
           <usage name="Industrial"/>    <!-- spawn location tags -->
       </type>
   </types>
   ```
3. Attachments/cargo presets per spawn: `cfgspawnabletypes.xml`. Trader/quest-given items
   don't need types.xml, but anything looted or persistent-limited does.
4. Admin-spawned/static placements: server addons like VPPAdminTools or init.c
   `GetGame().CreateObjectEx` calls in the mission.

Caveat (wiki): CE file *definitions append*; a modded types entry with the same name
overrides the vanilla entry entirely, and defining attachments/cargo overrides all
originals for that type.

## Chains & framework thinking (design advice)

A processing chain is pure data once each step is expressed as
`{deviceClass, inputClass, outputs[], time, chance, consumesInput, requiresPower}`:
the output classname of step N is the accepted input of step N+1. Implement ONE generic
`ActionProcessInDevice` + ONE generic device base class reading a JSON ruleset, instead of
one action per device. Admin config then wires any items — vanilla, STALKER packs, mutant
parts — into chains without new code. Validate configs at server start and `Error()` loudly
on unknown classnames (`GetGame().ConfigIsExisting("CfgVehicles " + cls)` to verify).

Config path grammar: all `ConfigIsExisting` / `ConfigGetText` / `ConfigGetFloat` /
`ConfigGetTextArray`... paths are SPACE-separated —
`GetGame().ConfigGetText("CfgVehicles " + cls + " displayName", name)` (`string name;` —
never call a variable `out`, it is a keyword: enforce-script.md). A dotted path
silently returns nothing (lookup just misses, no error raised).

## Inventory hooks: which ones are safe to block (verified 2026-08-02)

### NEVER block the RECEIVE hooks — Bohemia's own comment says why

`itembase.c:4171` and `:4180` (1.30; 4193/4202 in 1.29) carry the same disabled code with the note
`//removed 15.06. coz of loading from storage -> after load items in cargo was lost`.
Blocking `CanReceiveItemIntoCargo` / `CanReceiveAttachment` conditionally is therefore a
data-loss risk: the engine calls them **while loading an entity from storage**, and a `false`
there can drop the cargo. If your condition depends on entity state (e.g. "device is busy"),
the state is exactly what storage is restoring at that moment.

Second, subtler failure: a device that blocks its own cargo cannot receive items **from its
own script either**. `GameInventory.CreateInInventory` (inventory.c:876) resolves a slot via
`FindFirstFreeLocationForNewEntity`, and the engine consults the script condition
(`scriptConditionExecute(this, item, "CanReceiveItemIntoCargo")`, entityai.c:1576 in 1.30). A station
that produced output while still flagged "busy" therefore failed to place it into its own
empty cargo and fell through to a ground drop — every single time, with no error.

### Server-side moves land AFTER the frame that asked (measured 2026-09-07/08 on 1.29)

**Use the 1.30 Target forms.** 1.30 marks the old names `[Obsolete]`, and every call or
override of one prints a compile warning (persistence-networking.md, 1.30 sections):

| 1.29 call (obsolete in 1.30) | 1.30 replacement |
|---|---|
| `EntityAI.ServerTakeEntityAsAttachmentEx(item, slot)` — `[Obsolete]` entityai.c:4885; the `Man` override (1.29 man.c:508) is gone | `ServerTakeEntityToTargetAttachmentEx(target, item, slot)` (entityai.c:2031, man.c:677) |
| `EntityAI.ServerTakeEntityToCargo(item)` — `[Obsolete]` entityai.c:4848; the `Man` override (1.29 man.c:430) is gone | `ServerTakeEntityToTargetCargo(target, item)` (entityai.c:1999, man.c:634) |
| `Man.ServerTakeEntityToInventory(flags, item)` (man.c:402; obsolete note commented out at :401) | `ServerTakeEntityToTargetInventory(target, flags, item)` (entityai.c:1983) |
| `GameInventory.TakeEntityAsAttachmentEx(mode, item, slot)` — `[Obsolete]` inventory.c:1374 | `TakeEntityAsTargetAttachmentEx(mode, owner, item, slot)` (inventory.c:1100) |
| `GameInventory.TakeEntityToCargo` / `TakeEntityToCargoEx` — `[Obsolete]` inventory.c:1345/:1357 | `TakeEntityToTargetCargo(mode, owner, item)` (inventory.c:1077) / `TakeEntityToTargetCargoEx(mode, cargo, item, row, col)` with a `CargoBase` (:1084) |

`ServerDropEntity` (man.c:147) is not obsolete. Measured on 1.29: these calls answered a bool
at once, and the move was applied later: reading the item's location in the same frame still
showed the OLD place (the slot still held the battery after `true`; the next command found it
moved). A move whose source or destination is the HANDS goes through the hand state machine
and lands on a later command-handler frame, later still when the machine is busy. On 1.29
`HumanInventory` overrode `TakeEntityAsAttachmentEx`/`TakeEntityToCargoEx` to post a
`HandEventMoveTo`; 1.30 removed those overrides, and the hands path is now
`HumanInventory.TakeToDst` → `RedirectToHandEvent` (humaninventory.c:243-251, :200-241:
`HandEventMoveTo` from hands, `HandEventTake` into hands); `TakeEntityToInventory` still
redirects the same way (:254). The timing has not been re-measured on 1.30. Judge a move by
re-reading the location on a later tick with a bounded wait, never by the call's bool: with a
full inventory the call answers `false` and the item simply stays where it was.

### `CanReleaseAttachment` IS safe to block

The storage warning covers receive paths only; release is not called during load. This is the
right hook for "you may not pull the tool out while the machine is running". Block narrowly —
only the specific attachment the current operation needs, and only while running — otherwise
the item stays locked after a config change.

### `EnumerateInventory` returns ATTACHMENTS too

`GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items)` on a device yields
cargo contents **and** items in attachment slots. A recipe whose input/consumable classname
matches a tool will happily eat the machine's own tool. Filter explicitly:

```c
protected static bool IsDeviceAttachment(EntityAI device, EntityAI candidate)
{
    int n = device.GetInventory().AttachmentCount();
    for (int i = 0; i < n; i++)
        if (device.GetInventory().GetAttachmentFromIndex(i) == candidate)
            return true;
    return false;
}
```

## Moving items by script: what the engine answers vs what it does (measured 2026-09-25)

All of this was measured on an UNANNOUNCED container (`ECE_LOCAL |
ECE_NOPERSISTENCY_WORLD`, netid 00) used as the authority of a storage mod, on
the retail server build. Several of them cost a working day each, and every one
of them answers `true` while doing something else.

### `TakeToDst` into a TAKEN cargo cell: true, and nothing happens

`GameInventory.TakeToDst(InventoryMode.LOCAL, src, dst)` onto a cell another
item stands on returns **true** and leaves the item where it was -- or slides it
into the first free cell, which is at the top of the grid. The same call to an
EMPTY cell reports itself correctly, so it is the taken cell the engine refuses,
not the reading that is unreliable. Two items of equal footprint get an implicit
exchange and look fine, which is why this hides until somebody drags a 1x2 onto
a 2x1.

**Do:** empty the target rectangle first (park whoever is there), then move.
Never hand `TakeToDst` a cell you have not confirmed is free.

### `FindFirstFreeLocationForNewEntity` proposes a cell that is not one -- and using it KILLS THE SERVER

On an unannounced container it answers **true** and fills in cell **0,0**
whatever is standing there. `LocationCanAddEntity` on the very same location
then answers **false** -- the engine contradicting itself -- and handing that
location to `TakeToDst` ends the server process: **no crash dump, no Windows
event, the script log stops mid-line.** Reproduced five times with different
items and different sources (ground, cargo of a shirt, another container).

**Do:** walk the grid yourself and ask `LocationCanAddEntity` per candidate
cell, both orientations, and stop at the first yes. Never move into a
destination the engine has not approved.

### `CreateEntityInCargoEx` returns the entity whether or not it honoured the cell

Asked for 6,4 turned, in a box where 6,4 was empty, it made the item at 6,3 --
and the next item then landed on top of it. Read the location back after every
creation and correct it.

### The two slot tables, and why a container refuses every attachment

The engine keeps two lists with nearly identical names, and its own headers
say which is which:

| Asked of an entity | Means |
|---|---|
| `GetSlotIdCount()` / `GetSlotId(i)` / `HasInventorySlot(id)` | slots **this item can hang in** |
| `GetAttachmentSlotsCount()` / `GetAttachmentSlotId(i)` / `HasAttachmentSlot(id)` | slots **this container offers** |

Asking a container `HasInventorySlot` answers **no** for every slot it
declares, and `GetSlotIdCount()` answers 1 (where the container itself could
hang). A box whose config declared four weapon slots refused every weapon until
the questions were swapped.

### Slot ids are NEGATIVE hashes -- `slot < 0` is not "no slot"

`MyMod_Weapon_4` resolves to a negative hash (`-692829675` for the slot name that
was measured; another name gives another value). Testing `slot < 0` for "the caller did
not name a slot" is true for every real slot. Use `InventorySlots.INVALID` and
pass it explicitly; never use `-1` as a sentinel.

### `TakeToDst` ignores the slot in an attachment destination

Given an attachment location naming slot 4, it hangs the item on the FIRST free
slot the item fits. On 1.29 `TakeEntityAsAttachmentEx(mode, item, slot)` on the
PARENT's inventory was the call that is told which slot; on 1.30 that name is `[Obsolete]`
(inventory.c:1374) and is a pure delegation to `TakeEntityAsTargetAttachmentEx(mode, owner,
item, slot)` (inventory.c:1100) — call that one. Verify afterwards
against the CONTAINER's table (`FindAttachment(id) == item`), not against the
item's own `GetCurrentInventoryLocation`.

### A LOCAL move does not ask the script gates -- ask them yourself (measured 2026-09-27)
- A server-side move with a named cell (`TakeToDst(InventoryMode.LOCAL, src, dst)` after
  the mod's own footprint checks) put a `Rag` into a bag whose `CanReceiveItemIntoCargo`
  returns true for `BandageDressing` alone. Nothing on that path runs the script gates:
  `CanReceiveItemIntoCargo` / `CanPutInCargo` (and `CanReceiveAttachment` /
  `CanPutAsAttachment`) are asked by the vanilla SCREEN, through the native
  `GameInventory.CanAddEntityInCargo[Ex]` (measured: false for the rag, true for the
  bandage, on a client-local mirror container), and the client's
  `PredictiveTakeEntityToTargetInventory` returns false when that gate says no -- the
  server never even sees such a request from the vanilla UI.
- So a mod that moves items by its own protocol must ask the four gates itself before
  any LOCAL move, item first then holder, exactly as vanilla composes them; a refusal is
  cheap and the alternative is a container holding what its own script forbids.
  Leave RESTORE / load paths ungated, as vanilla leaves
  `CanLoadItemIntoCargo` open: what a load gate refuses is lost, not parked.
- Vanilla receivers that gate by ITEM in script, for reference: `Pot`/`Cauldron`/
  `FryingPan` (`IsCargoException4x3` + any item with a slot the vessel fits),
  `PlateCarrierPouches` (no filled cargo unless `IsContainer()`), `Container_Base` and
  `Clothing` (nesting rules); by STATE only: barrels, gift box, scientific briefcase,
  tents, deployable containers, fireplace. 55 vanilla `CanPutInCargo` overrides on the
  item side (barrel with contents, tents, explosives, garden plots ...).

### The panel draws a magazine's number from `GetAmmoCount()` and nothing else

`QuantityConversions.GetItemQuantityText` and `GetItemQuantity` both branch on
`IsMagazine()` and return `GetAmmoCount()`. An ammo pile carries **quantity 1**
and its rounds in the ammo count (measured live: `qty 1  ammo 20/20`). So:

* a stack emptied by combining is **not** caught by `GetQuantity() <= 0` -- it
  leaves a pile of zero rounds behind;
* a client-side copy that never receives the ammo count draws whatever a FRESH
  pile of that class holds;
* `LocalSetAmmoCount(n)` is the client-side setter; `ServerSetAmmoCount` does
  nothing on a client.

`Magazine.CanBeSplit()` is `canBeSplit` from the class config **and**
`GetAmmoCount() > 1`, which is why weapon magazines never divide and ammo piles
do. `Magazine` overrides `SplitItemToInventoryLocation` to move half the
cartridges ONE AT A TIME with their own damage and type; the stack-max split
works on quantity and therefore does nothing to a pile.

### Right-click split never touches the split methods

`Icon.MouseClick` calls `ItemBase.OnRightClick`, which on a client does not
split: it works out a destination and sends the request to the server as a
`ScriptInputUserData` naming the item **by entity**. An item with no network id
cannot ride that message, so the click does nothing and says nothing.

### A script move into a container created in the same frame

Still true (see the ghost note in persistence-networking.md), and it has a
second face: an item CREATED and MOVED in the same frame can end the server the
same way a bad destination does.

### `ECE_LOCAL` does not stop an object being SAVED

The two are separate flags and they answer separate questions: `ECE_LOCAL`
means "never tell the network about this", `ECE_NOPERSISTENCY_WORLD` means
"never write this into the world file". A temporary object created with only
`ECE_LOCAL` is invisible to clients *while it lives* and still goes into the
save — so a world save that lands in the frame it exists (autosave, an orderly
shutdown, a crash) resurrects it on the next boot as an ordinary, ANNOUNCED
item, at the position it was created.

Measured 2026-09-25 (a storage stand mod): a restore built containers-with-cargo
on the ground two metres from the box for one frame before moving them inside,
with `ECE_LOCAL | ECE_PLACE_ON_SURFACE | ECE_NOLIFETIME`. Copies accumulated
beside the box across restarts, one per save that caught the frame, each also
still listed in the mod's own record — a duplicate the mod could not see,
because on its side the item was inside the container. Any scratch object a
script makes needs BOTH flags.

### Deleting a container fires `EECargoOut` for every item in it

`ObjectDelete` on a container walks its cargo and calls `EECargoOut` (and
`EEItemDetached` for attachments) once per child, with nothing to distinguish
that from a player emptying it. A mod that audits those hooks writes one
"taken" row per item every time it tears a container down: a 121-item container
released at the end of a session produced 121 phantom take events in the admin
history (measured 2026-09-25, storage stand mod). Set a "this is a teardown"
flag on the container before deleting it and check it in the audit hook — the
same shape as the `m_Restoring` flag most mods already have for the fill side.

## Verify every `model=` path before shipping — a missing model CRASHES the client

An item whose `model` points at a non-existent p3d spawns fine and looks fine in config, but
taking it **into hands** crashes the client with `ACCESS_VIOLATION`. The script log shows
only unrelated vanilla noise, so this reads as "random crash".

One-line check against the game's PBO file tables (they are plaintext in the header):

```bash
cd "<DayZ>/Addons" && grep -ail "toolbox" *.pbo
```

Always sanity-check the method with a path you know works (e.g. `Protector_Case` → found)
before trusting a negative result. In 1.29 (124708) `\dz\gear\tools\Toolbox.p3d` does NOT
exist, while `RemoteDetonator_Receiver`, `Syringe_Full`, `Chemlight`, `Protector_Case` do. In
1.30 no `PersonalRadio.p3d` exists anywhere; PersonalRadio's model is
`\dz\gear\radio\WalkieTalkie.p3d` (checked 2026-11-01 against the Addons pbo headers).

## Making an item a radio: `simulation=`, not the script class

An entity's NATIVE type comes from the config field `simulation`, and from
nothing else. Not from the config parent, and not from the script class.

```cpp
class My_RadioBoard : Some_Base
{
    scope = 2;
    simulation = "itemTransmitter";   // <- the line that matters
};
```

```c
class My_RadioBoard extends TransmitterBase { }   // without the line above: ignored
```

Measured on 1.29: with `simulation` missing, the entity comes out a plain
`ItemBase` however faithfully its script class extends `TransmitterBase` —
silently. `ItemTransmitter.Cast(entity)` returns null, `ClassName()` reports
`ItemBase`, nothing is logged, and the item works as an ordinary object. Since
voice over radio is routed by the engine looking for a transmitter the player
carries, a board the engine does not consider a transmitter carries no voice at
all.

Reparenting the config to vanilla's `Transmitter_Base` does NOT help: that class
is an empty `Inventory_Base` with `scope=0` and carries no type information. The
vanilla `PersonalRadio` config declares `simulation="itemTransmitter"` itself.

Two consequences worth knowing:

* **A persisted item is re-created from the CURRENT config on load.** Fixing the
  config fixed the boards that were already lying in the world; no wipe needed.
* **A transmitter that gets switched on starts broadcasting on its own.**
  `SwitchOn(true)` is enough to put it on the air, so a push-to-talk device has
  to call `EnableBroadcast(false)` explicitly every time it syncs its power
  state — otherwise it transmits everything its owner says.

## Everything in the inventory really is an ItemBase (read 2026-09-22)

`4_world/entities/game/super/inventoryitem.c:4`:

```c
typedef ItemBase InventoryItemSuper;
```

So the classes that look like separate branches are not: `ItemTransmitter extends
InventoryItemSuper` and `Weapon extends InventoryItemSuper` both resolve to `ItemBase`
(`4_world/entities/core/inherited/inventoryitem.c:14`,
`4_world/entities/core/inherited/weapon.c:4`), and `Weapon_Base extends Weapon`
(`4_world/entities/firearms/weapon_base.c:38`). `ItemBase extends InventoryItem`
(`4_world/entities/itembase.c:23` in 1.30), `InventoryItem extends EntityAI`
(`3_game/entities/inventoryitem.c:1`).

Consequence worth remembering before writing `modded class ItemBase`: the override lands
on clothing, food, magazines, radios AND every firearm. A per-frame body there runs for
every item in the world.

## The inventory screen's gestures on PC (read from source 2026-09-26, the hooks measured)

- **There is no keyboard "quick transfer".** `UAUIFastTransferItem` (take to inventory)
  and `UAUIFastTransferToVicinity` (drop) exist as inputs and `inventory.c:906-1000` reads
  them, but `dta/bin.pbo`'s inputs bind them on the console presets only (`ps4X` click /
  hold, `x1X`); the keyboard preset lists them in the inventory context without a key. So
  on PC the calls behind them (`CargoContainer.TransferItem` ->
  `player.PredictiveTakeEntityToInventory(CARGO, item)`) are reached by nothing.
- **The one PC modifier click is Ctrl+LMB = drop to the ground**, and it is not an input
  at all: `g_Game.IsLeftCtrlDown()` inside the LEFT case of five click handlers --
  `Icon.MouseClick` (cargo icons and the hands preview), `PlayerContainer.MouseClick`
  (worn slots), `ContainerWithCargoAndAttachments.MouseClick2` (a container's own slots),
  `AttachmentCategoriesRow.MouseClick`, `HandsContainer.MouseClick2`. A modded gesture goes
  in the same five places, ahead of `super`. The slot handlers resolve the item with
  `SlotsIcon icon; w.GetUserData(icon); icon.GetEntity()`; the hands one through the
  `ItemPreviewWidget` named `PanelWidget`->`Render`.
- **Alt has no getter.** `DayZGame` keeps `m_IsLeftAltHolding` private (`IsLeftCtrlDown()`
  is the only one exposed). Read the key itself: `KeyState(KeyCode.KC_LMENU)` /
  `KC_RMENU` (1_Core `ensystem.c:291`, a global proto vanilla's script console uses).
- **`WidgetEventHandler` keeps one handler per widget per event** (`map<Widget, ref Param>`
  filled with `Insert`, widgeteventhandler.c:5-30): registering a second `MouseButtonDown`
  on a slot icon's panel does not add a handler -- override the container's method instead.
- **`GameInventory.CanSwapEntitiesEx(looseGroundItem, itemInLocalContainer)` is true**
  (measured 2026-09-26 on a client-local mirror container): the screen offers SWAP (`PerformCombination`
  -> `player.PredictiveSwapEntities(target, selected)`) for an item lying on the ground
  dropped onto an item in a client-local container. A ground `InventoryLocation` has no
  parent -- server code that names the source by `GetParent()` must branch on
  `GetType() == GROUND`.
- **Which call a drop makes** (icon.c): onto a grid cell -> `SplitItemUtils.
  TakeOrSplitToInventoryLocation` -> `PredictiveTakeToDst`; onto another item's icon ->
  `ItemManager.GetChosenCombinationFlag` in the order COMBINE_QUANTITY2 (`target.
  CanBeCombined(selected)` -> `target.CombineItemsClient(selected)`), ADD_AS_ATTACHMENT,
  ADD_AS_CARGO, SWAP_MAGAZINE, SWAP (`CanSwapEntitiesEx`), FSWAP (`CanForceSwapEntitiesEx`),
  else nothing at all, silently.

## Nesting containers: only under a slot (read from source, measured 2026-09-26)

`Container_Base.CanPutInCargo(parent)` and `CanReceiveItemIntoCargo(item)`
(container_base.c:8/19) both refuse when `AreChildrenAccessible()` is false, and
`EntityAI.AreChildrenAccessible()` (entityai.c:1685 in 1.30) walks up the hierarchy and returns
false the moment any ancestor sits in CARGO or PROXYCARGO. So a bag lying in a crate's
cargo can neither take an item nor be put into another bag there; a jacket HUNG IN AN
ATTACHMENT SLOT (a player's Body slot, a mod's slot on a container) keeps its pockets
usable. Measured: a bandage moved into a bag in a container's cargo -> `LocationCanAddEntity`
false; the same into a hoodie in a slot -> fine. Vanilla's own sea chest behaves the same.
A container arriving WITH contents is fine either way (the tree moves as a whole).

## A placed item from a structure p3d (measured 2026-09-26)

`class X: Inventory_Base { model = "\DZ\structures\Furniture\Cases\locker\locker_closed_v1.p3d"; ... }`
works as a placed item: it stands upright on `PlaceOnSurface`, gets a network id, takes
`SetLifetime`, is saved with the world and comes back after a restart, and an
`ActionInteractBase` with `CCTCursor` finds it (measured on 1.29; since 1.30 the server
judges `CCTCursor` by the object's origin distance + collision radius, see actions.md). Overrides that give back what a
`HouseNoDestruct` had for free: `IsInventoryVisible() false` (not in the vicinity panel),
`IsTakeable() false`, `CanPutInCargo/CanPutIntoHands/CanPutAsAttachment false`, hitpoints
in `DamageSystem`. A `House` created by script is NOT in the world save -- it is gone at
the next boot -- which is why a script-placed static "disappears".

## A weapon draws its magazine only when told to (read from source, measured 2026-09-26)

`Weapon_Base.ShowMagazine()`/`HideMagazine()` (weapon_base.c:2133/:2141 in 1.30) switch the magazine
selection: `SelectionMagazineShow()/Hide()` (proto native on `Weapon`, weapon.c:447) or
`SetSimpleHiddenSelectionState(m_magazineSimpleSelectionIndex, ...)` on the weapons that
have one. The game calls them from exactly two places: the weapon FSM
(`SyncSelectionState(has_bullet, has_mag)` in `LoadCurrentFSMState`, `AfterStoreLoad`
and weaponfsm.c:557) and `ForceSyncSelectionState()` (weapon_base.c:454, used by
`RandomizeFSMState`, scarh.c, magnum.c). `Weapon_Base.EEItemAttached` (line 1108) only
calls `GetPropertyModifierObject().UpdateModifiers()` -- attaching a magazine by itself
shows nothing. MEASURED: a magazine created with `CreateAttachmentEx` into the slot of a
client-local (`ECE_LOCAL`) weapon is listed by the inventory panel, but the weapon's
preview is drawn without it. The fix is to call `weapon.ForceSyncSelectionState()` after
the attach (it reads what is attached now; `IsChamberFull` is false on such a weapon, so
the bullet stays hidden), and `HideMagazine()` directly when the magazine is deleted:
`ObjectDelete` is deferred to the end of the frame and `GetMagazine()` still answers the
doomed item until then. (The fix was written on 2026-09-26; its in-game verification
was still pending a rebuild at that date.)

## A RUINED container dumps its inventory; a hidden server-local container still takes damage (measured 2026-09-26)

`Container_Base.EEHealthLevelChanged` (container_base.c:96) calls
`MiscGameplayFunctions.DropAllItemsInInventoryInBounds(this, m_HalfExtents)` when the new
level is RUINED and the container has no parent -- every cargo item and attachment is
`TakeToDst`-ed to the GROUND at the container's own position (the same is done by
FireplaceBase, TentBase and GiftBox_Base). An object created with `ECE_LOCAL |
ECE_NOPERSISTENCY_WORLD` is not announced to any client but it is solid on the server and
TAKES DAMAGE like anything else: an explosive charge set off beside a placed box ruined the
unannounced container standing in the box's coordinates and dumped its 264 entities as
server-local ghosts on the ground. `Object.SetAllowDamage(false)` (proto native,
object.c:1196 in 1.30) is the switch that makes an entity ignore damage; call it right after
creation. Entities made with `LocationCreateLocalEntity` inside a non-persistent container
do NOT inherit the flag: once dropped on the ground they are ordinary local entities and
are written to the world save.

## ObjectDelete does not flag the entity within the frame; EntityAI.Delete() does (measured 2026-09-27)

`EntityAI.IsSetForDeletion()` is `IsPreparedToDelete() || m_PendingDelete || ToDelete() ||
IsPendingDeletion()` (entityai.c:812 in 1.30). `GetGame().ObjectDelete(e)` schedules the engine's
deletion but a tree walk in the same frame still met the entity (a merged-away stack was
written into a record right after the call). `EntityAI.Delete()` sets `m_PendingDelete = true`
and then queues `g_Game.ObjectDelete` through the system call queue (entityai.c:779 in 1.30,
object.c:82), so `IsSetForDeletion()` is true from the next line on. Delete mid-operation
entities with `e.Delete()` when anything in the same frame walks the tree.

