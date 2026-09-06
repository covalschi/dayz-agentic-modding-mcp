"""The p3d byte strings five test files build, in one place.

They were pasted into each file on the stated principle that a test must not
depend on another test. That principle is about test STATE, and these are pure
byte constants -- but the copies had drifted apart on the one thing they all
encode: how many LODs a synthetic ODOL/MLOD carries by default (2/3 in one
file, 4/5 in three others). A default that differs by file is a fact about
nothing, and the day a check starts caring about the LOD count the files
disagree without saying so.

Not a conftest fixture: `GOOD_ODOL` and its neighbours are read at module
import in the files that use them, and a fixture cannot be.
"""
from __future__ import annotations

import struct

#: The prefix the synthetic mod's paths carry. Deliberately not any real mod --
#: and a parameter of the builders below, because one file's mod is named
#: differently and its paths have to match its own prefix.
PREFIX = "somemod"

#: What the ODOL/MLOD helpers default to. One number, one place, for the same
#: reason as everything else in this module.
ODOL_LODS = 4
MLOD_LODS = 5


def odol(lods: int = ODOL_LODS, tail: bytes = b"") -> bytes:
    """The smallest byte string the reader must accept as a binarized model."""
    return b"ODOL" + struct.pack("<II", 55, lods) + b"\x00" * (4 * lods) + tail


def mlod(lods: int = MLOD_LODS, tail: bytes = b"") -> bytes:
    """The same, for an unbinarized source model."""
    return b"MLOD" + struct.pack("<II", 0x101, lods) + tail


def named(*names: str) -> bytes:
    """Names as a p3d stores them: NUL-terminated, back to back."""
    return b"".join(n.encode("ascii") + b"\x00" for n in names)


def resolved(prefix: str = PREFIX) -> bytes:
    """What `binarize` leaves in an artifact when it really resolved the rvmat:
    procedural stages, a vanilla path, and the mod's own textures."""
    return named(
        "#(ai,64,64,1)fresnel(1,0.7)",
        "#(argb,8,8,3)color(1,1,1,1,dt)",
        r"dz\data\data\env_land_co.paa",
        rf"{prefix}\data\textures\thing_nohq.paa",
        rf"{prefix}\data\textures\thing_smdi.paa",
    )


def material(prefix: str = PREFIX) -> bytes:
    """The rvmat reference itself, with nothing resolved behind it."""
    return named(rf"{prefix}\data\textures\thing.rvmat")


RESOLVED = resolved()
MATERIAL = material()

#: A build from the declared root: every marker of a resolved material.
GOOD_ODOL = odol(tail=MATERIAL + RESOLVED)

#: A valid ODOL with plausible paths and no inlined material -- what a run from
#: the wrong working directory produces, with a success exit code.
UNRESOLVED_ODOL = odol(tail=MATERIAL)
