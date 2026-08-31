"""The game client: start it, look at it, act through it, judge how it ended.

Phase 2 gave the SERVER a channel -- spawn, teleport, actions, a snapshot of the
world, all server-side. What it cannot do is look at the client's screen or act
through the client: open a mod's menu, walk a character across ground, watch a
widget appear. That is what these tools are.

THREE INPUT LAYERS, EACH MEASURED AGAINST A LIVE CLIENT (2026-08-21). They are
not alternatives; each does what the others cannot:

    the world      the bridge (world_*, phase 2)      server-side, no client
    movement,      a virtual gamepad (ViGEmBus)       BACKGROUND, no focus
    camera, menus,
    inventory
    text in chat   the bridge again (the mod's own    BACKGROUND, no focus
                   chat verb, server-side)
    text in a mod's real keyboard input (SendInput)   FOREGROUND REQUIRED
    input field

The measurements behind that table, so nobody re-derives them:

  * keyboard emulation does NOT move the character -- SendInput scancodes with
    the foreground verified, 25 s of forward, moved it 0 m; window messages
    (PostMessage/SendMessage) moved it 0 m and did not drive the menus either.
    The engine reads movement from raw input and ignores emulated keys, so
    `PostMessage` is offered nowhere in this module.
  * the virtual gamepad DOES, and it does it unfocused: 13.06 m walked while a
    third-party application held the foreground. Buttons drive the interface
    too -- RB switched an options tab, BACK opened the inventory, B left the
    menu, all with the game window behind another application.
  * the eyes need no focus either: a capture is live with the window at the
    very bottom of the z-order. What they cannot survive is a MINIMIZED window,
    whose client area collapses to 0x0.

SO EXACTLY ONE TOOL HERE TAKES THE FOREGROUND: `client_type`, and only because
a mod's own input field exists on the client alone and can only be filled the
way a person fills it. Chat is not that case -- chat is a server-side message,
the bridge delivers it as data, and `client_chat` says so rather than letting a
caller who knows the general rule about typing go and fight for the foreground
where nothing requires it.

AND ALL OF IT RESTS ON ONE CLIENT SETTING, `pauseMode` (GAME -> UPDATE IN
BACKGROUND). At the value measured here the client keeps drawing and
simulating while unfocused, which is WHY the frame is live and the stick moves
the character from the background. At "no graphics" both would stop, silently:
the frame would be a frozen picture and nothing would say so. `client_start`
therefore READS it and warns; it never writes it, because it belongs to the
person who owns the machine.
"""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

from .. import gamepad, winui
from ..bridge.channel import Channel
from ..errors import Result, fail, ok
from ..jobs import QUEUED, RUNNING
from ..paths import GAME_PROBE
from ..procs import is_alive, pids_of, spawn, stop, udp_port_holders
from ..verdict import build_verdict
from . import session
from .lifecycle import (
    CLIENT_PROFILE_DIRNAME,
    SIGNATURE_HINT,
    mod_list,
    server_profiles_dir,
    signature_problem,
)
from .lifecycle import _stand as stand_root
from .project import require_project
# The world command builder, imported rather than re-implemented. It is the one
# place that stamps a command with the live session, refuses when the bridge is
# not ticking, and passes the mod's own words back verbatim; a second copy here
# would be a second opinion about how a command is sent, which is exactly how a
# tool starts reporting success for a world in which nothing happened. Private
# by name because nothing outside this package should be sending raw commands,
# not because this module is reaching somewhere it should not.
from .world import WORLD_TIMEOUT_SECONDS
from .world import _run as _world_command

# The client comes in TWO BUILDS AND THEY DO NOT MIX. The engine refuses the
# pairing itself, at connect time, with
#
#     CONNECTING FAILED (0x00020017)
#     Client is using Diag exe while the server is not.
#
# which costs a boot and a full connect timeout to discover, and which arrives
# as "the player count stayed at 0" -- a symptom that says nothing about the
# cause.
#
# WHICH BUILD IS RIGHT IS NOT A SETTING, because it is not a choice. It follows
# from the fact that already decides the SERVER's image in lifecycle.py:
# `machine.server` names a separate DayZServer install, so the stand runs the
# retail dedicated binary and the client must be retail too; without it the
# stand is DayZDiag_x64.exe out of the game directory and the client must be
# the diag build. A knob here could only be used to re-create the one
# combination the engine forbids, so there is none.
DIAG_IMAGE = GAME_PROBE
RETAIL_IMAGE = "DayZ_x64.exe"

# The retail client has to be started through its own BattlEye launcher, and
# this is a fact about the CLIENT, not about the stand. Measured here: with
# `BattlEye = 0;` in the server config -- so nothing on the server asks for it
# -- a directly launched DayZ_x64.exe reached the world load, was refused at
# connect with a BattlEye message, and the bridge never saw a player. The same
# build launched through DayZ_BE.exe joined the same stand.
#
# The launcher starts the game as a SEPARATE process and keeps running beside
# it, so the pid returned by spawn() is the launcher's. Every tool downstream
# keys off the tracked pid -- the window to capture, the window to focus, the
# process to stop, the .RPT to judge -- so the launched game has to be adopted
# before anything is recorded.
BE_LAUNCHER = "DayZ_BE.exe"

# How long the launcher gets to produce a game process. Generous: it verifies
# its own files first, and a slow disk has been seen to take a while.
BE_HANDOFF_SECONDS = 60.0
BE_HANDOFF_POLL = 0.5


def _adopt_launched_game(image: str, before: set[int], deadline: float) -> int:
    """The pid of the game the BattlEye launcher started, or 0.

    A launcher cannot be asked what it spawned, so this watches for a process
    with the game's image that was not there before the launcher ran. On a
    stand nobody else is starting DayZ, so the new one is ours.

    Waits to the deadline rather than giving up when the launcher exits: the
    launcher handing off and disappearing is a normal path, and treating it as
    a failure would report a client that is starting perfectly well as dead.
    """
    while time.time() < deadline:
        fresh = pids_of(image) - before
        if fresh:
            # Lowest pid when several appear at once, so the choice is at least
            # deterministic; on a stand there is only ever one.
            return min(fresh)
        time.sleep(BE_HANDOFF_POLL)
    return 0


def _client_image_for(prof) -> str:  # noqa: ANN001 - the loaded profile, or None
    """Which client build this stand can be joined by."""
    return RETAIL_IMAGE if (prof and prof.machine.server) else DIAG_IMAGE


def _tracked_image() -> str:
    """The image to check a tracked pid against.

    RECORDED first, derived only as a fallback. The profile can be reloaded
    (project_open) between starting a client and stopping it, and a derived
    name would then guard the pid against the wrong image -- which reads as
    "the client is gone" for a client that is running perfectly well, and
    leaves it running with nothing tracking it.

    Recorded and checked at all for the reason server_start records its own:
    Windows recycles pids, and a tracked pid since handed to something else
    must not be reported as a running client -- nor killed as one.
    """
    return session.client_image() or _client_image_for(session.profile())

# Where the client connects. The stand is on THIS machine by definition -- the
# eyes (a window handle) and the hands (a virtual device on this machine's own
# USB bus) have no meaning anywhere else -- so there is no host setting to get
# wrong.
STAND_ADDRESS = "127.0.0.1"

# How long client_start will wait for the player to appear in the bridge state.
# The measured connect time on this machine is about 50 s and it varies with
# what the stand is doing, so this is generous on purpose; it is a ceiling on a
# wait, never a substitute for the readiness signal itself.
CONNECT_TIMEOUT_SECONDS = 180.0

# How often the readiness poll looks. The mod publishes its state once a
# second, so anything faster only re-reads the same document.
CONNECT_POLL_SECONDS = 2.0

# Launch arguments this tool computes and therefore owns. The engine honours
# the LAST occurrence of a repeated argument, so an extra copy of one of these
# would silently displace the tool's own -- refused instead. `-window` is in
# the list and is not cosmetic: a fullscreen D3D window will not yield the
# foreground at all (an attempt to cover one hung), so a client launched
# fullscreen is a client nothing can be verified against.
OWNED_LAUNCH_ARGS = ("-connect", "-port", "-mod", "-profiles", "-window",
                     "-nolauncher", "-exe")

# Said in the answer of the one tool that takes the screen away, the same way
# gamepad.py announces that a controller has been plugged in: a side effect on
# the person at the machine is named, never implied.
FOREGROUND_NOTICE = (
    "The client window was moved to the foreground in order to type, so for the "
    "duration of this call the person at this machine could not type into their "
    "own window. This is the only tool in the client set that takes the "
    "foreground -- the screenshot, the gamepad (movement, camera, menus, "
    "inventory) and client_chat all work with the client in the background."
)

# What to do when Windows will not hand over the foreground. It names the
# cause and the action, and it deliberately does NOT offer the bridge as a
# substitute: a mod's input field exists only on the client, so sending the
# caller to a server-side message would send them to a tool that cannot do the
# job. The distinction is stated, because it is the useful half.
FOREGROUND_REFUSED_HINT = (
    "Windows refuses to hand the foreground to a background process. Make the "
    "client window active -- click it, or leave the machine to this session -- and "
    "call this again. client_chat is not a way around this: chat is a server-side "
    "message and needs no focus at all, but a mod's own input field exists only on "
    "the client and can only be filled the way a person fills it."
)

# When the bridge build in the stand has no chat verb. Chat is delivered
# server-side (the engine's ChatMP), so the verb has to exist in the mod; the
# mod's own refusal lists the verbs it does know, and this says what to do
# about it instead of leaving the caller staring at that list.
CHAT_VERB_MISSING_HINT = (
    "the stand is running a bridge build older than this server: chat is delivered "
    "server-side by the mod, and the verb ships with this repository's bridge "
    "sources. Rebuild it with bridge_build and restart the stand so it loads the "
    "new pbo. There is no keyboard path to chat in this tool set -- client_type "
    "fills an input field that is already open, and it costs the foreground."
)

# When the stand on the port was not started by this session. Every other
# client tool works against such a stand on purpose -- client_start says so and
# joins it anyway -- but chat is delivered server-side, so it goes through the
# same channel as the world tools and inherits their rule. Without this the
# caller gets world.py's generic refusal, whose hint says "start one with
# server_start" -- and server_start would refuse the held port. Measured live:
# a client joined a neighbouring stand happily and then client_chat failed with
# advice that could not be followed.
def _foreign_stand_hint(stand: dict) -> str:
    holders = ", ".join(str(h) for h in stand.get("port_holders") or [])
    whose = (
        f"The stand on udp/{stand.get('port')} is held by pid(s) {holders}, which this "
        "session did not start, so server_start would refuse that port until it is stopped. "
        if holders
        else ""
    )
    return (
        "chat is delivered server-side, through the same channel as the world tools, so it "
        "needs a stand THIS session started -- unlike client_start, client_shot and the "
        f"gamepad, which work against whatever stand is on the port. {whose}Either restart "
        "the stand through this session (server_stop, then server_start) or send the line "
        "from the session that owns it. There is no keyboard path to chat here: client_type "
        "fills an input field that is already open, and it costs the foreground."
    )

# The guard against two client starts at once, and PROCESS-global on purpose:
# what it protects is that there is one client profile directory and one
# machine's worth of memory, whichever project happens to be open. See
# client_start's own comment for the window it closes.
_start_guard = threading.Lock()
_start_in_flight: dict = {"job_id": "", "store": None}


def _client_start_in_flight() -> str:
    """The id of a client start still running, or "".

    Re-checked against the job's own recorded status rather than trusted as a
    flag: a worker that somehow failed to clear the slot would otherwise refuse
    every later start for the life of the process, and the job's status is the
    fact of the matter either way. Call under `_start_guard`.
    """
    job_id = str(_start_in_flight["job_id"] or "")
    if not job_id:
        return ""
    store = _start_in_flight["store"]
    job = store.get(job_id) if store is not None else None
    if job is None or job.status not in (QUEUED, RUNNING):
        return ""
    return job_id


def _release_start_slot(job_id: str) -> None:
    """Give the slot back, if this job is still the one holding it."""
    with _start_guard:
        if _start_in_flight["job_id"] == job_id:
            _start_in_flight.update(job_id="", store=None)


def client_profiles_dir() -> Path:
    """The -profiles directory the LIVE client runs against. THE definition of
    that location -- client_start launches against it, client_verdict reads the
    .RPT out of it, and the pauseMode check reads the settings file inside it.

    Derived from the one owner of the stand's layout rather than recomputing
    machine.stand_root here: two places computing one path is how the
    client-side log location ended up with two disagreeing owners once already.

    Deliberately NOT the same directory as client_compile_check's, which gets a
    fresh throwaway per job. This one has to PERSIST: the window size and the
    UPDATE IN BACKGROUND setting live in it, and a profile thrown away between
    runs would reset both every time.
    """
    return server_profiles_dir().parent / CLIENT_PROFILE_DIRNAME


def _newest(folder: Path, pattern: str) -> Path | None:
    if not folder.is_dir():
        return None
    items = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return items[0] if items else None


def _crash_dump_since(profiles: Path, since: float) -> Path | None:
    """A minidump written by THIS run, or None.

    A client that fails to start does not reliably die, and that is the whole
    reason this exists. An engine error message leaves the process alive behind
    a modal dialog it holds until someone clicks it: `is_alive` then answers
    truthfully, the player count never moves, and the wait runs to the full
    timeout before blaming the bridge for a silence the client caused. The dump
    beside the profile is the event itself, and it is on disk within seconds.

    `since` is the job's own start, so the dumps of every earlier run -- and
    this profile directory accumulates them -- are not read as this one's.
    """
    newest = _newest(profiles, "*.mdmp")
    if newest is None:
        return None
    try:
        written = newest.stat().st_mtime
    except OSError:  # being written, or gone again -- either way, not evidence
        return None
    return newest if written >= since else None


def _crash_reason(pid: int, dump: Path) -> str:
    """Why the start failed, told apart by which kind of dump was written.

    The two are not the same event to act on: one has a window on screen
    waiting for a click, the other has nothing left running.
    """
    if dump.name.startswith("ErrorMessage_"):
        return (
            f"the client (pid {pid}) stopped on an engine error message while starting and "
            f"wrote {dump.name}. The process is still alive because the dialog is waiting to "
            "be clicked -- which is why the player count would never have moved and this "
            "would otherwise have run to the full timeout. client_shot shows the message "
            "itself; client_verdict reads the .RPT behind it"
        )
    return (
        f"the client (pid {pid}) crashed while starting and wrote {dump.name} -- "
        "client_verdict reads its .RPT, which is where the reason will be"
    )


def _background_setting() -> dict:
    """What the client's UPDATE IN BACKGROUND setting is, in a shape that can
    be reported whether or not it could be read.

    A client that has never run has no settings file, and that is not a
    failure -- it is "unknown", and it warns for the same reason a wrong value
    warns: the caller cannot otherwise tell a frozen frame from a live one.
    """
    read = winui.read_pause_mode(client_profiles_dir())
    if read.ok:
        return dict(read.data)
    return {
        "pause_mode": None,
        "background_verified": False,
        "settings_file": None,
        "note": f"{read.error} -- {read.hint}",
    }


def _stand_view(port: int) -> dict:
    """Is there anything on this port for a client to join, and whose is it.

    A stand this session started is the ordinary case. A stand someone else
    started is still perfectly joinable -- this machine really does host a
    neighbouring stand from time to time -- so it is not refused; it is named,
    because the readiness signal is read out of THIS project's profile
    directory, and only a stand using that same -profiles directory writes
    there.
    """
    pid = session.server_pid()
    ours = bool(pid and is_alive(pid, image=session.server_image()))
    holders = [] if ours else list(udp_port_holders(port))
    view = {
        "port": port,
        "started_by_this_session": ours,
        "server_pid": pid if ours else 0,
        "port_holders": holders,
        "alive": ours or bool(holders),
    }
    if not ours and holders:
        view["note"] = (
            f"the stand on udp/{port} was not started by this session (pid(s) "
            f"{', '.join(str(h) for h in holders)}). The client can still join it, but "
            f"readiness is read from {server_profiles_dir() / 'dayz_mcp_state.json'}, "
            "which only a stand using that same -profiles directory writes -- and the "
            "world_* tools will refuse, because they only act on a server this session "
            "started"
        )
    return view


def _players_in(state) -> int | None:
    """How many players the bridge says are connected, or None when the state
    could not be read or does not carry the field.

    None and 0 are different answers and must stay different: 0 means the
    bridge is publishing and nobody has joined (the client is still
    connecting), while None means the signal itself is unavailable -- most
    often because the bridge mod is not loaded at all. Telling a caller their
    client failed to connect when the truth is that nothing was ever measured
    is the class of false diagnosis this whole product exists to remove.
    """
    if state is None:
        return None
    world = getattr(state, "world", None)
    if not isinstance(world, dict):
        return None
    value = world.get("players")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _require_live_client() -> tuple[int, Result | None]:
    """`(pid, None)` when this session has a client that is actually running,
    `(0, refusal)` otherwise. A tracked pid whose process is gone is cleared
    here, so the next call says "no client" rather than repeating a lie."""
    guard = require_project()
    if guard:
        return 0, guard
    pid = session.client_pid()
    if not pid:
        return 0, fail(
            "this session has no client running",
            hint="start one with client_start and wait for its job to finish; only a "
                 "client started through client_start is tracked",
        )
    if not is_alive(pid, image=_tracked_image()):
        session.set_client_pid(0)
        return 0, fail(
            f"the client this session started (pid {pid}) is no longer running",
            hint="start a new one with client_start -- and client_verdict judges the "
                 "old one's .RPT, which is where the reason it ended will be",
        )
    return pid, None


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def client_start(
    timeout: float = CONNECT_TIMEOUT_SECONDS, extra_args: list[str] | None = None
) -> Result:
    """Start the game client and connect it to the test stand. Returns a job id.

    Three things worth knowing before calling, all of them observable:

    IT PICKS THE CLIENT BUILD FROM THE STAND, and does not offer the choice.
    A stand booted from a separate DayZServer install can only be joined by the
    retail client; a stand booted from DayZDiag_x64.exe only by the diag one.
    The engine refuses the other pairing at connect time ("Client is using Diag
    exe while the server is not"), and that refusal reaches a caller here as
    "the player count stayed at 0" -- a boot and a full connect timeout spent
    on a mismatch that was knowable before launch. `machine.server` decides it,
    the same setting that already decides the server's own image.

    THE RETAIL CLIENT GOES THROUGH ITS BATTLEYE LAUNCHER, and that is a fact
    about the client rather than about the stand. Measured here: with
    `BattlEye = 0;` in the server config, so nothing on the server asks for it,
    a directly launched DayZ_x64.exe still reached the world load, was refused
    at connect with a BattlEye message, and the bridge never saw a player --
    while the same build launched through DayZ_BE.exe joined the same stand.
    The launcher starts the game as a separate process, so this adopts the
    process it spawned and tracks THAT: everything downstream keys off the
    tracked pid.

    IT LAUNCHES WINDOWED, always. A fullscreen D3D window does not yield the
    foreground -- an attempt to cover one simply hung -- so a fullscreen client
    can be neither typed into nor left behind while the owner works. The window
    size itself is the client profile's business (DayZ.cfg), not this server's.

    IT REFUSES WHEN THERE IS NO STAND TO JOIN. The client connects, it does not
    listen, so there is no port of its own to pre-flight; what there must be is
    something already on the stand's port. Without that the client sits at the
    server browser forever and every later tool answers about a client that
    never joined anything.

    IT READS `pauseMode` AND WARNS. That setting (GAME -> UPDATE IN BACKGROUND,
    stored in the client's own profile) is why the screenshot is a live frame
    and why the gamepad moves the character while another window has the
    foreground. At another value both stop working with nothing said anywhere,
    so the value is reported on every start and a warning is attached when it is
    not the one measured here. It is never rewritten: it belongs to the person
    who owns this machine and is changed from inside the game.

    READINESS IS THE PLAYER COUNT IN THE BRIDGE STATE, NOT A TIMER. Connecting
    took about 50 s when it was measured and it varies; a timer would call a
    still-loading client ready and a never-connecting client a success. The job
    finishes when the bridge publishes `players >= 1`, and if that never
    happens the failure says which of the two things went wrong -- the bridge
    was never readable (the signal was unavailable) or it was readable and
    nobody joined (the client itself did not get in).

    `extra_args` appends launch arguments after the fixed ones, an explicit
    one-run opt-in. Arguments this tool computes (-connect, -port, -mod,
    -profiles, -window, -nolauncher) are refused rather than allowed to
    displace its own.
    """
    guard = require_project()
    if guard:
        return guard
    prof = session.profile()

    if extra_args:
        if isinstance(extra_args, str) or not all(isinstance(a, str) for a in extra_args):
            return fail(
                "extra_args must be a list of strings",
                hint='pass each argument separately, e.g. ["-name=probe"] -- a single '
                     "string would have to be re-split, and quoting rules are where that "
                     "goes wrong",
            )
        for arg in extra_args:
            head = arg.split("=", 1)[0].lower()
            owned = next((o for o in OWNED_LAUNCH_ARGS if head == o.lower()), None)
            if owned:
                return fail(
                    f"extra_args may not carry {owned}: this tool computes it, and the "
                    "engine would honour the extra one instead of the tool's own",
                    hint=f"{owned} follows from the profile and from how this tool has to "
                         "run the client (windowed, joined to the stand) -- change the "
                         "profile, not the command line",
                )

    running = session.client_pid()
    if running and is_alive(running, image=_tracked_image()):
        return fail(
            f"a client is already running for this session (pid {running})",
            hint="stop it with client_stop, or ask client_status what it is doing",
        )

    game = session.game()
    if not game:
        return fail("game not found", hint="set machine.game in dayz-mcp.local.toml")

    # A REFUSAL here, where server_start only warns: this client would be
    # rejected on connect, and the engine's own message names a vanilla pbo
    # rather than the signature policy that actually turned it away.
    client_mods, _server_mods = mod_list()
    signatures = signature_problem(
        Path(game), stand_root() / prof.machine.config, client_mods
    )
    if signatures:
        return fail(signatures, hint=SIGNATURE_HINT)

    port = prof.machine.port
    stand = _stand_view(port)
    if not stand["alive"]:
        return fail(
            f"nothing is listening on udp/{port}, so there is no stand for the client to "
            "join",
            hint="start one with server_start and wait for its boot job to finish. The "
                 "client connects rather than listens, so a client started now would sit "
                 "at the server browser with nothing to join and no error anywhere",
        )

    # Which build, and does it exist. Derived from the stand, never chosen:
    # see the note beside DIAG_IMAGE.
    image = _client_image_for(prof)
    exe = Path(game) / image
    if not exe.exists():
        return fail(
            f"this stand needs the {'retail' if image == RETAIL_IMAGE else 'diag'} client "
            f"({image}), and it is not in the game directory: {exe}",
            hint="a stand booted from a separate DayZServer install can only be joined by "
                 "the retail client, and a stand booted from DayZDiag_x64.exe only by the "
                 "diag one -- the engine refuses the other pairing at connect time. Check "
                 "machine.game points at the DayZ installation, not at the tools",
        )

    # The retail client goes through its BattlEye launcher. See BE_LAUNCHER for
    # why that is not optional and not a property of the stand's config.
    launcher = Path(game) / BE_LAUNCHER
    via_launcher = image == RETAIL_IMAGE and launcher.exists()
    if image == RETAIL_IMAGE and not via_launcher:
        return fail(
            f"this stand needs the retail client, and {BE_LAUNCHER} is not in the game "
            f"directory: {launcher}",
            hint="the retail client is refused at connect without its BattlEye "
                 "launcher, whatever the server config says about BattlEye. Verify "
                 "the DayZ installation through Steam",
        )

    profiles = client_profiles_dir()
    profiles.mkdir(parents=True, exist_ok=True)
    background = _background_setting()

    # -serverMod mods are dedicated-server only; a client never loads them, so
    # only the client half of the profile's split goes on this command line.
    client_mods, _server_mods = mod_list()
    #  -exe comes FIRST and is the launcher's own argument, not the game's;
    # everything after it is handed to the game untouched.
    argv0 = [str(exe)]
    if via_launcher:
        argv0 = [str(launcher), "-exe", image]

    cmd = argv0 + [
        f"-connect={STAND_ADDRESS}",
        f"-port={port}",
        f"-mod={client_mods}",
        f"-profiles={profiles}",
        "-nolauncher",
        "-window",
    ]
    extras_note = ""
    if extra_args:
        cmd.extend(extra_args)
        extras_note = f" | extra args: {' '.join(extra_args)}"

    warning = "" if background["background_verified"] else background["note"]
    warn_note = f" | WARNING: {warning}" if warning else ""

    store = session.jobs()
    job = None
    channel = Channel(server_profiles_dir())

    def run() -> None:
        store.start(job.id)
        # An uncaught exception here has to reach the job, not just the stdio
        # server's stderr where no agent will ever see it: a game directory
        # whose executable is not a runnable image passes the existence probe
        # above and then makes spawn() raise.
        try:
            # Snapshot BEFORE the spawn: the adopted pid is the one that was
            # not there a moment ago.
            before = pids_of(image) if via_launcher else set()

            pid = spawn(cmd, Path(game))

            if via_launcher:
                launcher_pid = pid
                pid = _adopt_launched_game(
                    image, before, time.time() + BE_HANDOFF_SECONDS
                )
                if not pid:
                    store.fail(
                        job.id,
                        f"{BE_LAUNCHER} (pid {launcher_pid}) did not start "
                        f"{image} within {BE_HANDOFF_SECONDS:.0f}s -- the launcher "
                        "verifies its files first and shows its own window, so "
                        "check whether it is waiting for something",
                    )
                    return

            session.set_client_pid(pid, image)
            deadline = time.time() + timeout
            ever_readable = False
            last_seen = None
            while True:
                # BEFORE the liveness check, not after: a crash that did kill
                # the process has both signals, and the dump is the one that
                # names the event instead of only its outcome.
                dump = _crash_dump_since(profiles, job.started)
                if dump is not None:
                    store.fail(job.id, _crash_reason(pid, dump))
                    return
                if not is_alive(pid, image=image):
                    store.fail(
                        job.id,
                        f"the client process (pid {pid}) died before it connected -- "
                        "client_verdict reads its .RPT, which is where the reason will be",
                    )
                    return
                players = _players_in(channel.read_state())
                if players is not None:
                    ever_readable = True
                    last_seen = players
                    if players >= 1:
                        store.finish(
                            job.id, 0,
                            summary=f"connected: the bridge reports {players} player(s) "
                                    f"{time.time() - job.started:.0f}s after launch (pid "
                                    f"{pid}, windowed). Readiness is the player count in the "
                                    f"bridge state, not a timer{warn_note}{extras_note}",
                        )
                        return
                if time.time() >= deadline:
                    break
                time.sleep(CONNECT_POLL_SECONDS)

            if not ever_readable:
                # The signal itself was unavailable. Saying "the client did not
                # connect" here would be an accusation nothing measured.
                store.fail(
                    job.id,
                    f"the client (pid {pid}) is running, but the bridge never published a "
                    f"readable state within {timeout:g}s, so the player count could never "
                    "be read at all -- this signal needs the bridge mod loaded in the "
                    "stand; bridge_status says whether it is",
                )
                return
            store.fail(
                job.id,
                f"the client (pid {pid}) is running and the bridge is publishing, but the "
                f"player count stayed at {last_seen} for {timeout:g}s -- the client never "
                "finished joining. client_verdict reads its .RPT; client_shot shows what "
                "is on its screen",
            )
        except Exception as exc:  # noqa: BLE001 - must reach the job, not just stderr
            store.fail(job.id, f"{type(exc).__name__}: {exc}")
        finally:
            _release_start_slot(job.id)

    # ONE guarded region from "this call is going ahead" to "a worker owns the
    # job". The pid check above cannot close this window on its own: this
    # function returns the moment the worker is handed the job, and the pid it
    # checks is only recorded once that worker has spawned the process -- so
    # two calls in quick succession both pass it and start two clients, each
    # several gigabytes and both writing the same profile directory.
    try:
        with _start_guard:
            busy = _client_start_in_flight()
            if busy:
                return fail(
                    f"a client start is already running (job {busy})",
                    hint=f"wait for it with job_wait('{busy}'), or look at it with "
                         f"job_status('{busy}') -- there is one client profile directory "
                         "and one machine, so a second client cannot be started alongside "
                         "the first",
                )
            job = store.create("client-boot")
            _start_in_flight.update(job_id=job.id, store=store)
        threading.Thread(target=run, daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - a raised tool call answers nobody
        detail = f"{type(exc).__name__}: {exc}"
        if job is not None:
            _release_start_slot(job.id)
            try:
                store.fail(job.id, f"the client was never started: {detail}")
            except Exception:  # noqa: BLE001 - the store is the broken part here
                pass
        return fail(
            f"the client could not be started: {detail}",
            hint="this is the job store or the profile path, not the game -- check that "
                 f"{Path(store.root).as_posix()} exists and is writable, then try again",
        )

    started = {
        "job_id": job.id,
        "since": job.started,
        "profiles": str(profiles),
        "windowed": True,
        "stand": stand,
        "background": background,
    }
    # Only when something is actually wrong: a field that is always present and
    # almost always empty stops being read.
    if warning:
        started["warning"] = warning
    return ok(started)


def client_stop() -> Result:
    """Stop the client this session started, and unplug the virtual controller.

    The pad goes with it deliberately. Nothing else in this tool set closes it,
    and a controller left attached is visible inside any game running on this
    machine -- DayZ switches its on-screen hints to controller mode as soon as
    one appears. Stopping the client is the end of the input session, so it is
    where the device is given back.

    Takes no pid on purpose. On a diag stand the client and the server are the
    same executable, so a pid argument could not be checked against the image
    the way server_stop's is; and even where they differ, "stop this pid" would
    become a general process killer.
    """
    pid = session.client_pid()
    pad = gamepad.close_pad()
    data = {"gamepad": pad.data}

    if not pid:
        return ok({**data, "stopped": False, "reason": "no client was started by this session"})
    if not is_alive(pid, image=_tracked_image()):
        session.set_client_pid(0)
        return ok({**data, "stopped": True, "pid": pid, "note": "it had already exited"})
    stopped = stop(pid)
    session.set_client_pid(0)
    return ok({**data, "stopped": stopped, "pid": pid})


def client_status() -> Result:
    """Everything about the client that decides whether the other tools can work.

    Four questions in one answer, because each of them has silently broken a
    run before: is the process alive; is its window in a state the eyes can
    capture (a MINIMIZED window cannot be captured at all -- its client area is
    0x0); is UPDATE IN BACKGROUND still at the value that keeps the frame live
    and the gamepad effective while unfocused; and has the client actually
    joined the stand (the player count the bridge publishes).
    """
    guard = require_project()
    if guard:
        return guard
    pid = session.client_pid()
    running = bool(pid and is_alive(pid, image=_tracked_image()))

    window: dict = {}
    if running:
        geometry = winui.geometry(pid)
        window = dict(geometry.data) if geometry.ok else {
            "error": geometry.error, "hint": geometry.hint
        }

    return ok({
        "pid": pid,
        "running": running,
        "window": window,
        "background": _background_setting(),
        "players": _players_in(Channel(server_profiles_dir()).read_state()),
        "stand": _stand_view(session.profile().machine.port),
        "profiles": str(client_profiles_dir()),
        # Whether a virtual controller is currently attached to this machine.
        # Reported here because it is a change to the system under test that
        # outlives the call that made it.
        "gamepad": gamepad.status().data,
    })


# ---------------------------------------------------------------------------
# eyes
# ---------------------------------------------------------------------------


def client_shot(path: str = "") -> Result:
    """Capture the client's window to a PNG. NO FOCUS NEEDED.

    Measured rather than assumed: the frame is live with the client at the very
    bottom of the z-order and live when it is in front. The one state it cannot
    survive is MINIMIZED -- the client area collapses to 0x0 and there is
    nothing to copy -- and that is refused with a hint saying to restore the
    window, not reported as an empty picture.

    `lit_fraction` in the answer is the honest half: a black capture is exactly
    the failure that otherwise reports success, so the fraction of non-black
    pixels comes back with every shot. A dark frame is never failed -- night is
    dark -- but a caller reading 0.0 knows the eyes were shut.

    With no `path` the file lands in this project's own .dayz-mcp/shots.
    """
    pid, refusal = _require_live_client()
    if refusal:
        return refusal

    if path:
        target = Path(path)
    else:
        target = (
            Path(session.profile().root) / ".dayz-mcp" / "shots"
            / f"client-{int(time.time() * 1000)}.png"
        )

    result = winui.shot(pid, target)
    if not result.ok:
        return result

    data = dict(result.data)
    # The one failure lit_fraction cannot see: with UPDATE IN BACKGROUND at the
    # wrong value an unfocused client stops drawing, and the capture is a
    # perfectly bright picture of a moment that has passed. Only raised when
    # both halves are true, so the field stays worth reading.
    background = _background_setting()
    if not data.get("foreground") and not background["background_verified"]:
        data["warning"] = (
            "the client was NOT in the foreground and its pauseMode is not the value "
            f"measured to keep it drawing while unfocused ({background['note']}). This "
            "frame may be a frozen one -- a stale picture looks exactly like a live one, "
            "and lit_fraction cannot tell them apart"
        )
    return ok(data)


# ---------------------------------------------------------------------------
# hands -- the virtual gamepad, all of it in the background
# ---------------------------------------------------------------------------


def client_move(x: float, y: float, seconds: float) -> Result:
    """Walk the character with the left stick. NO FOCUS NEEDED.

    `x` is positive to the right, `y` positive forward, both in [-1, 1] and
    clamped (and reported as clamped) beyond that. `client_move(0, 1, 6)` is the
    measured 24 m walk. The stick is back at rest when this returns, on every
    path including a failure -- a stick left engaged is a character running
    forever with nobody watching.

    This is the only tract that moves the character at all: keyboard emulation
    was measured at 0 m over 25 s with the foreground verified. It is also the
    only one that is ANALOG, so "walking rather than sprinting" is testable
    here and nowhere else.

    The first call attaches a virtual controller to this machine, which the
    game can see -- DayZ switches its on-screen hints to controller mode -- and
    the answer says so. `client_stop` unplugs it.
    """
    _pid, refusal = _require_live_client()
    if refusal:
        return refusal
    return gamepad.move(x, y, seconds)


def client_look(x: float, y: float, seconds: float) -> Result:
    """Turn the camera with the right stick. NO FOCUS NEEDED.

    Same units and the same guarantees as `client_move`: `x` positive right,
    `y` positive up, clamped to [-1, 1], released on every exit path.
    """
    _pid, refusal = _require_live_client()
    if refusal:
        return refusal
    return gamepad.look(x, y, seconds)


def client_press(button: str, seconds: float = gamepad.DEFAULT_PRESS_SECONDS) -> Result:
    """Press one gamepad button. NO FOCUS NEEDED -- this drives the INTERFACE too.

    Measured with a third-party application holding the foreground the whole
    time: `right_shoulder` moved between options tabs, `back` opened and closed
    the inventory, `b` left the menu. So menus and inventory are reachable
    without ever taking the screen from the person at the machine, and that is
    why this module offers no window messages and no mouse: window messages
    were measured to do nothing at all.

    What a pad cannot do is type -- DayZ has no on-screen keyboard. Text in
    chat goes through `client_chat` (server-side, no focus); text in a mod's own
    input field goes through `client_type` (real input, foreground).

    Buttons: a b x y back start left_shoulder right_shoulder left_thumb
    right_thumb dpad_up dpad_down dpad_left dpad_right.
    """
    _pid, refusal = _require_live_client()
    if refusal:
        return refusal
    return gamepad.press(button, seconds)


def client_trigger(which: str, value: float = 1.0,
                   seconds: float = gamepad.DEFAULT_PRESS_SECONDS) -> Result:
    """Pull one ANALOG trigger. NO FOCUS NEEDED. This is how the weapon fires.

    DayZ binds the right trigger to FIRE and the left trigger to RAISE/AIM, and
    neither is reachable through `client_press`: triggers are axes, not
    buttons. Without this, a pad can walk the character and drive every menu
    but can never make the weapon under test discharge -- so a firearm mod's
    fire animation, recoil, muzzle flash and ejection could not be observed at
    all.

    `which` is "left" or "right"; `value` is travel in [0, 1] (clamped, and the
    clamp is reported), `seconds` how long to hold it. A tap is a single shot
    in semi-auto; a hold of a second or two is what distinguishes a full-auto
    mode from a semi-auto one. The trigger is back at rest when this returns on
    every exit path.
    """
    _pid, refusal = _require_live_client()
    if refusal:
        return refusal
    return gamepad.trigger(which, value, seconds)


# ---------------------------------------------------------------------------
# text -- two different cases, and the server's job is to say which is which
# ---------------------------------------------------------------------------


def client_chat(text: str, color: str = "", timeout: float = WORLD_TIMEOUT_SECONDS) -> Result:
    """Put a line in the connected player's chat. FOCUS IS NOT NEEDED.

    Said plainly because the general rule about typing does not apply here.
    Chat is a SERVER-SIDE MESSAGE: the engine hands the mod a call that
    delivers text to a player, so the bridge sends it as data -- no keyboard,
    no window, no foreground, and nothing taken away from whoever is at the
    machine. It is also more trustworthy than real typing, because the command
    carries an id and comes back with the mod's own confirmation instead of
    "typed it and hoped".

    This is NOT the tool for a mod's own input field -- a PDA, a terminal, a
    form. Those exist only on the client and are filled by `client_type`, which
    does need the foreground.

    The line goes to EVERY connected player, and the answer says how many got
    it: the engine's call names one recipient, so a verb that quietly took the
    first player would put the line on one screen and leave it missing from the
    one the caller was watching.

    `color` is one of colorStatusChannel (the default), colorAction,
    colorFriendly or colorImportant. Anything else is refused BY THE MOD rather
    than passed on, because the client turns a colour class it does not know
    into plain white and says nothing about it. Long lines are refused too,
    rather than cut somewhere the caller cannot see.

    What success promises is that the engine accepted the call for each named
    recipient -- not that the line was visible. A client drops whole chat
    channels according to the player's own profile options, so an accepted line
    that nobody can see is a client setting, not a fault in the bridge.

    Requires the bridge to be loaded and ticking, like every other world
    command, and requires the stand to be running a bridge build that knows the
    verb; if it is not, the mod says so and the hint says what to do.
    """
    if not text or not text.strip():
        return fail(
            "client_chat needs something to say",
            hint="pass the line to put in chat, e.g. client_chat('hello')",
        )
    args: dict = {"text": text}
    if color:
        args["color"] = color

    result = _world_command("chat", args, timeout)
    if not result.ok and "unknown verb" in (result.error or ""):
        # The mod's own words are kept -- they list what this build does know.
        # Only the hint is replaced, because the generic one would send the
        # reader looking at their own project's mod rather than at the bridge.
        return Result(False, result.data, result.error, CHAT_VERB_MISSING_HINT)
    if not result.ok and "no server started by this session" in (result.error or ""):
        # Same treatment, same reason: the words are true, the generic hint is
        # not actionable here. See _foreign_stand_hint.
        return Result(
            False, result.data, result.error,
            _foreign_stand_hint(_stand_view(session.profile().machine.port)),
        )
    return result


def client_type(text: str, submit: bool = False) -> Result:
    """Type into a CLIENT-SIDE INPUT FIELD -- a mod's PDA, terminal or form.
    REQUIRES THE ACTIVE WINDOW, and takes it.

    This is not chat. Chat is a server-side message and `client_chat` delivers
    it with no focus at all; use that unless the point of the test IS the
    typing. What this tool is for is the case the bridge cannot reach: a field
    that exists only on the client, which can only be filled the way a person
    fills it -- the input line opening, the characters landing in the field,
    the keyboard layout behaving.

    So it brings the client window to the front and VERIFIES that it got there
    before sending a single keystroke. If Windows refuses -- which it does to a
    background process -- nothing is typed and the refusal says so, rather than
    keystrokes going into whatever window the person at the machine is using.
    That accident happened here once and is why the verification is not
    optional.

    A successful call reports that the foreground was taken, because that is a
    side effect on a human, not an implementation detail.

    The text is typed as US-layout scancodes, because the client starts on
    another layout and a virtual-key code would produce different characters.
    Anything with no scancode is refused by name BEFORE the screen is taken --
    an underscore that arrived as a hyphen once failed a run as if the mod were
    at fault.

    `submit` presses Enter afterwards, so filling a field and confirming it does
    not need a second tool that takes the foreground all over again. With
    `submit=True` and EMPTY text it sends Enter and nothing else, which is the
    only way anything in this tool set can confirm or open something. Measured
    on a live client: the game binds its chat line to Enter alone, and the
    virtual gamepad's A button -- the obvious candidate for a confirm -- moved
    nothing in the pause menu at 0.1 s or 0.5 s, while B (back) dismissed it at
    the default. So dismissing is a gamepad job and confirming is this one.
    Empty text WITHOUT submit is still refused: it would take the foreground to
    do nothing at all.
    """
    pid, refusal = _require_live_client()
    if refusal:
        return refusal
    if not text and not submit:
        return fail(
            "client_type needs something to type",
            hint="pass the text for the field; submit=True presses Enter afterwards, and "
                 "submit=True with no text presses Enter alone -- which is how the client's "
                 "own chat line is opened, since the gamepad has no confirm",
        )

    # Checked HERE, ahead of the focus grab, even though type_text checks it
    # again: taking the screen away from the owner and only then discovering
    # the string cannot be typed costs them the foreground for nothing.
    missing = winui.unsupported_characters(text)
    if missing:
        return fail(
            "cannot type " + ", ".join(repr(c) for c in missing)
            + ": there is no US-layout scancode for these characters",
            hint="this types scancodes because the client starts on another keyboard "
                 "layout; send the value in ASCII. (If the text was only ever meant for "
                 "chat, client_chat carries any text at all, through the bridge.)",
        )

    if not winui.focus(pid):
        holder = winui.foreground_pid()
        return fail(
            f"the client window could not be brought to the front: the foreground belongs "
            f"to pid {holder}, not to the client (pid {pid}), so nothing was typed",
            hint=FOREGROUND_REFUSED_HINT,
        )

    if text:
        typed = winui.type_text(pid, text)
        if not typed.ok:
            return typed
        data = dict(typed.data)
    else:
        data = {"typed": "", "characters": 0}

    data["foreground_taken"] = True
    data["side_effect"] = FOREGROUND_NOTICE
    data["submitted"] = False
    if submit:
        pressed = winui.press_key(pid, "enter")
        if not pressed.ok:
            return Result(False, data, pressed.error, pressed.hint)
        data["submitted"] = True
    return ok(data)


def client_key(name: str, hold_ms: int = 0) -> Result:
    """Press -- or HOLD -- one named key in the client.

    REQUIRES THE ACTIVE WINDOW, and takes it, exactly like `client_type`.

    What this reaches that nothing else does is a key a mod polls rather than
    reads as text: push-to-talk, a hold-to-open wheel, a modifier. `hold_ms`
    keeps the key down for that long, because a tap and a hold are different
    events -- a mod that samples the key once a frame can miss a press and a
    release that happen inside the same frame, and then the feature looks
    broken when it is the test that was too fast.

    For letters and words use `client_type`; for movement and menus use the
    gamepad, which needs no foreground at all.
    """
    pid, refusal = _require_live_client()
    if refusal:
        return refusal

    pressed = winui.press_key(pid, name, hold_ms)
    if not pressed.ok:
        return pressed

    data = dict(pressed.data)
    data["foreground_taken"] = True
    data["side_effect"] = FOREGROUND_NOTICE
    return ok(data)


# ---------------------------------------------------------------------------
# the client's own verdict
# ---------------------------------------------------------------------------


def client_verdict(since: float | None = None) -> Result:
    """Judge the running (or last) client by its own .RPT.

    The .RPT and not a script log, and that is a finding rather than a
    preference: in the Steam DIAG build the script log receivers are compiled
    out, so -logToFile / -logToScript / -logToRpt inject nothing and no
    script_*.log is produced for a client at all. Whatever the mod printed to
    the RPT is here; whatever it wrote only to the script log does not exist,
    and this says so instead of returning a clean verdict over an empty file.

    Not to be confused with `log_verdict(source="client")`, which judges the
    throwaway profile a `client_compile_check` job produced. This one judges
    the LIVE client -- the one `client_start` launched, against the stand.

    `since` ties the verdict to a run (pass the value `client_start` returned):
    a report last modified before it belongs to an earlier client and is
    refused as a reason rather than silently judged.
    """
    guard = require_project()
    if guard:
        return guard
    folder = client_profiles_dir()
    report = _newest(folder, "*.RPT")
    if report is None:
        return fail(
            f"no client .RPT under {folder}",
            hint="start the live client with client_start -- the game writes its .RPT into "
                 "its own -profiles directory. (For a compile check's throwaway profile, "
                 "the tool is log_verdict(source='client') instead.)",
        )
    if since is not None:
        mtime = report.stat().st_mtime
        if mtime < since:
            return fail(
                f"the newest client .RPT predates the run being judged (report last "
                f"modified at {mtime:.1f}, run started at {since:.1f})",
                hint="wait for the client to write fresh output, then call client_verdict "
                     "again with the same since",
            )
    lines = report.read_text(encoding="utf-8", errors="replace").splitlines()

    # The project's [expect] block describes the SERVER's log, and three of its
    # keys name things only a server produces: the ready line and its counters
    # come from the mod's server-side init, and max_warnings is a budget counted
    # over that same log. Judging a client .RPT by them turns a perfectly
    # healthy client into a failure -- measured on the first live client this
    # tool ever saw: verdict "fail", 0 errors, 0 crashes, and nine reasons, all
    # of them the absence of server-side facts from a client log. The rest of
    # [expect] is about the TEXT of a log line (forbidden patterns, extra error
    # patterns, project noise) and applies to any log this product reads, so it
    # is kept. `not_applied` says which keys were dropped, because a verdict
    # that silently ignores part of a declaration is its own trap.
    expect = session.profile().expect
    dropped = [
        name for name, declared in (
            ("expect.ready_line", bool(expect.ready_line)),
            ("expect.counters", bool(expect.counters)),
            ("expect.max_warnings", expect.max_warnings is not None),
        ) if declared
    ]
    data = build_verdict(
        lines, replace(expect, ready_line="", counters={}, max_warnings=None)
    )
    data["log"] = str(report)
    data["source"] = "client .RPT"
    data["not_applied"] = dropped
    data["note"] = (
        "judged from the client's .RPT, not from a script log: the Steam DIAG build has "
        "the script log receivers compiled out, so a client writes no script_*.log at "
        "all. Anything the mod printed to the RPT is judged here; anything it logged only "
        "to the script log is not missing, it was never written. This is an ERRORS-AND-"
        "CRASHES verdict: "
        + (
            "the declared " + ", ".join(dropped) + " describe the server's log and were "
            "not applied -- a client never prints the mod's server-side ready line or its "
            "counters, so applying them would fail every healthy client"
            if dropped
            else "the project declares no server-side ready line or counters, so there was "
                 "nothing to leave out"
        )
    )
    return ok(data)
