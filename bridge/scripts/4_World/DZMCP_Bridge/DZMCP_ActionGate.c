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
//   The delivery lives here (DZMCP_DeliverAction, Task 7) together with the
//   two primitives 5_Mission provably cannot have: reading whether the manager
//   is still holding action data, and clearing it.
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

    // Deliver one action through the engine's own gate, from inside the class
    // whose protected member the engine actually reads.
    //
    // Returns one of:
    //   ""              the action ran and ended within this call (instant)
    //   "accepted"      the engine accepted it; the real start happens on the
    //                   player's next command-handler frame, and the caller
    //                   must keep watching DZMCP_HasPendingActionData()
    //   "pending"       accepted, but parked awaiting a client acknowledgment
    //                   -- see below; the caller must treat this as running
    //                   with a deadline, because the acknowledgment id here is
    //                   -1 and no client ever sent this request
    //   "refused: ..."  a named refusal; the manager holds nothing afterwards
    //
    // The refusal classification happens BEFORE the engine is touched (brief
    // R24), mirroring the gate at actionmanagerserver.c:142 term by term, so
    // that "the player is sprinting" and "the mod's own Can() said no" come
    // back as different sentences -- the second one being the meaningful
    // negative result this tool exists to produce.
    //
    // R25, the most dangerous line of the phase: SetupAction assigns its out
    // parameter (this manager's own m_CurrentActionData -- actionbase.c:164)
    // BEFORE any early return, so a SetupAction that returns false leaves the
    // manager holding data, and a manager holding data refuses every later
    // action for the rest of the session. Every failure path below therefore
    // releases the data before reporting. One null check on the data suffices:
    // SetupAction assigns m_Action on the line after creating the data, before
    // every early return, so held data always has its action.
    string DZMCP_DeliverAction(string actionClassName, Object targetObject)
    {
        if (!GetGame() || !GetGame().IsDedicatedServer())
            return "refused: the bridge action gate is inert outside a dedicated server";

        // The player-state half of the gate, read before anything is created.
        string block = DZMCP_DescribePlayerBlock();
        if (block != "")
            return "refused: " + block;

        // ToType() answers a null typename for a name no loaded script declares
        // (enstring.c:95); GetAction answers null for a class that is real but
        // was never registered as an action. Both are "unknown action class",
        // told apart in the text because the remedies differ.
        typename actionType = actionClassName.ToType();
        if (!actionType)
            return "refused: unknown action class '" + actionClassName + "' -- no loaded script declares it";

        ActionBase action = ActionManagerBase.GetAction(actionType);
        if (!action)
            return "refused: '" + actionClassName + "' is a script class but is not registered as an action";

        ItemBase item = m_Player.GetItemInHands();
        ActionTarget target = new ActionTarget(targetObject, null, -1, vector.Zero, 0);

        // The mod's own applicability check -- the meaningful negative result.
        // Checked before SetupAction so a plain "conditions did not hold" never
        // creates action data at all.
        if (!action.Can(m_Player, target, item))
            return "refused: the action's own Can() said no -- its conditions did not hold for this player, target and item";

        if (LogManager.IsActionLogEnable())
            Debug.ActionLog("bridge delivery", action.ToString(), "n/a", "DZMCP_DeliverAction", m_Player.ToString());

        if (!action.SetupAction(m_Player, target, item, m_CurrentActionData))
        {
            // R25: the out parameter was assigned before the early return.
            DZMCP_ReleasePendingActionData();
            return "refused: SetupAction declined after creating action data -- the data was released so the player can act again";
        }

        StartDeliveredAction();

        // R26: the outcome is read, never assumed.
        if (!m_CurrentActionData)
            return "";

        if (m_CurrentActionData.m_State == UA_AM_ACCEPTED)
            return "accepted";

        if (m_CurrentActionData.m_State == UA_AM_PENDING)
            return "pending";

        // Anything else means the gate inside StartDeliveredAction rejected it
        // -- with our pre-checks passed, that is AddActionJuncture failing on
        // the target lock (actionbase.c:1075-1099). The rejected branch does
        // not clear the data (actionmanagerserver.c:166-181), so this must.
        DZMCP_ReleasePendingActionData();
        return "refused: the engine rejected the action at the target lock (the target is reserved by another action or player)";
    }
}
