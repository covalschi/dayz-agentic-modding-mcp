# Frameworks & open-source mods — what to reuse, what to only study

Legal status matters: code pasted into a mod must allow redistribution under that mod's own
license. The rules below take a non-commercial, share-alike mod (CC BY-NC-SA 4.0) as the
target. Rules of thumb below are per-license; when copying anything verbatim, keep the
license header and attribute. Full license map across 60+ researched mods:
patterns-cookbook.md §12; per-mod entries: mods-catalog.md. Versions and "in the modpack"
(the 54-mod modpack the catalog was compiled from) below are as of 2026-07-31; any
installed PBO can be read locally (toolchain.md §PBO inspection).

## Community Framework (CF) — DEPEND ON IT, may copy patterns

- Repo: https://github.com/Jacob-Mango/DayZ-CommunityFramework — **LGPL-2.1**.
  Workshop id 1559212036 (4.86M subscribers, actively maintained; v1.5.8 in modpack).
- Depend via `requiredAddons[] += {"JM_CF_Scripts"}`.
- Gives: `GetRPCManager()` string-keyed RPC (`AddRPC(mod, fn, instance,
  SingleplayerExecutionType.X)`, `SendRPC(mod, fn, Param, guaranteed, identity, target)` —
  all mods multiplex over engine id 10042; handler `(CallType, ParamsReadContext,
  PlayerIdentity, Object)`), CF_ModStorage versioned item persistence (`storageVersion > 0`
  in CfgMods REQUIRED + `CF_OnStoreSave/Load(CF_ModStorageMap)`), module system
  (`[CF_RegisterModule(X)]` on `CF_ModuleWorld` — clean home for a server singleton),
  CF_NetworkedVariables, NotificationSystem (server→client toasts), JSON/XML utils, SHA-256.
- Module API verified in 1.5.8 sources: `[CF_RegisterModule(X)]` + OnInit `Enable*()`
  opt-ins (EnableInvokeConnect/EnableMissionStart/EnableMissionFinish/EnableUpdate…) +
  `CF_Modules<X>.Get()` retrieval. CF_Window does NOT exist in 1.5.8.
- No-CF fallback for a singleton service: vanilla `PluginBase` + `GetPlugin(PluginX)` —
  but registration goes through modded PluginManager lists (verify per game version);
  where CF is a dependency anyway, CF modules do the same job.
- Docs in repo `docs/`. Verified API contracts: patterns-cookbook.md §1/§3.

## Dabs Framework — dependency OK, code is look-don't-copy

- Repo: https://github.com/InclementDab/DayZ-Dabs-Framework — **CC BY-NC-ND 4.0**.
  Workshop id 2545327648 (3.39M subscribers). In the modpack.
- Gives: MVC UI (`ScriptView` + auto-bound widget vars, `ViewController` +
  NotifyPropertyChanged + ObservableCollection, layout `ViewBinding`/RelayCommand,
  `ScriptViewMenu` focus handling, DIAG hot-reload), `[RegisterAction]` attribute,
  profile-settings attributes, EventManager, prefab layouts + FontAwesome-style
  imagesets (usable as tech-tree icons by reference). CfgPatches: `DF_Scripts`.
- ND license ⇒ depend at runtime, call its public classes, never redistribute its code.
- DayZ Editor (80k subs) is built on Dabs+CF — largest proof of the stack.
- `[RegisterAction(...)]` argument is the ACTION typename (verified:
  `class RegisterAction: Class { void RegisterAction(typename action_type) }`).
- GHOST-MENU TRAP (community-reported): ScriptViewMenu's `GetLayoutFile()` path must
  match the mod's $PBOPREFIX$ exactly; on mismatch the layout fails to load but the menu
  singleton stays "open" forever, blocking ALL menus — add a defensive OnShow() that
  checks GetLayoutRoot() and clears the instance on null.

## DayZ Expansion — study-only reference implementation

- Repo: https://github.com/salutesh/DayZ-Expansion-Scripts — **CC BY-NC-ND 4.0**.
  In the modpack as `@DayZ-Expansion-Bundle` (v1.9.71) + `-Licensed`.
- Do NOT copy code. DO steal architecture ideas (re-implement cleanly):
  - **Settings**: ExpansionSettingBase, JSON in `$profile:ExpansionMod\Settings\`,
    Send(identity) on connect + OnSend/OnRecieve, CallLater resend, warn-once Get().
  - **Quests**: per-quest JSON + per-objective JSON folders; objective types incl.
    ACTION (named user actions) and CRAFTING; rewards {ClassName, Amount, Attachments,
    DamagePercent, Chance}; ConfigVersion chain-migration + Validate(); one-RPC config
    push. Blueprint for processing-rule and tech-tree configs.
  - **Hardline (reputation)**: server-authoritative int on PlayerBase + netsync,
    per-SteamID .bin via FileSerializer + versioned migration; per-faction map synced on
    dirty — the research-points model. Faction persistence (FactionID) lives here.
  - **eAI factions**: `[eAIRegisterFaction]`, `player.eAI_GetFactionTypeID()`,
    `eAI_OnFactionChange` — one optional faction provider behind `#ifdef EXPANSIONMODAI`
    (a server can ship the Expansion pbos and still not run eAI, so never make it the only
    provider; patterns-cookbook.md §9).
  - **Book UI**: third-party tabs via modded ExpansionBookMenuManager — candidate host
    for the tech-tree screen (complete example: hardline factions tab).
  - **Market**: currencies = item classnames; SpawnMoney/FindMoneyAndCountTypes.
- ExpansionRPCManager (string-registered, auto ints) takes the same approach as CF's
  string-keyed RPC manager.

## TerjeMods — MAY ADAPT CODE (attribution, non-commercial)

- Repo: https://github.com/TerjeBruoygard/TerjeMods — **CC BY-NC 4.0** (no ND) — study,
  modify, integrate parts into non-commercial community projects with attribution.
- Architecture worth adapting: singleton registry (GetTerjeSkillsRegistry, TerjeSkillCfg
  with exp thresholds + perks map), accessor pattern
  (TerjePlayerSkillsAccessor.AddSkillExperience/GetSkillLevel/AddPerkLevel), own RPC
  streams, `$profile:TerjeSettings\*.cfg` server→client sync, tiered skill books
  (TerjeBookX1..5 consumables), skill-tree UI embedded in inventory (modded RightArea.c,
  per-node .layout instantiation — scales best for config-driven trees).
- TerjeRadiation is STALKER-relevant if a server swaps radiation systems.

## Zen's mods — free for any use (the permissive exception)

- https://github.com/ZenarchistCode (ZenSkills, ZenNotes, many more) — author explicitly
  allows repacking/any use. The ONLY permissively reusable skill-tree source.
- ZenSkills: keybound full-screen skill menu, ZenSkillsPlayerDB.c (wipe-safe separate
  per-UID DB), ZenExpNerfDef.c (anti-AFK XP nerf — reuse against point farming),
  client/server config split. ZenNotes: per-item ref-class persistence +
  ServerReplaceItemInHandsWithNew lambda (sample items carrying experiment state).

## Admin tools (structure references)

- **COT** https://github.com/Jacob-Mango/DayZ-CommunityOnlineTools — **CC-BY-SA-4.0**
  (share-alike is viral and BY-SA is not BY-NC-SA: reuse structure, never paste code into
  a CC BY-NC-SA mod). 2.23M subs, maintained.
  Canonical modular admin pattern: JMModuleBase registry, permission node tree checked
  server-side per RPC, RPC enum in 3_Game, UI forms in 5_Mission, Example module.
- **VPP Admin Tools** — copyright (source: github.com/VanillaPlusPlus/VPP-Admin-Tools).
  Pattern: namespaced permission strings verified in every RPC handler + bulk
  map<string,bool> client reply; ConfigurablePlugin JSON base; own engine RPC id (10420).
- **ZomBerry** — GPL-3.0, stale since 2022 — prefer COT.

## Bohemia DayZ-Samples — copy freely within DayZ modding (ADPL-SA)

- https://github.com/BohemiaInteractive/DayZ-Samples — Test_Crafting (recipe +
  PluginRecipesManagerBase + CfgMods wiring), Test_Inputs (keybinds), Test_GardenPlot etc.

## Other verified references

- **TraderX** (github.com/TheDmitri/TraderX, open code / no-repack): the best modern
  EnforceScript architecture seen — DDD layering, repository pattern, event bus, JSON
  config folder-per-concern, companion browser config editor. Blueprint for pairing a JSON
  schema with a web editor.
- **Syberia Project** — custom license: derivatives must be public GitHub forks. Do NOT
  copy code into a mod; skill-tree UI ideas only.
- **Rejects Quest Mod** — MIT (only freely-adaptable quest framework found);
  **CraftSystem** — modify+repack OK (hot-reload + offline-completing timed crafts).
- **StarDZ-Team/Dayz-Modding-Skills** (MIT) + **DayZ-Modding-Wiki** (CC BY-SA, RU
  available) — public AI skill + wiki (105 chapters; worth a local clone); legally minable
  with attribution.
- **Agentic-Z** (DayZ-n-Chill) — agent stack + dayz-rag MCP (vector index over vanilla
  sources) — complementary tooling idea.
- Script API reference (searchable vanilla classes): https://dayz-scripts.yadz.app/

## Modpack context

The 54-mod STALKER RP server modpack that the catalog was compiled from (2026-07-31
snapshot): licenses of its mods are in patterns-cookbook.md §12 and the per-mod entries.
The catalog of its mods + top workshop mods: **references/mods-catalog.md**.
