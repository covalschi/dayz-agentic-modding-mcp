"""Judging a built model artifact: C1-C12.

Nothing here runs a tool. Every check reads a file that already exists and
says whether it is good, because the tools that produce these files are
structurally unable to report failure -- three separate broken outcomes were
measured returning a success code, one of them with an empty output directory
and not a line of text.

**Why some of these refuse instead of warning.** Four of the twelve have an
unambiguous answer and zero false positives across every artifact on the
machine they were built against: C1 (a built model exists, has bytes, and is
an `ODOL`), C3 (no reference escapes the mod), C4 (a resolved material was
inlined) and C10 (never hand an `ODOL` back to `binarize`). Those refuse. The
rest are heuristics -- a clock comparison, a file that may legitimately be
absent, a name that may legitimately be short -- and they warn. A warning in
a log nobody reads is exactly how every one of these traps survived.

**Why every finding carries an action.** "The paths are wrong" is not
actionable; the paths are an effect. A build made from the wrong working
directory is fixed by declaring the project root once, in the profile, so
nothing has to remember to `cd` -- and the refusal says that, because the
person reading it is not the person who learned it.

Three false alarms were designed OUT of this module, each of which would have
fired on an artifact that works in game:

* `binarize` collapses five LODs into four. Nothing here compares LOD counts
  between a source and its build.
* The vanilla prefix `dz` is legitimate in a correct artifact -- `binarize`
  inlines the game's own textures. C3 and C5 pass it.
* A short animation name is missing from the reader's filtered string set by
  design. C8 asks the unfiltered superset instead.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from ..packer import name_matches
from .p3d import (
    MLOD,
    ODOL,
    RVMAT_INLINE_MARKERS,
    UNKNOWN,
    Fingerprint,
    P3dError,
    P3dInfo,
    asciiz_strings,
    fingerprint,
    inlined_material_markers,
    parse_p3d,
)
from .paa import DXT1, PaaError, alpha_levels, paa_format

#: What a check can conclude. `PASS` and `SKIP` are both quiet, and they are
#: not the same thing: `SKIP` means the question could not be asked, and a
#: caller that treats it as a pass is trusting an answer nobody gave.
PASS = "pass"
WARN = "warn"
REFUSE = "refuse"
SKIP = "skip"

#: Path prefixes that belong to the game rather than to any mod. `binarize`
#: inlines these into a CORRECT artifact, so a check that refused them would
#: refuse every good build there is.
VANILLA_PREFIXES: tuple[str, ...] = ("dz",)

#: The profile key that declares where `binarize` must run from. Named here,
#: once, so the refusal text and the profile loader cannot drift apart.
PROJECT_ROOT_KEY = "build.project_root"

#: A finding must stay a decision, not a data dump. A model with two hundred
#: dangling references would otherwise hand the caller its reference list back
#: in place of an answer; the true count is always in the detail, so the
#: truncation is visible rather than silent.
MAX_EVIDENCE = 20
_QUOTED_IN_DETAIL = 3

_REFERENCE_SUFFIXES = (".paa", ".rvmat", ".p3d")
_SEPARATORS = ("\\", "/")


@dataclass(frozen=True)
class Finding:
    """One check's answer about one artifact.

    `detail` says what was seen, `action` says what to do about it. A finding
    that fires without an action is a defect in this module: the whole reason
    these checks refuse instead of logging is that nobody acts on prose.
    """

    check: str
    title: str
    status: str
    detail: str = ""
    action: str = ""
    evidence: tuple[str, ...] = ()

    @property
    def fired(self) -> bool:
        return self.status in (WARN, REFUSE)


@dataclass(frozen=True)
class Report:
    """Every finding about one artifact, and whether it may be shipped."""

    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """False only for a refusal. A warning never stops a build -- that
        distinction is the whole point of having two severities."""
        return not any(f.status == REFUSE for f in self.findings)

    @property
    def refusals(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.status == REFUSE)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.status == WARN)

    @property
    def summary(self) -> str:
        refused = ", ".join(f.check for f in self.refusals)
        warned = ", ".join(f.check for f in self.warnings)
        if refused:
            return f"refused by {refused}" + (f"; warnings from {warned}" if warned else "")
        return f"warnings from {warned}" if warned else "clean"

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "findings": [asdict(f) for f in self.findings],
            "reasons": [f"{f.check}: {f.detail}" for f in self.findings if f.fired],
        }


@dataclass(frozen=True)
class Artifact:
    """A `.p3d` read once, in both of the views the checks need.

    `info.strings` is the filtered set -- what a person would call a name --
    and it is what the fingerprint is built from. `names` is the raw
    NUL-terminated superset, and membership questions must use it: the filter
    needs four characters to call a run a name, so a genuine three-character
    animation is absent from the first and present in the second.
    """

    path: str
    info: P3dInfo
    names: tuple[str, ...]


def read_artifact(path: str | os.PathLike[str]) -> Artifact:
    """Read a `.p3d` off the disk, once, into both string views.

    Refuses a missing or an empty file by name, as `p3d.read_p3d` does: zero
    length is the measured signature of a `binarize` crash, and an empty file
    that read as "a model with no strings" would pass every check that asks
    what a model contains.
    """
    p = Path(path)
    try:
        data = p.read_bytes()
    except FileNotFoundError as exc:
        raise P3dError(f"{p} is not there: nothing was built, or it was built elsewhere") from exc
    except OSError as exc:
        raise P3dError(f"{p} cannot be read: {exc}") from exc
    if not data:
        raise P3dError(
            f"{p} is empty: binarize crashes on an already-binarized model and leaves "
            "a zero-length file behind"
        )
    info = parse_p3d(data, path=str(p))
    # Past the magic and its version word, for the same reason the reader
    # skips them: the ODOL version byte is 0x37, so a scan from zero puts the
    # name "ODOL7" into every artifact's string set.
    names = asciiz_strings(data, start=8 if info.kind != UNKNOWN else 0)
    return Artifact(path=str(p), info=info, names=names)


def references(artifact: Artifact) -> tuple[str, ...]:
    """Every file this artifact names, exactly as it lies in the bytes.

    Taken from the UNFILTERED superset on purpose. The reader's path pattern
    exists to keep float noise out of a fingerprint, and it drops shapes that
    are real references -- a leading separator, for one, which is how a
    config.cpp writes the same path. A check that judged only what that
    pattern admitted could not see the very defects it exists to catch.

    Two conditions, and both earn their place: the string ends in an extension
    this pipeline resolves, and it contains a separator. The second keeps out
    the eight-byte fragments of real paths that leak from compressed regions
    (`co.paa` is one, measured) -- read as a reference, such a fragment would
    look like a file in a mod called `co.paa`.
    """
    found = {
        s for s in artifact.names
        if s.lower().endswith(_REFERENCE_SUFFIXES)
        and not s.startswith("#")
        and any(sep in s for sep in _SEPARATORS)
    }
    return tuple(sorted(found))


# --------------------------------------------------------------- path helpers


def _normalise(reference: str) -> str:
    """A reference in one spelling: forward slashes, lowercase, no leading
    separator. DayZ paths are case-insensitive, and `\\SomeMod\\x` names the
    same file inside the same pbo as `SomeMod\\x`."""
    return reference.replace("\\", "/").lstrip("/").lower()


def _first_segment(reference: str) -> str:
    return _normalise(reference).split("/", 1)[0]


def _escapes(reference: str) -> bool:
    """Whether a reference points outside the mod it belongs to: upwards, or
    at a drive on the machine that built it. Both name a file that will not
    exist inside the pbo, and neither produces an error at build time."""
    normalised = _normalise(reference)
    first = normalised.split("/", 1)[0]
    return first in ("..", ".") or re.fullmatch(r"[a-z]:", first) is not None


def _quote(items: Sequence[str], limit: int = _QUOTED_IN_DETAIL) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def _cap(items: Sequence[str]) -> tuple[str, ...]:
    return tuple(items[:MAX_EVIDENCE])


def _worst(parts: Sequence[Finding], check: str, title: str, quiet: str) -> Finding:
    """Fold per-file findings into the single answer for one check.

    Used where a check has to be asked of several files at once -- every rvmat
    an artifact references, say -- because a report with a variable number of
    entries for the same check is a report the caller has to loop over before
    it can be read.
    """
    if not parts:
        return Finding(check, title, SKIP, quiet)
    fired = [p for p in parts if p.fired]
    if not fired:
        status = PASS if any(p.status == PASS for p in parts) else SKIP
        return Finding(check, title, status, "; ".join(p.detail for p in parts if p.detail)[:400])
    status = REFUSE if any(p.status == REFUSE for p in fired) else WARN
    return Finding(
        check, title, status,
        detail="; ".join(p.detail for p in fired),
        action=fired[0].action,
        evidence=_cap([e for p in fired for e in p.evidence]),
    )


# ------------------------------------------------------------ the model.cfg


@dataclass(frozen=True)
class Animation:
    name: str
    source: str
    selection: str


@dataclass(frozen=True)
class ModelEntry:
    name: str
    skeleton: str
    sections: tuple[str, ...]
    animations: tuple[Animation, ...]


@dataclass(frozen=True)
class SkeletonEntry:
    name: str
    inherits: str
    bones: tuple[str, ...]


@dataclass(frozen=True)
class ModelCfg:
    """What a `model.cfg` declares, in the four vocabularies the checks use.

    Deliberately not a config parser: the format is a class tree with
    inheritance, and resolving inheritance properly would mean carrying the
    game's own base configs. What is read here is what one file states about
    itself, and every check built on it says so when it cannot see far enough
    -- see C9 and an inherited skeleton.
    """

    skeletons: dict[str, SkeletonEntry] = field(default_factory=dict)
    models: dict[str, ModelEntry] = field(default_factory=dict)

    def model(self, name: str) -> ModelEntry | None:
        return self.models.get(str(name).lower())

    def skeleton(self, name: str) -> SkeletonEntry | None:
        return self.skeletons.get(str(name).lower())

    def bones(self, name: str) -> tuple[str, ...]:
        entry = self.skeleton(name)
        return entry.bones if entry else ()

    def resolved_bones(self, name: str) -> tuple[set[str], str]:
        """Every bone of a skeleton and its declared ancestors, lowercased,
        plus the name of the first ancestor this file does not declare.

        The second half is what makes the answer honest: a mod skeleton that
        inherits from a vanilla one has bones nobody here can see, and a check
        that judged on the visible half would refuse every such mod.
        """
        bones: set[str] = set()
        seen: set[str] = set()
        current = str(name)
        while current:
            if current.lower() in seen:  # a skeleton that inherits from itself
                return bones, ""
            seen.add(current.lower())
            entry = self.skeleton(current)
            if entry is None:
                return bones, current  # spelled as the child names it
            bones |= {b.lower() for b in entry.bones}
            current = entry.inherits
        return bones, ""


_CLASS_HEADER = re.compile(r"\bclass\s+([A-Za-z_]\w*)\s*(?::\s*[A-Za-z_]\w*\s*)?\{")


def _strip_comments(text: str) -> str:
    """`//` and `/* */`, without touching either inside a quoted string.

    A commented-out animation must not be read as a declaration: half the
    model.cfg files on this machine carry an older version of themselves in a
    comment block.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == '"':
            end = text.find('"', i + 1)
            end = n if end < 0 else end + 1
            out.append(text[i:end])
            i = end
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _matching_brace(text: str, open_at: int) -> int:
    """The index of the `}` that closes the `{` at `open_at`, or -1.

    Braces inside a quoted value are not structure -- a procedural texture is
    written `#(argb,8,8,3)color(...)` today, but nothing stops a value from
    carrying a brace, and a counter that took one would swallow the rest of
    the file into one class.
    """
    depth = 0
    i, n = open_at, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            end = text.find('"', i + 1)
            i = n if end < 0 else end + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _classes(body: str) -> dict[str, str]:
    """`class NAME { ... }` blocks at the top level of `body`, by name.

    Nested classes are skipped rather than flattened: the scan jumps past each
    block it takes, so `class Animations` inside `class thing` belongs to
    `thing` and is never mistaken for a sibling of it.
    """
    found: dict[str, str] = {}
    pos = 0
    while True:
        header = _CLASS_HEADER.search(body, pos)
        if header is None:
            return found
        end = _matching_brace(body, header.end() - 1)
        if end < 0:
            return found
        name, inner = header.group(1), body[header.end():end]
        # A class opened twice is one class: these configs are routinely
        # written as several blocks that reopen `CfgModels`, and keeping only
        # the last one would drop every declaration in the others.
        found[name] = f"{found[name]}\n{inner}" if name in found else inner
        pos = end + 1


def _own(body: str) -> str:
    """`body` with its nested class blocks removed, so an array read from it
    belongs to this class and not to one of its children."""
    out: list[str] = []
    pos = 0
    while True:
        header = _CLASS_HEADER.search(body, pos)
        if header is None:
            out.append(body[pos:])
            return "".join(out)
        end = _matching_brace(body, header.end() - 1)
        if end < 0:
            out.append(body[pos:])
            return "".join(out)
        out.append(body[pos:header.start()])
        pos = end + 1


def _array(body: str, key: str) -> tuple[str, ...]:
    match = re.search(rf"\b{re.escape(key)}\s*\[\s*\]\s*=\s*\{{(.*?)\}}", _own(body), re.S)
    if match is None:
        return ()
    return tuple(re.findall(r'"([^"]*)"', match.group(1)))


def _value(body: str, key: str) -> str:
    match = re.search(rf'\b{re.escape(key)}\s*=\s*"([^"]*)"', _own(body))
    return match.group(1) if match else ""


def parse_model_cfg(text: str) -> ModelCfg:
    """Read the skeletons and models one `model.cfg` declares."""
    stripped = _strip_comments(text)
    top = _classes(stripped)

    skeletons: dict[str, SkeletonEntry] = {}
    for name, body in _classes(top.get("CfgSkeletons", "")).items():
        # skeletonBones[] is a flat list of (bone, parent) PAIRS. Read as a
        # flat set, a parent that nobody declared as a bone would silently
        # become one, and the check that asks "is this selection a bone" would
        # answer yes about a typo.
        flat = _array(body, "skeletonBones")
        skeletons[name.lower()] = SkeletonEntry(
            name=name,
            inherits=_value(body, "skeletonInherit"),
            bones=tuple(b for b in flat[::2] if b),
        )

    models: dict[str, ModelEntry] = {}
    for name, body in _classes(top.get("CfgModels", "")).items():
        animations = []
        for anim_name, anim_body in _classes(_classes(body).get("Animations", "")).items():
            animations.append(Animation(
                name=anim_name,
                source=_value(anim_body, "source"),
                selection=_value(anim_body, "selection"),
            ))
        models[name.lower()] = ModelEntry(
            name=name,
            skeleton=_value(body, "skeletonName"),
            sections=_array(body, "sections"),
            animations=tuple(animations),
        )
    return ModelCfg(skeletons=skeletons, models=models)


def read_model_cfg(path: str | os.PathLike[str]) -> ModelCfg | None:
    """`parse_model_cfg` on a file, or None when there is no readable file."""
    try:
        return parse_model_cfg(Path(path).read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return None


def _declared(cfg: ModelCfg) -> set[str]:
    """A model.cfg's declarations as a flat set of comparable statements.

    Comments and layout are not in it, which is the point: two copies of one
    file drift in comments constantly, and a byte comparison would call every
    such pair a mismatch.
    """
    out: set[str] = set()
    for skeleton in cfg.skeletons.values():
        out.add(f"skeleton {skeleton.name} inherits {skeleton.inherits}")
        out |= {f"skeleton {skeleton.name} bone {b}" for b in skeleton.bones}
    for model in cfg.models.values():
        out.add(f"model {model.name} skeleton {model.skeleton}")
        out |= {f"model {model.name} section {s}" for s in model.sections}
        out |= {
            f"model {model.name} animation {a.name} {a.source} {a.selection}"
            for a in model.animations
        }
    return out


# ------------------------------------------------------------------ the checks


def c1_artifact_is_a_binarized_model(path: str | os.PathLike[str]) -> Finding:
    """C1 -- the artifact exists, is not empty, and starts with `ODOL`.

    Catches an empty output directory (measured: `binarize` returns 0 and
    writes nothing when handed a file where it wanted a directory) and the
    zero-length file it leaves behind when it crashes.
    """
    title = "a built model is there"
    try:
        artifact = read_artifact(path)
    except P3dError as exc:
        return Finding(
            "C1", title, REFUSE, str(exc),
            action="build the model from its MLOD source before packing; if a build did run, "
                   "look at the directory it was told to write into -- binarize returns success "
                   "for an empty one",
        )
    if artifact.info.kind == MLOD:
        return Finding(
            "C1", title, REFUSE,
            f"{path} is an MLOD source, not a built artifact "
            f"(version {artifact.info.version}, {artifact.info.lod_count} LODs)",
            action="run binarize on it and pack the result; the engine does not load MLOD, and "
                   "it says nothing about it either -- the item is simply invisible",
        )
    if artifact.info.kind != ODOL:
        return Finding(
            "C1", title, REFUSE,
            f"{path} is neither MLOD nor ODOL: {artifact.info.size} bytes beginning "
            f"{Path(path).name!r} with an unrecognised magic",
            action="check what wrote this file; a p3d always begins with MLOD or ODOL",
        )
    return Finding(
        "C1", title, PASS,
        f"ODOL v{artifact.info.version}, {artifact.info.size} bytes, "
        f"{artifact.info.lod_count} LODs",
    )


def c2_artifact_is_newer_than_its_inputs(
    path: str | os.PathLike[str],
    inputs: Iterable[str | os.PathLike[str]],
) -> Finding:
    """C2 -- nothing it was built from has been touched since.

    A build that was silently skipped leaves last week's artifact in place,
    and every other check then passes on it happily. A clock comparison is a
    heuristic -- a copy resets a timestamp, and a build machine's clock can
    disagree with a modeller's -- so this warns.
    """
    title = "the artifact postdates its inputs"
    artifact = Path(path)
    inputs = [Path(i) for i in inputs]
    if not inputs:
        return Finding("C2", title, SKIP, "no inputs were declared to compare against")
    if not artifact.is_file():
        return Finding("C2", title, SKIP, f"{artifact} is not there; C1 says so first")

    built = artifact.stat().st_mtime
    missing = [str(i) for i in inputs if not i.exists()]
    newer = [str(i) for i in inputs if i.exists() and i.stat().st_mtime > built]
    if missing or newer:
        parts = []
        if newer:
            parts.append(f"{len(newer)} input(s) changed after the build: {_quote(newer)}")
        if missing:
            parts.append(f"{len(missing)} declared input(s) are not there: {_quote(missing)}")
        return Finding(
            "C2", title, WARN, "; ".join(parts),
            action="rebuild the model, or correct the input list -- a declared input that is "
                   "absent makes this comparison pass by accident",
            evidence=_cap(newer + missing),
        )
    return Finding("C2", title, PASS, f"newer than all {len(inputs)} declared input(s)")


def c3_references_stay_inside_the_mod(
    artifact: Artifact,
    prefix: str,
    also_allow: Sequence[str] = VANILLA_PREFIXES,
) -> Finding:
    """C3 -- no reference escapes the mod, and every one starts with its prefix.

    The most valuable of the twelve, because it names the cause. `binarize`
    has no project-root switch at all: the root is the process's working
    directory, so the same command run from somewhere else bakes that
    somewhere else into the artifact and exits 0. What comes out is a valid
    ODOL with plausible paths that the engine renders untextured.

    Refuses, because there is no reading under which `..\\..\\x_co.paa` or
    `e:\\work\\x_co.paa` is a file that will exist inside a pbo.
    """
    title = "references stay inside the mod"
    allowed = {p.lower() for p in [*also_allow, prefix] if p}
    refs = references(artifact)
    if not refs:
        return Finding("C3", title, PASS, "the artifact names no files")

    escaping = [r for r in refs if _escapes(r)]
    foreign = [
        r for r in refs
        if r not in escaping and prefix and _first_segment(r) not in allowed
    ]
    if escaping or foreign:
        parts = []
        if escaping:
            parts.append(
                f"{len(escaping)} reference(s) point outside any mod: {_quote(escaping)}")
        if foreign:
            segments = sorted({_first_segment(r) for r in foreign})
            parts.append(
                f"{len(foreign)} reference(s) begin with {_quote(segments)} instead of "
                f"{prefix!r}: {_quote(foreign)}"
            )
        return Finding(
            "C3", title, REFUSE, "; ".join(parts),
            action=(
                f"binarize has no project-root switch -- the root is whatever directory it was "
                f"started in, and it reports nothing when that is wrong. Declare the root once "
                f"as {PROJECT_ROOT_KEY} in the project profile so the server sets it, make sure "
                f"that root contains a folder named {prefix!r}, and rebuild from the MLOD source. "
                f"Editing the paths in the artifact fixes nothing: they are an effect."
            ),
            evidence=_cap(escaping + foreign),
        )
    if not prefix:
        return Finding(
            "C3", title, PASS,
            f"{len(refs)} reference(s), none escaping; no mod prefix was declared, so the "
            "first segment of each was not checked",
        )
    return Finding("C3", title, PASS, f"all {len(refs)} reference(s) begin with {prefix!r} or a "
                                      f"prefix that ships with the game")


def c4_materials_were_inlined(artifact: Artifact) -> Finding:
    """C4 -- the artifact carries the strings a resolved rvmat leaves behind.

    When `binarize` resolves a material it copies that material's own stage
    textures into the model: `fresnel`, `#(argb,8,8,3)`, `env_land_co.paa`,
    `_nohq.paa`, `_smdi.paa` -- strings that are in no MLOD. Measured on six
    artifacts with no exceptions: a build from the correct directory carries
    all five and a build from the wrong one carries none, while both are valid
    ODOL files with plausible texture paths. This single test found a broken
    artifact nobody knew about, and it is the only cheap thing that separates
    the two.

    Refuses on zero markers. A PARTIAL set warns instead: it was never
    measured, and a material with fewer stages would produce one legitimately.
    """
    title = "a resolved material was inlined"
    if artifact.info.kind != ODOL:
        return Finding(
            "C4", title, SKIP,
            f"inlining is something binarize does; a {artifact.info.kind} carries none of the "
            "markers by design",
        )
    if not artifact.info.materials:
        return Finding("C4", title, SKIP, "the artifact references no rvmat, so there is "
                                          "nothing for binarize to inline")
    markers = inlined_material_markers(artifact.info)
    if not markers:
        return Finding(
            "C4", title, REFUSE,
            f"the artifact references {len(artifact.info.materials)} material(s) "
            f"({_quote(list(artifact.info.materials))}) and carries none of the strings a "
            "resolved rvmat leaves behind -- binarize could not open them and exited 0 anyway",
            action=(
                f"rebuild with the working directory set to the project root (see "
                f"{PROJECT_ROOT_KEY}), and check the rvmat's own texture stages: a stage that "
                f"names another mod's prefix cannot resolve under this root either. The engine "
                f"renders this artifact untextured and reports nothing."
            ),
            evidence=_cap(list(artifact.info.materials)),
        )
    if len(markers) < len(RVMAT_INLINE_MARKERS):
        return Finding(
            "C4", title, WARN,
            f"only {len(markers)} of {len(RVMAT_INLINE_MARKERS)} inlined-material markers are present "
            f"({_quote(list(markers))}); every artifact measured on a working build carried all "
            "of them",
            action="compare this model's rvmat with one from a working build -- a missing stage "
                   "is the usual reason, and it is not always a defect",
            evidence=_cap(list(markers)),
        )
    return Finding("C4", title, PASS, f"all {len(markers)} inlined-material markers are present")


def _dropped_by_the_packer(relative: PurePosixPath, patterns: Sequence[str]) -> str:
    """The first path component the packer's exclude list removes, or "".

    Matched by NAME at any depth, and a matched directory takes everything
    under it -- `packer.name_matches` itself, not a second spelling of it, so
    this prediction and the packing it predicts cannot drift apart.
    """
    for part in relative.parts:
        if name_matches(part, patterns):
            return part
    return ""


def c5_references_land_inside_the_pbo(
    artifact: Artifact,
    roots: Mapping[str, str | os.PathLike[str]],
    also_allow: Sequence[str] = VANILLA_PREFIXES,
    exclude: Sequence[str] = (),
) -> Finding:
    """C5 -- every `.paa` and `.rvmat` it names is a file that will be packed.

    A dangling reference costs nothing at build time and renders the surface
    untextured in game. Warns rather than refuses: a texture can be delivered
    by a dependency, or built by a later step in the same run, and refusing
    would make those workflows impossible.

    "On the disk" and "inside the pbo" are two different questions, and only
    the second one is this check's. The packer drops everything matching the
    project's `build.exclude` before FileBank ever sees the tree, so a file
    sitting in an excluded folder is exactly as absent in game as one that was
    never built -- and it looks perfectly fine to a check that only calls
    `exists()`. Pass the project's list to be told the difference.
    """
    title = "references land inside the pbo"
    vanilla = {p.lower() for p in also_allow}
    roots = {str(k).lower(): Path(v) for k, v in roots.items()}
    if not roots:
        return Finding("C5", title, SKIP, "no source root was declared for any prefix")

    missing: list[str] = []
    dropped: list[str] = []
    reasons: set[str] = set()
    checked = unknown = shipped_with_the_game = 0
    for ref in references(artifact):
        first = _first_segment(ref)
        if first in vanilla or _escapes(ref):
            # The game's own files, or C3's business. Counted, not ignored:
            # "every reference is vanilla" is an answer, and "no root was
            # declared for any of them" is not the same answer.
            shipped_with_the_game += first in vanilla
            continue
        root = roots.get(first)
        if root is None:
            unknown += 1
            continue
        rest = _normalise(ref).split("/", 1)[1] if "/" in _normalise(ref) else ""
        checked += 1
        if not (root / PurePosixPath(rest)).exists():
            missing.append(ref)
            continue
        cut = _dropped_by_the_packer(PurePosixPath(rest), exclude) if exclude else ""
        if cut:
            dropped.append(ref)
            reasons.add(cut)

    if missing or dropped:
        parts: list[str] = []
        actions: list[str] = []
        if missing:
            parts.append(f"{len(missing)} of {checked} reference(s) have no file behind them: "
                         f"{_quote(missing)}")
            actions.append("copy or build those files into the mod before packing; the engine "
                           "renders the surfaces that use them untextured and logs nothing")
        if dropped:
            parts.append(
                f"{len(dropped)} reference(s) resolve to a file build.exclude keeps out of the "
                f"pbo ({_quote(sorted(reasons))}): {_quote(dropped)}"
            )
            actions.append("move those files where the packer will ship them, or drop the "
                           "pattern from build.exclude -- a file that is on the disk but not in "
                           "the pbo is exactly as missing in game as one that was never built")
        return Finding(
            "C5", title, WARN, "; ".join(parts), action=" ".join(actions),
            evidence=_cap(missing + dropped),
        )
    if not checked:
        if unknown:
            return Finding(
                "C5", title, SKIP,
                f"no reference belongs to a declared root ({unknown} carry a prefix nobody "
                "declared a root for; C3 answers that one)",
            )
        return Finding(
            "C5", title, PASS,
            f"nothing to resolve: all {shipped_with_the_game} reference(s) ship with the game",
        )
    return Finding("C5", title, PASS, f"all {checked} reference(s) resolve to a file on the disk")


def c6_rvmat_stages_stay_inside_the_mod(
    path: str | os.PathLike[str],
    prefix: str,
    also_allow: Sequence[str] = VANILLA_PREFIXES,
) -> Finding:
    """C6 -- every `texture=` in an rvmat is a `.paa` inside this mod.

    This is C4's cause, one file upstream: an rvmat that sits in one mod and
    names another mod's prefix cannot resolve under this mod's root, so
    `binarize` inlines nothing and exits 0. A real rvmat on this machine does
    exactly that.

    Procedural stages are not paths at all -- four of the seven stages in
    every correct rvmat here are `#(argb,8,8,3)color(...)` -- and a check that
    demanded a `.paa` of them would fire on every good material there is.
    """
    title = "rvmat stages stay inside the mod"
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return Finding(
            "C6", title, WARN, f"{p} cannot be read: {exc}",
            action="the artifact names this material; put the file where the model expects it, "
                   "or fix the reference",
        )

    allowed = {x.lower() for x in [*also_allow, prefix] if x}
    stages = [s for s in re.findall(r'texture\s*=\s*"([^"]*)"', text) if s]
    paths = [s for s in stages if not s.startswith("#")]

    not_paa = [s for s in paths if not s.lower().endswith(".paa")]
    foreign = [
        s for s in paths
        if s not in not_paa and prefix and (_escapes(s) or _first_segment(s) not in allowed)
    ]
    if not_paa or foreign:
        parts = []
        if not_paa:
            parts.append(f"{len(not_paa)} stage(s) are not .paa: {_quote(not_paa)}")
        if foreign:
            parts.append(
                f"{len(foreign)} stage(s) name something other than {prefix!r}: {_quote(foreign)}")
        return Finding(
            "C6", title, WARN, f"{p.name}: " + "; ".join(parts),
            action=(
                "convert the source to .paa and point the stage at it, and keep every stage "
                f"inside {prefix!r} -- binarize resolves an rvmat under ONE root, so a stage "
                "naming another mod resolves to nothing and is reported nowhere (this is what "
                "C4 sees later, as an artifact with no inlined material)"
            ),
            evidence=_cap(not_paa + foreign),
        )
    return Finding(
        "C6", title, PASS,
        f"{p.name}: {len(paths)} path stage(s) and {len(stages) - len(paths)} procedural one(s), "
        "all inside the mod",
    )


def c7_transparency_survived_conversion(
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
) -> Finding:
    """C7 -- a graded alpha did not fall into DXT1's single bit.

    The loss is not a missing channel, it is quantisation: DXT1 carries one
    bit of alpha, and the same source measured 6 levels going in and 2 coming
    out of a `_co` conversion. Which format `ImageToPAA` picks is decided by
    the SOURCE FILE'S NAME, so this is entirely avoidable and entirely silent.

    Two things this deliberately does NOT do. It does not compare the output's
    format against the one the suffix table predicts -- `_smdi` sources come
    out DXT1 here while the table says DXT5, and twelve correct textures on
    this machine would carry a warning. And it does not judge from the output
    alone: a legitimately opaque texture and one whose transparency was
    destroyed are identical in DXT1, and only the source separates them.
    """
    title = "transparency survived the conversion"
    src, dst = Path(source), Path(output)
    try:
        levels = alpha_levels(src)
    except (PaaError, OSError) as exc:
        return Finding("C7", title, SKIP, f"{src} cannot be measured: {exc}")
    try:
        head = dst.open("rb").read(2)
    except OSError as exc:
        return Finding("C7", title, SKIP, f"{dst} cannot be read: {exc}")

    fmt = paa_format(head)
    if levels > 2 and fmt == DXT1:
        return Finding(
            "C7", title, WARN,
            f"{src.name} carries {levels} distinct alpha levels and {dst.name} is {DXT1}, "
            "which keeps one bit of them",
            action=f"the format follows the SOURCE file's name: rename it to end in `_ca` (or "
                   f"drop a `_co` suffix) and convert again -- {dst.name} itself cannot be "
                   f"repaired, the levels are already gone",
            evidence=(f"{src.name}: {levels} alpha levels", f"{dst.name}: {fmt}"),
        )
    return Finding(
        "C7", title, PASS,
        f"{src.name} carries {levels} alpha level(s) and {dst.name} is {fmt}",
    )


def c8_animations_reached_the_artifact(artifact: Artifact, cfg: ModelCfg) -> Finding:
    """C8 -- every animation the model.cfg declares is a string in the model.

    The measured mechanism, written down by the modeller who hit it: with an
    empty `skeletonBones[]`, `binarize` drops every animation from the ODOL
    and exits 0. The item then has no moving parts in game, and nothing
    anywhere says why.

    Asks the UNFILTERED superset. The reader's filtered set needs four
    characters to call a run a name, so a genuine `up` is absent from it --
    asking that set would raise a false alarm on a correct build.
    """
    title = "the animations reached the artifact"
    model = Path(artifact.path).stem
    entry = cfg.model(model)
    if entry is None:
        return Finding(
            "C8", title, WARN,
            f"the model.cfg declares no class for {model!r}",
            action=f"add `class {model}` to CfgModels; without one the engine falls back to "
                   "Default -- no skeleton, no sections and no animations, silently",
        )
    if not entry.animations:
        return Finding("C8", title, PASS, f"{model!r} declares no animations")

    present = set(artifact.names)
    missing = [
        f"{a.name} (source {a.source})" if a.source and a.source != a.name else a.name
        for a in entry.animations
        if a.name not in present and (not a.source or a.source not in present)
    ]
    if missing:
        return Finding(
            "C8", title, WARN,
            f"{len(missing)} of {len(entry.animations)} animation(s) declared for {model!r} are "
            f"not in the artifact: {_quote(missing)}",
            action="check that every animated selection is listed in the skeleton's "
                   "skeletonBones[] and rebuild -- with an empty skeletonBones[] binarize drops "
                   "the animations and reports success",
            evidence=_cap(missing),
        )
    return Finding("C8", title, PASS,
                   f"all {len(entry.animations)} declared animation(s) are in the artifact")


def c9_selections_are_declared(
    cfg: ModelCfg,
    model: str,
    hidden_selections: Sequence[str] = (),
) -> Finding:
    """C9 -- the same defect as C8, caught upstream in the source file.

    Two halves. Every selection an animation moves has to be a bone in the
    skeleton, or `binarize` silently drops the animation. Every hidden
    selection a config declares has to be in `sections[]`, or the texture
    swap does nothing.

    Where a skeleton inherits from one this file does not declare, the bone
    half reports that it cannot see far enough rather than judging on the
    visible half -- mods build on vanilla skeletons constantly, and answering
    from half a bone list would refuse all of them.
    """
    title = "animated and hidden selections are declared"
    entry = cfg.model(model)
    if entry is None:
        return Finding("C9", title, SKIP,
                       f"the model.cfg declares no class for {model!r}; C8 reports that")

    animated = sorted({a.selection for a in entry.animations if a.selection})
    parts: list[str] = []
    actions: list[str] = []
    evidence: list[str] = []
    statuses: list[str] = []

    if not animated:
        statuses.append(PASS)
    elif not entry.skeleton:
        statuses.append(WARN)
        parts.append(f"{model!r} animates {len(animated)} selection(s) and names no skeleton")
        actions.append("give the model a skeletonName and list every animated selection in that "
                       "skeleton's skeletonBones[]")
        evidence.extend(animated)
    else:
        bones, unresolved = cfg.resolved_bones(entry.skeleton)
        if cfg.skeleton(entry.skeleton) is None:
            statuses.append(WARN)
            parts.append(f"{model!r} names the skeleton {entry.skeleton!r}, which this "
                         "model.cfg does not declare")
            actions.append(f"declare `class {entry.skeleton}` in CfgSkeletons, or point "
                           "skeletonName at one that is declared")
        elif unresolved:
            statuses.append(SKIP)
            parts.append(f"{entry.skeleton!r} inherits from {unresolved!r}, which is not "
                         "declared here, so its full bone list cannot be seen")
        else:
            not_bones = [s for s in animated if s.lower() not in bones]
            if not_bones:
                statuses.append(WARN)
                parts.append(
                    f"{len(not_bones)} animated selection(s) are not bones of "
                    f"{entry.skeleton!r}: {_quote(not_bones)}"
                )
                actions.append(
                    "add them to skeletonBones[] as (bone, parent) pairs -- with an empty or "
                    "incomplete skeletonBones[] binarize drops the animations from the ODOL and "
                    "exits 0"
                )
                evidence.extend(not_bones)
            else:
                statuses.append(PASS)

    hidden = [h for h in hidden_selections if h]
    if hidden:
        sections = {s.lower() for s in entry.sections}
        undeclared = [h for h in hidden if h.lower() not in sections]
        if undeclared:
            statuses.append(WARN)
            parts.append(f"{len(undeclared)} hidden selection(s) are not in the sections[] of "
                         f"{model!r}: {_quote(undeclared)}")
            actions.append("add them to sections[]; a hiddenSelection that is not a section "
                           "cannot be retextured, and nothing reports it")
            evidence.extend(undeclared)
        else:
            statuses.append(PASS)

    if WARN in statuses:
        status = WARN
    elif PASS in statuses:
        status = PASS
    else:
        status = SKIP
    if status == PASS and not parts:
        parts.append(f"every selection {model!r} animates is a bone, and every hidden selection "
                     "is a section")
    return Finding("C9", title, status, "; ".join(parts), " ".join(actions), _cap(evidence))


def c10_binarize_input_is_a_source(path: str | os.PathLike[str]) -> Finding:
    """C10 -- never hand an already-binarized model back to `binarize`.

    Measured: it dies with `0xC0000005` and leaves a ZERO-LENGTH file in the
    output directory, which then replaces a working artifact. This refusal
    happens before the process starts, which is the only place it helps.
    """
    title = "the binarize input is a source model"
    try:
        artifact = read_artifact(path)
    except P3dError as exc:
        return Finding(
            "C10", title, REFUSE, str(exc),
            action="point the build at the MLOD the modeller exported",
        )
    if artifact.info.kind == ODOL:
        return Finding(
            "C10", title, REFUSE,
            f"{path} is already an ODOL (v{artifact.info.version})",
            action="build from the MLOD export instead. binarize crashes on a binarized model "
                   "with 0xC0000005 and leaves a zero-length file in the output directory, so "
                   "the run destroys the artifact it was pointed at",
        )
    if artifact.info.kind != MLOD:
        return Finding(
            "C10", title, REFUSE,
            f"{path} is not a p3d at all: {artifact.info.size} bytes with an unrecognised magic",
            action="point the build at the MLOD the modeller exported",
        )
    return Finding("C10", title, PASS,
                   f"MLOD v{artifact.info.version}, {artifact.info.lod_count} LODs")


def c11_model_cfg_is_the_one_it_was_built_from(
    shipped: str | os.PathLike[str],
    built: str | os.PathLike[str] | None = None,
    *,
    artifact: str | os.PathLike[str] | None = None,
) -> Finding:
    """C11 -- the model.cfg beside the mod is the one `binarize` actually read.

    `binarize` reads the model.cfg under ITS root, not the one next to the
    model in the repository, and bakes the result into the ODOL. When those
    two copies are different files, the one a person edits is not the one the
    artifact came from -- which is the state this machine is in right now.

    Compared on declarations, never on bytes: the two copies drift in comments
    constantly and a byte comparison would fire on every pair. With only one
    copy to look at, the check falls back to the clock, and says so -- a
    fallback that is strictly weaker, and demonstrably so: the live mismatch
    on this machine has an OLDER shipped copy, which a clock comparison calls
    fine.
    """
    title = "the model.cfg is the one it was built from"
    shipped_path = Path(shipped)
    shipped_cfg = read_model_cfg(shipped_path)
    if shipped_cfg is None:
        return Finding(
            "C11", title, WARN, f"{shipped_path} cannot be read",
            action="put the model.cfg the model was built with beside the model; without one "
                   "the engine gives the item no skeleton and no sections",
        )

    if built is not None:
        built_path = Path(built)
        built_cfg = read_model_cfg(built_path)
        if built_cfg is None:
            return Finding(
                "C11", title, WARN, f"{built_path} cannot be read",
                action="point the check at the model.cfg under the build root, the one binarize "
                       "reads",
            )
        only_shipped = sorted(_declared(shipped_cfg) - _declared(built_cfg))
        only_built = sorted(_declared(built_cfg) - _declared(shipped_cfg))
        if only_shipped or only_built:
            parts = []
            if only_built:
                parts.append(f"{len(only_built)} declaration(s) only in the built copy: "
                             f"{_quote(only_built)}")
            if only_shipped:
                parts.append(f"{len(only_shipped)} only in the shipped copy: "
                             f"{_quote(only_shipped)}")
            return Finding(
                "C11", title, WARN,
                f"{shipped_path.name} and the copy under the build root disagree; "
                + "; ".join(parts),
                action="copy the model.cfg from the build root into the mod (or make the build "
                       "root read the mod's own), so the file a person edits is the file the "
                       "artifact is built from",
                evidence=_cap(only_built + only_shipped),
            )
        return Finding("C11", title, PASS, "both copies declare the same thing")

    if artifact is None:
        return Finding("C11", title, SKIP,
                       "only one model.cfg was given and no artifact to date it against")
    art = Path(artifact)
    if not art.is_file():
        return Finding("C11", title, SKIP, f"{art} is not there; C1 says so first")
    if shipped_path.stat().st_mtime > art.stat().st_mtime:
        return Finding(
            "C11", title, WARN,
            f"{shipped_path.name} was changed after {art.name} was built",
            action="rebuild the model so the artifact carries what the model.cfg now says; "
                   "binarize bakes the model.cfg in, so editing it alone changes nothing",
        )
    return Finding("C11", title, PASS,
                   f"{shipped_path.name} predates {art.name}; no second copy was given to "
                   "compare it against")


def c12_fingerprint_matches_the_recorded_one(
    artifact: Artifact,
    recorded: Fingerprint | None,
) -> Finding:
    """C12 -- the artifact is structurally the one that was recorded.

    A content hash cannot answer this: three exports of one unmodified source
    gave three different SHA-256 hashes at a constant size, the difference
    being an ordering permutation inside a TAGG. So "did the model change" is
    answered by structure -- the kind, the size, the LOD count and the set of
    names -- and a re-export of unchanged work does not look like a change.
    """
    title = "the artifact matches its recorded fingerprint"
    current = fingerprint(artifact.info)
    if recorded is None:
        return Finding("C12", title, SKIP,
                       f"nothing was recorded for this artifact yet (its fingerprint is now "
                       f"{current.digest[:12]})")
    if current == recorded:
        return Finding("C12", title, PASS, f"unchanged since {current.digest[:12]}")

    moved: list[str] = []
    if current.kind != recorded.kind:
        moved.append(f"kind {recorded.kind} -> {current.kind}")
    if current.size != recorded.size:
        moved.append(f"size {recorded.size} -> {current.size}")
    if current.lod_count != recorded.lod_count:
        moved.append(f"LODs {recorded.lod_count} -> {current.lod_count}")
    gained = sorted(set(current.strings) - set(recorded.strings))
    lost = sorted(set(recorded.strings) - set(current.strings))
    if gained:
        moved.append(f"{len(gained)} name(s) appeared: {_quote(gained)}")
    if lost:
        moved.append(f"{len(lost)} name(s) went: {_quote(lost)}")
    return Finding(
        "C12", title, WARN,
        f"the artifact is not the one recorded ({'; '.join(moved)})",
        action="rebuild if this artifact is stale, or record the new fingerprint if the change "
               "is intended -- a re-export of unchanged work does NOT move this, so a difference "
               "here is a real difference",
        evidence=_cap(moved),
    )


# ------------------------------------------------------------- the orchestrators


def check_model(
    path: str | os.PathLike[str],
    *,
    prefix: str = "",
    roots: Mapping[str, str | os.PathLike[str]] | None = None,
    inputs: Sequence[str | os.PathLike[str]] = (),
    model_cfg: str | os.PathLike[str] | None = None,
    built_model_cfg: str | os.PathLike[str] | None = None,
    hidden_selections: Sequence[str] = (),
    recorded: Fingerprint | None = None,
    also_allow: Sequence[str] = VANILLA_PREFIXES,
    exclude: Sequence[str] = (),
) -> Report:
    """Every model check, in one report, with exactly one finding per check.

    C1 comes first and stops the rest: once the file is missing, empty or not
    a built model, everything else would be asking questions about nothing,
    and an answer of "pass" to a question nobody could ask is how a broken
    artifact gets shipped.
    """
    order = ("C2", "C3", "C4", "C5", "C6", "C8", "C9", "C11", "C12")
    c1 = c1_artifact_is_a_binarized_model(path)
    if c1.status == REFUSE:
        return Report((c1, *(
            Finding(c, _TITLES[c], SKIP, "not asked: C1 refused this artifact") for c in order
        )))

    artifact = read_artifact(path)
    roots = dict(roots or {})
    cfg = read_model_cfg(model_cfg) if model_cfg is not None else None
    model = Path(artifact.path).stem

    findings = {
        "C2": c2_artifact_is_newer_than_its_inputs(path, inputs),
        "C3": c3_references_stay_inside_the_mod(artifact, prefix, also_allow),
        "C4": c4_materials_were_inlined(artifact),
        "C5": c5_references_land_inside_the_pbo(artifact, roots, also_allow, exclude),
        "C6": _check_materials(artifact, roots, prefix, also_allow),
        "C12": c12_fingerprint_matches_the_recorded_one(artifact, recorded),
    }
    if cfg is None:
        reason = ("no model.cfg was given" if model_cfg is None
                  else f"{model_cfg} cannot be read")
        findings["C8"] = Finding("C8", _TITLES["C8"], SKIP, reason)
        findings["C9"] = Finding("C9", _TITLES["C9"], SKIP, reason)
    else:
        findings["C8"] = c8_animations_reached_the_artifact(artifact, cfg)
        findings["C9"] = c9_selections_are_declared(cfg, model, hidden_selections)
    findings["C11"] = (
        c11_model_cfg_is_the_one_it_was_built_from(model_cfg, built_model_cfg, artifact=path)
        if model_cfg is not None
        else Finding("C11", _TITLES["C11"], SKIP, "no model.cfg was given")
    )
    return Report((c1, *(findings[c] for c in order)))


def _check_materials(
    artifact: Artifact,
    roots: Mapping[str, Path],
    prefix: str,
    also_allow: Sequence[str],
) -> Finding:
    """C6 over every rvmat the artifact references, folded into one finding."""
    lowered = {str(k).lower(): Path(v) for k, v in roots.items()}
    vanilla = {p.lower() for p in also_allow}
    parts = []
    for ref in references(artifact):
        if not ref.lower().endswith(".rvmat"):
            continue
        first = _first_segment(ref)
        if first in vanilla or _escapes(ref):
            continue
        root = lowered.get(first)
        if root is None:
            continue
        rest = _normalise(ref).split("/", 1)[1] if "/" in _normalise(ref) else ""
        target = root / PurePosixPath(rest)
        if not target.is_file():
            # C5 already reports a reference with no file behind it, in the
            # words that fit it. Saying it twice, in two vocabularies, teaches
            # the reader that half the findings are echoes.
            parts.append(Finding("C6", _TITLES["C6"], SKIP, f"{ref} is not on the disk; C5 "
                                                            "reports that"))
            continue
        parts.append(c6_rvmat_stages_stay_inside_the_mod(target, prefix, also_allow))
    return _worst(parts, "C6", _TITLES["C6"],
                  "no rvmat of this mod could be located to read")


def check_texture(
    source: str | os.PathLike[str],
    output: str | os.PathLike[str] | None = None,
) -> Report:
    """C7 on one converted texture. The output defaults to the `.paa` beside
    the source under the same stem, which is how the pipeline pairs them."""
    src = Path(source)
    dst = Path(output) if output is not None else src.with_suffix(".paa")
    return Report((c7_transparency_survived_conversion(src, dst),))


_TITLES = {
    "C1": "a built model is there",
    "C2": "the artifact postdates its inputs",
    "C3": "references stay inside the mod",
    "C4": "a resolved material was inlined",
    "C5": "references land inside the pbo",
    "C6": "rvmat stages stay inside the mod",
    "C7": "transparency survived the conversion",
    "C8": "the animations reached the artifact",
    "C9": "animated and hidden selections are declared",
    "C10": "the binarize input is a source model",
    "C11": "the model.cfg is the one it was built from",
    "C12": "the artifact matches its recorded fingerprint",
}
