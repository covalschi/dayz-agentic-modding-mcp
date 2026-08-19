"""Uniform result envelope returned by every tool.

The agent must never have to parse prose: `error` says what went wrong,
`hint` says what to do about it.
"""
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class Result:
    ok: bool
    data: Any = None
    error: str = ""
    hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def ok(data: Any = None) -> Result:
    return Result(True, data)


def fail(error: str, hint: str = "") -> Result:
    return Result(False, None, error, hint)
