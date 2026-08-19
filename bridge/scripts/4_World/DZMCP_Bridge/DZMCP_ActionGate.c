// 4_World tier of the bridge mod.
//
// WHY THIS TIER EXISTS NOW, before the task that uses it (brief R1):
//
//   ActionManagerServer.StartDeliveredAction() takes no arguments. It reads
//   m_CurrentActionData and returns on its first line when that member is
//   null -- the whole failure handling there is one comment, no Print, no
//   Error, no log line at all (actionmanagerserver.c:115-121). The member is
//   `protected ref ActionData m_CurrentActionData`, declared on
//   ActionManagerBase (actionmanagerbase.c:61), i.e. in THIS tier. Enforce
//   Script's tier visibility is strictly upward: 5_Mission can see the class
//   but cannot write its protected members. So an action delivered from a
//   modded MissionServer -- the shape the spec originally described -- fills
//   a caller-owned local instead of the manager's own member, the engine
//   silently does nothing, and the tool reports success. That is the worst
//   possible failure for a tool whose entire value proposition is "a refusal
//   is a meaningful result".
//
//   The delivery itself is Task 7. What is here now is the ENTRY POINT SHAPE
//   plus the two primitives 5_Mission provably cannot have: reading whether
//   the manager is still holding action data, and clearing it. Both are
//   load-bearing for Task 7 and both prove the tier claim above at compile
//   time rather than on a six-minute boot.
//
// Everything here is inert unless this is a dedicated server (brief R2). This
// tier compiles on clients too -- the bridge is only ever MEANT for
// -serverMod, but nothing in the engine enforces that placement, so the guard
// is the enforcement.
//
// No `extends` clause on the modded class (brief R3): an inheritance clause on
// a `modded class` silently detaches it from the modded chain, and the
// detachment is not reported anywhere.
modded class ActionManagerServer
{
    // Is the manager still holding action data?
    //
    // Task 7 needs this as its wedge check: SetupAction assigns its `out
    // ActionData` parameter (actionbase.c:164) BEFORE any early return, so a
    // SetupAction that returns false still leaves the manager holding data,
    // and a manager left holding data can never start another action for the
    // rest of the session -- the gate at actionmanagerserver.c:142 refuses
    // every subsequent one. Vanilla has the same hole and gets away with it
    // because it rarely reaches that path; a tool that delivers actions on
    // demand reaches it on demand.
    bool DZMCP_HasPendingActionData()
    {
        if (!GetGame() || !GetGame().IsDedicatedServer())
            return false;

        return m_CurrentActionData != null;
    }

    // Release action data the manager is still holding, if any.
    //
    // OnActionEnd() is the public method that actually nulls
    // m_CurrentActionData (actionmanagerbase.c:311-324) -- assigning null
    // directly would skip ActionCleanup and ClearActionJuncture.
    //
    // The null check here is NOT redundant, and this is the one place in the
    // whole mod where a missing guard would crash the server. With the engine
    // action log enabled (which this mod enables at init, brief R28),
    // ActionManagerBase.OnActionEnd's second Debug.ActionLog call dereferences
    // m_CurrentActionData.m_Player from OUTSIDE the `if (m_CurrentActionData)`
    // that guards the first one. Vanilla never trips it because
    // ActionManagerServer's own override guards the super call; anything that
    // calls OnActionEnd directly must guard it itself.
    //
    // Returns true when something was actually released.
    bool DZMCP_ReleasePendingActionData()
    {
        if (!GetGame() || !GetGame().IsDedicatedServer())
            return false;

        if (!m_CurrentActionData)
            return false;

        OnActionEnd();
        return true;
    }

    // The part of the engine's own action gate that depends on nothing but the
    // player's current state -- read before the engine is called, so a refusal
    // can name a specific reason instead of collapsing into "it did not work".
    //
    // Mirrors actionmanagerserver.c:142 term by term, minus the action's own
    // Can(): that one is the MEANINGFUL negative result (the mod's conditions
    // did not hold) and belongs to Task 7's caller, which has the action, the
    // target and the item to hand it.
    //
    // Returns an empty string when nothing in the player's state blocks an
    // action right now.
    string DZMCP_DescribePlayerBlock()
    {
        if (!GetGame() || !GetGame().IsDedicatedServer())
            return "the bridge action gate is inert outside a dedicated server";

        if (m_CurrentActionData)
            return "manager busy -- a previous action is still owned";

        if (!m_Player)
            return "no player is attached to this action manager";

        if (m_Player.GetCommandModifier_Action() || m_Player.GetCommand_Action())
            return "player is already performing an action";

        if (m_Player.IsSprinting())
            return "player is sprinting";

        return "";
    }
}
