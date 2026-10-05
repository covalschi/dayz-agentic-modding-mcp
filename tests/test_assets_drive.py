"""A drive letter of its own for a model root, for as long as one run needs it.

`binarize` walks the whole DRIVE its working directory is on before it reads a
single model, so the same build takes a tenth of a second from a drive of its
own and nine minutes from a folder on a large one (measured; see
`assets/drive.py`). The cure is a substituted letter whose root IS the model
root. These tests pin the part
that can go wrong without anybody noticing: a letter that is not really free, a
mapping that is somebody else's, and a letter left behind.

Hermetic, but for the two tests at the bottom: the `subst` command and the "is
this letter a drive" question are both injected, so the decisions are pinned
without a real letter being mapped. The last two ask the real machine, because
stand-ins only prove the logic against what the stand-ins assume.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from dayz_mcp.assets.drive import LETTERS, letter_taken, substituted


class Subst:
    """A stand-in for the `subst` command: a table of letters, and every call.

    Answers the way the runner in `procs` does -- a code and the text, never an
    exception -- and refuses a letter that is already mapped, as the real one
    does.
    """

    def __init__(self, table: dict[str, str] | None = None):
        self.table = dict(table or {})
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(list(args))
        letter = args[0][0].upper()
        if args[1].upper() == "/D":
            return (0, "") if self.table.pop(letter, None) is not None else (1, "Invalid parameter")
        if letter in self.table:
            return 1, "Drive already SUBSTed"
        self.table[letter] = args[1]
        return 0, ""


def nothing_taken(letter: str) -> bool:
    return False


def test_a_free_letter_is_mapped_for_the_run_and_given_back(tmp_path):
    subst = Subst()
    with substituted(tmp_path, letters="ZY", subst=subst, taken=nothing_taken, platform="win32") as drive:
        assert drive == Path("Z:\\")
        assert subst.table == {"Z": str(tmp_path)}
    assert subst.table == {}
    assert subst.calls == [["Z:", str(tmp_path)], ["Z:", "/D"]]


def test_a_letter_that_is_already_a_drive_is_passed_over(tmp_path):
    """A card reader with no card in it holds a letter that no path exists on,
    so "does `Z:\\` exist" is the wrong question. The machine is asked which
    letters are drives, and `subst` is not even tried on one of those."""
    subst = Subst()
    with substituted(tmp_path, letters="ZY", subst=subst, taken=lambda l: l == "Z", platform="win32") as drive:
        assert drive == Path("Y:\\")
    assert subst.calls == [["Y:", str(tmp_path)], ["Y:", "/D"]]


def test_a_letter_subst_refuses_is_passed_over(tmp_path):
    """Two builds at once both see the same letter free. The one that loses
    takes the next letter instead of failing -- or, worse, of running from the
    winner's root."""
    elsewhere = str(tmp_path / "elsewhere")
    subst = Subst({"Z": elsewhere})
    with substituted(tmp_path, letters="ZY", subst=subst, taken=nothing_taken, platform="win32") as drive:
        assert drive == Path("Y:\\")
    assert subst.table == {"Z": elsewhere}


def test_a_letter_somebody_else_put_on_this_very_root_is_not_borrowed(tmp_path):
    """It may be another build's, about to be taken away in the middle of this
    one. Nothing tells that apart from a letter left by a run that was killed,
    so every run maps its own and removes only its own."""
    subst = Subst({"Z": str(tmp_path)})
    with substituted(tmp_path, letters="ZY", subst=subst, taken=nothing_taken, platform="win32") as drive:
        assert drive == Path("Y:\\")
    assert subst.table == {"Z": str(tmp_path)}


def test_no_free_letter_means_no_drive_rather_than_a_failure(tmp_path):
    subst = Subst()
    with substituted(tmp_path, letters="ZY", subst=subst, taken=lambda l: True, platform="win32") as drive:
        assert drive is None
    assert subst.calls == []


def test_a_subst_that_cannot_start_is_not_asked_again(tmp_path):
    """127 is the runner's code for "the command could not be started". That is
    the same answer for every letter, and it must cost the speed, not the
    build -- and not twenty more attempts either."""
    calls: list[list[str]] = []

    def gone(args: list[str]) -> tuple[int, str]:
        calls.append(list(args))
        return 127, "cannot start"

    with substituted(tmp_path, letters="ZYX", subst=gone, taken=nothing_taken, platform="win32") as drive:
        assert drive is None
    assert len(calls) == 1


def test_there_is_no_drive_to_substitute_off_windows(tmp_path):
    subst = Subst()
    with substituted(tmp_path, letters="Z", subst=subst, taken=nothing_taken, platform="linux") as drive:
        assert drive is None
    assert subst.calls == []


def test_a_root_that_is_a_drive_already_needs_no_letter(tmp_path):
    """A work drive mounted for the project is exactly what a substitution
    would make. Mapping a second letter onto it changes nothing but the count
    of letters."""
    whole = Path(tmp_path.anchor)
    subst = Subst()
    with substituted(whole, letters="Z", subst=subst, taken=nothing_taken, platform="win32") as drive:
        assert drive == whole
    assert subst.calls == []


def test_the_letter_is_given_back_when_the_run_blows_up(tmp_path):
    subst = Subst()
    with pytest.raises(RuntimeError):
        with substituted(tmp_path, letters="Z", subst=subst, taken=nothing_taken, platform="win32"):
            raise RuntimeError("the run died")
    assert subst.table == {}


def test_the_letters_on_offer_leave_the_system_and_the_tools_alone():
    """A and B belong to floppies in every dialog that still remembers them, C
    is the system, and P is where DayZ Tools mounts its own work drive."""
    assert not set("ABCP") & set(LETTERS)
    assert len(set(LETTERS)) == len(LETTERS) >= 10


# ------------------------------------------------------------- the real command

windows = pytest.mark.skipif(os.name != "nt", reason="drive letters and subst are Windows' own")


@windows
def test_the_drive_this_test_runs_on_is_a_taken_letter(tmp_path):
    assert letter_taken(tmp_path.drive[0])


@windows
def test_a_real_letter_shows_the_root_and_is_gone_afterwards(tmp_path):
    """The one test that maps a real letter, for the instant it takes to look
    through it."""
    (tmp_path / "inside.txt").write_text("here", encoding="utf-8")
    with substituted(tmp_path) as drive:
        assert drive is not None, "no free drive letter on this machine"
        assert drive.parent == drive and drive != tmp_path
        assert (drive / "inside.txt").read_text(encoding="utf-8") == "here"
        assert letter_taken(drive.drive[0])
    assert not drive.exists()
    assert not letter_taken(drive.drive[0])
