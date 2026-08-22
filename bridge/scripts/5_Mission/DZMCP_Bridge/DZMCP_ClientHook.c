// Lifecycle hook for the CLIENT half, and the mirror of DZMCP_MissionHook.
//
// MissionGameplay is the in-game client mission (missiongameplay.c:1, OnInit
// at :96) and exists only on a client, so this class alone already makes the
// half client-only -- the explicit guard below is belt and braces, exactly as
// the server hook's is.
//
// MissionMainMenu is deliberately NOT hooked. It is a separate class, the
// world does not exist behind it, and nothing this half offers has anything to
// walk there yet. Whether the menu is worth reaching is one of the questions
// the spec leaves open for the first live run.
//
// No `extends` clause: an inheritance clause on a `modded class` silently
// detaches it from the modded chain, with nothing reported anywhere.
modded class MissionGameplay
{
    protected ref DZMCP_ClientBridgeCore m_DZMCP_ClientBridge;

    override void OnInit()
    {
        super.OnInit();

        // A dedicated server never constructs this mission, but a listen
        // server would run client code in the same process, and arming a
        // second bridge there would put two of them in one -profiles
        // directory. The guard states the intent rather than relying on the
        // class alone to enforce it.
        if (!GetGame() || GetGame().IsDedicatedServer())
            return;

        m_DZMCP_ClientBridge = new DZMCP_ClientBridgeCore();
        m_DZMCP_ClientBridge.Init();

        // The same queue and the same rate as the server half. NOT the same
        // measurement, though: the 1 Hz figure (75 -> 80 ticks across five
        // seconds) was taken from a modded MissionServer.OnInit on a live
        // boot, and the client is a different mission with a different load
        // order. Whether it fires here at all is the first thing to measure
        // when the stand is allowed again -- it is written down in the spec as
        // a question, not assumed here.
        GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(this.DZMCP_OnClientTick, 1000, true);
        DZMCP_Log.Info("client tick armed at 1 Hz on CALL_CATEGORY_SYSTEM");
    }

    void DZMCP_OnClientTick()
    {
        if (m_DZMCP_ClientBridge)
            m_DZMCP_ClientBridge.OnTick();
    }

    // A repeating call left pointing at a destroyed object is the engine
    // calling into freed memory. The client leaves a mission every time the
    // player disconnects to the menu, so this runs far more often here than
    // its server counterpart ever does.
    override void OnMissionFinish()
    {
        if (GetGame())
            GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).Remove(this.DZMCP_OnClientTick);

        super.OnMissionFinish();
    }
}
