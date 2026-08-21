"""Running `binarize`, from a root that was declared instead of assumed.

**`binarize` has no project-root option.** The full switch list was enumerated
against the real binary and none of them is a root: the root is the process's
WORKING DIRECTORY. The same command, the same input, a different current
directory -- and out comes a valid ODOL with plausible-looking texture paths
that the engine renders untextured, with a success exit code and not one line
of complaint. That single fact is why this module exists and why it takes the
root as a required argument rather than inheriting whatever directory the
server happens to be sitting in.

**The tool's report is not evidence.** Three separate broken outcomes were
measured returning success:

* handed a file where it wanted a directory -- exit 0, empty output, no text;
* a material that failed to load -- exit 0, an ODOL of 46,190 bytes where a
  correct build is 58,644;
* handed an already-binarized model -- `0xC0000005`, and a ZERO-LENGTH file
  left in the output directory, on top of whatever was there before.

So every verdict here is read off the artifact (`checks.py`), and the exit code
is recorded without being believed in either direction. The third outcome is
refused before the process starts, because after it starts there is nothing
left to save: the artifact it would have replaced is already gone.

**The wait is on the process handle, with a ceiling.** Runtime was measured at
seconds, at 68 s, at 78 s, and at "still going after 120 s" -- there is no
duration that can be assumed. Worse, `binarize` spawns a FileServer grandchild
that inherits the output handle and outlives it, so waiting for end-of-stream
waits for the GRANDCHILD and an unattended job hangs forever. `procs.run_blocking`
already waits on the handle and tree-kills on expiry, which is why nothing here
starts a process itself.

**`-silent` is not optional.** Without it the tool opens message boxes and
waits for a click. In a job nobody is watching that is a hang with no
diagnosis attached.

**Its log is boilerplate.** One small jar produced 112 lines, of which 73 were
"vertices of bone X are shared with parent bone Y" -- one per bone per LOD --
and 25 were a config family that a correct `-binpath` removes entirely. The
noise list below was counted, not guessed, and what it mutes stays visible as a
count: a filter nobody can audit is how a real complaint disappears for a year.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..logparse import classify, group_key
from ..paths import BINARIZE_BINPATH_REL
from ..procs import run_blocking
from .checks import (
    PROJECT_ROOT_KEY,
    REFUSE,
    Finding,
    Report,
    c10_binarize_input_is_a_source,
    check_model,
)

#: Never traverse subdirectories. The proven invocation builds ONE directory of
#: models; recursion would pull in every sibling tree under the root, including
#: other mods' prefix folders. A source directory that holds no model is
#: refused below, so this can never silently build nothing.
NORECURSE = "-norecurse"
#: Never let the tool decide from timestamps whether to work. A skipped build
#: leaves last week's artifact in place, and every check downstream then passes
#: on it happily -- that is C2's whole subject, and this removes the cause.
ALWAYS = "-always"
#: Never open a message box, never wait for a click.
SILENT = "-silent"

#: The ceiling on one run. Generous on purpose: the point is not to guess the
#: duration -- which was measured varying by more than an order of magnitude --
#: but to guarantee that a job can always be reclaimed. Matches the packer's
#: own ceiling for FileBank.
BINARIZE_TIMEOUT = 1800.0

#: Substrings that mark a line as boilerplate. Counted in a real build's log,
#: not guessed, with the count each family contributed to that log's 112 lines.
#: Substrings rather than regexes for the same reason `logparse.DEFAULT_NOISE`
#: is: a pattern that has to be maintained is a pattern that goes stale.
NOISE: tuple[str, ...] = (
    "are shared with parent bone",   # 73 -- one per bone per LOD
    "No entry '.",                   # 19 -- config lookups with no main config
    "Trying to access error value",   # 6 -- the same family, phrased differently
    "missing in PreloadConfig",       # 5 -- appears once -binpath supplies one
    "will be too slow, should use",   # terrain advice, meaningless for a model
    "Distance 0.0 - min grid",
    "FileServer MaxCacheSize",
    "Binarize started",
    "Binarize run in silent mode",
    "BINARIZE rev.",
    "DIAG-SERIALIZED",
)

#: What `-binpath=` must contain to be worth passing.
BINPATH_PROBE = Path("bin") / "config.cpp"


@dataclass(frozen=True)
class LogDigest:
    """One run's log, with the boilerplate counted instead of printed."""

    total: int = 0
    dropped: int = 0
    kept: tuple[str, ...] = ()
    muted: tuple[tuple[str, int], ...] = ()

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "dropped": self.dropped,
            "kept": list(self.kept),
            "muted": [{"group": g, "count": c} for g, c in self.muted],
        }


@dataclass(frozen=True)
class ModelBuild:
    """One model that came out, and what the checks say about it."""

    source: str
    output: str
    size: int
    report: Report


@dataclass(frozen=True)
class BinarizeResult:
    """The outcome of one run.

    `ok` is never "the tool exited 0". It means every model asked for came out,
    every one of them passed the checks that refuse, and the run finished
    within its ceiling. `code is None` means nothing was ever started.
    """

    ok: bool
    root: str
    source: str
    output: str
    code: int | None = None
    seconds: float = 0.0
    error: str = ""
    hint: str = ""
    builds: tuple[ModelBuild, ...] = ()
    findings: tuple[Finding, ...] = ()
    notes: tuple[str, ...] = ()
    log: LogDigest | None = None

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(b.output for b in self.builds)

    @property
    def refusals(self) -> tuple[Finding, ...]:
        """Every refusing finding, from before the launch and from after it."""
        return tuple(f for f in self.findings if f.status == REFUSE) + tuple(
            f for b in self.builds for f in b.report.refusals
        )

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "root": self.root,
            "source": self.source,
            "output": self.output,
            "code": self.code,
            "seconds": round(self.seconds, 2),
            "error": self.error,
            "hint": self.hint,
            "models": [
                {"source": b.source, "output": b.output, "size": b.size,
                 "report": b.report.to_dict()}
                for b in self.builds
            ],
            "notes": list(self.notes),
            "log": self.log.to_dict() if self.log else None,
        }


def find_binpath(tools: str | os.PathLike[str] | None) -> str:
    """Where `-binpath=` should point in this DayZ Tools install, or "".

    It wants the folder that CONTAINS a `bin` directory holding the main
    config, not that `bin` directory itself. Measured, because the difference
    is the entire effect: pointed one level too deep the switch changed not a
    single line of a real build's log, and pointed correctly it removed 25 of
    112. Returns "" when the config is not there, because a `-binpath` with
    nothing behind it is a switch that only looks like it did something.
    """
    if not tools:
        return ""
    folder = Path(tools) / BINARIZE_BINPATH_REL
    return str(folder) if (folder / BINPATH_PROBE).is_file() else ""


def binarize_command(
    binarize_exe: str | os.PathLike[str],
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    binpath: str = "",
) -> list[str]:
    """The one invocation this server makes. `-silent` is in it unconditionally."""
    flags = [NORECURSE, ALWAYS, SILENT]
    if binpath:
        flags.append(f"-binpath={binpath}")
    return [str(binarize_exe), *flags, str(source), str(output)]


def digest_log(text: str) -> LogDigest:
    """Split a run's log into what is worth reading and what is boilerplate.

    A crash line is never muted, whatever else matches it. `0xC0000005` is the
    measured signature of an already-binarized model being fed back in, and it
    is the one line that explains a zero-length artifact -- the crash shapes
    come from `logparse` rather than being re-spelled here, so the two cannot
    drift apart.
    """
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    crashes = {c["text"] for c in classify(lines, [], [])["crashes"]}

    kept: list[str] = []
    muted: dict[str, int] = {}
    for line in lines:
        stripped = line.strip()
        if stripped not in crashes and any(n in line for n in NOISE):
            key = group_key(line)[:120]
            muted[key] = muted.get(key, 0) + 1
            continue
        kept.append(stripped)
    return LogDigest(
        total=len(lines),
        dropped=len(lines) - len(kept),
        kept=tuple(kept),
        muted=tuple(sorted(muted.items(), key=lambda kv: -kv[1])),
    )


# ------------------------------------------------------------------- the runner


@dataclass
class _Attempt:
    """Scratch state while the pre-flight decides whether to start anything."""

    root: Path
    source: Path
    output: Path
    notes: list[str] = field(default_factory=list)


def _refuse(
    attempt: _Attempt, error: str, hint: str = "", findings: tuple[Finding, ...] = (),
) -> BinarizeResult:
    return BinarizeResult(
        ok=False, root=str(attempt.root), source=str(attempt.source),
        output=str(attempt.output), error=error, hint=hint, findings=findings,
        notes=tuple(attempt.notes),
    )


def binarize_models(
    binarize_exe: str | os.PathLike[str] | None,
    *,
    root: str | os.PathLike[str] | None,
    source: str | os.PathLike[str] | None,
    output: str | os.PathLike[str] | None,
    log_path: str | os.PathLike[str],
    prefix: str = "",
    timeout: float = BINARIZE_TIMEOUT,
    binpath: str = "",
    run=run_blocking,
    judge=None,
) -> BinarizeResult:
    """Build every model in one directory, from the declared project root.

    `root` becomes the process's working directory, which is the only thing
    that decides what the prefixed paths inside the models will resolve to.
    `prefix` is the mod's path prefix; when it is given, the source must lie
    under `<root>/<prefix>`, and that one rule turns the measured silent
    failure -- a run from one level too deep, which produced a 46,190-byte ODOL
    where a correct build is 58,644, and exited 0 -- into a refusal that
    happens before a process exists.

    `run` is the waiter, injected so this can be exercised without DayZ Tools.
    Its default waits on the process HANDLE with a ceiling and tree-kills on
    expiry; nothing here may replace it with a wait on end-of-stream, because
    the FileServer grandchild `binarize` spawns keeps the stream open after the
    tool itself is gone.

    `judge(built, source) -> Report` decides what each output IS, and there is
    exactly one verdict per model on purpose. The default asks the questions
    this module can answer on its own: references resolved against the build
    root, and the model.cfg sitting beside the source. A caller who knows more
    -- which directory ends up in the pbo, what the packer drops, where the
    OTHER copy of the model.cfg lives -- passes its own, and that one answer
    then gates `ok`. Two passes over one artifact would mean two verdicts, and
    a build refused by one and allowed by the other is not a decision.
    """
    attempt = _Attempt(
        root=Path(root) if root else Path(),
        source=Path(source) if source else Path(),
        output=Path(output) if output else Path(),
    )

    # ---- everything below this line happens BEFORE a process is started ----

    if not binarize_exe or not Path(binarize_exe).is_file():
        return _refuse(
            attempt,
            f"binarize not found at {binarize_exe}",
            hint="install DayZ Tools, or point the server at the install that has it; "
                 "nothing can be built from an MLOD without it",
        )

    if not attempt.root.is_dir():
        return _refuse(
            attempt,
            f"the project root is not a directory: {attempt.root}",
            hint=f"declare it as {PROJECT_ROOT_KEY} in the project profile. binarize has no "
                 "project-root switch at all -- the root is whatever directory it is started "
                 "in, and started in the wrong one it produces a broken artifact and exits 0",
        )
    root_resolved = attempt.root.resolve()

    if not attempt.source.is_dir():
        return _refuse(
            attempt,
            f"the source is not a directory: {attempt.source}",
            hint="point it at the folder that holds the models. Handed a FILE, binarize exits "
                 "0, writes nothing at all and prints not one line -- there would be no way "
                 "to tell that run from a build that had nothing to do",
        )
    source_resolved = attempt.source.resolve()

    try:
        relative = source_resolved.relative_to(root_resolved)
    except ValueError:
        return _refuse(
            attempt,
            f"the source {source_resolved} is not inside the project root {root_resolved}",
            hint=f"every path inside a model resolves against the root, so a source outside "
                 f"it cannot produce references that resolve. Move the models under the root, "
                 f"or correct {PROJECT_ROOT_KEY}",
        )

    if prefix:
        first = relative.parts[0] if relative.parts else ""
        if first.lower() != prefix.lower():
            return _refuse(
                attempt,
                f"the source is under {first or '(the root itself)'!r}, not under the mod's "
                f"own {prefix!r} folder, so the root is off by at least one level",
                hint=f"set {PROJECT_ROOT_KEY} to the directory that CONTAINS {prefix!r} -- "
                     f"one level too deep is the measured silent failure: the artifact comes "
                     f"out valid, smaller, with plausible texture paths and a success code, "
                     f"and the engine renders it untextured",
            )

    sources = sorted(p for p in attempt.source.glob("*.p3d") if p.is_file())
    if not sources:
        return _refuse(
            attempt,
            f"no .p3d in {attempt.source}",
            hint="point the build at the directory holding the MLOD exports; subdirectories "
                 "are deliberately not searched",
        )

    # C10, and it only helps here. On an already-binarized model binarize dies
    # with 0xC0000005 and leaves a zero-length file in the output directory --
    # so a run that is allowed to start has already destroyed whatever artifact
    # was sitting there.
    blocked = [c10_binarize_input_is_a_source(p) for p in sources]
    refusing = tuple(f for f in blocked if f.status == REFUSE)
    if refusing:
        return _refuse(
            attempt,
            "C10 refuses the input: " + "; ".join(f.detail for f in refusing),
            hint=refusing[0].action,
            findings=refusing,
        )

    # ---- from here on a process may start ----

    attempt.output.mkdir(parents=True, exist_ok=True)
    expected = {src: (attempt.output / src.name) for src in sources}
    # Removed BEFORE the run, never after: left in place, a good artifact from
    # an earlier build makes a run that produced nothing at all look like a
    # success, and every check downstream would pass on it.
    for dst in expected.values():
        dst.unlink(missing_ok=True)

    cmd = binarize_command(binarize_exe, attempt.source, attempt.output, binpath)
    if not binpath:
        attempt.notes.append(
            "no -binpath: the log will carry the engine's config lookups (25 lines of 112 on "
            "the build this was measured against). They are muted, not read"
        )

    started = time.monotonic()
    code, tail = run(cmd, root_resolved, Path(log_path), timeout)
    seconds = time.monotonic() - started

    # The FULL log, not the waiter's tail: the tail is capped at a few thousand
    # characters and one small model's log is already larger than that, so the
    # counts would be counts of whatever happened to fit.
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = tail
    digest = digest_log(text)

    outcome = BinarizeResult(
        ok=False, root=str(root_resolved), source=str(source_resolved),
        output=str(attempt.output.resolve()), code=code, seconds=seconds, log=digest,
    )

    if code == 124:
        # `run_blocking`'s own code for "the ceiling expired and the tree was
        # killed". Whatever is in the output directory was written by a process
        # that did not finish, so it is not judged at all -- a truncated model
        # can still parse.
        return replace(
            outcome,
            error=f"binarize did not finish within {int(timeout)} s and was stopped",
            hint="raise the ceiling if the model is genuinely large, or check that the "
                 "source is what you think it is; runtime was measured varying from seconds "
                 "to well over two minutes for one small model",
            notes=tuple(attempt.notes),
        )

    if code != 0:
        attempt.notes.append(
            f"binarize exited {code}; the artifact was judged on its own, because this "
            "tool's exit code is evidence in neither direction"
        )

    builds: list[ModelBuild] = []
    missing: list[str] = []
    model_cfg = attempt.source / "model.cfg"
    prefix_dir = root_resolved / relative.parts[0] if relative.parts else root_resolved

    def default_judge(built: Path, source: Path) -> Report:
        return check_model(
            built,
            prefix=prefix,
            roots={prefix: prefix_dir} if prefix else {},
            inputs=[source],
            model_cfg=model_cfg if model_cfg.is_file() else None,
        )

    verdict = judge or default_judge
    for src, dst in expected.items():
        if not dst.exists():
            missing.append(dst.name)
            continue
        builds.append(ModelBuild(
            source=str(src.resolve()),
            output=str(dst.resolve()),
            size=dst.stat().st_size,
            report=verdict(dst, src),
        ))

    outcome = replace(outcome, builds=tuple(builds), notes=tuple(attempt.notes))
    if missing:
        return replace(
            outcome,
            error=f"binarize exited {code} and produced no {', '.join(missing)} in "
                  f"{attempt.output}",
            hint="check that the source directory is a directory and holds the models you "
                 "meant; a success code from this tool is not evidence that anything was "
                 "written -- an empty output directory with no log text was measured",
        )
    refused = outcome.refusals
    if refused:
        return replace(
            outcome,
            error="the artifact was built and refused by "
                  + ", ".join(f"{f.check} ({f.detail})" for f in refused),
            hint=refused[0].action,
        )
    return replace(outcome, ok=True)
