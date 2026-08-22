"""Asking a running DayZ server which mods it runs, over UDP, with the standard
library and nothing else.

**A2S works on DayZ -- on the QUERY port, not the game port.** That correction
is the reason this module exists: the note this project carried for a while,
that DayZ does not answer Source queries, was measured against the game port.
Checked again on six live servers, the game port was silent on all six and the
query port answered on all six.

**The query port is not derivable from the game port.** A live sample of
public servers showed 252 distinct offsets between the two. `{game+1, game+3,
27016}` covers roughly three quarters of them and nothing covers all: for our
own stand the number is `steamQueryPort` in the server config, and for a
foreign server the caller supplies it (every server browser displays it).

What the two queries cost and give:

  `A2S_INFO`   no challenge, one round trip: the server's name, its player
               count, its version, and -- in the extra-data field -- the GAME
               port, which is how a caller can tell the two apart.
  `A2S_RULES`  a challenge first (header `A` and four bytes), then the resend
               carrying them (header `E`). The reply holds the mod list.

**The mod list is chunked inside the rules dictionary.** The reply arrives as
one datagram -- no Source-level split and no bzip2 was ever observed -- but the
dictionary carries, beside ordinary string rules, a set of entries whose key is
two RAW bytes, `(chunk index, chunk total)`, 1-based. Values are 127 bytes each
except the last, and they are escaped so no value can contain a NUL (which ends
a string) or 0xFF (which starts a packet header): `01 01` is `00`, `01 02` is
`FF`, `01 03` is `01`. **The escapes are undone after the chunks are joined,
never per chunk** -- an escape pair straddling a 127-byte boundary decodes
wrong otherwise, and at 127 bytes that is one boundary in 127, not a rarity.

Decoded, the blob is: 4 header bytes (the first is a version, 0x02 observed),
one byte of mod count, then per mod 4 bytes of checksum, one byte whose LOW
NIBBLE is the byte length of the Workshop id, the id little-endian, one byte of
name length and the UTF-8 name; then a signature count and the `.bikey` names.
Measured ceiling across the live sample: 122 mods, 33 chunks, 4139 bytes, no
truncation, and the declared mod count equalled the parsed count every time.

**The known failure mode has a name.** Some hosts answer `A2S_INFO` and then
issue a fresh `A2S_RULES` challenge forever -- an anti-amplification filter.
Retrying does not converge, so this stops after a bounded number of rounds and
says which of the two happened. Like every other long operation in this server,
it also carries a deadline: a query that never returns is the one failure the
calling agent cannot diagnose.

This module speaks the protocol and decodes bytes. It does not decide anything
about the mods it finds -- matching them against what is installed is
`knowledge/scope.py`, and it is done by Workshop id, never by name.
"""
from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field, replace

#: A whole (unsplit) Source packet.
WHOLE = b"\xff\xff\xff\xff"
#: One fragment of a split reply.
SPLIT = b"\xfe\xff\xff\xff"

_INFO_VERB = b"TSource Engine Query\x00"
_RULES_VERB = b"V"

_HEADER_CHALLENGE = 0x41  # 'A'
_HEADER_INFO = 0x49       # 'I'
_HEADER_RULES = 0x45      # 'E'

#: Seconds a whole query may take, handshake included.
DEFAULT_TIMEOUT = 6.0
#: How many times a challenge may be answered before the host is declared to be
#: rotating it. Three is generous: a cooperative host answers on the second
#: send, and a filtering one never answers at all.
CHALLENGE_ROUNDS = 3
#: Observed chunk size. Documented rather than enforced -- the decoder reads
#: what arrives, and a host that chose another size still decodes.
CHUNK_BYTES = 127
_MAX_DATAGRAM = 65535
#: The four bytes that stand in for "I have no challenge yet".
_NO_CHALLENGE = WHOLE

#: The escape table, measured. Written once and used in both directions by the
#: tests, so a change here cannot pass by being made twice.
_ESCAPES = {0x01: 0x00, 0x02: 0xFF, 0x03: 0x01}
_ESCAPE = 0x01


class A2SError(Exception):
    """A query that could not be completed. Always says which half failed."""


class A2STimeout(A2SError):
    """Nothing came back before the deadline."""


class A2SProtocolError(A2SError):
    """Something came back and it was not what the protocol describes."""


class ChallengeRotation(A2SError):
    """The host kept asking for a challenge instead of answering.

    Measured on live hosts: an anti-amplification filter that issues a fresh
    token every time and never accepts one. Named rather than retried, because
    retrying is what turns a diagnosable refusal into a hang.
    """


# ------------------------------------------------------------------- decoding


class _Short(Exception):
    """Internal: the buffer ended in the middle of a field."""


class _Cursor:
    """A bounds-checked read head. Every overrun becomes one exception type, so
    a truncated reply degrades into a partial answer instead of an IndexError
    from whichever field happened to be last."""

    def __init__(self, blob: bytes, at: int = 0):
        self.blob = blob
        self.at = at

    def take(self, count: int) -> bytes:
        if count < 0 or self.at + count > len(self.blob):
            raise _Short(f"wanted {count} bytes at {self.at} of {len(self.blob)}")
        out = self.blob[self.at:self.at + count]
        self.at += count
        return out

    def byte(self) -> int:
        return self.take(1)[0]

    def text(self) -> str:
        """A NUL-terminated string, as every Source string is."""
        end = self.blob.find(b"\x00", self.at)
        if end < 0:
            raise _Short(f"unterminated string at {self.at}")
        out = self.blob[self.at:end]
        self.at = end + 1
        return out.decode("utf-8", "replace")


def unescape(raw: bytes) -> bytes:
    """Undo the value escaping. Call it on the JOINED chunks, never on one.

    A lone escape byte at the very end has nothing to escape and is kept: it is
    a byte of the payload, and dropping it would shift nothing visible (there
    is nothing after it) while shortening the blob by one -- the quietest
    possible corruption. An unrecognised pair keeps the byte that followed
    rather than guessing at a fourth escape nobody has seen.
    """
    out = bytearray()
    index = 0
    while index < len(raw):
        byte = raw[index]
        if byte == _ESCAPE and index + 1 < len(raw):
            out.append(_ESCAPES.get(raw[index + 1], raw[index + 1]))
            index += 2
            continue
        out.append(byte)
        index += 1
    return bytes(out)


def split_rules(body: bytes) -> tuple[int, list[tuple[bytes, bytes]], bool]:
    """An `A2S_RULES` body as (declared count, raw key/value pairs, truncated).

    Keys and values stay BYTES. Decoding them as text here would destroy the
    chunk keys, which are two raw bytes and not a string at all.
    """
    if len(body) < 2:
        return 0, [], True
    declared = struct.unpack_from("<h", body, 0)[0]
    at = 2
    pairs: list[tuple[bytes, bytes]] = []
    while at < len(body):
        end = body.find(b"\x00", at)
        if end < 0:
            return declared, pairs, True
        key = body[at:end]
        at = end + 1
        end = body.find(b"\x00", at)
        if end < 0:
            return declared, pairs, True
        pairs.append((key, body[at:end]))
        at = end + 1
    return declared, pairs, False


@dataclass(frozen=True)
class Reassembled:
    """The chunked payload put back together, with the transport facts that say
    whether to believe it."""

    blob: bytes = b""
    total: int = 0
    seen: int = 0
    missing: tuple[int, ...] = ()
    plain: dict[str, str] = field(default_factory=dict)


def reassemble(pairs) -> Reassembled:
    """Join the chunked rules by index and un-escape the result.

    A chunk key is exactly two bytes, `(index, total)`, 1-based. That shape
    alone is not enough to tell a chunk from a two-letter rule name, so the
    candidates are grouped by their declared total and the group whose indices
    are exactly 1..total wins. A stray two-byte rule forms a group of one with
    a large total, which is never complete; a real set with a lost chunk is not
    complete either, and then the largest group wins and the gap is REPORTED --
    joining what arrived and calling it the answer would decode into plausible
    nonsense, which is the worst outcome for a list about to be trusted.
    """
    groups: dict[int, dict[int, bytes]] = {}
    plain: dict[str, str] = {}
    for key, value in pairs:
        if len(key) == 2 and 1 <= key[0] <= key[1]:
            groups.setdefault(key[1], {})[key[0]] = value
        else:
            plain[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")

    chosen_total = 0
    chosen: dict[int, bytes] = {}
    for total, parts in sorted(groups.items()):
        complete = len(parts) == total
        best_complete = chosen_total and len(chosen) == chosen_total
        if best_complete and not complete:
            continue
        if complete and not best_complete:
            chosen_total, chosen = total, parts
            continue
        if len(parts) > len(chosen):
            chosen_total, chosen = total, parts

    # Everything in a group that lost is an ordinary rule after all.
    for total, parts in groups.items():
        if total == chosen_total:
            continue
        for index, value in parts.items():
            plain[bytes([index, total]).decode("utf-8", "replace")] = value.decode(
                "utf-8", "replace"
            )

    joined = b"".join(chosen[index] for index in sorted(chosen))
    missing = tuple(i for i in range(1, chosen_total + 1) if i not in chosen)
    return Reassembled(
        blob=unescape(joined),
        total=chosen_total,
        seen=len(chosen),
        missing=missing,
        plain=plain,
    )


@dataclass(frozen=True)
class ServerMod:
    """One mod as the server describes it. The id is the identity; the name is
    a label, and matching on it is what `knowledge/scope.py` refuses to do."""

    workshop_id: int
    name: str
    checksum: str = ""

    def to_dict(self) -> dict:
        return {"workshop_id": self.workshop_id, "name": self.name,
                "checksum": self.checksum}


@dataclass(frozen=True)
class ModAnswer:
    """What one server said it runs, and how sure the decoder is of it."""

    mods: tuple[ServerMod, ...] = ()
    signatures: tuple[str, ...] = ()
    declared: int = 0
    signatures_declared: int = 0
    version: int = 0
    leftover: int = 0
    problem: str = ""
    chunk_total: int = 0
    chunks_seen: int = 0
    missing_chunks: tuple[int, ...] = ()
    blob_bytes: int = 0

    @property
    def complete(self) -> bool:
        """True only when nothing was lost and nothing disagrees.

        The declared count is part of it because across every live server
        sampled it equalled the parsed count -- so a disagreement is evidence
        of a decoding fault, not a quirk to shrug at.
        """
        return (
            not self.problem
            and not self.missing_chunks
            and self.declared == len(self.mods)
        )

    def to_dict(self) -> dict:
        return {
            "mods": [m.to_dict() for m in self.mods],
            "signatures": list(self.signatures),
            "declared": self.declared,
            "parsed": len(self.mods),
            "signatures_declared": self.signatures_declared,
            "version": self.version,
            "leftover": self.leftover,
            "problem": self.problem,
            "chunk_total": self.chunk_total,
            "chunks_seen": self.chunks_seen,
            "missing_chunks": list(self.missing_chunks),
            "blob_bytes": self.blob_bytes,
            "complete": self.complete,
        }


def decode_mods(blob: bytes) -> ModAnswer:
    """The joined, un-escaped payload as a mod list.

    Never raises on a short buffer: a partial answer that SAYS it is partial is
    worth having, and an exception would throw away the records that did
    decode. What it must never do is return records without the disagreement --
    that is the silent lie this whole phase is built against.
    """
    if len(blob) < 5:
        return ModAnswer(
            blob_bytes=len(blob),
            problem="the reply carried no mod list at all "
                    f"({len(blob)} bytes, at least 5 are needed for a header and a count)",
        )
    cursor = _Cursor(blob)
    version = cursor.byte()
    cursor.take(3)  # not identified; constant across every sample taken
    declared = cursor.byte()
    mods: list[ServerMod] = []
    signatures: list[str] = []
    signatures_declared = 0
    problem = ""
    try:
        for _ in range(declared):
            checksum = cursor.take(4).hex()
            length = cursor.byte() & 0x0F
            workshop_id = int.from_bytes(cursor.take(length), "little")
            name = cursor.take(cursor.byte()).decode("utf-8", "replace")
            mods.append(ServerMod(workshop_id=workshop_id, name=name, checksum=checksum))
        signatures_declared = cursor.byte()
        for _ in range(signatures_declared):
            signatures.append(cursor.take(cursor.byte()).decode("utf-8", "replace"))
    except _Short as exc:
        problem = (
            f"the mod list ended early: {exc} -- {len(mods)} of {declared} mod(s) and "
            f"{len(signatures)} of {signatures_declared} signature(s) decoded"
        )
    if not problem and declared != len(mods):
        problem = f"the reply declared {declared} mod(s) and carried {len(mods)}"
    return ModAnswer(
        mods=tuple(mods),
        signatures=tuple(signatures),
        declared=declared,
        signatures_declared=signatures_declared,
        version=version,
        leftover=len(blob) - cursor.at,
        problem=problem,
        blob_bytes=len(blob),
    )


@dataclass(frozen=True)
class ServerInfo:
    """`A2S_INFO`, decoded. `game_port` is the field that matters here: it is
    how a caller confirms the port it was handed is the query port."""

    name: str = ""
    map: str = ""
    folder: str = ""
    game: str = ""
    players: int = 0
    max_players: int = 0
    version: str = ""
    game_port: int = 0
    steam_id: int = 0
    keywords: str = ""
    protocol: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name, "map": self.map, "folder": self.folder,
            "game": self.game, "players": self.players,
            "max_players": self.max_players, "version": self.version,
            "game_port": self.game_port, "steam_id": self.steam_id,
            "keywords": self.keywords, "protocol": self.protocol,
        }


def decode_info(body: bytes) -> ServerInfo:
    cursor = _Cursor(body)
    try:
        protocol = cursor.byte()
        name = cursor.text()
        map_name = cursor.text()
        folder = cursor.text()
        game = cursor.text()
        cursor.take(2)  # app id, always 0 here
        players = cursor.byte()
        max_players = cursor.byte()
        cursor.take(1)  # bots
        cursor.take(1)  # server type
        cursor.take(1)  # environment
        cursor.take(2)  # visibility, vac
        version = cursor.text()
    except _Short as exc:
        raise A2SProtocolError(f"the server info reply is truncated: {exc}") from exc

    game_port = steam_id = 0
    keywords = ""
    try:
        extra = cursor.byte()
        if extra & 0x80:
            game_port = struct.unpack("<H", cursor.take(2))[0]
        if extra & 0x10:
            steam_id = int.from_bytes(cursor.take(8), "little")
        if extra & 0x40:
            cursor.take(2)
            cursor.text()
        if extra & 0x20:
            keywords = cursor.text()
    except _Short:
        # The extra-data field is optional and its layout varies by engine.
        # What was read before the end still stands; what was not is left at
        # its zero, which reads as "not reported" everywhere it is used.
        pass
    return ServerInfo(
        name=name, map=map_name, folder=folder, game=game, players=players,
        max_players=max_players, version=version, game_port=game_port,
        steam_id=steam_id, keywords=keywords, protocol=protocol,
    )


# ------------------------------------------------------------------ transport


def _receive(sock: socket.socket, deadline: float) -> tuple[int, bytes]:
    """One logical reply: a whole packet, or every fragment of a split one.

    Split replies were never observed from DayZ's rules query, but the protocol
    allows them and a client that read one fragment as the whole answer would
    decode half a list without noticing.
    """
    parts: dict[int, bytes] = {}
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise A2STimeout(_late(parts, total))
        sock.settimeout(remaining)
        try:
            data, _ = sock.recvfrom(_MAX_DATAGRAM)
        except TimeoutError as exc:
            raise A2STimeout(_late(parts, total)) from exc
        except OSError as exc:
            raise A2SError(f"the query socket failed: {type(exc).__name__}: {exc}") from exc

        if data[:4] == WHOLE:
            if len(data) < 5:
                raise A2SProtocolError("a reply arrived with a header and no body")
            return data[4], data[5:]
        if data[:4] != SPLIT or len(data) < 12:
            # Something else on the wire. Ignored rather than fatal: the socket
            # is unconnected and anything may arrive at it.
            continue
        packet_id, total, number, _size = struct.unpack_from("<Ibbh", data, 4)
        at = 12
        if packet_id & 0x80000000:
            raise A2SProtocolError(
                "this reply is bzip2-compressed; no DayZ server observed has sent one, "
                "and decoding it here would be an untested guess at the header layout"
            )
        parts[number] = data[at:]
        if total and len(parts) == total:
            body = b"".join(parts[i] for i in sorted(parts))
            if body[:4] != WHOLE:
                raise A2SProtocolError("the reassembled split reply has no packet header")
            if len(body) < 5:
                raise A2SProtocolError("the reassembled split reply has no body")
            return body[4], body[5:]


def _late(parts: dict, total: int) -> str:
    if parts:
        return (
            f"no answer before the deadline; {len(parts)} of {total or '?'} fragment(s) "
            "had arrived"
        )
    return (
        "no answer before the deadline -- on DayZ this is what the GAME port does; "
        "A2S answers on the query port, which is not derivable from it"
    )


def _ask(
    host: str, port: int, verb: bytes, first_suffix: bytes, want: int,
    timeout: float, rounds: int,
) -> bytes:
    """Send `verb`, answer a challenge if one comes back, return the body.

    Bounded twice over: at most `rounds` challenges are answered, and the whole
    exchange runs against one deadline. Either bound alone leaves a hole -- a
    host can rotate challenges quickly enough to make the round count the only
    thing that stops it, and slowly enough to make the deadline the only thing
    that does.
    """
    if not host:
        raise A2SError("no host to query")
    if not 0 < int(port) < 65536:
        raise A2SError(f"{port!r} is not a port number")
    deadline = time.monotonic() + max(0.1, float(timeout))
    payload = WHOLE + verb + first_suffix
    tokens: list[bytes] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _ in range(max(1, int(rounds)) + 1):
            try:
                sock.sendto(payload, (host, int(port)))
            except socket.gaierror as exc:
                raise A2SError(f"{host} does not resolve: {exc}") from exc
            except OSError as exc:
                raise A2SError(
                    f"cannot send to {host}:{port}: {type(exc).__name__}: {exc}"
                ) from exc
            header, body = _receive(sock, deadline)
            if header == want:
                return body
            if header == _HEADER_CHALLENGE:
                token = body[:4]
                tokens.append(token)
                payload = WHOLE + verb + token
                continue
            raise A2SProtocolError(
                f"{host}:{port} answered with header 0x{header:02x}, "
                f"expected 0x{want:02x}"
            )
    rotating = len(set(tokens)) > 1
    raise ChallengeRotation(
        f"{host}:{port} asked for a challenge {len(tokens)} time(s) and never answered -- "
        + (
            "a fresh token every time, which is an anti-amplification filter: this host "
            "does not serve its mod list to anyone"
            if rotating else
            "the same token every time, so the resend is not being accepted"
        )
    )


def query_info(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> ServerInfo:
    """`A2S_INFO` from the server's QUERY port.

    Needs no challenge on any host measured, but answers one if asked. Its
    value beside the mod list is diagnostic: a host that answers this and
    refuses the rules query is filtering, not down.
    """
    body = _ask(host, port, _INFO_VERB, b"", _HEADER_INFO, timeout, CHALLENGE_ROUNDS)
    return decode_info(body)


def query_mods(
    host: str, port: int, timeout: float = DEFAULT_TIMEOUT,
    rounds: int = CHALLENGE_ROUNDS,
) -> ModAnswer:
    """The mod list a server runs, from its QUERY port.

    Raises `ChallengeRotation` on a host that filters the rules query,
    `A2STimeout` on silence (which is also what the GAME port does), and
    `A2SProtocolError` on a reply that is not what the protocol describes.
    A reply that arrives but decodes short comes back as an answer whose
    `complete` is False -- what was read is worth more than an exception, as
    long as the doubt travels with it.
    """
    body = _ask(host, port, _RULES_VERB, _NO_CHALLENGE, _HEADER_RULES, timeout, rounds)
    declared, pairs, truncated = split_rules(body)
    joined = reassemble(pairs)
    answer = decode_mods(joined.blob)
    problem = answer.problem
    if truncated and not problem:
        problem = f"the rules reply ended inside a pair ({declared} declared)"
    return replace(
        answer,
        chunk_total=joined.total,
        chunks_seen=joined.seen,
        missing_chunks=joined.missing,
        problem=problem,
    )
