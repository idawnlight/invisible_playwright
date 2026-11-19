"""What the page-bytes e2e needs, apart from the browser: the probe's values, the PNG comparison on decoded pixels, and the
comparison with a reference committed per operating system (tests/data/page_bytes_<os>.json).

The probe (tests/page_bytes_probe.js) resolves to a flat object {key: value} where a value is
  {"s": "a short string"}                       a string of at most 3000 characters, or
  {"h": ..., "n": ..., "s0": ...}               a longer string: the hash of its bytes (cyrb53), its length, its first 200 characters, or
  {"h": ..., "n": ..., "w": ..., "b64": ...}    bytes: hash, length, pixels per row, the bytes themselves when at most 400 KiB.
A PNG that the page reads (toDataURL, toBlob, convertToBlob) also carries `b64`: the bytes of the file.

Two kinds of key in the reference:
  "keys"  stable values, equal in every launch: compared as they are (the string, or the hash and the length);
  "pngs"  the keys that are PNG files. A PNG the engine writes holds a 16-byte ancillary chunk, `deBG`, that differs in every
          launch (measured on the Windows runner, run 43652292533: the same arm launched twice differs in that chunk and
          nowhere else, the length is the same). So its bytes are not compared: its length, and the pixels it decodes to
          (width, height, colour type, and the sha256 of the unfiltered scanlines).

No value here is made up: an entry the reference does not hold is a problem (the test is red), and the problem carries the
entry the run measured, in the form the reference file takes, so that a run on the engine of the day writes it down.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DATA_PREFIX = "data:image/png;base64,"
#: the end of a key that is a PNG file (the jpeg and webp encodings are stable and are compared as they are)
PNG_KEY_ENDS = (".png", ".toBlob", ".convertToBlob", ".toDataURL")
DATA_DIR = Path(__file__).resolve().parent / "data"


# ------------------------------------------------------------------------------------------------------------------ PNG

class NotAPng(ValueError):
    pass


def png_chunks(png: bytes) -> List[Tuple[bytes, bytes]]:
    if not png.startswith(PNG_SIGNATURE):
        raise NotAPng("no PNG signature")
    out, i = [], len(PNG_SIGNATURE)
    while i + 12 <= len(png):
        size = struct.unpack(">I", png[i:i + 4])[0]
        tag, data = png[i + 4:i + 8], png[i + 8:i + 8 + size]
        if len(data) != size:
            raise NotAPng("a chunk is cut")
        out.append((tag, data))
        i += 12 + size
    return out


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def unfiltered(png: bytes) -> Dict[str, Any]:
    """{w, h, ct, channels, data} of a non-interlaced 8-bit PNG (grey, RGB, grey+alpha, RGBA): `data` is the scanlines with their filters
    undone, `w * channels` bytes a row and `h` rows."""
    chunks = png_chunks(png)
    head = [d for t, d in chunks if t == b"IHDR"]
    if not head:
        raise NotAPng("no IHDR")
    w, h, depth, ct, _comp, _filt, interlace = struct.unpack(">IIBBBBB", head[0])
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(ct)
    if depth != 8 or interlace != 0 or channels is None:
        raise NotAPng("only 8-bit, non-interlaced, non-palette PNG is decoded here (bit depth %d, colour type %d, interlace %d)"
                      % (depth, ct, interlace))
    raw = zlib.decompress(b"".join(d for t, d in chunks if t == b"IDAT"))
    stride = w * channels
    if len(raw) != h * (stride + 1):
        raise NotAPng("the scanlines are %d bytes, %d expected" % (len(raw), h * (stride + 1)))
    out = bytearray()
    prev = bytearray(stride)
    pos = 0
    for _ in range(h):
        ftype = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 255
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ftype == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                upleft = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 255
        elif ftype != 0:
            raise NotAPng("filter type %d" % ftype)
        out += line
        prev = line
    return {"w": w, "h": h, "ct": ct, "channels": channels, "data": bytes(out)}


def decode_png(png: bytes) -> Dict[str, Any]:
    """{w, h, ct, sha256} of a non-interlaced 8-bit PNG (grey, RGB, grey+alpha, RGBA): the sha256 is over the unfiltered
    scanlines, so two files with the same pixels and different ancillary chunks or compression give the same answer."""
    u = unfiltered(png)
    return {"w": u["w"], "h": u["h"], "ct": u["ct"], "sha256": hashlib.sha256(u["data"]).hexdigest()}


def crop(image: Dict[str, Any], x: int, y: int, w: int, h: int) -> Dict[str, Any]:
    """The box (x, y, w, h) of an `unfiltered` image, as an image of its own (same colour type). A box that is not inside it is an error:
    a crop that silently ends at the edge would compare less than it was asked to."""
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > image["w"] or y + h > image["h"]:
        raise ValueError("the box %r is not inside the %dx%d image" % ((x, y, w, h), image["w"], image["h"]))
    ch, stride = image["channels"], image["w"] * image["channels"]
    rows = [image["data"][(y + r) * stride + x * ch:(y + r) * stride + (x + w) * ch] for r in range(h)]
    return {"w": w, "h": h, "ct": image["ct"], "channels": ch, "data": b"".join(rows)}


def encode_png(image: Dict[str, Any]) -> bytes:
    """The PNG file of an `unfiltered`-shaped image (no filter, one IDAT): what a crop is kept as."""
    w, h, ch = image["w"], image["h"], image["channels"]
    stride = w * ch
    raw = b"".join(b"\x00" + image["data"][r * stride:(r + 1) * stride] for r in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, image["ct"], 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def pixels_differ(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """How two images of the same size and colour type differ: {pixels, first: [x, y] | None, box: [x0, y0, x1, y1] | None}; a pixel
    differs when any of its channels does. Different sizes or colour types are an error, not a difference."""
    if (a["w"], a["h"], a["ct"]) != (b["w"], b["h"], b["ct"]):
        raise ValueError("%dx%d (colour type %d) against %dx%d (colour type %d)" % (a["w"], a["h"], a["ct"], b["w"], b["h"], b["ct"]))
    ch, w = a["channels"], a["w"]
    count, first, x0, y0, x1, y1 = 0, None, w, a["h"], -1, -1
    da, db = a["data"], b["data"]
    for i in range(0, len(da), ch):
        if da[i:i + ch] != db[i:i + ch]:
            x, y = (i // ch) % w, (i // ch) // w
            count += 1
            first = first or [x, y]
            x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
    return {"pixels": count, "first": first, "box": [x0, y0, x1, y1] if count else None}


def coloured_pixels(image: Dict[str, Any]) -> int:
    """How many pixels of an RGB or RGBA image have a red, green and blue that are not equal: black text on white that is drawn with grey
    antialiasing has none, and one drawn with subpixel (LCD) antialiasing has them at its edges."""
    ch = image["channels"]
    if ch < 3:
        return 0
    d = image["data"]
    return sum(1 for i in range(0, len(d), ch) if not d[i] == d[i + 1] == d[i + 2])


def png_bytes_of(value: Dict[str, Any]) -> Optional[bytes]:
    """The PNG file a probe value holds, or None when the probe did not keep its bytes."""
    if "b64" in value:
        return base64.b64decode(value["b64"])
    text = value.get("s")
    if isinstance(text, str) and text.startswith(DATA_PREFIX):
        return base64.b64decode(text[len(DATA_PREFIX):])
    return None


def is_png_key(key: str) -> bool:
    return key.endswith(PNG_KEY_ENDS)


# --------------------------------------------------------------------------------------------------------- the reference

def ident(value: Dict[str, Any]) -> Dict[str, Any]:
    """What identifies a stable value: its string, or the hash and the length of its bytes."""
    if "s" in value:
        return {"s": value["s"]}
    return {"h": value.get("h"), "n": value.get("n")}


def png_entry(value: Dict[str, Any]) -> Dict[str, Any]:
    """The entry of a PNG key: its length and, when the bytes were kept, the pixels they decode to."""
    # n: the length of the string for a data URL, of the file for bytes
    entry: Dict[str, Any] = {"n": value["n"] if "n" in value else len(value.get("s", ""))}
    data = png_bytes_of(value)
    if data is None:
        return entry
    try:
        entry.update(decode_png(data))
    except (NotAPng, zlib.error) as exc:
        entry["undecodable"] = str(exc)
    return entry


def entry_of(key: str, value: Dict[str, Any]) -> Dict[str, Any]:
    return png_entry(value) if is_png_key(key) else ident(value)


def reference_path(os_name: str) -> Path:
    return DATA_DIR / ("page_bytes_%s.json" % os_name)


def load_reference(os_name: str) -> Dict[str, Any]:
    """The reference of an operating system; an empty one when the file does not exist yet (every key is then a problem
    that carries its own entry, which is how the first reference is written)."""
    path = reference_path(os_name)
    if not path.is_file():
        return {"keys": {}, "pngs": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def compare(reference: Dict[str, Any], measured: Dict[str, Dict[str, Any]]) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """(problems, record). `problems` is empty when every key the probe returned is in the reference with the same value
    and the reference holds no key the probe did not return. `record` is the entry the run measured for every key that is
    missing from the reference or incomplete in it (a PNG with no decoded pixels), in the form the reference file takes."""
    problems: List[str] = []
    record: Dict[str, Dict[str, Any]] = {}
    stable, pngs = reference.get("keys", {}), reference.get("pngs", {})
    for key in sorted(measured):
        mine = entry_of(key, measured[key])
        if is_png_key(key):
            if "undecodable" in mine:
                problems.append("%s: the PNG the page read does not decode (%s)" % (key, mine["undecodable"]))
                continue
            if "sha256" not in mine:
                problems.append("%s: the probe did not keep the bytes of this PNG" % key)
                continue
            ref = pngs.get(key)
            if ref is None:
                problems.append("%s: not in the reference" % key)
                record[key] = mine
            elif ref.get("n") != mine.get("n"):
                problems.append("%s: the PNG is %s bytes, the reference says %s" % (key, mine.get("n"), ref.get("n")))
            elif "sha256" not in ref:
                problems.append("%s: the reference has the length of this PNG and not yet its decoded pixels" % key)
                record[key] = mine
            elif (ref["w"], ref["h"], ref["ct"], ref["sha256"]) != (mine["w"], mine["h"], mine["ct"], mine["sha256"]):
                problems.append("%s: the decoded pixels differ (%sx%s ct %s %s, the reference %sx%s ct %s %s)"
                                % (key, mine["w"], mine["h"], mine["ct"], mine["sha256"][:12],
                                   ref["w"], ref["h"], ref["ct"], ref["sha256"][:12]))
        else:
            ref = stable.get(key)
            if ref is None:
                problems.append("%s: not in the reference" % key)
                record[key] = mine
            elif ref != mine:
                problems.append("%s: %s, the reference %s" % (key, _short(mine), _short(ref)))
    for key in sorted((set(stable) | set(pngs)) - set(measured)):
        problems.append("%s: in the reference and not read by the probe" % key)
    return problems, record


def _short(entry: Dict[str, Any], limit: int = 120) -> str:
    return json.dumps(entry, sort_keys=True)[:limit]


def message(os_name: str, problems: List[str], record: Dict[str, Dict[str, Any]]) -> str:
    """The failure text: the problems, and the entries to add to tests/data/page_bytes_<os>.json."""
    text = "%d problem(s) in the bytes a page reads on %s against %s:\n  %s" % (
        len(problems), os_name, reference_path(os_name).name, "\n  ".join(problems[:60]))
    if record:
        keys = {k: v for k, v in record.items() if not is_png_key(k)}
        pngs = {k: v for k, v in record.items() if is_png_key(k)}
        text += ("\nTO RECORD (merge into tests/data/page_bytes_%s.json, from a run on the engine whose bytes are the reference):\n"
                 "RECORD-BEGIN %s\n%s\nRECORD-END" % (os_name, os_name, json.dumps({"keys": keys, "pngs": pngs}, sort_keys=True)))
    return text
