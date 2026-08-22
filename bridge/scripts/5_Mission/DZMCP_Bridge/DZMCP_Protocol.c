// The wire contract, expressed as Enforce Script classes.
//
// FIELD NAMES ARE THE WIRE. Enforce's JSON serializer writes a class member
// out under its own name, so renaming a member here silently renames a key on
// the wire. Every name below is matched against the Python reader
// (src/dayz_mcp/bridge/protocol.py) as it stands, not against the prose in any
// document -- three places in the implementation brief still say `session`
// where the reader says `session_id`.
//
// The values that reject the ENTIRE document rather than one field, measured
// against that reader:
//
//   session_id  must be a genuine, non-empty JSON string. "" -- which is what
//               an unset Enforce `string` serializes to, and therefore the
//               single most likely mistake on this side -- is rejected, as are
//               null, 0 and false.
//   tick        must be a genuine JSON integer. "7" and 7.0 are both rejected.
//               Hence `int`, never `float`, and never assembled as text.
//   errors      must be a JSON array; world must be a JSON object.
//   command     if published at all, must carry both id and status, and status
//               must be one of the four below. A missing status rejects the
//               whole snapshot.
//
// Tolerated by that reader, so no effort is spent working around them: unknown
// extra top-level keys are ignored, a null command block is accepted, and
// finished_at may be null or a number.

const string DZMCP_STATUS_IDLE    = "idle";
const string DZMCP_STATUS_RUNNING = "running";
const string DZMCP_STATUS_DONE    = "done";
const string DZMCP_STATUS_FAILED  = "failed";

// Transport file names, inside the server's -profiles directory. "$profile:"
// resolves to exactly that directory, which is where the Python half looks.
const string DZMCP_CMD_PATH   = "$profile:dayz_mcp_cmd.json";
const string DZMCP_STATE_PATH = "$profile:dayz_mcp_state.json";

// The mod's report on the command it currently knows about.
//
// Never published as a null reference (brief R12): an idle bridge publishes
// {id:"", status:"idle", detail:"", finished_at:0}. The reader tolerates a
// null command block, but the SERIALIZER's behaviour on a null ref member is
// untested, and a stand boot is an expensive place to find out.
//
// finished_at is the MOD's own clock -- GetGame().GetTickTime(), seconds since
// the game started -- and not a POSIX timestamp (brief R16). Nothing on the
// Python side may compare it against time.time(); nothing does today, and
// classify_timeout takes its times as explicit arguments precisely so that
// stays true.
class DZMCP_CommandState
{
    string id;
    string status;
    string detail;
    float finished_at;

    void DZMCP_CommandState()
    {
        id = "";
        status = DZMCP_STATUS_IDLE;
        detail = "";
        finished_at = 0;
    }
}

// The world block. Free-form on the Python side (`world: dict`), so this is
// where diagnostics live that the fixed part of the contract has no room for.
//
// Task 5 publishes only what the acceptance observations need to answer
// questions the sources cannot:
//
//   tick_time        GetGame().GetTickTime(), for comparing the publish
//                    counter against the game's own clock over a long uptime
//                    -- the only test of the community report that CallLater
//                    drifts after several hours.
//   handler_entries  how many times the tick handler was ENTERED, as against
//                    publishes. A gap between the two is a tick that died
//                    partway through, which is otherwise invisible.
//   publishes        how many state documents were actually written.
//   commands_claimed how many mailbox files were successfully claimed.
//   errors_total     how many errors were ever recorded, including the ones
//                    the ring has since dropped.
//   pad              deliberate padding, set for exactly ONE publish by the
//                    probe_bloat verb, then cleared. This is how the "does
//                    opening a file for writing truncate it" question gets
//                    answered: a long document followed by a short one, with
//                    the file size sampled from outside. If the size does not
//                    drop, the tail of the long document survives past the new
//                    closing brace and the channel is unreadable PERMANENTLY,
//                    not intermittently -- which is why this is measured
//                    before any variable-length world snapshot exists.
//
// From Task 6 it also carries the world itself, refreshed every tick:
//
//   players          how many are connected. Zero is the ordinary state on a
//                    headless stand, and the reason every verb needing a
//                    player refuses in words rather than doing nothing.
//   player_pos       "x y z", the simple form -- which is exactly what
//                    string.ToVector() reads back, so a position taken from a
//                    snapshot can be handed straight back as an argument.
//                    Empty when nobody is connected.
//   player_health    the first player's health, -1 when nobody is connected
//                    (0 is a real health value; absence needs its own).
//   hands            config class of what the first player is holding, empty
//                    for empty hands or no player.
//   query_class      the last count asked for, and its answer. query_count is
//   query_radius     -1 until a query has run, for the same reason as health:
//   query_count      zero found is a real answer and must not read as "never
//                    asked". This is how "is the item I spawned still there?"
//                    gets answered a minute later.
class DZMCP_WorldSnapshot
{
    float tick_time;
    int handler_entries;
    int publishes;
    int commands_claimed;
    int errors_total;
    string pad;

    int players;
    string player_pos;
    float player_health;
    string hands;
    string query_class;
    float query_radius;
    int query_count;

    // Is the first player's action manager holding action data? 1/0, or -1
    // when there is no player to ask. This is the wedge indicator: a manager
    // that stays at 1 with no action running is a player who can never act
    // again (brief R25/O10), and publishing it every tick is what lets the
    // wedge check be read from outside without a command round trip.
    int action_pending;

    // The world's own clock and weather, refreshed every tick like the player
    // block. Published rather than answered on request for the same reason the
    // player position is: a caller asking "what time is it" should not pay a
    // command round trip for something the mod already knows every second.
    //
    // The date fields are the engine's own five (World.GetDate, out params),
    // and the weather values are GetActual() -- what it IS now, not what it is
    // heading towards, because a forecast reads as a lie to anyone comparing
    // it against the sky.
    int date_year;
    int date_month;
    int date_day;
    int date_hour;
    int date_minute;
    float weather_overcast;
    float weather_rain;
    float weather_fog;
    float weather_wind;

    // The last `entities` listing. Separate from query_count, which counts and
    // nothing else: a caller that wants to know WHICH objects are there cannot
    // get it from a number, and a caller that wants the number should not pay
    // for a list.
    //
    // entities_total is what was actually found; the array holds at most
    // ENTITY_LIST_MAX of them. The two differing is the honest way to say "the
    // list is a page, not the answer" -- a truncated list that did not say so
    // would read as the whole world.
    string entities_class;
    float entities_radius;
    int entities_total;
    ref array<string> entities;

    void DZMCP_WorldSnapshot()
    {
        tick_time = 0;
        handler_entries = 0;
        publishes = 0;
        commands_claimed = 0;
        errors_total = 0;
        pad = "";

        players = 0;
        player_pos = "";
        player_health = -1;
        hands = "";
        query_class = "";
        query_radius = 0;
        query_count = -1;
        action_pending = -1;

        date_year = 0;
        date_month = 0;
        date_day = 0;
        date_hour = 0;
        date_minute = 0;
        weather_overcast = -1;
        weather_rain = -1;
        weather_fog = -1;
        weather_wind = -1;

        entities_class = "";
        entities_radius = 0;
        entities_total = -1;
        entities = new array<string>();
    }
}

// One published snapshot.
//
// tick starts at 1 on the very first publish and grows by exactly one per
// publish (brief R13). Zero is not "no command yet", it is "no readable
// snapshot ever existed" as far as the reader is concerned -- including for
// the very first "here is my session" document.
class DZMCP_State
{
    int tick;
    string session_id;
    ref DZMCP_CommandState command;
    ref array<string> errors;
    ref DZMCP_WorldSnapshot world;

    void DZMCP_State()
    {
        tick = 0;
        session_id = "";
        command = new DZMCP_CommandState();
        errors = new array<string>();
        world = new DZMCP_WorldSnapshot();
    }
}

// Stage one of the mailbox parse: the three fields that decide whether a
// command can be answered AT ALL.
//
// The brief's R6 says stage one carries id and verb only. session_id is here
// as well, deliberately: R9 requires a command from a foreign session to be
// rejected without being executed and without becoming the published command
// block, and that decision has to be reachable on exactly the path stage one
// exists to serve -- when stage two (the args) has failed. Splitting it out
// into a third stage would buy nothing and cost another parse.
//
// Enforce's JSON has no variant type: the documented value set is int, float,
// vector, string, object, array, set and map. One unexpected value under
// `args` can therefore fail the whole document, which is exactly why the
// fields that carry correlation are parsed separately from the ones that
// carry payload.
class DZMCP_CommandEnvelope
{
    string id;
    string verb;
    string session_id;

    void DZMCP_CommandEnvelope()
    {
        id = "";
        verb = "";
        session_id = "";
    }
}

// Stage two: the whole command, args included.
//
// Not derived from the envelope class: inheritance in a serialized type is
// untested against this engine's deserializer, and the four fields are cheaper
// to repeat than to debug on a boot.
//
// args is a map because that is what an object on the wire deserializes into,
// and a map is the only carrier that can answer "was this key present?" as an
// actual question (Contains) rather than inferring it from an empty value.
// That distinction is load-bearing: the deserializer DROPS members a class
// does not declare and still returns success, so a command with a typo in an
// argument name would otherwise report done having done nothing -- the exact
// silent-success shape this tool exists to prevent, and the loud counterpart
// of the unknown-verb refusal.
//
// Pre-created in the constructor so a command with no args at all leaves an
// empty map rather than a null reference.
class DZMCP_CommandFull
{
    string id;
    string verb;
    string session_id;
    ref map<string, string> args;

    void DZMCP_CommandFull()
    {
        id = "";
        verb = "";
        session_id = "";
        args = new map<string, string>();
    }
}
