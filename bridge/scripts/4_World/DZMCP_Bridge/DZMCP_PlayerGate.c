// Modded PlayerBase: one diagnostic primitive the action verb needs on a
// headless stand.
//
// Vanilla constructs a player's ActionManagerServer inside OnSelectPlayer()
// (playerbase.c:6085) -- a path only a CONNECTING CLIENT ever takes. A
// survivor conjured with CreateObjectEx never passes through it, so its
// m_ActionManager is null (measured on this stand: every delivery aimed at a
// conjured survivor answered "no server action manager"). The member is
// `ref protected` on PlayerBase, which is exactly why this lives here: a
// modded PlayerBase may write its own protected member, and nothing above
// this tier can.
//
// The construction below mirrors vanilla's own line for the server instance
// types vanilla itself gives a server manager to (playerbase.c:6083-6100:
// INSTANCETYPE_SERVER, and INSTANCETYPE_AI_SINGLEPLAYER; INSTANCETYPE_AI_SERVER
// is included because it is the AI-on-server counterpart of the same case).
// Anything else -- a client instance, a remote -- is refused with the type
// named, because refusing with evidence is the whole point of this bridge.
//
// No `extends` clause (brief R3): an inheritance clause on a `modded class`
// silently detaches it from the modded chain.
modded class PlayerBase
{
    // Make sure this player has a server-side action manager, creating one the
    // way vanilla itself would have. Returns "" on success (including "one was
    // already there"), or a refusal naming the instance type.
    //
    // Called ONLY from the bridge's `action` verb, and only on its
    // subject-by-class path: a connected player always has a manager already,
    // so for them this is a no-op by the first check.
    string DZMCP_EnsureServerActionManager()
    {
        if (!GetGame() || !GetGame().IsDedicatedServer())
            return "the bridge action gate is inert outside a dedicated server";

        if (m_ActionManager)
            return "";

        int instanceType = GetInstanceType();
        bool serverSide = instanceType == DayZPlayerInstanceType.INSTANCETYPE_SERVER || instanceType == DayZPlayerInstanceType.INSTANCETYPE_AI_SERVER || instanceType == DayZPlayerInstanceType.INSTANCETYPE_AI_SINGLEPLAYER;
        if (!serverSide)
            return "instance type " + instanceType + " does not host a server action manager";

        m_ActionManager = new ActionManagerServer(this);
        return "";
    }
}
