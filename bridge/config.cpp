// DZMCP_Bridge -- server-side bridge mod for dayz-agentic-modding-mcp.
//
// This mod belongs to the MCP server, not to any project it drives: it lives
// in this repository, is packed by this repository's own packer.pack_one,
// and is never referenced by a project's profile except through mods.extra.
//
// Phase 2 Task 1: heartbeat-only probe. Proves whether GetGame().GetCallQueue
// (CALL_CATEGORY_SYSTEM).CallLater(...) actually fires inside a MOD -- an
// earlier probe placed in a mission's init.c did not see it fire, and every
// later task (command dispatch, world actions) depends on the answer.

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
        version = "0.1.0";
        type = "mod";
        // Only the mission tier is used at this step -- MissionServer exists
        // server-side only, which is what makes this mod inherently
        // server-only without any extra guard.
        dependencies[] = {"Mission"};

        class defs
        {
            class missionScriptModule { value = ""; files[] = {"DZMCP_Bridge/scripts/5_Mission"}; };
        };
    };
};
