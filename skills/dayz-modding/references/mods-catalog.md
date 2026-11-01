# DayZ Mods Catalog (205 entries, researched 2026-07-31)

Catalog of DayZ mods and ecosystem resources, compiled on 2026-07-31 from a 54-mod
STALKER RP server modpack plus public sources.
Sources: direct PBO extraction/decompilation of that modpack in
DayZ\!Workshop (BankRev/CfgConvert + custom LZSS scanners for obfuscated PBOs),
Steam Workshop pages + keyless Steam Web API (GetPublishedFileDetails), GitHub repos.
`source: local-modpack` marks an entry read from files extracted from that modpack.
Every entry was verified against files or pages actually read by the research agents.

Read it as a dated snapshot. Versions, subscriber counts, licenses and the "in the local
modpack" notes are as of 2026-07-31. Several mods have two or three entries from
different research passes (CF, Dr. Jones Trader, BBP, CannabisPlus, the WT mods inside
@Radio, …) — grep every hit, and where two entries
disagree, the one sourced from `local-modpack` (extracted files) beats a web page.

Entry format: name; category/source/popularity/license; url; description;
T: = implementation techniques relevant to a processing/research framework;
CLASSNAMES: = item classnames usable as processing inputs/outputs or device bases.

Distilled cross-cutting lessons live in references/patterns-cookbook.md.

## Sections
- Local modpack — frameworks and admin tools
- Local modpack — DayZ Expansion and traders
- Local modpack — WeTernal STALKER framework (@Radio folder)
- Local modpack — WeTernal STALKER mutants (@Radio folder)
- Local modpack — STALKER content packs
- Local modpack — AI and creatures
- Local modpack — KarmaKrew server pack
- Local modpack — base building, storage, locks
- Local modpack — gameplay and misc
- Local modpack — weapons and clothing packs
- Steam Workshop — frameworks, admin, economy
- Steam Workshop — progression, RPG, skill trees
- Steam Workshop — STALKER-themed mods
- Steam Workshop — science, medical, processing stations
- Steam Workshop — quests, unlocks, economy
- Steam Workshop — top mods by subscribers (breadth sweep)
- Ecosystem — AI skills, dev tools, web config editors
- Ecosystem — Claude Code plugins and MCP servers

# Local modpack — frameworks and admin tools

## Community Framework (CF)
- category: framework | source: local-modpack | popularity: 4,859,583 subscribers, 5 stars (10,116 ratings) | license: Workshop: repacking prohibited; source on GitHub (Jacob-Mango/DayZ-CommunityFramework, LGPL-2.1 per project notes)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1559212036
- desc: Foundational scripting framework: shared RPC dispatch that resolves RPC-ID conflicts between mods, per-entity mod persistence (ModStorage), module/lifecycle system, networked variables, notifications, file/XML/JSON utilities. v1.5.8 in modpack; scripts.pbo fully readable (170 .c files).
  - T: RPC: GetRPCManager().AddRPC(modName, funcName, instance, SingleplayerExecutionType.Server|Client|Both); SendRPC(modName, funcName, Param, guaranteed, identity, target); all mods multiplex over one engine RPC id FRAMEWORK_RPC_ID=10042; handler signature is (CallType type, ParamsReadContext ctx, PlayerIdentity sender, Object target) and must ctx.Read(ParamN<...>)
  - T: Persistence: on ItemBase/PlayerBase override CF_OnStoreSave(CF_ModStorageMap storage) / CF_OnStoreLoad(storage); get stream via storage["ModName"]; typed ctx.Write/Read for bool/int/float/vector/string; version gate with ctx.GetVersion()
  - T: storageVersion is an int in the mod's CfgMods class, read via ConfigGetInt(modPath + " storageVersion"); HasModStorage() == storageVersion > 0 — persistence is OFF unless you set it
  - T: CF_ModStorage wire format v5 writes typed entries + mod name hashes; CF_ModStorageModule keeps $profile:modstorageplayers to protect pre-CF saves; file documents exact connect/disconnect/respawn event order
  - T: Module system: [CF_RegisterModule(MyModule)] class MyModule: CF_ModuleWorld with opt-in events (OnInit + EnableMissionFinish/OnMissionFinish etc.) — clean place for a server-side PointsDB singleton
  - T: CF_NetworkedVariables: register variables (even nested paths "a.b.c") by string for net sync, max 256 per instance
  - T: NotificationSystem.CreateNotification(StringLocaliser title, StringLocaliser text, icon, color, time, sendToIdentity) — server-to-client toast over RPC
  - T: Also ships: JSON/XML readers, binary/base64 streams, SHA-256, expression VM, input bindings, in-editor build/launch plugins

## Dabs Framework
- category: framework | source: local-modpack | popularity: 3,388,197 subscribers, 4 stars (3,798 ratings) | license: Workshop: repacking prohibited; open-source on GitHub (InclementDab/DayZ-Dabs-Framework)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2545327648
- desc: Lightweight modding framework by dab (InclementD), best known for its MVC UI system (ScriptView/ViewController/ViewBinding) plus attribute-based registration helpers, event manager, Discord webhooks and profile settings. Scripts fully readable (227 .c); GUI.pbo carries prefab layouts, styles and 5 FontAwesome-style icon imagesets.
  - T: ScriptView: subclass, override GetLayoutFile() returning your .layout; widget member variables are auto-assigned by matching widget names (LoadWidgetsAsVariables via EnScript.SetClassVar) — no FindAnyWidget boilerplate
  - T: ViewController: public fields are bindable properties; override PropertyChanged(name); call NotifyPropertyChanged(name, notify_self=false) after mutating; ObservableCollection<T> drives listboxes/comboboxes; sub-object paths like m_Obj.value bind too
  - T: Layout binding syntax: in .layout give the root scriptclass "MyController", and any widget scriptclass "ViewBinding" + ScriptParamsClass { Binding_Name "prop" Two_Way_Binding 1 Relay_Command "..." }; RelayCommand = controller methods bool MyButtonExecute(ButtonCommandArgs)
  - T: ScriptViewMenu: full menu via internal UIScriptedMenu (ShowScriptedMenu), handles input excludes and ChangeGameFocus for mouse/keyboard; ScriptViewTemplate<TController> gives typed GetTemplateController(); DIAG hot-reload of layouts (ScriptView.ReloadAll)
  - T: [RegisterAction(AT_MyAction)] attribute auto-inserts user actions into modded ActionConstructor.RegisterActions — no per-action mission edits
  - T: [RegisterProfileSetting]/[RegisterMissionSetting] attribute-registered settings with auto-generated options UI (slider/color/multistate/editbox selectors)
  - T: Utility layer: EventManager (scheduled server events), DiscordWebhook + WebApiBase REST wrapper, WidgetAnimator, SuppressedObjectManager (map-object hiding used by Editor Loader)
  - T: UI assets for reference: prefab layouts (button/checkbox/dropdown/listbox/slider/colorpicker/editbox), .styles look-n-feel, FontAwesome-style imagesets (solid/regular/light/thin/brands) usable as tech-tree node icons

## VPPAdminTools
- category: admin-tools | source: local-modpack | popularity: 3,779,209 subscribers, 4.6 stars (4,617 ratings) | license: Copyright, no repacks (Steam posting only by DaOne); source on GitHub (VanillaPlusPlus/VPP-Admin-Tools)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1828439124
- desc: Server admin toolkit (item/player/teleport/weather/object managers, bans, ESP, XML editor, webhooks) built on CF. 193 readable .c files plus 69 native .layout menus. Includes VPPStaticItems addon with vbldr_* static prop replicas (based on HouseNoDestruct) for object placement sets.
  - T: Permission model: flat namespaced strings "MenuItemManager:SpawnItem" registered via AddPermissionType(); UserGroup {name, permissionLevel, members, permissions[]} persisted with JsonFileLoader to $profile:VPPAdminTools/Permissions/UserGroups.json + SuperAdmins.txt
  - T: Server-side gate VerifyPermission(steamid, permName, targetId, sendNotify) checked inside every RPC handler; never trusts client
  - T: Client UI sync: client asks VerifyButtonsPermission with a list of perm strings, server replies map<string,bool> via RPC to enable/disable menu buttons — good pattern for hiding research-admin UI from players
  - T: Parallel RPC channel: mods CF's RPCManager adding VSendRPC/VOnRPC on its own VPPAT_FRAMEWORK_RPC_ID=10420 and modded DayZGame.OnRPC routing — pattern for claiming your own engine RPC id instead of sharing CF's 10042
  - T: ConfigurablePlugin base class (PluginBase + Load/Save + JSONPATH) for every JSON-configured subsystem; $profile file enumeration via FindFile/FindNextFile
  - T: Menu architecture: one folder per submenu, VPPUIManager plugin tracks VPPScriptedMenu instances, creates via vanilla GetUIManager().EnterScriptedMenu(menuID, parent) + typename lookup, menu IDs added to MissionBase switch
  - T: ChatCommandManager with pluggable command modules (argument / no-argument / player-targeted)
  - T: Audit trail: WebHooksManager.PostData(AdminActivityMessage, ...) posts admin actions to Discord

## GameLabs
- category: server-integration | source: local-modpack | popularity: 1,227,782 subscribers, 4 stars (1,007 ratings) | license: Proprietary, CFTools EULA; commercial use restricted
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2464526692
- desc: CFTools Cloud GameAPI connector by CFTools: streams player events, telemetry and server metrics to the cloud dashboard and executes remote Dynamic Actions (heal, teleport, spawn, locked-container open) on the server. PBO is Mikero-obfuscated; analysis done via filename-table/strings plus Workshop page.
  - T: Structure (from PBO name table): 3_Game/API + 3_Hooking, 4_World/4_Hooking/{BaseBuilding, Core, DynamicEvents, EntityTracking, Player}, 5_Mission/DynamicActions/{debug, player} — hook-based telemetry architecture
  - T: Classes seen in strings: GameLabsActions (map<string,int> action registry), GameLabsConfiguration, GameLabsLogger, GameLabsMetricsDump (serverFps), GLBaseBuildingMetaStorage, GLLoadoutItem, CFCloud_LockedContainerOpen action
  - T: Workshop-documented integration points: DayZ Expansion side/team chat transmission, in-game player reports to Discord, custom RCon message styling, server/client performance metrics access
  - T: Requires CFTools Cloud registration (Server ID + API key); works as a library other mods can query
  - T: Obfuscation pattern worth knowing: Mikero junk entries (COM1/AUX/CON device names, fake config.bin/cpp pairs) defeat BankRev; readable fragments recoverable by ASCII-scanning raw PBO bytes

## DayZ Editor Loader
- category: server-tool | source: local-modpack | popularity: 2,665,725 subscribers, 4 stars (2,046 ratings) | license: No repacking permitted (author dab/InclementD)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2276010135
- desc: Tiny server-side loader (one modded MissionServer file) that spawns/deletes map objects from DayZ Editor .dze files at mission start. Reads both binary (FileSerializer) and JSON (JsonSerializer.ReadFromString) .dze; deletes map objects via Dabs SuppressedObjectManager. Requires Dabs Framework.
  - T: OnMissionStart: Directory.EnumerateFiles($mission:\EditorFiles, "*.dze", depth 5) then spawn each EditorObjectData (supports raw .p3d names and EditorOnly flag)
  - T: Dual-format save data: EditorSaveData.IsBinnedFile() -> FileSerializer.Read, else JsonSerializer.ReadFromString — a clean pattern for accepting both binary and JSON configs
  - T: Map-object deletion without CE edits via GetDayZGame().GetSuppressedObjectManager().Suppress(obj)
  - T: LoadCustomBuilds(inout array<string>) virtual hook so other mods can append packed .dze content
  - T: Optional mapgrouppos.xml export ($storage:export) to feed CE loot spawner from edited buildings

## LootManagerPlus
- category: client-qol-ui | source: local-modpack | popularity: 7,365 subscribers, 4 stars (92 ratings) | license: No repacking/modification without author (hummigummi) permission
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3678395974
- desc: Client-side inventory QoL mod: adds category sort menu and per-container Transfer/Stack/Drop/Lock buttons to the vanilla inventory, with JSON config at $profile/LootManagerPlus/config.json (category priorities, blacklists, feature toggles). PBO is Mikero-obfuscated; findings from strings + Workshop page.
  - T: Server->client feature config over dedicated RPC: const LMP_RPC_FEATURE_CONFIG = 0x4C4D.. with a client-side FeatureConfigCache
  - T: UI injection: LootManagerPlusGUI extends ScriptedWidgetEventHandler; modded ClosableContainer and MissionGameplay add LMP_CategoryBtn/LMP_TransferBtn/LMP_StackBtn/LMP_DropBtn/LMP_LockBtn button widgets into container layouts
  - T: Compile-time feature switches as consts (LMP_COMPAT_HOOK_ENABLED, LMP_GUI_RUNTIME_ENABLED, LMP_CATEGORY_MENU_ONLY) for compatibility toggling
  - T: JSON admin config in $profile with category priority tiers and classname blacklists (per Workshop docs)

## MPG_TDM_Logs
- category: server-logging | source: local-modpack | popularity: 2,066 subscribers, 5 stars (172 ratings) | license: Custom: free to use/modify server-side; no repack/reupload; Steam publishing only by pafnuty10
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3248961196
- desc: Pure server-side (-servermod) item-tracking logger by a Russian author: logs every item MOVE/TAKE/DROP with persistent item id and coordinates as TSV, uploads to map.mpg-dayz.ru for map-based analysis, and fires Discord webhook alerts on suspiciously long-distance transfers. Fully readable source (6 small .c files).
  - T: JSON config lifecycle: JsonFileLoader<T>.JsonLoadFile if exists else create defaults; configVersion field + UpdateConfig() migration on load; ships a commented .json5 example in the PBO help folder
  - T: Classname matching for admin lists: GetGame().IsKindOf(type, baseClass) for inheritance matching, with a '|1' suffix convention to force exact-class match — directly reusable for the input rules of a research framework
  - T: Item tracking hook: override ItemBase.EEItemLocationChanged(oldLoc, newLoc) and classify MOVE/TAKE/DROP from InventoryLocation parents
  - T: Discord webhook alert with templated message (%1..%7 placeholders incl. izurvive map links)
  - T: Server-side-only distribution via -servermod= so clients need nothing


# Local modpack — DayZ Expansion and traders

## DayZ-Expansion-Bundle
- category: framework / content bundle (market, quests, hardline, book UI, AI, base building) | source: local-modpack | popularity:  | license: CC BY-NC-ND 4.0 (header in every script; code cannot be copied, only patterns reimplemented; modded-class extension of their classes at runtime is standard practice)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2572331007
- desc: v1.9.71 by Expansion Team; ~250 PBOs. Script modules extracted and read: core_scripts (settings framework, RPC manager), scripts, quests_scripts (admin-JSON quest system), hardline_scripts (reputation), market_scripts (traders/currency/ATM), book_scripts (tabbed player menu), personalstorage_scripts, modstorage_scripts (config-only stub enabling CF ModStorage bridging).
  - T: Settings sync flow: ExpansionSettingBase subclass per settings type; server ServerInit()->Load() JSON from $profile:ExpansionMod\Settings\ (defaults auto-generated+saved if missing); modded MissionServer calls g_exGlobalSettings.Send(identity) on client connect (plus CallLater 20s resend path); each setting serializes itself via OnSend/OnRecieve over a registered RPC; client stores received copy, Get() warns if accessed before received
  - T: ExpansionRPCManager (core_scripts/3_game/.../expansionrpcmanager.c): string-named RPCs registered per owner class via RegisterClient/RegisterServer returning auto-assigned int IDs -> no global RPC-id collisions; modules call Expansion_EnableRPCManager()+Expansion_RegisterClientRPC("RPC_Fn") in OnInit, handler methods found by name
  - T: CF module lifecycle (documented in expansionhardlinemodule.c): connect order OnClientPrepare -> OnClientNew/OnClientReady -> OnInvokeConnect; load per-player data in OnClientPrepare (available before StartingEquipSetup), attach to PlayerBase in OnInvokeConnect, drop cache in OnClientDisconnect
  - T: Reputation (= research-points analog): int per player; persisted server-side as $profile:...\PlayerData\<SteamID>.bin via FileSerializer with CONFIGVERSION int + versioned OnRead migration; live value synced by RegisterNetSyncVariableInt("m_Expansion_Reputation") on modded PlayerBase + SetSynchDirty(); per-faction rep is map<factionID,int> sent by custom RPC only when dirty; spent/gained only via server-side Expansion_AddReputation
  - T: Quest admin-JSON schema: one JSON per quest in $profile:ExpansionMod\Quests\Quests\ + one JSON per objective in Objectives\<Type>\ (Travel, Delivery, Collection, Target, TreasureHunt, AIPatrol, AICamp, Action, Crafting); quest fields: ID, Type, Title, Descriptions[3], Objectives[], QuestItems[], Rewards[] {ClassName, Amount, Attachments[], DamagePercent, Chance}, PreQuestIDs, FollowUpQuest, ReputationReward/Requirement, RequiredFaction, FactionReward, FactionReputationRequirements/Rewards (map faction->int), Repeatable/Daily/Weekly, IsGroupQuest; loaded with ExpansionJsonFileParser<T>.Load + ConfigVersion chain-migration + Validate(), full config array serialized to client on connect via one RPC
  - T: Objective 'Action' type completes when player performs named user actions (ActionNames[]) and 'Crafting' type on ItemNames[] crafted — same trigger concept as processing actions feeding a tech tree
  - T: Book UI: ExpansionBookMenu with tab framework — tabs are ExpansionScriptView subclasses with GetLayoutFile()/GetControllerType()/GetTabName()/GetTabIconName()/CanShow(); MVC binding via ExpansionViewController + ObservableCollection + NotifyPropertiesChanged; third-party tab = modded ExpansionBookMenuManager override RegisterBookMenuTabs (hardline's Factions tab is a complete in-tree example)
  - T: Market currency = ordinary item classnames: MarketSettings.json Currencies[] (lowercase classnames, default expansionbanknotehryvnia); per-trader Currencies[] override; value from item's market price, denominations auto-sorted; SpawnMoney/FindMoneyAndCountTypes walk player inventory stacks; ATM adds bank balance int per player (MaxDepositMoney)
  - CLASSNAMES: ExpansionCircuitBoard_MK1, ExpansionCircuitBoard_MK2, ExpansionCircuitBoard_MK3, ExpansionBriefcase, ExpansionGoldNugget, ExpansionGoldBar, ExpansionSilverNugget, ExpansionSilverBar, ExpansionBanknoteHryvnia, ExpansionBanknoteUSD, ExpansionBanknoteEuro, ExpansionQuestBoardSmall, ExpansionQuestBoardLarge, ExpansionPersonalStorageChest, Dogtag_Survivor

## DayZ-Expansion-Licensed
- category: content pack (vehicles) | source: local-modpack | popularity:  | license: Bohemia-licensed assets (separate PBO because these ports need special permission); scripts CC BY-NC-ND 4.0
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2116157322
- desc: v1.9.70. Companion asset pack to the Expansion Bundle: helicopters (UH1H, MH6, Merlin, An2, C130), boats/carrier, ground vehicles (Vodnik, Bus, Tractor), explosives, airdrop objects. No framework scripts of interest — all logic lives in the Bundle.

## Stalker Anomaly - Expansion Factions
- category: faction definitions (server-critical micro-mod) | source: local-modpack | popularity:  | license: unknown (author ZombieX, credits 'based on Smexy mod')
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3012880771
- desc: 12.7 KB PBO, fully extracted. Defines the modpack's 10 STALKER factions as eAIFaction subclasses in scripts/3_Game/dayzexpansion_ai/factions/: ClearSky, Duty, Ecologist (= 'Учёные' per RU stringtable), Freedom, Loner, Mercenary, Military, Monolith, Renegade, Bandit. Each class: [eAIRegisterFaction(ClassName)] attribute, constructor sets m_Loadout = "<Name>Loadout", override IsFriendly(eAIFaction other) enumerating friendly faction types via IsInherited. Requires DayZExpansion_AI_Scripts; whole file wrapped in #ifdef EXPANSIONMODAI.
  - T: Registering a custom faction: class X: eAIFaction + [eAIRegisterFaction(X)] attribute + m_Loadout name; friendliness matrix via IsFriendly override with IsInherited checks
  - T: All faction scripts guarded with #ifdef EXPANSIONMODAI so the addon soft-depends on Expansion AI
  - T: Display names via stringtable keys STR_EXPANSION_AI_FACTION_<NAME> (RU: ЧИСТОЕ НЕБО, УЧЕНЫЕ/Ecologist, ДОЛГ...)
  - T: Player faction = faction of the player's eAIGroup; hardline module recreates group from persisted FactionID on connect: SetGroup(eAIGroup.CreateGroup(eAIFaction.Cast(factionType.Spawn())))

## Trader (Dr_J0nes Trader Mod)
- category: trader / economy | source: local-modpack | popularity:  | license: unknown (closed source conditions typical; scripts unobfuscated)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1590841260
- desc: v1.9, classic AI-trader mod: trader NPCs, safezones, vehicle keys, ruble currency items, wallet. Config is NOT JSON — plain text files in $profile:Trader\ (TraderConfig.txt, TraderObjects.txt, TraderVehicleParts.txt, TraderVariables.txt, TraderAdmins.txt) hand-parsed line-by-line with a custom FileReadHelper (FGets + comment trimming) in modded MissionServer.
  - T: Config format: <CurrencyName> Ruble; <Currency> MoneyRuble100,100 (classname,value denominations); <Trader> Name; <Category> Name; item lines 'classname,quantity,buyValue,sellValue'; comments '//'
  - T: Client sync: on connect server pushes currency, trader names, categories and every item entry as separate Param RPCs; client blocks trading until complete — illustrates why batched serialization (Expansion-style) is preferable
  - T: ActionTrade: ActionInteractBase on arbitrary object/NPC classname list from config — pattern for opening a device UI from a world object
  - T: TraderObjects.txt spawns arbitrary world objects/NPCs at configured pos+ypr at server start (admin-placed stations without map edits)
  - T: Safezone implementation via scripted trigger areas (safezone.c) + modded ContaminatedArea_Local
  - T: Money as splittable stackable Inventory_Base items (varQuantityMax=500, canBeSplit) valued by config denominations
  - CLASSNAMES: MoneyRuble1, MoneyRuble5, MoneyRuble10, MoneyRuble25, MoneyRuble50, MoneyRuble100, TM_Wallet, VehicleKeyBase, VehicleKeyDuplicate, VehicleKeyLost, Land_RoadCone

## Trader Fix Pack
- category: patch / compatibility fixes for Trader | source: local-modpack | popularity:  | license: unknown
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3702981694
- desc: Two PBOs. TraderInventoryRotationFix (3 KB, readable, Russian comments): modded class TM_InventoryTransactions replacing CreateItemInPlayerInventory — spawns a temporary local 'probe' entity, uses FindFreeLocationFor(probe, CARGO) across attachment containers to find a rotation-aware free slot, then LocationCreateEntity there, deleting the probe. Gelik_TraderJonesFix (71 KB): fully obfuscated with 'KGB' obfuscator — extraction yields decoy files with reserved device names (com1/lpt1) and unicode paths; content unreadable, purpose unverifiable.
  - T: Reliable inventory item placement: create ECE_LOCAL probe -> iterate attachment containers -> GetInventory().FindFreeLocationFor(probe, FindInventoryLocationType.CARGO, loc) -> LocationCreateEntity(loc, classname, ECE_IN_INVENTORY, RF_DEFAULT) -> ObjectDelete(probe); fallback to ground spawn (createOnGroundOnFailure)
  - T: PBO obfuscation in the wild (KGB #include chains, reserved-device-name decoy files) — BankRev extracts junk; don't rely on being able to audit third-party PBOs


# Local modpack — WeTernal STALKER framework (@Radio folder)

## @Radio (WeTernal/"WT" STALKER framework pack by Satoshi)
- category: stalker-framework-collection (meta-package) | source: steam-workshop | popularity:  | license: proprietary - obfuscated with PBO Tools v1.8.1 (pbo.tools); author "Satoshi"; no license, do NOT copy code
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3597910943
- desc: Folder is named @Radio but meta.cpp publishedid=3597910943 ("Radio"). A single Workshop item bundling a whole STALKER RP framework: FramerworkWT (admin/trader/fraction/anomaly config core), PDAWT (handheld PDA device+UI), AnomalySystemWT (anomalies+detectors), WTHealth (radiation/psy), PersonalChestWT (persistent storage), DLCSlots (extra slots), Satoshi_Mamoebov (mutant skeletons) plus ~15 mutant PBOs. Every PBO is heavily obfuscated (tens of thousands of decoy files with garbage names and reserved device-name paths like COM3/LPT6 to break extractors); real content recovered by BI-LZSS decompression + content-signature filtering.
  - T: PBO Tools obfuscation packer
  - T: CfgMods gameScriptModule 3_Game/4_World/5_Mission split
  - T: imageSets registered in CfgMods defs
  - T: RestApi HTTP server backend
  - CLASSNAMES: SF_DETECTOR_VELES, SF_DETECTOR_EYE, WT_AnomalySystem_Bolt, PDA_WT, PersonalStorage, Anomaly_Electra_Active, LZA_FOG, Anomaly_Catcher

## FramerworkWT (WT admin / economy / anomaly config core)
- category: admin-framework / json-config + rpc-sync | source: steam-workshop | popularity:  | license: proprietary/obfuscated (Satoshi); no license
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3597910943
- desc: The framework backbone. Ships NO content, only script modules + layouts. Provides a single admin UIScriptedMenu (WTFrameworkPanel) that branches into sub-panels (Animal spawns, Anomalies, Fields, Bundles, Trader, Passport, MutantControl, Players), each gated by per-player admin bool flags (m_IsRootWT, m_AdminTrader, etc.). One big RPC enum WTFrameworkRPC (base 125237) drives Request*/Response* pairs; client OnRPC dispatches to the matching panel's Handle*(ctx). Config data modeled as plain Json classes read/written straight over RPC with ctx.Read/ctx.Write.
  - T: single admin UIScriptedMenu with sub-panels
  - T: RPC enum with Request_/Response_ pairs (base offset int)
  - T: GetDayZGame().Event_OnRPC.Insert(OnRPC) client dispatch
  - T: per-player admin bool gating on PlayerBase
  - T: Json config classes serialized directly via ScriptRPC + ctx.Read/Write
  - T: JsonFileLoader<T>.JsonLoadData
  - T: modded MissionServer.InvokeOnConnect for per-connect sync
  - T: GetRestApi().GetRestContext(url).GET/GET_now backend
  - CLASSNAMES: WTFrameworkPanel, WTFrameworkAnomaliesPanel, WTTraderJson, WTTraderItemJson, ADMFieldJson

## AnomalySystemWT (anomalies + artifact detectors + bolt)
- category: anomaly-system / detector-devices / artifacts | source: steam-workshop | popularity:  | license: proprietary/obfuscated (Satoshi); no license
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3597910943
- desc: Full STALKER anomaly + detector system (362MB, mostly assets; scripts are small). Anomalies are HouseNoDestruct-derived objects (Anomaly_<Type>_Active/_Idle) that spawn a ScriptedLightBase + Particle + looped sound in their constructor client-side; radiation is separate LZA_FOG* zone objects. Six handheld detectors extend a shared SF_Detector_Base : Inventory_Base (ItemBase) with EnergyManager (battery attachment BatteryD), OpenableBehaviour, and a StalkerDetectors RPC enum; the detector broadcasts its position+orientation from OnWork via GetGame().RPC and beeps (art_found/veles_art_detect) based on nearby artifacts. veles_marker_anomaly/veles_marker_artefact are on-screen marker props. Note: artifact ITEM classnames are not defined here - detectors only detect them; artifacts live in a separate STALKER content mod. An Eye chip (WT_Detector_Eye_Chip in slot DetectorEyeChip) unlocks artifact-search mode.
  - T: SF_Detector_Base : Inventory_Base with EnergyManager battery (BatteryD)
  - T: OnWork energy-tick broadcasts pos/dir via GetGame().RPC
  - T: device RPC enum + override OnRPC on ItemBase for beep sounds
  - T: OnVariablesSynchronized + RegisterNetSyncVariable for device state
  - T: OpenableBehaviour for lid animation
  - T: anomaly = HouseNoDestruct spawning ScriptedLightBase.CreateLight + Particle.PlayOnObject + PlaySound (IsClient-guarded)
  - T: Action classes: ActionDetectorSwitchMode/ActionThrowBolt (ActionSingleUseBase)
  - T: AddItemInHandsProfileIK in ModItemRegisterCallbacks for hold anims
  - CLASSNAMES: SF_Detector_Base, SF_DETECTOR_VELES, SF_DETECTOR_SVAROG, SF_DETECTOR_GILKA, SF_DETECTOR_EYE, SF_DETECTOR_OTKLIK, SF_DETECTOR_BEAR, WT_AnomalySystem_Bolt, WT_Detector_Eye_Chip, Anomaly_Electra_Active, Anomaly_Kisel_Active, Anomaly_Myasorubka_Active, LZA_FOG, Anomaly_Catcher

## PDAWT (handheld PDA device with full UI, chat, map, quests, storage)
- category: pda-device / portable-device-ui / persistence | source: steam-workshop | popularity:  | license: proprietary/obfuscated (Satoshi); no license
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3597910943
- desc: The single most valuable prior-art for portable science devices (237MB, big assets, small scripts). PDA_WT is a Clothing item worn in a custom DLCSlot1 slot with EnergyManager + a PDAFlashDrive attachment. In-hand action ActionTurnOnOffPDAInHands (ActionSingleUseBase, OnExecuteClient) opens the PDAClient_visual_Class UIScriptedMenu; a separate ActionPDAStorage (ActionInteractBase, CCTObject) opens a networked storage menu on a PDAStorage object. A global singleton PDAClient (Get_PDAClient) holds the menu refs and dispatches a large PDAMenuRPC enum (MENU_ID=57907529) to Handle*(ctx) methods. Item state (account/fraction/friend visibility, avatar) lives in RegisterNetSyncVariableInt fields on the item; server data (contacts, chat, quests, storage, fractions, capture map) is modeled as dozens of Json classes streamed over RPC. Rich stringtable + imagesets (pda_icons, PDAMenuSet, PDAAvatars).
  - T: worn Clothing device in custom slot + EnergyManager
  - T: ActionSingleUseBase.OnExecuteClient opens UIScriptedMenu
  - T: ActionInteractBase + CCTObject to open menu on a world object
  - T: singleton client (Get_PDAClient) holding menu refs + Event_OnRPC dispatch
  - T: large PDAMenuRPC enum (MENU_ID sentinel) Request/Response
  - T: RegisterNetSyncVariableInt device state + RPCSingleParam sync request
  - T: virtual server-side storage (PDAStorageDBJson) streamed via ctx.Read
  - T: layouts + imageset icon sets for device UI
  - CLASSNAMES: PDA_WT, PDA_FlashDrive, PDAStorage, Slot_DLCSlot1, PDAFlashDrive

## WTHealth (radiation + psy/mental survival system)
- category: survival-stat-system / consumable-config / ppe | source: steam-workshop | popularity:  | license: proprietary/obfuscated (Satoshi); no license
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3597910943
- desc: Adds two accumulating stats to PlayerBase (AccumulatedRadiation, AccumulatedMental) with resist values, persisted via OnStoreSave/OnStoreLoad and synced via RegisterNetSyncVariableFloat. Field trigger objects (radiation/psy zones, FieldTypes enum) tick damage; protective gear and cures are DATA-DRIVEN Json tables synced to the client: WTHealthProtectBundleJson (a set of item classnames granting a Resist value, with per-item slot Depends), WTMedicItemJson (Classname+Recovery), WTHealthProtectConsumeItemJson (Classname+Consume). Radiation postprocess via a modded PPERequesterBank + PPERequester_Radiation (film grain/saturation/chrom-aberration). Clothing carries synced WTRadiationResist/WTMentalResist.
  - T: accumulating PlayerBase stats with RegisterNetSyncVariableFloat
  - T: OnStoreSave/OnStoreLoad + AfterStoreLoad+SetSynchDirty persistence
  - T: classname-keyed Json config tables (bundle/medic/consumer) synced via RPC
  - T: item.CanWork checks FindAttachmentByName + GetType().Contains for slot deps
  - T: FieldTypes/field-trigger zone damage system
  - T: modded PPERequesterBank + PPERequester_GameplayBase screen effects
  - CLASSNAMES: WTHealthProtectBundleJson, WTMedicItemJson, WTHealthProtectConsumeItemJson, WTHealthProtectItemJson

## PersonalChestWT (per-player persistent virtual storage)
- category: persistent-storage / virtual-inventory | source: steam-workshop | popularity:  | license: proprietary/obfuscated (Satoshi); no license
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3597910943
- desc: A deployable PersonalStorage : SeaChest object whose real cargo size is 0 - items are stored VIRTUALLY in a server-side WTPersonalStorageDB (CurrentSpace/MaximalSpace + arrays of WTPersonalStorageItemJson capturing classname, quantity, healthlevel, space cost, and nested attachments). Interaction is an ActionPersonalStorage (ActionInteractBase, CCTObject) that opens WTPersonalChestMenu (two-pane inventory<->storage UI with category filter). Item transfer goes over a PChestRPC enum (PutItem/GetItem/GetStorage) handled by a singleton PChestManager; server sends SynchObjects list of which classnames count as storage. modded PlayerBase blocks taking storage objects into hands and registers the action.
  - T: SeaChest-derived object with 0 real cargo, virtual server DB
  - T: WTPersonalStorageDB Json capturing classname/quantity/healthlevel/space/attachments
  - T: ActionInteractBase + CCTObject(10) to open storage menu
  - T: PChestRPC enum + singleton manager (Get_PChestManager)
  - T: two-pane inventory<->storage UIScriptedMenu with category filter
  - T: modded PlayerBase.CanReceiveItemIntoHands + SetActions(InputActionMap)
  - T: server SynchObjects classname whitelist to client
  - CLASSNAMES: PersonalStorage, WTPersonalStorageItemJson, WTPChestPlayerItemJson

## DLCSlots (extra equipment slots framework)
- category: inventory-slot-framework | source: steam-workshop | popularity:  | license: proprietary (Satoshi); config plaintext, no license
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3597910943
- desc: Small (0.3MB) unobfuscated dependency that adds 10 generic character attachment slots (DLCSlot1..DLCSlot10) via SurvivorBase attachments[]+= and InventoryEquipment playerSlots[]+=, with matching cfgSlots (ghost icons from a DLCIcons imageset) and ProxyAttachment CfgNonAIVehicles proxies. PDAWT depends on it (PDA_WT is worn in DLCSlot1). Slot names map to intended uses: PDA, timer/watch, HP, unloading.
  - T: SurvivorBase attachments[]+= and InventoryEquipment playerSlots[]+=
  - T: cfgSlots with ghostIcon from imageset (set:dlc_icons image:...)
  - T: CfgNonAIVehicles ProxyAttachment per slot
  - T: additive base-class extension for new wearable slots
  - CLASSNAMES: Slot_DLCSlot1, Slot_DLCSlot2, DLCSlot1, ProxyDLCPlayerSlot1

## Satoshi_Mamoebov (mutant skeleton/anim definitions)
- category: skeleton-anim-support (content-support) | source: steam-workshop | popularity:  | license: proprietary/obfuscated (Satoshi); no license
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3597910943
- desc: Support PBO (4.3MB) referenced by mod.cpp skeletonDefinitions="Satoshi_Mamoebov/Mutants.anim.xml". Contains cfgSkeletons/anim XML (Mutants.anim.xml lists ChimeraSkeleton.xob etc.) and the shared mutant animation assets used by the pack's creature PBOs. This PBO is NOT obfuscated the same way (BankRev extracted it cleanly) and served as the ground-truth to validate the LZSS decompressor against the other obfuscated PBOs.
  - T: cfgSkeletons + Mutants.anim.xml skeleton definitions
  - T: clean (non-obfuscated) BankRev-extractable PBO
  - CLASSNAMES: ChimeraSkeleton


# Local modpack — WeTernal STALKER mutants (@Radio folder)

## WeTernal STALKER Mutants (Satoshi) - animal/infected class
- category: mutant-content-pack | source: local-modpack | popularity: Steam item 'Radio' (id 3597910943): 409 subscribers; REMOVED from Workshop for guideline violation (likely unlicensed repack) | license: Proprietary/obfuscated, NO redistribution. Each PBO custom-obfuscated (custom LZSS + junk device-named entries) and phones home to https://dayz-mods.net/API/License/Status on connect, calling AbortMission() if the license check fails. Do NOT copy code; classnames are safe to reference as config inputs.
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3597910943
- desc: Content pack of STALKER creature entities that reuse vanilla DayZ AI bases (AnimalBase / DayZInfected / ZombieMaleBase). Corpses are harvested with a knife to drop configured body-part items. These body parts are exactly the kind of 'processing input' a research framework consumes (e.g. pseudodog tail -> petri dish -> microscope).
  - T: Corpse loot = VANILLA config-only: inside each creature CfgVehicles class a 'class Skinning { class ObtainedX { item="..."; count=N; itemZones[]={"Zone_Head"}; quantityMinMaxCoef[]={0.75,1}; countByZone[]={...} } }' block. Knife-skinning the corpse yields the items; NO EEKilled/CreateInInventory event handler is used.
  - T: Script side only overrides 'bool CanBeSkinned(){return true;}' on the creature class to enable harvesting
  - T: Entities subclass vanilla AI: AnimalBase (predators), DayZInfected (small infected), ZombieMaleBase (humanoid bosses); aiAgentTemplate ties to a CfgAIBehaviours template
  - T: Custom melee via CfgAmmo (e.g. MeleeTushkanKing : MeleeDamage) + PathGraphFilters + CfgAIBehaviours BehaviourHLZombie tuning (vision/noise/alert multipliers)
  - T: Held-item IK for body parts registered via 'modded class ModItemRegisterCallbacks' + AddItemInHandsProfileIK
  - T: DamageZones per body part (Zone_Head/Torso/Legs) with per-zone ArmorType (Projectile/Melee/FragGrenade) for weak-spot design
  - CLASSNAMES: Satoshi_Skinning_Tushkan_Head, Satoshi_Skinning_PsyDog_Head, Satoshi_Skinning_PsyDog_Tail, Satoshi_Skinning_Bloodsucker_Jaw, Satoshi_Skinning_Flesh_Hoof_1, Satoshi_Skinning_Cat_Left_Hand, Satoshi_Skinning_Cat_Right_Hand, Satoshi_Skinning_Boar_Hoof, Satoshi_Skinning_Boar_Claw, Satoshi_Skinning_BlindDog_Head, Satoshi_Skinning_Bullterrier_Head, Satoshi_Mutant_Psydog, Satoshi_Mutant_Bloodsucker, Satoshi_Mutant_Tushkan, Satoshi_Mutant_Cat

## WeTernal STALKER Mutants - boss/psi tier (Burer, Chimera, Controller, PseudoGiant, Izlom)
- category: mutant-content-pack | source: local-modpack | popularity:  | license: Proprietary/obfuscated, same license-check + no-redistribution as the animal-class PBOs. Reference classnames only.
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3597910943
- desc: Large humanoid/boss mutants. Bosses drop a rare unique 'hand/claw' trophy plus bulk HumanSteakMeat from the torso. Izlom is an infected with a scripted grab (RPC_RequestCatchByIzlom disables player control). These rare trophies suit high-tier / late research-tree inputs.
  - T: Same config-only 'class Skinning { class Obtained... }' loot; bosses list multiple ObtainedX blocks (rare hand + bulk HumanSteakMeat) with different itemZones and count
  - T: Boss variants are thin scope=2 subclasses of a scope=0 _Base (e.g. Controller_1/Controller_2 : Controller_Base) - lets admins spawn visual variants sharing one loot table
  - T: Izlom scripted capture ability: server->client ScriptRPC (enum MutantsWTRPC.RPC_RequestCatchByIzlom) toggling PlayerControlDisable(INPUT_EXCLUDE_ALL) - pattern reusable for a 'device busy, lock player' processing animation
  - CLASSNAMES: BurerHand, ChimeraClaw, ControllerHand, PseudoGiantLeftHand, PseudoGiantRightHand, Satoshi_Mutant_Part_Izlom_Hand, HumanSteakMeat, Burer, Chimera_1, Chimera_2, Controller_1, Controller_2, PseudoGiant, Satoshi_Mutant_Izlom, Satoshi_Mutant_Izlom_Dark

## FramerworkWT (WeTernal admin framework)
- category: admin-framework | source: local-modpack | popularity:  | license: Proprietary/obfuscated (author '123'); phones home to http://45.89.52.31/WhoIs. Do NOT copy code - use as an architecture reference only.
- desc: In-game admin GUI framework bundled in @Radio. A single root menu (WTFrameworkPanel) opens sub-panels for Fields, Trader, PDA, Presets, Animal spawns, Anomalies, Mutant-control, Passport, Players. Admin-authored data lives in JSON DB objects synced over ScriptRPC. This is the closest local analog to the admin-config + client-sync architecture a research framework needs.
  - T: ScriptRPC used ~66x for all client<->server sync (NOT CF GetRPCManager) - each admin panel edits a JSON DB then pushes via ScriptRPC
  - T: Admin authorization via net-synced bools on PlayerBase: RegisterNetSyncVariableBool('m_IsRootWT','m_AdminFields','m_AdminTrader',...) set server-side, gate UI client-side
  - T: JSON data-model classes drive every panel: WTAnimalTriggerItem, WTAnimalPointItem, WTDynamicZoneItem (anomalies), WTBundleItem/WTBundleDependItem, WTTraderItem, WTPresetItem, WTFractionItem/WTFractionMemberItem, WTMutantControlItem
  - T: Tier system: creatures/artifacts grouped into numbered tiers with per-trigger Chance, ReloadTime, ReloadClean, CreateRadius, DeleteRadius - a proven schema shape for recipe/tier configs
  - T: UIScriptedMenu + .layout widgets (TextListboxWidget search list, EditBox, XComboBox, ItemPreviewWidget, MapWidget) - native-widget UI approach (no Dabs MVC)
  - T: 'Copy classname from live search list' pattern (RTSearcher + RTSearchList + RTCopyButton + ItemPreviewWidget) for admins picking item classnames - great for recipe input/output pickers
  - T: Client singleton Get_WTFrameworkClient() driven from modded MissionGameplay OnKeyPress/OnUpdate/OnMouseButtonPress; server side modded MissionServer InvokeOnConnect for per-player setup
  - CLASSNAMES: WTAnimalTriggerItem, WTAnimalPointItem, WTDynamicZoneItem, WTBundleItem, WTTraderItem, WTPresetItem, WTFractionItem, WTMutantControlItem

## WTHealth (WeTernal radiation & psy survival system)
- category: survival-mechanics | source: local-modpack | popularity:  | license: Proprietary/obfuscated. Reference only.
- desc: STALKER radiation + psi/mental field survival layer bundled in @Radio. Players accumulate radiation and mental damage from zones; clothing provides typed resistances; consumable meds decrement counters. Item tooltips show protection stats via a custom imageset overlay.
  - T: Net-synced survival stats on modded PlayerBase: AccumulatedRadiation / AccumulatedMental with Limits, plus RadiationResist/MentalResist; field accumulation ticked in OnUpdate timers (m_FieldTimeElapsed etc.)
  - T: Typed resistance carried on modded Clothing via RegisterNetSyncVariableFloat('WTRadiationResist','WTMentalResist') + Get/SetResist(FieldTypes.RadiationFieldType,...) switch
  - T: JSON-driven config arrays: WTHealthProtectBundleJson (per-item resistances), WTMedicItemJson (meds), WTHealthProtectConsumeItemJson (consumables that decrement hazard)
  - T: Custom item tooltip overlay: modded ItemManager.PrepareTooltip injects a .layout child into GridSpacerWidget with per-stat icons from an imageset (set:WTStats image:WTStats_icon_N) - technique for showing research-value/recipe hints on items
  - T: Post-process + audio ambience keyed to field state: modded PPERequesterBank/PPEGlow register REQ_RADIATION/REQ_CONTROLLER_PSI requesters; MissionGameplay plays psy soundsets while in a psi field
  - CLASSNAMES: WTHealthProtectBundleJson, WTMedicItemJson, WTHealthProtectConsumeItemJson

## Zombified (WeTernal humanoid stalker-zombies)
- category: mutant-content-pack | source: local-modpack | popularity:  | license: Proprietary/obfuscated (config.bin binarized here, not text-obfuscated; still license-locked). Reference classnames only.
- desc: Humanoid infected reskinned as fallen STALKER factions, extending ZombieBase. Dozens of faction/armor variants (Novice, Zorya, Exoskelet, Ecolog, Merc, Duty, Freedom, Monolith, ClearSky, Military). Explicitly NOT skinnable (CanBeSkinned returns false) - they drop no body-part trophies.
  - T: Extends ZombieBase with a custom DayZCreatureAnimInterface command table (ZombifiedCommandScriptTable: BindCommand('CMD_WOUND'/'CMD_RELOAD'/'CMD_ATTACK'), BindVariableInt/Bool, BindTag) - pattern for driving custom creature/device animation graphs
  - T: Server->client sound push via OnRPC(rpc_type==135168, Param1<string>) -> SEffectManager.PlaySound with SetSoundAutodestroy
  - T: Faction variants are thin ZombieBase subclasses (Zombified_Zorya_Loner, Zombified_Ecolog_Blue, Zombified_Exoskelet_Duty ...) sharing one base
  - T: modded Weapon_Base OnRPC force-fire-sound trick for client-side weapon audio
  - CLASSNAMES: Zombified_Novice_ClearSky, Zombified_Zorya_Loner, Zombified_Zorya_Freedom, Zombified_Exoskelet_Duty, Zombified_Exoskelet_Monolith, Zombified_Ecolog_Blue, Zombified_Ecolog_White, Zombified_Merc_1, Zombified_Duty_Soldier, Zombified_Millitary


# Local modpack — STALKER content packs

## STALKER Equipment (SGE)
- category: content pack: faction clothing + artifact containers | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2841265089
- desc: Faction gear pack (prefix SGE_): 14 PBOs, one per faction (Stalker/Loner base, ClearSky, Duty, Freedom, Military, Mercenaries, Monolith, Bandits, Neutrals, Sunrise, ssp). Base suit classes (Sunrise, SEVA, Exo, gas masks) live in Stalker.pbo; faction PBOs are thin retexture children overriding hiddenSelectionsTextures. items.pbo adds artifact carry containers; ssp.pbo (CfgPatches SGE_Ecolog) adds the SSP-99 'Ecolog' scientist suit set.
  - T: Faction variant pattern: external base class declaration + child overriding only hiddenSelectionsTextures (per-faction PBOs load independently)
  - T: Custom attachment slots: CfgSlots + ProxyAttachment subclasses in CfgNonAIVehicles + inventorySlot[] on item (Bulat helmet glass up/down as two slots)
  - T: Item accepting modded attachments via foreign slot name (attachments[]={"NHArt1"} on artifact container)
  - T: ghostIcon via custom imageset (set:SGE_icons image:...)
  - T: repairableWithKits/repairCosts + DamageSystem healthLevels reusing vanilla rvmats
  - CLASSNAMES: SGE_Art_Container_Crude, SGE_SugarCase, SGE_ContainerParts, SGE_SSP_90, SGE_SSP_99M, SGE_SSP_Under, SGE_SSP_90_BackPack, SGE_SSP_90M_BackPack, SGE_SSP_90_Glass, SGE_RP5_Filter, SGE_ClearSky_Seva_Jacket, SGE_ClearSky_Seva_Vest, SGE_ClearSky_Seva_BackPack, SGE_ClerSky_Vest_CN1, SGE_ClerSky_Helmet

## STALKER Equipment ReTex
- category: retexture add-on | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2915365221
- desc: Single Retex.pbo (208MB) with per-subfaction folders (Bandits, Bartenders, Campfogor, Chup, Duty, ...) each holding textures plus its own config.bin. Retextures SGE gear (Bulat/Beril/Heavy/Seva/Sunrise/Wind/Overall lines) for RP subfactions like Bartenders.
  - T: Multiple config.bin per texture folder inside one PBO, each registering retexture child classes

## STALKER Weapons UA (Ukrainian Project Weapons)
- category: weapons pack | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3192684461
- desc: Single 585MB up_weapons.pbo, ~40 weapon families (AK12/AK105/Malyuk/Gadyka/Fort17/Groza/Abakan etc., prefix up_weapons_). Ships a ready types.xml (119 entries, all nominal=0) in the mod root's types/ folder for server admins.
  - T: Distributing types.xml in mod root (types/Ukrainian_Project_Weapons.xml) with nominal=0 so admins opt-in to spawns
  - CLASSNAMES: up_weapons_abakan, up_weapons_ak12, up_weapons_ak105, up_weapons_malyuk_545, up_weapons_gadyka, up_weapons_groza, up_weapons_fort17, up_weapons_pm, up_weapons_toz106

## DEAD AIR - Weapon pack STALKER (Air DevTeam)
- category: weapons pack + gameplay scripts | source: local-modpack | popularity:  | license: Free use, but $info.txt explicitly FORBIDS repacking into server packs/modpacks — must be used via Workshop collection
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2300143806
- desc: STALKER-trilogy weapon pack (100+ guns, grenade launchers, RPG-7, Gauss rifle, knives, gas/flash grenades, STALKER throwing bolt models in 1_ammo). air_WeaponScripts.pbo is an OBFUSCATED Mikero PBO (BankRev refuses extraction), but its file table is readable: modded Weapon_Base, WeaponGuard/WeaponGuardBattery/WeaponGuardNoBattery, ComponentEnergyManager (3_Game), gauss_Base, teargas grenade, monoscope, recipe scripts (AllowRecipe.c, sawofftoz34.c), ModItemRegisterCallbacks.
  - T: Obfuscated PBO distribution (Mikero) — anti-repack protection; file-name table still readable from header
  - T: Battery/energy gating via custom WeaponGuard chain (WeaponGuardBattery/NoBattery) on Weapon_Base
  - T: modded ComponentEnergyManager in 3_Game for energy behavior tweaks
  - T: Custom RecipeBase recipes shipped in 4_World/classes/recipes (sawofftoz34)
  - T: ModItemRegisterCallbacks.c on ManBase for registering item anim callbacks
  - T: Gas grenade (teargas) area-effect entity under Entities/grenade_base

## GSC Gameworld Assets (JMC Edition) / Area of Decay assets
- category: map assets: STALKER structures + example device scripts | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3154500253
- desc: 8 PBOs (~7.6GB) under prefix areaofdecay\: ported STALKER building sets — Agroprom incl. full underground/basement, Jupiter, Limansk, Dead City, Marshes, Dark Valley, Garbage, Darkscape, Dry Gully, Backwater (Skadovsk ship + Backwater_x12lab_1..6 lab modules), CHNPP monument — plus texture packs (incl. act_* mutant/corpse taxidermy textures) and tiny dz_yanova_models/dz_yanova_scripts PBOs with a cooking-pot station: static Land_Y_CookingPot + portable Y_CookingPotItem (extends FireplaceBase) and Land_Escape_village_source (extends Well).
  - T: Twin-class device: Y_CookingPotItem (portable, extends FireplaceBase) + Land_Y_CookingPot (static, extends HouseNoDestruct/BuildingWithFireplace) sharing one model
  - T: Free interactivity by base-class choice: extends Well gives drink/fill actions, extends BuildingWithFireplace gives fireplace UI on a map static
  - T: GUIInventoryAttachmentsProps: named slot groups (Fuel/Kindling/CookingEquipment) with icons in inventory UI — template for device Input/Output sections
  - T: Advanced placement: hologramMaterial + hologramMaterialPath + AddAction(ActionTogglePlaceObject/ActionPlaceObject) + GetPlaceSoundset
  - T: EEItemAttached/EEItemDetached switch on slot_name to track DirectCooking slots; SetCookingTime(0) reset to prevent exploits
  - T: EntityEvent.CONTACT|TOUCH mask + ThrowAllItemsInInventory on physics contact for destructible placed devices
  - T: CfgMods with worldScriptModule only (script mod without 3_Game/5_Mission modules)
  - CLASSNAMES: Land_Y_CookingPot, Y_CookingPotItem, Land_fireplace_1, Land_fireplace_2, Land_Escape_village_source

## STALKER NewHorizon Objects (Legacy)
- category: map assets: buildings, lab props, mutant taxidermy, anomaly statics | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2673371735
- desc: Huge (~7GB, 50 PBOs) STALKER environment library by [NewHorizon] Persik (v0.5.1458): Chernobyl/Pripyat/Zaton/Rostok/Jupiter/CNPP building sets, X8 lab modules (x8_part*_Laba), anomaly statics (anomaly_water with animated sphere, kogot claws, anomaly stones, distorted vehicles), Monolith corpse statics, and NH_ChernobylZoneFurniture.pbo with ~500 indestructible interior statics: bunker-lab interiors, instrument consoles, workbenches, safes, radioactive barrels, stuffed mutants. Also 4_World scripts adding custom continuous actions to statics (nh_OldWell with ActionSladyDrinkWellContinuous/ActionSladyWashHands, NH_BuildingWithFireplace, NHPointLight).
  - T: Custom ActionContinuousBase actions registered on static map objects (nh_OldWell: drink/wash-hands continuous actions)
  - T: All furniture statics extend HouseNoDestruct with zeroed GlobalArmor damage — stable prop base
  - T: AnimationSources on statics for toggleable lamps/vents (Land_prop_roof_celling_light with lamp_1..3 sources)
  - T: Per-region config.cpp inside one PBO (agroprom/anomaly/jupiter/... each with own config)
  - T: Script classes for point light/fire effects on map objects (NHPointLight, NHPointFire, MapSmokeEffect)
  - CLASSNAMES: Land_int_bunker_laba, Land_prop_pribori_02, Land_prop_pribori_03, Land_prop_ecolog, Land_pult, Land_mtl_pult1, Land_prop_pult4, Land_mtl_vesa, Land_mtl_verstak, Land_mtl_safe_02, Land_mtl_barrel_radioactive, Land_mtnt_chhl_krovosos, Land_mtnt_chhl_psevdog, Land_Military_paper, Land_prop_map

## STALKER Ambience & Music
- category: audio + JSON-config scripts | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2876083575
- desc: By Shlyosha: STALKER ambient tracks and zone moans played to players on randomized server timers. Tiny script set (6 files) implementing a $profile: JSON config (StalkerAmbience.json) with auto-generated defaults, and server->owner-client RPC triggers for music playback.
  - T: $profile: JSON config: const m_ConfigRoot, FileExist -> CreateDefaultConfig -> JsonSaveFile, else JsonFileLoader<T>.JsonLoadFile
  - T: UpgradeConfig(): load-migrate-resave hook for config schema versioning
  - T: Config singleton on modded DayZGame (Set/GetBackgroundMusicConfig), loaded in modded MissionServer constructor
  - T: Targeted per-player RPC: GetGame().RPCSingleParam(this, RPC_ID, NULL, true, GetIdentity()) with global const int IDs
  - T: Server-side periodic logic in PlayerBase.OnScheduledTick(deltaTime) with randomized offsets


# Local modpack — AI and creatures

## PseudoGiant
- category: ai-creature | source: local-modpack | popularity: 188,397 subscribers (510,866 lifetime), updated 2022-08-12 | license: ADPL-SA + CC BY-NC-ND 4.0; ported GSC Game World assets remain GSC commercial property
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2847957663
- desc: STALKER pseudogiant ported as a DayZ infected: PseudoGiant_Base extends ZombieMaleBase with a custom animation graph, custom CfgAIBehaviours agent template and PathGraphFilters. Adds scripted AOE shockwave attack, sleep and injured states via DayZInfectedCommandScript subclasses. Corpse is skinnable but drops only vanilla items (HumanSteakMeat x20, Guts, Lard, Bone).
  - T: DayZInfectedCommandScript subclasses + StartCommand_Script for scripted creature states (AOE attack, sleep, injured)
  - T: DayZCreatureAnimInterface BindCommand/BindTag/BindVariableBool for anim-graph control
  - T: RegisterAnimationEvent("ScriptClient"/"ScriptServer") to route anim events to client (particles, camera shake) vs server (Explode + physics impulse) handlers
  - T: Explode(DT_EXPLOSION, customAmmo) + GetGame().GetObjectsAtPosition3D + dBodyApplyImpulse for AOE damage
  - T: EEOnDamageCalculated returning false to gate damage below a threshold / during invulnerable states
  - T: class Skinning{} in CfgVehicles with per-zone item yields (itemZones/countByZone/quantityMinMaxCoef)
  - T: CfgAIBehaviours custom agent template (aiAgentTemplate) + PathGraphFilters to restrict pathing
  - T: override CanBeSkinned()/CanBeBackstabbed()/ResistContaminatedEffect() creature capability flags
  - CLASSNAMES: PseudoGiant, PseudoGiant_Base, HumanSteakMeat, Guts, Lard, Bone

## Renegade Snork
- category: ai-creature | source: local-modpack | popularity: 29,322 subscribers (71,389 lifetime), updated 2025-02-25 | license: Proprietary: no repacks allowed; inspecting the code for learning explicitly permitted (workshop page)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3405220401
- desc: STALKER snork on its own skeleton, SMCS_Snork_Base extends ZombieMaleBase with overridden FightLogic (jump attack at 7-8 m, random close attacks). Skinning drops MutantSnorkHead/MutantSnorkLeg via CfgVehicles Skinning class, plus extra JSON-configured loot spawned by a modded ActionSkinning.
  - T: JsonFileLoader<T> config in $profile:RenegadeSnork\ with CreateDefaultConfig autogen on first start and an UpgradeConfig hook for migrations
  - T: modded ActionSkinning.OnFinishProgressServer + body.IsInherited(X) to add JSON-configured corpse loot (GetGame().CreateObject + PlaceOnSurface)
  - T: config singleton stored on modded DayZGame (GetDayZGame().GetSnorkConfig()) loaded in MissionServer constructor
  - T: FightLogic/ChaseAttackLogic/FightAttackLogic overrides with StartCommand_Attack(target, attackTypeIndex, subtype)
  - T: DamageSystem.CloseCombatDamageName with custom CfgAmmo melee classes for distinct heavy/medium/light hits
  - T: anim variable binding class (BindVariableInt "InjuryLevel") driving injured movement animations
  - T: mod.cpp skeletonDefinitions pointing at custom_skeletons.xml for a fully custom creature skeleton
  - CLASSNAMES: SMCS_Snork, SMCS_Snork_Hard, SMCS_Snork_Base, MutantSnorkHead, MutantSnorkLeg

## InediaInfectedAI
- category: ai-overhaul | source: local-modpack | popularity: 233,850 subscribers (484,495 lifetime), updated 2026-07-25 | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3031784065
- desc: Large zombie-AI and medical overhaul by Yauhen Saroka (johnkind): smarter infected (search, door opening, projectile throwing), an irritant/smell sensory system, and a pain/injury medical layer with surgery-style continuous user actions. Everything is driven by a giant JSON config in $profile:Inedia\ with a configuration guide on GitHub (ysaroka/InediaInfectedAI wiki).
  - T: custom InediaInfectedAI_JsonFileLoader<Class T> wrapping JsonSerializer.ReadFromString to capture GetLastError() and raise an admin-friendly Error() on malformed config
  - T: [NonSerialized()] attribute to exclude runtime fields from JSON serialization
  - T: config lifecycle hooks per section: Initialize / InitializeBeforeCreatingConfigFile / InitializeAfterCreatingConfigFile + SaveConfigAfterInit flag
  - T: hot reload: static ReloadInstance() bound to \ireload admin chat command (MissionServer.OnEvent(ChatMessageEventTypeID) router, commands prefixed with backslash)
  - T: admin gating via separate InediaAdminsConfig.json with AdminsSteamIds array checked against identity
  - T: player-state persistence via OnStoreSave/OnStoreLoad delegated to a manager object (PainManager.OnStoreSave)
  - T: RegisterNetSyncVariableInt bitfield packing (many bools in one synced int)
  - T: shipping optional server-economy presets (types/casual, types/hardcore XML) in the mod folder for admins to merge
  - CLASSNAMES: InediaPain_FirstAidKit_White, InediaPain_FirstAidKit_Black, InediaPain_FirstAidKit_Camouflage, InediaPain_FirstAidCase_White, InediaPain_FirstAidCase_Black, InediaPain_FirstAidCase_Camouflage, InediaPain_Tramadol, InediaPain_Tranexam, InediaPain_Calcium, InediaPain_Sorbifer, InediaPain_Metoclopramide, InediaPain_SurgicalKit, InediaPain_SplintKit, InediaPain_HemostaticDressing

## SuppressionMod (SpaghettiSuppression)
- category: gameplay-mechanics | source: local-modpack | popularity: 42,408 subscribers (133,517 lifetime), updated 2024-01-13 | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2934318559
- desc: Tiny (30 KB) scripts-only mod adding suppressive-fire effects: raycast from the muzzle finds suppressed players, applies a decaying suppression level with PPE tunnel vision, camera shake, sound muffling and stamina drain. Ear-protection items cancel it, configurable via a text list.
  - T: [CF_RegisterModule(X)] under #ifdef SERVER + CF_ModuleWorld + EnableClientPrepare(); OnClientPrepare(CF_EventPlayerPrepareArgs) sends settings to the connecting client
  - T: whole-object config sync: ScriptRPC().Write(configInstance) server-side; client reads via ctx.Read(config) in modded DayZGame.OnRPC with a custom RPC enum
  - T: RPC id collision avoidance: sentinel enum values (INVALID = -234234) and high unique ids in an ERPCs extension enum
  - T: JsonFileLoader load-then-save idiom: JsonLoadFile then JsonSaveFile immediately, auto-creating defaults and auto-adding new fields after mod updates
  - T: alternative CF RPCManager path: GetRPCManager().AddRPC("ModName", "FuncName", this) + SendRPC in MissionServer.InvokeOnConnect
  - T: plain-text line-per-entry config (FGets loop) for simple item lists
  - T: custom player modifier: ModifierBase subclass + modded ModifiersManager.Init() AddModifier, with enum extension (eModifiers : = 42)
  - T: modded StaminaHandler registering custom depletion/recovery modifiers via enum extension

## Alteria (server map)
- category: map | source: local-modpack | popularity: 78,771 subscribers (177,221 lifetime), 4.88 GB, updated 2025-05-03 | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3296994216
- desc: The modpack's terrain by JohnMcLane (DeerIsle team): 20 PBOs of map data including a stalker-themed object pack (jmc_stlk.pbo, textures/buildings), sewers, industrial assets. Its scripts.pbo contains only a custom loading/login screen and one gameplay class: Land_jmc_drinkmachine extends Well (drink machines act as water sources).
  - T: map-object gameplay via one-liner class inheritance (Land_X extends Well) instead of scripted actions
  - T: modded LoadingScreen / LoginQueueBase / LoginTimeBase for server branding (LoadImageFile on ImageWidget)
  - T: PBO filename-table header reading confirmed contents without extracting 600 MB archives
  - CLASSNAMES: Land_jmc_drinkmachine

## StalkerAIPack2
- category: ai-framework-unverified | source: steam-workshop | popularity:  | license: 
- desc: Not analyzable: the !Workshop\@StalkerAIPack2 folder is a junction to steamapps\workshop\content\221100\3600948064, whose target does not exist locally, and the Steam API (GetPublishedFileDetails) returns result=9 (file not found) for id 3600948064 — the workshop item is hidden, private or removed. Likely the modpack's private AI pack upload.

## StalkerAIserver
- category: ai-framework-unverified | source: steam-workshop | popularity:  | license: 
- desc: Not analyzable: junction target steamapps\workshop\content\221100\3579869254 is missing locally and the Steam API returns result=9 (file not found) for the id — hidden/private/removed workshop item. The name suggests a server-side companion PBO for the StalkerAI pack.


# Local modpack — KarmaKrew server pack

## KK_SkillFramework (@KarmaKrew Alteria Balance)
- category: skill-framework | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3135954430
- desc: Minimal 'framework' PBO: one script file declaring `enum EPlayerStats_KK: EPlayerStats_current { STAMINASOFTSKILL, COLDRESISTANCE }` plus config.bin. It merely reserves PlayerStats slot IDs; actual skills are registered by consumer PBOs through modded PlayerStatsPCO_current.Init() with RegisterStat(). Persistence rides on the vanilla player-stats save (per-life).
  - T: enum EPlayerStats_KK: EPlayerStats_current — reserving PlayerStats IDs across mods
  - T: modded class PlayerStatsPCO_current { override Init() { RegisterStat(id, new PlayerStat<float>(min,max,default,name,EPSstatsFlags.SYNCED)); } }
  - T: PlayerStat<float>.Cast(GetPlayerStats().GetStatObject(id)) .Get()/.Set()/.Add()
  - T: Engine-side persistence of registered stats in player save (per-life)

## KK_StaminaSoftSkill (@KarmaKrew Alteria Balance)
- category: skill-framework | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3135954430
- desc: Consumer of KK_SkillFramework: a soft-skill that grows max stamina cap when sprinting outside territories and shrinks it when idle/inside territory, ticked every 20s in modded StaminaHandler. Server config lean_config.json in $profile:KK_StaminaSoftSkill, synced to clients by CF RPC on connect; negative skill carried across death via per-UID JSON saved in EEKilled and restored+deleted in OnClientNewEvent.
  - T: RegisterStat(EPlayerStats_KK.STAMINASOFTSKILL, new PlayerStat<float>(-15,15,0,"StaminaSoftSkill",EPSstatsFlags.SYNCED))
  - T: RegisterNetSyncVariableFloat on PlayerBase + SetSynchDirty for HUD mirror
  - T: JsonFileLoader<LeanConfig> in $profile with CreateTemplate() on first start
  - T: GetRPCManager().SendRPC("KK_StaminaSoftSkill","Lean_OnSync",Param1<ref LeanSyncData>,...) in OnClientReadyEvent
  - T: Per-UID death carry-over: EEKilled -> save $profile:.../saves/<uid>.json; OnClientNewEvent -> load+DeleteFile
  - T: BP_FlagPole_Base.FindNearestFlag(pos) territory detection inside StaminaHandler tick
  - T: #ifdef SERVER guards to strip server config classes from client build

## ZenNotes (bundled in @KarmaKrew; by Zenarchist)
- category: notes-documents | source: local-modpack | popularity:  | license: 
- desc: Handwritten in-game notes: pen + paper opens a UIScriptedMenu editor with fonts and colors; server validates text (word/SteamID blacklist), then replaces the Paper item with a ZenNote whose text/font/color persist in OnStoreSave. Reading requests data from server by RPC; notes are placeable and burnable in fireplaces.
  - T: ActionContinuousBase gated by ConfigIsExisting("penColor") on tool — config-driven tool detection
  - T: OnStartClient opens GUI / OnStartServer prepares data + sends date RPC — split client/server action hooks
  - T: Item-level OnRPC with unique negative enum ids (WRITE_NOTE=-3761990)
  - T: ServerReplaceItemInHandsWithNew(TurnItemIntoItemLambda) to convert Paper->ZenNote with data
  - T: OnStoreSave/OnStoreLoad ctx.Write/Read of ref ZenNoteData (no CF ModStorage)
  - T: UIScriptedMenu + .layout + MultilineEditBoxWidget + PPEffects.SetBlurMenu; EnterScriptedMenu with custom menu id
  - T: JSON config $profile:Zenarchist/ZenNotesConfig.json with CONFIG_VERSION string + _old backup on mismatch
  - T: NotificationSystem.SendNotificationToPlayerIdentityExtended for server->player warnings
  - CLASSNAMES: ZenNote, Paper, PunchedCard, Pen_Black, Pen_Red, Pen_Green, Pen_Blue, Pen_Pink, Pen_Purple, Pen_Orange

## KarmaKrew Scripts (Scripts.pbo, @KarmaKrew)
- category: server-framework | source: local-modpack | popularity:  | license: CC BY-NC-ND 4.0
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2864245850
- desc: The modpack's main script mod: a BaseBuildingPlus fork (BP_ prefixed base building, crafting recipes, upgrade/repair actions, flag territories with group management) plus KarmaKrew bunker systems (fusebox minigame, biometric scanner, elevator, bunker door), placement-limit zones, and custom disease agents (BAD_AIR with hypoxia modifiers). License CC BY-NC-ND 4.0 (DayZ Black Project).
  - T: BP_ElectronicsBase: device base with isPowered/isLowPower net-sync, IsStatic() override, UpdateElectronics() called from OnVariablesSynchronized
  - T: KK_FuseBox: FuseBoxState enum + RegisterNetSyncVariableBoolSignal one-shots + digit display via SetObjectTexture, screen via SetObjectMaterial
  - T: Group system: BP_FlagPole_Base.GetGroupHash(), PlayerBase RegisterNetSyncVariableInt(m_GroupHash), PluginGroupManager (PluginBase) with named RPCs
  - T: Placement limits: ZoneConfig {circleZones[], polygonZones[]} JSON synced by RPC (PluginPlacementLimits)
  - T: Custom agents: enum eKKAgents: eAgents {BAD_AIR} + transmission agent + ModifiersManager entries (hypoxia)
  - T: Continuous station actions: bp_actiontablerepairitem, bp_actionupgradecontainer/flag on placed structures
  - T: Recipe classes registered via PluginRecipesManager fork (bp_ craft recipes)
  - T: Access-item consumables for devices (keycard/fuse/severed finger for biometric scanner)
  - CLASSNAMES: KK_Card, KK_Finger, KK_Fuse, KK_BiometricScanner, KK_FuseBox, KK_Elevator, KK_BunkerDoor, BP_PowerGeneratorStatic, BP_FlagPole_Base, BP_ContainerKit, KK_Palisade, KK_Palisade_Kit

## KK_Core (@KarmaKrew)
- category: content-core | source: local-modpack | popularity:  | license: CC BY-NC-ND 4.0
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2864245850
- desc: 175MB content core: headgear, items (mostly skin-variant clones of vanilla like TacticalBaconCan_2..N), custom maps, radio, structures (bunker room statics, loot-spawn furniture variants), vests, weapons, zombies, plus kk_scripts (group-hash actions, ghillie recipes, HUD tweaks). Config-heavy; scripts are small.
  - T: ActionSingleUseBase writing flag GroupHash into an item (ActionSetGroupHash_CompensationCrate)
  - T: Static prop subclasses as CE loot spawn points (StaticObj_Furniture_shelf_LootSpawn pattern)
  - T: Skin-variant item cloning (ClassName_2..N inheriting base) for retextures
  - T: RPC channel/name constants in a dedicated 3_game file (KK_CORE_RPC_CHANNEL)
  - CLASSNAMES: PunchedCard_2, PunchedCard_3, LXD_Battery9V, PersonalRadio, KK_Banner, KK_ChernarusMap, KK_EssekerMap, KK_AlteriaMap, KK_LivoniaMap_1, Admin_CompensationCrate, KK_Spetznas_Room1, StaticObj_Furniture_shelf_LootSpawn, StaticObj_Furniture_shelf_DZ_KKIndustrialWorkstation

## LBmaster_Core (@KarmaKrew, by LBmaster)
- category: admin-framework | source: local-modpack | popularity:  | license: proprietary (no modify/redistribute)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2864245850
- desc: LBmaster's core library: a JSON config framework (LBConfigBase with version field, OnLoad/OnSave hooks, custom RPC read/write, per-player configs, in-game reload menu), an int-id RPC handler base with permission-gated admin sends, notification system, currency config, webhook (Discord) logging, REST API client, QR-code generator, and a declarative widget-linker UI framework. Proprietary license: no modification or redistribution.
  - T: LBConfigBase: version int + UpdateVersion/LoadDefault/OnLoad(bool save)/OnReloadedFromDisk lifecycle + WriteToCtx/ReadFromCtx for RPC sync
  - T: LB_RPCHandler_Base: map<int, handler-config> registered RPC ids, Send/SendAll/SendAdmins(permission)
  - T: LBGroupInfoShare: static map<steamid, Param2<groupName,shortName>> pushed by raw ScriptRPC to permission-holding admins
  - T: Permission system: LBAdmins.Get().HasPermission("admin.view.playerinfo", identity)
  - T: Widget-linker: declarative binding of script vars to layout widgets (lbwidget_linker)
  - T: Discord webhook logging + REST API client (RestApi) from Enforce
  - T: In-game config reload admin menu (lbconfigreloadmenu)

## LBmaster_AdminTools (@KarmaKrew, by LBmaster)
- category: admin-tools | source: local-modpack | popularity:  | license: proprietary (no modify/redistribute)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2864245850
- desc: Admin menu suite on LBmaster_Core: player list/info/map, ESP, item & preset spawner, object builder, teleports, ban/watchlist managers (with web API structs), weather/time presets, audit log, spectator camera, console commands. VPP building-set import supported.
  - T: Modular admin menu frames (one class per tab/module)
  - T: Audit log messages mirrored to webhooks
  - T: Console command classes (lbconsolecommandbase) with modifier toggles

## KK_C4_Timer (@KarmaKrew)
- category: config-pattern | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2864245850
- desc: Timed plastic explosive: craft recipes gated by config, arm/disarm continuous actions, countdown with accelerating beep curve. Ships the cleanest config service in the pack: versioned JSON with stepwise Validate() migrations, defaults auto-write, RPC sync-to-client on demand with a client received-flag.
  - T: JsonFileLoader<T>.LoadFile/SaveFile with out errorMessage (newer API)
  - T: int version + Validate() stepwise migrations, auto re-save after migration
  - T: Static config service class (Get/Load/SyncTo/ReceiveClientConfig/HasClientSync)
  - T: GetRPCManager SendRPC of whole config object in Param1<ref T>
  - T: Recipe enable/disable via config flags checked in recipe/action conditions
  - T: Rules sub-object per device type (GetRulesByTypeName)
  - CLASSNAMES: KKCT_TimedPlasticExplosive

## ZenCauseOfDeath (bundled in @KarmaKrew; by Zenarchist)
- category: gameplay-action | source: local-modpack | popularity:  | license: 
- desc: Adds a 2-second 'check cause of death' continuous action on player corpses; server tracks death causes via modded death modifiers and returns a localized message. JSON config for strings/genders.
  - T: ActionContinuousBase with CAContinuousTime(N) fixed-duration component
  - T: ContinuousInteractActionInput for hold-to-interact on target
  - T: Death-cause tracking via modded player modifiers, read post-mortem
  - T: TryHideItemInHands(true/false) during action

## KK_ToolBox_Client (@KarmaKrew)
- category: crafting-tweak | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2864245850
- desc: Tiny PBO: modded PluginRecipesManager adds UnRegisterRecipies(array<string>) helper that unregisters recipes then regenerates the recipe cache; missiongameplay hook applies it. Used to strip unwanted vanilla recipes.
  - T: UnregisterRecipe(name) + GenerateRecipeCache() in modded PluginRecipesManager

## KK_Balance_Client (@KarmaKrew)
- category: balance | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2864245850
- desc: Config-only rebalance PBO: config.bin overrides for muzzles, projectiles, vest armor, and weapons. No scripts.

## CS_Buildables (@KarmaKrew, by CanadianSniper)
- category: buildables | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2864245850
- desc: 'CS' is CanadianSniper, not Clear Sky: six static treehouse buildings (Land_CS_Treehouse .. _5 inheriting HouseNoDestruct) with camo-net/wood materials. CfgMods declares script modules but the PBO ships no .c files.
  - CLASSNAMES: Land_CS_Treehouse, Land_CS_Treehouse_1, Land_CS_Treehouse_2, Land_CS_Treehouse_3, Land_CS_Treehouse_4, Land_CS_Treehouse_5

## bastard_framework (@KarmaKrew, by Bastard)
- category: vehicle-framework | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2864245850
- desc: Shared script base for the bastard_* vehicle PBOs (UAZ, LiAZ, Dnepr, Tigr, quadbike): modded CarScript syncing battery/plug/radiator health from attachments in EEItemAttached, MotoBattery item, headlight switching action, animation defines. Russian anti-theft ASCII-art license comment; treat as all-rights-reserved.
  - T: EEItemAttached(item, slot_name) switch to mirror attachment state into device vars
  - T: Shared framework PBO consumed by per-vehicle script PBOs
  - CLASSNAMES: MotoBattery, CanisterGasoline

## QuickLoader (@KarmaKrew Alteria Balance)
- category: server-performance | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3135954430
- desc: Server-load accelerator: converts networked objects to static where safe (config lists ForcedDynamicObjects), ships a full CE mission-data mirror, and registers its settings declaratively via `[RegisterMissionSetting(QuickLoaderSettings, "quick_loader.json")]` on a MissionSetting subclass with JsonSerializer-based WriteToJson/ReadFromJson.
  - T: [RegisterMissionSetting(Class, "file.json")] attribute-based settings registration
  - T: MissionSetting base with JsonSerializer WriteToString/ReadFromString overrides

## KK_StaminaSoftSkill siblings: KK_FootSteps_Client / NoForceWeaponRaise / z_KK_Loading_Generic (@KarmaKrew Alteria Balance)
- category: misc-qol | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3135954430
- desc: Small config-only PBOs in the Alteria Balance pack: footstep sound tweaks (config.bin only, no scripts), weapon-raise behavior removal, and a loading-screen override. No processing-relevant content.


# Local modpack — base building, storage, locks

## MMG Base Storage
- category: base-building / storage containers | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3210162677
- desc: Placeable lockable storage (lockers, safes, crates, gun racks, fridges, greenhouse) built with a kit->hologram->placement pattern. 492MB PBO; full script set and configs extracted via header carving. JSON server config auto-generated in $profile:\MMG_Storage\MMG_Config.json and pushed to clients on connect.
  - T: Kit->placement: kit extends ItemBase, AddAction(ActionTogglePlaceObject)+ActionDeployObject; OnPlacementComplete server-side GetGame().CreateObject(placedClass, pos) then kit.Delete(); config props hologramMaterial/slopeTolerance/yawPitchRollLimit/alignHologramToTerain
  - T: Hologram preview: modded class Hologram overriding ProjectionBasedOnParent() (returns holo classname from item in hands), PlaceEntity() (returns entity itself), EvaluateCollision() SetIsColliding(false)
  - T: JSON admin config: PluginBase ctor auto-creates $profile: dir+file, JsonFileLoader<T>.JsonLoadFile + config.Validate() + save-back; client receives via Event_OnRPC + magic int RPC id; MissionServer.InvokeOnConnect sends Param1<ConfigClass> via RPCSingleParam
  - T: Openable storage: RegisterNetSyncVariableBool(m_IsOpened), OnStoreSave/OnStoreLoad persist open state, AfterStoreLoad CallLater auto-close, GetInventory().LockInventory(HIDE_INV_FROM_SCRIPT) when closed, CanReceiveItemIntoCargo gated by IsOpen()
  - T: Tier upgrade in place: ActionContinuousBase checks quantities in custom attachment slots (woodenplank_upgrade_x10, nail_upgrade_x30), then ObjectDelete + CreateObject(nextTier) preserving position/orientation
  - T: Soft cross-mod integration via #ifdef CodeLock / #ifdef EXPANSIONMODBASEBUILDING (CfgMods defines) incl. CanDisplayAttachmentCategory gating attachment slots by config
  - T: SetSimpleHiddenSelectionState + SetAnimationPhase for door/lid visual state driven by netsync
  - T: RecipeBase kit crafting + dismantle recipes as separate recipe classes registered in modded PluginRecipesManager
  - CLASSNAMES: mmg_storage_openable_base, mmg_storage_placeable_base, mmg_box_kit, mmg_supplycrate_level1, mmg_supplycrate_level3, mmg_equipment_locker_green, mmg_solo_locker_medic, mmg_armored_cabinet_grey, mmg_old_safe_green, mmg_lockable_gun_rack_grey, mmg_weapon_crate_wood, mmg_fridge_minsk_blue, mmg_palette_storage, mmg_greenhouse_kit, mmg_gear_stand

## Code Lock (GDayZ)
- category: lock / attachment+UI | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1646187754
- desc: Classic CodeLock: an ItemBase attachment for fences/tents that adds a scripted keypad UI. Owner/guest lists persisted in the entity; plugin-based RPC handler, permission manager and file logger. Config is a JSON class with Validate().
  - T: Attachment adds interactions to parent: ActionSingleUseBase OnExecuteClient does PredictiveTakeEntityToTargetAttachmentEx(parent, item, slotID) then GetCLUIManager().ShowMenu(id, parent)
  - T: UIScriptedMenu keypad: EnterScriptedMenu + menu.SetTarget(itembase); OnShow disables player control (INPUT_EXCLUDE_ALL), shows cursor, hides HUD
  - T: Server RPC plugin: PluginBase subscribing GetDayZGame().Event_OnRPC, switch over enum CLRPC with Param2/Param3 typed reads, per-action owner checks, error feedback RPC back to client
  - T: Server-authoritative secrets: GetPasscode() returns dummy on client; UI never sees stored code
  - T: Netsync force trick: _flipBool toggled with each change so SetSynchDirty always propagates; RegisterNetSyncVariableInt for enum action (LockAction) drives client sound/visual replay
  - T: Sync-on-demand: client OnVariablesSynchronized sends RPC_SERVER_SYNCHLOCK, server replies with owner+guests only to that requester
  - T: OnStoreSave/OnStoreLoad persisting string + bool + Param1<ref array<string>> guest list
  - T: Attachment slot injected into vanilla classes via config: class CodeLock attachment slot + GUIInventoryAttachmentsProps category on MediumTent/LargeTent/Fence
  - CLASSNAMES: CodeLock

## IAT_Codelock (ItsATreee Codelock)
- category: lock / CF ModStorage showcase | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3510402193
- desc: Modernized codelock by ItsATreee (source at github.com/Treee/DayZMods): CF ModStorage persistence with vanilla fallback, JSON config with auto-generation, entity-level OnRPC handling, passcode rate limiting with damage-on-fail, and a RecipeBase that converts the legacy CodeLock item.
  - T: CF ModStorage: class IAT_Codelock extends ModStructure in 3_Game; CF_OnStoreSave/CF_OnStoreLoad with auto ctx = storage[IAT_Codelock]; arrays persisted as count + loop; wrapped in #ifdef CF_MODSTORAGE with #ifndef vanilla OnStoreSave fallback
  - T: JSON config: [NonSerialized()] attribute on path fields; TryGetCodelockConfig() creates dir, writes defaults with JsonFileLoader<T>.SaveFile(path, obj, errorMessage), else LoadFile; cached on modded DayZGame
  - T: Entity-level RPC: override OnRPC on the item, enum-typed messages, server replies with ScriptRPC rpc.Write(...) rpc.Send(this, type, true, sender)
  - T: Anti-bruteforce: RegisterNetSyncVariableInt failed-attempt counter on PlayerBase, config-driven damage per failed passcode entry and cooldown interval
  - T: RecipeBase item migration: ingredient CodeLock + Pliers -> IAT_Codelock_Basic with CanDo checking GetHierarchyParent() == null
  - T: Config-driven raid tools as array of typed objects (classname, damage, time, uses) in JSON
  - CLASSNAMES: IAT_Codelock_Basic, IAT_Codelock_Painted, IAT_Codelock_CombinationLock3, IAT_Codelock_CombinationLock4, IAT_LockableSeaChest, IAT_RaidHacksaw_Basic, IAT_RaidHacksaw_Admin

## IAT_MoreDoors
- category: base-building / multi-variant placement | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3523295683
- desc: One door kit that places any of ~15 door types. The kit reads its option list from config arrays and exposes the choice through action variants; the selected variant drives both the hologram preview and the spawned object. By ItsATreee, source on GitHub.
  - T: Action variants: action ctor subscribes GetVariantManager().GetOnUpdateInvoker(); OnUpdateActions calls SetActionVariantCount(options.Count()); m_VariantID identifies chosen entry; OnActionInfoUpdate rewrites m_Text per variant
  - T: Client->server action payload: custom ActionData/ActionReciveData subclasses with overridden SetupAction, WriteToContext, ReadFromContext, HandleReciveData carrying the selected variant int
  - T: Config-driven variants: iat_KitOptionDisplayNames[] / iat_KitOptionClassNames[] read via ConfigGetTextArray in InitItemVariables — per-kit subclass just changes config arrays
  - T: Selection-dependent hologram: modded Hologram GetProjectionName(item) returns kit.GetDoorClassNameFromSelection()
  - T: OnPlacementComplete uses CreateObjectEx(name, pos, ECE_PLACE_ON_SURFACE) + DeleteSafe(); IsBasebuildingKit/DoPlacingHeightCheck/HeightCheckOverride for placement rules
  - CLASSNAMES: IAT_MoreDoor_Kit, IAT_MoreDoorKit_WoodOnly, IAT_MoreDoorKit_MetalOnly, IAT_MoreDoorKit_IronOnly, IAT_ConstructionDoor_Double_Iron, IAT_ConstructionDoor_Single_Left_Wooden, IAT_LandDoor_Colorbase

## CBD Map Buildings Codelock Addon
- category: lock / static map-object state | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3655731091
- desc: Adds codelocks to doors of STATIC map buildings (no entity attachment): per-door state lives in a JSON registry keyed by building world position + door index, restored on mission start. Built on a CF module with named RPCs.
  - T: CF module lifecycle: [CF_RegisterModule(X)] class X : CF_ModuleWorld; OnInit EnableInvokeConnect/EnableMissionStart; OnMissionStart defers init via GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(InitializeModule, 1000)
  - T: CF named RPCs: GetRPCManager().AddRPC("ModName", "RPC_Fn", this, SingeplayerExecutionType.Server/Client); SendRPC with Param1<ObjectClass> including whole settings objects
  - T: Static object registry: JSON array of {BuildingTypeClassName, BuildingPos, Locks[{doorIndex, code, owner, guests[], isLocked}]} in $profile:; lookup by vector.Distance(pos) < 1.0 + index
  - T: Secret-stripping sync: Registry.ToSyncData() deep-copies with m_Code="" before broadcasting to all clients
  - T: State restore: on mission start iterate registry and re-apply Building.LockDoor(pos, doorIndex) to world objects
  - T: Admin gating: admins JSON file server-side, client informed via RPC_SetAdminsFromFile bool
  - T: OnInvokeConnect (CF_EventPlayerArgs) used to send per-player config + registry + admin flag

## BuilderItems
- category: content-pack / static map props | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1565871491
- desc: Huge static-prop library for map editing: 1437+ bldr_prop_* House-based static versions of vanilla items (plus walls, rails, rocks, plants packs). Tiny script adds particle+light+sound to static fireplace props, guarded by #ifndef EXPANSIONMOD.
  - T: Static prop pattern: class bldr_prop_X extends House with vanilla item model — decoration versions of dynamic items
  - T: Client-side VFX on statics: ctor CallLater(SpawnParticles, 2000); Particle.PlayOnObject(ParticleList.CAMP_NORMAL_FIRE, this) + PlaySoundSetLoop + ScriptedLightBase.CreateLight attached via AttachOnMemoryPoint(this, "light")
  - T: #ifndef EXPANSIONMOD guard to avoid class clash with Expansion's own bldr props
  - CLASSNAMES: bldr_prop_LabCoat, bldr_prop_PowerGenerator, bldr_prop_BatteryCharger, bldr_prop_CableReel, bldr_prop_BaseRadio, bldr_prop_PersonalRadio, bldr_prop_CanisterGasoline, bldr_prop_LargeGasCanister, bldr_prop_Refridgerator, bldr_prop_SeaChest, bldr_prop_WoodenCrate, bldr_prop_Spotlight, bldr_prop_GasMask, bldr_prop_Chemlight_White

## S.T.A.L.K.E.R.: Area Of Decay Building
- category: map-assets / STALKER structures | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2871588282
- desc: 6.8GB asset pack of STALKER structures for the Area Of Decay map, by AOD Dev Team (v0.7.6). Header-only analysis: contains full X18 Dark Valley lab and Backwater X12 lab building models (structures_darkvalley\x18\Darkvalley_x18lab_*.p3d, structures_backwater\Backwater_x12lab_*.p3d), sinkhole and zone structures.

## Area Of Decay Map
- category: map + STALKER mutants/NPCs | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2871587357
- desc: AOD exclusion-zone map plus the full STALKER mutant roster and trader NPC models (Sidorovich, Sakharov, Barman...). Mutants are reskinned vanilla infected/animal AI; each mutant ships a skinning config yielding a unique trophy body part (Edible_Base) and mutant steak meat. Configs carved from the 961MB dz_aod_mutants.pbo.
  - T: Mutant AI reuse: mutants extend ZmbM_HermitSkinny_Base (humanoid) or Animal_CanisLupus / Animal_UrsusArctos (quadruped) — pure config+model reskins, no new AI code
  - T: Skinning trophy drops: per-mutant skinning config adds unique Edible_Base part + reskinned SteakMeat (extends WolfSteakMeat) to butcher results
  - T: Separate tiny client_core PBO holding only CfgMods script/layout/imageset registrations while heavy assets live in data-only PBOs
  - CLASSNAMES: Mutant_AoD_Krovosos_Jaw, Mutant_AoD_Controller_Hand, Mutant_AoD_Burer_Hand, Mutant_AoD_Snork_Leg, Mutant_AoD_Pdog_Tail, Mutant_AoD_Pgigant_Hand, Mutant_AoD_RatWolf_Eye, Mutant_AoD_Poltergeist_Eye, Mutant_AoD_Izlom_Hand, Mutant_AoD_Himera_Kogot, Mutant_AoD_Forester_Kogot, Mutant_AoD_Tushkano_Head, Mutant_AoD_Tark_Horn, Mutant_AoD_Zombi_Hand, Mutant_AoD_Snork_Base

## FlipTransport
- category: vehicle QoL / config infrastructure | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1832448183
- desc: Flip/push vehicles with a hydraulic jack. Small mod with disproportionately good infrastructure: versioned JSON config classes with migration, ScriptRPC config broadcast, and live settings editors integrated into both VPPAdminTools and Community Online Tools.
  - T: Versioned JSON config: class Config_v4 : ConfigBase with Copy(same version) and Update(older version) migration; static singleton GetInstance()
  - T: Config sync: static SyncDataSend(identity) writes whole config object into ScriptRPC; client handles it in modded DayZGame.OnRPC and Copy()s into singleton
  - T: VPPAdminTools settings UI: subclass AdminHudSubMenu, CreateWidgets(layout), FindAnyWidget-bound checkboxes/editboxes, save button sends config back (guarded #ifdef VPPADMINTOOLS_NOTREADY)
  - T: COT settings UI: JMModuleConstructor registration + settings form module (guarded by COT define)
  - T: GameLabs (CFTools) integration stubs via #ifdef GAMELABS
  - CLASSNAMES: HydraulicJack, TrafficCone, TrafficCone_Prop


# Local modpack — gameplay and misc

## AgricultureCore
- category: farming framework | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3690289718
- desc: Framework by IceBlade for custom growable plants: child mods register plant types (grow time, crop count, yield) in a static registry; the mod overrides vanilla PlantBase growth-stage timers and harvest. Ships small support items (empty bags, brick base, rolling papers). Extracted sources: (session scratch, ephemeral - re-extract from DayZ\!Workshop via BankRev/ScanPbo.ps1)
  - T: Static registry: static ref map<string, ref AG_PlantRegistryEntry> + AC_RegisterPlant() called by child mods at init
  - T: Growth timers: m_FullMaturityTime from registry GrowTime (minutes or 48x scale), divided by fertility; m_StateChangeTime = maturity/(stages-2); spoil via MinutesToPlantSpoiling
  - T: JSON config via JsonFileLoader<T> in $profile: with ConfigVersion parsed from CfgMods version (ConfigGetText), auto-backup to ConfigBackup/ and regen on version mismatch, ValidateConfig() clamps
  - T: CF RPC config sync: client SendRPC(AC_CLIENTCONFIGREQUEST) in OnMissionStart, server replies Param1<ConfigClass> -> whole config object serialized in one RPC
  - T: OnStoreSave/OnStoreLoad of m_HasCrops/m_CropsCount/yield (server-side only, version-agnostic)
  - T: ActionContinuousBase with CAContinuousRepeat(UATimeSpent.MAG_LOAD) and CAContinuousTime(5) callbacks; abstract pack-load action base with per-cycle OnFinishProgressServer
  - T: Config-driven outputs: RipOpenBrick reads per-item CfgVehicles 'Resources' subclass (ConfigGetChildrenCount/ConfigGetChildName/ConfigGetInt value) to decide unbox output+count
  - T: ReplaceItemWithNewLambdaBase + ServerReplaceItemInHandsWithNew for in-hands transformation; ServerSetAmmoCount when output is a Magazine pile
  - CLASSNAMES: AC_EmptyBag, AC_BrickBase, AC_CigarettePack_Empty, AC_RollingPapers, AC_RollingPapers_Green, AC_RollingPapers_Silver

## CannabisPlus
- category: processing stations / farming content | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1932611410
- desc: IceBlade's cannabis mod built on AgricultureCore: 8 strains, deployable drying post (raw plant -> dried bud over configurable seconds) and a placeable workbench that converts buds+empty bags -> strain bags -> plastic-wrapped bricks on a repeating timer, optionally consuming battery power. Per-strain JSON configs autogenerate in $profile:. (sources were extracted to session scratch, ephemeral - re-extract from the mod PBO in DayZ\!Workshop via BankRev/ScanPbo.ps1).
  - T: Processing station: named attachment slots as roles (input buds, empty bags consumable, bagger/wrapper tools, LargeBattery energy, output bags/bricks); Timer.Run(seconds, this, "DoProcessing", NULL, true) loop
  - T: Slot locking during run: GetInventory().SetSlotLock(slotId, bool) + item.LockToParent(); unlock to CreateAttachment(output) then relock
  - T: State sync: RegisterNetSyncVariableBool(running/paused) + SetSynchDirty; client updates lock UI in OnVariablesSynchronized
  - T: Energy gating: VehicleBattery attachment, GetCompEM().GetEnergy() check and AddEnergy(-cost) per cycle; RequireBattery JSON toggle; CanDisplayAttachmentSlot hides battery slot when disabled
  - T: Timed transformation (DryPost): CallLater(FinishDrying, SecondsToDry*1000) then delete raw attachments and CreateInInventory outputs with stacking computed from ConfigGetInt varStackMax
  - T: Per-entity JSON configs: one JSON per strain in $profile:CannabisPlus/Strains/, autogenerated defaults if missing; config key migration by string replace over the JSON file
  - T: Naming-convention chains: CP_StrainHelper derives bud->bag->brick->joint classnames from string prefixes instead of explicit recipe config
  - T: RestCallback/JsonMakeData stub for posting transactions to an external HTTP API
  - CLASSNAMES: CP_Workbench, CP_Workbench_Kit, CP_DryPost, CP_DryPost_Kit, CP_Bagger, CP_PlasticWrapper, CP_PlasticRoll, CP_CannabisBud, CP_CannabisBags, CP_CannabisBrickBase, CP_RawPlantBase, CP_DriedCannabisPlant, CP_CBDCrudeOil, CP_CompostBin, CP_JointBase

## AdditionalMedicSupplies
- category: medical items + diagnostic UI | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2579252958
- desc: Medical content pack (bandages, pills, injectors, NBC pouches, gas masks GP-21/GP-7B, med chests) with modded vanilla medical actions and disease modifiers. Its blood-test action spawns an AMS_TestKitReport paper whose disease values are read in a custom UIScriptedMenu with progress bars. (sources were extracted to session scratch, ephemeral - re-extract from the mod PBO in DayZ\!Workshop via BankRev/ScanPbo.ps1).
  - T: Result-document pattern: action OnFinishProgressServer -> CreateInInventory("AMS_TestKitReport") with SpawnEntityOnGroundOnCursorDir fallback -> SetDataFromPlayer fills RegisterNetSyncVariableFloat fields + SetSynchDirty
  - T: Report persistence: OnStoreSave/OnStoreLoad of each float; SetLifetime from GetEconomyProfile().GetLifetime()
  - T: Reader UI: UIScriptedMenu subclass, CreateWidgets(custom .layout), FindAnyWidget + SimpleProgressBarWidget.SetCurrent, close on UAUIBack
  - T: Server-side feature toggle read from serverDZ.cfg: GetGame().ServerConfigGetInt("amsDisableReport")
  - T: modded vanilla actions (ActionTestBloodTarget etc.) overriding only OnFinishProgressServer — additive pattern
  - T: Disease data read via player.GetSingleAgentCountNormalized(eAgents.X)
  - CLASSNAMES: AMS_TestKitReport, AMS_FirstAidKit, AMS_MFAK, AMS_Ai2, AMS_NBCPouch, AMS_GasMaskGP21, AMS_GasMaskGP21_Filter, AMS_GasMaskGP7B, AMS_MedChest_Red, AMS_Absorbent, AMS_AntiSept, AMS_Vikasol, AMS_Methadone, AMS_Splint, AMS_Coldpack

## Only Ukraine money (GRN)
- category: currency content pack | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3003037657
- desc: Config-only item pack by TeoDor: hryvnia banknotes in three series (1992, 2014, 'new' 20-1000 GRN), coins 1/2/5/10 GRN, gold bar/nugget/egg, jewelry trinkets, wallet and suitcase containers. No scripts — pure CfgVehicles items (verified from embedded config.bins; PBO 256MB analyzed header-only). (sources were extracted to session scratch, ephemeral - re-extract from the mod PBO in DayZ\!Workshop via BankRev/ScanPbo.ps1).
  - T: Config-only mod: items defined entirely in CfgVehicles with no script module
  - CLASSNAMES: TD_Money_Base, TD_GRNnew_money100, TD_GRNnew_money200, TD_GRNnew_money500, TD_GRNnew_money1000, TD_GRN2014_money500, TD_GRN1992_money100, TD_Coin_Base, TD_GRN_Coin10, TD_Gold_Bar, TD_Gold_nugget, TD_Gold_egg, TD_Wallet, TD_Suitcase, TD_BanderoCoin

## Ear-Plugs
- category: client QoL | source: local-modpack | popularity:  | license: All Rights Reserved (per file headers, Cooltrain)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1819514788
- desc: Client-side sound muffling toggle by Cooltrain: custom keybinds lower game volume in 10 steps with a HUD icon and fading volume number. No items, no server logic. (sources were extracted to session scratch, ephemeral - re-extract from the mod PBO in DayZ\!Workshop via BankRev/ScanPbo.ps1).
  - T: Custom keybinds: CfgMods inputs="...inputs.xml" (actions + sorting category + default preset), polled with GetUApi().GetInputByName(...).LocalClick() from a repeating CallLater
  - T: Input guard: suppress binds when GetUIManager().GetMenu() != null or IsCursorVisible()
  - T: HUD widget: GetWorkspace().CreateWidgets(layout) at mission level, WidgetFadeTimer.FadeOut for transient text
  - T: HUD visibility respect: modded IngameHud/IngameHudVisibility exposing flag check (HUD_DISABLE|HUD_HIDE|MENU_OPEN)
  - T: ScriptInvoker event (OnMuteVolChange) decoupling manager from HUD
  - T: Debug mode toggled by presence of a $profile: flag file

## Mag Obfuscation
- category: UI tweak | source: local-modpack | popularity:  | license: CC BY-NC-ND 4.0 (per script header, DayZ CreatorZ Mod Team)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2449234595
- desc: Tiny script-only mod that hides exact ammo counts: modded QuantityConversions.HasItemQuantity returns a progress bar instead of a number for magazines (unless the item is a visible cartridge stack), plus matching inspect-menu/HUD tweaks. (sources were extracted to session scratch, ephemeral - re-extract from the mod PBO in DayZ\!Workshop via BankRev/ScanPbo.ps1).
  - T: modded QuantityConversions.HasItemQuantity switching QUANTITY_COUNT/QUANTITY_PROGRESS/QUANTITY_HIDDEN based on config probes (ConfigIsExisting("iconCartridge"), quantityShow, quantityBar)
  - T: Ships editor plugin scripts inside the same Scripts.pbo (build/project-manager helpers)

## Delta's Ammunition Info UI
- category: inventory info UI | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3491989895
- desc: Client UI mod adding ballistic stats (dispersion, health/shock/armor damage, velocity, BC) to the inventory tooltip and inspect window for any ammo item, computed live from CfgAmmo. Replaces the vanilla tooltip layout with an extended copy and adds an icon imageset. (sources were extracted to session scratch, ephemeral - re-extract from the mod PBO in DayZ\!Workshop via BankRev/ScanPbo.ps1).
  - T: modded ItemManager: constructor replaces m_TooltipWidget with CreateWidgets of the mod's extended copy of day_z_inventory_new_tooltip.layout
  - T: override PrepareTooltip(EntityAI item): cast to target type, Show(true/false) a WrapSpacerWidget section, fill TextWidget/ProgressBarWidget by FindAnyWidget name
  - T: Config-driven stats on any item incl. other mods' items: ConfigGetString("ammo") then GetGame().ConfigGetFloat("CfgAmmo <class> <prop>") path lookups — no hardcoded item list
  - T: Three-segment low/medium/high ProgressBarWidget coloring pattern
  - T: Custom .imageset for icons + overridden inspect/icon layouts
  - T: modded Ammunition_Base adding getter methods only (fully additive)

## Uncuepas Civilian Clothing
- category: clothing content pack | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1762444175
- desc: Large civilian clothing variety pack (caps, beanies, jackets, firefighter/civil gear, gas mask recolors, drybags) by Uncuepa, v1.26. Analyzed header + embedded config.bins only (143MB PBO); items are CfgVehicles variants of vanilla clothing classes. (sources were extracted to session scratch, ephemeral - re-extract from the mod PBO in DayZ\!Workshop via BankRev/ScanPbo.ps1).
  - T: Content pack pattern: recolors via hiddenSelectionsTextures on vanilla base classes
  - CLASSNAMES: Uncuepa_GasMask_Black, Uncuepa_GP5GasMask_Black, Uncuepa_FirefightersHelmet_PowerPlant, Uncuepa_HikingJacket_whiteblue, Uncuepa_DryBag_White

## Last Road RP Items Pack
- category: RP props / lab equipment content | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3443508153
- desc: Huge Russian RP prop pack (4 obfuscated PBOs, 1.5GB total) by Rosomahaha: laboratory gear (analyzer, blood tests, microscope, flasks, test tubes, lab preparations, mutant-part cases, anomaly/artifact scanners), documents (folders, passports, IDs, notebooks, science books incl. chemistry/physics), data carriers (floppies, USB sticks, SD cards, PDA, laptops, computer parts), chemistry craft components (acids, alkali, sulfur, coal), money/coins, food with vanilla RecipeBase crafts, and melee. Configs recovered by parsing the obfuscated PBO headers and LZSS-decompressing the Cprs config.bin entries (session scratch, ephemeral - re-extract via ScanPbo.ps1).
  - T: Obfuscated distribution: decoy header entries (*.*, \\\), garbled Cyrillic filenames, LZSS 'Cprs'-compressed config.bin — still fully recoverable via header parse + LZSS + CfgConvert
  - T: Prop registration: modded ModItemRegisterCallbacks RegisterTwoHanded/RegisterOneHanded with AddItemInHandsProfileIK reusing vanilla .asi/.anm for custom props
  - T: Item-only classes declared in bulk in one ROItems.c (class X : ItemBase {})
  - T: Vanilla RecipeBase crafting recipes for food combining (RoRpCraft_*.c in food PBO)
  - T: Simple ActionFlipMelee user action on melee items
  - CLASSNAMES: ro_rp_analizator, ro_rp_micro, ro_rp_colba, ro_rp_probir, ro_rp_analizkrovi, ro_rp_preparat_biohim, ro_rp_preparat_organic, ro_rp_mutantcasebig, ro_rp_mutantcasesmall, ro_rp_anomalscaner, ro_rp_artefactlscaner, ro_rp_disketa, ro_rp_flashka, ro_rp_papkadocumenti_secret, ro_rp_knigarp_himik


# Local modpack — weapons and clothing packs

## SNAFU Weapons
- category: weapons content pack | source: local-modpack | popularity:  | license: DayZ Public License - No Derivatives (DPL-ND) (stated in mod.cpp overview)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2443122116
- desc: Large weapon pack (~3.5GB, 11 PBOs) by the SNAFU server team: modern/WW2 rifles, shotguns, pistols, scopes. Small SNAFU_Scripts.pbo contains all gameplay scripting for weapons, lasers, and scope-mount crafting.
  - T: modded class PluginRecipesManagerBase { override RegisterRecipies() } to additively register RecipeBase recipes
  - T: RecipeBase crafting: InsertIngredient x2, m_IngredientDestroy, AddResult, m_ResultToInventory (snafu_scopes/crafting/*.c)
  - T: Invisible adapter base items: class SNAFU_AK_Mount_Base : Inventory_Base as generic mount ingredient
  - T: ActionSingleUseBase pair for toggling weapon laser on/off + ScriptedLightBase laser light source
  - T: modded ActionCraftBolts (ActionContinuousBase override of vanilla action)
  - T: Custom weapon FSM states (customfsmstates.c) and .asi animation graph registration
  - T: stringtable.csv localization inside script PBO

## Tactical Flava
- category: weapons/gear pack + gameplay scripts | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2646817942
- desc: Weapon/armor/clothing pack (~1.3GB) by Wayward Son with an unusually rich script PBO: chemical/teargas/EMP/phosphorus grenades with area-effect zones, disassemblable armor vests, bipods, magnifier optics, custom player modifiers and notifiers.
  - T: ActionContinuousBase + CAContinuousTime(2) disassemble actions converting item A -> items B+C via MiscGameplayFunctions.TurnItemIntoItemEx with custom TurnItemIntoItemLambda (OverrideNewLocation to hands)
  - T: Preserving item state across conversion: GetHealth01/GetWet/GetAgents read before, SetHealth01/SetWet/TransferAgents applied to spawned output (CreateObjectEx, ECE_PLACE_ON_SURFACE)
  - T: Configurable area-effect entity (tf_basezoneeffect.c): ~14 RegisterNetSyncVariable for radius/rings/particle ID/decay state - server-authoritative zone synced to clients
  - T: Scripted Trigger entities (chemicalgrenadeareatrigger, teargasareatrigger) with net-synced m_AreaState
  - T: Custom player modifier: enum TF_Modifiers: eModifiers { MDF_PHOSPHORUS = 616 } + modded ModifiersManager.Init() AddModifier(new PhosphorusMdfr)
  - T: Custom notifier via NotifiersManager (tf_poisonednotfr.c) for HUD status icons
  - T: Bitflag player state sync: RegisterNetSyncVariableInt("tf_Flags") on PlayerBase and ZombieBase
  - T: Custom .imageset icons; ActionChangeMagnifier up/down for switchable optic magnification

## Forward Operator Gear
- category: clothing/gear content pack | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2931560672
- desc: Large (~4.8GB) modern military clothing/helmet/vest pack. Its 1MB Scripts.pbo implements togglable NVG mounts, rotating headsets, toggleable sleeves, helmet lights and boltcutter multi-actions.
  - T: Custom action input class: ToggleNVGPositionActionInput : DefaultActionInput with SetInput("UAToggleNVGPosition") + inputs.xml keybind, using ForceActionTarget on an attachment found via FindAttachmentBySlotName - keypress triggers action on equipment not held in hands
  - T: Instant ActionBase (IsInstant()=true) with CCTNonRuined target condition
  - T: State persistence pattern (toggleable_sleeves.c): RegisterNetSyncVariableInt("m_SleevesState",0,2) in ctor, server-only setter + SetSynchDirty, OnStoreSave/OnStoreLoad, OnVariablesSynchronized -> visual update
  - T: m_Openable component reuse with RegisterNetSyncVariableBool("m_Openable.m_IsOpened") for open/close gear states
  - T: Multi-purpose tool actions on one item (fog_boltcutters: MineBush/Unrestrain/Lock/Unlock/DisarmMine/DisarmExplosive)
  - T: Custom inventory slot icons via own .imageset (fog_slots.imageset)

## SFP
- category: stalker RP content megapack | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2623995836
- desc: Huge (~5GB) Russian STALKER-flavored pack by FIDOv: mutants, mutant part slices, artifacts, JSON-configured NPCs/devices, casino terminal, collection journals, implant module system, cryo cases. Only header + bundled MOD CLASS NAMES.txt analyzed (script code is inside 1.6-1.8GB PBOs, not extracted).
  - T: Per-item JSON admin configs documented for many items (casino terminal: accepted input item + reward item + randomness; monolith shard lifetime in minutes; NPC camp spawner radius; server text display; alarm sound selection)
  - T: Item-in/item-out random-reward terminal device (SFP_Kazino_terminal_01) - single-step processing station precedent
  - T: Companion/summoner system driven by 'bait card' registration items (SFP_Summoner_Bait_*)
  - T: Modular implant/module system (SFP_Module_SystemBase + CPU/Health/Blood/Stamina/BoneFix modules)
  - T: JSON-configured custom mutants (SFP_MutaWolf_black werewolf, SFP_Tenevnik)
  - CLASSNAMES: SFP_Muta_Case_big, SFP_Muta_Case_small, SFP_kosterez_tentacles_srez, SFP_kosterez_lapa_srez, SFP_Nezdan_uho, SFP_MozgoTras_ear, SFP_tenevnik_eye, SFP_Kislot_Narost, SFP_Muhomor_Otrezok, SFP_Oskolok_monolit, SFP_Oskolok_monolit_time, SFP_Artefact_Monolit_T2, SFP_Kazino_terminal_01, SFP_Collection_Jural_90, SFP_Collection_folder

## SFP_anim_system
- category: animation plugin | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2952842170
- desc: Optional companion plugin that activates SFP's custom emote animations by overriding the player animation graph (player_main.pbo) and registering ~70 emotes. Readme explicitly warns it conflicts with any other mod that overrides player animations.
  - T: modded class EmoteConstructor { override RegisterEmotes(TTypenameArray emotes) } + emote classes for ~70 custom emotes
  - T: modded gesturesmenu.c (5_mission GUI) to surface custom emotes in the gesture wheel
  - T: Player animGraph override shipped as separate optional mod because only one player_main override can exist per server (documented conflict)
  - T: Emote/animation constants extension in 3_game/constants.c

## Advanced Weapon Scopes
- category: weapon attachments | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2143128974
- desc: Scope pack (XPS3 + G33 flip magnifier, SpecterDR, RMR, ACOG, NFATACR, LeupoldMk4, OKP7, PVS4, Pilad, MRS) in one 250MB PBO. Structure verified from the PBO filename table only (not extracted): per-scope Classes, mount/dismount Actions, and Recipes.
  - T: Recipe-based combining of optic + mount (Scripts/4_World/Recipes/MountScopeRMR.c, MountG33ToXPS.c, MountScopeB13.c, MountScopeMosinMount.c + RegisterRecipies.c)
  - T: Dismount user actions to reverse recipe-made combos (ACOGRMR_Dismount.c, B13Mount_Dismount.c, XPS34HHS_Dismount.c, MosinMount_Dismount.c)
  - T: Action registration on player via RegisterActions_Playerbase.c
  - T: modded ActionToggleNVMode for NV-capable optics (moddedActionToggleNVMode.c)

## TOP Guns
- category: weapons content pack | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3359711406
- desc: Russian weapon pack (single 802MB ved.pbo, no script PBO): 185 TG_-prefixed classnames (AK family, SVT-40, AVS-36, KSVK, RSh-12, MGL, GP-25, hunting rifles). Ships TG_classname.txt, TG_types.txt and TG_cfgspawnabletypes.txt for admins.
  - T: Ships admin economy files in mod root: classname list, types.xml and cfgspawnabletypes.xml fragments

## Cold War Uniforms: Project Iron
- category: clothing content pack | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1999666859
- desc: Small (17MB) Cold War era uniform pack, single Cold_War.pbo, no script PBO and no docs in the mod root. Header-level entry only.

## Dutch Army Clothing
- category: clothing content pack | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3097629884
- desc: 50MB Dutch military clothing pack, single PBO, no script PBO or docs. Header-level entry only.

## MaharlikaPH_MilitaryPack
- category: vehicles (drivable armor) | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3529074158
- desc: Drivable BMP-3 and M1A2 Abrams with a compact Van_TankScript.pbo implementing turret control, gunner UI, shell entity and object destruction. Actively maintained (Changes.txt documents 1.29 fixes).
  - T: JSON config manager with versioning (Van_TankScript/scripts/3_game/van_tankscript/configmanager.c): FileExist/MakeDirectory for $profile: subdirs, JsonFileLoader<T>.JsonLoadFile/JsonSaveFile, CONFIG_VERSION string compared to stored Tank_ConfigVersion, DeleteFile + regenerate defaults on mismatch, #ifdef SERVER guards
  - T: Nested config classes per device (Van_TankConfigData per vehicle) + string-array matching config (DestructibleTypes classname substrings, P3D path substrings)
  - T: RegisterNetSyncVariableObject("m_Tank") on shell entity - net-syncing entity references, lesser-known API
  - T: RegisterNetSyncVariableFloat/Int for turret yaw/pitch phases and operator seat; OnStoreSave override on vehicle
  - T: Custom in-vehicle UI: .layout file + edds crosshair (tankgunnerui.c) and inputs.xml keybinds
  - T: modded ActionGetInTransport/GetOutTransport to assign crew roles; modded DayZPlayerCameras for vehicle camera
  - T: Client-side per-frame control moved from OnUpdate to EOnFrame for smoothness (per changelog)

## WindstridesClothingPack
- category: clothing content pack | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1797720064
- desc: 852MB civilian/military clothing pack by Windstride, single WindstrideClothing.pbo, no script PBO. Header-level entry only.


# Steam Workshop — frameworks, admin, economy

## Community Online Tools (COT)
- category: admin-framework | source: steam-workshop | popularity: 2,230,424 subscribers, 8,927 favorites (Steam API, 2026-07-31); last update 2026-07-15 | license: CC-BY-SA-4.0 (GitHub Jacob-Mango/DayZ-CommunityOnlineTools)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1564026768
- desc: GUI-based admin tool framework by the Community Offline Mode developers (Jacob_Mango, Arkensor, LieutenantMaster, cbw-lava). Modular design lets other mods (e.g. Expansion) register their own admin menus; features include teleport, player stats editing, weather, item/vehicle spawning, ESP, loadouts, Discord webhooks. Requires CF.
  - T: Module architecture verified in repo tree: JMModuleBase subclasses instantiated via JMModuleConstructor and orchestrated by JMModuleManager (JM/COT/Scripts/4_World/CommunityOnlineTools/Classes/Module/)
  - T: Per-module admin permission tree: JMPermission / JMPermissionManager / JMPermissionType — roles with granular permission nodes, checked server-side per RPC
  - T: Central RPC enum registry in 3_Game (JM/COT/Scripts/3_Game/CommunityOnlineTools/RPC.c) to avoid RPC ID collisions
  - T: Clean layer split: module logic in 4_World, per-module UI forms in 5_Mission/CommunityOnlineTools/modules/ (Camera, ESP, ItemStats, Loadout, Object, Player, Teleport, Vehicles, Weather, Webhook, Example)
  - T: Discord webhook subsystem (JMWebhook* classes) for server-side audit logging of admin actions
  - T: Ships an Example module as a template for third-party COT extensions

## ZomBerry Admin Tools
- category: admin-framework | source: steam-workshop | popularity: 869,599 subscribers, 4,965 favorites (Steam API, 2026-07-31); last update 2022-10-19 | license: GPL-3.0 (stated on workshop page and in Moondarker/ZomBerry-DayZAdminTools repo)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1582756848
- desc: Lightweight client/server admin GUI by Vaker (GitHub: Moondarker), designed to be minimal for users and developers. Item spawn menu with classname display and filters, god mode, heal, vehicle repair/refuel, freecam; admins listed in admins.cfg by Steam64ID. Requires CF.
  - T: Tiny extensible command API: AddCategory(name, hexColorARGB) + AddCommand(displayName, functionName, classInstance, category, needsTarget, paramArray) — commands execute with admin ID, target ID, cursor position and param values
  - T: Server-authoritative command list synced to client GUI (client renders only what server registered)
  - T: Flat-file admin whitelist (admins.cfg with Steam64IDs in server profile) — simplest possible auth pattern
  - T: Repack permitted if unobfuscated with links back — GPL-compatible distribution stance

## LBmaster Advanced Groups
- category: groups/party | source: other | popularity: no public workshop page; paid distribution (150 EUR permanent per server, 30 EUR 2-week test, +50 EUR per extra instance) | license: proprietary, paid per-server license; only the LBmaster Steam account may publish it
- url: https://lbmaster.de/product.php?id=4
- desc: Paid closed-source group/party system: clans with names/tags, rank hierarchies, subgroups, group leveling, group+global chat, LBmaster map with group/private markers, GPS/compass/minimap, 3D member indicators, admin menu with its own permission system. Sold via lbmaster.de, not on Steam Workshop (stable v5.4.1 at time of research).
  - T: JSON config files in profile (Admins.json with permission groups — an admin can be in multiple groups; Permissions.json gating e.g. group-ATM withdraw/deposit per rank)
  - T: Ecosystem integration: its Killfeed/Banking/Garage/Airdrop mods query group membership internally — group-as-faction prior art
  - T: Rank-gated actions (flag pole DLC restricts building radius to group members by rank) — same gating shape as faction-gated research devices
  - T: Third-party external API tool (FlorianSW/lbmaster-advanced-groups-api) manages the mod's JSON configs from outside the server process — keeping admin state in JSON files enables external web editors
  - T: Webhooks for event-based integrations; monetization hooks via Tip4Serv

## BaseBuildingPlus (BBP)
- category: base-building/crafting | source: steam-workshop | popularity: 2,822,350 subscribers, 8,412 favorites (Steam API, 2026-07-31); last update 2025-03-22 | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1710977250
- desc: The most-subscribed community base building mod: tiered (T1-T3) buildable parts crafted from kits, a workbench station, snapping placement with hologram, land claim flags, safes/vaults/wells/solar panels. Config-only GitHub repo (FelixForesight2020/BaseBuildingPlus) with wiki; scripts ship packed in the PBO.
  - T: Workbench = slot-based processing station: 7 dedicated attachment slots (melee weapon/tool, blueprint, logs, planks, metal sheets, concrete, nails); player approaches with blueprint in hands to trigger craft — direct UX prior art for a science station
  - T: Recipe = material-count matrix per output kit (e.g. Door Frame: 2 logs + 10 planks + 15 nails) consumed from station slots
  - T: JSON server config in profile: BBP_Settings.json with hologram/placement flags (BBP_FloatingPlacement, BBP_CanRaiseAndLowerHologram, BBP_HologramHasCollision, BBP_InventoryToggle) and BaseBuilding.json (e.g. CodelockAttachMode)
  - T: Kit-then-build placement flow: craft a *Kit item, deploy via hologram preview, then feed materials to finish — pattern for deployable portable devices
  - T: Gameplay-relevant powered prop (BBP_Solar) — precedent for an energy-flavored device without full vanilla energy manager wiring
  - T: Ships mergeable types.xml (143 classnames) in the GitHub Config folder for CE economy registration
  - CLASSNAMES: BBP_Workbench, BBP_Blueprint, BBP_Solar, BBP_Solar_PanelKit, BBP_T1_WallKit, BBP_T2_WallKit, BBP_T3_WallKit, BBP_Land_Claim_FlagKit

## TraderPlus
- category: economy/trader | source: steam-workshop | popularity: 1,073,973 subscribers, 2,962 favorites (Steam API, 2026-07-31); last update 2025-06-19 | license: source-available with custom terms: free to view/modify/PR, but Workshop publication only under the original author's Steam account
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2458896948
- desc: Standalone trader mod (successor to Dr. Jones lineage, credits him): dynamic stock with stock-driven pricing, item-condition-aware value, multi-currency, trade licenses, banking with credit cards and transfer fees, vehicle garage in an external database, CarLock, insurance, safezones with personal stashes. V1 source now community-maintained on GitHub (TheDmitri/TraderPlusV1).
  - T: JSON config suite in server profile (serverProfileFiles/TraderPlus/, incl. TraderPlusPriceConfig.json) + external Excel tool for bulk price editing — admin-tooling-first mindset
  - T: Trade licenses bought at trader or assigned to players to gate which trades a player may perform — closest existing analog to research-unlock gating
  - T: Dynamic stock/price coupling (stock level modulates price and availability) — reusable idea for diminishing returns on repeated processing of the same input
  - T: Multi-currency support where currency is configurable items
  - T: Vehicle persistence in an external database separate from the CE economy for performance
  - T: Item-condition (pristine/damaged) modifies trade value — precedent for condition-scaled research yield

## TraderX
- category: economy/trader | source: steam-workshop | popularity: 8,952 subscribers (Steam API, 2026-07-31); created 2025-09-01, last update 2026-02-07 | license: open-source custom terms (GitHub TheDmitri/TraderX): modification and server use allowed, attribution required, derivatives must stay open source; repacking as a separate mod and commercial resale prohibited
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3560310973
- desc: Clean-rewrite successor to TraderPlus by TheDmitri (early access, experimental): multi-item cart trading, precise quantity rules (buy/sell full/empty/no-matter), weapons with attachments, vehicles with parking positions, presets and favorites, and a browser-based config tool that auto-converts ~90% of TraderPlus V1 configs. Runs side-by-side with V1 via separate IDs/configs/save paths.
  - T: Domain-Driven Design in EnforceScript: domain layer (product/transaction/currency/NPC models + value objects), application layer (services, controllers, UI components), mission layer for server event wiring
  - T: Repository pattern for data access and an event system for decoupling components — rare, modern architecture example in DayZ scripting
  - T: JSON config split into folders per concern (categories/, products/, NPCs/) in the server profile instead of one monolith
  - T: Companion web app (traderpluseditor.dmitri-dev-area.dev) generates/validates the JSON config and migrates old formats — live proof of the web-editor-for-profile-JSON workflow
  - T: Side-by-side coexistence discipline: separate mod IDs, folders, configs, save paths to run next to its predecessor
  - T: Dynamic stock-based pricing, multi-currency, transaction logging

## Trader (Dr. Jones)
- category: economy/trader | source: steam-workshop | popularity: 2,353,558 subscribers, 9,602 favorites (Steam API, 2026-07-31); last update 2024-10-16 | license: proprietary — workshop description states reupload is not allowed in modified or unmodified form
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1590841260
- desc: The original AI trader mod (since 2018): trader cities and safezones with configurable NPC traders, buy/sell menus, and a physical ruble currency in denominations that only enters the economy when players sell items (unless the admin adds it to loot). Configured via plain-text TraderConfig.txt and TraderObjects.txt in the server profile. Now officially maintained again (per the archived rework repo).
  - T: Plain-text config (TraderConfig.txt: categories, items, buy/sell prices; TraderObjects.txt: trader NPC/object placements) — the pre-JSON generation of admin config, shows why JSON+validation is the upgrade
  - T: Object Entry mechanism attaches the trading user-action menu to arbitrary non-character objects (vending machines, cashboxes) — same trick as an 'any prop becomes a device' design
  - T: Physical denomination currency items with sink/source control (currency spawns only via selling, not loot, unless admin opts in)
  - T: Safezone implementation blocking hostile actions
  - T: Community fix history (PBO-Tools rework, GPLv3, archived 2026-05-05) fixed NULL pointers and unsafe down-casting — cautionary examples of EnforceScript pitfalls
  - CLASSNAMES: MoneyRuble1, MoneyRuble5, MoneyRuble10, MoneyRuble25, MoneyRuble50, MoneyRuble100

## CF (Community Framework)
- category: framework | source: steam-workshop | popularity: 4,859,576 subscribers, 19,557 favorites (Steam API, 2026-07-31); last update 2026-02-19 | license: LGPL-2.1 (GitHub Jacob-Mango/DayZ-CommunityFramework); workshop page prohibits repacking and permits server monetization under BI rules
- url: https://steamcommunity.com/workshop/filedetails/?id=1559212036
- desc: The de-facto standard DayZ scripting framework by Jacob_Mango, Arkensor, CBW-Lava: solves conflicting RPC type IDs across mods and provides RPC manager, networked variables/streams, lifecycle events, ModStorage persistence, logging, file I/O, localization, input bindings, ExpressionVM.
  - T: 15 feature modules: RPC/networking, networked variables, stream management, event handlers, lifecycle events, logging, file I/O, cryptography, date/time, type converters, localization, input bindings, ExpressionVM, surfaces, weapon systems
  - T: GetRPCManager() namespaced RPC registration eliminating cross-mod RPC ID collisions (its stated core purpose)
  - T: CF_ModStorage versioned per-mod item persistence (storageVersion in CfgMods)
  - T: Workshop dependency with active maintenance through 2026 — safe to hard-depend on


# Steam Workshop — progression, RPG, skill trees

## TerjeMods suite (TerjeCore + TerjeSkills / TerjeMedicine / TerjeRadiation / TerjeStartScreen)
- category: skills/RPG framework | source: github | popularity: Terje-Skills workshop repost: 81,278 subscribers / 126,389 lifetime (Steam API), 174 ratings; GitHub 45 stars / 66 forks | license: CC BY-NC 4.0 (attribution required, non-commercial; donations allowed)
- url: https://github.com/TerjeBruoygard/TerjeMods
- desc: Full source of Terje Bruoygard's mod suite: TerjeCore provides RPC, settings sync, per-player profile database and a skills registry; TerjeSkills adds 9 skills with 100+ perks (Immunity, Medicine, Metabolism, Athletics, Strength, Stealth, Survival, Hunting, Fishing) that persist through death. Community-maintained workshop repost 'Terje-Skills' by Streatman is the live version; admin config is TerjeSettings/Skills.cfg in the server profile folder.
  - T: Singleton registry: GetTerjeSkillsRegistry() -> TerjeSkillsRegistry with map<string, TerjeSkillCfg>; TerjeSkillCfg holds id, icon, perkPointsPerLevel, expLoseOnDeath, array<int> levels (exp thresholds), perks map, modifiers map (verified in TerjeCore/Scripts/4_World/Classes/Skills/TerjeSkillsRegistry.c)
  - T: Accessor pattern: modded class TerjePlayerSkillsAccessor with GetSkillLevel(skillId), GetSkillExperience, SetSkillLevel, AddSkillExperience(skillId, value, affectModifiers, showNotification), backed by player.GetTerjeProfile() (TerjePlayerProfile.c per-player persistence, PluginTerjeDatabase.c)
  - T: Own RPC/sync layer in TerjeCore: TerjeRpcManager.c, TerjeStreamRpc.c, TerjeSettingSynch.c (server settings pushed to client), PluginTerjeSettings.c reads TerjeSettings/*.cfg
  - T: XP gain zone modifiers: PluginTerjeScriptableAreas.TryCalculateTerjeEffectValue(player, "exp", skillId, mod) plus global TerjeSettingsCollection.SKILLS_EXPERIENCE_GAIN_MODIFIER / _LOSE_MODIFIER settings, TerjeExperienceModScriptableArea entity
  - T: Book-based XP: ActionTerjeReadSkillbook.c + TerjeBookBase.c item base class, tiered books per skill shipped with an External/Types/Types.xml for the central economy
  - T: Skill UI = native widgets inside the inventory screen: modded 5_Mission/RightArea.c + TerjeSkillsContainer.c / TerjeSkillWidget.c, dynamic instantiation of per-element layouts (TerjeSkillLayout.layout, TerjePerkLayout.layout, TerjePerkTooltip.layout, TerjeBlankLayout.layout)
  - T: XP hooks are modded vanilla classes: ActionSkinning, ActionFishingNew, CAContinuousMineWood, Cooking, recipes (CraftFishingSkill.c), melee/stamina handlers — a catalog of where to intercept player activity
  - T: Admin config: Skills.cfg with per-perk enable/disable, points per level, exp lost on death, level caps, perk stage costs (up to 10 stages)
  - CLASSNAMES: TerjeBookAthletic1, TerjeBookAthletic5, TerjeBookFishing1, TerjeBookHunting1, TerjeBookMetabolism1, TerjeBookStealth1, TerjeBookStrength1, TerjeBookSurvival1

## Syberia Project
- category: RPG overhaul (skills/perks, medicine, sleep, PDA) | source: github | popularity: Syberia - Beta test: 149 subscribers (Steam API); main workshop item delisted | license: Custom Terje license: attribution + links to Discord/GitHub required, derivative mods must be public GitHub forks, paid distribution prohibited, DayZ server monetization allowed (verified from fork's LICENSE)
- url: https://github.com/xxGrinkorxx/SyberiaScripts
- desc: Terje Bruoygard's earlier full RPG overhaul: skills and perks that survive death, advanced medicine (10+ diseases, 100+ meds), sleep system, realistic metabolism, and a PDA with GPS. The original workshop item is delisted (id 2569522069 now errors); only a 'Syberia - Beta test' shell (149 subs) and community forks/continuations remain (xxGrinkorxx/SyberiaScripts + SyberiaServer, archived macmakesxyz/SyberiaModular).
  - T: Split client/server repos (SyberiaScripts + SyberiaServer) — server-side logic kept out of the client mod
  - T: Skill tree UI built on full-screen image assets: src/data/gui/Skills/background.paa, plus gui subfolders Badges, Homebook, MindState, Sleeping (paa-based screens rather than imageset-composed widgets)
  - T: PDA device with GPS as in-game info hub (src/data/pda/ models/textures) — precedent for a faction 'science PDA' UI shell
  - T: Build workflow documented in README: copy src to P:\SyberiaScripts, pack with Mikero tools or AddonBuilder

## Zen's Skill Perk Tree (ZenSkills)
- category: skill tree / perks | source: steam-workshop | popularity: 57,654 subscribers / 97,223 lifetime (Steam API), 675 ratings | license: No formal license file; workshop description explicitly allows repacking and any modification ('free and open source'), source at github.com/ZenarchistCode/ZenSkills
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3601119520
- desc: Lightweight skill perk tree (4 skills per icon sets: crafting, gathering, hunting, survival) deliberately avoiding PvP impact; inspired by Terje's mods. EXP from normal play with an anti-AFK auto-nerf; perk rewards, EXP rates and boosts fully configurable in JSON; player data kept in its own database separate from vanilla storage. Requires Zens Core Mod (123,511 subs).
  - T: Config classes in 3_Game/ZenSkills/config: ZenSkillsConfig.c (server JSON), ZenSkillsClientConfig.c (client-side settings), ZenSkillsEXP.c, ZenSkillsPlayerDB.c (per-player persistence), ZenSkillsHighscoresDB.c (server-wide leaderboard)
  - T: Data definitions split into constants (ZenSkillConstants.c, ZenPerks.c) and defs (ZenSkill.c, ZenPerk.c, ZenExpNerfDef.c) — clean schema for tree nodes
  - T: Anti-grind: EXP auto-nerf when player lingers in one area (ZenExpNerfDef.c) — relevant to anti-abuse for research-point farming
  - T: Full-screen menu on rebindable key 'U': data/gui/layouts/zen_skills_menu.layout (47 KB) + hud_left/hud_right/tutorial/highscores layouts; UI sounds (perkunlock.ogg, perknotify.ogg) for feedback
  - T: Rank icons as PNG+PAA sets per skill/level plus composite atlases — imageset-style art pipeline for tree icons
  - T: Optional types.xml ships consumable progression items (EXP boost injector, perk-reset injector, skill books — textures injector_boost_co.paa, injector_perk_reset_co.paa)
  - T: Separate-database persistence advertised as wipe-safe: does not touch vanilla storage versioning

## Namalsk Survival
- category: survival overhaul / item-based progression | source: steam-workshop | popularity: 1,289,422 subscribers / 2,415,172 lifetime (Steam API), 3,602 ratings | license: Proprietary; monetization explicitly forbidden (even queue slots), reuploads DMCA'd; server files public at github.com/SumrakDZN/Namalsk-Server
- url: https://steamcommunity.com/workshop/filedetails/?id=2289461232
- desc: Sumrak's (DayZ Creative Lead) survival pack for Namalsk: EVR storms and blizzard event system, frostbite, a 'cold resistance' soft skill that grows over lifetime, custom clothing, curated loot scarcity. Progression is item-driven: players collect a blueprint plus scattered modules to assemble the LEHS environmental suit and activate A2 lore devices.
  - T: Soft-skill progression bound to character lifetime (cold resistance grows the longer you survive) — no UI tree, stat-based
  - T: Item-as-progression-token design: blueprint item + module items gate access to end-game gear/areas instead of XP
  - T: Dynamic event manager (EVR storms, blizzards) with staged screen effects and knockout — overhauled by InclementDab (Dabs Framework author)
  - T: Lore documents as collectible items driving player-driven 'research' gameplay
  - T: Hardcore vs Regular economy presets shipped as two mission folders with separate types.xml — pattern for difficulty-tiered configs
  - CLASSNAMES: dzn_detector, dzn_blueprint_lehs, dzn_module_card, dzn_module_lantia, dzn_module_surge, dzn_module_ext, dzn_module_ext2, dzn_athena_planning, dzn_athena_action1, dzn_athena_action2, dzn_athena3_exp1, dzn_athena3_exp2, dzn_lehs, dzn_lehs_o2tank, dzn_lehs_battery

## HGC Leveling System
- category: XP/leveling | source: steam-workshop | popularity: 50,781 subscribers / 109,422 lifetime (Steam API) | license: Proprietary: repacking/modifying not allowed, overrides allowed (per description)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3446823299
- desc: CoD-style global leveling: XP from kills, fishing, crafting, cooking and survival time; levels 1-70 with 10 prestiges and US Army rank names. UI opens on 'J'; HUD elements toggleable; rewards granted on level-up/prestige via a rewards folder. Compat PBOs exist for Expansion Market (level-locked vendors), Expansion Quests, BaseBuildingPlus and others.
  - T: Two-JSON admin config auto-generated in profiles on first start: LevelingSystem.json (HUD toggles, survival-time XP, level-up sound presets) + TriggersConfig.json (per-action-category XP amounts)
  - T: Server-only PBO (LevelingSystem_Server.pbo) shipped inside the workshop mod folder for admin to copy to server addons — hides server logic, slims the client
  - T: Reward-items-per-level via a rewards folder admins populate freely
  - T: Level/prestige gating of Expansion Market traders via compat PBO — precedent for gating vendors/actions by progression tier
  - T: Compatibility distributed as optional server-side PBOs through Discord rather than separate workshop items

## Perks & Skills System [Updated] (RELIFE)
- category: perks/unlock gating | source: steam-workshop | popularity: 641 subscribers / 3,446 lifetime (Steam API), last updated 2023 | license: Proprietary: copyright VirusomanVS, repacking not allowed
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2930657404
- desc: VirusomanVS's character progression mod (~80-100 perks): activating a skill unlocks otherwise blocked actions and crafts. Admins define in JSON which crafts/actions grant XP; perks and settings are also JSON-stored, and the author describes the system as easy to extend with new skills. Requires a private server-side part.
  - T: Unlock-gating: actions/crafts are blocked until the corresponding perk is activated — the closest existing DayZ implementation of tech-tree-style unlocks
  - T: JSON-defined XP sources: admins list craft recipes and actions that award experience
  - T: Perks themselves stored/configured as JSON, stringtable-based localization for any language
  - T: Client mod useless without closed server part — copy-protection pattern for paid mods

## GHOST's dev: Skills System (+ Quest system)
- category: skills + quests (closed/commercial) | source: steam-workshop | popularity: Skills System: 161 subscribers / 878 lifetime, 275 favorites (Steam API); Quest system: 63 ratings | license: Proprietary/private (server part distributed personally by author)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3264848296
- desc: Russian mod family: Skills System with 7 skill branches (dexterity, athleticism, medicine, engineering, gunsmith, fishing, hunting) whose sub-skills gate crafting of ammo/medicine; optional skill retention on death. The companion Quest system adds NPC dialogues, reputation gating, and 14 quest types including crafting, 'research' (visit locations), cache hunts, and locksmith-table build/repair/disassembly tasks with JSON-configured limits. Server part is private — obtained via the author's Discord.
  - T: Skill branches gate crafting recipes (ammo components, medicine) — profession-style specialization suited to faction RP
  - T: Reputation currency that rises/falls on quest complete/abandon and gates quest availability — direct analog for faction-gated research
  - T: Quest objective types 'Crafting a specific item' and 'Research (visiting places)' with JSON-set active-quest limits
  - T: Workstation-bound gameplay: Locksmith Table mod required for object creation/repair/disassembly quests — station-centric processing, like a device-based research framework


# Steam Workshop — STALKER-themed mods

## Stalker Anomaly and Detectors Free by Ibourbon
- category: anomaly/artifact/detector framework | source: steam-workshop | popularity: 83 ratings; comments active as of July 2026 | license: Free/open — author grants blanket permission for any use, matching the original 2021 mod's terms
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3716633669
- desc: Actively maintained (updated 2026-06-15, works on DayZ 1.29) free continuation of the 2021 'Stalker Anomaly & Detectors Free'. Adds 7 anomaly types (Jarka, Par, Electra, Kisel, Tramplin, Voronka, Teleport) and 4 detectors (Otklik, Bear, Velas, Svarog); artifacts are arbitrary classnames bound to detectors via server config — author explicitly states any item from any mod works ('впиши хоть фонарик — будет артефакт'). Split architecture: client PBOs (StalkerAnomaly, StalkerDetectors) + server-only PBOs (SA_ServerSide_Anomaly/_Detectors/_ArtefactProperties/_AutoReload).
  - T: client/server PBO split: spawn logic in -servermod only, players don't need server PBOs
  - T: JSON dynamic zones with geometry validation (no spawn in textures/underground/air)
  - T: per-detector whitelist of detectable artifact classnames + radius
  - T: damage configured separately for HP/Shock/Blood per target type (player/infected/animal/item)
  - T: proximity activation: anomalies only active near players for performance
  - T: settings hot-reload without server restart (dedicated AutoReload server PBO)
  - T: optional VPP Admin Tools integration
  - T: static spawns at coordinates + dynamic scatter zones in one config

## Stalker Anomaly & Detectors Free (2021 original)
- category: anomaly/artifact/detector framework | source: steam-workshop | popularity: 26,913 subscribers, 627 favorites, 4 stars (244 ratings), 94.663 MB, 42 dependent mods, 457 collections | license: Declared free: 'You can download and upload whatever you want. You do not need to pay or ask for permission.'
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2651714408
- desc: The original free repack of the STALKER anomaly+detector system by 'STALKER: THE ZONE', posted Nov 2021, last updated Jun 2022; now broken on current DayZ and superseded by the Ibourbon version. Server customization via an 'AnomalySpawner' folder dropped into the server profile; loaded with -mod=@Anomaly-Detectors-FREE plus a servermod part. Page carries a long anti-DMCA manifesto (this repack sparked the paid-mod drama around naiv4ik/nIkRon's private versions).
  - T: profile-folder config directory (AnomalySpawner) read by servermod
  - T: -mod client part + -servermod server part split
  - T: bilingual (RU/EN) setup instructions shipped inside the mod

## Detector System
- category: detector device / artifact spawner | source: steam-workshop | popularity: 49 ratings | license: Private mod — usage only by arrangement with the author via Discord
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2942776989
- desc: naiv4ik11's private mod (access via Discord) implementing 5 STALKER detectors: Response (front cone, configurable angle), Branch (omni, with lamp indicator that lights when facing anomalies), Bear (omni, radar shows direction to nearest artifact), Veles/Svarog (180° front, artifacts as radar points). Detectors require an inserted battery to switch on; each has its own config listing detectable artifact classnames and action radius. Includes an artifact spawner with presets: weighted random choice among N artifacts, latent 'in-anomaly' spawn revealed only on detection, respawn chance with min/max timers and max-respawn cap, and optional physics 'throw out of anomaly' on detection.
  - T: battery-insert energy gating for device activation
  - T: tiered device capability (cone/omni/radar/direction UI) as progression
  - T: artifact presets: multiple outputs with per-item spawn chance, random weighted pick
  - T: latent spawn: item exists logically but spawns physically only when detected by qualifying device
  - T: respawn control: chance + random min/max delay + max successful respawns cap

## Anomaly System
- category: anomaly framework | source: steam-workshop | popularity: 261 subscribers, 65 ratings (3-star), 9.165 MB, last updated Mar 2023; page removed from public listing | license: Private mod — contact author on Discord (nIkRon)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2942785384
- desc: naiv4ik11's companion anomaly mod (requires his Detector System), now removed from public Workshop (guidelines violation) and distributed privately via Discord. Eight anomaly types (Burner, Electro, FruitPunch, Springboard, Steam, Teleport, Vortex, BurntFuzz) with per-anomaly damage configs; Burner/Steam affect player temperature and wetness; Teleport can require a specific key item to activate; spawner supports static (name+position) and dynamic (zone center+radius+counts, random placement) modes.
  - T: per-anomaly damage/behavior config
  - T: environment stat manipulation (temperature/wetness) as effect type
  - T: item-gated interaction (anomaly usable only with specific item)
  - T: static + dynamic zone spawner modes

## AnomalySpawner
- category: server-side spawner (generic) | source: steam-workshop | popularity: 'Not enough ratings'; comments 2022–2023 | license: Repack/modification allowed with attribution to 123new
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2768700423
- desc: Pure server-side mod by 123new (uploader HaЧaJl'HиkA) that spawns admin-specified anomalies and artifacts in zones — contains NO content itself; any in-game object classname from any mod can be an anomaly, artifact, or 'detector' (any object held in hands). JSON config (Config_AnomalySpawner.json) controls dynamic artifact spawn distance around a detector-holding player, find counts per anomaly point, per-player search timeout, max artifacts per point, and detection chance %; config is auto-recreated on parse errors with details in scripts.log. Tested compatible DayZ 1.07–1.25.
  - T: contentless framework: all content referenced by admin-config classnames from other mods
  - T: server-only mod, zero client footprint
  - T: JSON config auto-regeneration on errors + error tagging in scripts.log
  - T: player-proximity + held-item ('detector') conditional spawning with chance % and timeouts
  - T: known flaw to avoid: artifact duplication across restarts because old-object cleanup was skipped

## RadZone (+ RadZoneNukeMissions)
- category: radiation zone / device system | source: steam-workshop | popularity: 362 ratings; addon RadZoneNukeMissions separate (id 2382269420) | license: No repack, no rework/reuse without consent
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2294323732
- desc: Radiation-zone mod by Dmitri where players AND individual items (clothes, weapons, tools) accumulate radiation points; NBC suit + filtered gas mask required in zones (NWAF/NEAF by default). Ships a device family — geigercounter (environment), geigercounter_Opal (own/target blood), geigercounter_Sova (items and target's body), geigercounter_Osa (environment+blood) — plus radiation blocker, radcure, decontamination spray, and a decontamination shower existing as static map object or player-placeable kit (packed with a wrench, consumes a gasoline canister of water). RadiationCase loot crates spawn hourly per zone with chance-based loot sets; RadZoneNukeMissions addon adds plane-dropped nuke events.
  - T: per-item persistent stat (radiation points) on clothes/weapons/tools
  - T: device family where each variant reads a different scope (environment/blood/items/target)
  - T: placeable device kit: deploy, consume liquid resource, toggle with particles+sound, repack with wrench
  - T: HUD icon with two live numbers (blood level / ambient level) next to vanilla badges
  - T: JSON config folder in serverprofile (RZConfig) + explanation.txt + own log folder (RZLogs)
  - T: chance/probability loot tables per zone per crate
  - T: types.xml integration requirement documented for admins
  - CLASSNAMES: geigercounter, geigercounter_Opal, geigercounter_Sova, geigercounter_Osa, RadiationCase

## STALKER EVR Anomaly Storm DayZone (+ Chernarus variant)
- category: emission/blowout event | source: steam-workshop | popularity: 'Not enough ratings'; 18 comments | license: Copyright Paul Harker; repack by request via Discord
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2814272032
- desc: Harkness (Harks Modding, Paul Harker) port of the Namalsk EVR blow-out emission to the DayZone map (variant 2801069395 for Chernarus). Requires Namalsk Survival mod; the emission is registered through the vanilla event system in mission init.c: m_EventManagerServer.OnInitServer(true, minTime, maxTime, maxParallel) then RegisterEvent(EVRStorm, 0.35). Survival requires the 'APSI' protection mask (must be added to types.xml/trader).
  - T: vanilla EventManager: m_EventManagerServer.OnInitServer + RegisterEvent(EVRStorm, chance) in mission init.c
  - T: cross-mod reuse: depends on Namalsk Survival for the event implementation
  - T: survival gated by wearing a specific item classname (APSI)
  - T: map-specific teleport/exit coordinates configured in mission
  - CLASSNAMES: APSI

## Harks Stalker PDA
- category: PDA UI / communication | source: steam-workshop | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2900589445
- desc: Stalker SOC-style PDA by Harkness: opens with P key; pages for Contacts (global and private text messaging), Factions (player groups + ESP markers), Map with in-game clock, Player Statistics (survival time, kills, distance, longest hit, vitals bars), and a Personal Diary. Auto-generates HarksStalkerPDA.json in the server profile/config folder on first start. Workshop page currently returns an error (item removed or hidden) — details above come from search snippets, not a live page fetch.
  - T: autogenerated JSON config in profile on first server start
  - T: multi-page handheld-device UI (contacts/factions/map/stats/diary)
  - T: faction/group page with map markers
  - T: global + private player-to-player messaging

## StalkerZ DayZone
- category: total conversion server pack | source: steam-workshop | popularity: 30 ratings | license: No repack/reupload; usable only on the StalkerZ server
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2530777436
- desc: Server-locked pack of the StalkerZ hardcore RP server (whitelisted): complex radiation system, enhanced medicals, anomalies and artifacts with side effects on the player when utilized, mutants, custom skybox/weather, PDA text communication, blowouts/emissions, traders with stock simulation, crafting, lootable stashes, player warehouses, factions, events. Credits Moroz, harry, affenb3rt, Qtong, CAMINOonPC, Koruzaki, Dogtoothcg, Mole, Tizzalicious.
  - T: artifacts as equipped items with player side effects (stat modifiers)
  - T: trader stock simulation for a scarcity economy
  - T: faction + event systems integrated in one pack

## StalkerZ Mutants
- category: mutants / AI | source: steam-workshop | popularity: 107 ratings | license: No repack/reupload; StalkerZ server only
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2485116735
- desc: Custom-modeled reinterpretations of STALKER mutants by the StalkerZ team (affenb3rt, Dogtoothcg, Dr Q); part of the 'StalkerZ AoD Server Collection' (22 items). The page publishes the full entity classname list (sz_Mutant_* — BloodSucker/Flesh/PseudoGiant/PyroGeist/Snork/Burer variants). Server-locked: no repack or use outside StalkerZ.
  - T: publishing classnames on the workshop page for admin convenience
  - CLASSNAMES: sz_Mutant_BloodSucker_Marsh_H, sz_Mutant_BloodSucker_Neutral_N, sz_Mutant_BloodSucker_Dark_H, sz_Mutant_Flesh_E, sz_Mutant_Flesh_N, sz_Mutant_Flesh_Dark_N, sz_Mutant_PseudoGiant_A, sz_Mutant_PseudoGiant_N, sz_Mutant_PyroGeist_N, sz_Mutant_Snork_Infected_N, sz_Mutant_Snork_Infected_Wire_N, sz_Mutant_Snork_Infected_Crawl_N, sz_Mutant_Burer_N, sz_Mutant_Burer_F

## XZone STALKER Mutants
- category: mutants / AI | source: steam-workshop | popularity: 497 ratings; 209 comments; pinned servers-using-mod thread | license: No repack ('server pack' inclusion explicitly forbidden) — servers must reference it via Steam Collection
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2525642572
- desc: ZAKAT's pack of 15 STALKER-universe creature models (Boar, Burer, Cat, Himera, Blind-dog, Pseudo-flesh, Izlom, Krovosos, Pseudo-dog, Pseudo-giant, Poltergeist, Snork, Tushkano, two zombie types). Not updated in ~4 years; recent comments report skinning-related crash logs and missing damage zones (e.g. [Mutant_XZ_Dog_Fur] 'Could not find zone Zone_Spine_Back'). Pinned 'Types' discussion provides types.xml entries.
  - T: types.xml published via pinned workshop discussion
  - T: collection-only distribution policy instead of repack permission
  - CLASSNAMES: Mutant_XZ_Dog_Fur, Mutant_XZ_Burer1, Mutant_XZ_Krovosos1, Zombi_XZ_Civ_Trup3

## Stalker Classic Mutants
- category: mutants / AI | source: steam-workshop | popularity: 'Not enough ratings' | license: No repack/reupload; Stalker Classic server only
- url: https://steamcommunity.com/workshop/filedetails/?id=3005495117
- desc: Mutant pack of the Stalker Classic RP server using 3D content by Kyne's Peace and Dogtoothcg (permission via Stalker Survival), with some content reuploaded from The Whisper project and Area of Decay partners. Description is credits/permissions only — no classnames or configs published.

## Alteria (map)
- category: map | source: steam-workshop | popularity: 146 ratings, 20 change notes | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3296994216
- desc: STALKER-flavored map by JohnMcLane1 (DeerIsle author) — workshop page is download-only, with all documentation, server files and support routed through the shared Deerisle/Alteria Discord and the deerisle wiki. Present in the local modpack (with a KarmaKrew Alteria Balance companion mod).

## S.T.A.L.K.E.R.: Area Of Decay Map (+ Building pack)
- category: map + building assets | source: steam-workshop | popularity: 355 ratings (map) | license: CC BY-NC-ND 4.0
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2871587357
- desc: Ground-up recreation of the STALKER Exclusion Zone by an international team, aiming to eventually include canon mechanics (traders, factions, territory war, anomalies, artifacts, mutants). Licensed CC BY-NC-ND 4.0, use on servers only via mod collection, no repack/modification. The companion 'Area Of Decay Building' pack (id 2871588282, in the local modpack) distributes GSC-derived building assets that other projects (e.g. Pripyat map) use as a free 'include as dependency' asset source.
  - T: GSC assets distributed as a dependency-only building pack (legal reuse channel)

## STALKER Exclusion Zone (map)
- category: map | source: steam-workshop | popularity: 140 ratings; in a 15-item collection | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1887895259
- desc: Russian-community DayZ port of the classic-trilogy Exclusion Zone map by Playboi Carti (VK: dayz_stalk), online since ~2019-2020; description is image-based with Discord/VK links, comments indicate server-side parts are distributed off-workshop. A STALKER RP server reports its map as 'ExclusionZone', matching this map's lineage; two later forks (id 3024591513 'STALKER Exclusion zone' and id 2951293993 'STALKER-THEZONE') now return workshop errors (removed).

## Pripyat (map)
- category: map | source: steam-workshop | popularity: 172 ratings | license: DayZ Public License - No Derivatives (DPL-ND)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2929038098
- desc: 1:1-scale from-scratch recreation of the real Chernobyl Exclusion Zone by Eri Hachiman (419.43 km², 3.7M+ objects, Duga radar, Chernobyl town), free for any server; mission files ship inside the addon and on GitHub (FrenchiestFry15/PripyatMissionFiles). Being superseded by 'Pripyat Beta' (id 2938009193). Licensed DPL-ND, repack prohibited; page documents the asset-permission dispute with Area of Decay authors.
  - T: mission files published on GitHub for server owners
  - T: custom assets legally reused via dependency addons (GSC via Area of Decay Buildings)

## STALKER NEW Anomaly | Аномалии Сталкер
- category: anomaly pack (paid) | source: steam-workshop | popularity: 'Not enough ratings'; 4 comments | license: Closed/paid — key not publicly distributed
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2981643378
- desc: Pack by Sdayz with 40+ anomaly types, each configurable (sounds, radius, damage, protection) with its own server part; anomalies are placed via DayZ Editor and claimed to be FPS-neutral via optimization. No public .bisign key — effectively a paid/private mod arranged through the authors' Discord.
  - T: DayZ Editor-based object placement workflow for admins
  - T: per-anomaly config: sound, radius, damage, protection
  - T: proximity-based activation for performance

## Heavy Zone Project | STALKER RP
- category: server pack (weapons/equipment) | source: steam-workshop | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3568160222
- desc: STALKER RP server pack (id 3568160222) focused primarily on adding a large number of gun models and equipment per community request. Only researched via search snippets (page not fetched); no anomaly/artifact system details surfaced.

## WeTernal 'WT' mods (S.T.A.L.K.E.R. Anomaly System WT, PDA WT) — web search only
- category: unresolved lead (superseded) | source: other | popularity:  | license: 
- desc: A web search (workshop text search for 'WT'/'WeTernal'/'Anomaly System WT'/'PDA WT', Google/RU-web, Steam profile check) found no WeTernal-authored STALKER mods as separate Workshop items. The WT mods DO exist: they ship inside the @Radio item (3597910943 — FramerworkWT, WTHealth, PersonalChestWT, the WT mutants) and, since autumn 2026, @protocol (3737604819 — PDAWT, MutantsWT); see the @Radio entries. Other equivalents: naiv4ik11's Anomaly System + Detector System (private, partially delisted after the free-repack/DMCA feud) and Harkness's Harks PDA/EVR mods (PDA page also now removed).


# Steam Workshop — science, medical, processing stations

## CannabisPlus
- category: processing-station / growing | source: local-modpack | popularity:  | license: Repack not allowed (proprietary)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1932611410
- desc: Cannabis growing and processing mod by IceBlade/kuli_vt. Adds grow-plant chain (seeds to plant to buds), a drying post, and a powered workbench that bags buds and wraps bricks. Present in the local modpack; PBO scripts fully inspected locally.
  - T: Device = ItemBase with const attachment-slot names for input/consumable/output buffers (Wrapper, Bagger, LargeBattery, CP_PlasticRoll, AC_Empty_Bags, bud/bag/brick slots)
  - T: Server-side Timer.Run(interval, this, "DoProcessing", NULL, true) loop; pause/resume via RegisterNetSyncVariableBool + SetSynchDirty
  - T: CF RPC config sync: client sends CLIENTCONFIGREQUEST in MissionGameplay.OnMissionStart, server replies CONFIGRESPONSE targeted to sender with Param2<config, map<...>>; maps do not deserialize intact — client rebuilds them field by field
  - T: Client polls GetCPConfig() readiness via CallLater(CP_LoadConfig, 500) until RPC config arrives
  - T: Power: VehicleBattery in LargeBattery slot, energy drained per cycle from config (PowerUsed)
  - T: Consumable drain per cycle (PlasticWrapUsage percent of CP_PlasticRoll)
  - T: Deployment: Kit item IsDeployable()=true, OnPlacementComplete -> CreateObjectEx(device, ECE_PLACE_ON_SURFACE), transfer attachments to placed object, HideAllSelections + CallLater(DeleteKit,0); separate _Holo class for placement hologram
  - T: Config: master CannabisPlus.json + per-strain JSONs enumerated via FindFile(path + "*.json"); delete folder to regenerate defaults
  - CLASSNAMES: CP_Workbench, CP_Workbench_Kit, CP_DryPost, CP_DryPost_Kit, CP_Bagger, CP_PlasticWrapper, CP_PlasticRoll, CP_CannabisBagSkunk, CP_CannabisBrickSkunk, CP_JointSkunk, CP_CannabisSeedsPackSkunk, CP_Plant_CannabisSkunk, CP_Relief_Balm, CP_Pouch

## DrugsPLUS
- category: processing / economy | source: steam-workshop | popularity:  | license: Repack not allowed (proprietary)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2170927235
- desc: Companion mod to CannabisPlus (same authors): grow coca, craft CocaLeaves into bags and bricks, magic mushrooms, impairment effects. Removed from public Workshop listing for guideline violation; description verified via Skymods mirror.
  - T: Multi-step recipe chain via vanilla crafting recipes (CocaLeaves+CocaLeaves=CocaBag; 35xCocaBag+DuctTape=CocaBrick) instead of a station
  - T: Config file in profiles for grow time / crop count / seeds per pack
  - T: Admin economy guidance: keep seed packs rare or plant-growing loops destroy server economy
  - CLASSNAMES: DP_CocaLeaves, DP_CocaBag, DP_CocaBrick, DP_Vodka, DP_Tobacco, DP_Plant_Tobacco, DP_TobaccoSeeds, DP_TobaccoSeedsPack, DP_Cigarette

## AgricultureCore
- category: framework | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3690289718
- desc: Shared foundation mod for CannabisPlus/DrugsPlus/GardeningPlus. Centralizes plant behavior (spoiling, fertilizing, harvest) with its own JSON config in profiles/AgricultureCore/, plus shared packaging items.
  - T: Runtime registration API (AC_RegisterPlant) called by dependent mods in MissionServer.OnInit so storage-loaded entities are in the registry after restart
  - T: Single shared JSON config governing behaviors across all dependent mods
  - CLASSNAMES: AC_EmptyBag, AC_CigarettePack_Empty, AC_RollingPapers, AC_RollingPapers_Green, AC_RollingPapers_Silver

## AdditionalMedicSupplies
- category: medical item pack | source: local-modpack | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2579252958
- desc: Medical item pack (author affenb3r) present in the local modpack: bandages, splints, antiseptics, gas masks with filters, med chests, first-aid kits, plus a test-kit report item. Ships economy files (types.xml, spawnabletypes, mapgroupproto) in the mod folder.
  - T: Distributes ready-made CE economy files (types/spawnabletypes/mapgroupproto) alongside the PBO for admin drop-in
  - CLASSNAMES: AMS_TestKitReport, AMS_FirstAidKit, AMS_MFAK, AMS_Ai2, AMS_Polaris, AMS_Narcotics, AMS_Methadone, AMS_AntiSept, AMS_Vikasol, AMS_Splint, AMS_GasMaskGP21, AMS_GasMaskGP21_Filter, AMS_NBCPouch, AMS_MedChest_Red, AMS_Heatpack

## MunghardsItempack
- category: item pack | source: steam-workshop | popularity: 972,236 subscribers, 5 stars (1,904 ratings) | license: Repacking not allowed
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1734713776
- desc: One of the largest DayZ item packs (all original models): weapons, backpacks, clothing, masks, props, consumables. No dedicated lab/science line, but includes a brain specimen, hazmat suit, NBC longcoat, gas masks, oil lamp and similar science-adjacent props. Public GitHub repo exists (no license file, 1 commit).
  - T: gui/imagesets folder pattern for custom inventory icons
  - T: _mung classname suffix convention (verified: Zippo_mung, zenitco_pt3_buttstock_mung)
  - CLASSNAMES: Zippo_mung, zenitco_pt3_buttstock_mung

## MuchStuffPack
- category: item pack / furniture-props | source: steam-workshop | popularity: 1,964,142 subscribers, 4 stars (2,465 ratings) | license: Repack not allowed; server monetization allowed if items not individually monetized
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1991570984
- desc: Massive collection of base-building, furniture and decorative items, melee weapons and clothing by Helkhiana; requires MuchFramework. Author announced support ended with DayZ 1.26 (community fix mods exist).
  - T: Framework+content split (MuchFramework dependency)
  - T: Cautionary tale: framework mods break on game updates (STATE_PLANTED compile error after 1.28) and spawn third-party _Fix mods

## Mass's Many Item Overhaul (MMIO)
- category: item pack | source: steam-workshop | popularity:  | license: Unpack/repack/redistribute forbidden
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1566911166
- desc: Long-running vanilla-plus item overhaul: weapons, clothing, food, crafting recipes (stew from meat+vegetable boiled in pot, armbands cut from clothing, improvised raincoat bags). Later split into MassGuns/MassClothing/MassFood/MassMisc for server control. Workshop page fetch failed (Steam 429) — details from search snippets and MyDayZ mirror; stats unverified.
  - T: Component-mod split (MassGuns/MassClothing/MassFood/MassMisc) so admins pick subsets
  - T: Recipe-based transformations chained through vanilla cooking (stew mix -> boiled stew)

## MedicalAttention / MedicalAttentionUpdated
- category: medical overhaul | source: steam-workshop | popularity:  | license: Original author permitted the Updated fork
- url: https://steamcommunity.com/workshop/filedetails/?id=2265389602
- desc: Hardcore disease overhaul (original by Lucian, community-updated fork 2265389602 maintained with the original author's permission): cholera, salmonella, wound infection, cold/flu, hemolytic reactions, tiredness, drunkenness, quarantine/biohazard zones with delayed-onset staged symptoms. Workshop pages unreachable this session (Steam 429) — description from search snippets; stats unverified.
  - T: Staged affliction model: delayed onset, escalating severity, per-stage treatments
  - T: Zone-based hazard gating (quarantine zone) — analogous to faction/area gating of research devices

## TerjeMedicine
- category: medical overhaul / skills | source: steam-workshop | popularity:  | license: Custom license agreement on GitHub (TerjeMods repo is CC BY-NC per project research notes)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3649957536
- desc: Comprehensive realistic medicine mod by Terje Bruoygard (203 MB, requires TerjeCore): 25+ pills, 25+ ampoules, 15+ injectors, vaccines, surgery system with staged wounds, sleep and mental-health systems, knockout/coma with defibrillator revive. Medicine and Immunity skills integrate with TerjeSkills.
  - T: Skill registry consumed by sibling mods (Medicine/Immunity skills require TerjeSkills)
  - T: Server config in profile folder: TerjeSettings/Medicine.cfg
  - T: Core mod + optional feature mods (TerjeCore dependency) architecture
  - T: Public wiki on GitHub for admin documentation

## DayZ Mining System with Ores and Gems
- category: processing-station | source: steam-workshop | popularity:  | license: Repack/reupload forbidden without permission
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2794626429
- desc: Mining/smelting/crafting mod by MrFreezeGamesPC (CF dependency, 213 MB): ore nodes (gold, iron, silver, copper, tin, uranium) mined into ore rocks, smelted in a Furnace into nuggets, forged on an Anvil into ingots; gems cut on a Grinding Stone. Legacy ID 2794626429, V2 at 3604049451.
  - T: Multi-station chain with per-stage thresholds (10 nuggets to unlock next stage)
  - T: JSON configs in $profile:/FreezeMining/ (Config.json, settings.json, GemCuttingConfig.json) created on first run
  - T: Chance-tier outputs configurable per admin (EnableGemCuttingMiniGame 0/1)
  - T: Per-recipe admin disable with player-facing message: 'This crafting recipe is disabled by admins.'
  - T: minedItemsProbabilities weighted-drop map in JSON for vanilla-rock mining
  - T: Ships TEMPLATE_cfgeventspawns.xml/events.xml/types.xml for CE integration
  - T: String-table translation system covering 13 languages
  - CLASSNAMES: Freezestone_iron, Freezestone_tin, Freezestone_silver, Freezestone_gold, Freezestone_copper, Freezestone_uranium, FreezeGem_Amber, FreezeGem_Amethyst, FreezeGem_Aquamrine, FreezeGem_FancyColourDiamond, FreezeGem_Iolite, FreezeGem_Jade, FreezeGem_Ruby, FreezeGem_Turquoise

## IAT_Smelting_Metallurgy
- category: processing-station framework | source: steam-workshop | popularity: 6,038 subscribers (not enough ratings) | license: Repack/republish forbidden; serverpack inclusion prohibited
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3616136934
- desc: Smelting framework by ItsATreee: 4 furnace tiers (BasicStone, BasicBrick, AdvancedStone, Industrial), 19 alloy ingots, and an explicit extension framework for other modders/admins to add furnaces, ores and smelting results via JSON. Includes an Over/Under wager minigame (bet fuel+ore for bars) with per-player win/loss tracking. Currently requires IAT_Mining_Enhanced.
  - T: Extensible registry: modders add new furnace/ore/result combos; admins combine multiple mining mods via JSON
  - T: Device tiers gating recipe availability (4 furnace types)
  - T: Gamble/chance mechanic consuming inputs with tracked per-player statistics in JSON

## Public Metallurgy & Forging
- category: processing-station (open source) | source: github | popularity:  | license: GNU AGPL-3.0
- url: https://github.com/Dumpgrah/Public-Metallurgy-Forging
- desc: Open-source (GNU AGPL-3.0) mining/refining/smithing mod by Dumpgrah: harvest ore from boulders, refine to metal in smelters/forges, smith nails, wedges and tools. Server-profile config controls temperatures, smelting times and other parameters.
  - T: Profile-folder config for processing temperatures/times
  - T: Cross-mod compatibility contract with the author's Ammo Making mod (shared materials)

## ThingsToDo
- category: activities / processing | source: steam-workshop | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3239441684
- desc: Activity bundle adding moonshine production and gold mining/processing: deploy a moonshine still kit, prepare mash pots (cooking pot + pumpkin slices + sugar + yeast), attach 2 mash pots and propane to the still, ignite, get 6 bottles per batch; mine gold ore with pickaxe/sledgehammer and process it. Timers admin-configurable. Workshop page unreachable this session (Steam 429) — details from search snippets; stats unverified.
  - T: Deployable station kit + attachment slots for N input containers and fuel
  - T: Batch processing with fixed multi-item output per run
  - T: Admin-configurable process timers

## MoonShining
- category: processing-station | source: steam-workshop | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3301234558
- desc: Standalone moonshine still mod by Phantom: craft mash pots (cooking pot + pumpkin slices + sugar + yeast), attach two plus a dedicated propane canister and hose to the still, ignite burner, wait out a configurable brew time. Mechanics confirmed via search snippets and a DayZRP suggestion thread (accepted for testing there); workshop page unreachable (429), stats unverified.
  - T: Intermediate container items (mash pots) crafted from vanilla items carry the recipe state
  - T: Dedicated fuel attachment (unique propane can) gates operation
  - T: Configurable brew duration

## NamantHs Moonshining Expansion
- category: recipe addon | source: steam-workshop | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3386828651
- desc: Addon adding 8 new moonshine recipes (using vanilla items) to Phantom's MoonShining mod; also compatible with ThingsToDo. From search snippets; page not fetched.
  - T: Third-party recipe pack extending another mod's processing station

## fx-moonshine
- category: processing-station | source: steam-workshop | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3180720344
- desc: Distillery machine mod: attach a water barrel, gas stove and gas canister to the machine, insert potatoes, turn on the stove and vodka collects in a pot. From search snippets; page not fetched.
  - T: Multi-attachment machine (barrel + stove + gas) with output collected into a container item

## MilitaryBioLab
- category: map/POI | source: steam-workshop | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2295220978
- desc: Map addition: an abandoned biological laboratory where scientists unsuccessfully developed a Z-virus vaccine. From search snippet; page not fetched.


# Steam Workshop — quests, unlocks, economy

## DayZ Expansion Quests
- category: quest-framework | source: steam-workshop | popularity: 687 ratings, 1,102,845 subscribers (workshop page, fetched 2026-07-31) | license: Repost restricted to official Expansion Steam account; Expansion code is CC BY-NC-ND (no copying, pattern reuse only)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2828486817
- desc: MMO-like quest framework by the Expansion team (Salutesh, Jacob_Mango et al.), part of the Expansion Bundle. Quests are per-file JSON configs in $profile:ExpansionMod\Quests\Quests\ (objectives in Objectives\<Type>\; the installed 1.9.81 quests_scripts.pbo agrees) with NPC/board givers, 10 objective types (TARGET=2, TRAVEL=3, COLLECT=4, DELIVERY=5, TREASUREHUNT=6, AIPATROL=7, AICAMP=8, AIVIP=9, ACTION=10, CRAFTING=11), item + reputation + faction rewards. Requires CF, Dabs Framework, Expansion-Core.
  - T: Quest JSON schema: ID, Type, Title, Descriptions[3], Objectives[], FollowUpQuest, PreQuestIDs[], Repeatable/IsDailyQuest/IsWeeklyQuest/IsGroupQuest/IsAchievement, SequentialObjectives, CancelQuestOnPlayerDeath
  - T: Reward schema: {ClassName, Amount, Attachments[], DamagePercent, Chance, QuestID} + NeedToSelectReward, RandomReward, RandomRewardAmount, RewardsForGroupOwnerOnly
  - T: Gating: ReputationRequirement, RequiredFaction, FactionReputationRequirements map<string,int>; rewards add via questPlayer.Expansion_AddReputation() gated by Hardline UseReputation setting
  - T: ACTION objective: ActionNames[] (action class names) + AllowedClassNames[]/ExcludedClassNames[] + ExecutionAmount — exact pattern for detecting 'used action X with item Y', which a processing framework needs
  - T: COLLECT objective: Collections[{ClassName, Amount, QuantityPercent, MinQuantityPercent}], NeedAnyCollection; CRAFTING objective: ItemNames[] + ExecutionAmount
  - T: Persistence (verified in local quests_scripts.pbo): ExpansionQuestPersistentData saved per player UID as .bin via FileMode.WRITE; group quest data keyed by GroupID string; ExpansionQuestModule.GetPlayerQuestDataByUID()
  - T: Client sync (verified in PBO): RPC_SendClientQuestConfigs + RPC_SendClientQuestData sent on player init; RPC_RequestOpenQuestMenu / RPC_ExitQuestMenu for UI
  - T: Config migration: versioned classes ExpansionQuestRewardConfigBase -> V1 -> current + ConfigVersion int in every JSON
  - CLASSNAMES: WaterBottle, TunaCan, TaloonBag_Blue, SledgeHammer, Apple, ImprovisedFishingRod, PotatoSeed

## Enhanced Banking (LB Banking, LBmaster)
- category: banking-economy | source: steam-workshop | popularity: 140 ratings (workshop page, fetched 2026-07-31) | license: All rights reserved; no repack/modify/reupload without written permission
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2454879181
- desc: Server economy mod by [2Low] LBmaster: personal and group bank accounts, robbable ATMs, credit-card requirement option, custom currencies, paychecks. All admin config is JSON in $profile:LBBanking (or LBGroup with Advanced Groups): ATMConfig.json + ATMPositions.json.
  - T: JSON config in $profile:LBBanking/ATMConfig.json: account caps (personalATMAccountMaxMoney), start balance, transactionDelayMS anti-spam
  - T: currencyValues: [{itemclassname, value}] — maps arbitrary item classnames to abstract currency value (works with any mod's items)
  - T: Per-object config array ATMPositions.json: {classname, position, orientation, canBeRobbed, needLockpickToRobATM, cooldown minutes, needCreditCard} — pattern for admin-placed device instances with per-instance settings
  - T: Server-tracked mutable state written back into the config JSON (cashDeposited overwritten by server at runtime)
  - T: Timed risk action with radius constraint (robbing: stay in radius N minutes, per-player re-rob cooldown 1440 min)
  - T: Global notification + map marker broadcast with $PLAYERNAME$ / %ATM_POSITION% placeholder substitution
  - T: Extensible device base class: admins add own ATM models by inheriting LB_ATM_Base
  - T: One-flag data import from other banking mods (importFromDeadcraftBanking / importFromKRBanking)
  - CLASSNAMES: LB_ATM_Base, LB_ATM_Green, MoneyRuble1

## DNA Keycards
- category: gated-access | source: steam-workshop | popularity: 724 ratings (workshop page, fetched 2026-07-31) | license: Free to use on servers incl. monetized; repack/modify/reupload prohibited (DMCA enforced)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2714183642
- desc: Free keycard access system by oSoDirty and Arrakis: 5 color tiers of holographic keycards, matching loot crates and two-floor vault buildings (strongrooms), keycard doors/walls for map builders, plus a craftable workbench. Fully configured via JSON files in $profile:DNA_Keycards; includes types.xml and trader files.
  - T: Two-step card reader interaction: card inserted, open triggers on removal — deliberate UX to avoid losing multi-use cards; spent cards visually show ERROR
  - T: Active/inactive keycard variants: inactive variant spawns in loot, uses get assigned via KeyCard_Main_System_Config.json (config path verified in GitHub wiki)
  - T: $profile:DNA_Keycards JSON config: tier order, colors, loot types, spawn mode (dynamic vs static/editor-placed)
  - T: Tiered gating: 5 card tiers matched by color to crates and vaults, illuminated tier signage on devices
  - T: Editor-placeable gate objects (doors/walls) that work without config
  - T: Multi-stage timed opening sequence on vault activation (4 stages, 9 seconds)
  - T: Ships types.xml + trader files alongside the mod for admin economy wiring
  - T: Author recommends 4h restarts due to unreliable engine timer functions on long uptime — relevant caveat for long-running processing timers
  - CLASSNAMES: DNA_Workbench_Kit, DNA_Workbench, DNA_Workbench_Perm, DNA_Crate_Red, DNA_WoodCrate_Blue

## PristineRepair
- category: repair-qol | source: steam-workshop | popularity: 660 ratings (workshop page, fetched 2026-07-31) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2216645010
- desc: Minimal single-purpose mod: repairing items always results in Pristine/max health, and Worn items become repairable. No configuration surface documented on the page.
  - T: Override of vanilla repair action outcome (result state forced to Pristine, Worn allowed as repairable) via script mod, no config

## NCPR Core (N3msis Craftingpack Redux)
- category: recipe-learning | source: steam-workshop | popularity: 72 ratings (workshop page, fetched 2026-07-31) | license: Free; repacking into servermod allowed (not recommended) with credit via NCPR GitHub link
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2999980206
- desc: Core module of the largest DayZ crafting pack (~500 items, 700 recipes across addons) by nemsi. Players discover crafting recipes by collecting recipe-sketch items that spawn in the world; sketches can be stored in a notebook or hung on a sketchboard. Crafting itself stays vanilla-style; mechanics can be toggled via JSON. Requires CF.
  - T: Recipe discovery via collectible sketch items spawned in world loot; per-addon sketch pools
  - T: Knowledge storage as item containment: sketches stored in notebook item or displayed on sketchboard
  - T: Modular core+addons architecture; mechanics (de)activated via JSON
  - T: Deliberately keeps vanilla crafting UX (sketches inform rather than hard-gate)
  - T: Admin-facing complete recipe/item list + types published on GitHub instead of workshop page
  - T: 13-language localization via stringtable

## CraftSystem
- category: crafting-framework | source: steam-workshop | popularity: 217 ratings (workshop page, fetched 2026-07-31) | license: Free; repack into server pack allowed, modification allowed; standalone re-upload prohibited
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3626433444
- desc: Lightweight free crafting framework by Reash: unlimited custom recipes (up to 5 ingredients) defined purely in $profile:CraftSystem/CS_Recipes.json, with a custom crafting menu (F9), 3D previews, timed crafts with progress bar, and server-side execution. Hot-reloads the JSON every 60 seconds without restart.
  - T: Recipe JSON schema: {DisplayName, DisplayPic, ResultItem, ResultQuantity, TimeCrafting(sec, 0=instant), inventory(1=inventory fallback ground, 0=ground), Ingredients[{Classname, Quantity}] up to 5}
  - T: Hot-reload: server polls JSON every 60s, changes apply without restart
  - T: Server-authoritative crafting with full client-server RPC sync; recipes auto-sync to every client on connect
  - T: Persistent in-progress crafts: continue and complete while player is offline/disconnected
  - T: Automatic config migration on mod update with backup files
  - T: Result placement control per recipe (inventory vs ground) for large items
  - T: Stackable vs non-stackable item detection when consuming/granting
  - T: Works with any mod's classnames (validated against server's loaded types)
  - CLASSNAMES: FurnitureKit_Chair, Rope, BurlapSack, DuctTape

## GHOST's dev: Quest system
- category: quest-system | source: steam-workshop | popularity: 63 ratings (workshop page, fetched 2026-07-31) | license: Private/paid server part; contact author for server use
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3576595294
- desc: Russian-language quest mod with NPC dialogue trees and a reputation system; 14 quest types including Crafting, Action, Research (visit locations), cache search, zone clearing, and station-based creation/repair via their separate Locksmith Table mod. Client on workshop, server part is private (contact author). JSON-configured (e.g., active quest limits).
  - T: Dialogue trees with reputation-conditional answers and greetings; optional voiceover
  - T: Reputation increases/decreases on quest complete/abandon; gates quest availability alongside completed-quest prerequisites
  - T: 14 objective types incl. Action, Crafting, Research (visit places), cache spawn-on-accept (incl. buried), player-specific activation zones that spawn mobs
  - T: Quest objective bound to an external crafting station mod (Locksmith table) — device-coupled objectives
  - T: Per-quest-giver repeatable quests with daily/weekly/instant cooldowns configured in JSON
  - T: NPC outfitting/pose configuration; NPC relocation announcements over radio

## Rejects Quest Mod
- category: quest-system | source: steam-workshop | popularity: 114 ratings, 6,420 subscribers (workshop page, fetched 2026-07-31) | license: MIT — free to use, modify, repack with attribution
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3697368129
- desc: MIT-licensed NPC-driven, chat-based quest system by Taco Donkey and [GS] JhonWinchester: press F on quest givers, /q in chat for progress, 201 pre-written quests over 4 quest givers. Three objective types (Collect, Kill, Deliver), quest chains via prerequisites, repeatable quests with cooldowns — everything in one JSON file, zero recompile, no database.
  - T: Single JSON config for all quests ('zero-recompile config')
  - T: Quest chains: prerequisites unlock content progressively
  - T: Repeatable quests with cooldown-based resets
  - T: Chat-command status interface (/q) instead of a menu — minimal UI approach
  - T: No external database; uses native save/profile persistence (page states 'no databases, no complex setup')

## RA Base Building
- category: base-building-blueprint | source: steam-workshop | popularity: 333 ratings, 193,336 subscribers (workshop page, fetched 2026-07-31) | license: No repacks
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=3173739565
- desc: Rust-style base building (Gormirn, AJ45 et al.): player crafts an RA_Blueprint from paper, holds it in hands and raises it to open a radial building menu; three material tiers (wood/metal/concrete) with upgrade costs, territory + upkeep system, custom codelock and C4 raiding.
  - T: Knowledge-item gating: crafted blueprint item in hands + raise opens the radial build menu
  - T: Radial menu UI on held item
  - T: Tiered upgrade costs per placed object (log to place, planks/metal/cinder per tier)
  - T: Territory + upkeep system for server balancing; no-build zone support
  - T: Placement restriction logic (no clipping into non-plant/rock objects)
  - CLASSNAMES: RA_Blueprint, RA_C4, RA_ClawHammer, RA_CodeLock, TerritoryHQ, RA_Cinderblock

## BaseBuildingPlus (BBP)
- category: base-building-blueprint | source: steam-workshop | popularity: 3,699 ratings, 2,822,352 subscribers (workshop page, fetched 2026-07-31) | license: No repacking or reuploads under any circumstances; posting only from Chopper/HennySea accounts
- url: https://steamcommunity.com/workshop/filedetails/?id=1710977250
- desc: One of the most-subscribed DayZ mods (Chopper, HennySea, DaemonForge et al.), snap-based base building with many part recipes documented on an external wiki. Search snippets attribute a 'BBP blueprint (crafting book)' requirement for crafting workbenches, but the workshop page itself does not describe the unlock mechanism — that detail is unverified.
  - T: Admin onboarding via shipped info folder: merge provided types.xml into server economy to prevent despawn
  - T: External wiki as the recipe/parts reference instead of in-game docs (unlock mechanism not documented on workshop page)


# Steam Workshop — top mods by subscribers (breadth sweep)

## CF (Community Framework)
- category: framework | source: steam-workshop | popularity: 4,859,576 current subscribers (6,196,649 lifetime), 5 stars / 10,116 ratings — Steam API 2026-07-31 | license: LGPL-2.1 (verified via github.com/Arkensor/DayZ-CommunityFramework); workshop page forbids repacking
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1559212036
- desc: The #1 most-subscribed DayZ mod of all time. Community framework whose flagship feature is resolving conflicting RPC type IDs between mods; also provides ModStorage persistence. Required dependency of most script mods.
  - T: RPC manager with string-named handlers (avoids RPC ID collisions)
  - T: CF_ModStorage versioned persistence (storageVersion in CfgMods)
  - T: JsonApi / config helpers

## Code Lock
- category: base-building / security | source: steam-workshop | popularity: 4,242,301 current subscribers (5,662,019 lifetime), 5 stars / 5,701 ratings | license: No repacking, no server/mod packs; monetization requires author approval
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1646187754
- desc: Replaces vanilla combination locks with a keypad UI. Configurable raid schedules, raid logs and client rate limiting.
  - T: keypad modal UI on attachment item
  - T: server-side config (raid times, logs)
  - T: client rate limiting for code attempts

## VPPAdminTools
- category: admin tools | source: steam-workshop | popularity: 3,779,212 current subscribers (4,940,206 lifetime) | license: custom license on GitHub (VanillaPlusPlus/VPP-Admin-Tools, SPDX NOASSERTION)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1828439124
- desc: Feature-rich GUI admin tools (V++). Permissions system, notifications module, item/object spawning menus. Requires CF. Also in the local modpack.
  - T: permissions framework with roles
  - T: GUI admin menus
  - T: VPPNotifications module
  - T: server-profile config files
  - T: Workbench plugin for development

## BuilderItems
- category: mapping / props | source: steam-workshop | popularity: 2,964,817 current subscribers (4,549,468 lifetime) | license: Author grants full permission to repack; monetized servers OK
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1565871491
- desc: Gives ~1700 map props, walls, roads and structures proper class configs so server owners can place them as decoration via editor tools. Built into DayZ Expansion (do not double-load).
  - T: config-only exposure of engine props as placeable classes

## Dabs Framework
- category: framework | source: steam-workshop | popularity: 3,388,193 current subscribers (4,506,769 lifetime) | license: open source on GitHub (InclementDab/DayZ-Dabs-Framework, custom terms, SPDX NOASSERTION); workshop: no repacking
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2545327648
- desc: Open-source lightweight modding framework by InclementDab, foundation for DayZ Editor. Provides UI (MVC) and scripting utilities. Also in the local modpack.
  - T: MVC UI framework with view-model binding
  - T: lightweight event/utility layer for mods

## FlipTransport
- category: vehicles / QoL | source: steam-workshop | popularity: 3,020,606 current subscribers (4,416,875 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1832448183
- desc: Flip overturned vehicles with a wrench or hydraulic jack, push vehicles in neutral. Config in server profile: configs/FlipTransport.v4.json.
  - T: versioned JSON config filename (.v4.json)
  - T: tool-requirement user action (wrench/jack)
  - T: safezone-aware action gating

## WindstridesClothingPack
- category: clothing content | source: steam-workshop | popularity: 2,918,343 current subscribers (4,392,510 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1797720064
- desc: High-quality post-apocalyptic clothing pack (gas masks, ponchos, sweaters, shemaghs) with male/female models, LODs, damage states and wet-texture scripting.

## BaseBuildingPlus
- category: base building | source: steam-workshop | popularity: 2,822,350 current subscribers (4,301,303 lifetime) | license: No repacking or reuploads under any circumstances
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1710977250
- desc: Major base-building overhaul: kit-based placement, 30+ building parts, carpets/wallpaper/plaster, recipe wiki. Updated for 1.27.
  - T: kit item -> placement hologram -> staged construction
  - T: companion item-pack mod to keep core lean
  - T: external wiki for recipes

## Advanced Weapon Scopes
- category: weapons content | source: steam-workshop | popularity: 2,698,693 current subscribers (4,166,451 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2143128974
- desc: Replacement optics pack (EOTech, ACOG+RMR, night vision, magnifier combos) with unique abilities per scope.

## Trader (Dr. Jones)
- category: economy | source: steam-workshop | popularity: 2,353,562 current subscribers (4,074,712 lifetime) | license: No reupload in modified or unmodified state
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1590841260
- desc: The classic AI trader mod: trader cities, safezones, currency item (Ruble), fully admin-configured via TraderConfig in the profile folder. Also in the local modpack.
  - T: profile-folder text config for items/prices/vehicles
  - T: currency spawned only via selling (economy sink control)
  - T: safezone system
  - T: NPC/vending trader interaction
  - CLASSNAMES: Ruble

## VanillaPlusPlusMap
- category: map / QoL | source: steam-workshop | popularity: 2,479,340 current subscribers (4,049,095 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1623711988
- desc: In-game map (M key) with placeable client markers and 3D markers. Server-side VPPMapConfig.json is auto-generated on first start and hot-reloads when saved while the server runs. Requires CF.
  - T: auto-generated $profile JSON config
  - T: live config reload on save
  - T: server-pushed global markers
  - T: client persistence of markers across servers

## CannabisPlus
- category: production / processing | source: steam-workshop | popularity: 2,431,132 current subscribers (3,910,952 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1932611410
- desc: Full production loop: growable strains with per-strain JSON configs (auto-generated defaults), a drying station (CP_DryPost) that locks attachments during a timed process and spawns outputs into its inventory, and a deployable workbench (kit-crafted) that converts buds->bags->bricks with battery power, consumables and configurable processing time.
  - T: deployable station via craftable kit (planks+nails)
  - T: attachment slots as typed input/consumable slots (Wrapper, Bagger, LargeBattery, bags, plastic roll)
  - T: configurable power draw + battery requirement
  - T: configurable processing interval; locked attachments during process
  - T: outputs spawned into device inventory on completion
  - T: global + per-strain JSON configs auto-generated if missing
  - T: start/stop in-world continuous actions
  - CLASSNAMES: CP_DryPost, CP_Workbench, CP_Workbench_Kit, CP_CannabisBud, AC_EmptyBag

## DayZ Editor Loader
- category: server tool | source: steam-workshop | popularity: 2,665,732 current subscribers (3,779,281 lifetime) | license: No repacking (InclementDab)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2276010135
- desc: Loads .dze files from DayZ Editor into the server mission at startup; supports mass object deletion (40k objects/sec) and auto loot-position export.
  - T: mission-folder file drop (EditorFiles) as admin workflow
  - T: mapgrouppos auto-export for loot

## Community-Online-Tools (COT)
- category: admin tools | source: steam-workshop | popularity: 2,230,424 current subscribers (3,752,829 lifetime) | license: CC-BY-SA-4.0 (verified via github.com/Jacob-Mango/DayZ-CommunityOnlineTools)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1564026768
- desc: GUI admin tools from the Community Offline Mode developers: teleport, player stats, weather, vehicle spawning. Modular design lets other mods (e.g. Expansion) register their own admin menus. Per-player JSON permission files keyed by SteamID/GUID; Discord webhooks with per-channel filtering.
  - T: modular admin-menu registration API for third-party mods
  - T: PermissionsFramework: per-player JSON files by SteamID
  - T: role assignment in-game
  - T: configurable Discord webhooks (multiple channels, event filtering)

## Breachingcharge
- category: raiding | source: steam-workshop | popularity: 2,332,700 current subscribers (3,729,032 lifetime) | license: No repacking, no derivatives without permission
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1827241477
- desc: C4 breaching charges that work through DayZ's native damage system against vanilla and modded walls (BBP, Expansion, More Doors); classnames and types template documented on a GitHub wiki.
  - T: compatibility-submod pattern for cross-mod support
  - T: GitHub wiki as admin documentation

## Ear-Plugs
- category: client QoL | source: steam-workshop | popularity: 2,124,856 current subscribers (3,679,013 lifetime) | license: No repack/reupload; monetized use restricted to approved servers
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1819514788
- desc: Rebindable key toggles/lowers sound-effect volume with an on-screen indicator, so VOIP stays audible.

## MuchStuffPack
- category: furniture / deco content | source: steam-workshop | popularity: 1,964,137 current subscribers (3,593,650 lifetime) | license: No repacking; monetized servers allowed with conditions
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1991570984
- desc: Large collection of placeable base, furniture and decorative items (fridges, gunwalls, fireplaces), melee weapons and clothing; kits dismantle with tools. Requires MuchFramework. Support ended at game version 1.26.
  - T: kit deployment dismantlable by tool type
  - T: profile config to hide proxy renders for performance

## DayZ-Expansion-Licensed
- category: expansion module | source: steam-workshop | popularity: 2,270,784 current subscribers (3,489,084 lifetime) | license: ADPL-SA for the assets (stated on workshop page); scripts CC BY-NC-ND 4.0 like the rest of Expansion
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2116157322
- desc: Part of the Expansion bundle containing Bohemia-licensed assets under ADPL-SA (share-alike, noncommercial, Arma/DayZ only).

## DayZ-Expansion-Core
- category: framework / expansion module | source: steam-workshop | popularity: 2,345,508 current subscribers (3,459,526 lifetime) | license: scripts CC BY-NC-ND 4.0 (script headers, see the local Expansion entry); posting restricted to the Expansion team account
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2291785308
- desc: Foundation module required by all Expansion mods; adds safezones, Expansion logging and debug settings.
  - T: modular multi-PBO architecture (Core + feature modules)
  - T: settings classes synced server->client on connect
  - T: safezone system

## MuchCarKey
- category: vehicles | source: steam-workshop | popularity: 1,769,400 current subscribers (3,130,305 lifetime) | license: No repacking
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2049002856
- desc: Lock any car with colored, taggable keys. Key duplication happens at a placeable MCK_Grinder station and wears the key out (1 key -> 2 copies before ruined); admin MasterKey/ResetKey items; configurable lockpick raiding. Support ended at 1.26.
  - T: key<->vehicle assignment persisted on entities
  - T: station-based item duplication with damage-per-use
  - T: admin-only special items
  - T: config-selectable raid tool
  - CLASSNAMES: MCK_Grinder, MCK_MasterKey, MCK_ResetKey

## SNAFU Weapons
- category: weapons content | source: steam-workshop | popularity: 1,602,064 current subscribers (2,836,047 lifetime) | license: DPL-ND (stated on workshop page)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2443122116
- desc: Very large weapon pack of purchased/custom models, released under the DayZ Public License - No Derivatives (DPL-ND).

## Airdrop-Upgraded
- category: events | source: steam-workshop | popularity: 1,435,182 current subscribers (2,830,828 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1870524790
- desc: Server-driven airdrop events: configurable frequency, loot container types, per-location zombie spawns, aircraft behavior and logging; players can call private drops with an AirdropFlare item. Settings file auto-converts between mod versions.
  - T: auto-migration of settings file across versions
  - T: player-consumable event trigger item
  - T: per-location event config
  - T: detailed activity logging
  - CLASSNAMES: AirdropFlare

## Banking
- category: economy | source: steam-workshop | popularity: 1,361,309 current subscribers (2,749,832 lifetime) | license: No repack/reupload; no derivatives without permission; monetizing features prohibited
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1836257061
- desc: ATM-based banking: deposit/withdraw money at map ATMs, persistent per-player accounts, highly configurable limits; documented on GitHub. By the Breachingcharge author.
  - T: per-player account persistence
  - T: interaction via placed world objects (ATMs)
  - T: configurable caps to balance economy

## Server_Information_Panel
- category: UI / info | source: steam-workshop | popularity: 1,438,960 current subscribers (2,712,933 lifetime) | license: No reupload/repack
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1680019590
- desc: In-game server info panel (rules, links, tabs) opened with a key; configured via ServerPanelConfig.json in the profile folder. Requires CF.
  - T: ServerPanelConfig.json in profile
  - T: client keybind opens config-driven UI

## CarCover
- category: vehicles / performance | source: steam-workshop | popularity: 1,469,434 current subscribers (2,675,064 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2303483532
- desc: Cover a car with a camo net/tarp: the vehicle becomes a static object (survives restarts, saves server FPS) and on uncover restores full state — damage, fluids, attachments with nested inventory, lock state across several lock mods.
  - T: entity -> static object swap with full state capture
  - T: recursive inventory serialization (weapons with attachments)
  - T: cross-mod lock-state compatibility layer
  - T: per-vehicle config with model offsets

## DayZ-Expansion-Vehicles
- category: vehicles / expansion module | source: steam-workshop | popularity: 1,615,974 current subscribers (2,655,088 lifetime) | license: scripts CC BY-NC-ND 4.0 (Expansion team; see the local Expansion entry)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2291785437
- desc: Helicopters (Little Bird, Merlin, UH1H), boats, cars and vanilla skins plus car keys, towing and vehicle covering.

## Unlimited Stamina
- category: gameplay tweak | source: steam-workshop | popularity: 1,247,953 current subscribers (2,539,155 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1560819773
- desc: Removes stamina influence on speed and weight entirely.

## Cl0ud's Military Gear
- category: clothing content | source: steam-workshop | popularity: 1,200,778 current subscribers (2,486,012 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1630943713
- desc: Military clothing in 16 camo patterns: ghillies, vests, backpacks, helmets; ships sorted types.xml and TraderConfig snippets for admins.

## BodyBags
- category: performance / gameplay | source: steam-workshop | popularity: 1,718,461 current subscribers (2,452,761 lifetime) | license: No repacking (DMCA enforced)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2819373632
- desc: Dead player bodies turn into body bags after a delay (serverDZ.cfg-configurable); the bag's lifetime inherits from the longest-lifetime item inside. Built for the Rearmed servers with InclementDab.
  - T: timed corpse -> container transform
  - T: lifetime inheritance from contents
  - T: custom serverDZ.cfg variables for settings

## Namalsk Survival
- category: survival overhaul | source: steam-workshop | popularity: 1,289,425 current subscribers (2,415,171 lifetime) | license: No monetization of any kind; reuploads DMCA'd
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2289461232
- desc: Official Namalsk companion by Sumrak (BI dev): EVR storms and blizzard event system, frostbite, a cold-resistance soft skill that grows the longer you survive, plus new clothing and ambient music.
  - T: server event system (EVR storm, blizzard) with client effects
  - T: soft-skill progression (cold resistance)
  - T: frostbite/injury system tied to exposure

## DayZ-Expansion-Market
- category: economy / expansion module | source: steam-workshop | popularity: 1,589,593 current subscribers (2,408,058 lifetime) | license: scripts CC BY-NC-ND 4.0 (Expansion team; see the local Expansion entry)
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2572328470
- desc: Expansion's market: trading zones with dynamic stock and pricing, NPC or static-object traders (eAI supported), item variations, and any stackable item configurable as currency. Rank ~31 by subscribers.
  - T: zone-based dynamic stock/pricing
  - T: arbitrary item-as-currency config
  - T: item variations sharing base config
  - T: condition-affects-price

## DrugsPLUS
- category: production / processing | source: steam-workshop | popularity: 1,211,267 current subscribers (2,293,499 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2170927235
- desc: From the CannabisPlus authors: grow coca, craft CocaLeaves -> CocaBag -> CocaBrick chains (with duct tape), magic mushrooms and LSD with impairment effects; grow times and crop counts in a config file. Rank ~38 by subscribers.
  - T: multi-step crafting chain with consumables
  - T: config file for grow time / crop count
  - T: impairment post-process effects
  - CLASSNAMES: CocaLeaves, CocaBag, CocaBrick

## TraderPlus
- category: economy | source: steam-workshop | popularity: 1,073,973 current subscribers (1,973,167 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2458896948
- desc: Standalone trader with dynamic stock-driven pricing, item-condition value, multi-currency, trade licenses (bought or admin-assigned), vehicle ownership receipts and built-in banking with credit-card items. Rank ~54 by subscribers.
  - T: license items gating access
  - T: dynamic stock->price coupling
  - T: multi-currency
  - T: banking via credit-card item + fees

## Notes
- category: gameplay / documents | source: steam-workshop | popularity: 723,387 current subscribers (1,785,489 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1999076953
- desc: Write on paper in-game with pens; paper transforms into a WrittenNote item for persistence, notes are placeable/readable and logged server-side with author SteamID. Rank ~59 by subscribers.
  - T: item transform on write (Paper -> WrittenNote) for types.xml persistence
  - T: write/read UI from hand or by targeting
  - T: server-side content logging with identity
  - CLASSNAMES: WrittenNote

## Dogtags
- category: gameplay / identity items | source: steam-workshop | popularity: 1,201,201 current subscribers (2,090,551 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2303554682
- desc: Every survivor spawns with a dogtag storing name, tag date and blood type; tags are lootable only from the dead, with hero/bandit/survivor variants and serverDZ.cfg playtime gating. Rank ~50 by subscribers.
  - T: per-item metadata persistence (name/date/bloodtype)
  - T: spawn-time item injection to player
  - T: conditional removal rules via serverDZ.cfg vars

## Notifications
- category: framework / UI | source: steam-workshop | popularity: 1,117,727 current subscribers (2,232,419 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2353998362
- desc: Prettier replacement notification system exposing a public static API (NotificationSystem.SimpleNoticiation with title, icon, color, duration, target identity) plus JSON config and BattlEye-message capture. Rank ~43 by subscribers.
  - T: public static API consumed by other mods
  - T: JSON config with version field
  - T: targeted per-player notifications via PlayerIdentity

## Mass'sManyItemOverhaul (MMIO)
- category: content overhaul | source: steam-workshop | popularity: 1,160,269 current subscribers (2,363,838 lifetime) | license: No unpack/repack/redistribution (DMCA enforced), no server-pack exceptions
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1566911166
- desc: One of DayZ's longest-running content mods: weapons, clothing, crafting recipes and mechanics in a vanilla-plus style. Rank ~35 by subscribers.

## MunghardsItempack
- category: content / misc items | source: steam-workshop | popularity: 972,235 current subscribers (2,105,531 lifetime) | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=1734713776
- desc: Large misc item pack (WW2 firearms, backpacks, rigs, grenades, and oddities including a brain model item) with attachment-heavy container items. Rank ~48 by subscribers.


# Ecosystem — AI skills, dev tools, web config editors

## StarDZ-Team/Dayz-Modding-Skills (Agent Skill)
- category: ecosystem | source: github | popularity: 16 stars, 8 forks | license: MIT
- url: https://github.com/StarDZ-Team/Dayz-Modding-Skills
- desc: A public Agent Skill (open Agent Skills standard) that makes AI assistants experts at DayZ modding: SKILL.md plus 6 reference files (enforce-script-reference, api-patterns, architecture, development-workflow, advanced-patterns, gui-patterns), 3,940+ lines built from 2,800+ vanilla scripts and 10+ production mods. Works with Claude Code, Cursor, Copilot, Gemini CLI. Answers the key question: yes, a public DayZ modding skill already exists.
  - T: Enforce Script language reference incl. 30+ gotchas
  - T: engine API patterns: entities, RPC networking, file I/O, widgets, timers
  - T: 5-layer compilation hierarchy / mod architecture docs
  - T: performance patterns: widget pooling, caching, batched processing
  - T: Module-Form-Window UI pattern, CanvasWidget, RichTextWidget

## StarDZ-Team/DayZ-Modding-Wiki
- category: ecosystem | source: github | popularity: 16 stars, 7 forks, 103 commits | license: Docs CC BY-SA 4.0, code examples MIT
- url: https://github.com/StarDZ-Team/DayZ-Modding-Wiki
- desc: Companion open-source documentation project: 9 sections, 92+ chapters (Enforce Script, mod structure, GUI/layouts, file formats and tools incl. PBO packing, config files, 23-chapter engine API reference, patterns, tutorials, server administration). 1,107+ files, 31+ Mermaid diagrams, available in 12 languages including Russian; updated for DayZ 1.29.
  - T: engine API method documentation (23 chapters)
  - T: GUI and layout system guides (10 chapters)
  - T: PBO packing and file-format chapters
  - T: server config file documentation
  - T: copy-paste code examples reverse-engineered from production mods

## Agentic-Z (DayZ-n-Chill)
- category: ecosystem | source: github | popularity: 13 stars, 7 forks | license: Custom license (free for DayZ mod development, copyright Brian Orr / DayZ n' Chill)
- url: https://github.com/DayZ-n-Chill/Agentic-Z
- desc: An 'AI Agent Stack for DayZ Modding': 11 specialist agents (scripting, configs, 3D, mapmaking, UI, server admin, debugging, Workbench plugins), 26 slash-command skills (/dayz-preflight, /dayz-init, PBO building, local test servers), and a dayz-rag MCP server doing semantic search over vanilla DayZ sources (Enforce Script, configs, layouts, materials) plus the Bohemia community wiki via Voyage AI embeddings. Prebuilt vector index downloadable.
  - T: RAG/MCP semantic search over vanilla game sources instead of raw grep
  - T: skill-orchestrated toolchain: DayZ Tools, P:\ drive mount, test server launch, texture/model compilation
  - T: multi-agent decomposition by modding domain
  - T: /sync-skills pattern for sharing skills across agent CLIs

## Claude skill marketplaces (smithery.ai) — DayZ coverage check
- category: ecosystem | source: other | popularity:  | license: 
- url: https://smithery.ai/skills
- desc: Searched smithery.ai skills and awesome-claude-skills-style lists for DayZ: no DayZ modding skill is listed there; only generic skill-creation skills appear. The only public DayZ AI resources found anywhere are StarDZ-Team's skill/wiki and Agentic-Z (both GitHub-hosted, not on marketplaces).
  - T: skill marketplace distribution model (searchable directory, install via CLI)

## Mikero's Dos Tools (pboProject / MakePbo / ExtractPbo / DeRap)
- category: ecosystem | source: other | popularity:  | license: Freemium proprietary
- url: https://mikero.bytex.digital/Downloads
- desc: The community-standard PBO toolchain for Arma/DayZ, hosted at mikero.bytex.digital: pboProject (strict project packer), MakePbo, ExtractPbo, DePbo, DeRap/Rapify (config.bin to cpp and back), Eliteness, dayz2p, texture converters. Freemium: core tools free to download, premium features behind a Bytex Market license.
  - T: strict PBO packing with binarization and dependency validation (catches errors Addon Builder silently passes)
  - T: config.bin decompilation (DeRap) for inspecting other mods' configs
  - T: command-line automation of pack/extract for CI-style build scripts

## PBO Manager (winseros/pboman3)
- category: ecosystem | source: github | popularity: 168 stars, 22 forks | license: AGPL-3.0
- url: https://github.com/winseros/pboman3
- desc: Open-source Windows GUI for opening, packing and unpacking Arma/DayZ PBO files, with in-archive file preview, support for Mikero-mangled files, Windows Explorer context-menu integration (incl. Win11 immersive menu), and metadata via pbo.json / $PBOPREFIX$.
  - T: quick PBO inspection without full extraction
  - T: Explorer shell integration for pack/unpack
  - T: $PBOPREFIX$ / pbo.json metadata handling

## RaG PBO Builder (Tyson89)
- category: ecosystem | source: github | popularity:  | license: 
- url: https://github.com/Tyson89/RaG-PBO-Builder
- desc: Free DayZ mod build tool for packing, binarizing, converting configs, signing PBOs, copying keys, and running preflight checks for missing paths, configs, textures, materials and unreadable P3D references (per repo description; not fetched in depth — search-snippet basis).
  - T: one-tool pack+binarize+sign+copy-keys pipeline
  - T: preflight validation of asset references before packing

## VSCode Enforce Script extensions (survey)
- category: ecosystem | source: other | popularity:  | license: 
- url: https://marketplace.visualstudio.com/items?itemName=WalaceWilker.dayz-enforce-script
- desc: Four marketplace extensions verified: 'DayZ Enforce Script' (publisher WalaceWilker; full dev support incl. Community Framework), 'EnScriptSyntax' (Risage; autocomplete + syntax highlighting, updated for DayZ 1.27, GitHub RisageDev/EnScriptSyntax), 'Enfusion Script' (yuval; Enforce language support + DayZ tooling, also on Open VSX), and 'jm-dayztools' (Jacob-Mango, the CF author; DayZ SA tools integration). JeromeK13/vscode-dayz-enforce-script-extension exists on GitHub as well.
  - T: Enforce Script syntax highlighting and autocomplete in VSCode
  - T: CF-aware completion (WalaceWilker extension)
  - T: editor tooling integration with DayZ Tools (jm-dayztools)

## DayZ CE Schema (rvost) — config linter
- category: ecosystem | source: github | popularity: 2,256 installs (VS Marketplace) | license: 
- url: https://github.com/rvost/dayz-ce-schema
- desc: VSCode extension providing schema validation and autocompletion for DayZ Central Economy mission files: types.xml, events.xml, cfgspawnabletypes.xml, cfgeventspawns/groups, cfgeconomycore.xml, cfgenvironment.xml, mapgroupproto/pos, globals.xml, messages.xml, plus JSON spawning-gear and object-spawner configs. Detects DayZ-specific schema violations, offers classname autocompletion, hover docs, rename/references, snippets. Requires Java 17 + RedHat XML extension.
  - T: XML/JSON schema-based validation of server configs
  - T: classname autocompletion from schema data
  - T: code actions and snippets for config authoring

## DayZ Editor (mod, by dab / InclementDab)
- category: ecosystem | source: steam-workshop | popularity: 80,789 subscribers, 5,620 favorites, 5-star (2,392 ratings) | license: Open source repo; redistribution of the PBO restricted
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2250764298
- desc: Open-source offline 3D map editing tool: place any in-game or modded object, terrain brush tools (incl. tree painting), camera/weather filmmaker tools, and export of placements to server formats (init.c, .vpp, Object Spawner). Requires Dabs Framework and CF. GitHub repo InclementDab/DayZ-Editor (114 stars) with full wiki.
  - T: in-game object placement UI with placement modes (precedent for placing static devices)
  - T: export pipeline: editor session to init.c / .vpp / spawner JSON
  - T: built on Dabs Framework + CF
  - T: import of objects from arbitrary mods by classname

## DZconfig (dzconfig.com)
- category: ecosystem | source: other | popularity:  | license: Proprietary
- url: https://dzconfig.com/
- desc: Proprietary web-based DayZ server config platform: form-based editors for 54 file types including types.xml, events.xml, cfgweather.xml, cfggameplay.json, trader files and DayZ Expansion / TraderPlus configs. Exports server-ready XML/JSON via zip download, FTP/SFTP push, or API pull. Freemium: free tier (1 instance, 2,500 classnames), Platinum $49.99/yr (4 instances, 6,000 classnames, API, unlimited audit history). Extras: audit history with rollback, bulk edit, loot simulation.
  - T: forms-over-raw-XML/JSON editing model
  - T: export exactly in the file structure the server reads
  - T: FTP/SFTP push deployment from web editor to server
  - T: audit history + rollback of config changes
  - T: modded-server support via imported classname lists

## DZPage (dzpage.com, ex dayztypes.com)
- category: ecosystem | source: other | popularity:  | license: Free, unofficial fan project (not open source)
- url: https://dzpage.com/
- desc: Free browser-based DayZ toolkit: item database of ~5,000 items extracted from game files, visual types.xml editor with plain-language field explanations, XML/JSON validators with line-numbered errors, editors for cfggameplay.json, weather, globals, spawnabletypes, events, spawn gear, plus interactive 3D/loot maps and a free REST API with personal keys. Donation-supported, not open source.
  - T: client-side web editing with upload-your-file / download-result flow
  - T: validator UX: every error at once, line numbers, plain-language explanations
  - T: item database extracted from game files powering autocomplete/icons
  - T: public REST API over item data

## Loot Editor for DayZ (dayzlooteditor.com)
- category: ecosystem | source: other | popularity:  | license: Proprietary (paid, Steam)
- url: https://dayzlooteditor.com/
- desc: Paid commercial desktop app (sold on Steam) for types.xml: spreadsheet UI for 10,000+ items, real-time XML validation, bulk operations, mod-dependency import, non-destructive saving preserving comments and indentation. Not open source.
  - T: spreadsheet-style bulk editing of large configs
  - T: smart import of mod classname dependencies
  - T: non-destructive save preserving comments/formatting

## DayZMod.tools (normandintech/dayz-mod-assistant)
- category: ecosystem | source: other | popularity:  | license: 
- url: https://normandintech.github.io/dayz-mod-assistant/tools/trader.html
- desc: GitHub-Pages-hosted web tool suite focused on trader/market configs: TraderPlus converter, Dr. Jones-to-TraderPlus migrator, vehicle-parts config generator, and DayZ Expansion Market JSON generator/editor/extractor with batch price/stock updates. No visible license or repo link on the tools page.
  - T: classname-list to config-JSON generation
  - T: format converters between competing mods' config schemas
  - T: batch value editing across a JSON config

## TraderPlusEditor (Steam Workshop tool)
- category: ecosystem | source: steam-workshop | popularity:  | license: 
- url: https://steamcommunity.com/sharedfiles/filedetails/?id=2979001087
- desc: Editor for the TraderPlus mod's PriceConfig JSON, created because the hand-edited JSON is fragile ('one comma too much and the whole mod doesn't work'). Verified via search snippet; workshop page not fetched in depth.
  - T: dedicated GUI editor shipped alongside a JSON-configured mod to prevent formatting errors

## DayZLootEdit (ojemineh)
- category: ecosystem | source: github | popularity: 21 stars, 9 forks | license: MIT
- url: https://github.com/ojemineh/DayZLootEdit
- desc: Open-source Windows desktop editor (.NET Framework 4.8) for types.xml loot tables. Minimally maintained (v0.3.14, 26 commits) but MIT-licensed.
  - T: GUI table editing over types.xml

## dzsa.tools / WOBO Tools (unverified)
- category: ecosystem | source: other | popularity:  | license: 
- desc: Two often-cited web tool sites that could not be fetched: dzsa.tools failed with an expired TLS certificate (search snippet describes a loot/types.xml editor with search/filter, real-time preview, backups), and wobo.tools returned HTTP 403 (search snippet describes interactive DayZ info tools and wiki by content creator WOBO). Reported on search-snippet basis only.


# Ecosystem — Claude Code plugins and MCP servers

## Superpowers (Claude Code plugin, obra)
- category: ecosystem | source: github | popularity:  | license: MIT
- url: https://github.com/obra/superpowers
- desc: A software-development methodology packaged as composable Claude Code skills: brainstorming (Socratic design refinement), writing-plans, test-driven-development (RED-GREEN-REFACTOR), systematic-debugging (4-phase root-cause), verification-before-completion, requesting/receiving code review, subagent-driven development, git worktrees, and writing-skills (create new skills per best practices). Installed via /plugin install superpowers@claude-plugins-official, or via /plugin marketplace add obra/superpowers-marketplace, which also hosts a 'Superpowers: Developing for Claude Code' plugin (skills for building Claude Code plugins/extensions) and Private Journal MCP.
  - T: composable skill library with bootstrap instructions that force skill usage
  - T: Socratic brainstorming flow for design docs
  - T: plan-then-execute with human checkpoints (executing-plans)
  - T: subagent-driven development with two-stage review
  - T: meta-skill for authoring new skills (applies to maintaining a modding skill like this one)
  - T: distribution via Claude Code plugin marketplaces (claude-plugins-official / obra/superpowers-marketplace)

## steam-mcp (TMHSDigital)
- category: ecosystem | source: github | popularity:  | license: CC BY-NC-ND 4.0
- url: https://github.com/TMHSDigital/steam-mcp
- desc: MCP server wrapping Steam/Steamworks Web APIs: 25 tools (18 read, 7 write) including steam_queryWorkshop (search/browse Workshop items with filters) and steam_getWorkshopItem (title, description, tags, subscriber counts). Node.js 20+/TypeScript, runs locally on Windows via npx; 10 tools need no API key, 8 read tools need a free Steam Web API key, write tools need a publisher key.
  - T: Steam Workshop metadata retrieval (subscribers, tags, descriptions) via Web API
  - T: keyless operation for public store/workshop lookups
  - T: local stdio MCP server pattern on Windows (npx + env var for API key)

## enforce-script-lsp (devz-tools)
- category: ecosystem | source: github | popularity: 7 GitHub stars, 25 commits (early stage) | license: GPL-3.0
- url: https://github.com/devz-tools/enforce-script-lsp
- desc: Rust (tower-lsp) language server for DayZ Enforce Script: real-time diagnostics, go-to-definition, completion, hover, find references, signature help, document symbols, folding; partial rename/code actions. Builds to a Windows exe with cargo; no prebuilt VSCode extension yet (custom client config required).
  - T: LSP-based static diagnostics for Enforce Script (pre-pack error catching)
  - T: indexing vanilla script sources for API-accurate completion

## EnScriptSyntax (RisageDev) / yuvalino enscript — VSCode Enforce Script extensions
- category: ecosystem | source: github | popularity:  | license: 
- url: https://github.com/RisageDev/EnScriptSyntax
- desc: EnScriptSyntax is a VSCode extension published on the Visual Studio Marketplace (Risage.enscriptsyntax) with full Enforce Script syntax highlighting and autocomplete for DayZ 1.27, automatic class and inheritance-chain detection, and method completion from parent classes. Alternative: yuvalino/enscript (github.com/yuvalino/enscript) indexes enfusion-script for jump-to-definition and other IDE features.
  - T: inheritance-chain-aware autocomplete for modded class development
  - T: marketplace-installable, no compilation needed (contrast with the LSP)

## arma-server-manager (fugasjunior)
- category: ecosystem | source: github | popularity:  | license: 
- url: https://github.com/fugasjunior/arma-server-manager
- desc: Web administration tool for Arma 3, Arma Reforger and DayZ dedicated servers: installs Steam Workshop mods, uploads scenarios, manages headless clients (per repo description; not deeply inspected in this pass).
  - T: automated Workshop mod installation onto a DayZ dedicated server







## Upstream AI-skill sources (added 2026-07-31 after mining)
- category: ecosystem | source: github
- **SWARMDayZ/dayz-enfusion-skills** — origin of the Enforce Script pattern library vendored
  by Agentic-Z (_shared/enscript/); no license file, author granted public 'use for whatever'
  permission per Agentic-Z's attribution note.
- **TrueDolphin references wiki** — the EnScript style guide Agentic-Z's enscript-style.md mirrors.
- **DayZ-n-Chill/Agentic-Z** — v1.6.0 mined in full: 14 agents, 30 skills, dayz-rag MCP
  (LanceDB + Voyage AI; key required for every query; 207MB prebuilt index on GitHub releases).
  License: free to use/modify for DayZ mod development; mods built with it are yours; may share
  the software free-of-charge with license preserved; selling forbidden. Its dayz-particles
  skill is a separate install. Docs: agentic-z.com.
- **StarDZ-Team/DayZ-Modding-Wiki** — worth a permanent local clone
  (en/ + full RU translation in ru/, 105 chapters; docs CC BY-SA 4.0, code examples MIT).
