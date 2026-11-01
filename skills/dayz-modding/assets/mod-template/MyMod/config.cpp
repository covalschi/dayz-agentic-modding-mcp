class CfgPatches
{
    class MyMod
    {
        units[] = {};
        weapons[] = {};
        requiredVersion = 0.1;
        requiredAddons[] =
        {
            "DZ_Data",
            "DZ_Scripts",
            "JM_CF_Scripts"        // CF: RPC + ModStorage (remove if not using CF)
        };
    };
};

class CfgMods
{
    class MyMod
    {
        dir = "MyMod";
        name = "My Mod";
        author = "YourName";
        version = "0.1";
        type = "mod";
        storageVersion = 1;        // CF_ModStorage opt-in
        dependencies[] = {"Game", "World", "Mission"};

        class defs
        {
            class gameScriptModule    { value = ""; files[] = {"MyMod/scripts/3_Game"}; };
            class worldScriptModule   { value = ""; files[] = {"MyMod/scripts/4_World"}; };
            class missionScriptModule { value = ""; files[] = {"MyMod/scripts/5_Mission"}; };
        };
    };
};

class CfgVehicles
{
    class Inventory_Base;

    class MyMod_Device : Inventory_Base
    {
        scope = 2;
        // NOTE prefix contexts: config.cpp uses "$STR_key"; scripts/layouts use "#STR_key";
        // inputs.xml loc= takes the bare key (verified against vanilla + production mods)
        displayName = "$STR_mymod_device";
        descriptionShort = "$STR_mymod_device_desc";
        model = "\dz\gear\cooking\CookingStand.p3d";   // placeholder vanilla model (cooking, NOT camping — verified against gear_cooking.pbo)
        weight = 6000;
        itemSize[] = {4, 3};
        itemsCargoSize[] = {4, 2};
        rotatable = 1;
    };
};
