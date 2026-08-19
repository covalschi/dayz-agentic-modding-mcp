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
            Malfunction("the mailbox exists but could not be opened for reading");
            return;
        }

        string raw;
        ReadFile(handle, raw, READ_LIMIT);
        CloseFile(handle);

        // Checked, not assumed. An unclaimed mailbox that stays put would be
        // executed again on every tick from here to the end of the session.
        // Bailing out without parsing leaves the file for the next tick to
        // try again, which is recoverable; executing it repeatedly is not.
        if (!DeleteFile(DZMCP_CMD_PATH))
        {
            Malfunction("the mailbox was read but could not be deleted -- the command is NOT claimed" + " and nothing was executed; the next tick will try again");
            return;
        }

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

    protected string KnownVerbs()
    {
        return "ping, probe_bloat, probe_stall, probe_fault";
    }

    protected bool IsKnownVerb(string verb)
    {
        return verb == "ping" || verb == "probe_bloat" || verb == "probe_stall" || verb == "probe_fault";
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
    protected void VerbFault(map<string, string> args)
    {
        if (RefuseUnknownArgs(args, "|", "(none)"))
            return;

        DZMCP_Log.Info("command " + m_State.command.id + " will raise a deliberate script fault; the next tick reports what survived");

        m_NeverAssigned.status = "this line dereferences a null reference on purpose";
    }

    // -----------------------------------------------------------------------
    // Publishing
    // -----------------------------------------------------------------------

    protected void Publish()
    {
        m_Tick++;

        m_State.tick = m_Tick;
        m_State.session_id = m_SessionId;

        m_State.world.tick_time = GetGame().GetTickTime();
        m_State.world.handler_entries = m_HandlerEntries;
        m_State.world.publishes = m_Publishes;
        m_State.world.commands_claimed = m_CommandsClaimed;
        m_State.world.errors_total = m_ErrorsTotal;
        m_State.world.pad = m_PadNext;
        m_PadNext = "";

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
            Malfunction("the state file could not be opened for writing");
            return;
        }
        FPrint(handle, text);
        CloseFile(handle);

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

    // A short, safe excerpt of text that arrived from outside, for quoting
    // into an error entry. Sanitized (printable ASCII only) because one
    // non-UTF-8 byte anywhere in the document makes the WHOLE document
    // unreadable to the Python side, permanently, for as long as it keeps
    // being written -- a mailbox full of some other encoding must not be able
    // to take the channel down by being quoted back.
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
