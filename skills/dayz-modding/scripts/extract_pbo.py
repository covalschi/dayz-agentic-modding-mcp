#!/usr/bin/env python3
"""Extract a Bohemia Interactive PBO archive (DayZ/Arma).

Supports uncompressed entries and LZSS-compressed entries ('Cprs').
Usage: python3 extract_pbo.py <file.pbo> <outdir>
"""
import sys, os, struct, re

def read_asciiz(buf, pos):
    end = buf.index(b"\x00", pos)
    return buf[pos:end].decode("utf-8", errors="replace"), end + 1

_BAD = re.compile(r'[<>:"|?*\x00-\x1f]')

def sanitize(name):
    """Make obfuscated/decoy entry names filesystem-safe (reserved devices, '///', etc.)."""
    parts = []
    for p in name.replace("\\", "/").split("/"):
        p = _BAD.sub("_", p).strip(" .")
        if p.upper().split(".")[0] in ("", "CON", "PRN", "AUX", "NUL") or \
           p.upper().startswith(("COM", "LPT")) and p[3:4].isdigit():
            p = "_j_" + p
        if p in ("", "..", "."):
            p = "_j"
        parts.append(p)
    return "/".join(parts)

def lzss_decompress(data, expected_size):
    """Distance-based LZ77 as used in DayZ/Arma PBOs.

    Back-reference = distance from current output position:
    dist = b1 | ((b2 & 0xF0) << 4)  (dist == 0 means 4096),
    length = (b2 & 0x0F) + 3; positions before start read as 0x20.
    Validated byte-identical against Bohemia's BankRev.exe output.
    (The classic space-filled ring-buffer variant decodes the first bytes
    correctly, then diverges - do not revert to it.)
    """
    out = bytearray()
    i = 0
    n = len(data)
    while len(out) < expected_size and i < n:
        flags = data[i]; i += 1
        for bit in range(8):
            if len(out) >= expected_size or i >= n:
                break
            if flags & (1 << bit):
                out.append(data[i]); i += 1
            else:
                if i + 1 >= n:
                    break
                b1, b2 = data[i], data[i + 1]; i += 2
                dist = b1 | ((b2 & 0xF0) << 4)
                if dist == 0:
                    dist = 4096
                length = (b2 & 0x0F) + 3
                src = len(out) - dist
                for k in range(length):
                    if len(out) >= expected_size:
                        break
                    if src + k < 0:
                        out.append(0x20)  # space-fill before start
                    else:
                        out.append(out[src + k])
    return bytes(out)

def extract(pbo_path, outdir):
    with open(pbo_path, "rb") as f:
        buf = f.read()
    pos = 0
    entries = []
    props = {}
    while True:
        name, pos = read_asciiz(buf, pos)
        packing, orig, _res, _ts, dsize = struct.unpack_from("<IIIII", buf, pos)
        pos += 20
        if packing == 0x56657273:  # 'Vers' product entry -> key/value props
            while True:
                k, pos = read_asciiz(buf, pos)
                if not k:
                    break
                v, pos = read_asciiz(buf, pos)
                props[k] = v
            continue
        if name == "" and packing == 0 and dsize == 0:
            break  # header terminator
        entries.append((name, packing, orig, dsize))
    data_pos = pos
    stats = {"files": 0, "compressed": 0, "bytes": 0}
    for name, packing, orig, dsize in entries:
        raw = buf[data_pos:data_pos + dsize]
        data_pos += dsize
        if packing == 0x43707273:  # 'Cprs'
            raw = lzss_decompress(raw, orig)
            stats["compressed"] += 1
        rel = sanitize(name)
        dest = os.path.join(outdir, rel)
        d = os.path.dirname(dest)
        try:
            if d:
                os.makedirs(d, exist_ok=True)
            if os.path.isdir(dest):
                dest += ".__file"
            with open(dest, "wb") as out:
                out.write(raw)
        except (OSError, NotADirectoryError):
            # decoy path collides with a file/reserved name - flatten it
            dest = os.path.join(outdir, rel.replace("/", "__"))
            with open(dest, "wb") as out:
                out.write(raw)
        stats["files"] += 1
        stats["bytes"] += len(raw)
    return props, stats

if __name__ == "__main__":
    pbo, outdir = sys.argv[1], sys.argv[2]
    props, stats = extract(pbo, outdir)
    print("props:", props)
    print("stats:", stats)
