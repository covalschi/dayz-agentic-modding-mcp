"""Two tools for the active mod set: declare one, or ask a server for one.

The index holds every mod found on this machine. Work happens against ONE
server, running its own subset -- so an answer about a class from a mod that
server does not run is a harmful answer, and nothing in it says so. These two
tools are how the agent tells the index which subset is in play.

`knowledge_scope`  declare, inspect or clear the set. The state lives beside
                   the index, so it outlives a restart: the server it describes
                   does not change when this process does.
`server_mods`      ask an address what it runs, and PROPOSE a set from it.

**The proposal never applies itself.** A server query returns three buckets --
matched, on the server but not installed, installed but not on the server --
and hands back the exact call that would apply the first of them. Rescoping the
index from a query would turn a mismatch into an invisible action instead of a
fact the caller read; the same reason the filtered-out half of a search is
named rather than dropped.

**Matching is by Workshop id and there is no fallback to name.** The measured
reasons live in `knowledge/scope.py`; the short version is that one mod carries
up to four different names and 19 names among 14 740 ids belong to more than
one mod, so a name match can silently equate two different builds.
"""
from __future__ import annotations

from .. import a2s
from ..errors import Result, fail
from ..knowledge import scope as modscope
from ..knowledge.store import DEPS
from . import session
# The one index opener, shared rather than re-spelled: a failure to open the
# index must read identically whichever tool ran into it.
from .knowledge import _age_text, _index
from .project import require_project

#: What a caller sees when it declares a set without saying where it came from.
_DECLARED = "declared by the model"

#: The offsets that cover most public servers, named in the refusal that asks
#: for a query port. Measured: 252 distinct offsets in a live sample, so this
#: is a list of candidates to try, never a derivation.
_COMMON_OFFSETS = "game+1, game+3, or 27016 (about three quarters of a live sample)"


def _address(address: str, query_port: int) -> tuple[str, int]:
    """`host` and query port out of "host", "host:port" and an explicit port.

    IPv4 and hostnames. An IPv6 literal would need brackets to be unambiguous
    here, and no DayZ server has been seen published as one.

    The port test is `isascii() and isdigit()`, not `isdigit()` alone. Python
    calls a superscript a digit and `int()` then refuses it, so `host:` followed
    by one raised a ValueError out of a tool that answers in `Result` envelopes;
    and it calls Arabic-Indic numerals digits too, which `int()` accepts, so a
    port nobody typed would have been read as one. Anything that is not a plain
    decimal now stays part of the host, and the caller is told a query port is
    missing -- a named refusal instead of a crash or a guess.
    """
    text = str(address or "").strip()
    host, embedded = text, 0
    head, separator, tail = text.rpartition(":")
    if separator and head and tail.isascii() and tail.isdigit():
        host, embedded = head, int(tail)
    return host.strip(), int(query_port or 0) or embedded


# ------------------------------------------------------------- declaring it


def _folder_view(folders, active: modscope.ActiveSet) -> tuple[list[dict], int, int]:
    """Every mod folder the dependency layer holds, and how the set splits it.

    Takes the folder list rather than the store: listing it is a grouped scan of
    the whole layer, and the caller already needed it to resolve the names it
    was given. Two scans of 10 925 declarations to answer one question is the
    kind of waste that only shows up on a real index.
    """
    view: list[dict] = []
    inside = outside = 0
    for folder, held in folders:
        in_scope = active.contains(folder) if active.active else True
        view.append({"folder": folder, "declarations": held, "in_scope": in_scope})
        if in_scope:
            inside += held
        else:
            outside += held
    return view, inside, outside


def knowledge_scope(
    mods: list[str] | None = None,
    clear: bool = False,
    source: str = "",
    note: str = "",
) -> Result:
    """Declare, inspect or clear the active mod set.

    With no arguments it reports the set in force and every mod folder the
    dependency layer holds, so a caller can see what there is to choose from.

    `mods=[...]` narrows every knowledge answer to those mod folders. The game
    and the project's own code always answer -- the game is the substrate a
    DayZ mod is written against, and the project layer is the code being
    written, so narrowing either would report "no such class" about code you
    are looking at.

    **Nothing is hidden by the narrowing.** A search whose answer lies in a mod
    outside the set does not come back empty: it comes back naming the mod that
    holds it, and it comes back as a refusal, so it cannot be read as "no such
    thing". That is the whole point of the feature -- an invisible narrowing is
    the same silent lie as an answer from a stale layer.

    `source` is free text saying where the set came from ("the server at
    <address>", "the project profile"). It is carried into every narrowed
    answer, because "why is this answer smaller than the index" is the question
    a narrowing has to be able to answer about itself.

    `clear=True` returns the index to answering from every mod it holds. An
    empty `mods=[]` is refused rather than read as "narrow to nothing": those
    are different requests and only one of them is ever meant.

    The set is stored beside the index and survives a restart. It is NOT
    changed by `server_mods`, which only proposes one.
    """
    guard = require_project()
    if guard:
        return guard
    store, failure = _index()
    if failure:
        return failure
    directory = store.path.parent

    deps_built = store.layer(DEPS) is not None
    folders = store.mod_folders(DEPS) if deps_built else []
    known = {folder.lower(): folder for folder, _ in folders}

    if clear and mods is not None:
        return fail(
            "clear= and mods= ask for opposite things",
            hint="pass mods=[...] to narrow, or clear=True to stop narrowing -- not both",
        )

    changed = ""
    if clear:
        modscope.clear(directory)
        active = modscope.ActiveSet()
        changed = "cleared"
    elif mods is not None:
        wanted = [str(m).strip() for m in mods if str(m).strip()]
        if not wanted:
            return fail(
                "an empty mod set would mean no dependency mod may answer at all",
                hint="that is almost certainly not what was meant: pass clear=True to stop "
                     "narrowing, or name the mod folders with mods=['<mod folder>', ...]",
            )
        # Resolved to the index's own spelling: the set is compared against
        # labels the build wrote, and reading back a name that appears nowhere
        # in the index is how a caller ends up doubting a correct set.
        resolved = [known.get(name.lower(), name) for name in wanted]
        unrecognised = [name for name in resolved if name.lower() not in known]
        if deps_built and len(unrecognised) == len(resolved):
            return fail(
                "none of " + ", ".join(repr(name) for name in resolved)
                + " is a mod folder the dependency layer holds, so this set would blank "
                  "every dependency answer while looking like a successful call",
                hint="knowledge_scope() with no arguments lists the folders that are "
                     "indexed; a mod that is installed but not declared as a dependency of "
                     "this project has nothing in the index to narrow",
            )
        active = modscope.save(
            directory, resolved, source=source or _DECLARED, note=note
        )
        changed = "set"
    else:
        active = modscope.load(directory)

    view, inside, outside = _folder_view(folders, active)
    unknown = [name for name in active.mods if name.lower() not in known]
    data = {
        "scope": {**active.to_dict(), "age": _age(active)},
        "changed": changed,
        "deps_built": deps_built,
        "available": view,
        "not_indexed": unknown,
        "inside": inside,
        "outside": outside,
    }
    hints = []
    if not deps_built:
        hints.append(
            f"the {DEPS!r} layer has never built, so this set could not be checked against "
            f"anything -- build it with knowledge_build(layer='{DEPS}')"
        )
    elif unknown:
        hints.append(
            "named but not in the dependency layer, so they narrow nothing: "
            + ", ".join(unknown)
            + " -- installed mods this project does not declare as dependencies are not "
              "indexed, and a typo looks exactly the same from here"
        )
    if active.active:
        hints.append(
            f"answers now come from {len(active.mods)} mod(s) plus the game and this "
            f"project; {outside} declaration(s) sit outside the set and will be NAMED, "
            "not hidden, when a search reaches them"
        )
    else:
        hints.append("no set in force: every mod in the index answers")
    return Result(True, data, "", " | ".join(hints))


def _age(active: modscope.ActiveSet) -> str:
    """How long the set has been in force, in the same words every layer age is
    printed in -- one formula, imported rather than re-spelled."""
    return _age_text(active.age()) if active.active and active.set_at else ""


# --------------------------------------------------------- asking a server


#: Stated rather than discovered. Two of them are reasoning, not measurement,
#: and they say so.
_BOUNDARIES = (
    "-serverMod mods are probably ABSENT from this list: it describes what a client "
    "must load, and a server-only mod is by definition not that. Reasoned, not tested.",
    "Version is not identity: nothing here reports which BUILD of a mod the server "
    "runs, so a local mod with the right id may be older than what is running there.",
    "A mod downloaded but not linked into the modpack is still installed; this walks "
    "the modpack folder and the mods this project declares, not the Steam content "
    "directory.",
)


def _refuse_query(host: str, port: int, exc: Exception) -> Result:
    if isinstance(exc, a2s.ChallengeRotation):
        return fail(
            f"{host}:{port} kept issuing a challenge and never answered the mod-list "
            f"query: {exc}",
            hint="this is a host-side filter, not a fault here -- retrying does not "
                 "converge, and nothing else asks the same question. Take the mod set "
                 "from the server's own listing instead, and declare it with "
                 "knowledge_scope(mods=[...])",
        )
    if isinstance(exc, a2s.A2STimeout):
        return fail(
            f"{host}:{port} did not answer: {exc}",
            hint=f"the most likely cause is that {port} is the GAME port, which never "
                 f"answers -- A2S answers on the QUERY port. It cannot be derived from "
                 f"the game port (252 distinct offsets in a live sample); common ones are "
                 f"{_COMMON_OFFSETS}, every server browser shows it, and for a local stand "
                 "it is steamQueryPort in the server config",
        )
    return fail(
        f"{host}:{port} answered with something this cannot read: {exc}",
        hint="the reply arrived but is not the Source query protocol -- check that the "
             "port is the query port of a DayZ server",
    )


def server_mods(address: str = "", query_port: int = 0, timeout: float = 6.0) -> Result:
    """Ask a running server which mods it runs, and PROPOSE an active set.

    `address` is "host" or "host:port"; `query_port` overrides an embedded one.
    **It must be the QUERY port, not the game port.** The game port never
    answers a Source query -- measured on six live servers, silent on all six,
    while the query port answered on all six. The two are not related by a
    fixed offset (252 distinct offsets in a live sample): a server browser
    shows it, and for a local stand it is `steamQueryPort` in the server config.

    The answer is three buckets, matched by Workshop id and never by name:

      matched                    the server runs it and it is installed here
      on_server_not_installed    the server runs it and this machine has not
      installed_not_on_server    installed here, the server does not run it

    **Nothing is applied.** `proposed_scope` and `apply` are a suggestion and
    the exact call that would take it; running that call is a separate,
    deliberate act. A query that silently rescoped the index would make a
    mismatch an invisible action rather than something the caller read.

    Three things this cannot see, and it says so in `notes` rather than letting
    the answer read as complete: server-only mods (reasoned, not tested), the
    BUILD of a mod behind an id, and mods downloaded but not linked into the
    modpack.
    """
    guard = require_project()
    if guard:
        return guard
    host, port = _address(address, query_port)
    if not host:
        return fail(
            "no server address",
            hint="pass address='host:queryport', or address='host' with query_port=",
        )
    if not port:
        return fail(
            "a query port is needed and cannot be derived from the game port",
            hint=f"measured: 252 distinct offsets between the two in a live sample. Common "
                 f"ones are {_COMMON_OFFSETS}; every server browser displays it, and for a "
                 "local stand it is steamQueryPort in the server config",
        )
    if not 0 < port < 65536:
        # Checked here rather than left to the socket: down there the same
        # mistake comes back through the catch-all as "answered with something
        # this cannot read", which describes a reply that never happened.
        return fail(
            f"{port} is not a port number",
            hint="a query port is between 1 and 65535 -- nothing was sent",
        )
    store, failure = _index()
    if failure:
        return failure

    notes: list[str] = []
    try:
        info = a2s.query_info(host, port, timeout=timeout)
    except a2s.A2SError as exc:
        return _refuse_query(host, port, exc)
    try:
        answer = a2s.query_mods(host, port, timeout=timeout)
    except a2s.A2SError as exc:
        return _refuse_query(host, port, exc)

    profile = session.profile()
    installed = modscope.installed_mods(profile, session.game())
    buckets = modscope.match_by_id(answer.mods, installed)

    deps_built = store.layer(DEPS) is not None
    indexed = {folder.lower() for folder, _ in store.mod_folders(DEPS)} if deps_built else set()
    data = buckets.to_dict()
    for entry in data["matched"]:
        entry["indexed"] = entry["folder"].lower() in indexed
    for entry in data["installed_not_on_server"]:
        entry["indexed"] = entry["folder"].lower() in indexed

    proposed = buckets.proposed()
    source = f"the server at {host}:{port}"
    unusable = [entry["folder"] for entry in data["matched"] if not entry["indexed"]]

    if not answer.complete:
        notes.append(
            "the mod list did not decode completely, so this may be a PARTIAL set: "
            + (answer.problem or "")
            + (f" (chunks {answer.chunks_seen} of {answer.chunk_total}, missing "
               f"{list(answer.missing_chunks)})" if answer.missing_chunks else "")
        )
    if buckets.unidentified:
        notes.append(
            "installed here with no Workshop id, so they cannot be matched at all: "
            + ", ".join(m.folder for m in buckets.unidentified)
        )
    for workshop_id, folders in buckets.shared_ids():
        notes.append(
            f"{workshop_id} is carried by more than one folder here ({', '.join(folders)}), "
            "and all of them are proposed -- nothing reports which BUILD the server runs, "
            "so which copy matches it cannot be decided from here"
        )
    own = list(getattr(profile, "own_mod_dirs", []) or [])
    if own:
        notes.append(
            "this project's own mods have no Workshop id and never appear in a match: "
            + ", ".join(own) + " -- they live in the project layer, which no set narrows"
        )
    if unusable:
        notes.append(
            "matched but not indexed, so scoping to them narrows nothing: "
            + ", ".join(unusable)
            + " -- they are not declared as dependencies of this project"
            + ("" if deps_built else f", and the {DEPS!r} layer has never been built")
        )
    notes.extend(_BOUNDARIES)

    data.update({
        "address": {"host": host, "query_port": port, "game_port": info.game_port},
        "server": info.to_dict(),
        "mods": [m.to_dict() for m in answer.mods],
        "transport": {
            "declared": answer.declared, "parsed": len(answer.mods),
            "chunk_total": answer.chunk_total, "chunks_seen": answer.chunks_seen,
            "missing_chunks": list(answer.missing_chunks),
            "blob_bytes": answer.blob_bytes, "signatures": list(answer.signatures),
        },
        "complete": answer.complete,
        "proposed_scope": proposed,
        "apply": f"knowledge_scope(mods={proposed!r}, source={source!r})",
        "applied": False,
        "notes": notes,
    })
    hint = (
        f"{len(data['matched'])} of {len(answer.mods)} mod(s) on that server are installed "
        f"here. NOTHING was changed -- apply the proposal with {data['apply']}"
    )
    if data["on_server_not_installed"]:
        hint += (
            f" | {len(data['on_server_not_installed'])} mod(s) run there and are not "
            "installed here, so the index can say nothing about them at all"
        )
    if not answer.complete:
        hint += " | the list decoded incompletely -- see notes before applying it"
    return Result(True, data, "", hint)
