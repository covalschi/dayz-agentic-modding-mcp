"""Building the bridge mod, and asking whether it is alive.

The bridge is the one mod this server owns: its sources live in THIS
repository (bridge/), not in whatever project happens to be open, and every
project that uses it uses the same one. `bridge_build` therefore packs the
server's own tree with the server's own packer, and `bridge_status` answers
one question only -- is the code inside the running game still ticking.

"Alive" is deliberately expensive to claim here. The protocol has the mod
republish its state file once a second whether or not the world is
progressing, and the file outlives the server that wrote it, so its mere
existence proves nothing. Only a tick number that MOVED between two samples
proves anything, which is why this tool samples twice and why `ok` is true in
exactly that one case.

That 1 Hz publish is the protocol's design, and this file talks about it in
those terms rather than as something already happening: the bridge mod shipped
today writes a heartbeat file and nothing else -- no state document, no
mailbox reading. Every answer below is therefore reachable, but the ones that
describe a published state document only start occurring when the mod-side
task lands.

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

import json
import shutil
import threading
import time
from pathlib import Path

from ..bridge.channel import (
    CMD_FILENAME,
    HEARTBEAT_GROWING,
    HEARTBEAT_RESTARTED,
    HEARTBEAT_UNMEASURABLE,
    STATE_FILENAME,
    Channel,
)
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
# `project` is the root of the project whose job store holds `job_id`. Kept
# because the refusal has to be actionable from a DIFFERENT project: job stores
# are per project, so after a switch job_status/job_wait answer "unknown job"
# for a job that is perfectly real -- and their hint then sends the reader
# hunting a typo.
_build_in_flight: dict = {"job_id": "", "store": None, "project": ""}

# How long bridge_status may spend watching the tick. The protocol publishes
# once a second, so anything under ~2s can see the same tick twice and call a
# healthy bridge frozen; the ceiling keeps a health check from becoming a disguised
# long wait (that is what job_wait is for).
STATUS_WINDOW_DEFAULT = 2.0
STATUS_WINDOW_MAX = 10.0

# How long to wait before asking the state file a second time when the first
# read says "written by a mod older than this server". Longer than the
# protocol's 1 Hz publish interval on purpose: a document mangled by one in-place write
# is repaired by the next one, so it cannot look the same across this gap,
# while a genuinely old mod looks old however long you wait.
SECOND_OPINION_SECONDS = 1.1

# The shortest probe bridge_clear will run. The channel refuses to clear on a
# "stalled" verdict taken over a window shorter than the mod's publish interval
# -- correctly, since a live bridge that has simply not ticked again yet looks
# identical to a frozen one there. Flooring the window means a caller's small
# number never turns into a refusal about their own argument.
CLEAR_PROBE_MIN_SECONDS = 1.1

# Every field the pre-session protocol had, and nothing else. Anything outside
# this set in an otherwise old-looking document means the document was damaged
# rather than written by an old mod -- see _reads_as_pre_session.
_PRE_SESSION_KEYS = frozenset({"tick", "command", "errors", "world"})


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


def _mailbox_view(mailbox: Path) -> dict:
    """What the command mailbox looks like from outside the game.

    Reported on every answer, because "is a command sitting there" is a fact a
    caller cannot get any other way and cannot guess from the rest: nothing in
    the running game empties this file except the mod claiming the command, so
    with no mod running it is not a queue, it is a wedge. It does not decay on
    its own either -- the -profiles directory is reused across restarts -- so
    it is removed only by bridge_clear or by server_start's pre-boot clearing.
    """
    try:
        age = round(max(0.0, time.time() - mailbox.stat().st_mtime), 1)
    except OSError:
        return {"present": False, "path": str(mailbox), "age_seconds": None}
    return {"present": True, "path": str(mailbox), "age_seconds": age}


def _window_hint() -> str:
    """What to do when the tick was read but its movement was not measured."""
    return (
        f"call bridge_status with window >= {STATUS_WINDOW_DEFAULT:g} -- the mod publishes a "
        "tick once a second, so that is the smallest window that can prove movement"
    )


def _strip_signing_artifacts(mod_dir: Path) -> list[str]:
    """Leave nothing behind that claims this pbo is signed, or that carries
    anyone's key. Returns what it could NOT remove -- empty on success.

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

    BOTH halves report. The keys half used to pass `ignore_errors=True`, so a
    directory that could not be removed (held open by a scanner, a permission
    change) survived in silence while the job reported success and the summary
    said "(unsigned)" -- the precise accumulate-every-project's-key state this
    exists to prevent, with no signal left anywhere. Reported rather than
    raised, so a failure here cannot replace the packing exception it may be
    cleaning up after; the caller decides what to do with both.
    """
    problems: list[str] = []
    addons = mod_dir / "addons"
    if addons.is_dir():
        for sig in addons.glob("*.bisign"):
            try:
                sig.unlink()
            except OSError as exc:
                problems.append(f"{sig}: {exc}")
    # Every project ever opened on this machine used to leave its public key
    # here. Our own mod folder is not a collection of unrelated projects' keys.
    keys_dir = mod_dir / "keys"
    if keys_dir.exists():
        try:
            shutil.rmtree(keys_dir)
        except OSError as exc:
            problems.append(f"{keys_dir}: {exc}")
    return problems


def _not_alive(state: str, data: dict, error: str, hint: str) -> Result:
    """A refusal that still carries what was observed.

    `ok` is reserved for a bridge proven alive, so an agent that only looks at
    the envelope can never mistake a frozen tick or a stopped server for a
    working one. The measurements still come back in `data` -- a frozen tick's
    number IS the evidence, and dropping it would force the caller to go
    reading the state file by hand.
    """
    return Result(False, {"state": state, "alive": False, **data}, error, hint)


def _bridge_build_in_flight() -> tuple[str, str]:
    """(job id, owning project root) of a bridge build still running anywhere
    in this process, or ("", "").

    Held with the store that owns it and re-checked against that store rather
    than trusted as a flag: a worker that somehow never cleared the slot would
    otherwise block every later build for the life of the process, and a job's
    own recorded status is the fact of the matter either way.

    That re-check is a backstop, not the mechanism -- it only rescues a slot
    whose job status CHANGED, and the paths that used to leak a slot (a failure
    before `store.start`, a thread that never started) left the job sitting at
    "queued" forever, which this would faithfully keep refusing on. The real
    fix is that every one of those paths now releases the slot itself.

    Call under `_build_guard`.
    """
    job_id = str(_build_in_flight["job_id"] or "")
    if not job_id:
        return "", ""
    store = _build_in_flight["store"]
    job = store.get(job_id) if store is not None else None
    if job is None or job.status not in (QUEUED, RUNNING):
        return "", ""
    return job_id, str(_build_in_flight["project"] or "")


def _release_build_slot(job_id: str) -> None:
    """Give the output directory back, if this job is still the one holding it."""
    with _build_guard:
        if _build_in_flight["job_id"] == job_id:
            _build_in_flight.update(job_id="", store=None, project="")


def _abandon_build(store, job, exc: BaseException) -> Result:
    """Something between accepting the call and handing the job to a worker
    raised. Give back whatever was claimed and ANSWER -- a tool that raises
    tells the calling agent nothing at all, while the failure it hides is
    invariably the mundane one: the job store's directory is gone, read-only,
    or replaced by a file.

    `job` is None when nothing was claimed yet (the failure was in
    `store.create` itself), and the marking of the job is best-effort by
    design: the store is precisely the thing that just failed, so its refusal
    to record the failure must not become a second escaping exception.
    """
    detail = f"{type(exc).__name__}: {exc}"
    if job is not None:
        _release_build_slot(job.id)
        try:
            store.fail(job.id, f"the build never started: {detail}")
        except Exception:  # noqa: BLE001 - the store is the broken part here
            pass
    return fail(
        f"the bridge build could not be started: {detail}",
        hint=f"this is the job store or the output path, not the mod -- check that "
             f"{Path(store.root).as_posix()} exists and is writable, then try again",
    )


def bridge_build() -> Result:
    """Pack the bridge mod from this repository's own sources.

    The bridge is the server's own mod, not the project's: one copy serves every
    project, and it is built UNSIGNED -- no project's signing key is used, and
    the output folder is kept free of signatures and keys.

    Building it does not load it. Attaching it stays a profile decision (the
    job summary prints the two lines to add), because the bridge is an extra
    pbo in the stand and a run without it has to remain possible.
    """
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
    project_root = str(session.profile().root)
    job = None
    log_dir = None

    def run() -> None:
        # EVERYTHING that can fail lives inside this try, `store.start`
        # included. It used to sit above it, and anything failing there -- an
        # antivirus lock on job.json, a removed .dayz-mcp -- killed the thread
        # before its `finally`: the slot was never released, the job stayed
        # "queued" forever, and every later bridge_build in the process was
        # refused, naming a job that would never run. The only trace was a
        # traceback on stderr, where the calling agent never looks.
        leftovers: list[str] = []
        try:
            store.start(job.id)
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
                leftovers = _strip_signing_artifacts(mod_dir)
            for log in sorted(log_dir.glob("pack-*.log")):
                store.add_artifact(job.id, log)
            if result.error:
                store.fail(job.id, f"{result.name}: {result.error}")
                return
            if leftovers:
                # A pbo was built, but the folder still holds a signature or
                # someone's key. Reporting success here would put the artifact
                # back in exactly the state the strip exists to prevent, with
                # a summary saying "(unsigned)" over a signed folder.
                store.fail(
                    job.id,
                    "the bridge was packed, but its output directory still holds signing "
                    f"artifacts that could not be removed: {'; '.join(leftovers)}",
                )
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
            message = f"{type(exc).__name__}: {exc}"
            if leftovers:
                message += f"; and the output directory still holds: {'; '.join(leftovers)}"
            try:
                store.fail(job.id, message)
            except Exception:  # noqa: BLE001 - the store is the broken part here
                # Same policy as _abandon_build: when the job store itself is
                # what failed, it cannot also be where the failure is recorded.
                # The job then stays non-terminal (visible through job_status,
                # and marked lost on the next process start) -- but the slot
                # below is still given back, which is what keeps every LATER
                # build from being refused for the rest of the process.
                pass
        finally:
            _release_build_slot(job.id)

    # ONE guarded region from "this call is going ahead" to "a worker owns the
    # job", rather than a line-by-line audit of which statement can raise. Three
    # separate defects of exactly this shape were fixed one line at a time --
    # store.start(), the Thread construction, then store.artifacts_dir()'s mkdir
    # -- and each fix left the next one standing. Everything in here either
    # claims nothing or hands the claim to `run`; anything that escapes lands in
    # one handler that gives the claim back and answers the caller.
    try:
        # Claiming the output directory and creating the job happen together
        # under one lock, or two callers both pass the check before either has a
        # job to be seen. See _build_in_flight for why this is not the project's
        # job store.
        with _build_guard:
            busy, owner = _bridge_build_in_flight()
            if busy:
                shared = (f"there is one {mod_dir.name} output directory, shared by every "
                          "project, so this refusal holds across a project switch too")
                if owner and owner != project_root:
                    # The job is real, but invisible from here: job stores are
                    # per project. Saying "job_wait('...')" without this would
                    # send the caller to a tool that answers "unknown job",
                    # whose own hint then blames a typo. Spelled with as_posix()
                    # like every other path in a hint -- a raw Windows path
                    # inside quotes cannot be pasted back into project_open.
                    owner_path = Path(owner).as_posix()
                    hint = (f"that build belongs to the project at {owner_path}, and job_* "
                            f"tools only answer for the project that is open -- reopen it "
                            f"with project_open('{owner_path}') before job_wait('{busy}'). "
                            f"{shared}")
                else:
                    hint = (f"wait for it with job_wait('{busy}'), or look at it with "
                            f"job_status('{busy}') -- {shared}")
                return fail(f"a bridge build is already running (job {busy})", hint=hint)
            job = store.create(BRIDGE_BUILD_KIND)
            _build_in_flight.update(job_id=job.id, store=store, project=project_root)
        log_dir = store.artifacts_dir(job.id)
        # Construction as well as start(): both allocate, and a process out of
        # threads or memory can fail at either.
        threading.Thread(target=run, daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - a raised tool call answers nobody
        return _abandon_build(store, job, exc)
    return ok({"job_id": job.id, "mod_dir": str(mod_dir)})


def bridge_status(window: float = STATUS_WINDOW_DEFAULT) -> Result:
    """Is the bridge inside the running game still ticking?

    Blocks for up to `window` seconds (default 2, clamped to STATUS_WINDOW_MAX)
    because that is what it costs to see a tick MOVE. `window=0` returns at
    once and then usually cannot tell -- it reports "unknown", never "frozen".

    A stand with no state file at all now costs the FULL window too (measured:
    2.07s at the default, 10.05s at the cap), because the reader retries to the
    deadline rather than giving up on the first miss. If the question is only
    "is the bridge publishing anything yet" -- the usual one while wiring it up
    -- pass window=0 and get the same answer in a tenth of a second; a window
    buys movement, and nothing else.

    The answers, told apart in this order:

      no_server         nothing is running, so there is nothing to ask. Checked
                        FIRST and on the process, not on the file: the state
                        file outlives the server that wrote it, and reading a
                        leftover snapshot as a live bridge is precisely the lie
                        this ordering prevents.
      stale_command     the same, but with a command still sitting in the
                        mailbox. Its own answer because the remedy is its own:
                        the command does not expire, it blocks every send, and
                        a stand booted OUTSIDE these tools would pick it up.
                        (server_start clears the transport before every boot,
                        so a server started through these tools will not.)
      no_state_file /   the server is up but nothing readable came back. Four
      outdated_bridge / fixes, so four answers: the mod is not loaded; the mod
      invalid_state /   predates this server's protocol (no session_id at all);
      unreadable_state  the document is valid JSON but a named field is wrong
                        (the answer says which field, what was expected and
                        what was seen, and it is checked twice a publish
                        interval apart so a mangled write is never reported as
                        a schema bug); or it does not parse at all.
      alive / restarted a comparison was made. `alive` means the tick moved;
      / frozen /        `restarted` means a new world came up mid-sample (also
      unknown           alive, and NOT frozen); `frozen` means the same world
                        was seen twice without moving; `unknown` means no
                        comparison could be made. Only the first two return ok.
    """
    guard = require_project()
    if guard:
        return guard

    window = max(0.0, min(window, STATUS_WINDOW_MAX))
    profiles = server_profiles_dir()
    state_file = profiles / STATE_FILENAME
    mailbox = _mailbox_view(profiles / CMD_FILENAME)
    pid = session.server_pid()
    base = {
        "server_pid": pid,
        "state_file": str(state_file),
        "mailbox": mailbox,
        "window": window,
        # The channel's own verdict, passed through unchanged rather than
        # re-spelled: "growing" / "stalled" / "restarted" / "unmeasurable".
        # None on the answers that never got as far as measuring anything.
        "heartbeat": None,
        "tick": None,
        # The live world's id, on every answer that actually read a sample.
        # Task 5's acceptance probes need it, and nothing else can tell them
        # what it is.
        "session_id": None,
        "advancing": None,
    }

    if not (pid and is_alive(pid, image=session.server_image())):
        if mailbox["present"]:
            # Not the same answer as "no server". Only the mod ever removes
            # this file, so with nothing running it can never be claimed --
            # and server_start reuses the -profiles directory, so the command
            # does not expire, it WAITS. That the file survives a boot is
            # measured; that the mod then runs it is not yet -- the shipped
            # bridge reads no mailbox at all today. So the wording states the
            # part that is true now and stays true once command reading lands,
            # rather than asserting a behaviour the current mod does not have.
            return _not_alive(
                "stale_command",
                base,
                f"no server is running, and a command has been sitting unclaimed in "
                f"{mailbox['path']} for {mailbox['age_seconds']}s -- nothing can claim it "
                "while the stand is down, and it does not expire on its own. It still blocks "
                "every send, and a stand booted OUTSIDE these tools would pick it up",
                hint="discard it with bridge_clear(); server_start also clears the transport "
                     "before every boot, so a server started through this tool will not run "
                     f"it (the file itself is {mailbox['path']})",
            )
        return _not_alive(
            "no_server",
            base,
            "no server started by this session is running, so there is nothing for the "
            "bridge to run inside",
            hint="start one with server_start and wait for the job to finish, then call "
                 "bridge_status again (only servers this session started are tracked)",
        )

    # heartbeat() samples the tick at both ends of `window`, tolerating the
    # torn reads that come with a mod that cannot write atomically, and reports
    # FOUR outcomes rather than a bool. Each one gets its own answer here --
    # collapsing "could not take the second sample" into "the tick did not
    # move" is how a measurement failure became a diagnosis, and sent a reader
    # hunting script errors that were never there.
    # heartbeat_detail, not heartbeat: the plain (status, tick) reduction has no
    # room for the session id, and cannot say whether a sample was read at all.
    # This replaced a reach-in to the channel's private sampling internals --
    # the information is public now, so the layering violation goes away rather
    # than being re-aimed at whatever those internals became.
    channel = Channel(profiles)
    detail = channel.heartbeat_detail(window)
    status, tick = detail.status, detail.tick
    observed = {**base, "heartbeat": status, "tick": tick, "session_id": detail.session_id}

    if status == HEARTBEAT_GROWING:
        return ok({"state": "alive", "alive": True, **observed, "advancing": True})

    if status == HEARTBEAT_RESTARTED:
        # A DIFFERENT world published between the two samples. Alive, and the
        # exact opposite of frozen -- but its own answer rather than a flavour
        # of "alive", because two things follow that "alive" would hide:
        # nothing was measured about movement within the new session, and
        # anything sent to the previous one died with it.
        return ok(
            {
                "state": "restarted", "alive": True, **observed, "advancing": None,
                # BOTH halves of the restart: what it was and what it is now.
                # "a restart happened" without the old id leaves a caller unable
                # to say whether the session IT was talking to is the one that
                # went away.
                "previous_session_id": detail.previous_session_id,
                "note": "a new world came up between the two samples -- the tick belongs to "
                        "that new session and was deliberately not compared with the old "
                        "one. Any command sent to the previous session is gone with it.",
            }
        )

    if status == HEARTBEAT_UNMEASURABLE:
        # WAS a sample read -- not "is its tick non-zero". The reduction reports
        # the last tick seen, or 0 when it saw nothing, so a mod publishing a
        # genuine tick 0 is indistinguishable from "nothing readable" by that
        # number alone. Branching on the value sent a readable tick-0 sample to
        # `unreadable_state` -- claiming nothing could be read when something
        # was, throwing the tick away, and advising a rebuild -- while the same
        # scenario at tick 7 answered correctly.
        #
        # An extra read was tried here and is not enough: it fails exactly when
        # the second sample failed for a persistent reason (a torn file that
        # stays torn), which is the case that produced the report. `before` is
        # the only thing that answers the question, and it is why the raw
        # samples are taken above.
        if _a_sample_was_read(detail):
            return _not_alive(
                "unknown",
                observed,
                f"read one sample at tick {tick}, but the second could not be read, "
                "so whether the tick is advancing was not measured",
                hint=_window_hint(),
            )
        return _no_snapshot_answer({**base, "heartbeat": status}, channel, state_file, mailbox)

    # HEARTBEAT_STALLED: two samples, the same world, the same tick.
    if window <= 0:
        return _not_alive(
            "unknown",
            observed,
            f"the bridge published tick {tick}, but with window=0 the two samples are taken "
            "back to back, which cannot show movement either way",
            hint=_window_hint(),
        )

    return _not_alive(
        "frozen",
        {**observed, "advancing": False},
        f"the bridge's tick is stuck at {tick} over {window}s -- the same world was observed "
        "twice and did not move, so the state file is there while nothing inside the game "
        "updates it",
        hint="the server process is alive while its script side is not: look for script errors "
             "with log_verdict and log_tail, then restart with server_stop and server_start",
    )


def _a_sample_was_read(detail) -> bool:
    """Did the probe read a state document at all?

    `HeartbeatSample.session_id` is documented as the most recently observed
    session, and `None` only when NEITHER sample came back -- so this is the
    field that says what is being asked, rather than a sentinel that happens to
    correlate (the tick does not: a mod publishing a genuine tick of 0 is
    indistinguishable from "nothing read" by that number, which is exactly the
    defect this replaced).

    It rests on one invariant of the layer below: a state document that parses
    always carries a non-empty session id, because `parse_state` rejects any
    other. If that ever stops holding, "read but sessionless" would silently
    read as "not read" here -- so the invariant is pinned by a test of its own
    rather than assumed (see test_tools_bridge's seam test).
    """
    return detail.session_id is not None


def _persistent_rejection(channel: Channel):
    """A schema rejection that is still there a publish interval later, or None.

    The difference between a mod-side bug and a moment's bad luck, and the same
    discipline `_predates_the_session_contract` uses for the same reason: a
    genuine schema mistake repeats on every tick until someone fixes the field,
    while a document mangled by one in-place overwrite is repaired by the very
    next write. Without the second look, a single mangled write would tell an
    author to go and fix a field that is perfectly correct -- and the answer
    would claim, falsely, that it will keep happening every tick.

    Same field both times, not merely "some rejection twice": two different
    fields failing in succession is a file being written through, not a
    consistent shape being published.
    """
    first = channel.read_state_rejection()
    if first is None:
        return None
    time.sleep(SECOND_OPINION_SECONDS)
    second = channel.read_state_rejection()
    if second is None or second.field != first.field:
        return None
    return second


def _render_value(value: object) -> str:
    """The offending value as the caller would recognise it, truncated. Kept as
    a real Python value all the way from the parser so this layer chooses the
    presentation -- and quoted, because "7" and 7 are the whole difference in
    the most common of these."""
    text = repr(value)
    return text if len(text) <= 120 else text[:117] + "..."


def _no_snapshot_answer(data: dict, channel: Channel, state_file: Path, mailbox: dict) -> Result:
    """Nothing readable came back at all. Four different reasons, four
    different fixes, and getting this wrong is expensive in both directions."""
    data = {**data, "invalid_field": None}
    if state_file.exists():
        if _predates_the_session_contract(state_file):
            # The one cost of making session_id required. A state document that
            # parses cleanly but has no session_id was written by a bridge mod
            # older than this server -- calling that "the mod is writing
            # something it cannot finish" is simply false, and sends the reader
            # to log_verdict and a rebuild of their own mod instead of ours.
            return _not_alive(
                "outdated_bridge",
                data,
                f"{state_file} parses, but carries no session_id -- it was written by a "
                "bridge mod older than this server, which cannot tell a restart from a "
                "freeze and is therefore not trusted",
                hint="rebuild the bridge with bridge_build and restart the server; nothing "
                     "is wrong with the project's own mod",
            )
        # A document that IS valid JSON but breaks the schema is a mod-side bug
        # that will repeat on every tick until the field is fixed -- the exact
        # opposite of a torn write, which fixes itself a millisecond later.
        # Checked AFTER the pre-session test above, which is the stricter and
        # more specific claim: a document missing session_id entirely is
        # reported as an outdated mod, not as a field to go and correct.
        rejection = _persistent_rejection(channel)
        if rejection is not None:
            seen = "" if rejection.value is None and "missing" in rejection.reason else (
                f", saw {_render_value(rejection.value)}"
            )
            return _not_alive(
                "invalid_state",
                {**data, "invalid_field": rejection.field, "invalid_reason": rejection.reason,
                 "invalid_value": _render_value(rejection.value)},
                f"{state_file} is valid JSON, but the field {rejection.field!r} is not usable: "
                f"{rejection.reason}{seen}. The mod is publishing a state document this "
                "server cannot accept, and will keep publishing it every tick",
                hint=f"fix {rejection.field!r} where the bridge mod writes its state document "
                     "(bridge/scripts), then bridge_build and restart the server. This is a "
                     "schema mistake in the document, not a half-written file and not a "
                     "script error -- nothing in the server log will mention it",
            )

        return _not_alive(
            "unreadable_state",
            data,
            f"{state_file} exists but no readable snapshot could be taken from it",
            hint="a single torn read is ordinary and already retried; a file that never "
                 "parses means the mod is writing something it cannot finish -- check "
                 "log_verdict and log_tail for script errors, and rebuild with bridge_build",
        )

    waiting = ""
    if mailbox["present"]:
        # The bridge is not loaded, so nothing will ever claim this -- and the
        # moment the wiring is fixed, the first tick runs a command sent long
        # before. Better said here than discovered then.
        # Says what actually works from HERE. A plain bridge_clear() refuses in
        # this exact state -- the server is running, which is this tool's own
        # liveness gate -- for a command nothing in that world can claim,
        # because the bridge is not loaded. An instruction that leads to a
        # refusal costs a call to find out, so it names the flag.
        waiting = (
            f"; a command has also been waiting unclaimed in {mailbox['path']} for "
            f"{mailbox['age_seconds']}s, and it will not expire on its own -- it waits for a "
            "bridge that reads commands. Nothing in this world can claim it (the bridge is "
            "not loaded), but the server IS running, so discarding it takes "
            "bridge_clear(force=True), or server_stop first"
        )
    return _not_alive(
        "no_state_file",
        data,
        f"the server is running but the bridge has never written {state_file}{waiting}",
        hint=f"build it with bridge_build, then let the stand load it: {wiring_instructions()}",
    )


def _reads_as_pre_session(state_file: Path) -> bool:
    """One read: does this look like a COMPLETE document from the protocol
    that predates `session_id`?

    Strict about the key set, not just about `session_id` being absent. The
    loose version rested on "a torn write cannot produce valid JSON", which
    holds only if the mod TRUNCATES when it overwrites. Under a non-truncating
    in-place overwrite it does not: a length change ahead of the key (tick 999
    to 1000) can leave valid JSON whose `session_id` has been mangled into
    something else -- and the tool then tells the user to rebuild a perfectly
    current bridge. Which write model the mod actually uses is still an open
    question on the mod side, so this must not depend on the answer.
    Every such mangle leaves a key that does not belong, which `keys <=
    _PRE_SESSION_KEYS` rejects; a genuinely old document carries only the
    fields that protocol had.

    The cost is that an old mod which wrote extra fields of its own reads as
    corrupt rather than as outdated -- a vaguer answer, never a false
    accusation, which is the right way round for advice that says "rebuild".
    """
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict) or "session_id" in raw:
        return False
    if "tick" not in raw or not set(raw) <= _PRE_SESSION_KEYS:
        return False
    return isinstance(raw["tick"], int) and not isinstance(raw["tick"], bool)


def _predates_the_session_contract(state_file: Path) -> bool:
    """Two agreeing reads, a full publish interval apart.

    The second read is the half that survives BOTH write models: a mangle from
    an in-place overwrite is transient -- the next publish repairs it -- so it
    cannot look pre-session twice across a whole interval. A mod that really is
    old looks old on every read, however many are taken.

    Only reached when nothing readable came back at all, which is already a
    failing answer, so the wait buys the difference between "rebuild your
    bridge" and "your state file is corrupt" at a cost nobody is timing.
    """
    if not _reads_as_pre_session(state_file):
        return False
    time.sleep(SECOND_OPINION_SECONDS)
    return _reads_as_pre_session(state_file)


def bridge_clear(force: bool = False, probe_window: float = STATUS_WINDOW_DEFAULT) -> Result:
    """Discard whatever command is sitting in the mailbox.

    The remedy for `bridge_status`'s `stale_command`. Inside the game, claiming
    a command IS deleting the file, so a command sent while the stand was down,
    or before the bridge was wired into -serverMod, is never claimed and never
    expires on its own: it blocks every later send until something removes it.
    Two things on this side do -- this tool, and server_start, which clears the
    transport before every boot. A stand booted outside these tools would run
    the command instead.

    Its own tool, and never a side effect of asking for status: throwing away a
    queued command is a decision, and `bridge_status` reporting the wedge must
    not be the thing that silently resolves it.

    Refuses when anything suggests the bridge is alive, because a running mod
    could claim that command at any moment and destroying live in-flight work
    is worse than leaving the wedge. FIRST on the plain fact that a server this
    session started is running -- whatever its bridge is or is not publishing,
    which matters most for a mod that has not started writing state yet -- and
    that refusal costs no probe at all. Otherwise the channel probes for
    `probe_window`
    seconds and refuses on a tick that moved, on a world that restarted, AND on
    a readable first sample followed by an unreadable second one -- that last
    is proof something was alive moments ago, which a downed stand never
    produces. `force=True` overrides all of it, and what it overrode is
    reported either way.
    """
    guard = require_project()
    if guard:
        return guard

    # The state file is not the only evidence of life, and this layer holds the
    # other half. A server this session started IS running whatever its bridge
    # publishes -- and a mod that has not written a state document yet (every
    # mod until the state writer lands) looks exactly like a downed stand to a
    # probe that only reads files. Checked BEFORE the probe, so a refusal costs
    # nothing: the channel now retries its first sample to the window's
    # deadline, which would otherwise buy a whole window to learn nothing.
    pid = session.server_pid()
    server_running = bool(pid and is_alive(pid, image=session.server_image()))
    if server_running and not force:
        return fail(
            f"a server this session started is running (pid {pid}), so the bridge inside it "
            "could claim this command at any moment",
            hint="stop it with server_stop, or pass force=True if you are certain the command "
                 "should be discarded out from under a live world; bridge_status first if you "
                 "are not sure",
        )

    # Floored as well as capped: below the mod's publish interval a "same tick"
    # verdict proves nothing, and the channel rightly demands force for it --
    # so a caller passing 0.1 would be refused over their own window rather
    # than over anything about the bridge.
    probe_window = max(CLEAR_PROBE_MIN_SECONDS, min(probe_window, STATUS_WINDOW_MAX))
    profiles = server_profiles_dir()
    result = Channel(profiles).clear_mailbox(force=force, probe_window=probe_window)
    if not result.ok:
        # The channel's own refusals ("already empty", "looks alive, pass
        # force=True") already name the path and the way out; re-wording them
        # here would only create a second version of the same message.
        return result

    data = dict(result.data or {})
    discarded = data.get("discarded")
    # The id, promoted out of the payload: "which command did I just throw
    # away" is the question a caller actually has, and making them dig it out
    # of a nested dict invites not checking at all.
    data["discarded_id"] = discarded.get("id") if isinstance(discarded, dict) else None
    data["mailbox"] = str(profiles / CMD_FILENAME)
    data["forced"] = force
    # What a force overrode ON THIS SIDE, next to the channel's own
    # override_reason: a forced clear over a live server is the one that can
    # destroy work someone is waiting on.
    data["server_running"] = server_running
    data["server_pid"] = pid
    return ok(data)
