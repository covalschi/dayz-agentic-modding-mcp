// Logging discipline for the bridge.
//
// The rule that shapes this file: our own log verdict scans the server's
// script log for the whole words ERROR and FATAL, case-insensitively, and
// classifies any line carrying one as a failed boot -- checked BEFORE the
// noise filter, so it cannot be excused as noise. Two consequences, and they
// pull in opposite directions:
//
//   * No informational line may contain either word. A world command correctly
//     reporting "the mod's own conditions did not hold" is a SUCCESSFUL test;
//     if that line turned every negative test into a red boot verdict, the
//     tool would be unable to report its most valuable answer. Info() masks
//     both words -- including in text that arrived from outside, which is the
//     only way a bridge line can pick one up by accident.
//
//   * A genuine bridge malfunction -- the state file will not open, the
//     document will not serialize, the mailbox cannot be deleted -- SHOULD
//     turn the verdict red, because in that state nothing the bridge reports
//     can be trusted. Fault() is that path, and it deliberately does not mask.
//
// Fault() uses Print with the word spelled out rather than ErrorEx or Error.
// Both of those are documented to raise a message box above INFO severity
// (endebug.c:68-90; Error is literally Error2("", err), a message box). A
// modal dialog on a headless dedicated server is a hung stand and a wasted
// boot, and the verdict only ever reads the text of the line -- so spelling
// the word into a Print buys the exact same red verdict with none of that
// risk. See the task report for this trade being a deliberate deviation.
class DZMCP_Log
{
    static const string PREFIX = "[DZMCP_Bridge] ";

    // An ordinary line about what the bridge did. Safe to hand outside text:
    // it is sanitized to printable ASCII and the two verdict words are broken.
    static void Info(string message)
    {
        string safe = DZMCP_Text.Sanitize(message, 900);
        Print(PREFIX + DZMCP_Text.MaskVerdictWords(safe));
    }

    // The bridge itself is broken. Turns the boot verdict red on purpose.
    // Never call this for a command that legitimately failed -- that is a
    // result, not a malfunction.
    static void Fault(string message)
    {
        string safe = DZMCP_Text.Sanitize(message, 900);
        Print(PREFIX + "ERROR " + DZMCP_Text.MaskVerdictWords(safe));
    }
}
