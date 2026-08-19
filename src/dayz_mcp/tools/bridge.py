"""Building the bridge mod, and asking whether it is alive.

The bridge is the one mod this server owns: its sources live in THIS
repository (bridge/), not in whatever project happens to be open, and every
project that uses it uses the same one. `bridge_build` therefore packs the
server's own tree with the server's own packer, and `bridge_status` answers
one question only -- is the code inside the running game still ticking.

"Alive" is deliberately expensive to claim here. The mod republishes its state
file once a second whether or not the world is progressing, and the file
outlives the server that wrote it, so its mere existence proves nothing. Only
a tick number that MOVED between two samples proves anything, which is why
this tool samples twice and why `ok` is true in exactly that one case.

The bridge is built UNSIGNED, and that is a ruling, not an oversight. There is
one output directory for the whole process, and it used to be fed by whichever
project happened to be open: the project's private key signed our mod, its
public key accumulated inside our mod folder, and a build under a key-less
project left the PREVIOUS project's signature sitting next to a pbo it no
longer covered. A mod loaded through -serverMod never travels to a client, so
nothing verifies it; consuming a user's signing identity to produce it was
paying a real price for nothing. Verified on a live stand -- see the task
report.
"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

from ..bridge.channel import STATE_FILENAME, Channel
from ..errors import Result, fail, ok
from ..jobs import QUEUED, RUNNING
from ..packer import pack_one
from ..procs import is_alive
from . import session
from .lifecycle import server_profiles_dir
from .project import require_project

# The repository this server is running from: <root>/src/dayz_mcp/tools/bridge.py.
# The bridge's sources are data shipped alongside the code, so they are found
# relative to the code rather than to any project or working directory.
SERVER_REPO_ROOT = Path(__file__).resolve().parents[3]

# Also the pbo name, the config.cpp class name and the @folder name -- the same
# one identity, spelled once here (see bridge/config.cpp).
BRIDGE_MOD_NAME = "DZMCP_Bridge"

# The job kind, kept distinct from mod_build's "build": the two write different
# output directories, so one must not lock the other out.
BRIDGE_BUILD_KIND = "bridge-build"

# The guard against two bridge builds at once. PROCESS-global, because the
# thing it protects is: there is exactly one @DZMCP_Bridge output directory,
# whichever project is open. A per-project check (the job store) reads as the
# obvious choice and is wrong -- project_open(B) mid-build walks straight past
# a build still running for project A, and both then write the same pbo. The
# spec asks for acceptance on two projects, so that sequence is routine.
_build_guard = threading.Lock()
_build_in_flight: dict = {"job_id": "", "store": None}

# How long bridge_status may spend watching the tick. The mod publishes once a
# second, so anything under ~2s can see the same tick twice and call a healthy
# bridge frozen; the ceiling keeps a health check from becoming a disguised
# long wait (that is what job_wait is for).
STATUS_WINDOW_DEFAULT = 2.0
STATUS_WINDOW_MAX = 10.0


def session_tools_root() -> str | None:
    return session.tools_root()


def bridge_source_dir() -> Path:
    return SERVER_REPO_ROOT / "bridge"


def bridge_mod_dir() -> Path:
    """Where the packed bridge lands: a stable, absolute path inside this
    repository. Not inside the open project -- the bridge is not the project's
    mod, and dropping a build artifact into someone's repository to make a
    command line shorter is not a trade worth making."""
    return SERVER_REPO_ROOT / f"@{BRIDGE_MOD_NAME}"


def wiring_instructions() -> str:
    """How a stand actually ends up loading the bridge.

    Deliberately a profile edit rather than something server_start does by
    itself: the bridge is an extra pbo in the stand, so it changes the system
    being measured, and a run WITHOUT it has to stay possible. The profile
    decides -- see the spec's own note on this.

    Reachable from BOTH the successful build and the "never wrote state"
    refusal. Only the refusal carried it once, which meant the instructions
    arrived one wasted boot too late.
    """
    return (
        f'add "{bridge_mod_dir().as_posix()}" to mods.extra and "@{BRIDGE_MOD_NAME}" to '
        f"mods.server_only in dayz-mcp.local.toml, so server_start passes it as -serverMod"
    )


def _window_hint() -> str:
    """What to do when the tick was read but its movement was not measured."""
    return (
        f"call bridge_status with window >= {STATUS_WINDOW_DEFAULT:g} -- the mod publishes a "
        "tick once a second, so that is the smallest window that can prove movement"
    )


def _strip_signing_artifacts(mod_dir: Path) -> None:
    """Leave nothing behind that claims this pbo is signed, or that carries
    anyone's key.

    The bridge is built unsigned (see the module docstring), which pack_one
    handles correctly for the pbo itself -- but its stale-signature cleanup
    lives inside `if priv:`, so a build that signs nothing also unlinks
    nothing. The measured result was a real .bisign covering a 3564-byte pbo
    sitting next to the 3648-byte pbo that replaced it, while the summary said
    "(unsigned)". A stand that verifies signatures rejects that, and
    bridge_status then blames the wiring -- sending the agent to fix something
    already correct.

    Run whatever the build did, including when it failed: a failed build leaves
    the OLD pbo in place, and a signature next to an out-of-date artifact is
    worse than no signature at all.
    """
    addons = mod_dir / "addons"
    if addons.is_dir():
        for sig in addons.glob("*.bisign"):
            sig.unlink(missing_ok=True)
    # Every project ever opened on this machine used to leave its public key
    # here. Our own mod folder is not a collection of unrelated projects' keys.
    shutil.rmtree(mod_dir / "keys", ignore_errors=True)


def _not_alive(state: str, data: dict, error: str, hint: str) -> Result:
    """A refusal that still carries what was observed.

    `ok` is reserved for a bridge proven alive, so an agent that only looks at
    the envelope can never mistake a frozen tick or a stopped server for a
    working one. The measurements still come back in `data` -- a frozen tick's
    number IS the evidence, and dropping it would force the caller to go
    reading the state file by hand.
    """
    return Result(False, {"state": state, "alive": False, **data}, error, hint)


def _bridge_build_in_flight() -> str:
    """The id of a bridge build still running anywhere in this process, or "".

    Held with the store that owns it and re-checked against that store rather
    than trusted as a flag: a worker that somehow never cleared the slot would
    otherwise block every later build for the life of the process, and a job's
    own recorded status is the fact of the matter either way. Call under
    `_build_guard`.
    """
    job_id = str(_build_in_flight["job_id"] or "")
    if not job_id:
        return ""
    store = _build_in_flight["store"]
    job = store.get(job_id) if store is not None else None
    if job is None or job.status not in (QUEUED, RUNNING):
        return ""
    return job_id


def bridge_build() -> Result:
    """Pack the bridge mod from this repository's own sources."""
    guard = require_project()
    if guard:
        return guard
    tools_root = session_tools_root()
    if not tools_root:
        return fail(
            "DayZ Tools not found",
            hint="install DayZ Tools from Steam, or set machine.tools in dayz-mcp.local.toml",
        )

    # Read once, here, so the worker thread cannot see a different repository
    # than the call that validated it.
    repo_root = SERVER_REPO_ROOT
    src = bridge_source_dir()
    if not (src / "config.cpp").is_file():
        return fail(
            f"the bridge sources are not where this server expects them: {src}",
            hint="bridge/ ships with this server's repository -- run the server from a source "
                 "checkout, or restore bridge/config.cpp",
        )

    store = session.jobs()
    mod_dir = bridge_mod_dir()
    # Claiming the output directory and creating the job happen together under
    # one lock, or two callers both pass the check before either has a job to
    # be seen. See _build_in_flight for why this is not the project's job store.
    with _build_guard:
        busy = _bridge_build_in_flight()
        if busy:
            return fail(
                f"a bridge build is already running (job {busy})",
                hint=f"wait for it with job_wait('{busy}'), or look at it with job_status('{busy}') "
                     f"-- there is one {mod_dir.name} output directory, shared by every project, "
                     "so this refusal holds across a project switch too",
            )
        job = store.create(BRIDGE_BUILD_KIND)
        _build_in_flight.update(job_id=job.id, store=store)
    log_dir = store.artifacts_dir(job.id)

    def run() -> None:
        store.start(job.id)
        # An uncaught exception here must still resolve the job, or it stays
        # "running" forever and the traceback goes only to the server's stderr,
        # where the calling agent never sees it.
        try:
            try:
                result = pack_one(
                    BRIDGE_MOD_NAME,
                    # OUR repository, not the project's root. `root` is only
                    # what pack_one reads keys/ from and runs FileBank in, and
                    # this repository ships no keys -- which is exactly the
                    # point: the bridge is built unsigned and no project's
                    # signing identity is ever consumed (module docstring).
                    repo_root,
                    Path(tools_root),
                    log_dir / f"pack-{BRIDGE_MOD_NAME}.log",
                    mod_dir=mod_dir,
                    src=src,
                    # NOT optional. FileBank names its output after the SOURCE
                    # FOLDER's basename, not after -property prefix=, so packing
                    # "bridge/" directly produces bridge.pbo and pack_one then
                    # correctly reports DZMCP_Bridge.pbo "was not produced".
                    # Staging copies the source into a directory named after the
                    # mod, which is the supported way to pack a folder whose name
                    # differs from the mod's -- and it keeps pack_one's stale-pbo
                    # check honest, because that check measures `src`, the real
                    # tree, never the copy.
                    stage=True,
                )
            finally:
                # Whatever happened -- built, refused, or raised -- the output
                # directory must not be left claiming a signature or holding
                # someone's key.
                _strip_signing_artifacts(mod_dir)
            for log in sorted(log_dir.glob("pack-*.log")):
                store.add_artifact(job.id, log)
            if result.error:
                store.fail(job.id, f"{result.name}: {result.error}")
                return
            # "(unsigned)" is stated, not implied: it is the intended outcome
            # here, and a reader who knows mod_build's summaries would otherwise
            # read its absence as a signing failure.
            summary = f"{result.name} {result.size}B (unsigned) -> {mod_dir}"
            if result.note:
                summary = f"{summary} | notes: {result.note}"
            summary = f"{summary} | to load it: {wiring_instructions()}"
            store.finish(job.id, 0, summary=summary)
        except Exception as exc:  # noqa: BLE001 - must reach the job, not just stderr
            store.fail(job.id, f"{type(exc).__name__}: {exc}")
        finally:
            with _build_guard:
                if _build_in_flight["job_id"] == job.id:
                    _build_in_flight.update(job_id="", store=None)

    threading.Thread(target=run, daemon=True).start()
    return ok({"job_id": job.id, "mod_dir": str(mod_dir)})


def bridge_status(window: float = STATUS_WINDOW_DEFAULT) -> Result:
    """Is the bridge inside the running game still ticking?

    Blocks for up to `window` seconds (default 2, clamped to STATUS_WINDOW_MAX)
    because that is what it costs to see a tick MOVE. `window=0` returns at
    once and then usually cannot tell -- it reports "unknown", never "frozen".

    Three genuinely different answers, and they are told apart in this order:

      no_server         nothing is running, so there is nothing to ask. Checked
                        FIRST and on the process, not on the file: the state
                        file outlives the server that wrote it, and reading a
                        leftover snapshot as a live bridge is precisely the lie
                        this ordering prevents.
      no_state_file /   the server is up but the mod never published anything --
      unreadable_state  almost always "the bridge is not in -serverMod", or was
                        never built. Told apart because the fixes differ.
      alive / frozen /  a snapshot exists; the tick decides. Only a tick that
      unknown           advanced returns ok.
    """
    guard = require_project()
    if guard:
        return guard

    window = max(0.0, min(window, STATUS_WINDOW_MAX))
    profiles = server_profiles_dir()
    state_file = profiles / STATE_FILENAME
    pid = session.server_pid()
    base = {
        "server_pid": pid,
        "state_file": str(state_file),
        "window": window,
        "tick": None,
        "advancing": None,
    }

    if not (pid and is_alive(pid, image=session.server_image())):
        return _not_alive(
            "no_server",
            base,
            "no server started by this session is running, so there is nothing for the "
            "bridge to run inside",
            hint="start one with server_start and wait for the job to finish, then call "
                 "bridge_status again (only servers this session started are tracked)",
        )

    # heartbeat() samples the tick at both ends of `window`, tolerating the
    # torn reads that come with a mod that cannot write atomically.
    channel = Channel(profiles)
    growing, tick = channel.heartbeat(window)

    if tick <= 0:
        # heartbeat reports 0 both for "nothing readable at either end" and for
        # a snapshot whose tick genuinely IS 0, and the two deserve opposite
        # answers: one sends the reader hunting script errors, the other is a
        # perfectly well-formed file. One extra read settles it.
        snapshot = channel.read_state()
        if snapshot is not None:
            return _not_alive(
                "unknown",
                {**base, "tick": snapshot.tick},
                f"the state file reads back cleanly with tick {snapshot.tick}, but no two "
                "comparable samples were obtained across the window, so whether the tick is "
                "advancing is unknown",
                hint=_window_hint(),
            )
        if state_file.exists():
            return _not_alive(
                "unreadable_state",
                base,
                f"{state_file} exists but no readable snapshot could be taken from it",
                hint="a single torn read is ordinary and already retried; a file that never "
                     "parses means the mod is writing something it cannot finish -- check "
                     "log_verdict and log_tail for script errors, and rebuild with bridge_build",
            )
        return _not_alive(
            "no_state_file",
            base,
            f"the server is running but the bridge has never written {state_file}",
            hint=f"build it with bridge_build, then let the stand load it: {wiring_instructions()}",
        )

    observed = {**base, "tick": tick}
    if growing:
        return ok({"state": "alive", "alive": True, **observed, "advancing": True})

    if window <= 0:
        return _not_alive(
            "unknown",
            observed,
            f"the bridge published tick {tick}, but with window=0 there is no second sample "
            "to compare it against, so whether it is still advancing is unknown",
            hint=_window_hint(),
        )

    return _not_alive(
        "frozen",
        {**observed, "advancing": False},
        f"the bridge's tick is stuck at {tick} over {window}s -- the state file is there, but "
        "nothing inside the game is updating it",
        hint="the server process is alive while its script side is not: look for script errors "
             "with log_verdict and log_tail, then restart with server_stop and server_start. "
             "Call bridge_status once more first -- a single torn read at the wrong moment can "
             "also look like a stuck tick",
    )
