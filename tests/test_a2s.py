"""The Source query protocol, as DayZ actually speaks it.

Every shape asserted here was measured against live servers before it was
written down, and each test names the way the decoder could otherwise be
quietly wrong:

* the reply arrives as ONE datagram, but the mod list inside it is cut into
  127-byte chunks keyed by two raw bytes `(index, total)`, 1-based;
* the chunk values are escaped, and the escapes are undone AFTER the chunks
  are joined -- an escape pair split across a chunk boundary is the case that
  tells a correct implementation from one that happens to work;
* the mod record's Workshop id has a length in the LOW NIBBLE of one byte;
* some hosts answer INFO and rotate the RULES challenge forever, which must
  end in a named refusal rather than a loop.

No test here reaches the network. The transport tests bind a UDP socket on
the loopback interface and answer themselves, so the suite neither needs a
public server to be up nor is slowed by one that is not.
"""
from __future__ import annotations

import socket
import struct
import threading

import pytest

from dayz_mcp import a2s

# --------------------------------------------------------------- fixtures

WHOLE = b"\xff\xff\xff\xff"


def escape(raw: bytes) -> bytes:
    """The inverse of `a2s.unescape`, so the fixtures below are built the way
    a server builds them rather than by copying the decoder's own table."""
    out = bytearray()
    for byte in raw:
        if byte == 0x00:
            out += b"\x01\x01"
        elif byte == 0xFF:
            out += b"\x01\x02"
        elif byte == 0x01:
            out += b"\x01\x03"
        else:
            out.append(byte)
    return bytes(out)


def mod_blob(mods, signatures=(), header=b"\x01\x00\x00\x00", count=None) -> bytes:
    """The decoded payload: 4 header bytes, a mod count, then the records."""
    out = bytearray(header)
    out.append(len(mods) if count is None else count)
    for workshop_id, name in mods:
        out += b"\xaa\xbb\xcc\xdd"
        raw = workshop_id.to_bytes(max(1, (workshop_id.bit_length() + 7) // 8), "little")
        # High nibble deliberately set: the length is the LOW nibble, and a
        # decoder reading the whole byte would ask for hundreds of bytes.
        out.append(0x30 | len(raw))
        out += raw
        encoded = name.encode("utf-8")
        out.append(len(encoded))
        out += encoded
    out.append(len(signatures))
    for signature in signatures:
        encoded = signature.encode("utf-8")
        out.append(len(encoded))
        out += encoded
    return bytes(out)


def chunk_rules(blob: bytes, size: int = 127) -> list[tuple[bytes, bytes]]:
    """`blob` as the rules dictionary carries it: escaped first, then cut."""
    escaped = escape(blob)
    pieces = [escaped[i:i + size] for i in range(0, len(escaped), size)] or [b""]
    total = len(pieces)
    return [(bytes([i + 1, total]), piece) for i, piece in enumerate(pieces)]


def rules_body(pairs, declared: int | None = None) -> bytes:
    out = struct.pack("<h", len(pairs) if declared is None else declared)
    for key, value in pairs:
        out += key + b"\x00" + value + b"\x00"
    return out


def info_body(name="stand", players=3, game_port=2302) -> bytes:
    out = bytearray()
    out.append(17)
    for text in (name, "chernarusplus", "dayz", "a link"):
        out += text.encode("utf-8") + b"\x00"
    out += struct.pack("<H", 0)
    out += bytes([players, 60, 0])
    out += b"d"
    out += b"w"
    out += bytes([0, 1])
    out += b"1.29.163709\x00"
    out.append(0x80)  # EDF: the game port follows
    out += struct.pack("<H", game_port)
    return bytes(out)


class FakeServer:
    """A UDP responder on the loopback interface.

    `script` is called with each request and returns the bytes to answer with,
    or None to stay silent -- which is how the timeout path is exercised
    without waiting for a real server that is not there.
    """

    def __init__(self, script):
        self.script = script
        self.requests: list[bytes] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.25)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._sock.getsockname()
        return host, port

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, peer = self._sock.recvfrom(65535)
            except (TimeoutError, OSError):
                continue
            self.requests.append(data)
            reply = self.script(data, len(self.requests))
            for packet in (reply if isinstance(reply, list) else [reply]):
                if packet is not None:
                    self._sock.sendto(packet, peer)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()


@pytest.fixture
def serve():
    made: list[FakeServer] = []

    def start(script) -> FakeServer:
        server = FakeServer(script)
        made.append(server)
        return server

    yield start
    for server in made:
        server.close()


def rules_script(pairs, declared=None):
    """Answer the challenge, then the rules -- the two-step A2S_RULES needs."""

    def script(request, nth):
        if request[4:5] != b"V":
            return None
        if request[5:] == WHOLE:
            return WHOLE + b"A" + b"\x11\x22\x33\x44"
        return WHOLE + b"E" + rules_body(pairs, declared)

    return script


# ------------------------------------------------------------- unescaping


def test_the_three_escapes_are_undone_and_nothing_else_is():
    """Measured: values are escaped so they can never contain a NUL (which
    would end the string) or 0xFF (which starts a packet header). 0x01 has to
    be escaped too, because it is the escape."""
    assert a2s.unescape(b"\x01\x01") == b"\x00"
    assert a2s.unescape(b"\x01\x02") == b"\xff"
    assert a2s.unescape(b"\x01\x03") == b"\x01"
    assert a2s.unescape(b"\x02\x03\x04") == b"\x02\x03\x04"
    assert a2s.unescape(escape(bytes(range(256)))) == bytes(range(256))


def test_a_trailing_escape_byte_is_kept_rather_than_swallowed():
    """A lone 0x01 at the very end has nothing to escape. Dropping it would
    shorten the blob by one byte and shift every field after it -- and there is
    nothing after it, which is exactly why the bug would go unnoticed."""
    assert a2s.unescape(b"\x07\x01") == b"\x07\x01"


def test_an_unknown_escape_keeps_the_byte_it_names():
    """Only three pairs were ever observed. An unknown one is passed through as
    the byte that followed rather than guessed at or dropped."""
    assert a2s.unescape(b"\x01\x09") == b"\x09"


# ---------------------------------------------------------------- the rules


def test_rules_split_into_keys_and_values_and_report_the_declared_count():
    body = rules_body([(b"a", b"1"), (b"bb", b"22")])
    declared, pairs, truncated = a2s.split_rules(body)
    assert declared == 2
    assert pairs == [(b"a", b"1"), (b"bb", b"22")]
    assert truncated is False


def test_a_rules_body_cut_short_says_so_instead_of_inventing_a_pair():
    body = rules_body([(b"a", b"1")])[:-3]
    declared, pairs, truncated = a2s.split_rules(body)
    assert truncated is True
    assert len(pairs) < declared or not pairs


def test_chunks_are_joined_by_index_not_by_arrival_order():
    """The key is two raw bytes, `(index, total)`, and it is 1-based. A decoder
    that concatenated the pairs in the order they arrived would still pass on a
    server that happens to send them in order."""
    blob = mod_blob([(101, "one"), (202, "two")])
    pairs = chunk_rules(blob, size=8)
    shuffled = list(reversed(pairs))
    joined = a2s.reassemble(shuffled)
    assert joined.blob == blob
    assert joined.total == len(pairs)
    assert joined.seen == len(pairs)
    assert joined.missing == ()


def test_a_missing_chunk_is_named_rather_than_silently_concatenated():
    """`total` is in every key precisely so a gap can be seen. Joining what
    arrived and calling it the answer would produce a blob that decodes into
    plausible nonsense -- the worst possible outcome for a list the caller is
    about to trust."""
    blob = mod_blob([(101, "one"), (202, "two"), (303, "three")])
    pairs = chunk_rules(blob, size=8)
    assert len(pairs) > 3
    without = [p for p in pairs if p[0][0] != 2]
    joined = a2s.reassemble(without)
    assert joined.missing == (2,)
    assert joined.seen == len(pairs) - 1
    assert joined.total == len(pairs)


def test_ordinary_rules_are_kept_apart_from_the_chunked_ones():
    """The dictionary carries plain string rules beside the mod chunks, and a
    real rule name must not be swallowed into the blob.

    `zz` is a two-byte key whose bytes DO form a legal `(index, total)` pair, so
    the shape check cannot reject it -- what keeps it out is losing the
    comparison to a complete chunk set. The two tests below cover the other
    half: the shape check on its own, and the comparison on its own.
    """
    blob = mod_blob([(101, "one")])
    pairs = chunk_rules(blob, size=64) + [(b"zz", b"9"), (b"allowed", b"1")]
    joined = a2s.reassemble(pairs)
    assert joined.blob == blob
    assert joined.plain["allowed"] == "1"
    assert joined.plain["zz"] == "9"


def test_two_byte_rule_names_cannot_gang_up_and_outvote_a_real_chunk_set():
    """The shape check, isolated. A chunk key is `(index, total)`, 1-based, so
    its first byte is never zero and never exceeds its second -- and it has to
    be the SHAPE that rejects a rule name, not the size comparison.

    Three rule names ending in the same letter look like three chunks of one
    40-chunk set. Three beats a real set that lost a chunk, and the winner
    decodes into plausible nonsense -- the one outcome worse than a reported
    gap. So the real set here is deliberately INCOMPLETE: nothing but the shape
    check can be what saves it.
    """
    blob = mod_blob([(101, "one"), (202, "two"), (303, "three")])
    pairs = chunk_rules(blob, size=8)
    assert 4 <= len(pairs) < 97, "the fixture must be smaller than the stray group"
    lost = pairs[:2]  # a real set, several chunks short
    # (98, 97), (99, 97), (100, 97): the first byte is above the second.
    strays = [(bytes([n, 97]), b"9") for n in (98, 99, 100)]

    joined = a2s.reassemble(lost + strays)
    assert joined.total == len(pairs)
    assert joined.seen == 2
    assert joined.missing == tuple(range(3, len(pairs) + 1))
    for n in (98, 99, 100):
        assert joined.plain[bytes([n, 97]).decode()] == "9"


def test_a_complete_chunk_set_beats_a_bigger_incomplete_one():
    """The comparison, isolated. Completeness is decided BEFORE size: choosing
    the largest group alone would let three strays claiming to be 3 of 40 beat
    a whole two-chunk list. Size only breaks ties between incomplete groups,
    where the largest is at least the most that arrived."""
    blob = mod_blob([(101, "one")])
    escaped = escape(blob)
    half = len(escaped) // 2 + 1
    real = [(bytes([1, 2]), escaped[:half]), (bytes([2, 2]), escaped[half:])]
    strays = [(bytes([n, 40]), b"x") for n in (1, 2, 3)]

    joined = a2s.reassemble(real + strays)
    assert joined.blob == blob
    assert joined.total == 2
    assert joined.missing == ()


def test_the_escapes_are_undone_after_joining_not_per_chunk():
    """THE case that separates a correct decoder from a lucky one. An escape
    pair can straddle a chunk boundary: un-escaping each chunk on its own sees
    a dangling 0x01 at the end of one and an orphaned 0x01/0x02 at the start of
    the next, and both halves decode wrong. Measured chunk size is 127 bytes,
    so this is not a rare alignment -- it is one in 127.
    """
    blob = bytes([0x00, 0xFF, 0x01]) * 40
    escaped = escape(blob)
    # Cut so that the boundary lands between an escape byte and its partner.
    pairs = [(bytes([1, 2]), escaped[:5]), (bytes([2, 2]), escaped[5:])]
    assert escaped[4] == 0x01, "the fixture must actually split an escape pair"
    assert a2s.reassemble(pairs).blob == blob


# ----------------------------------------------------------- the mod blob


def test_the_mod_records_decode_to_workshop_id_and_name():
    blob = mod_blob([(101, "one"), (202, "a name with spaces")], signatures=["k.bikey"])
    answer = a2s.decode_mods(blob)
    assert [(m.workshop_id, m.name) for m in answer.mods] == [
        (101, "one"), (202, "a name with spaces"),
    ]
    assert answer.declared == 2
    assert answer.signatures == ("k.bikey",)
    assert answer.leftover == 0


def test_the_workshop_id_length_is_the_low_nibble_only():
    """Measured on live data: the byte before the id carries its byte length in
    the low nibble and something else in the high one. Reading the whole byte
    asks for hundreds of bytes and derails every record after it."""
    blob = mod_blob([(4294967295, "four bytes"), (7, "one byte")])
    answer = a2s.decode_mods(blob)
    assert [m.workshop_id for m in answer.mods] == [4294967295, 7]


def test_a_non_ascii_mod_name_survives_the_decode():
    blob = mod_blob([(101, "Зона — mod")])
    assert a2s.decode_mods(blob).mods[0].name == "Зона — mod"


def test_a_truncated_blob_reports_how_far_it_got_instead_of_raising():
    """A short read must degrade into a partial, self-describing answer: the
    caller can see that the count and the records disagree. Raising would throw
    away the records that did decode, and returning them without the disagreement
    would be the silent lie."""
    blob = mod_blob([(101, "one"), (202, "two"), (303, "three")])[:-6]
    answer = a2s.decode_mods(blob)
    assert answer.declared == 3
    assert len(answer.mods) < 3
    assert answer.complete is False
    assert answer.problem


def test_a_blob_whose_count_overshoots_its_records_runs_out_and_says_so():
    """A count larger than the records is read as a short buffer, because that
    is what it is: the decoder asks for a fifth record and the blob ends."""
    blob = mod_blob([(101, "one")], count=4)
    answer = a2s.decode_mods(blob)
    assert answer.declared == 4
    assert len(answer.mods) == 1
    assert answer.complete is False
    assert answer.problem


def test_a_declared_count_that_disagrees_with_the_records_is_not_complete():
    """Measured across every live server sampled: the declared count always
    equalled the parsed count. That makes a disagreement evidence of a decoding
    fault, and it must be visible rather than assumed away.

    Asserted on `ModAnswer` directly rather than through `decode_mods`. Inside
    the decoder the two can only disagree by way of a short read, which has
    already set `problem` -- so a test that went in through the decoder would
    pass with this rule deleted, which is exactly what it did. The rule still
    has work to do out here: `query_mods` rebuilds the answer with `replace`,
    and the tools hold and re-check one.
    """
    one = (a2s.ServerMod(101, "one"),)
    assert a2s.ModAnswer(mods=one, declared=4).complete is False
    assert a2s.ModAnswer(mods=one, declared=1).complete is True


def test_an_empty_blob_is_an_answer_not_a_crash():
    answer = a2s.decode_mods(b"")
    assert answer.mods == ()
    assert answer.complete is False
    assert answer.problem


# ------------------------------------------------------------- the transport


def test_the_rules_query_answers_the_challenge_before_it_gets_the_list(serve):
    """A2S_RULES needs a handshake: the first send gets header 'A' and four
    challenge bytes, and only the resend carrying them gets header 'E'."""
    blob = mod_blob([(101, "one"), (202, "two")])
    server = serve(rules_script(chunk_rules(blob)))
    host, port = server.address
    answer = a2s.query_mods(host, port, timeout=3.0)
    assert [m.workshop_id for m in answer.mods] == [101, 202]
    assert len(server.requests) == 2
    assert server.requests[0].endswith(WHOLE)
    assert server.requests[1][5:] == b"\x11\x22\x33\x44"


def test_a_host_that_rotates_the_challenge_forever_is_named_and_stopped(serve):
    """The one failure mode worth having a name for: some hosts answer INFO and
    keep issuing a fresh challenge for RULES, an anti-amplification filter.
    Retrying is what a naive client does until the agent gives up on it."""

    def script(request, nth):
        return WHOLE + b"A" + bytes([nth, 0, 0, 0])

    server = serve(script)
    host, port = server.address
    with pytest.raises(a2s.ChallengeRotation) as caught:
        a2s.query_mods(host, port, timeout=2.0, rounds=3)
    assert "challenge" in str(caught.value).lower()
    assert len(server.requests) <= 4


def test_silence_becomes_a_timeout_not_a_hang(serve):
    server = serve(lambda request, nth: None)
    host, port = server.address
    with pytest.raises(a2s.A2STimeout):
        a2s.query_mods(host, port, timeout=0.4, rounds=2)


def test_the_info_query_needs_no_challenge_and_carries_the_game_port(serve):
    """Measured: INFO answers on the first send. The game port comes back in
    the extra-data field, which is how a caller can tell that the port it was
    given is the QUERY port and what the game port beside it is."""

    def script(request, nth):
        return WHOLE + b"I" + info_body(name="a stand", players=7, game_port=2302)

    server = serve(script)
    host, port = server.address
    info = a2s.query_info(host, port, timeout=3.0)
    assert info.name == "a stand"
    assert info.players == 7
    assert info.game_port == 2302
    assert len(server.requests) == 1


def test_an_info_challenge_is_answered_when_a_host_asks_for_one(serve):
    """Not every host skips it, and the protocol allows either."""

    def script(request, nth):
        if nth == 1:
            return WHOLE + b"A" + b"\x01\x02\x03\x04"
        return WHOLE + b"I" + info_body(name="challenged")

    server = serve(script)
    host, port = server.address
    assert a2s.query_info(*server.address, timeout=3.0).name == "challenged"
    assert len(server.requests) == 2


def test_a_split_reply_is_reassembled_in_order(serve):
    """No DayZ rules reply observed so far was split at the Source level, but
    the protocol allows it and a client that ignored it would read half a
    packet as the whole answer."""
    blob = mod_blob([(101, "one"), (202, "two"), (303, "three")])
    whole = WHOLE + b"E" + rules_body(chunk_rules(blob))
    half = len(whole) // 2
    header = struct.pack("<ibbh", 7, 2, 0, 1400)

    def script(request, nth):
        if request[5:] == WHOLE:
            return WHOLE + b"A" + b"\x11\x22\x33\x44"
        return [
            a2s.SPLIT + header + whole[:half],
            a2s.SPLIT + struct.pack("<ibbh", 7, 2, 1, 1400) + whole[half:],
        ]

    server = serve(script)
    answer = a2s.query_mods(*server.address, timeout=3.0)
    assert [m.workshop_id for m in answer.mods] == [101, 202, 303]


def test_a_compressed_split_reply_is_refused_by_name(serve):
    """bzip2 was never observed from DayZ. Guessing at the compressed split
    header would be an untested path pretending to be a feature; a named
    refusal tells the caller exactly what was not decoded."""
    header = struct.pack("<ibbh", -1, 1, 0, 1400) + struct.pack("<II", 10, 0)

    def script(request, nth):
        if request[5:] == WHOLE:
            return WHOLE + b"A" + b"\x11\x22\x33\x44"
        return a2s.SPLIT + header + b"junk"

    server = serve(script)
    with pytest.raises(a2s.A2SProtocolError) as caught:
        a2s.query_mods(*server.address, timeout=2.0)
    assert "compress" in str(caught.value).lower()


def test_the_answer_carries_the_transport_facts_it_was_built_from(serve):
    """Chunk counts and byte totals are what a caller checks a suspicious
    answer against -- and what the acceptance run recorded against live
    servers."""
    blob = mod_blob([(101, "one"), (202, "two")])
    server = serve(rules_script(chunk_rules(blob)))
    answer = a2s.query_mods(*server.address, timeout=3.0)
    assert answer.chunk_total == answer.chunks_seen > 0
    assert answer.blob_bytes == len(blob)
    assert answer.complete is True
