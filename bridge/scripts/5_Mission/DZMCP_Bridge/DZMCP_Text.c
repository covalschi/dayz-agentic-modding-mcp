// String hygiene for the bridge.
//
// Two rules drive every function here, and both are about the READER, not
// about tidiness:
//
//  1. The Python side reads the state file as strict UTF-8 and treats a
//     decoding failure as "unreadable" -- not "this one field is bad", but the
//     WHOLE snapshot, for as long as the offending byte keeps being written.
//     Measured on the Python half: a BOM, any non-UTF-8 byte anywhere, and any
//     byte after the closing brace each make the entire document unreadable.
//     Until the encoding of the Enforce write path has been measured on a live
//     stand, every string this mod publishes stays printable ASCII -- Sanitize
//     is what guarantees that for text this mod did not author (the raw
//     mailbox contents, a verb name, an argument value).
//
//  2. Our own log verdict classifies any line matching the whole words ERROR
//     or FATAL (case-insensitively) as a boot failure. An informational line
//     that happens to quote outside text containing one of those words would
//     paint a perfectly healthy boot -- including a CORRECT negative test
//     result -- as broken. MaskVerdictWords is what keeps informational lines
//     informational; genuine bridge malfunctions deliberately do NOT go
//     through it, so the verdict goes red when it deserves to.
class DZMCP_Text
{
    // A copy of `raw` that is safe to publish: printable ASCII only, at most
    // `maxLen` bytes.
    //
    // Byte-wise on purpose. Enforce's string.Length()/Get()/Substring() are
    // byte-based (SubstringUtf8/LengthUtf8 exist separately for the character
    // -based versions), so walking with Get(i) inspects individual bytes and
    // any byte of a multi-byte sequence is replaced -- which is exactly the
    // guarantee wanted here, because a multi-byte character split by the
    // length cap would otherwise leave a truncated UTF-8 sequence that makes
    // the whole document undecodable.
    static string Sanitize(string raw, int maxLen)
    {
        int len = raw.Length();
        if (len > maxLen)
            len = maxLen;

        string buf = "";
        for (int i = 0; i < len; i++)
        {
            string ch = raw.Get(i);
            int code = ch.ToAscii();
            if (code >= 32 && code <= 126)
                buf = buf + ch;
            else
                buf = buf + " ";
        }
        return buf;
    }

    // Break the two words the log verdict treats as a failure, so a line that
    // quotes outside text cannot fail a boot that succeeded. Case-insensitive,
    // because the verdict's own match is.
    //
    // Deliberately NOT a string.Replace() call: Replace is case-sensitive, and
    // the words arrive from outside in whatever case the sender chose.
    static string MaskVerdictWords(string s)
    {
        string lower = s;
        lower.ToLower();

        int len = s.Length();
        string buf = "";
        int i = 0;
        while (i < len)
        {
            if (i + 5 <= len)
            {
                string window = lower.Substring(i, 5);
                if (window == "error")
                {
                    buf = buf + "err_r";
                    i = i + 5;
                    continue;
                }
                if (window == "fatal")
                {
                    buf = buf + "fat_l";
                    i = i + 5;
                    continue;
                }
            }
            buf = buf + s.Get(i);
            i++;
        }
        return buf;
    }

    // Pull a JSON string value out of raw text by key, WITHOUT a parser.
    //
    // This exists for exactly one job: recovering a command's id when the
    // parser could not (or when it succeeded but left the id empty, which is
    // at least as common -- fields are pre-created, so a partial document
    // yields an empty value rather than a parse failure). Without a recovered
    // id no failure can ever be correlated back to the caller that is waiting
    // for it, and an uncorrelatable failure is precisely the silent timeout
    // this whole tool exists to abolish.
    //
    // Returns "" when the key is absent or its value is not a string.
    static string ExtractJsonString(string raw, string key)
    {
        string needle = "\"" + key + "\"";
        int at = raw.IndexOf(needle);
        if (at < 0)
            return "";

        int len = raw.Length();
        int i = at + needle.Length();

        // skip whitespace, then require the colon
        while (i < len && IsSpace(raw.Get(i)))
            i++;
        if (i >= len || raw.Get(i) != ":")
            return "";
        i++;

        // skip whitespace, then require the opening quote
        while (i < len && IsSpace(raw.Get(i)))
            i++;
        if (i >= len || raw.Get(i) != "\"")
            return "";
        i++;

        string buf = "";
        while (i < len)
        {
            string ch = raw.Get(i);
            if (ch == "\\")
            {
                // One level of unescaping only: enough to step over an escaped
                // quote so the value's end is found correctly. A command id
                // that needs more than this is one the Python side should
                // never have minted (see the report: the verb charset wants
                // restricting on that side).
                i++;
                if (i >= len)
                    break;
                buf = buf + raw.Get(i);
                i++;
                continue;
            }
            if (ch == "\"")
                break;
            buf = buf + ch;
            i++;
        }
        return buf;
    }

    // Tab and carriage return are matched by ASCII code rather than by "\t"
    // and "\r" literals. The escapes this file does use -- \" and \\ and \n --
    // all appear in the vanilla sources and are therefore known to lex; \t and
    // \r appear nowhere in them, and an escape the lexer does not know is a
    // compile failure discovered six minutes into a boot.
    static bool IsSpace(string ch)
    {
        if (ch == " " || ch == "\n")
            return true;

        int code = ch.ToAscii();
        return code == 9 || code == 13;
    }

    // A string of `count` copies of `unit`, built by doubling rather than by
    // appending one at a time -- O(log n) concatenations instead of O(n), and
    // this is called with counts in the thousands.
    static string Repeat(string unit, int count)
    {
        if (count <= 0 || unit.Length() == 0)
            return "";

        string buf = unit;
        while (buf.Length() < count)
            buf = buf + buf;

        return buf.Substring(0, count);
    }

    // Zero-padded two-digit rendering of a small non-negative number, for the
    // session token. Kept explicit rather than relying on any format-width
    // support in string.Format, which Enforce does not have.
    static string Pad2(int value)
    {
        if (value < 0)
            value = 0;
        if (value < 10)
            return "0" + value.ToString();

        return value.ToString();
    }
}
