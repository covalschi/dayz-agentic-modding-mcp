# Patterns cookbook — verified techniques from 60+ production mods (2026-07-31 research)

Distilled from direct PBO extraction/decompilation of a 54-mod STALKER RP server modpack plus
web research of top workshop mods. Every pattern below was seen in real shipped code (source
mod named in parentheses). Full per-mod entries: references/mods-catalog.md. That modpack
(2026-07-31 snapshot) is the evidence base here.

## 1. RPC and config sync

- **CF RPC exact contract** (CF source): `GetRPCManager().AddRPC(modName, fnName, instance,
  SingleplayerExecutionType.Server|Client|Both)`; `SendRPC(modName, fnName, new ParamN<...>,
  guaranteed, identity, target)`. Handler signature `(CallType type, ParamsReadContext ctx,
  PlayerIdentity sender, Object target)` and must `ctx.Read(ParamN<...>)`. All mods share
  engine RPC id 10042; routing is two strings in the payload; mistyped names only warn in logs.
- **Whole-object RPC serialization**: an entire config class serializes through one
  `Param1<ref ConfigClass>` RPC — no per-field packing (AgricultureCore, KK mods, MMG,
  SuppressionMod: `ScriptRPC().Write(instance)` / `ctx.Read(instance)` works for ref classes).
  CAVEAT: `map<string, ref T>` does NOT survive RPC serialization intact — rebuild maps
  entry-by-entry on the client (CannabisPlus).
- **Sync flow** (Expansion, KK, WT — same shape): server loads JSON in MissionServer
  init; on client connect (`OnClientReadyEvent` / CF module `OnClientPrepare` /
  `InvokeOnConnect`) send config to that identity; client keeps a `s_ConfigReceived` flag and
  blocks dependent actions until synced; Expansion adds a CallLater(20000) resend path.
- **CF module event order** (verified in Expansion comments): new char connect =
  OnClientPrepare → OnClientNew → OnInvokeConnect; existing = OnClientPrepare →
  OnClientReady → OnInvokeConnect; respawn = OnClientRespawn → OnClientPrepare →
  OnClientNew → OnInvokeConnect. Load per-player files in OnClientPrepare, attach to
  PlayerBase in OnInvokeConnect, drop cache in OnClientDisconnect.
- **RPC id hygiene**: never hardcode small ints. Options: CF string-keyed manager;
  enum with large sentinel base (WT: 125237, 57907529; ZenNotes: negative bases
  like -3761990); claim a private engine id via modded `DayZGame.OnRPC` (VPP: 10420);
  Expansion auto-assigns ints to string-registered RPCs. Same applies to extended vanilla
  enums (`ERPCs`, `eModifiers`, `EPlayerStats_current`) — collisions are silent.
- **One "Json class" for everything** (WT architecture, reusable): model every config/DB
  table as a plain script class of primitives + arrays; the SAME class is (a) loaded via
  JsonFileLoader, (b) written straight into ScriptRPC via ctx.Write/Read, (c) held on a
  client singleton. Unifies persistence + sync + admin editing with one type.

## 2. Admin JSON configs

- **Loader API**: prefer `JsonFileLoader<T>.LoadFile(path, obj, errorMessage)` /
  `.SaveFile(path, obj, errorMessage)` (bool + error string; KK_C4_Timer, IAT). Create `obj`
  with `new` first and pass a variable or member, never `this` (vanilla CfgGameplayHandler
  loads into its static `m_Data`). The old `JsonLoadFile` logs `Cannot load data from ...`
  through ErrorEx on malformed JSON but returns nothing to the caller, which carries on with
  whatever the object held (`3_game/tools/jsonfileloader.c:105-131`); InediaInfectedAI instead wraps
  `JsonSerializer.ReadFromString` to capture the parse error and `Error()`s naming the
  broken file/parameter — copy this. `[NonSerialized()]` keeps runtime fields out of JSON.
- **Load-then-save idiom**: JsonLoad immediately followed by JsonSave both autogenerates
  defaults on first start and transparently adds new fields to existing configs after mod
  updates (Renegade Snork, StalkerAmbience + dedicated `UpgradeConfig()` hook).
- **ConfigVersion + migration** (Expansion best-in-class): int `ConfigVersion` in every
  JSON root; static Load() reads base class first, chain-migrates version-by-version,
  re-saves; `Validate()` after load (skip broken entries, log, never crash the server);
  classnames validated at use with `ConfigIsExisting`/`GetGame().ConfigIsExisting`.
  CannabisPlus adds: backup old JSON to `ConfigBackup/` before regenerating, and key
  renames migrated by string-replace over the raw file.
- **Per-entity JSON folders**: one JSON per device/recipe in
  `$profile:Mod/<Folder>/<Name>.json`, enumerate with `FindFile(path + "*.json")` /
  `FindNextFile`; "delete the folder to regenerate defaults" semantics (CannabisPlus
  strains, Expansion quests). Better fit for processing rules than one monolithic file.
- **Classname matching by inheritance** (MPG_TDM_Logs convention): match config entries
  via `GetGame().IsKindOf(type, entry)` so admins can write base classes; explicit `|1`
  suffix for exact-match.
- **Hot reload**: poll the $profile JSON every 60s (CraftSystem), or reload on admin chat
  command in `MissionServer.OnEvent(ChatMessageEventTypeID)` with commands prefixed `\`
  gated by an AdminsSteamIds JSON (Inedia), or a separate AutoReload server PBO (Ibourbon).
- **Config UX details**: when a recipe is disabled by config, tell the player "disabled by
  admins" instead of silently hiding the action (FreezeMining — kills false bug reports).
  Ship an `explanation.txt` next to generated configs and a dedicated log folder (RadZone).
  Per-mod `$profile:` subdir is universal: `$profile:<Mod>\` + MakeDirectory first.
- **Simple scalars**: some top mods read custom vars straight from serverDZ.cfg
  (`dogtagsPlaytimeRequired`) — zero-dependency option for one-off toggles.

## 3. Persistence

- **CF ModStorage contract** (CF source): set `storageVersion = N;` (int > 0) in your
  CfgMods class or ModStorage silently does nothing. Override
  `CF_OnStoreSave/CF_OnStoreLoad(CF_ModStorageMap storage)`; `ctx = storage["YourModName"]`;
  typed Write/Read; gate new fields with `ctx.GetVersion()`. Real-world usage wraps in
  `#ifdef CF_MODSTORAGE` with a vanilla OnStoreSave/OnStoreLoad fallback (IAT).
- **Vanilla item persistence**: plain `ref DataClass` + `ctx.Write(m_Data)`/`ctx.Read` in
  OnStoreSave/OnStoreLoad works for custom item state (ZenNotes). Order must match
  save↔load; version-gate reads. For player stats: OnStoreSave/OnStoreLoad +
  `AfterStoreLoad()` + `SetSynchDirty()` + RegisterNetSyncVariableFloat for live display
  (WTHealth).
- **Per-player server DB options** (research points): (a) Expansion Hardline model —
  server-authoritative int on modded PlayerBase, `RegisterNetSyncVariableInt` +
  SetSynchDirty for live sync, persisted as `$profile:...\PlayerData\<SteamID>.bin` via
  FileSerializer with CONFIGVERSION + versioned OnRead migration; richer maps
  (per-faction) synced by explicit RPC only when dirty. (b) JSON per UID (KK soft-skill
  carryover: save in EEKilled, restore+delete in OnClientNewEvent). (c) Virtual DB JSON
  (PersonalChestWT: items as classname+quantity+health+attachments records keyed by
  `identity.GetPlainId()`).
- **Engine PlayerStats (soft skills)**: register via `modded class PlayerStatsPCO_current
  { override Init() { RegisterStat(EPlayerStats_KK.X, new PlayerStat<float>(min,max,def,
  "Name", EPSstatsFlags.SYNCED)); } }`; read via `PlayerStat<float>.Cast(
  player.GetPlayerStats().GetStatObject(id))`. Engine persists per-life automatically.
  WARNING: stat IDs come from `enum X: EPlayerStats_current` inheritance — two mods doing
  this collide silently. KK_SkillFramework (KarmaKrew pack) already does this
  (2026-07-31 modpack snapshot) → a mod that may share a server with it must NOT register
  PlayerStats; keep points in its own server-side DB. 1.30 added a stats version:
  `EPlayerStats_current` now extends `EPlayerStats_v130` (new `THERMAL_BIAS`) and
  `PlayerStatsPCO_current` extends `PlayerStatsPCO_v130` (`playerstatspco.c:356-365`).
- **CRITICAL pitfall — timers are not persisted**: processing driven by Timer/CallLater
  silently cancels on server restart (CannabisPlus workbench/drying leave slots locked).
  Persist remaining time or absolute completion timestamp in OnStoreSave and resume in
  AfterStoreLoad. Engine timers also degrade after ~4h uptime (DNA Keycards author) —
  reconcile against timestamps, don't trust long timers.
- **Static map-object state without entities** (CBD): JSON registry in $profile keyed by
  world position (`vector.Distance < 1.0` match) + sub-index; restore on mission start via
  CallLater-deferred init; blank secret fields before broadcasting to clients.

## 4. Processing stations (the core mechanic)

- **Deployable workbench blueprint** (CannabisPlus CP_Workbench — production-proven at
  2.4M subs): ItemBase subclass; const attachment-slot names as roles (input / consumable
  / tool / battery / output); server-side `Timer.Run(period, ..., true)` calling
  DoProcessing per cycle; `RegisterNetSyncVariableBool` running/paused + SetSynchDirty;
  `GetInventory().SetSlotLock` + LockToParent while running;
  `CreateAttachment(outputClass)` + AddQuantity for outputs; battery energy via
  `GetCompEM().AddEnergy(-cost)` or a VehicleBattery in a LargeBattery slot with
  config-defined cost per cycle; `CanDisplayAttachmentSlot`/`CanDisplayAttachmentCategory`
  to show slots conditionally.
- **Station interaction shape** (BBP workbench, 2.8M subs): dedicated material slots +
  catalyst item held in hands (blueprint/document) + user action on the station — matches
  "sample in device, protocol in hands" RP flavor.
- **Deployable kit pattern**: kit item `IsDeployable()=true` +
  AddAction(ActionTogglePlaceObject/ActionDeployObject); modded Hologram
  (`GetProjectionName` override is the clean hook); OnPlacementComplete (server):
  `CreateObjectEx(deviceClass, pos, ECE_PLACE_ON_SURFACE)`, transfer attachments via
  `ServerTakeEntityToTargetAttachmentEx`, HideAllSelections + CallLater(Delete, 0); a
  `_Holo` class for the preview. Config: hologramMaterial, hologramMaterialPath,
  slopeTolerance, yawPitchRollLimit[], alignHologramToTerain (MMG, IAT, CannabisPlus, GSC
  Yanova pot).
- **Recipe selection without UI**: one ActionContinuousBase exposes N menu variants —
  ctor subscribes `GetVariantManager().GetOnUpdateInvoker()`; callback calls
  `SetActionVariantCount(n)`; `m_VariantID` = chosen index; pass to server via custom
  ActionData subclass overriding SetupAction/WriteToContext/ReadFromContext (IAT_MoreDoors).
- **Correct output spawning into inventory** (TraderInventoryRotationFix): create
  ECE_LOCAL probe entity → iterate player/container attachment containers →
  `GetInventory().FindFreeLocationFor(probe, FindInventoryLocationType.CARGO, loc)`
  (rotation-aware) → `LocationCreateEntity(loc, classname, ECE_IN_INVENTORY, RF_DEFAULT)`
  → delete probe; fall back to ground spawn.
- **Item transform preserving state**: `MiscGameplayFunctions.TurnItemIntoItemEx` with a
  custom TurnItemIntoItemLambda subclass (`OverrideNewLocation` keeps result in hands);
  read GetHealth01/GetWet/GetAgents before conversion, apply to outputs (Tactical Flava
  disassemble action = full working example). In-hands variant:
  `player.ServerReplaceItemInHandsWithNew(lambda)` carrying custom data (ZenNotes).
- **Config-driven outputs without JSON**: read a per-item CfgVehicles `Resources` subclass
  via ConfigGetChildrenCount/ConfigGetChildName/ConfigGetInt — third-party packs declare
  processing results in their own config.cpp (AgricultureCore RipOpenBrick).
- **Chance mechanics**: weighted quality tiers (50/35/15) and a weighted-drop JSON map
  (FreezeMining minedItemsProbabilities) — ready schema shape for Chance/BonusOutput
  research-point drops. Roll server-side only.
- **Input wear as balancing knob**: MCK_Grinder damages the master item per copy.
- **Result documents**: server action spawns a Paper subclass, fills
  RegisterNetSyncVariableFloat fields + SetSynchDirty, persists in OnStoreSave; client
  action opens a UIScriptedMenu reading the synced fields (AMS_TestKitReport) — model for
  analysis reports / sample items carrying experiment state.
- **Registry API for extensibility**: static `ref map<string, ref Entry>` + global
  `Register()` that addon mods call in MissionServer.OnInit — must run before storage load
  so restarted entities resolve (AgricultureCore AC_RegisterPlant, IAT_Smelting). Expose
  `<Prefix>_RegisterDevice/<Prefix>_RegisterRecipe` alongside JSON.

## 5. Devices (portable and static)

- **Handheld electronic device pattern** (AnomalySystemWT detectors — canonical):
  Inventory_Base + `class EnergyManager` config (battery attachment, energyUsagePerSecond,
  autoSwitchOffWhenInCargo) + RegisterNetSyncVariable* for on/off/mode + `OnWork(consumed)`
  for periodic server work + OnVariablesSynchronized for visuals/sounds + private device
  RPC enum for one-shot effects. OpenableBehaviour for lid animation.
- **Device-with-UI pattern** (PDAWT — closest to a portable computer): worn Clothing
  item in a custom slot (see DLCSlots below) + EnergyManager; in-hands
  ActionSingleUseBase.OnExecuteClient opens a UIScriptedMenu; a global singleton
  (`Get_PDAClient()`) holds menu refs and dispatches a device RPC enum to Handle*(ctx)
  methods; item state in RegisterNetSyncVariableInt fields; server data as Json classes
  streamed over RPC.
- **Custom wearable slot** (DLCSlots — plaintext config, clean reference): SurvivorBase
  `attachments[]+=` + InventoryEquipment `playerSlots[]+=` + CfgSlots with ghostIcon from
  an imageset + ProxyAttachment in CfgNonAIVehicles — the quartet for a dedicated device
  slot.
- **Twin-class static/portable device** (GSC Yanova pot — adopt for station devices): one model
  exposed both as portable item (extends FireplaceBase/Inventory_Base) and static `Land_*`
  object (extends HouseNoDestruct or a functional base). Map statics inherit full vanilla
  interactivity purely via config parent (extends Well → drink actions,
  BuildingWithFireplace → fireplace UI); custom ActionContinuousBase on a HouseNoDestruct
  static is proven (NH nh_OldWell).
- **Device state machine + visuals without UI** (KK BiometricScanner pattern): state enum
  + RegisterNetSyncVariableInt(state) + RegisterNetSyncVariableBoolSignal for one-shot
  effects; visuals in OnVariablesSynchronized via SetObjectMaterial (screen on/off rvmat),
  SetObjectTexture per digit, SetAnimationPhase. `IsStatic()` override so map-placed
  statics skip power checks — model for static vs portable split.
- **Inventory UI slot grouping**: GUIInventoryAttachmentsProps groups a device's slots
  into labeled icon-tagged sections (Input / Consumables / Output) — GSC Yanova.
- **Cross-mod slots**: custom attachment slots are the interop currency — CfgSlots +
  ProxyAttachment + inventorySlot[]; a device can accept a FOREIGN mod's slot by name
  (SGE artifact container accepts NH's `NHArt1` slot) without hard dependency.
- **Detector tiering** (STALKER detector mods): tiered devices with per-tier config of
  detectable classnames + radius — template for "better microscope processes more inputs".
- **Anomaly/station ambience**: statics spawning ScriptedLightBase + Particle.PlayOnObject
  + looped sound in constructor, IsClient-guarded (AnomalySystemWT); PPERequesterBank
  requesters for screen effects while device active (WTHealth).
- **Virtual storage device**: real cargo {0,0} + server-side Json DB of
  classname/quantity/health/attachments + two-pane transfer UI over an RPC enum
  (PersonalChestWT) — avoids CE persistence limits for research-output storage.
- **In-place tier upgrade** (MMG): ActionContinuousBase validates materials in dedicated
  slots then ObjectDelete + CreateObject(nextTierClass) preserving pos/orientation.
- **Lock player during work**: server→client ScriptRPC toggling
  PlayerControlDisable(INPUT_EXCLUDE_ALL) (Izlom grab) — usable for "device busy" states.
  `Mission.PlayerControlDisable/Enable` are marked `//!deprecated` (`3_game/gameplay.c:795-796`,
  1.29 and 1.30); new code uses `AddActiveInputExcludes` / `RemoveActiveInputExcludes`.

## 6. Static station placement (admin side)

- **DayZ Editor Loader** (in modpack): spawns .dze (JSON) files from
  `$mission:\EditorFiles` at mission start — admins place a mod's stations with DayZ Editor
  (80k subs, open source, built on Dabs+CF); a framework only needs to make placed
  classnames interactive. DayZ Editor exports to init.c / .vpp / spawner JSON — devices
  must work via standard object-spawn paths.
- **Trader-style object list**: TraderObjects.txt spawns arbitrary objects/NPCs at
  configured pos+ypr at server start (Dr. Jones) — same idea as a `<Mod>_StaticDevices.json`.
- **Per-instance device config**: array of {classname, position, per-device flags}
  (Enhanced Banking) — prior art for admin-designated static stations with instance flags.
- **Attach behavior to existing map objects**: ActionInteractBase whose ActionCondition
  matches target classname against a configured list (Dr. Jones ActionTrade on arbitrary
  NPC/prop classes; CBD position-keyed registry for map buildings).

## 7. Mutant parts and processing inputs

- **Corpse loot is config-only in vanilla DayZ** (canonical way admins get mutant parts):
  a `class Skinning { class ObtainedX { item=; count=; itemZones[]=;
  quantityMinMaxCoef[]=; countByZone[]= } }` block inside the creature's CfgVehicles
  entry, harvested by knife-skinning; script side only needs
  `override bool CanBeSkinned() { return true; }`. No EEKilled handler (all WT mutants).
- **Add drops to ANY creature without touching its mod**: modded
  `ActionSkinning.OnFinishProgressServer`, check `body.IsInherited(SomeBase)`, spawn a
  JSON-configured item list via `GetGame().CreateObject(type, pos).PlaceOnSurface()`
  (Renegade Snork) — closes the gap when a creature only drops vanilla meat.
- **Boss-tier inputs**: bosses list multiple ObtainedX blocks (rare unique trophy at
  quantityMinMaxCoef 1.0 + bulk HumanSteakMeat by zone) — rare trophies = high-tier
  research inputs. Variants as thin scope=2 subclasses of a scope=0 _Base share one loot
  table.
- **Token-gated knowledge as inputs**: blueprint/schematic/sketch items that teach recipes
  (RA_Blueprint radial menu, NCPR recipe-sketches, Namalsk dzn_blueprint_lehs +
  dzn_module_* gating suit assembly) — documents/data-carriers are both processing inputs
  AND tradeable RP items.

## 8. Points, skills, progression

- **No tech-tree/research-tree mod existed on the DayZ workshop** (verified July 2026).
  Nearest others: perk trees (Terje, Zen), RELIFE perk-gated actions.
- **Points storage**: see §3 per-player DB options. Mutate server-side only; sync via
  netsync int (live) or RPC-on-dirty (maps). Faction pools = map<factionID, int> synced on
  change.
- **Terje architecture** (CC BY-NC, adaptable with attribution): singleton config registry
  (GetTerjeSkillsRegistry, TerjeSkillCfg with array<int> exp thresholds + perks map);
  per-player profile behind accessor class
  (TerjePlayerSkillsAccessor.AddSkillExperience(skillId, value, affectModifiers,
  showNotification)); own RPC layer + server→client settings sync; settings in
  `$profile:TerjeSettings/*.cfg`.
- **Zen's ZenSkills** (github.com/ZenarchistCode/ZenSkills — author allows any use): the
  only permissively reusable skill-tree source. Study ZenSkillsPlayerDB.c (wipe-safe
  separate DB), ZenExpNerfDef.c (anti-AFK XP nerf — reuse against research-point farming),
  client/server config split.
- **XP-source config patterns**: TriggersConfig.json (action category → XP amount, HGC)
  and JSON lists of crafts/actions granting XP (RELIFE) — same shape as the mod template's
  {Device, InputClass, Outputs, TimeSec, Chance} rules (not `Input`: vanilla declares
  `class Input`, and a script field may not share a type's name). Terje alternative: scriptable areas
  with 'exp' multiplier effects — consider zone multipliers (labs give bonus).
- **Consumable progression items**: tiered skill books (TerjeBookAthletic1..5 via
  ActionTerjeReadSkillbook), EXP-boost/perk-reset injectors (Zen) — "research data" items
  can ship the same way.
- **KK_SkillFramework (KarmaKrew pack, 2026-07-31 snapshot)**: reserves
  PlayerStats via `enum EPlayerStats_KK: EPlayerStats_current { STAMINASOFTSKILL,
  COLDRESISTANCE }`. Any parallel enum collides → a research mod keeps points OUT of
  PlayerStats (own JSON DB).

## 9. Faction gating

- **Ask a service, not a mod**: put "which faction is this player in?" behind a small core
  service (4_World, answers server-side): `MyMod_Identity.Get().FactionOf(uid)`. The core
  declares the interface and keeps an EMPTY default implementation (no faction, neutral
  answers), never null; the optional factions mod fills it with
  `MyMod_Identity.Provide(...)` from its `OnMissionStart`. The caller never checks whether a
  factions mod is loaded: "mod absent" becomes an answer instead of a crash, and every
  other mod runs with the core alone.
- **Expansion AI factions are an optional provider**: keep every line that touches them
  behind `#ifdef EXPANSIONMODAI` (a provider of such a service may read them). The next two
  entries are their API as seen in the modpack.
- **The modpack's factions (2026-07-31 snapshot)** come from
  `@Stalker Anomaly - Expansion Factions` (Anomaly-Factions.pbo, 12.7KB, extracted): 10 eAIFaction subclasses —
  eAIFactionClearSky ("Чистое небо"), eAIFactionEcologist (= "Учёные"), Duty, Freedom,
  Loner, Mercenary, Military, Monolith, Renegade, Bandit. Registration:
  `class X: eAIFaction` + `[eAIRegisterFaction(X)]` + `m_Loadout` name + IsFriendly()
  override; guarded `#ifdef EXPANSIONMODAI`.
- **Detect a player's faction** (Expansion AI API): `player.GetGroup().GetFaction()` or
  `player.eAI_GetFactionTypeID()`; resolve via `eAIFaction.GetTypeByID(id)`; iterate
  `eAIRegisterFaction.s_FactionTypes`; react via override
  `PlayerBase.eAI_OnFactionChange(oldID, newID)` (server, guarded
  EXPANSIONAI_ONFACTIONCHANGE). Persistent FactionID lives in Hardline player data when
  `EnableFactionPersistence=true` (group recreated on connect). Config should reference
  factions by classname string ("eAIFactionEcologist").
- **Alternative gating in the modpack (2026-07-31 snapshot)**: KarmaKrew
  territory flags (BP_FlagPole_Base holds GroupHash; PlayerBase net-syncs m_GroupHash; FindNearestFlag(position) lookup). LBmaster
  Groups is NOT active (only LBmaster_Core). Gear-based gating: SGE Ecolog/ClearSky suits
  as required-worn-item conditions (WTHealth-style classname bundles).
- **Precedents**: TraderPlus purchasable/assignable "trade licenses" gate access;
  keycard mods (DNA) use server-assigned item uses.

## 10. UI approaches for a tech tree

- **Dabs MVC** (in modpack, dependency OK): ScriptView subclass + GetLayoutFile();
  widget member vars auto-bound by name; ViewController fields + NotifyPropertyChanged +
  ObservableCollection for node lists; .layout binds via scriptclass "ViewBinding" +
  ScriptParamsClass { Binding_Name ... }; RelayCommand → controller bool methods;
  ScriptViewMenu handles focus/cursor; DIAG hot-reload; FontAwesome-style imagesets in
  Dabs GUI.pbo usable as node icons (reference, don't repack).
- **Expansion Book tab** (proven third-party host): modded ExpansionBookMenuManager
  { override RegisterBookMenuTabs { m_Tabs.Insert(new MyTab(book_menu)); super...; } };
  a tab = ExpansionScriptView with GetLayoutFile/GetControllerType/GetTabName/
  GetTabIconName/CanShow (complete example: hardline expansionbookmenutabfactions.c).
- **Native-widget skill trees** (both shipped without Dabs): Terje embeds in inventory
  screen (modded RightArea.c, dynamic instantiation of small per-node .layout files +
  tooltips — scales best for config-driven trees); Zen uses a keybound full-screen
  UIScriptedMenu (one big .layout + unlock sounds).
- **Native admin editor** (WT framework): UIScriptedMenu + TextListboxWidget search list +
  EditBox/XComboBox + ItemPreviewWidget + MapWidget; "search list + copy-classname +
  ItemPreviewWidget" combo is the recipe for admin recipe-input pickers.
- **Item tooltips**: modded ItemManager can swap/extend the vanilla tooltip layout and
  fill values from config lookups for ANY mod's items (Delta UI, WTHealth PrepareTooltip
  imageset overlay) — show research value / recipe hints on items.
- **Notifications**: three options — vanilla NotificationSystem (verified,
  3_Game/client/notifications: `SendNotificationToPlayerExtended(player, showTime,
  title, text, icon)` / `SendNotificationToPlayerIdentityExtended(identity_or_null_for_
  broadcast, ...)` server-side, `AddNotificationExtended(...)` client-local; icon =
  "set:dayz_gui image:icon_info" -- UNVERIFIED: no `icon_info` among the 321 images of the
  `dayz_gui.imageset` extracted on P:\ (pre-1.30); check the icon name before use);
  CF NotificationSystem.CreateNotification(...)
  (StringLocaliser-based); Notifications-mod public static API if loaded. Vanilla one
  needs zero dependencies — default for "points awarded" toasts.
- **HUD widgets**: mission-level CreateWidgets + IngameHudVisibility flags + custom
  keybinds via CfgMods inputs=inputs.xml + GetUApi().GetInputByName().LocalClick()
  (EarPlugs); custom keybind-driven actions on worn gear via DefaultActionInput subclass +
  SetInput("UACustomName") + UpdatePossibleActions with ForceActionTarget (FOG NVG).

## 11. PBO reading, obfuscation, and tooling

- **PBO readers** (`scripts/extract_pbo.py`, standard-library Python 3, is a valid
  fallback): `BankRev.exe` (DayZ Tools Bin\PboUtils) for normal PBOs — official extractor:
  `& "...\PboUtils\BankRev.exe" -f <outDir> <pbo>` (writes into `<outDir>\<pbo name>\`);
  `CfgConvert.exe -txt -dst out.cpp config.bin` for binarized configs; `scripts/ScanPbo.ps1`
  (bundled) for obfuscated PBOs.
- **PBO header is plaintext**: the file-name table sits at the start of any .pbo (even 2GB
  ones) — parse it up to the empty-name terminator entry and ASCII-decode to map contents
  without extraction (verified on 250MB+ PBOs). Its size grows with the entry count:
  `dta\scripts.pbo`'s table is ~229 KB (3076 entries, 1.30), so do not read a fixed 64 KB.
  config.bin entries can be surgically carved by computing offsets from the header
  (entries: name\0 + 5×uint32; data blocks sequential).
- **Obfuscators in the wild**: (a) Mikero junk COM/AUX entries + stripped names (GameLabs,
  DEAD AIR — BankRev refuses; header still readable); (b) "KGB" (unicode decoy paths,
  reserved device filenames, .KGB #include chains — BankRev extracts garbage);
  (c) "PBO Tools v1.8.1" (WT mods: tens of thousands of decoy entries + reserved device
  names + zero-width chars; real config.cpp often a corrupted decoy with the real one a
  separate compressed entry — recover by parsing the header yourself, LZSS-decompressing
  every Cprs entry, filtering by content signature: printable-ratio > 0.83 for text, or
  0x00 'raP' prefix for binarized config).
- **The correct LZSS** (validated byte-identical against BankRev): distance-based —
  control byte low-bit-first; literal on set bit; else read 2 bytes → dist = b1 |
  ((b2 & 0xF0) << 4), len = (b2 & 0x0F) + 3, copy from (outPos - dist), dist==0 means
  4096, positions before start read as 0x20. Ring-buffer/space-window variants decode the
  first bytes then diverge. Implemented in scripts/ScanPbo.ps1 and scripts/extract_pbo.py
  (re-validated 2026-07-31: 1868 compressed entries of TushkanWT.pbo byte-identical to
  BankRev). ScanPbo.ps1 saves entries that match ANY of: marker-substring list (CfgPatches,
  modded class, GetRPCManager, …), config.cpp/config.bin filename, `\0raP` binarized-config
  signature, printable-ratio > 0.83; `-All` saves every entry except those over 8 MB
  (stored size), which are listed in _listing.txt but never extracted — check the listing
  for what was skipped.
- **License-lock anti-piracy** (WT mutants): modded MissionServer ctor calls
  `GetRestApi().GetRestContext(url).GET("API/License/Status...")` and AbortMission() on
  failure — evidence of unlicensed repacks (avoid copying); also the reference for the
  RestApi/RestCallback/JsonFileLoader HTTP pattern. `GetRestApi()` is a GLOBAL proto
  (`3_game/http/restapi.c:183`), not a CGame method: `GetGame().GetRestApi()` does not
  compile. The header files both as "create/access methods out of Hive initialization":
  where no RestApi exists yet `GetRestApi()` is null, and `CreateRestApi()` (:181) makes one.
- **Steam Workshop metadata without scraping**: keyless Steam Web API —
  `POST https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/`
  with `itemcount=N&publishedfileids[0]=ID` returns title, description, subscriptions,
  time_updated. steamcommunity.com pages 429-rate-limit after ~6 rapid hits.

## 12. Licensing map (verified 2026-07)

**Code pasted into a mod must allow redistribution under that mod's own license; the map
below takes a CC BY-NC-SA 4.0 mod as the target.**
Permissive licenses (MIT, Zen's "any use") and CC BY-NC (Terje, with attribution) are fine.
CC BY-SA (COT), verbatim ADPL-SA chunks (BI samples), GPL (ZomBerry) and NoDerivatives
(Expansion, Dabs, KK) give structure and ideas only.

Safe to depend on / reuse:
- **CF** — LGPL-2.1 (GitHub), workshop "no repack". Depend + copy patterns.
- **Zen's mods** (ZenSkills, ZenNotes) — author allows any use, repacking included. Only
  permissive skill-tree source.
- **BuilderItems** — grants free repack (not uniquely: Zen's mods, CraftSystem and the MIT
  entries allow it too).
- **Rejects Quest Mod** — MIT (only freely-adaptable quest framework found).
- **CraftSystem** — allows modify+repack; **NCPR** — repack with credit.
- **BI DayZ-Samples** — ADPL-SA (share-alike, non-commercial, Bohemia games only); for
  a CC BY-NC-SA mod: ideas, no verbatim chunks.
- **StarDZ-Team/Dayz-Modding-Skills** — MIT; **their wiki** — CC BY-SA (attribute).
- **TerjeMods** — CC BY-NC 4.0 (adapt with attribution, non-commercial servers).
- **COT** — CC-BY-SA-4.0 (viral share-alike: reuse structure ideas, careful with code).
- **PBO Manager (pboman3)** — AGPL; **enforce-script-lsp** — GPL-3: tools to use, not code
  to paste.
Study-only (no copying):
- **ZomBerry** — GPL-3.0: study freely, but its code cannot be merged into a
  differently-licensed mod (mods-catalog.md "ZomBerry Admin Tools").
- **Dabs Framework**, **DayZ Expansion**, **Area of Decay** — CC BY-NC-ND 4.0.
- **VPP Admin Tools** — copyright, source on GitHub.
- **KarmaKrew** (KK_Core etc.) — CC BY-NC-ND (© DayZ Black Project); **LBmaster** —
  proprietary; **GameLabs** — proprietary (CFTools EULA); **WT/@Radio pack** — unlicensed
  obfuscated repack (removed from workshop), classnames safe to reference, code untouchable.
- **DEAD AIR** — free use but explicitly forbids repacking into modpacks.
- **TraderX** — open code, no-repack/no-resale; **TraderPlus V1** — source-available;
  **Dr. Jones** — proprietary.
- **Syberia Project** — custom license: derivatives must be public GitHub forks — do NOT
  copy into a mod's code.
- Item packs (Munghards, MuchStuffPack, MMIO, DrugsPLUS...) — no repacking; integrate
  classname-only via JSON + ConfigIsExisting guards.

## 13. Misc engine techniques

- **Soft cross-mod compat**: `#ifdef <define>` compiles optional integration only when the
  other mod is loaded. RUNTIME-VERIFIED 2026-07-31 (1.29, script.log defines list):
  (a) the engine AUTO-DEFINES the **CfgMods class name** of every loaded mod, visible in
  ALL script modules from GameLib up (`JM_CommunityFramework`, `AVPPAdminTools`, and a test
  mod's own class name observed; CfgPatches names like `JM_CF_Scripts` are NOT defined).
  Still so on 1.30.164200: a diag server's script log of 2026-10-31 lists
  `JM_CommunityFramework` and the CfgMods class names of the other loaded mods in the
  GameLib module, plus the version define `DAYZ_1_30`;
  (b) explicit `defines[]` in CfgMods also work (GameLabs, CF's CF_*, and a mod's own, e.g.
  `MYMOD_CORE`) —
  note VPP's own `VPPADMINTOOLS`/`VPPAT_NOTIFICATIONS` appear only from the Game module
  onward (defined in a config the 3_Game+ modules see), so for 4_World integrations both
  `AVPPAdminTools` (auto) and `VPPADMINTOOLS` work; a research mod used `AVPPAdminTools`
  (runtime-proven: "VPP integration: ACTIVE" with VPP loaded, "NOT COMPILED" without).
  Never leave an #ifdef block empty (reported segfault trap).
- **Recipes**: additive `modded class PluginRecipesManagerBase { override
  RegisterRecipies() { super...; RegisterRecipe(new X); } }` (universal); runtime
  UnregisterRecipe(name) MUST be followed by GenerateRecipeCache() (KK_ToolBox); gate
  recipe availability via JSON flags checked in the recipe's CanDo (KK_C4).
- **Action registration**: Dabs `[RegisterAction(AT_X)]` attribute, or vanilla-additive
  `modded ActionConstructor.RegisterActions { super; actions.Insert(AT_X); }`.
- **Player modifiers (buffs/perks)**: extend `enum X: eModifiers` with high unique id +
  `modded ModifiersManager.Init { AddModifier(...); }` + NotifiersManager for HUD icons
  (Tactical Flava id 616). Custom disease agents: `enum eKKAgents: eAgents` — max 32
  agents total, same collision caveat as all vanilla enums.
- **RegisterNetSyncVariableObject** exists for syncing entity references (Van_TankScript).
- **Player animation**: full animGraph override (player_main.pbo) conflicts server-wide —
  only one mod can own it; reuse existing command UIDs or ship animgraph as separate
  optional PBO (SFP).
- **Emissions/blowouts**: @Namalsk Survival's EventManager, NOT vanilla (the 1.29 and 1.30
  scripts have no `EventManager`, `EVRStorm` or `m_EventManagerServer`; the mod must be
  loaded) — m_EventManagerServer.OnInitServer(...) + RegisterEvent(EVRStorm, chance) in
  mission init.c; protection gated by worn classname.
- **Server-only distribution**: a `*_Server.pbo` inside the workshop folder that admins
  copy to server addons keeps server logic private (HGC); split client PBOs
  (models/UI) from -servermod PBOs (rules/spawn logic) — Ibourbon layout.
- **Ship types.xml in mod root with nominal=0** so admins opt in to spawns (STALKER
  Weapons UA, Inedia's casual/hardcore presets).
- **VPP permission model** (for admin tools; exact API verified 2026-07-31 from extracted
  VPPAdminTools.pbo): `GetPermissionManager()` lives in VPP's **4_World** (resolves via
  vanilla g_Plugins; NOT visible from 3_Game; null on clients and outside dedicated —
  registered under `IsDedicatedServer()`). Register per boot BEFORE first check:
  `GetPermissionManager().AddPermissionType({"MyMod:Thing"})` (in-memory only; unregistered
  strings are rejected even for SuperAdmins). Check:
  `bool VerifyPermission(string plainSteam64, string perm, string targetID = "",
  bool sendNotify = true)` — pass `sender.GetPlainId()`, use `"", false` tail for silent
  gating. Admin setup: `$profile:VPPAdminTools/Permissions/SuperAdmins/SuperAdmins.txt`
  (one Steam64 per line) or UserGroups.json (default "Admins" group snapshots the
  permission pool at creation — new mod permissions must be added manually on existing
  servers). VPP UI classes (VPPAdminHud/InsertButton) are version-fragile — integrate via
  the server-side API only.
- **CF module API** (verified in CF 1.5.8 sources): `[CF_RegisterModule(MyModule)]` on a
  `CF_ModuleWorld` (4_World) / `CF_ModuleGame` (3_Game); inside `OnInit()` opt in to
  events — `EnableInvokeConnect();`, `EnableMissionStart();`, `EnableMissionFinish();`,
  `EnableUpdate();` etc. — then override the matching OnX methods. Retrieve anywhere via
  `CF_Modules<MyModule>.Get()`. NOTE: `CF_Window` does NOT exist in CF 1.5.8 — window
  chrome comes from admin tools' own code, not CF.

## 14. Lifecycle, cleanup, performance

- **Statics survive mission restarts**: DayZ can restart a mission WITHOUT restarting the
  process (reconnect, server mission cycle) — static singletons carry stale config and
  handlers into the next session. Contract: null every static `s_Instance` in
  `OnMissionFinish` (modded MissionServer server-side / MissionGameplay client-side,
  before super); every `ScriptInvoker.Insert()` needs a matching `Remove()` (double
  Insert = handler fires twice); guard static invokers at teardown
  (`if (X.SI_Event) X.SI_Event.Remove(cb);` — static teardown order is not guaranteed);
  re-register RPC handlers in mission init rather than assuming registrations survive.
  No destructor runs on process kill — persist via dirty-flag autosave (~300 s) + save
  on critical ops + OnMissionFinish, never in destructors.
- **Every scheduled CallLater needs a Remove in the destructor of the object that scheduled it**:
  `GetGame().GetCallQueue(cat).Remove(this.Fn)` — a pending call on a destroyed object
  is a crash. Categories (verified vanilla usage): CALL_CATEGORY_SYSTEM (survives
  mission lifecycle), CALL_CATEGORY_GAMEPLAY (mission gameplay logic — vanilla
  MissionServer uses it), CALL_CATEGORY_GUI (UI). CallLater delay is MILLISECONDS,
  Timer.Run duration is SECONDS — mixing = 1000× bugs. `Timer.GetRemaining()` is what
  to persist for the resume-after-restart rule (§3).
- **Performance kit** (production-verified patterns): widget pooling for lists (>50
  ScrollWidget children / 500+ widgets in a layout = frame drops — pre-create, Show(false),
  acquire/release instead of destroy/recreate); batched processing (~50 light / 5-10
  heavy ops per frame); ~150 ms debounce on search filters; staggered timer offsets;
  cache CfgVehicles enumeration (ConfigGetChildrenCount/ConfigGetChildName once into a
  static array — never rescan per query); track placed devices via a registry (modded
  EEInit → Register, EEDelete → Unregister) instead of GetObjectsAtPosition3D scans;
  per-tick code reuses member arrays with .Clear() instead of `new` (GC churn = frame
  spikes); never JsonSaveFile in OnUpdate. Budget ~2 ms/frame per mod; the engine logs
  `SCRIPT (W): Exceeded X ms` when you blow it.
- **Device VFX**: particles are client-only — drive from OnVariablesSynchronized on
  netsync device state; `ParticleManager.GetInstance()` returns null on dedicated
  server (guard). Ready-made lab-flavored ids in ParticleList: POWER_GENERATOR_SMOKE,
  ITEM_HOT_VAPOR, COOKING_BOILING_START/DONE, SMOKING_BARREL. Looping particles never
  auto-end — Stop() on power-off/EEDelete. Full API + .ptc/.emat formats: the separate
  **dayz-particles** skill (from Agentic-Z, installed on its own; if the session did not
  load it, read its `SKILL.md` and `references\` by path).
- **Keybind/UI input**: cache `GetUApi().GetInputByName("UAX")` once (never per frame);
  poll LocalPress/LocalHold/LocalRelease/LocalValue in MissionGameplay.OnUpdate with
  `if (!GetGame().GetPlayer() || GetGame().GetUIManager().GetMenu()) return;` guards;
  while a menu is open use AddActiveInputExcludes({"menu"})/RemoveActiveInputExcludes;
  one-frame suppression is `input.Supress()` (one 'p', verified
  `3_game/inputapi/uainput.c:84` on 1.30.164200; :74 on 1.29). Full
  widget/layout mechanics: references/gui-layouts.md.

## Spawn position: hook `OnClientNewEvent`, NEVER `CreateCharacter`

`MissionServer.CreateCharacter` looks like the seam for "decide where a player
appears" — it is the function that places the character, and `modded class
MissionServer` compiles cleanly over it. It also never runs on a large share of
servers, and it fails silently.

The mission file `mpmissions/<mission>/init.c` declares `class CustomMission:
MissionServer` and overrides `CreateCharacter` **without calling super** —
vanilla `dayzOffline.chernarusplus` does exactly this (init.c:44 in the 1.30.164200 DayZ
Server):

```c
override PlayerBase CreateCharacter(PlayerIdentity identity, vector pos, ParamsReadContext ctx, string characterName)
{
    Entity playerEnt = GetGame().CreatePlayer(identity, characterName, pos, 0, "NONE");
    Class.CastTo(m_player, playerEnt);
    GetGame().SelectPlayer(identity, m_player);
    return m_player;                       // no super. your modded class is gone
}
```

The mission compiles **after every mod**, so this is not an ordering race you can
win — the override is simply unreachable. Measured in a test mod: a `modded
class MissionServer` override of `CreateCharacter` produced not one log line
across three deaths on a stock Chernarus mission.

Hook one step upstream instead. `OnClientNewEvent` passes `pos` straight into
both `CreateCharacter` call sites (`5_mission/mission/missionserver.c:573` and `:589`,
1.30.164200) unchanged, so
substituting it there reaches the mission's own implementation as an argument —
which it uses whether or not it calls super:

```c
modded class MissionServer
{
    override PlayerBase OnClientNewEvent(PlayerIdentity identity, vector pos, ParamsReadContext ctx)
    {
        return super.OnClientNewEvent(identity, MySpawns.Resolve(identity, pos), ctx);
    }
}
```

`OnClientNewEvent` fires only for a NEW character (missionserver.c:329).
Reconnects go through `OnClientReconnectEvent` with an already-built PlayerBase
and never reach it — which is what you want: a returning player stays where they
logged out, and cannot be teleported by accident.

**General rule:** anything `init.c` overrides is not moddable. Before hooking a
`MissionServer` method, grep the mission's `init.c` for it.

### Starting equipment: strip and dress AFTER super.OnClientNewEvent (measured 2026-09-02)

`StartingEquipSetup` is overridden by the mission too (init.c:55 stock 1.30), so "give
items on top" is unreachable the same way. What works, measured on a diag
server (1.29, a test mod):

- `PlayerBase player = super.OnClientNewEvent(...)` returns with the character
  ALREADY dressed by the mission (both paths: `PlayerSpawnHandler.ProcessEquipmentData`
  at missionserver.c:575, `EquipCharacter` at :593). Strip
  and dress in the SAME frame, no CallLater needed.
- Strip with `player.GetHumanInventory().LocalDestroyEntity(item)` for every
  item whose `GetHierarchyParent() == player` (plus `GetEntityInHands()`
  first). It queues `ObjectDelete`, but the slot is free immediately:
  `CreateAttachmentEx(type, InventorySlots.GetSlotIdFromString("Body"))` in the
  same frame succeeds.
- Do NOT call vanilla `PlayerBase.RemoveAllItems()`: it walks PREORDER into
  nested items and prints `LocalDestroyEntity: No inventory location` with a
  stack trace once the parent is already queued -- top-level only avoids it.
- Dress: `CreateAttachmentEx` by slot id, `GetHumanInventory().CreateInHands(type)`
  for the hands, `GetInventory().CreateInInventory(type)` for "wherever it
  fits"; nested content via the created entity's own `GetInventory()`;
  `Magazine.ServerSetAmmoCount` / `ItemBase.SetQuantity`, `SetHealth01("", "", h)`,
  `SetQuickBarEntityShortcut(e, idx)` (idx is 0-based: 1 shows as slot "2").
- **`SetHealth01(..., 0)` is a RUINED item, and `0.001` is not** (read 2026-09-07).
  `GetHealthLevelValue(level)` returns "the cutoff value ... the max health as
  allowed by the given health level" (3_game/entities/object.c:1086 and 1181-1185,
  1.30.164200); the cutoff of `GameConstants.STATE_RUINED` (3_game/constants.c:909,
  level 4) is
  `0.0`, so only health 0 is ruined. Vanilla's `+0.001` is the epsilon used to
  land a value just ABOVE a cutoff (4_world/.../magazine.c:411), i.e. `0.001`
  gives BADLY_DAMAGED. A config that wants a ruined item must be able to say a
  plain `0` -- do not "reserve" zero as a missing-key marker.
- **The quick bar has ten slots, `0..9`.** `MAX_QUICKBAR_SLOTS_COUNT = 10`
  (4_world/classes/quickbarbase.c:1); vanilla passes 0 first
  (4_world/classes/debug/presethandlers.c:46) and the inventory panel hands
  `SetQuickBarEntityShortcut` the raw column
  (5_mission/gui/inventory/inventoryquickbar.c:191). `GetQuickBarSize()` is the
  live size, clamped to that maximum.
- A field named `Slot` does not compile ("Variable name 'Slot' already used as
  type name") -- vanilla declares `class Slot` (4_world/entities/gardenbase/slot.c:22),
  and a field may not share a type's name. Name it `SlotName`.
