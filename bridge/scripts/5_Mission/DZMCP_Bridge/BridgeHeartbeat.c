// Phase 2 Task 1 probe: does GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(...)
// fire inside a MOD? An earlier probe placed in a mission's init.c (this project's own
// zp-research stand) never saw it fire; a mature third-party mod relies on it working
// inside a mod, so the two contexts evidently differ. This is the measurement.
//
// Deliberately minimal: a counter and one written line per tick, through FPrintln into
// an opened FileMode.WRITE handle -- not JsonFileLoader. This step is about the tick,
// not about serialisation, and OnUpdate/CallLater budgets JSON out entirely (~2ms/frame
// per mod).
modded class MissionServer
{
    protected int m_DZMCP_Tick;

    override void OnInit()
    {
        super.OnInit();

        // MakeDirectory creates one level only -- $profile: itself already exists, so a
        // single call is enough for $profile:DZMCP_Bridge.
        MakeDirectory("$profile:DZMCP_Bridge");

        GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(this.DZMCP_OnHeartbeat, 1000, true);
        Print("[DZMCP_Bridge] heartbeat armed via CallLater on CALL_CATEGORY_SYSTEM");
    }

    void DZMCP_OnHeartbeat()
    {
        m_DZMCP_Tick++;
        DZMCP_WriteHeartbeat(m_DZMCP_Tick);
    }

    void DZMCP_WriteHeartbeat(int tick)
    {
        FileHandle fh = OpenFile("$profile:DZMCP_Bridge/heartbeat.log", FileMode.WRITE);
        if (fh == 0)
        {
            Print("[DZMCP_Bridge] could not open heartbeat.log for writing");
            return;
        }
        // GetTickTime(): server-clock seconds since start, a plain float -- avoids
        // GetWorld().GetDate()'s out-parameters and any dependency on in-game calendar
        // time (which can be scaled or paused), for a step that only needs to show the
        // number changing between two reads five seconds apart.
        FPrintln(fh, "tick=" + tick + " time=" + GetGame().GetTickTime());
        CloseFile(fh);
    }
}
