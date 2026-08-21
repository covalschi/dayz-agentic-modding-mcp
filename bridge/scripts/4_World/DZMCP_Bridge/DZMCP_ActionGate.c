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
    // The null check here protects the RETURN VALUE (true only when something
    // was actually released), not the call: OnActionEnd dispatches to
    // ActionManagerServer's own override, whose first line is the same
    // `if (m_CurrentActionData)` guard, so a null-data call through THIS path
    // is a safe no-op. The real hazard lives one level down and is unreachable
    // from here: the BASE class's OnActionEnd dereferences
    // m_CurrentActionData.m_Player outside its own guard on its second
    // Debug.ActionLog line (actionmanagerbase.c:317, only with the action log
    // enabled, which R28 enables) -- anything that ever calls the base method
    // directly, or overrides the server one without the guard, inherits it.
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

        // A client's own action request that has arrived but not yet been
        // handled by the manager's next Update (actionmanagerserver.c:40-64
        // stores it; the Update at :202 consumes it). In that sub-frame window
        // m_CurrentActionData is still null, so without this check a delivery
        // would pass as "idle" -- and SetupAction reads the pending RECEIVE
        // data (actionbase.c:176-179), splicing the human's item and target
        // into the delivered action while their own request gets refused.
        // Only reachable by aiming a delivery at a human mid-request, but the
        // cost of that collision is a corrupted action on a real player.
        if (m_PendingAction || m_PendingActionReciveData)
            return "manager busy -- the player's own action request is mid-flight";

        if (!m_Player)
            return "no player is attached to this action manager";

        if (m_Player.GetCommandModifier_Action() || m_Player.GetCommand_Action())
            return "player is already performing an action";

        // IsSprinting() reads a CACHED HumanMovementState, refreshed only by
        // explicit GetMovementState() calls inside the player's own
        // command-handler frame. The engine's gate runs in that frame, so the
        // cache is fresh there; this gate runs from the 1 Hz CallLater,
        // OUTSIDE it, so it reads the native state fresh instead -- equal to
        // the cache whenever the cache is fresh, correct when it is not.
        // Honest status of this branch: it has never been observed to fire.
        // In the one live attempt, the server-side position trace showed the
        // sprinting client moving at walking speed, so the input never
        // produced a sprint the server could see, and the owner then ruled
        // the scenario out of scope: auto-tests act on conjured subjects the
        // model owns, never on a human, so this refusal is engine-gate
        // mirroring (the engine would reject the delivery anyway), not a
        // feature. It stays because a named reason beats the engine's silent
        // early return if it ever does fire.
        HumanMovementState dzmcpMove = new HumanMovementState();
        m_Player.GetMovementState(dzmcpMove);
        if (dzmcpMove.m_iMovement == DayZPlayerConstants.MOVEMENT_SPRINT)
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
    //   "pending"       accepted, parked in the acknowledgment state. NOT
    //                   necessarily parked forever: the server processes its
    //                   own ack juncture, and the delivered action's ack id
    //                   (-1) matches the manager's own pending id (-1)
    //                   (actionmanagerbase.c:161-164), so "pending" can
    //                   un-park into a real start on a later frame. The caller
    //                   must therefore watch the MANAGER, not this state --
    //                   completion is the manager releasing the data, and the
    //                   deadline is the bound either way
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
            return "refused: '" + actionClassName + "' -- the action's own Can() said no, its conditions did not hold for this player, target and item";

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
