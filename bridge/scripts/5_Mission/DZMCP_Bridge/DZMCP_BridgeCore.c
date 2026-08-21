// The bridge itself: one tick, one mailbox read, one dispatch, one publish.
//
// Lifecycle is owned by DZMCP_MissionHook.c -- this class is armed from
// MissionServer.OnInit() and its repeating call is removed on OnMissionFinish.
//
// EVERY file operation in this class happens inside the 1 Hz call, never in a
// per-frame update: a mod's frame budget is around 2 ms and serializing a JSON
// document does not fit inside it.
//
// FORMATTING RULE, measured on this stand rather than assumed: an Enforce
// statement ends at the end of its line. A line starting with "+" or "||" is
// not a continuation -- the compiler warns "Missing ';' at the end of line",
// then reports the next line as "Expected ',' or ')', not a '+'", and then
// blames the CLASS DECLARATION with "Syntax error", which is where a reader
// starts hunting. Not one of the 2810 vanilla script files begins a line with
// "+". So: one statement, one line, however long that line has to be.
class DZMCP_BridgeCore
{
    // ---- limits -----------------------------------------------------------

    // Read cap for the mailbox. Generous, but not the 100 MB vanilla's own
    // loader uses: a mailbox larger than this is not a command, and reading a
    // prefix of it is enough to fail the parse and report the reason. The file
    // is claimed (deleted) either way.
    static const int READ_LIMIT = 1048576;

    // The errors ring. Bounded on BOTH axes -- count and per-entry length --
    // because the document has to stay small: the mod cannot write it
    // atomically (Enforce has no rename), so every byte is time during which a
    // reader can observe a half-written file.
    static const int ERRORS_MAX = 10;
    static const int ERROR_LEN  = 200;

    // Caps on the two other externally-influenced strings in the document.
    static const int DETAIL_LEN = 400;
    static const int ID_LEN     = 160;

    // ---- the two in-game deadlines ----------------------------------------
    //
    // There are TWO, not one: a no-progress watchdog and a hard ceiling. Both
    // sit BELOW whatever the Python side waits, and the watchdog by at least
    // two seconds -- up to one second until the next publish, plus the
    // caller's own polling step.
    //
    // The direction matters and an earlier version of the spec, the plan and a
    // docstring all had it backwards ("the game-side timeout is set higher so
    // the mod's specific reason arrives before the generic one"). That is
    // self-refuting: whoever gives up later cannot report first. The mod must
    // give up EARLIER so that its specific reason -- which is the entire value
    // of the tool -- lands in a published state document while the caller is
    // still waiting, instead of the caller reporting a faceless timeout.
    //
    // Numbers chosen here, to be matched on the Python side:
    //   watchdog  20 s without progress
    //   hard cap  30 s in total
    //   => the Python-side wait for a world command should be 45 s, and the
    //      job ceiling above that. See the task report.
    static const float WATCHDOG_SECONDS   = 20.0;
    static const float HARD_LIMIT_SECONDS = 30.0;

    // How many publishes a terminal status must survive before the next
    // command may be claimed and overwrite it.
    //
    // Two, not one. The waiting side polls the state file about twice a
    // second with a single, non-tolerant read and only recognises a state
    // whose command id is its own -- so a result visible for a single publish
    // gives it two chances, each of which can land on a torn write. The
    // instant a new command is claimed the block is overwritten, and a
    // successful command that was never observed reads as a timeout with no
    // evidence anywhere. One extra second per command buys that whole class of
    // phantom timeout away.
    static const int TERMINAL_DWELL_PUBLISHES = 2;

    // Upper bound on the deliberate padding the probe_bloat verb can request.
    static const int PAD_MAX = 16384;

    // ---- the chat verb's own limits ---------------------------------------
    //
    // CHAT_TEXT_MAX is a refusal threshold, not a truncation point: the mailbox
    // read cap is a megabyte, and a megabyte handed to the engine's chat call
    // would be a stand-wide experiment nobody asked for. 256 bytes is longer
    // than any line worth putting in chat and the refusal names the length, so
    // an over-long line is a sentence the caller can act on rather than a
    // silently shortened message they would have to notice on screen.
    //
    // CHAT_NAME_LEN bounds each recipient name echoed back. A player name is
    // outside text of the worst kind -- chosen by a human, frequently not ASCII
    // at all -- and the detail it lands in is capped at DETAIL_LEN for the whole
    // document's sake.
    //
    // The colour classes are the four the CLIENT actually understands; see
    // VerbChat for where that list was read and why an unknown one is refused
    // rather than passed on.
    static const int    CHAT_TEXT_MAX      = 256;
    static const int    CHAT_NAME_LEN      = 32;
    static const string CHAT_COLOR_DEFAULT = "colorStatusChannel";
    static const string CHAT_COLORS        = "colorStatusChannel, colorAction, colorFriendly, colorImportant";

    // How many CONSECUTIVE failures of one file operation it takes before the
    // bridge stops calling it bad luck and starts calling it broken.
    //
    // Three of the four ways a file operation here can fail are self-healing:
    // another process holding a handle on the state file or the mailbox for a
    // moment (Windows blocks the delete and can block the open), which the very
    // next tick simply retries. Routing those straight to the red verdict path
    // would fail an otherwise perfect boot on one transient collision, with no
    // threshold anywhere to absorb it -- and the collision is not hypothetical:
    // the Python side's own mailbox clear opens the file for reading right
    // before unlinking it, and a caller waiting for a result polls the state
    // file about twice a second against this 1 Hz writer. Neither live boot
    // produced one across roughly 130 opens, so the rate is unmeasured; the
    // classification was wrong regardless of the rate.
    //
    // Five consecutive failures of the SAME operation is five seconds of one
    // file staying unavailable, which no ordinary collision survives.
    static const int FAULT_STREAK_LIMIT = 5;

    // ---- state ------------------------------------------------------------

    protected string m_SessionId;
    protected int    m_Tick;

    // The published document, kept as one live instance: its command block IS
    // the current command, its errors array IS the ring, its world block IS
    // the diagnostics. Nothing is copied into it at publish time except the
    // numbers that change every tick, so there is no second copy to drift.
    protected ref DZMCP_State m_State;

    protected ref JsonSerializer m_Json;

    protected int   m_HandlerEntries;
    protected int   m_Publishes;
    protected int   m_CommandsClaimed;
    protected int   m_ErrorsTotal;

    // True from the moment the tick handler is entered until the moment its
    // publish has completed. Seen still true at the next entry, it means the
    // previous tick did not finish -- a script fault somewhere in between.
    // Enforce has no try/catch/finally (not one occurrence in 2810 vanilla
    // files), so this flag is the only way the bridge can notice it happened.
    protected bool m_TickIncomplete;

    // Bookkeeping for the running command, none of which belongs on the wire.
    protected string m_CmdVerb;
    protected bool   m_CmdInstant;
    protected float  m_CmdStartedAt;
    protected float  m_CmdProgressAt;
    protected int    m_TerminalPublishes;

    // Set by probe_bloat for exactly one publish.
    protected string m_PadNext;

    // The action manager an in-flight `action` command was delivered through.
    // Deliberately NOT a ref: a plain object variable in Enforce is a weak
    // pointer, so if the acting player leaves the server this reads null
    // instead of keeping a destroyed manager alive. Null whenever no action
    // command is running.
    protected ActionManagerServer m_ActionManager;

    // Consecutive-failure counters, one per retryable file operation. Each is
    // reset by its own success, so they measure a RUN of failures rather than a
    // total -- see FAULT_STREAK_LIMIT.
    protected int m_MailboxOpenFails;
    protected int m_MailboxDeleteFails;
    protected int m_StateWriteFails;

    // Never assigned, on purpose: the probe_fault verb dereferences it to find
    // out whether a repeating CallLater survives a script fault raised inside
    // its own handler. The sources do not answer that, and the answer decides
    // whether the tick needs re-arming defensively.
    protected DZMCP_CommandState m_NeverAssigned;

    void DZMCP_BridgeCore()
    {
        m_State = new DZMCP_State();
        m_Json = new JsonSerializer();
        m_SessionId = "";
        m_Tick = 0;
        m_HandlerEntries = 0;
        m_Publishes = 0;
        m_CommandsClaimed = 0;
        m_ErrorsTotal = 0;
        m_TickIncomplete = false;
        m_CmdVerb = "";
        m_CmdInstant = false;
        m_CmdStartedAt = 0;
        m_CmdProgressAt = 0;
        m_TerminalPublishes = 0;
        m_PadNext = "";
        m_MailboxOpenFails = 0;
        m_MailboxDeleteFails = 0;
        m_StateWriteFails = 0;
        m_ActionManager = null;
    }

    // -----------------------------------------------------------------------
    // Init
    // -----------------------------------------------------------------------

    void Init()
    {
        BuildSessionId();

        // The only evidence source for the accept/reject decision taken INSIDE
        // StartDeliveredAction -- the engine writes "DeliveredAction",
        // "Action accepted" and "Action rejected" lines only when this is on.
        // Costs nothing while no action is being delivered, and switching it
        // on here rather than in the task that needs it keeps the compile
        // surface and the runtime behaviour identical between the two.
        LogManager.ActionLogEnable(true);

        DZMCP_Log.Info("session " + m_SessionId + " starting; watchdog " + FormatSeconds(WATCHDOG_SECONDS) + "s, hard limit " + FormatSeconds(HARD_LIMIT_SECONDS) + "s");

        // Publish immediately rather than waiting a second for the first tick.
        // This is the one file write outside the 1 Hz call, and it is
        // deliberate: the Python side cannot build a command at all until it
        // has read a session from a published state, so every second before
        // the first publish is a second in which the channel does not exist.
        Publish();
    }

    // A session token that is never reused and never empty.
    //
    // The engine gives script no boot identity: GetTime() (mission
    // milliseconds) and GetTickTime() (seconds since game start) both restart
    // near zero, so neither can distinguish two boots. Real wall clock alone
    // is not enough either -- second resolution repeats across a fast restart
    // -- and a process id is worse, because Windows recycles them and a
    // repeated token turns a healthy bridge into a "restart not detected"
    // false negative, which is the exact six-minute loss sessions exist to
    // remove. So: UTC wall clock, plus a random component, plus a CPU tick
    // count, all three.
    //
    // Non-empty is a hard requirement, not a nicety: an unset Enforce `string`
    // serializes to "" rather than to an absent key, and "" rejects the WHOLE
    // state document on the reader's side. The literal prefix guarantees it
    // cannot happen even if every call below returned zero.
    protected void BuildSessionId()
    {
        int year = 0;
        int month = 0;
        int day = 0;
        GetYearMonthDayUTC(year, month, day);

        int hour = 0;
        int minute = 0;
        int second = 0;
        GetHourMinuteSecondUTC(hour, minute, second);

        int rnd = Math.RandomInt(0, 1000000);
        int cpu = Math.AbsInt(TickCount(0));

        // Split across statements rather than chained into one expression: the
        // Enfusion parser gives up somewhere around fifteen or sixteen "+"
        // operators in a single expression with "Formula too complex", and it
        // reports that as a cascade of unrelated-looking failures in the same
        // file. Not worth being near the ceiling for the sake of one line.
        string stamp = year.ToString() + DZMCP_Text.Pad2(month) + DZMCP_Text.Pad2(day);
        stamp = stamp + "T" + DZMCP_Text.Pad2(hour) + DZMCP_Text.Pad2(minute);
        stamp = stamp + DZMCP_Text.Pad2(second) + "Z";

        m_SessionId = "s" + stamp + "-" + rnd.ToString() + "-" + cpu.ToString();

        if (m_SessionId == "")
            m_SessionId = "s-unknown-clock";
    }

    // -----------------------------------------------------------------------
    // The tick
    // -----------------------------------------------------------------------

    void OnTick()
    {
        m_HandlerEntries++;

        bool previousTickDied = m_TickIncomplete;
        m_TickIncomplete = true;

        if (previousTickDied)
        {
            RecordError("a previous tick did not reach its publish -- see the script log for the fault");
            if (m_State.command.status == DZMCP_STATUS_RUNNING && m_State.command.id != "")
            {
                FinishCommand(DZMCP_STATUS_FAILED, "the tick running this command did not complete -- see the script log");
            }
        }

        if (m_State.command.status == DZMCP_STATUS_RUNNING)
            AdvanceRunning();

        // At most one command claimed per tick. ClaimOne publishes on its own
        // the moment it takes a command, before any work is done.
        if (CanClaim())
            ClaimOne();

        Publish();
        m_TickIncomplete = false;
    }

    protected bool CanClaim()
    {
        string status = m_State.command.status;
        if (status == DZMCP_STATUS_RUNNING)
            return false;

        if (IsTerminal(status) && m_TerminalPublishes < TERMINAL_DWELL_PUBLISHES)
            return false;

        return true;
    }

    protected bool IsTerminal(string status)
    {
        return status == DZMCP_STATUS_DONE || status == DZMCP_STATUS_FAILED;
    }

    // A command that is still running is re-examined every tick. Instant verbs
    // are supposed to have reached a terminal status inside their own tick, so
    // one that is still running here did not survive its own dispatch.
    protected void AdvanceRunning()
    {
        float now = GetGame().GetTickTime();

        if (m_CmdInstant)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "the immediate verb '" + m_CmdVerb + "' did not report a result within its own" + " tick -- see the script log for the fault that stopped it");
            return;
        }

        // An action in flight: the engine "accepted" it, which is not success
        // -- the real start happens on the player's next command-handler frame,
        // and one frame later the engine re-checks conditions and can drop the
        // action without clearing the manager. So the only trustworthy signal
        // of completion is the manager actually letting go of the data.
        if (m_CmdVerb == "action")
        {
            if (!m_ActionManager)
            {
                // The weak pointer went null: the acting player left the
                // server, taking the manager with it.
                FinishCommand(DZMCP_STATUS_FAILED, "the acting player left the server while the action was in flight");
                return;
            }
            if (!m_ActionManager.DZMCP_HasPendingActionData())
            {
                FinishCommand(DZMCP_STATUS_DONE, "the action started and has ended -- the manager released it");
                return;
            }
            // Still held: fall through to the two deadlines below. When one
            // of them fires, FinishCommand's release block frees the manager
            // too -- without that, this player could never act again
            // (R25/R27).
        }

        float elapsed = now - m_CmdStartedAt;
        if (elapsed >= HARD_LIMIT_SECONDS)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "verb '" + m_CmdVerb + "' hit the hard limit after " + FormatSeconds(elapsed) + "s (limit " + FormatSeconds(HARD_LIMIT_SECONDS) + "s)");
            return;
        }

        float idle = now - m_CmdProgressAt;
        if (idle >= WATCHDOG_SECONDS)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "verb '" + m_CmdVerb + "' made no progress for " + FormatSeconds(idle) + "s (watchdog " + FormatSeconds(WATCHDOG_SECONDS) + "s)");
            return;
        }
    }

    // -----------------------------------------------------------------------
    // Claiming the mailbox
    // -----------------------------------------------------------------------
    //
    // Order is fixed and every step of it is load-bearing:
    //
    //   read the bytes -> CLOSE the handle -> delete the file -> parse
    //
    // Reading before deleting: deleting IS the claim, and deleting before a
    // successful read loses a command nobody can ever correlate. Closing
    // before deleting: an open handle blocks the delete. Parsing after
    // deleting: the file must go even when the parse fails, because during
    // normal operation nothing else in the system removes it -- the sender
    // never unlinks it, and every later send is refused as "already holds an
    // unclaimed command, wait for it to be claimed", a wait that would never
    // end.
    protected void ClaimOne()
    {
        if (!FileExist(DZMCP_CMD_PATH))
            return;

        FileHandle handle = OpenFile(DZMCP_CMD_PATH, FileMode.READ);
        if (handle == 0)
        {
            m_MailboxOpenFails++;
            Retryable(m_MailboxOpenFails, "the mailbox exists but could not be opened for reading");
            return;
        }
        m_MailboxOpenFails = 0;

        string raw;
        ReadFile(handle, raw, READ_LIMIT);
        CloseFile(handle);

        // Checked, not assumed. An unclaimed mailbox that stays put would be
        // executed again on every tick from here to the end of the session.
        // Bailing out without parsing leaves the file for the next tick to
        // try again, which is recoverable; executing it repeatedly is not.
        if (!DeleteFile(DZMCP_CMD_PATH))
        {
            m_MailboxDeleteFails++;
            Retryable(m_MailboxDeleteFails, "the mailbox was read but could not be deleted -- the command is NOT claimed and nothing was executed; the next tick will try again");
            return;
        }
        m_MailboxDeleteFails = 0;

        m_CommandsClaimed++;
        HandleClaimed(raw);
    }

    protected void HandleClaimed(string raw)
    {
        // Stage one: the fields that decide whether this command can be
        // answered at all. The raw text is kept for the rest of this method
        // because a failed parse is not the only way to end up without an id
        // -- fields are pre-created, so a partial document parses fine and
        // leaves the id empty, which is at least as likely.
        DZMCP_CommandEnvelope envelope = new DZMCP_CommandEnvelope();
        string parseError;
        bool envelopeOk = m_Json.ReadFromString(envelope, raw, parseError);

        string id = "";
        string verb = "";
        string session = "";
        if (envelopeOk)
        {
            id = envelope.id;
            verb = envelope.verb;
            session = envelope.session_id;
        }

        if (id == "")
            id = DZMCP_Text.ExtractJsonString(raw, "id");
        if (verb == "")
            verb = DZMCP_Text.ExtractJsonString(raw, "verb");
        if (session == "")
            session = DZMCP_Text.ExtractJsonString(raw, "session_id");

        id = DZMCP_Text.Sanitize(id, ID_LEN);
        verb = DZMCP_Text.Sanitize(verb, ID_LEN);
        session = DZMCP_Text.Sanitize(session, ID_LEN);

        if (id == "")
        {
            // Nothing to report to: no caller in this session is waiting on an
            // id we do not have, and an empty id published in the command
            // block would be recognised by nobody while blinding anyone who
            // IS waiting. The ring is the only place this can go.
            RecordError("a mailbox with no recoverable id was discarded: " + Excerpt(raw));
            DZMCP_Log.Info("discarded a mailbox with no recoverable id");
            return;
        }

        // A command addressed to another session -- INCLUDING one carrying no
        // session at all, which is a mismatch and not a wildcard -- is deleted
        // (already done above), recorded, and never executed. It must not
        // become the published command block either: nobody in this session is
        // waiting for that id, and overwriting the block would blind a caller
        // who is waiting for a real one.
        if (session != m_SessionId)
        {
            string seen = session;
            if (seen == "")
                seen = "(none)";

            RecordError("refused a command from another session: id=" + id + " carried " + seen + ", this session is " + m_SessionId);
            DZMCP_Log.Info("refused command " + id + " from session " + seen + "; this session is " + m_SessionId);
            return;
        }

        // Claimed for real. Publish the running block BEFORE any work starts,
        // for every verb including the immediate ones. The verbs that fail
        // hardest are the immediate ones -- they dereference things that may
        // not be there -- and if the tick dies mid-dispatch without this
        // publish, an immediate verb has published nothing at all while the
        // caller discards every state carrying a different id: total silence.
        // With this publish, the caller at least sees the mod took it, and the
        // next tick turns the still-running immediate verb into a failure that
        // points at the log.
        StartCommand(id, verb);
        Publish();

        Dispatch(verb, raw);
    }

    protected void StartCommand(string id, string verb)
    {
        m_State.command.id = id;
        m_State.command.status = DZMCP_STATUS_RUNNING;
        m_State.command.detail = "";
        m_State.command.finished_at = 0;

        m_CmdVerb = verb;
        m_CmdInstant = true;
        m_CmdStartedAt = GetGame().GetTickTime();
        m_CmdProgressAt = m_CmdStartedAt;
        m_TerminalPublishes = 0;

        DZMCP_Log.Info("claimed command " + id + " verb " + verb);
    }

    protected void FinishCommand(string status, string detail)
    {
        // Whatever ends an action command also ends the bridge's claim on its
        // manager -- and if the manager is still holding action data at that
        // moment (the watchdog fired on a stuck action, the hard limit hit, a
        // later tick failed it), it MUST be released here, or that player can
        // never perform any action again for the rest of the session (R25/R27:
        // neither the engine's rejected branch nor its dropped-at-possibility-
        // check path clears the data). The release is null-checked inside, and
        // the detail says it happened so the caller knows the engine accepted
        // the action and then never finished it.
        if (m_ActionManager)
        {
            if (m_ActionManager.DZMCP_ReleasePendingActionData())
                detail = detail + "; the manager still held the action data -- released, so the player can act again";
            m_ActionManager = null;
        }

        m_State.command.status = status;
        m_State.command.detail = DZMCP_Text.Sanitize(detail, DETAIL_LEN);
        m_State.command.finished_at = GetGame().GetTickTime();
        m_CmdInstant = false;
        m_TerminalPublishes = 0;

        DZMCP_Log.Info("command " + m_State.command.id + " -> " + status + ": " + m_State.command.detail);
    }

    // -----------------------------------------------------------------------
    // Dispatch
    // -----------------------------------------------------------------------

    // HOW A PROJECT ADDS ITS OWN VERB (the world_exec contract): edit YOUR
    // copy of this bridge -- add the name to KnownVerbs() and IsKnownVerb(),
    // route it in Dispatch(), and write a Verb<Name>() handler that checks its
    // own argument keys the way every handler here does. There is deliberately
    // no registration machinery: the MCP server neither types nor validates
    // project verbs, and marks every world_exec answer as non-standard,
    // because a verb the server validated would be a verb the server answers
    // for. Keep handler log lines free of the words the log verdict treats as
    // failure -- see DZMCP_Log.
    protected string KnownVerbs()
    {
        return "ping, spawn, teleport, set, delete, query, action, chat, probe_bloat, probe_stall, probe_fault";
    }

    protected bool IsKnownVerb(string verb)
    {
        if (verb == "ping" || verb == "probe_bloat" || verb == "probe_stall" || verb == "probe_fault")
            return true;

        if (verb == "spawn" || verb == "teleport" || verb == "set" || verb == "delete" || verb == "query")
            return true;

        if (verb == "action")
            return true;

        return verb == "chat";
    }

    protected void Dispatch(string verb, string raw)
    {
        // The verb is checked BEFORE the args are parsed, on purpose. A
        // command can arrive with both an unknown verb and an args block this
        // build cannot deserialize; of the two answers, "this build does not
        // know that verb, here are the ones it does" is the one the caller can
        // act on. Reporting the parse failure first would hide it.
        //
        // Silence is the one answer this tool must never give -- an unknown
        // verb is a loud, specific refusal, not a shrug.
        if (!IsKnownVerb(verb))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "unknown verb '" + verb + "'; this build knows: " + KnownVerbs());
            return;
        }

        // Stage two: the whole command, args included. Only reached once the
        // id is known, so a failure here is still reportable to the caller.
        DZMCP_CommandFull full = new DZMCP_CommandFull();
        string parseError;
        if (!m_Json.ReadFromString(full, raw, parseError))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "the command could not be parsed past its id and verb -- most likely an args value" + " that is not a string: " + Excerpt(parseError));
            return;
        }

        map<string, string> args = full.args;

        if (verb == "ping")
        {
            VerbPing(args);
            return;
        }
        if (verb == "spawn")
        {
            VerbSpawn(args);
            return;
        }
        if (verb == "teleport")
        {
            VerbTeleport(args);
            return;
        }
        if (verb == "set")
        {
            VerbSet(args);
            return;
        }
        if (verb == "delete")
        {
            VerbDelete(args);
            return;
        }
        if (verb == "query")
        {
            VerbQuery(args);
            return;
        }
        if (verb == "action")
        {
            VerbAction(args);
            return;
        }
        if (verb == "chat")
        {
            VerbChat(args);
            return;
        }
        if (verb == "probe_bloat")
        {
            VerbBloat(args);
            return;
        }
        if (verb == "probe_stall")
        {
            VerbStall(args);
            return;
        }
        if (verb == "probe_fault")
        {
            VerbFault(args);
            return;
        }

        // Unreachable while IsKnownVerb and the routing above agree. If they
        // ever drift apart, the caller is told rather than left waiting.
        FinishCommand(DZMCP_STATUS_FAILED, "verb '" + verb + "' is listed as known but has no handler in this build");
    }

    // The first key in `args` that `allowed` does not list, or "" when every
    // key is known. `allowed` is a pipe-fenced list, e.g. "|bytes|".
    //
    // This exists because the deserializer DROPS members the class does not
    // declare and still reports success. Without an explicit check, a command
    // with a typo in an argument name reports done having done nothing --
    // the silent counterpart of the loud unknown-verb refusal, and there is no
    // justification for the asymmetry.
    protected string UnknownArgKey(map<string, string> args, string allowed)
    {
        if (!args)
            return "";

        int count = args.Count();
        for (int i = 0; i < count; i++)
        {
            string key = args.GetKey(i);
            if (allowed.IndexOf("|" + key + "|") < 0)
                return key;
        }
        return "";
    }

    protected bool RefuseUnknownArgs(map<string, string> args, string allowed, string knownList)
    {
        string bad = UnknownArgKey(args, allowed);
        if (bad == "")
            return false;

        FinishCommand(DZMCP_STATUS_FAILED, "unknown argument '" + bad + "'; this verb knows: " + knownList);
        return true;
    }

    // ---- verbs ------------------------------------------------------------
    //
    // Task 5's verb set is deliberately small and diagnostic: the world verbs
    // are Task 6, and every verb here exists to make one acceptance
    // observation answerable from outside the game rather than by hand-editing
    // files in a profile directory.

    // Liveness, correlation and the terminal-dwell measurement.
    protected void VerbPing(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|", "(none)"))
            return;

        FinishCommand(DZMCP_STATUS_DONE, "pong from session " + m_SessionId);
    }

    // ---- world verbs ------------------------------------------------------
    //
    // Argument reading is uniform and explicit: presence is asked with
    // Contains, never inferred from an empty value, and every key a verb does
    // not know is named in the refusal. The deserializer drops members a class
    // does not declare and still reports success, so without that a typo in an
    // argument name reports done having done nothing.

    protected string ArgOr(map<string, string> args, string key, string fallback)
    {
        if (args && args.Contains(key))
            return args.Get(key);

        return fallback;
    }

    protected bool HasArg(map<string, string> args, string key)
    {
        return args && args.Contains(key);
    }

    // Every verb that needs a player calls this first, and the refusal is a
    // sentence rather than a shrug. Nobody connected is the ORDINARY state on
    // a headless stand, so "it did not work" here has to name the reason or a
    // caller will go looking for a bug in the mod under test.
    protected bool NoPlayerRefusal()
    {
        if (DZMCP_World.PlayerCount() > 0)
            return false;

        FinishCommand(DZMCP_STATUS_FAILED, "no player is on the server, so there is nobody to act on -- connect a client and try again, or use a verb that takes an explicit position");
        return true;
    }

    // spawn: class (required), where = ground|hands|inventory, pos, quantity.
    //
    // Ground spawning falls back to the player's own position when no pos is
    // given, and refuses in words when there is neither.
    protected void VerbSpawn(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|class|where|pos|quantity|", "class, where, pos, quantity"))
            return;

        string className = ArgOr(args, "class", "");
        if (className == "")
        {
            FinishCommand(DZMCP_STATUS_FAILED, "spawn needs a class argument naming what to create");
            return;
        }

        string where = ArgOr(args, "where", "ground");
        if (where != "ground" && where != "hands" && where != "inventory")
        {
            FinishCommand(DZMCP_STATUS_FAILED, "spawn: where must be ground, hands or inventory, not '" + where + "'");
            return;
        }

        if (where == "ground")
        {
            SpawnOnGround(className, args);
            return;
        }

        if (NoPlayerRefusal())
            return;

        Man player = DZMCP_World.FirstPlayer();
        EntityAI created;
        if (where == "hands")
            created = DZMCP_World.SpawnInHands(player, className);
        else
            created = DZMCP_World.SpawnInInventory(player, className);

        if (!created)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "the engine created nothing for class '" + className + "' in " + where + " -- the class may not exist, or there may be no room");
            return;
        }

        ApplyQuantity(created, args);
        FinishCommand(DZMCP_STATUS_DONE, "created " + created.GetType() + " in " + where);
    }

    protected void SpawnOnGround(string className, map<string, string> args)
    {
        vector pos;
        if (HasArg(args, "pos"))
        {
            string posText = args.Get("pos");
            if (!DZMCP_World.TextToPos(posText, pos))
            {
                FinishCommand(DZMCP_STATUS_FAILED, "pos must be three numbers like '7500 0 7500', not '" + posText + "'");
                return;
            }
        }
        else
        {
            if (NoPlayerRefusal())
                return;

            pos = DZMCP_World.FirstPlayer().GetPosition();
        }

        Object created = DZMCP_World.SpawnOnGround(className, pos);
        if (!created)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "the engine created nothing for class '" + className + "' -- the class most likely does not exist in any loaded config");
            return;
        }

        EntityAI asEntity;
        if (Class.CastTo(asEntity, created))
            ApplyQuantity(asEntity, args);

        FinishCommand(DZMCP_STATUS_DONE, "created " + created.GetType() + " on the ground at " + DZMCP_World.PosToText(created.GetPosition()) + " with no lifetime set");
    }

    // Quantity is optional everywhere it appears, and a class that has no
    // quantity is not an error -- saying so is better than pretending it was
    // applied.
    protected void ApplyQuantity(EntityAI entity, map<string, string> args)
    {
        if (!HasArg(args, "quantity"))
            return;

        ItemBase item;
        if (!Class.CastTo(item, entity))
            return;

        string raw = args.Get("quantity");
        item.SetQuantity(raw.ToFloat());
    }

    // teleport: pos (required).
    protected void VerbTeleport(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|pos|", "pos"))
            return;

        if (!HasArg(args, "pos"))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "teleport needs a pos argument like '7500 0 7500'");
            return;
        }

        string posText = args.Get("pos");
        vector pos;
        if (!DZMCP_World.TextToPos(posText, pos))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "pos must be three numbers like '7500 0 7500', not '" + posText + "'");
            return;
        }

        if (NoPlayerRefusal())
            return;

        Man player = DZMCP_World.FirstPlayer();
        string from = DZMCP_World.PosToText(player.GetPosition());
        player.SetPosition(pos);
        FinishCommand(DZMCP_STATUS_DONE, "moved the player from " + from + " to " + DZMCP_World.PosToText(player.GetPosition()));
    }

    // set: what = health|quantity, value (required), target = player|hands.
    //
    // target defaults per `what` rather than to one value for both: quantity on
    // a player is meaningless and health on empty hands is meaningless, so a
    // single default would make one of the two combinations a trap.
    protected void VerbSet(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|what|value|target|", "what, value, target"))
            return;

        string what = ArgOr(args, "what", "");
        if (what != "health" && what != "quantity")
        {
            FinishCommand(DZMCP_STATUS_FAILED, "set: what must be health or quantity, not '" + what + "'");
            return;
        }

        if (!HasArg(args, "value"))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "set needs a value argument");
            return;
        }

        string valueText = args.Get("value");
        if (!DZMCP_World.IsNumeric(valueText))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "set: value must be a number, not '" + valueText + "'");
            return;
        }

        string fallbackTarget = "player";
        if (what == "quantity")
            fallbackTarget = "hands";

        string target = ArgOr(args, "target", fallbackTarget);
        if (target != "player" && target != "hands")
        {
            FinishCommand(DZMCP_STATUS_FAILED, "set: target must be player or hands, not '" + target + "'");
            return;
        }

        if (NoPlayerRefusal())
            return;

        Man player = DZMCP_World.FirstPlayer();
        float value = valueText.ToFloat();

        if (target == "player")
        {
            if (what == "quantity")
            {
                FinishCommand(DZMCP_STATUS_FAILED, "set: a player has no quantity -- use target=hands to set the quantity of the held item");
                return;
            }
            player.SetHealth("", "", value);
            FinishCommand(DZMCP_STATUS_DONE, "player health set to " + value);
            return;
        }

        EntityAI held = player.GetEntityInHands();
        if (!held)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "the player's hands are empty, so there is nothing to set");
            return;
        }

        if (what == "health")
        {
            held.SetHealth("", "", value);
            FinishCommand(DZMCP_STATUS_DONE, "health of " + held.GetType() + " in hands set to " + value);
            return;
        }

        ItemBase item;
        if (!Class.CastTo(item, held))
        {
            FinishCommand(DZMCP_STATUS_FAILED, held.GetType() + " in hands is not an item that carries a quantity");
            return;
        }

        item.SetQuantity(value);
        FinishCommand(DZMCP_STATUS_DONE, "quantity of " + held.GetType() + " in hands set to " + value);
    }

    // delete: class (required), radius, pos.
    protected void VerbDelete(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|class|radius|pos|", "class, radius, pos"))
            return;

        string className = ArgOr(args, "class", "");
        if (className == "")
        {
            FinishCommand(DZMCP_STATUS_FAILED, "delete needs a class argument -- deleting everything nearby regardless of class is not something this verb will do");
            return;
        }

        vector pos;
        if (!ResolvePosition(args, pos))
            return;

        string radiusText = ArgOr(args, "radius", "0");
        float radius = DZMCP_World.ClampRadius(radiusText.ToFloat());

        array<Object> found;
        DZMCP_World.Gather(className, pos, radius, found);

        int removed = 0;
        for (int i = 0; i < found.Count(); i++)
        {
            GetGame().ObjectDelete(found.Get(i));
            removed++;
        }

        FinishCommand(DZMCP_STATUS_DONE, "deleted " + removed + " object(s) of class '" + className + "' within " + radius + "m of " + DZMCP_World.PosToText(pos));
    }

    // query: class, radius. Answers into the world block as well as the detail,
    // so a caller can read the count without correlating a command at all.
    protected void VerbQuery(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|class|radius|pos|", "class, radius, pos"))
            return;

        string className = ArgOr(args, "class", "");
        if (className == "")
        {
            FinishCommand(DZMCP_STATUS_FAILED, "query needs a class argument naming what to count");
            return;
        }

        vector pos;
        if (!ResolvePosition(args, pos))
            return;

        string radiusText = ArgOr(args, "radius", "0");
        float radius = DZMCP_World.ClampRadius(radiusText.ToFloat());

        array<Object> found;
        DZMCP_World.Gather(className, pos, radius, found);

        m_State.world.query_class = DZMCP_Text.Sanitize(className, ID_LEN);
        m_State.world.query_radius = radius;
        m_State.world.query_count = found.Count();

        FinishCommand(DZMCP_STATUS_DONE, "found " + found.Count() + " object(s) of class '" + className + "' within " + radius + "m of " + DZMCP_World.PosToText(pos));
    }

    // action: run a mod's own action through the engine's gate.
    //
    //   action        the action's script class name (required)
    //   target_class  config class of the object to aim at; resolved to the
    //                 first match near `pos`/the subject (optional -- many
    //                 actions take no target)
    //   subject       script/config class of a Man-derived entity to act AS,
    //                 instead of the first connected player. Exists because a
    //                 conjured survivor is not counted by GetPlayers (measured
    //                 in Task 6) but still owns an action manager -- and it is
    //                 how much of this path can be exercised on a stand with
    //                 no client attached.
    //   radius, pos   where to look for the target and the subject
    //
    // The refusal classification, the R25 release and the outcome reading all
    // live in the 4_World tier (DZMCP_DeliverAction) -- the fields the engine
    // reads are protected members of that tier and this one cannot touch them.
    // What this verb owns is the multi-tick half: "accepted" is NOT success
    // (the real start happens on the player's next command-handler frame, and
    // one frame later the engine re-checks conditions and can drop the action
    // without clearing it), so the command stays running until a later tick
    // observes the manager actually released -- see AdvanceRunning.
    protected void VerbAction(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|action|target_class|subject|radius|pos|", "action, target_class, subject, radius, pos"))
            return;

        string actionName = ArgOr(args, "action", "");
        if (actionName == "")
        {
            FinishCommand(DZMCP_STATUS_FAILED, "action needs an action argument naming the action's script class");
            return;
        }

        // The subject: a connected player by default, or a named Man-derived
        // entity found nearby.
        PlayerBase subject;
        string subjectClass = ArgOr(args, "subject", "");
        if (subjectClass == "")
        {
            if (NoPlayerRefusal())
                return;
            if (!Class.CastTo(subject, DZMCP_World.FirstPlayer()))
            {
                FinishCommand(DZMCP_STATUS_FAILED, "the connected player is not a PlayerBase, so it has no action manager to deliver through");
                return;
            }
        }
        else
        {
            vector searchPos;
            if (!ResolvePosition(args, searchPos))
                return;

            string subjectRadiusText = ArgOr(args, "radius", "0");
            float subjectRadius = DZMCP_World.ClampRadius(subjectRadiusText.ToFloat());

            array<Object> people;
            DZMCP_World.Gather(subjectClass, searchPos, subjectRadius, people);
            if (people.Count() == 0)
            {
                FinishCommand(DZMCP_STATUS_FAILED, "no '" + subjectClass + "' found within " + subjectRadius + "m to act as -- spawn one first, or drop the subject argument to use the connected player");
                return;
            }
            if (!Class.CastTo(subject, people.Get(0)))
            {
                FinishCommand(DZMCP_STATUS_FAILED, "'" + subjectClass + "' was found but is not a PlayerBase, so it has no action manager to deliver through");
                return;
            }

            // A conjured entity never went through OnSelectPlayer, so its
            // action manager was never constructed (measured: every delivery
            // answered "no server action manager" without this). Construct it
            // the way vanilla itself would have -- see DZMCP_PlayerGate.c.
            string ensure = subject.DZMCP_EnsureServerActionManager();
            if (ensure != "")
            {
                FinishCommand(DZMCP_STATUS_FAILED, "the subject cannot host an action manager: " + ensure);
                return;
            }
        }

        // The target: optional, resolved by class near the subject.
        Object targetObject = null;
        string targetClass = ArgOr(args, "target_class", "");
        if (targetClass != "")
        {
            string radiusText = ArgOr(args, "radius", "0");
            float radius = DZMCP_World.ClampRadius(radiusText.ToFloat());

            array<Object> candidates;
            DZMCP_World.Gather(targetClass, subject.GetPosition(), radius, candidates);
            if (candidates.Count() == 0)
            {
                FinishCommand(DZMCP_STATUS_FAILED, "no target of class '" + targetClass + "' within " + radius + "m of the subject");
                return;
            }
            targetObject = candidates.Get(0);
        }

        ActionManagerServer manager;
        if (!Class.CastTo(manager, subject.GetActionManager()))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "the subject has no server action manager -- it may not be fully initialized yet");
            return;
        }

        string outcome = manager.DZMCP_DeliverAction(actionName, targetObject);

        if (outcome == "")
        {
            FinishCommand(DZMCP_STATUS_DONE, "the action ran and ended within its own delivery (instant)");
            return;
        }

        if (outcome == "accepted" || outcome == "pending")
        {
            // Not success yet -- hold running and let AdvanceRunning watch
            // the manager. That watch is why both outcomes are safe to treat
            // identically: "pending" can un-park into a real start (the server
            // processes its own ack juncture and -1 matches its own pending
            // id -- see the gate's own comment), and "accepted" can be dropped
            // a frame later; in either case the truth is whether the manager
            // is still holding data, which is exactly what the watch reads.
            m_ActionManager = manager;
            m_CmdInstant = false;
            DZMCP_Log.Info("command " + m_State.command.id + " delivered action " + actionName + " -> " + outcome + "; watching the manager for completion");
            return;
        }

        // A named refusal, classified before the engine was touched (or the
        // engine's own target-lock rejection). The manager holds nothing.
        FinishCommand(DZMCP_STATUS_FAILED, outcome);
    }

    // An explicit pos, or the player's own, or a named refusal. Returns false
    // when it has already finished the command with that refusal.
    protected bool ResolvePosition(map<string, string> args, out vector pos)
    {
        pos = "0 0 0";

        if (HasArg(args, "pos"))
        {
            string posText = args.Get("pos");
            if (!DZMCP_World.TextToPos(posText, pos))
            {
                FinishCommand(DZMCP_STATUS_FAILED, "pos must be three numbers like '7500 0 7500', not '" + posText + "'");
                return false;
            }
            return true;
        }

        if (NoPlayerRefusal())
            return false;

        pos = DZMCP_World.FirstPlayer().GetPosition();
        return true;
    }

    // chat: put a line in the connected players' chat, from the server.
    //
    //   text   the line to say (required, non-empty)
    //   color  which of the client's four colour classes to use (optional)
    //
    // WHY THIS IS A VERB AT ALL. Chat is a SERVER-SIDE MESSAGE: the engine
    // hands the mod a call that delivers text to a player, so the bridge sends
    // it as data. No keyboard, no window, no foreground, nothing taken away
    // from whoever is sitting at the machine -- and the delivery is confirmed
    // against a command id instead of "typed it and hoped". A mod's OWN input
    // field (a PDA, a terminal, a form) exists only on the client and this verb
    // is no substitute for filling one.
    //
    // The engine call, read in the unpacked game sources rather than
    // remembered:
    //   CGame.ChatMP(Man recipient, string text, string colorClass)
    //       3_game/global/game.c:1036
    // Vanilla reaches it from the SERVER branch of PlayerBase.Message(text,
    // style) at 4_world/entities/manbase/playerbase.c:6596. Its client-side
    // twin ChatPlayer(text) -- what a human's typed line goes through, at
    // 5_mission/gui/chat/chatinputmenu.c:39 -- is deliberately NOT used here:
    // this bridge lives in MissionServer.
    //
    // WHO GETS IT, and why this verb does not just take the first player.
    // ChatMP names ONE recipient, so a verb holding a player list has to
    // decide. This one delivers to EVERY connected player and says how many got
    // it and who they were. Taking players.Get(0) the way the world verbs do
    // would be the silent wrong answer this whole tool exists to abolish: on a
    // two-client stand the line would land on one screen and be missing from
    // the screen the caller was watching, with nothing anywhere to explain the
    // difference. Nobody connected is a refusal in words -- the same one every
    // verb that needs a player gives.
    //
    // THE COLOUR CLASS IS NOT FREE-FORM, and getting it wrong is silent. The
    // client turns the string into a colour in ChatLine.ColorNameToColor
    // (5_mission/gui/chat/chatline.c): colorStatusChannel blue, colorAction
    // yellow, colorFriendly green, colorImportant red -- and every other value,
    // a typo included, falls out of that switch into plain white without a word
    // said. A caller would get a delivered line in a colour it never asked for
    // and could not tell from the default, so this verb checks the value itself
    // and names the four. The default is colorStatusChannel because that is
    // what vanilla's own MessageStatus uses for the game telling a player
    // something about itself, which is exactly what a line from a test harness
    // is.
    //
    // WHAT SUCCESS HERE DOES AND DOES NOT PROMISE. It promises the engine
    // accepted the call for each named recipient. It cannot promise the line
    // appeared: the client drops whole chat channels according to the player's
    // own profile options (Chat.Add, 5_mission/gui/chat/chat.c) and the channel
    // ChatMP posts to is decided in native code this bridge cannot read. If a
    // delivered line is invisible on screen, that setting is the first place to
    // look and not a fault in the bridge.
    protected void VerbChat(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|text|color|", "text, color"))
            return;

        // Presence asked as presence, never inferred from an empty value: an
        // absent key and a key set to "" are indistinguishable through the
        // value alone, and they deserve different sentences.
        if (!HasArg(args, "text"))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "chat needs a text argument carrying the line to say");
            return;
        }

        string text = args.Get("text");
        if (text == "")
        {
            FinishCommand(DZMCP_STATUS_FAILED, "chat was given a text argument with nothing in it, so there is nothing to say");
            return;
        }

        int textLen = text.Length();
        if (textLen > CHAT_TEXT_MAX)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "chat text is " + textLen + " bytes, past this bridge's limit of " + CHAT_TEXT_MAX + " -- send a shorter line rather than have the bridge cut it somewhere the caller cannot see");
            return;
        }

        string color = ArgOr(args, "color", CHAT_COLOR_DEFAULT);
        if (!IsChatColorClass(color))
        {
            FinishCommand(DZMCP_STATUS_FAILED, "chat: color must be one of " + CHAT_COLORS + ", not '" + color + "' -- the client turns a class it does not know into plain white and says nothing, so this verb will not pass one on");
            return;
        }

        // Deliberately NOT NoPlayerRefusal(). Its sentence ends "or use a verb
        // that takes an explicit position", which is a real alternative for the
        // world verbs and meaningless here: no position stands in for a
        // recipient. Same opening words -- the shape a caller already knows --
        // with an ending that is true of this verb.
        if (DZMCP_World.PlayerCount() == 0)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "no player is on the server, so there is nobody to say it to -- connect a client and try again; a chat line is delivered to a recipient and there is no recipient-free way to say one");
            return;
        }

        array<Man> players = new array<Man>;
        GetGame().GetPlayers(players);

        int sent = 0;
        string names = "";
        for (int i = 0; i < players.Count(); i++)
        {
            Man recipient = players.Get(i);
            if (!recipient)
                continue;

            GetGame().ChatMP(recipient, text, color);
            sent++;

            if (names != "")
                names = names + ", ";
            names = names + RecipientName(recipient);
        }

        // Reachable even though the count above was not zero: that count came
        // from one walk of the player list and this delivery from another, and
        // a null entry between them is a list caught mid-connect or
        // mid-disconnect. Say so rather than report a delivery to nobody as
        // done.
        if (sent == 0)
        {
            FinishCommand(DZMCP_STATUS_FAILED, "the player list held " + players.Count() + " entr(ies) but none of them was a usable recipient, so nothing was said -- the list was most likely caught mid-connect; try again");
            return;
        }

        // The gap between the two sinks, named rather than left to be
        // discovered. The engine got the line byte for byte; the echo below
        // went through Sanitize on its way into the state document, which the
        // Python side reads as strict UTF-8 and rejects WHOLE on one bad byte.
        string note = "";
        int exotic = DZMCP_Text.NonAsciiCount(text);
        if (exotic > 0)
            note = "; " + exotic + " byte(s) of it are outside printable ASCII -- the engine got them exactly as they arrived, but each one shows as a space in the echo below, because this detail has to stay ASCII";

        // Facts first, echo last, because the detail is capped at DETAIL_LEN:
        // whatever the cap eats should be the copy of the line the caller
        // already has, not the count of who received it.
        FinishCommand(DZMCP_STATUS_DONE, "said it to " + sent + " player(s) (" + names + ") in " + color + note + "; the line was: " + text);
    }

    // The four classes the client's own switch recognises. Anything else is
    // white, silently -- see VerbChat.
    protected bool IsChatColorClass(string value)
    {
        if (value == "colorStatusChannel" || value == "colorAction")
            return true;

        return value == "colorFriendly" || value == "colorImportant";
    }

    // A recipient's name, short and safe to echo.
    //
    // A player name is outside text of the worst kind: chosen by a human and
    // routinely not ASCII at all. It is capped and sanitized HERE rather than
    // relying on the sanitize FinishCommand does over the whole detail, so that
    // one long name cannot push the counts out of a capped string.
    protected string RecipientName(Man recipient)
    {
        PlayerIdentity identity = recipient.GetIdentity();
        if (!identity)
            return "(no identity)";

        string name = identity.GetName();
        if (name == "")
            return "(unnamed)";

        // A nick made entirely of characters Sanitize cannot keep -- a
        // Ukrainian one, on this project's own server, is the ordinary case --
        // would come back as a run of spaces, which reads as a bug in the
        // bridge rather than as somebody's name.
        if (DZMCP_Text.NonAsciiCount(name) == name.Length())
            return "(name is not ASCII)";

        return DZMCP_Text.Sanitize(name, CHAT_NAME_LEN);
    }

    // Makes the next published document long, then lets it snap back to its
    // normal length -- the file-truncation measurement. If opening the state
    // file for writing does NOT truncate it, the tail of the long document
    // survives past the short document's closing brace and the reader is
    // broken forever rather than occasionally. Measured before the world
    // snapshot makes the length vary on its own.
    protected void VerbBloat(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|bytes|", "bytes"))
            return;

        int bytes = 4096;
        // Presence asked as presence. An absent key and a key set to "0" are
        // indistinguishable through the value alone.
        if (args && args.Contains("bytes"))
        {
            string rawBytes = args.Get("bytes");
            bytes = rawBytes.ToInt();
        }

        if (bytes < 0)
            bytes = 0;
        if (bytes > PAD_MAX)
            bytes = PAD_MAX;

        m_PadNext = DZMCP_Text.Repeat("x", bytes);
        FinishCommand(DZMCP_STATUS_DONE, "padding exactly one state document with " + bytes + " bytes");
    }

    // Starts and never progresses, so the no-progress watchdog is the only
    // thing that can end it. The one verb in this build that exercises the
    // two-deadline design at all -- every other one finishes inside its own
    // tick and could never catch a broken deadline.
    protected void VerbStall(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|", "(none)"))
            return;

        m_CmdInstant = false;
        DZMCP_Log.Info("command " + m_State.command.id + " will stall on purpose until the watchdog ends it");
    }

    // Deliberately raises a script fault inside the tick handler.
    //
    // This is the only way to answer a question the sources do not: does a
    // repeating CallLater survive a fault raised in its own handler, or does
    // the tick stop for good? If the tick stops, every later task needs the
    // call re-armed defensively and the bridge needs to say so; if it
    // survives, the "a previous tick did not complete" path above is the whole
    // fix. Expect this verb to put a genuine script fault in the log -- that
    // is the measurement, not an accident.
    //
    // ANSWERED on a live stand: the tick survived (64 -> 77 over twelve
    // seconds) and the next tick correctly failed the command. Kept as the
    // regression test for that, behind an explicit confirmation.
    //
    // The interlock is not politeness. The fault leaves "NULL pointer to
    // instance" in the script log, a string this project's own profile lists
    // under expect.forbid, so running this verb FAILS THAT BOOT'S VERDICT. The
    // escape-hatch tool passes an arbitrary verb straight through, so anything
    // that reaches this name -- a typo, a replayed transcript, a model guessing
    // -- would otherwise poison a boot with no way to have said no.
    protected void VerbFault(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|confirm|", "confirm"))
            return;

        string confirm = "";
        if (args && args.Contains("confirm"))
            confirm = args.Get("confirm");

        if (confirm != "yes")
        {
            FinishCommand(DZMCP_STATUS_FAILED, "probe_fault refuses to run without confirm=yes: it raises a real script fault on purpose, which puts a forbidden string in the log and fails this boot's verdict");
            return;
        }

        DZMCP_Log.Info("command " + m_State.command.id + " will raise a deliberate script fault; the next tick reports what survived");

        m_NeverAssigned.status = "this line dereferences a null reference on purpose";
    }

    // -----------------------------------------------------------------------
    // Publishing
    // -----------------------------------------------------------------------

    // The world, as of this publish. Runs BEFORE the document is serialized and
    // long before the file is opened, so it never lands inside the write window
    // where a fault would leave a half-written file and an open handle.
    //
    // Published every tick rather than only in answer to a query: a snapshot a
    // caller has to ask for is a snapshot that costs a full command round trip
    // (a second to be claimed, a second of terminal dwell) to answer "where is
    // the player" -- and world_state, which exists precisely to be cheap, would
    // then be the most expensive tool in the set.
    //
    // The counted query is NOT here. It takes a class and a radius that only a
    // command carries, and walking every object in a sphere once a second on
    // the chance somebody wants it is the kind of cost that turns a 1 Hz tick
    // into a stutter. Its answer is left in place by the verb, so it stays
    // readable afterwards.
    protected void RefreshWorld()
    {
        int players = DZMCP_World.PlayerCount();
        m_State.world.players = players;

        if (players == 0)
        {
            m_State.world.player_pos = "";
            m_State.world.player_health = -1;
            m_State.world.hands = "";
            m_State.world.action_pending = -1;
            return;
        }

        Man player = DZMCP_World.FirstPlayer();
        m_State.world.player_pos = DZMCP_World.PosToText(player.GetPosition());
        m_State.world.player_health = player.GetHealth("", "");

        EntityAI held = player.GetEntityInHands();
        if (held)
            m_State.world.hands = DZMCP_Text.Sanitize(held.GetType(), ID_LEN);
        else
            m_State.world.hands = "";

        // The wedge indicator (see the protocol file): whether the player's
        // action manager is holding action data right now.
        m_State.world.action_pending = -1;
        PlayerBase asPlayer;
        ActionManagerServer manager;
        if (Class.CastTo(asPlayer, player) && Class.CastTo(manager, asPlayer.GetActionManager()))
        {
            if (manager.DZMCP_HasPendingActionData())
                m_State.world.action_pending = 1;
            else
                m_State.world.action_pending = 0;
        }
    }

    protected void Publish()
    {
        // The counter advances only when a document actually reaches the disk.
        // Bumping it up front and then failing the write would spend a tick
        // number on nothing and break the invariant every consistency check
        // here leans on -- that a published document always reads
        // tick == world.publishes + 1.
        int nextTick = m_Tick + 1;

        m_State.tick = nextTick;
        m_State.session_id = m_SessionId;

        m_State.world.tick_time = GetGame().GetTickTime();
        m_State.world.handler_entries = m_HandlerEntries;
        m_State.world.publishes = m_Publishes;
        m_State.world.commands_claimed = m_CommandsClaimed;
        m_State.world.errors_total = m_ErrorsTotal;
        m_State.world.pad = m_PadNext;
        m_PadNext = "";
        RefreshWorld();

        // Serialized by the engine's own serializer, never assembled by hand.
        // Two independent reasons: detail and error strings carry quotes,
        // backslashes and newlines that hand-rolled JSON would not escape, and
        // one such document makes the reader return nothing FOREVER; and the
        // Enfusion parser gives up at around fifteen chained "+" operators in
        // one expression, reporting it as a cascade of unrelated failures.
        //
        // "nice" is false. Pretty-printing is what vanilla's own SaveFile
        // hardcodes, and it roughly doubles the byte count -- which doubles
        // the window in which a reader can catch a half-written file.
        string text;
        if (!m_Json.WriteToString(m_State, false, text))
        {
            Malfunction("the state document could not be serialized");
            return;
        }

        // Nothing is computed between the open and the close -- not an
        // addition, not a call, not an entity walk. Enforce has no finally and
        // a file handle has no destructor, so a fault raised inside this
        // window leaves the file in an unknown state and the handle open.
        // Everything the document needs is already in `text`.
        FileHandle handle = OpenFile(DZMCP_STATE_PATH, FileMode.WRITE);
        if (handle == 0)
        {
            m_StateWriteFails++;
            Retryable(m_StateWriteFails, "the state file could not be opened for writing");
            return;
        }
        FPrint(handle, text);
        CloseFile(handle);
        m_StateWriteFails = 0;

        m_Tick = nextTick;
        m_Publishes++;
        if (IsTerminal(m_State.command.status))
            m_TerminalPublishes++;
    }

    // -----------------------------------------------------------------------
    // Errors and logging
    // -----------------------------------------------------------------------

    // One entry in the published ring. Bounded in count and in length, and
    // dropped from the FRONT with an ordered removal: a plain Remove swaps the
    // last element into the hole and destroys the ordering, which would make
    // the oldest surviving entry unpredictable.
    protected void RecordError(string message)
    {
        m_ErrorsTotal++;
        m_State.errors.Insert(DZMCP_Text.Sanitize(message, ERROR_LEN));
        while (m_State.errors.Count() > ERRORS_MAX)
            m_State.errors.RemoveOrdered(0);
    }

    // The bridge itself is broken, as against a command that legitimately
    // failed. Goes into the ring AND turns the boot verdict red, because in
    // this state nothing the bridge reports about anything else can be
    // trusted.
    protected void Malfunction(string message)
    {
        RecordError(message);
        DZMCP_Log.Fault(message);
    }

    // A file operation failed in a way the NEXT TICK can simply retry -- most
    // likely another process holding a handle for a moment, which on Windows
    // blocks both an open and a delete. `streak` is how many times this same
    // operation has failed in a row, its counter reset by its own success.
    //
    // Reported once when it starts and once when it becomes a malfunction, and
    // silently in between. Once per tick would flood a ten-entry ring in ten
    // seconds and push out every other error just when they matter most; never
    // at all would hide a condition that stops the bridge from doing its job.
    protected void Retryable(int streak, string message)
    {
        if (streak == 1)
        {
            RecordError(message);
            DZMCP_Log.Info(message + " -- retrying on the next tick");
            return;
        }

        if (streak == FAULT_STREAK_LIMIT)
            Malfunction(message + " -- " + streak + " ticks in a row, no longer treating this as a passing collision");
    }

    // A short, safe excerpt of text that arrived from outside, for quoting
    // into an error entry. Sanitized (printable ASCII, and no character a JSON
    // string would have to escape) because one bad byte anywhere in the
    // document makes the WHOLE document unreadable to the Python side, for as
    // long as it keeps being written -- and a ring entry keeps being written
    // until ten more errors rotate it out. A mailbox full of some other
    // encoding, or a truncated JSON document full of quotes, must not be able
    // to take the channel down by being quoted back at it.
    protected string Excerpt(string raw)
    {
        return DZMCP_Text.Sanitize(raw, 120);
    }

    protected string FormatSeconds(float seconds)
    {
        float rounded = Math.Round(seconds);
        return rounded.ToString();
    }
}
