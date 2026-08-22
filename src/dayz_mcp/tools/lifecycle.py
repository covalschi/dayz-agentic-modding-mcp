from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from ..bridge.channel import CMD_FILENAME, STATE_FILENAME
from ..compilecheck import client_cmd, judge
from ..errors import Result, fail, ok
from ..jobs import QUEUED, RUNNING
from ..paths import GAME_PROBE
from ..procs import is_alive, process_mods_tail, spawn, stop, udp_port_holders
from . import session
from .project import require_project

# server_status pulses the newest log twice this many seconds apart to see whether
# it is actually growing. Kept small and capped so the tool is a quick health check,
# not a disguised long wait -- long operations belong behind a job_id.
STATUS_PULSE_MAX = 10.0

# The executable server_start spawns -- also the vanilla probe file paths.py
# uses to recognise a game install, hence the shared import rather than a
# second copy of the literal.
SERVER_IMAGE = GAME_PROBE

# The diagnostic client gets its own throwaway -profiles directory, one per
# client-compile job, so a compile check never writes into (or reads from) the
# test stand the server boots against.
CLIENT_PROFILE_DIRNAME = "clientprofile"

# Launch arguments server_start owns: it computes them, and its preflight, log
# discipline and mod split all assume they are what it computed. The engine
# honours the LAST occurrence of a repeated argument, so an extra repeating one
# of these would silently override the tool's own -- refused instead, with the
# profile named as the right place to change them.
OWNED_LAUNCH_ARGS = ("-config", "-profiles", "-port", "-mod", "-serverMod")

# How long the no-ready-line path waits for the game port to be bound before
# giving up on that signal and answering honestly. Bounded on purpose and
# deliberately NOT `timeout`: the defect this branch exists to fix was waiting
# the full timeout for a signal that would never come. Measured against the
# real thing -- this project's stand binds udp/2302 16.9s after spawn, by the
# pid we spawned -- so a server that is going to bind has done it long before
# this, and one that has not is answered rather than waited on.
PORT_READY_WAIT_SECONDS = 90.0

# How long server_start lets a server settle before calling it started, when
# the profile declares no ready line and there is therefore nothing to wait
# for. Long enough that a boot which dies on its own command line is reported
# as dead rather than as started, short enough to stay a prompt answer. This
# is emphatically NOT a substitute readiness signal: a server still compiling
# its world is alive and not ready, and this configuration cannot tell the
# difference -- which is exactly what the job summary then says.
NO_READY_LINE_SETTLE_SECONDS = 3.0


def _stand() -> Path:
    prof = session.profile()
    return Path(prof.machine.stand_root or prof.root / "testenv")


def server_profiles_dir() -> Path:
    """The -profiles directory the server boots against.

    THE definition of that location. server_start passes it on the command
    line, server_status reads the log it collects, and the bridge tools read
    the state file the mod writes into it ($profile: resolves to exactly this
    directory inside the game). Computing it separately in each of those places
    is how the client-side log location ended up with two disagreeing owners
    (see client_profile_dir) -- one formula, one owner.
    """
    return _stand() / "profiles"


def _is_within(path: Path, base: Path) -> bool:
    """True if `path` is `base` or lives underneath it.

    Used to refuse a server config file that resolves (possibly through a
    symlink) outside machine.stand_root -- see the -config note on server_start.
    """
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def mod_list(profiles_extra: str = "") -> tuple[str, str]:
    """Split the profile's configured mods into (-mod, -serverMod) path strings.

    A mod is routed to -serverMod when its folder name is listed in
    mods.server_only; everything else goes to -mod. A mod in the wrong list
    breaks client connections without a single readable line in the log, so the
    split is made here, once, instead of being decided ad hoc by each caller.
    """
    prof = session.profile()
    game = session.game() or ""
    server_only = set(prof.mods.server_only)

    parts = [str(Path(game) / "!Workshop" / m) for m in prof.mods.required]
    parts += [str(Path(prof.root) / m) for m in prof.own_mod_dirs]
    parts += list(prof.mods.extra)
    if profiles_extra:
        parts.append(profiles_extra)
    parts = [p for p in parts if p]

    client_mods = [p for p in parts if Path(p).name not in server_only]
    server_mods = [p for p in parts if Path(p).name in server_only]
    return ";".join(client_mods), ";".join(server_mods)


def _newest(folder: Path, pattern: str) -> Path | None:
    items = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return items[0] if items else None


def client_profile_dir(job_id: str) -> Path:
    """Where the diagnostic client for `job_id` keeps its profile (and so its
    logs). THE definition of that location -- client_compile_check spawns the
    client against it and tools/logs.py reads it back through
    newest_client_profile below, so neither side computes the path itself.

    They did compute it independently once, and disagreed: the client was run
    against the job's artifacts while log_verdict(source="client") looked under
    machine.stand_root, a directory nothing ever creates. Both client-side log
    tools failed for every project, and their hint sent the user to change
    stand_root -- which would not have helped, because stand_root was never
    involved. The recurrence is what this function exists to prevent; the
    symptom was only the visible half.
    """
    return session.jobs().artifacts_dir(job_id) / CLIENT_PROFILE_DIRNAME


def newest_client_profile() -> Path | None:
    """The profile directory of the LATEST client compile check, or None if
    this project has never run one.

    Strictly the latest, even when that run produced no log at all (it died
    before spawning the client, say) -- deliberately not "the latest one that
    happens to hold a log", which would answer a question about this run with
    the previous run's output. That is the same mistake `since` exists to
    prevent on the server side, and it is worse here: nothing in the reply
    would say the log belongs to an older run. An empty directory therefore
    reads as "no client log found", which is the truth.
    """
    store = session.jobs()
    if store is None:
        return None
    checks = [j for j in store.all() if j.kind == "client-compile"]
    if not checks:
        return None
    return client_profile_dir(max(checks, key=lambda j: j.started).id)


def clear_bridge_transport(profiles: Path) -> list[str]:
    """Remove the bridge's two transport files before a boot. Returns what
    could not be removed (empty on success).

    WHY THESE TWO ARE CLEARED WHILE script_*.log IS DELIBERATELY NOT -- the
    asymmetry is intentional and reads like an oversight otherwise:

    A log is a RECORD of a run that already happened. Deleting one destroys
    evidence, a live server holds it open on Windows (so the unlink raises),
    and nothing needs it gone: `since` already tells this run's log from an
    earlier one. server_start leaves them exactly where they are.

    These two are not records, they are INSTRUCTIONS AND STATE for the run
    about to start, and the -profiles directory is reused across restarts:

      * a command left in the mailbox while the stand was down is not stale
        data, it is a pending action -- it executes at the first tick of a
        world the agent believes untouched;
      * the wedge it causes otherwise survives every restart, so the one
        escape hatch has to be used again and again;
      * a leftover state file keeps the published tick large and positive, so
        bridge_status's `no_state_file` answer -- the only one whose hint says
        to build the bridge and wire it into -serverMod -- becomes unreachable
        for the entire life of the stand directory;
      * the mod's counter restarts at 0 each boot while the file keeps the old
        number, so the published tick goes DOWN across a restart.

    `session_id` defends against the first and last of those from the other
    side. This is the other layer: the file should not be there at all.

    Nothing holds either file open at the moment this runs -- server_start
    refuses outright while its own server is alive -- but a scanner or an
    editor can, so failures are collected and returned rather than raised.
    Clearing is hygiene, never a precondition: refusing to boot over a json
    that would not delete is a worse trade than booting with it.
    """
    problems: list[str] = []
    for name in (CMD_FILENAME, STATE_FILENAME):
        leftover = profiles / name
        try:
            leftover.unlink(missing_ok=True)
        except OSError as exc:
            problems.append(f"{leftover}: {exc}")
    return problems


#: How the server config names the mission to load, e.g.
#: `template="dayzOffline.chernarusplus";` inside class Missions/class DayZ.
_TEMPLATE_RE = re.compile(r"""template\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def mission_template(config: Path) -> str:
    """The mission the server config asks for, or "" if it names none.

    Read with one pattern rather than by parsing the whole config: the only
    fact wanted here is which folder under `mpmissions` has to exist, and a
    config this cannot read must not block a boot that works today.
    """
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    found = _TEMPLATE_RE.search(text)
    return found.group(1).strip() if found else ""


def missing_mission(game: Path, config: Path) -> str:
    """The mission the engine will not find, or "" when there is nothing wrong.

    THE BOOT FAILURE THAT LOOKS LIKE A SUCCESS. This tool launches the
    DIAGNOSTIC EXECUTABLE OUT OF THE CLIENT INSTALL, and the engine resolves
    `mpmissions` next to the executable it is running -- not next to the
    -config it was handed. A machine whose missions live in the separate
    DayZServer install therefore starts a server that binds its port, logs not
    one error, passes the verdict, and then refuses every player with a single
    line: "Mission script has no main function, player connect will stay
    disabled!".

    Found by another session using this server, on a machine that did not
    happen to have a copy of the mission under the client install. The machine
    this was written on does have one, which is the only reason it never
    surfaced here.
    """
    template = mission_template(config)
    if not template:
        return ""
    return "" if (game / "mpmissions" / template).is_dir() else template


#: The engine's own statement that it has compiled the module a mod's mission
#: scripts live in. Written by every DayZ server, needs no mod and no declared
#: line -- the same properties that made the port bind worth watching, and
#: unlike the port it says the SCRIPTS are up.
MISSION_MODULE_LINE = "Module: Mission"


def mission_module_compiled(profiles: Path, since: float) -> bool:
    """Has THIS run compiled its mission module yet?

    Measured on this machine: the port binds about 17 s after spawn and the
    mission module compiles about 25 s after it. A boot called ready at the
    port bind has not compiled one line of the mod, and a verdict taken at that
    moment sees a log with no errors and says "pass". That is how a stand whose
    mission the engine could not find -- reported by another session -- passed
    every check while refusing every player.

    Only logs written by this run are read, by the same `since` cutoff
    log_verdict uses, so a previous boot's log cannot answer for this one.
    """
    for log in profiles.glob("script_*.log"):
        try:
            if log.stat().st_mtime < since:
                continue
            if MISSION_MODULE_LINE in log.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            # A log being written while it is read is ordinary; the next poll
            # gets it.
            continue
    return False


#: The key that signs the GAME's own pbos. It ships with the DayZServer
#: install, not with the client -- which is the whole problem below.
VANILLA_KEY = "dayz.bikey"

_VERIFY_RE = re.compile(r"verifySignatures\s*=\s*(\d+)", re.IGNORECASE)


def verify_signatures(config: Path) -> int | None:
    """The stand's signature policy, or None when its config states none."""
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = _VERIFY_RE.search(text)
    return int(found.group(1)) if found else None


def unsigned_pbo(mod_dir: Path) -> str:
    """The first pbo in a mod folder with no signature beside it, or "".

    A signature is a sibling file: `addons/x.pbo` next to
    `addons/x.pbo.<key>.bisign`. Checked by name rather than by content,
    because the question here is only whether the mod was signed at all.
    """
    addons = mod_dir / "addons"
    if not addons.is_dir():
        return ""
    for pbo in sorted(addons.glob("*.pbo")):
        if not any(addons.glob(pbo.name + ".*.bisign")):
            return pbo.name
    return ""


def signature_problem(game: Path, config: Path, client_mods: str) -> str:
    """Why every client will be rejected by this stand, or "" when none will.

    ONLY under `verifySignatures = 2`. A stand that turns signature checking
    off is a stand where none of this applies, and a check that fired anyway
    would block every local stand that deliberately turns it off.

    THE KEYRING IS THE CLIENT INSTALL'S. This tool launches the diagnostic
    executable out of the client install, so the engine reads `keys` beside
    THAT executable -- while `dayz.bikey`, which signs the game's own pbos,
    ships only with the separate DayZServer install. A keys folder that is
    missing, or present without that key, leaves the server unable to verify
    anything at all.

    And the engine says none of this out loud. It rejects the client with code
    118 and "missing dta\bin.pbo" -- a vanilla FILE NAME, with no mention of
    signatures. Another session lost a long session to that message, and it
    named a file that was byte-identical on both sides the whole time.
    """
    if verify_signatures(config) != 2:
        return ""

    keys = game / "keys"
    if not keys.is_dir():
        return (
            f"this stand runs with verifySignatures = 2, and the keyring the server will "
            f"read is {keys} -- which does not exist. The engine resolves `keys` beside the "
            f"executable it runs, and this tool runs the client install's diagnostic "
            f"executable, while {VANILLA_KEY} ships with the DayZServer install"
        )
    if not (keys / VANILLA_KEY).is_file():
        held = ", ".join(sorted(p.name for p in keys.glob("*.bikey"))) or "nothing"
        return (
            f"this stand runs with verifySignatures = 2, and {keys} holds {held} but not "
            f"{VANILLA_KEY} -- the key that signs the GAME's own pbos. Without it the "
            f"server cannot verify vanilla either"
        )

    for entry in [p for p in client_mods.split(";") if p.strip()]:
        bad = unsigned_pbo(Path(entry))
        if bad:
            return (
                f"this stand runs with verifySignatures = 2, and {bad} in {entry} has no "
                f".bisign beside it -- an unsigned mod on the client's -mod line is a "
                f"client the stand will reject"
            )
    return ""


SIGNATURE_HINT = (
    "the engine does not say 'signature' when it refuses: it answers code 118 and names a "
    "vanilla pbo, which sends the reader hunting for a corrupt game install. Put a complete "
    "keyring beside the executable being run -- a directory symlink to the DayZServer "
    "install's own keys folder is the usual way (mklink /D) -- and add each mod's .bikey to "
    "it. Or set verifySignatures = 0 in the stand's config, which is what a local stand "
    "usually wants"
)


def boot_in_flight() -> str:
    """The id of a boot job that is queued or running for this project, or "".

    A server that is STARTING is not a server that is absent, and answering
    "there is nothing to act on" for one is the same silent lie as reporting
    "frozen" where the honest answer was "could not measure". Measured: three
    live runs called a world tool straight after server_start and were told no
    server existed while one was coming up.

    The newest is the one named -- a project cannot usefully have two boots in
    flight (server_start refuses a second one), so the newest is the one the
    caller just asked for.
    """
    try:
        jobs = session.jobs()
    except Exception:  # noqa: BLE001 - a refusal must not become an exception
        return ""
    live = [j for j in jobs.all() if j.kind == "boot" and j.status in (QUEUED, RUNNING)]
    return live[-1].id if live else ""


def server_start(timeout: float = 420, extra_args: list[str] | None = None) -> Result:
    """Start the test server and wait for it to be ready. Returns a job id.

    Two things worth knowing before calling, both observable:

    It CLEARS THE BRIDGE TRANSPORT first -- the command mailbox and the state
    file in the -profiles directory are removed before the server is spawned, so
    no world ever starts against a command or a state document left by an
    earlier one. Script logs are deliberately left alone; see
    clear_bridge_transport for why the two are treated differently. A file that
    could not be removed is reported in `bridge_transport_left` and on the job,
    and never fails the boot.

    It REFUSES if the game port is already held, naming the pids holding it. A
    stand is shared -- one machine, one port, one profile directory -- and
    booting into a held port produces a server that dies during world load with
    nothing in its own log to say why. If the holder is a server this session
    started, the hint says to stop it with server_stop. If it is anyone else's,
    the refusal identifies it (pid and -mod= tail) and offers stopping it as the
    caller's own act -- the owner authorised stopping a neighbouring stand that
    blocks a live run -- but this tool never auto-stops a process it did not
    start.

    `extra_args` appends launch arguments after the fixed ones -- an explicit
    one-run opt-in, the same pattern as attaching the bridge, not profile
    surgery. A list of strings, never one string to re-split. Arguments the
    tool itself owns (-config, -profiles, -port, -mod, -serverMod) are refused:
    the profile is where those are decided. The extras are recorded in the boot
    job's summary, so a later reader can see the boot was non-standard. The
    known use is the engine's action log (`-doScriptLogs=1 -logToFile=1`),
    which writes to scriptExt.log -- a file log_verdict never reads, so these
    flags cannot poison a verdict.

    Readiness has two independent signals, and the summary always names which
    one answered. `expect.ready_line` appearing in a log written by THIS run
    says the MOD finished loading. The game port being bound by the pid we
    spawned says the ENGINE is up and listening -- which needs neither a mod nor
    a declared line, and is the readiness verdict for a project that declares
    none (measured on a real stand: bound 16.9s after spawn). With a ready line
    declared it remains the verdict, since a bound port cannot say a mod loaded;
    the port is then what tells "the boot failed" apart from "the server is
    listening and it is the mod's line that never appeared".
    """
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()

    if extra_args:
        # A single string would have to be re-split, and quoting rules are
        # exactly the kind of thing two halves disagree about.
        if isinstance(extra_args, str) or not all(isinstance(a, str) for a in extra_args):
            return fail(
                "extra_args must be a list of strings",
                hint='pass each argument separately, e.g. ["-doScriptLogs=1", "-logToFile=1"] '
                     "-- a single string would have to be re-split, and quoting rules are "
                     "where that goes wrong",
            )
        for arg in extra_args:
            head = arg.split("=", 1)[0].lower()
            owned = next((o for o in OWNED_LAUNCH_ARGS if head == o.lower()), None)
            if owned:
                return fail(
                    f"extra_args may not carry {owned}: this tool computes it, and the engine "
                    "would honour the extra one instead of the tool's own",
                    hint=f"{owned} is decided by the profile (dayz-mcp.toml / "
                         "dayz-mcp.local.toml) -- change it there, where every check that "
                         "depends on it will see the same value",
                )

    # A second boot on top of a live one would fight the first for the same port
    # and the same profiles directory instead of failing loudly. Checked against
    # the recorded image, not just the pid -- see is_alive's docstring.
    running_pid = session.server_pid()
    if running_pid and is_alive(running_pid, image=session.server_image()):
        return fail(
            f"a server is already running for this session (pid {running_pid})",
            hint="call server_stop first, or server_status to check on the running one",
        )

    game = session.game()
    if not game:
        return fail("game not found", hint="set machine.game in dayz-mcp.local.toml")

    stand = _stand()
    # The filename is a profile setting (machine.config), not a literal: this
    # project's own stand hangs forever after world-compile if booted with the
    # wrong one (a working config lives under a different name there), so the
    # default here is a starting point, never an assumption.
    cfg_path = stand / prof.machine.config
    if not cfg_path.exists():
        return fail(
            f"server config not found: {cfg_path}",
            hint=f"point machine.stand_root at a prepared stand, or set machine.config if the "
                 f"server config there is not named {prof.machine.config!r}",
        )

    # DayZDiag_x64.exe forces $currentdir to its own directory on launch, so a
    # relative -config is silently replaced by the game installation's own config
    # (verified against another project's comment for engine 1.29). Resolve to an
    # absolute path, and refuse if that resolution (e.g. through a symlink) lands
    # outside the stand we were told to use.
    cfg = cfg_path.resolve()
    stand_resolved = stand.resolve()
    if not _is_within(cfg, stand_resolved):
        return fail(
            f"server config resolves outside stand_root: {cfg}",
            hint=f"{prof.machine.config} must be a real file inside machine.stand_root, not a "
                 f"link to somewhere else",
        )

    # PRE-FLIGHT, and the one check that would have prevented the boot failure
    # this signal was built for: a stand is shared -- one machine, one port, one
    # -profiles directory -- and another tool (or another agent) may already be
    # holding it. Starting anyway produces a server that dies during world load
    # with nothing in its own log to say why, which is exactly how that failure
    # presented. The holder is NOT ours to stop: this refuses and names it.
    busy = udp_port_holders(prof.machine.port)
    if busy:
        # Whose it is decides what to do about it. A holder this session
        # started is ours, and server_stop is the way out. Anyone else's may
        # ALSO be stopped -- the owner authorised stopping a neighbouring
        # stand that blocks a live run -- but that decision authorises the
        # CALLER, not this machine: the tool never auto-stops what it did not
        # start, and the offer travels with identification (the pid, and the
        # -mod= tail where it can be read), because the caller is choosing
        # what to kill. The -mod= tail is the one cheap field that tells two
        # stands apart; this project once mistook a neighbour's server for a
        # relaunch of its own on every other field.
        ours = [holder for holder in busy if session.known_pid(holder)]
        if ours:
            hint = (f"that is a server this session started -- call server_stop(pid={ours[0]}) "
                    "and try again")
            described = ", ".join(str(x) for x in busy)
        else:
            described = ", ".join(
                f"{holder} (mods: {process_mods_tail(holder) or 'unknown'})" for holder in busy
            )
            hint = ("another stand holds this port -- the mod list above says whose. The "
                    "owner has authorised stopping a neighbouring stand when it blocks a "
                    f"live run: check the mods are not a stand you need, then stop it "
                    f"yourself (taskkill /PID {busy[0]} /F) and retry, or give this "
                    "project its own machine.port. This tool will not stop it for you")
        return fail(
            f"udp port {prof.machine.port} is already held by pid(s) {described}, "
            "so this server would not get it",
            hint=hint,
        )

    # Checked BEFORE the job exists, because this is a refusal and not a boot
    # outcome: a server started without its mission comes up looking healthy in
    # every way a job could report.
    absent = missing_mission(Path(game), cfg)
    if absent:
        return fail(
            f"the server config asks for mission {absent!r}, and the engine will not find "
            f"it: it looks for mpmissions beside the executable being run, which is "
            f"{Path(game) / 'mpmissions'}",
            hint=f"put {absent!r} under {Path(game) / 'mpmissions'} -- a directory symlink "
                 f"to the DayZServer install's own mpmissions is the usual way "
                 f"(mklink /D). Without it the server starts, binds its port and logs no "
                 f"error, and then refuses every player with 'Mission script has no main "
                 f"function, player connect will stay disabled!'",
        )

    client_mods, server_mods = mod_list()
    store = session.jobs()
    job = store.create("boot")
    # The moment the job record was created, just before the process is spawned.
    # log_verdict is given this back as `since` so it never judges a log left over
    # from an earlier boot.
    since = job.started
    profiles = server_profiles_dir()
    profiles.mkdir(parents=True, exist_ok=True)
    # Before the command line is even built, so nothing can start against a
    # transport left over from an earlier world. See clear_bridge_transport
    # for why these two are cleared and the logs beside them are not.
    transport_left = clear_bridge_transport(profiles)
    transport_note = ""
    if transport_left:
        transport_note = f" | WARNING: could not clear bridge transport: {'; '.join(transport_left)}"
    port = prof.machine.port
    cmd = [
        str(Path(game) / SERVER_IMAGE), "-server", f"-config={cfg}",
        f"-port={port}", f"-mod={client_mods}", f"-profiles={profiles}",
    ]
    if server_mods:
        cmd.append(f"-serverMod={server_mods}")
    extras_note = ""
    if extra_args:
        # After every fixed argument, so an extra can never displace one.
        cmd.extend(extra_args)
        extras_note = f" | extra args: {' '.join(extra_args)}"

    # SPAWNED HERE, in the caller's own thread, and not in the worker below.
    #
    # The pid is what every other tool asks the session for, and setting it
    # inside the worker left a window in which a server that had just been
    # started was invisible: three live runs called world_ready immediately
    # after this returned and were told "no server started by this session is
    # running" while the server was coming up. The window was small and
    # entirely avoidable -- spawning is a process create, not a wait, and the
    # thing worth doing on a thread is the READINESS WAIT that follows.
    #
    # A spawn that fails is now answered by this call rather than by the job.
    # An image that cannot be launched (a partial download, a placeholder --
    # DayZDiag_x64.exe exists, so find_game's probe passed) is not a boot
    # outcome, it is a refusal, and the caller should not have to make a round
    # trip through job_wait to learn it. The job is still recorded as failed,
    # so nothing is left looking alive.
    store.start(job.id)
    try:
        # Old script_*.log files are left alone: unlinking a file a live server
        # still holds open raises PermissionError on Windows. The `since` cutoff
        # below is what tells this run's log apart from theirs.
        pid = spawn(cmd, Path(game))
    except Exception as exc:  # noqa: BLE001 - the caller must hear this, not stderr
        store.fail(job.id, f"{type(exc).__name__}: {exc}")
        return Result(
            False, {"job_id": job.id, "since": since},
            f"the server could not be started: {type(exc).__name__}: {exc}",
            hint="check that machine.game points at a real DayZ installation and that "
                 f"{SERVER_IMAGE} there is a runnable image -- a partial download passes "
                 "the existence check and fails here",
        )
    session.set_server_pid(pid, SERVER_IMAGE)

    def run() -> None:
        # An uncaught exception here must still resolve the job, not just print a
        # traceback to the stdio server's stderr where the agent cannot see it.
        # Without this, the job stays "running" forever, and the next process
        # start relabels it "lost to a restart" instead of what actually
        # happened.
        try:
            marker = prof.expect.ready_line
            if not marker:
                # Nothing to wait for. load_profile already notes that
                # readiness cannot be detected without expect.ready_line, and
                # this used to ignore the note: it polled for the empty string,
                # which never matches, and after the full timeout (420s by
                # default) failed with "no ready line within 420s" -- a false
                # failure, seven minutes late, for a configuration nothing
                # documents as unsupported.
                #
                # So: start it, look once whether it is still there, and say
                # what this configuration can and cannot know. Not waiting for
                # readiness must not become not looking at all -- a server that
                # died on its own command line is the one failure still
                # detectable here.
                time.sleep(max(0.0, min(NO_READY_LINE_SETTLE_SECONDS, timeout)))
                if not is_alive(pid, image=SERVER_IMAGE):
                    store.fail(job.id, "the server process died moments after starting")
                    return
                # With no ready line there used to be nothing left to do but
                # dwell and admit readiness could not be determined. The port is
                # a real signal for exactly this case: it needs neither a mod nor
                # a declared line, and it is the server's own doing. Measured on
                # this project's stand: bound 16.9s after spawn, by the very pid
                # we spawned.
                # BOUNDED by its own constant, not by `timeout`. Waiting the
                # full timeout here would recreate the defect this branch was
                # written to fix -- a project that cannot declare a ready line
                # used to poll for a marker that could never match and fail
                # seven minutes later. The bound is set against the measured
                # bind time (16.9s on this project's stand), generously, so a
                # server that is going to bind has long since done it; a server
                # that has not by then is answered honestly instead of waited on.
                #
                # TWO signals, not one, and the second is why: the port binds
                # about 17 s after spawn and the mission module compiles about
                # 25 s after it. Finishing at the port alone reported "ready"
                # for a server that had not compiled a line of the mod -- and
                # a verdict taken at that moment reads a log with no errors and
                # says "pass". That is exactly how a stand whose mission the
                # engine could not find passed every check while refusing every
                # player.
                deadline = time.time() + min(timeout, PORT_READY_WAIT_SECONDS)
                port_bound = False
                scripts_up = False
                while time.time() < deadline:
                    port_bound = port_bound or pid in udp_port_holders(port)
                    scripts_up = scripts_up or mission_module_compiled(profiles, since)
                    if port_bound and scripts_up:
                        store.finish(
                            job.id, 0,
                            summary=f"ready, pid {pid}: udp/{port} bound AND the mission "
                                    "module compiled. expect.ready_line is empty, so this "
                                    "says the engine is listening and its mission scripts "
                                    "are up, NOT that any particular mod finished loading"
                                    f"{transport_note}{extras_note}",
                        )
                        return
                    if not is_alive(pid, image=SERVER_IMAGE):
                        store.fail(job.id, "the server process died before it bound its port")
                        return
                    time.sleep(2)

                # Listening but with no mission scripts is the one shape that
                # must NOT be called ready: the engine answers queries and
                # refuses every player, and the only line saying so is the mod
                # log's "Mission script has no main function".
                if port_bound and not scripts_up:
                    store.fail(
                        job.id,
                        f"pid {pid} is listening on udp/{port}, but the mission module never "
                        f"compiled within {min(timeout, PORT_READY_WAIT_SECONDS):g}s -- the "
                        "engine is up and the mission is not, which is a server that will "
                        "refuse every player. The commonest cause is the mission itself: the "
                        "engine looks for mpmissions beside the executable it runs, not "
                        "beside the -config",
                    )
                    return
                # Alive, never bound: the old honest answer, now naming the extra
                # thing that was actually looked at. Deliberately not a failure --
                # a server that does not bind this port is unusual, not proof of
                # anything, and this configuration could not judge readiness at
                # all before.
                waited_for = min(timeout, PORT_READY_WAIT_SECONDS)
                compiled = " the mission module compiled, but" if scripts_up else ""
                store.finish(
                    job.id, 0,
                    summary=f"started, pid {pid}; expect.ready_line is empty and{compiled} "
                            f"udp/{port} was never observed bound within {waited_for:g}s, so "
                            f"readiness cannot be detected -- only errors will be judged"
                            f"{transport_note}{extras_note}",
                )
                return
            deadline = time.time() + timeout
            port_bound = False
            while time.time() < deadline:
                if not is_alive(pid, image=SERVER_IMAGE):
                    store.fail(job.id, "the server process died before it was ready")
                    return
                # Watched, not waited on. With a ready line declared, THAT is the
                # readiness verdict: it says the mod finished loading, which the
                # port cannot. The port answers a different question -- is the
                # engine listening -- and its value here is telling two very
                # different failures apart at the end.
                if not port_bound and pid in udp_port_holders(port):
                    port_bound = True
                for log in profiles.glob("script_*.log"):
                    if log.stat().st_mtime < since:
                        continue
                    if marker and marker in log.read_text(encoding="utf-8", errors="replace"):
                        store.add_artifact(job.id, log)
                        bound_note = f"; udp/{port} bound" if port_bound else ""
                        store.finish(
                            job.id, 0,
                            summary=f"ready via expect.ready_line, pid {pid}{bound_note}"
                                    f"{transport_note}{extras_note}",
                        )
                        return
                time.sleep(2)
            if port_bound or pid in udp_port_holders(port):
                store.fail(
                    job.id,
                    f"no ready line within {timeout}s -- but pid {pid} holds udp/{port}, so the "
                    "server itself is up and listening: it is the mod's ready line that never "
                    "appeared, not the boot that failed",
                )
                return
            store.fail(job.id, f"no ready line within {timeout}s")
        except Exception as exc:  # noqa: BLE001 - must reach the job, not just stderr
            store.fail(job.id, f"{type(exc).__name__}: {exc}")

    threading.Thread(target=run, daemon=True).start()
    # `pid` is returned as well as recorded: a caller that wants to look at the
    # process itself should not have to wait for the readiness job to finish to
    # find out which one it is.
    started = {"job_id": job.id, "since": since, "pid": pid}
    # A WARNING, not a refusal. The server itself comes up perfectly; it is
    # only clients that will be turned away, and compile boots with no client
    # are half of what this tool is used for.
    signatures = signature_problem(Path(game), cfg, client_mods)
    if signatures:
        started["signatures"] = signatures
    # Only when something is actually wrong: a field that is always present
    # and almost always empty stops being read.
    if transport_left:
        started["bridge_transport_left"] = transport_left
    return ok(started)


def server_stop(pid: int = 0) -> Result:
    """Stop a server this session is responsible for.

    With no `pid`, stops the session's own currently tracked server (the
    original behaviour). With `pid`, stops that specific process instead --
    but only if this session started it at some point, or it was reported as
    `orphaned_server_pid` by project_open after a project switch (see
    session.known_pid). Any other pid is refused: this is the only way an
    orphaned server can be reached at all, and it must not become a general
    process killer.

    Either way, the pid is checked against the recorded image name before it
    is handed to `stop()` (which calls `taskkill`): a recycled Windows pid can
    belong to an unrelated process by the time this runs, and killing that
    process instead would be a worse outcome than the stale bookkeeping this
    guards against.
    """
    image = session.server_image()
    if pid:
        if not session.known_pid(pid):
            return fail(
                f"pid {pid} is not one this session started or reported as orphaned",
                hint="server_stop(pid=...) only accepts a pid this session's own server_start "
                     "produced, or a pid project_open reported as orphaned_server_pid",
            )
        if not is_alive(pid, image=image):
            if pid == session.server_pid():
                session.set_server_pid(0)
            return ok({"stopped": True, "pid": pid})
        stopped = stop(pid)
        if pid == session.server_pid():
            session.set_server_pid(0)
        return ok({"stopped": stopped, "pid": pid})

    session_pid = session.server_pid()
    if not session_pid:
        return ok({"stopped": False, "reason": "no server was started by this session"})
    if not is_alive(session_pid, image=image):
        session.set_server_pid(0)
        return ok({"stopped": True, "pid": session_pid})
    stopped = stop(session_pid)
    session.set_server_pid(0)
    return ok({"stopped": stopped, "pid": session_pid})


def server_status(pulse_seconds: float = 1.0) -> Result:
    """A quick health read: is the process alive, and is its log actually growing.

    A hung boot and a slow one both look "alive, no ready line yet" from the
    outside -- this project has hit a genuine post-compile server hang before --
    so this samples the newest log's size twice, `pulse_seconds` apart, and also
    reports how long it has been since the log last changed at all.
    """
    guard = require_project()
    if guard:
        return guard
    pid = session.server_pid()
    running = is_alive(pid, image=session.server_image()) if pid else False

    profiles = server_profiles_dir()
    log = _newest(profiles, "script_*.log")
    if log is None:
        return ok({"pid": pid, "running": running, "log": None, "growing": None, "stalled_seconds": None})

    pulse = max(0.0, min(pulse_seconds, STATUS_PULSE_MAX))
    try:
        size_before = log.stat().st_size
    except OSError:
        size_before = None
    time.sleep(pulse)
    try:
        stat_after = log.stat()
    except OSError:
        return ok({"pid": pid, "running": running, "log": str(log), "growing": None, "stalled_seconds": None})

    growing = size_before is not None and stat_after.st_size > size_before
    stalled_seconds = max(0.0, time.time() - stat_after.st_mtime)
    return ok(
        {
            "pid": pid,
            "running": running,
            "log": str(log),
            "growing": growing,
            "stalled_seconds": round(stalled_seconds, 1),
        }
    )


def client_compile_check(extra_mods: str = "", wait_seconds: float = 120) -> Result:
    """Compile the CLIENT half of the scripts and judge the result. Returns a
    job id.

    A server boot never compiles anything behind the client-only guard, so a
    broken menu, a broken widget or a broken client-side action passes every
    server check and then breaks in front of a player. This runs the diagnostic
    client for `wait_seconds`, stops it, and judges what it wrote -- and the
    verdict does not accept a clean log on its own: the game's own "Module:
    Mission" line has to appear, otherwise "no errors" only means "not that far
    yet" and the job says so.

    The client runs against a THROWAWAY -profiles directory inside this job's
    artifacts, so it never reads or writes the test stand. It also joins
    nothing: this is a compile pass, not a session. For the live client that
    connects to the stand -- and for looking at it, acting through it, and
    judging its .RPT -- the tools are client_start and its siblings, and
    log_verdict(source="client") reads THIS job's log while client_verdict
    reads the live client's.

    `extra_mods` appends to the -mod list for this run only, for checking that
    a mod still compiles alongside another one.
    """
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()
    game = session.game()
    if not game:
        return fail("game not found", hint="set machine.game in dayz-mcp.local.toml")

    store = session.jobs()
    job = store.create("client-compile")
    profiles = client_profile_dir(job.id)
    profiles.mkdir(parents=True, exist_ok=True)

    def run() -> None:
        store.start(job.id)
        try:
            # -serverMod mods are dedicated-server only; the diagnostic client never
            # loads them, so only the client half of the split is used here.
            client_mods, _server_mods = mod_list(extra_mods)
            pid = spawn(client_cmd(Path(game), client_mods, profiles), Path(game))
            time.sleep(wait_seconds)
            rpt = _newest(profiles, "*.RPT")
            slog = _newest(profiles, "script_*.log")
            stop(pid)
            rpt_lines = rpt.read_text(encoding="utf-8", errors="replace").splitlines() if rpt else []
            log_lines = slog.read_text(encoding="utf-8", errors="replace").splitlines() if slog else []
            for art in (rpt, slog):
                if art:
                    store.add_artifact(job.id, art)
            got = judge(rpt_lines, log_lines, prof.expect)
            if got["status"] == "ok":
                store.finish(job.id, 0, summary="client compiles")
            else:
                store.fail(job.id, f"{got['status']}: {got['reason']} {' | '.join(got['errors'][:5])}")
        except Exception as exc:  # noqa: BLE001 - must reach the job, not just stderr
            store.fail(job.id, f"{type(exc).__name__}: {exc}")

    threading.Thread(target=run, daemon=True).start()
    return ok({"job_id": job.id})
