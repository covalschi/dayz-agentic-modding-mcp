"""A drive letter of its own, for one run of a tool that walks its whole drive.

**`binarize` reads the entire drive before it reads a model.** Read off the
running process (DayZ Tools rev. 163709): for the first minutes its one busy
thread sits inside `FindNextFileW` and the process holds a handle on the ROOT
of the drive its working directory is on. Nothing is read from the model until
that walk is over, so how long a build takes is the size of the disk and not of
the model. Measured on one machine:

* five small models of one project, each started from the project's own folder
  on a large drive: 510, 522, 538 and 540 seconds for the four that were timed;
* the same five, each started from a letter substituted for that folder, with
  the source named on the letter and the output left where it was: a tenth of
  a second each, the artifacts the same size to within three bytes;
* a small model of another project, one run after the other: 650.8 seconds
  from its folder, a tenth of a second from a letter. From that folder it had
  taken 43.8 and then 75.6 to 78.7 seconds on earlier days.

The walk goes through junctions too: with an unpacked game tree of 103,359
files linked into the root, the tenth of a second became half a second. And
the very first run from a letter took 28 seconds, which no run after it did;
that one is not explained.

So the walk is not avoided, it is made short: the working directory goes on a
drive that holds the model root and nothing else. `subst` makes such a drive in
milliseconds, out of nothing but a name.

**A letter whose root is the model root is the same root.** `binarize` takes
its root from the working directory and from nowhere else (see `binarize.py`),
and every path inside a model resolves against it -- so from the letter the
artifact comes out resolved exactly as it does from the folder. That is not
argued here; the corpus test builds a real model from a real letter and reads
the two checks that only a correct working directory passes.

**Every run maps its own letter and removes only its own.** A letter that
already points at this very root may be another build's, about to be taken
away in the middle of this one, and nothing tells that apart from a letter
some killed run left behind. Borrowing it would save one short-lived letter
and risk a build whose root vanishes under it. The price is the other case: a
run that is killed outright -- the server process, not the tool -- never gives
its letter back, and it stays until the next logon, which is as long as any
`subst` mapping lives. `subst <letter>: /D` removes it by hand.

**Whether a letter is free is asked of the machine, not of the file system.**
A card reader with no card in it holds a letter on which no path exists, so
"is there anything at `Z:\\`" calls it free. And `subst` still has the last
word: two runs that both see a letter free cannot both map it, and the one
that is refused moves on to the next.

**No letter is never a failure.** Off Windows, with every letter taken, with
no `subst` to call: the caller is handed None and runs from the root itself,
which is as correct as it ever was and as slow.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ..procs import run_captured

#: The letters on offer, taken from the far end of the alphabet, where nothing
#: is plugged in by itself. A and B still belong to floppies in every dialog
#: that remembers them, C is the system, and P is left out because DayZ Tools
#: mounts its own work drive there.
LETTERS = "ZYXWVUTSRQONMLKJIHGFED"

#: `procs.run_captured`'s code for "the command could not be started": the
#: same answer for every letter, so the first one ends the search.
CANNOT_START = 127


def run_subst(args: list[str]) -> tuple[int, str]:
    """The `subst` command itself: `["Z:", <dir>]` maps, `["Z:", "/D"]` unmaps."""
    return run_captured(["subst", *args])


def letter_taken(letter: str) -> bool:
    """Whether the machine already has a drive under this letter -- a disk, a
    network share, another substitution, or a reader with nothing in it."""
    import ctypes

    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return bool(mask >> (ord(letter.upper()) - ord("A")) & 1)


@contextmanager
def substituted(
    root: str | os.PathLike[str],
    *,
    letters: str = LETTERS,
    subst: Callable[[list[str]], tuple[int, str]] = run_subst,
    taken: Callable[[str], bool] = letter_taken,
    platform: str = sys.platform,
) -> Iterator[Path | None]:
    """A drive whose root is `root`, for as long as the `with` block runs.

    Yields the drive's root (`Z:\\`), or None when there is no drive to be had
    and the caller should run from `root` as it is. A `root` that is a whole
    drive already is yielded back untouched: it is what a substitution would
    have made.

    `subst` and `taken` are the two questions put to the machine, injected so
    the decisions can be exercised without a letter appearing on it.
    """
    root = Path(root)
    if platform != "win32":
        yield None
        return
    if root.parent == root:
        yield root
        return

    mapped = ""
    for letter in letters:
        if taken(letter):
            continue
        code, _ = subst([f"{letter}:", str(root)])
        if code == 0:
            mapped = letter
            break
        if code == CANNOT_START:
            break
    if not mapped:
        yield None
        return

    try:
        yield Path(f"{mapped}:\\")
    finally:
        subst([f"{mapped}:", "/D"])
