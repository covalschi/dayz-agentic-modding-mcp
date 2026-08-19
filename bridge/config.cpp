// DZMCP_Bridge -- server-side bridge mod for dayz-agentic-modding-mcp.
//
// This mod belongs to the MCP server, not to any project it drives: it lives
// in this repository, is packed by this repository's own packer.pack_one,
// and is never referenced by a project's profile except through mods.extra.
//
// Phase 2 Task 5: the mod reads a command mailbox, dispatches it and publishes
// a state document once a second. Task 1 proved the tick itself fires
// (GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater, measured at a clean 1 Hz on a
// live boot); everything here is built on that measurement.

class CfgPatches
{
    class DZMCP_Bridge
    {
        units[] = {};
        weapons[] = {};
        requiredVersion = 0.1;
        requiredAddons[] =
        {
            "DZ_Data",
            "DZ_Scripts"
        };
    };
};

class CfgMods
{
    class DZMCP_Bridge
    {
        dir = "DZMCP_Bridge";
        name = "DZMCP Bridge";
        author = "dayz-agentic-modding-mcp";
        version = "0.2.0";
        type = "mod";
        // "World" is declared alongside "Mission" from Task 5 onwards, not from
        // Task 7 where the action delivery it exists for is actually written.
        // Reason (brief R1): ActionManagerServer keeps the action data the
        // engine reads in a PROTECTED member declared in the 4_World tier
        // (m_CurrentActionData, actionmanagerbase.c:61), and 5_Mission can see
        // that class but cannot write its protected members. Delivering an
        // action from a modded MissionServer therefore leaves the member null
        // and StartDeliveredAction() returns on its first line without logging
        // anything at all -- a silent no-op indistinguishable from success.
        // Adding a compile tier also changes the boot compile surface, so it
        // has to be in place for the very first boot this task is measured by,
        // not bolted on after that boot has already been paid for.
        dependencies[] = {"Game", "World", "Mission"};

        class defs
        {
            class worldScriptModule   { value = ""; files[] = {"DZMCP_Bridge/scripts/4_World"}; };
            class missionScriptModule { value = ""; files[] = {"DZMCP_Bridge/scripts/5_Mission"}; };
        };
    };
};
