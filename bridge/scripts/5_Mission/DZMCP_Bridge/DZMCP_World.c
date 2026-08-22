// Engine work for the world verbs, kept apart from the dispatcher so that
// DZMCP_BridgeCore stays about the protocol and this stays about the game.
//
// Every signature below was read in the unpacked game sources this session
// rather than remembered:
//   GetPlayers(out array<Man>)                     3_game/global/game.c:947
//   CreateObjectEx(type, pos, flags, rotation)     3_game/global/game.c:702
//   GetObjectsAtPosition(pos, r, out objs, out cs) 3_game/global/game.c:922
//   ObjectDelete(obj)                              3_game/global/game.c:704
//   Man.GetHumanInventory()                        3_game/entities/man.c:79
//   HumanInventory.CreateInHands(type)             .../humaninventory.c:44
//   EntityAI.GetInventory()                        3_game/entities/entityai.c:1839
//   GameInventory.CreateInInventory(type)          .../inventory.c:876
//   Object.SetPosition / GetPosition               3_game/entities/object.c:300 / 293
//   Object.SetHealth(zone, type, v) / GetHealth    3_game/entities/object.c:1011 / 990
//   Object.GetType() / IsKindOf(type)              3_game/entities/object.c:473 / 517
//   ItemBase.SetQuantity(...) -> bool              4_world/entities/itembase.c:3340
//   vector.ToString(beautify)                      1_core/proto/enconvert.c:21
//   World.GetDate(out y,mo,d,h,mi)                 3_game/global/world.c:33
//   World.SetDate(y, mo, d, h, mi)                 3_game/global/world.c:51
//   Game.GetWeather() -> Weather                   used at 3_game/dayzgame.c:3332
//   Weather.GetOvercast/GetRain/GetFog/GetSnowfall 3_game/weather.c:183-189
//   WeatherPhenomenon.Set(forecast, time, minDur)  3_game/weather.c:22
//   WeatherPhenomenon.GetActual()                  3_game/weather.c:11
//   Weather.SetWindSpeed(speed) / GetWindSpeed()   3_game/weather.c:243, 251
//
// FORMATTING RULE, the same one the dispatcher carries: an Enforce statement
// ends at the end of its line. One statement, one line, however long.
class DZMCP_World
{
    // How far a delete or a count may reach. A radius is an argument that
    // arrives from outside, and "delete every object of this class" with an
    // unbounded radius is one typo away from emptying the map.
    static const float RADIUS_MAX = 500.0;
    static const float RADIUS_DEFAULT = 30.0;

    // How many objects a single count or delete will walk. GetObjectsAtPosition
    // returns everything in the sphere, and at a large radius on a populated
    // map that is thousands of entries -- inside the once-a-second call, where
    // the whole tick has to finish before the next one starts.
    static const int SCAN_MAX = 2000;

    // The first player on the server, or null when nobody is connected.
    //
    // "Nobody is connected" is a real, common state on this stand -- a headless
    // server with no client attached has no players at all -- and every verb
    // that needs one has to say so in words rather than quietly doing nothing.
    // This project has already lost time to an action that never appeared
    // because the hands it needed were empty.
    static Man FirstPlayer()
    {
        array<Man> players = new array<Man>;
        GetGame().GetPlayers(players);
        if (players.Count() == 0)
            return null;

        return players.Get(0);
    }

    static int PlayerCount()
    {
        array<Man> players = new array<Man>;
        GetGame().GetPlayers(players);
        return players.Count();
    }

    // "x y z", the simple form -- which is exactly what string.ToVector()
    // reads back, so a position published in a snapshot can be handed straight
    // back as an argument. ToString(true) would produce "<x, y, z>" and would
    // need BeautifiedToVector to survive the round trip.
    static string PosToText(vector v)
    {
        return v.ToString(false);
    }

    // Parse "x y z" into a vector, refusing anything that is not three numbers.
    //
    // string.ToVector() cannot be trusted on its own here: it answers "0 0 0"
    // for a string it could not read, which is a legal position on this map's
    // corner and therefore indistinguishable from a real one. A teleport to
    // 0 0 0 because an argument was misspelled is exactly the silent wrong
    // answer this whole tool exists to prevent, so the text is validated
    // first and the parse only happens once it is known to be three numbers.
    static bool TextToPos(string text, out vector result)
    {
        result = "0 0 0";

        array<string> parts = new array<string>;
        text.Split(" ", parts);

        int seen = 0;
        for (int i = 0; i < parts.Count(); i++)
        {
            string part = parts.Get(i);
            if (part == "")
                continue;
            if (!IsNumeric(part))
                return false;
            seen++;
        }

        if (seen != 3)
            return false;

        result = text.ToVector();
        return true;
    }

    static bool IsNumeric(string text)
    {
        int len = text.Length();
        if (len == 0)
            return false;

        int digits = 0;
        for (int i = 0; i < len; i++)
        {
            string ch = text.Get(i);
            int code = ch.ToAscii();
            if (code >= 48 && code <= 57)
            {
                digits++;
                continue;
            }
            if (ch == "-" || ch == "+" || ch == "." || ch == "e" || ch == "E")
                continue;

            return false;
        }
        return digits > 0;
    }

    // Create on the ground.
    //
    // ECE_PLACE_ON_SURFACE is physics, navmesh and a trace onto the surface --
    // and NOTHING else; it does not include ECE_SETUP, and it says nothing
    // about lifetime. Without ECE_NOLIFETIME the item lives by the lifetime in
    // its own config and the central economy is free to clean it up partway
    // through a check, which turns "the test item vanished" into a mystery
    // about the mod under test. Both flags, always, for anything spawned by
    // this bridge. (The claim in another project's code that ECE_NOLIFETIME
    // disables persistence is wrong -- the source says the opposite.)
    static Object SpawnOnGround(string className, vector pos)
    {
        int flags = ECE_PLACE_ON_SURFACE | ECE_NOLIFETIME;
        return GetGame().CreateObjectEx(className, pos, flags);
    }

    static EntityAI SpawnInHands(Man player, string className)
    {
        HumanInventory inventory = player.GetHumanInventory();
        if (!inventory)
            return null;

        return inventory.CreateInHands(className);
    }

    static EntityAI SpawnInInventory(Man player, string className)
    {
        GameInventory inventory = player.GetInventory();
        if (!inventory)
            return null;

        return inventory.CreateInInventory(className);
    }

    // Everything of `className` within `radius` of `pos`, capped.
    //
    // A real player is never returned, whatever the class filter says --
    // deleting the only player on the stand would end the session the caller is
    // measuring, and no argument to a delete verb is worth that outcome.
    static void Gather(string className, vector pos, float radius, out array<Object> found)
    {
        found = new array<Object>;

        array<Man> players = new array<Man>;
        GetGame().GetPlayers(players);

        array<Object> objects = new array<Object>;
        array<CargoBase> cargos = new array<CargoBase>;
        GetGame().GetObjectsAtPosition(pos, radius, objects, cargos);

        int limit = objects.Count();
        if (limit > SCAN_MAX)
            limit = SCAN_MAX;

        for (int i = 0; i < limit; i++)
        {
            Object candidate = objects.Get(i);
            if (!candidate)
                continue;

            if (IsProtectedPerson(candidate, players))
                continue;

            if (className != "" && !candidate.IsKindOf(className))
                continue;

            found.Insert(candidate);
        }
    }

    // Is this object a person the delete verb must never touch?
    //
    // TWO independent tests, and either one alone is enough to protect a real
    // player -- which is the point, because the cost of being wrong here is the
    // session the caller is measuring.
    //
    // An earlier version simply skipped every Man. That was safe and also too
    // broad, and the live run showed why: a survivor created by the spawn verb
    // is a Man, is NOT counted by GetPlayers (measured: players stayed 0 with
    // one standing in the world), and could therefore be created by this bridge
    // and then removed by nothing at all. A tool that can make something it
    // cannot unmake leaves litter in every stand it touches.
    //
    // GetIdentity is the first test and the load-bearing one: a connected
    // player always has an identity, an entity conjured by CreateObjectEx never
    // does. Membership of GetPlayers is the second, so that a momentary empty
    // player list -- mid-connection, mid-disconnect -- cannot expose a real
    // player to a class filter that happens to match their character.
    // The date ranges the engine documents, checked here rather than passed
    // through: SetDate takes month 1-12, day 1-31, hour 0-23, minute 0-59, and
    // a value outside those is undefined behaviour in native code, which is the
    // one kind of failure this bridge cannot report on.
    static bool DateInRange(int month, int day, int hour, int minute, out string why)
    {
        why = "";
        if (month < 1 || month > 12)
            why = "month must be 1-12, not " + month;
        else if (day < 1 || day > 31)
            why = "day must be 1-31, not " + day;
        else if (hour < 0 || hour > 23)
            why = "hour must be 0-23, not " + hour;
        else if (minute < 0 || minute > 59)
            why = "minute must be 0-59, not " + minute;
        return why == "";
    }

    static string DateToText(int year, int month, int day, int hour, int minute)
    {
        return "" + year + "-" + Pad2(month) + "-" + Pad2(day) + " " + Pad2(hour) + ":" + Pad2(minute);
    }

    static string Pad2(int value)
    {
        if (value >= 0 && value < 10)
            return "0" + value;
        return "" + value;
    }

    // The phenomenon a weather verb names, or null when the name is not one.
    // Wind is deliberately NOT here: it is set through Weather itself, not
    // through a phenomenon, and returning null for it lets the caller say so.
    static WeatherPhenomenon Phenomenon(string what)
    {
        Weather weather = GetGame().GetWeather();
        if (!weather)
            return null;
        if (what == "overcast")
            return weather.GetOvercast();
        if (what == "rain")
            return weather.GetRain();
        if (what == "fog")
            return weather.GetFog();
        if (what == "snowfall")
            return weather.GetSnowfall();
        return null;
    }

    // One line per object: class, position, distance from the origin, health.
    // A flat string per entry rather than a nested object, because the world
    // block travels as free-form JSON and an array of strings is the one shape
    // whose serialization this bridge has already proven on the wire (errors).
    //
    // The separator is `|`: it cannot appear in a DayZ config class name, and a
    // position printed by PosToText has no bars in it either.
    // Fills the array it is GIVEN rather than making one: the destination is a
    // `ref` member of the published state, and every other array in that
    // document is mutated in place. Replacing a ref member would be a second
    // way of doing one thing, and the lifetime of the old array is the kind of
    // question this project does not answer by guessing.
    static void Describe(array<Object> found, vector origin, int limit, array<string> lines)
    {
        int count = found.Count();
        if (limit < count)
            count = limit;
        for (int i = 0; i < count; i++)
        {
            Object item = found.Get(i);
            if (!item)
                continue;
            vector at = item.GetPosition();
            // HORIZONTAL distance, deliberately. The engine's own radius test
            // in GetObjectsAtPosition ignores height, so a straight-line
            // distance disagrees with the radius the caller asked for: at the
            // centre of Chernarus, objects the engine returned for a 150 m
            // radius are 320 m away in three dimensions, because the terrain
            // there is 300 m up and the caller wrote "7500 0 7500". Measured,
            // not assumed -- and reporting a number that contradicts the
            // filter that produced it is how a tool teaches an agent to
            // distrust it.
            vector flat = at - origin;
            flat[1] = 0;
            float away = flat.Length();
            lines.Insert(item.GetType() + "|" + PosToText(at) + "|" + away + "|" + item.GetHealth("", ""));
        }
    }

    static bool IsProtectedPerson(Object candidate, array<Man> players)
    {
        Man asMan;
        if (!Class.CastTo(asMan, candidate))
            return false;

        if (asMan.GetIdentity())
            return true;

        return players.Find(asMan) >= 0;
    }

    static float ClampRadius(float radius)
    {
        if (radius <= 0)
            return RADIUS_DEFAULT;
        if (radius > RADIUS_MAX)
            return RADIUS_MAX;

        return radius;
    }
}
