// Lifecycle hook: where the bridge gets armed and, just as importantly, where
// it gets taken down.
//
// MissionServer exists server-side only, so this class alone already makes the
// mod inherently server-only -- the explicit dedicated-server guard below is
// belt and braces, and mirrors the one the 4_World tier genuinely needs (that
// tier does compile on clients).
//
// No `extends` clause: an inheritance clause on a `modded class` silently
// detaches it from the modded chain, with nothing reported anywhere.
modded class MissionServer
{
    protected ref DZMCP_BridgeCore m_DZMCP_Bridge;

    override void OnInit()
    {
        super.OnInit();

        if (!GetGame() || !GetGame().IsDedicatedServer())
            return;

        m_DZMCP_Bridge = new DZMCP_BridgeCore();
        m_DZMCP_Bridge.Init();

        // Task 1 measured this on a live boot: a repeating CallLater on
        // CALL_CATEGORY_SYSTEM, armed from a mod's modded MissionServer.OnInit,
        // fires at a clean 1 Hz (75 -> 80 ticks across a five-second window).
        // The same call placed in a mission's own init.c did not fire at all,
        // which is why it was measured before anything was built on it.
        GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(this.DZMCP_OnTick, 1000, true);
        DZMCP_Log.Info("tick armed at 1 Hz on CALL_CATEGORY_SYSTEM");
    }

    void DZMCP_OnTick()
    {
        if (m_DZMCP_Bridge)
            m_DZMCP_Bridge.OnTick();
    }

    // A repeating call left pointing at a destroyed object is the engine
    // calling into freed memory. Task 1's heartbeat armed one and removed
    // nothing; this is that fix.
    //
    // OnMissionFinish is declared on Mission (gameplay.c:702) and is not
    // overridden by MissionServer, so overriding it here is legal and calls
    // straight through to the empty base. It is INSURANCE, not the cleanup
    // mechanism: the stand is stopped by killing the process tree, which runs
    // no destructor and no mission shutdown at all. Clearing the transport
    // files is the Python side's job, and it does it before every boot.
    override void OnMissionFinish()
    {
        if (GetGame())
            GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).Remove(this.DZMCP_OnTick);

        super.OnMissionFinish();
    }
}
