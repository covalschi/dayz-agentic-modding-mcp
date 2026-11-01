// 5_Mission — server lifecycle hooks: settings init + (optionally) per-player data.

modded class MissionServer
{
    override void OnInit()
    {
        super.OnInit();
        MyModSettings.Get();   // force-load settings at server start (writes defaults on first run)
        Print("[MyMod] settings loaded, rules: " + MyModSettings.Get().Rules.Count());
    }

    override void InvokeOnConnect(PlayerBase player, PlayerIdentity identity)
    {
        super.InvokeOnConnect(player, identity);
        // Example: send player progress (or the rules, if the client must show TimeSec) to this
        // client via CF RPC here. The template itself needs no sync: the server applies the rules.
        // GetRPCManager().SendRPC("MyMod", "RPC_ReceiveRules", new Param1<string>(json), true, identity);
    }

    override void PlayerDisconnected(PlayerBase player, PlayerIdentity identity, string uid)
    {
        // Example: save per-player data here.
        super.PlayerDisconnected(player, identity, uid);
    }
}
