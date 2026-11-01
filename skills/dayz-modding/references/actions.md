# User Actions — interactive verbs (hold-F / press-F interactions)

The action system drives every "look at object → prompt appears → press/hold F" interaction.
This is the backbone for "use scientific equipment" gameplay. Verified against
`4_World/Classes/UserActionsComponent/` of 1.29 (124708); line numbers below re-checked
against 1.30.164200 (`scripts.pbo` 127662) on 2026-11-01.

## The three base families

| Base class | Behavior | Typical use |
|---|---|---|
| `ActionInteractBase` | instant press-F on a target | open/close, toggle device |
| `ActionSingleUseBase` | instant use of item in hands | eat pill, snap sample |
| `ActionContinuousBase` | hold-F with progress bar + animation | processing, filling, crafting-at-station |

Continuous actions pair with a **callback (CB) class** that defines the measuring
component (duration/quantity):

```c
class ActionAnalyzeSampleCB : ActionContinuousBaseCB
{
    override void CreateActionComponent()
    {
        // finish after N seconds of holding:
        m_ActionData.m_ActionComponent = new CAContinuousTime(8.0);
        // other stock components (verified ctors):
        //   CAContinuousRepeat(time_per_cycle)                       (CAContinuousRepeat.c:9)
        //   CAContinuousQuantity(quantity_used_per_second)           (CAContinuousQuantity.c:10)
        //   CAContinuousQuantityRepeat(qty_per_sec, time_to_repeat)  (CAContinuousQuantityRepeat.c:12)
        // CAContinuousRepeat never finishes on its own: each completed cycle calls
        // OnCompletePogress (engine typo, CAContinuousBase.c:8) -> OnFinishProgress ->
        // OnFinishProgressServer, resets the timer and keeps holding (Execute returns
        // UA_PROCESSING, CAContinuousRepeat.c:41-48) — natural fit for "process N inputs in a row".
    }
}
```

## Anatomy of a continuous action (verified virtuals)

```c
class ActionAnalyzeSample : ActionContinuousBase
{
    void ActionAnalyzeSample()
    {
        m_CallbackClass = ActionAnalyzeSampleCB;
        m_CommandUID    = DayZPlayerConstants.CMD_ACTIONFB_INTERACT;  // animation
        m_FullBody      = true;
        m_StanceMask    = DayZPlayerConstants.STANCEMASK_ERECT | DayZPlayerConstants.STANCEMASK_CROUCH;
        m_Text          = "#mymod_analyze_sample";     // stringtable key or literal
    }

    override void CreateConditionComponents()
    {
        m_ConditionItem   = new CCINonRuined();        // item in hands must not be ruined
        m_ConditionTarget = new CCTObject(UAMaxDistances.DEFAULT); // target = object under cursor
    }

    override bool ActionCondition(PlayerBase player, ActionTarget target, ItemBase item)
    {
        // Runs on BOTH client (to show prompt) and server (to validate). Keep it cheap & pure.
        MyLabDevice device = MyLabDevice.Cast(target.GetObject());
        if (!device) return false;
        return device.CanProcess(item);                // e.g. classname allowed by admin config
    }

    override void OnFinishProgressServer(ActionData action_data)
    {
        // SERVER-ONLY completion — the only safe place for rewards/consumption.
        MyLabDevice device = MyLabDevice.Cast(action_data.m_Target.GetObject());
        ItemBase item = action_data.m_MainItem;
        PlayerBase player = action_data.m_Player;
        device.ProcessServer(player, item);
    }
}
```

Key `ActionBase` overridables (with 1.30 source lines in ActionBase.c):
`CreateConditionComponents` (:245), `HasTarget` (:259), `HasProgress` (:265),
`ActionConditionContinue` (:436) — re-checked every tick while holding,
`ActionCondition` (:441), `OnStartServer` (:1391), `OnEndServer` (:1409);
`ActionContinuousBase.OnFinishProgressServer` (:289) — fires only if the hold completed.
Client-side hooks exist too (`OnStartClient`, `OnFinishProgressClient`) — cosmetic only.

## Behavior switches on ActionBase (verified, ActionBase.c)

- `IsLockTargetOnUse()` (:102) — default `m_LockTargetOnUse = HasTarget()` (:89): a targeted
  action locks its target to ONE player at a time (:1178). Override to return `false` to let
  several players use one static station simultaneously; keep the default to serialize access.
- `CanBePerformedFromInventory()` (:340) / `CanBePerformedFromQuickbar()` (:335) — expose the
  action in the inventory context menu / quickbar.
- Player-state gates feeding `InitConditionMask()` ACM_* bits (:107): `CanBeUsedInVehicle()`
  (:361), `CanBeUsedSwimming()` (:376), `CanBeUsedOnLadder()` (:381), `CanBeUsedInRestrain()` (:356).
- `HasProneException()` (:768) + `m_CommandUIDProne` — separate animation command while prone.
  The field the action sets is `AnimatedActionBase.m_CommandUIDProne` (animatedactionbase.c:148);
  the old citation `ActionBase.c:24` pointed at `ActionOverrideData`, which 1.30 moved to
  entityai.c:103-108.
- Server-side interrupt mid-action: `if (action_data.m_Callback) action_data.m_Callback.Interrupt();`
  (vanilla pattern, AnimatedActionBase.c:407; `ActionBase.Interrupt(action_data)` at :821).
- Continuous loop hooks: `OnStartAnimationLoop`/`OnEndAnimationLoop` with `*Server`/`*Client`
  splits (ActionContinuousBase.c:208/:228).
- Duration constants for consistency with vanilla: `UATimeSpent.DEFAULT = 1`,
  `DEFAULT_CONSTRUCT = 5`, `DEFAULT_CRAFT = 6`, `SKIN = 10`, `COLLECT_SAMPLE = 3`,
  `TEST_BLOOD = 3`... plus `UAQuantityConsumed` (actions/ActionConstants.c:27+, :3+).

## Condition components (stock, from ItemConditionComponents/ & TargetConditionsComponents/)

Item (in hands): `CCINone` (no item required!), `CCIDummy`, `CCINonRuined`, `CCINotPresent`,
`CCINotEmpty`, `CCINotRuinedAndDry`, `CCINotRuinedAndEmpty`, `CCIPresent`.
Target: `CCTNone` (no target), `CCTDummy`, `CCTSelf`, `CCTMan`, `CCTObject(dist)`,
`CCTNonRuined(dist)`, `CCTCursor`, `CCTCursorParent`, `CCTCursorNoObject`,
`CCTCursorNoRuinCheck`, `CCTSurface`, `CCTTree`, `CCTWaterSurface`; new in 1.30:
`CCTCursorInherited(array<typename> allowedTypes, dist)` (cursor hit on an object of one of the
given script types) and `CCTLiquid` (water/liquid targeting).

Since 1.30.164200 the three cursor components (`CCTCursor`, `CCTCursorParent`,
`CCTCursorNoRuinCheck`) judge differently on the SERVER: object origin to player position,
allowed up to `dist + GetCollisionRadius()`, with no cursor-hit test (the client still tests
the cursor). An action whose target's origin sits far from where players stand needs
`dist` to cover that (persistence-networking.md, "DayZ 1.30 Update 2").

Pick `CCINone` + `CCTObject` for "interact with device, empty hands";
`CCINonRuined` + `CCTNonRuined` for "apply held item to device".

**VERIFIED 2026-07-31 (live, cost hours — read the source, not the folklore):**
`CCIDummy.Can()` is literally `return item != null` (ccidummy.c:5) — it REQUIRES an item in
hands. `CCINone.Can()` is `return true` (ccinone.c) and works with hands full OR empty.
So for "interact with a device regardless of what's in hands" the ONLY correct component is
`CCINone`. Using `CCIDummy` there makes the prompt silently invisible with empty hands, and
`ActionCondition` is never even called (ActionBase.Can → ComputeConditionMask gates it), so
debug prints inside ActionCondition stay empty and mislead the diagnosis.
The earlier community claim ("with CCINone the engine may skip the action while holding an
item — use CCIDummy instead") is FALSE — `CCIBase.CanContinue` is what ties an action to the
held item, not `Can`. Deleted as disproven.

## Registering actions

**Step zero, for every NEW action class: register it in `ActionConstructor`** (measured
2026-09-16, build 1.29): `ItemBase.AddAction` asks `ActionManagerBase.GetAction(typename)`,
which only answers classes listed by `ActionConstructor.RegisterActions`; an unregistered
action is dropped with a `Debug.LogError("Action X dosn't exist!")` that lands in the .RPT,
not the script log, so the verb is silently absent for players and the stand's `world_action`
refuses it as "not registered as an action". The registration also assigns the action its
network id. (Read 2026-11-01 on 1.30: `GetAction` reads a map that only
`ActionConstructor.ConstructActions` fills, actionmanagerbase.c:77-80/:214-219; the
`PlayerBase.AddAction` path has no null check, playerbase.c:1819-1821, so an unregistered
action added there fails with a NULL-pointer script error instead.)

```c
modded class ActionConstructor
{
    override void RegisterActions(TTypenameArray actions)
    {
        super.RegisterActions(actions);
        actions.Insert(MyMod_ActionOpenBox);
        actions.Insert(MyMod_ActionCloseBox);
    }
}
```

On an **item** (the action appears when this item is in hands, targeting whatever):

```c
// ItemBase.c:255 (1.30) — void AddAction(typename actionName)
class PetriDish : ItemBase   // or: modded class SomeVanillaItem
{
    override void SetActions()
    {
        super.SetActions();
        AddAction(ActionPlaceSampleInDish);
    }
}
```

On the **player** (action available regardless of held item — used for target-centric
verbs; also the only way when the *target* mod item is not yours to edit):

```c
// PlayerBase.c:1817 (1.30) — void AddAction(typename actionName, out TInputActionMap InputActionMap)
modded class PlayerBase
{
    override void SetActions(out TInputActionMap InputActionMap)
    {
        super.SetActions(InputActionMap);
        AddAction(ActionAnalyzeSample, InputActionMap);
    }
}
```

On **buildings/static objects**: same `SetActions()` override pattern on the building class
(see vanilla `Well` in `4_World/Entities/Building/Well.c` adding `ActionDrinkWellContinuous`).
For map-placed statics you don't own, register on PlayerBase and filter by target type in
`ActionCondition`.

## Placed devices: preventing pickup (verified API composition)

A deployed station stays deployed only if you close ALL takeaway paths — `IsTakeable()`
alone leaves the action-wheel Take entries and the inventory-drag path open:

```c
class MyMod_LabDevice : ItemBase
{
    override bool IsTakeable() { return false; }                       // base at ItemBase.c:4372 (1.30)

    override bool CanPutInCargo(EntityAI parent) { return false; }     // blocks drag into containers

    override void SetActions()
    {
        super.SetActions();
        RemoveAction(ActionTakeItem);          // ItemBase.c:295 (1.30) — void RemoveAction(typename)
        RemoveAction(ActionTakeItemToHands);
    }
}
```

Vanilla precedent: `PowerGeneratorStatic` overrides `IsTakeable()` (PowerGeneratorStatic.c:219);
PlayerBase-registered actions have their own `RemoveAction(typename, out TInputActionMap)`
(PlayerBase.c:1843, 1.30).

## ActionData — what you get in server callbacks

`action_data.m_Player` (PlayerBase), `.m_MainItem` (held ItemBase),
`.m_Target` (ActionTarget → `.GetObject()`, `.GetComponentIndex()`), `.m_State`.
Component selections (e.g. which part of a device was cursor-hit):
`target.GetObject().GetActionComponentNameList(target.GetComponentIndex(), selections)` —
see `ActionFertilizeSlot.c` for the full selection→slot resolution example.

## Animations & UI text

- `m_CommandUID` picks the player animation: `DayZPlayerConstants.CMD_ACTIONFB_*`
  (fullbody, `m_FullBody = true`) or `CMD_ACTIONMOD_*` (additive). The list is
  `enum DayZPlayerConstants` in `3_game/dayzplayer.c:601` (there is no DayZPlayerConstants.c);
  `CMD_ACTIONFB_INTERACT` is the generic reach-out.
- `m_Text` shows in the HUD prompt; use a `#stringtable_key` for localization.
- Progress bar appears automatically for continuous actions.

## Design rules

- `ActionCondition` must be side-effect-free and identical on client & server — it runs on
  both; divergence = ghost prompts or server rejections.
- All state mutation goes in `OnStartServer` / `OnFinishProgressServer` / `OnEndServer`.
- Long lookups (JSON config checks) in `ActionCondition`: precompute a fast lookup
  structure (map of classname→rule) when loading settings, not per-frame parsing.
- After changing a device's visible state server-side, sync it (netsync vars +
  `SetSynchDirty()`) so other clients see the change — see persistence-networking.md.
- New actions in 4_World require client AND server to run the mod → the mod must be in
  the server's `-mod=` list and players' launchers; purely server-side "-servermod" cannot
  add client-visible actions.

## Hard-won runtime facts (verified live, 1.29 (124708), 2026-08-02)

These cost multiple debugging cycles in a real mod. Each one is silent — no error, no chat
message, no script log entry — so they are indistinguishable from "my code is wrong".

### `OnExecuteServer` runs from an ANIMATION EVENT — `m_Target` may be NULL there

`OnExecuteServer` is NOT called in the frame where `ActionCondition` passed. The chain is
`AnimatedActionBase.OnAnimationEvent` (animatedactionbase.c:201 in 1.30) → `OnExecuteServer`, and by
then `action_data.m_Target` can be null. Touching it unguarded throws
`NULL pointer to instance` and the action silently does nothing — the prompt still shows,
the player sees no message, and the script log has only the exception.

```c
override void OnExecuteServer(ActionData action_data)
{
    if (!action_data) return;
    PlayerBase player = action_data.m_Player;
    if (!player) return;
    if (action_data.m_Target)                  // MAY BE NULL — check, don't assume
    {
        Object obj = action_data.m_Target.GetObject();
        if (obj) { /* re-validate here */ }
    }
    // Losing the target does NOT weaken security: the engine already ran Can() server-side
    // (actionmanagerserver.c:168 in 1.30), i.e. your ActionCondition executed before this point.
}
```

### The server evaluates `Can()` ONCE per press — not per tick

`ActionManagerServer.StartDeliveredAction` (actionmanagerserver.c:168 in 1.30) gates on
`!GetCommandModifier_Action() && !GetCommand_Action() && !IsSprinting() && pickedAction.Can(...)`.
Consequences:

- Any of those four failing = **silent rejection**. The prompt stays visible because the
  CLIENT draws it from its own condition. "Prompt shows, F does nothing" is often this.
- Per-tick condition evaluation happens on the CLIENT only (to pick which prompt to show).
  So expensive server-side work in `ActionCondition` costs once per press, not per frame —
  but expensive CLIENT-side work costs every tick.
- **Test tip:** stand still. Sprinting silently blocks every interact action.

### Diagnose with a probe command, not with rebuilds

Add an admin command that runs the same server-side checks and prints each result. Without
it, "no prompt" is one bit of information and every hypothesis costs a rebuild + restart
cycle. Log the VALUES a decision is made from (resolved faction, matched rule, device type),
never just "entered branch X" — a branch-entry log cannot distinguish the cases.

### Dropping / opening from the SERVER side (measured 2026-09-02, retail server 1.29)

- `ActionDropItem` run through the server-side action manager (the DZMCP bridge's
  `world_action ActionDropItem`) DOES drop the item in hands: its `OnExecuteServer`
  calls `player.ServerDropEntity(GetItemInHands())` on a multiplayer server.
- `ActionDropItemSimple` does NOTHING when started on the server: its whole drop
  lives under `#ifndef SERVER` (`PredictiveDropEntity`), so "the action started and
  ended" with the item still in hands is the expected outcome, not a bug.
- A vanilla or modded single-use action that opens a menu from `OnExecuteServer`
  via an RPC to the client (e.g. a PDA-style "open" action that sends
  a show-menu RPC) opens the menu on the client even when the action is started
  server-side. Actions that open menus only in `OnStartClient`/`OnExecuteClient`
  cannot be driven this way.
- A radius delete (`GetObjectsAtPosition3D` style) never reaches items inside a
  player's inventory or hands: `world_delete` reported 0 for a radio in hands.
