# Mod template — minimal script mod skeleton

Copy `MyMod/` as the starting point for a new DayZ script mod, then rename everything
(`MyMod` → your mod name; keep the prefix consistent: folder name, `$PBOPREFIX$`,
`CfgMods.dir`, paths in `files[]`).

```
MyMod/
├── $PBOPREFIX$                  # PBO prefix override (single line: MyMod)
├── config.cpp                   # CfgPatches + CfgMods + example item
├── stringtable.csv              # localization: vanilla 14-column form (UTF-8 BOM, CRLF,
│                                #   trailing comma), Ukrainian original + English
└── scripts/
    ├── 3_Game/MyMod/MyModSettings.c      # admin JSON settings ($profile) skeleton
    ├── 4_World/MyMod/MyModDevice.c       # example device item + continuous action
    └── 5_Mission/MyMod/MyModMission.c    # modded MissionServer: load/save/sync hooks
```

A rule in `$profile:MyMod\Settings.json` is `{Device, InputClass, Outputs, TimeSec, Chance,
BonusOutput, ConsumeInput}`. The field is `InputClass`, not `Input`: vanilla declares
`class Input`, and a field may not share a type's name. The server loads and applies the
rules; a multiplayer client checks only that the action's target is the device.

Release layout. With the `dayz` MCP, `mod_build` packs with FileBank and signs
with DSSignFile (references/toolchain.md). Without it: Addon Builder + DSUtils -- Addon
Builder's CLI failed headless in the setup measured (references/toolchain.md).

```
@MyMod/
├── addons/MyMod.pbo + MyMod.pbo.<Key>.bisign
├── keys/<Key>.bikey
└── mod.cpp
```

The scripts call only vanilla API; nothing calls CF yet. `JM_CF_Scripts` in requiredAddons
and `storageVersion = 1` are there for CF RPC and CF ModStorage once you use them (the
commented `GetRPCManager` line in MyModMission.c). Without CF, drop `JM_CF_Scripts` and
send with vanilla ScriptRPC instead.
