// 3_Game — admin-editable settings, loaded server-side from $profile:MyMod\Settings.json
// Defaults are written on first run so admins get a self-documenting file to edit.

class MyModDeviceRule
{
    string Device;                 // device classname, e.g. "MyMod_Device"
    string InputClass;             // accepted input classname (base classes allowed); NOT "Input":
                                   // vanilla declares class Input, and a field may not share a type's name
    ref array<string> Outputs = new array<string>();   // spawned on success
    float TimeSec = 5.0;           // hold duration
    float Chance = 1.0;            // 0..1 roll for bonus output (e.g. research point item)
    string BonusOutput = "";       // spawned when the roll succeeds ("" = none)
    bool ConsumeInput = true;
}

class MyModSettings
{
    int Version = 1;
    ref array<ref MyModDeviceRule> Rules = new array<ref MyModDeviceRule>();

    [NonSerialized()]
    static ref MyModSettings s_Instance;

    static MyModSettings Get()
    {
        if (!s_Instance)
        {
            s_Instance = new MyModSettings();
            LoadServer();
        }
        return s_Instance;
    }

    // Loads into the static instance created above -- the way vanilla CfgGameplayHandler
    // loads into its static m_Data (do not pass `this` as the `out` argument)
    static void LoadServer()
    {
        if (GetGame().IsMultiplayer() && !GetGame().IsServer())
            return;   // rules stay on the server; the client-side ActionCondition does not need them

        string dir = "$profile:MyMod";
        string path = dir + "\\Settings.json";
        string err;

        if (!FileExist(dir))
            MakeDirectory(dir);

        if (FileExist(path))
        {
            if (!JsonFileLoader<MyModSettings>.LoadFile(path, s_Instance, err))
                Error("[MyMod] Settings.json invalid: " + err);
            if (!s_Instance)
                s_Instance = new MyModSettings();
        }
        else
        {
            s_Instance.WriteDefaults();
            if (!JsonFileLoader<MyModSettings>.SaveFile(path, s_Instance, err))
                Error("[MyMod] cannot write default settings: " + err);
        }

        s_Instance.Validate();
    }

    void WriteDefaults()
    {
        MyModDeviceRule rule = new MyModDeviceRule();
        rule.Device = "MyMod_Device";
        rule.InputClass = "Apple";               // example: any vanilla classname
        rule.Outputs.Insert("PlantMaterial");
        rule.TimeSec = 5.0;
        rule.Chance = 0.25;
        rule.BonusOutput = "Paper";
        Rules.Insert(rule);
    }

    void Validate()
    {
        foreach (MyModDeviceRule r : Rules)
        {
            if (!GetGame().ConfigIsExisting("CfgVehicles " + r.Device))
                Error("[MyMod] unknown Device classname in Settings.json: " + r.Device);
            if (!GetGame().ConfigIsExisting("CfgVehicles " + r.InputClass))
                Error("[MyMod] unknown InputClass in Settings.json: " + r.InputClass);
        }
    }

    // Lookup used by the device; itemType = the input's classname (item.GetType()).
    // A string, not ItemBase: ItemBase is a 4_World class and 3_Game cannot see it.
    MyModDeviceRule FindRule(string deviceType, string itemType)
    {
        foreach (MyModDeviceRule r : Rules)
        {
            if (r.Device != deviceType)
                continue;
            if (itemType == r.InputClass || GetGame().IsKindOf(itemType, r.InputClass))
                return r;
        }
        return null;
    }
}
