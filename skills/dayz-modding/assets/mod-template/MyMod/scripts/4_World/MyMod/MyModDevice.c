// 4_World — the device item + a generic continuous "process" action.

class MyMod_Device : ItemBase
{
    override void SetActions()
    {
        super.SetActions();
        // action is registered on the PLAYER below (target-based); nothing needed here
        // unless the device itself is held in hands.
    }

    // Rules are loaded on the server only (MyModSettings.LoadServer), so call this there
    bool CanProcess(ItemBase item)
    {
        if (!item)
            return false;
        return MyModSettings.Get().FindRule(GetType(), item.GetType()) != null;
    }

    // SERVER: consume input, spawn outputs, roll bonus
    void ProcessServer(PlayerBase player, ItemBase input)
    {
        if (!GetGame().IsServer() || !input)
            return;

        MyModDeviceRule rule = MyModSettings.Get().FindRule(GetType(), input.GetType());
        if (!rule)
            return;

        foreach (string outCls : rule.Outputs)
        {
            GiveOrDrop(player, outCls);
        }

        if (rule.BonusOutput != "" && Math.RandomFloat01() < rule.Chance)
        {
            GiveOrDrop(player, rule.BonusOutput);
        }

        if (rule.ConsumeInput)
        {
            input.DeleteSafe();
        }
    }

    protected void GiveOrDrop(PlayerBase player, string classname)
    {
        EntityAI created = player.CreateInInventory(classname);   // NULL when inventory full
        if (!created)
            player.SpawnEntityOnGroundPos(classname, player.GetPosition());
    }
}

class ActionProcessInDeviceCB : ActionContinuousBaseCB
{
    override void CreateActionComponent()
    {
        // an MP client holds no rules and keeps this default; sync the rules to clients
        // if the client's progress bar must show TimeSec
        float duration = 5.0;
        MyMod_Device device = MyMod_Device.Cast(m_ActionData.m_Target.GetObject());
        if (device && m_ActionData.m_MainItem)
        {
            MyModDeviceRule rule = MyModSettings.Get().FindRule(device.GetType(), m_ActionData.m_MainItem.GetType());
            if (rule)
                duration = rule.TimeSec;
        }
        m_ActionData.m_ActionComponent = new CAContinuousTime(duration);
    }
}

class ActionProcessInDevice : ActionContinuousBase
{
    void ActionProcessInDevice()
    {
        m_CallbackClass = ActionProcessInDeviceCB;
        m_CommandUID = DayZPlayerConstants.CMD_ACTIONFB_INTERACT;
        m_FullBody = true;
        m_StanceMask = DayZPlayerConstants.STANCEMASK_ERECT | DayZPlayerConstants.STANCEMASK_CROUCH;
        m_Text = "#STR_mymod_process";
    }

    override void CreateConditionComponents()
    {
        m_ConditionItem = new CCINonRuined();
        m_ConditionTarget = new CCTNonRuined(UAMaxDistances.DEFAULT);
    }

    override bool ActionCondition(PlayerBase player, ActionTarget target, ItemBase item)
    {
        MyMod_Device device = MyMod_Device.Cast(target.GetObject());
        if (!device || !item)
            return false;
        // An MP client holds no rules: it checks only that the target is the device.
        // The server runs this condition again before it accepts the action
        // (ActionManagerServer, pickedAction.Can) and applies the rules there.
        if (GetGame().IsMultiplayer() && !GetGame().IsServer())
            return true;
        return device.CanProcess(item);
    }

    override void OnFinishProgressServer(ActionData action_data)
    {
        MyMod_Device device = MyMod_Device.Cast(action_data.m_Target.GetObject());
        if (device)
            device.ProcessServer(action_data.m_Player, action_data.m_MainItem);
    }
}

modded class PlayerBase
{
    override void SetActions(out TInputActionMap InputActionMap)
    {
        super.SetActions(InputActionMap);
        AddAction(ActionProcessInDevice, InputActionMap);
    }
}
