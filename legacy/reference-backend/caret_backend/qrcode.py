"""A minimal QR Code encoder — standard library only.

Caret's setup flow shows a QR the phone scans, and this backend has one
hard rule about dependencies: there are none. Every other option for
producing a QR meant adding one — a pip package, or shelling out to
`qrencode`, which is not installed on a stock Mac or a stock VPS. Neither
is acceptable for a reference backend whose whole promise is "download it
and run it with the Python that is already there", so the encoder is here,
in about four hundred lines of stdlib.

Scope is deliberately narrow — exactly what a connection code needs and
nothing more:

  * byte mode only (a `caret-connect:v1:` payload is ASCII)
  * error-correction level M (~15% recovery, the usual default for
    screen-displayed codes)
  * versions 1 to 14, chosen automatically — up to 362 bytes, where a
    connect payload is around 100

Anything larger raises rather than silently truncating. The output is a
matrix of booleans; `render_text` turns it into something a terminal can
show and a phone camera can read, and `render_png` into a file.

Reference: ISO/IEC 18004. The tables below (block structure, alignment
pattern centres) are from the standard; everything else — the Galois
field, the BCH codes, mask selection — is computed rather than tabulated,
so there is less to get wrong.

Correctness here is not something unit tests can establish on their own —
a symbol with one wrong module is still a picture of a QR code and simply
does not scan. The output was checked module-by-module against an
independent encoder and decoded back with an independent decoder; see
`tests/test_qrcode.py`, which pins the results as golden vectors.
"""

from __future__ import annotations

# ------------------------------------------------------------ GF(256)

_PRIMITIVE = 0x11D  # x^8 + x^4 + x^3 + x^2 + 1, the QR field polynomial

_EXP = [0] * 512
_LOG = [0] * 256


def _build_tables() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= _PRIMITIVE
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_build_tables()


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator_poly(degree: int) -> list[int]:
    """(x - a^0)(x - a^1)…(x - a^(degree-1)), coefficients high-order first."""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _mul(coef, _EXP[i])
        poly = nxt
    return poly


def _ec_codewords(data: list[int], count: int) -> list[int]:
    """Reed-Solomon remainder — the error-correction codewords."""
    gen = _generator_poly(count)
    residue = list(data) + [0] * count
    for i in range(len(data)):
        coef = residue[i]
        if coef:
            for j in range(1, len(gen)):
                residue[i + j] ^= _mul(gen[j], coef)
    return residue[len(data):]


# --------------------------------------------------------- spec tables

# Level-M block structure per version:
#   (ec codewords per block, blocks in group 1, data codewords in a group-1
#    block, blocks in group 2, data codewords in a group-2 block)
_BLOCKS_M = {
    1: (10, 1, 16, 0, 0),
    2: (16, 1, 28, 0, 0),
    3: (26, 1, 44, 0, 0),
    4: (18, 2, 32, 0, 0),
    5: (24, 2, 43, 0, 0),
    6: (16, 4, 27, 0, 0),
    7: (18, 4, 31, 0, 0),
    8: (22, 2, 38, 2, 39),
    9: (22, 3, 36, 2, 37),
    10: (26, 4, 43, 1, 44),
    11: (30, 1, 50, 4, 51),
    12: (22, 6, 36, 2, 37),
    13: (22, 8, 37, 1, 38),
    14: (24, 4, 40, 5, 41),
}

# Alignment-pattern centre coordinates per version (version 1 has none).
_ALIGNMENT = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
    11: (6, 30, 54),
    12: (6, 32, 58),
    13: (6, 34, 62),
    14: (6, 26, 46, 66),
}

_EC_LEVEL_M_BITS = 0b00  # level M, as encoded in the format information
_MAX_VERSION = 14


def _capacity_bytes(version: int) -> int:
    ec_cw, g1, g1_cw, g2, g2_cw = _BLOCKS_M[version]
    data_cw = g1 * g1_cw + g2 * g2_cw
    header_bits = 4 + (8 if version <= 9 else 16)
    return (data_cw * 8 - header_bits) // 8


def _pick_version(length: int) -> int:
    for version in range(1, _MAX_VERSION + 1):
        if length <= _capacity_bytes(version):
            return version
    raise ValueError(
        f"payload of {length} bytes exceeds this encoder's limit of "
        f"{_capacity_bytes(_MAX_VERSION)} bytes (QR version {_MAX_VERSION}, "
        "level M) — keep pairing payloads short"
    )


# ------------------------------------------------------------- encoding


def _bitstream(data: bytes, version: int) -> list[int]:
    ec_cw, g1, g1_cw, g2, g2_cw = _BLOCKS_M[version]
    data_cw = g1 * g1_cw + g2 * g2_cw
    bits: list[int] = []

    def push(value: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            bits.append((value >> shift) & 1)

    push(0b0100, 4)  # byte mode
    push(len(data), 8 if version <= 9 else 16)
    for byte in data:
        push(byte, 8)

    capacity_bits = data_cw * 8
    push(0, min(4, capacity_bits - len(bits)))  # terminator
    while len(bits) % 8:
        bits.append(0)

    codewords = [
        int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)
    ]
    for pad in _cycle_pads(data_cw - len(codewords)):
        codewords.append(pad)
    return codewords


def _cycle_pads(count: int) -> list[int]:
    # The standard's fixed pad bytes, alternating.
    return [0xEC if i % 2 == 0 else 0x11 for i in range(count)]


def _interleave(codewords: list[int], version: int) -> list[int]:
    ec_cw, g1, g1_cw, g2, g2_cw = _BLOCKS_M[version]
    blocks: list[list[int]] = []
    pos = 0
    for count, size in ((g1, g1_cw), (g2, g2_cw)):
        for _ in range(count):
            blocks.append(codewords[pos:pos + size])
            pos += size
    ec_blocks = [_ec_codewords(block, ec_cw) for block in blocks]

    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        for block in blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ec_cw):
        for block in ec_blocks:
            out.append(block[i])
    return out


# ------------------------------------------------------------ the matrix


def _bch_format(mask: int) -> int:
    data = (_EC_LEVEL_M_BITS << 3) | mask
    value = data << 10
    for i in range(4, -1, -1):
        if value & (1 << (i + 10)):
            value ^= 0x537 << i
    return ((data << 10) | value) ^ 0x5412


def _bch_version(version: int) -> int:
    value = version << 12
    for i in range(5, -1, -1):
        if value & (1 << (i + 12)):
            value ^= 0x1F25 << i
    return (version << 12) | value


_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)


def _blank(size: int) -> list[list[int]]:
    return [[0] * size for _ in range(size)]


def _place_function_patterns(matrix, reserved, version: int) -> None:
    size = len(matrix)

    def finder(top: int, left: int) -> None:
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = top + r, left + c
                if not (0 <= rr < size and 0 <= cc < size):
                    continue
                inside = 0 <= r < 7 and 0 <= c < 7
                dark = inside and (
                    r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4)
                )
                matrix[rr][cc] = 1 if dark else 0
                reserved[rr][cc] = 1

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    # Timing patterns.
    for i in range(size):
        if not reserved[6][i]:
            matrix[6][i] = 1 if i % 2 == 0 else 0
            reserved[6][i] = 1
        if not reserved[i][6]:
            matrix[i][6] = 1 if i % 2 == 0 else 0
            reserved[i][6] = 1

    # Alignment patterns, skipping any that would collide with a finder.
    centres = _ALIGNMENT[version]
    for r in centres:
        for c in centres:
            if (r < 8 and c < 8) or (r < 8 and c > size - 9) or (r > size - 9 and c < 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    dark = max(abs(dr), abs(dc)) != 1
                    matrix[r + dr][c + dc] = 1 if dark else 0
                    reserved[r + dr][c + dc] = 1

    # Format-information area (values written later) and the dark module.
    for i in range(9):
        for rr, cc in ((8, i), (i, 8)):
            reserved[rr][cc] = 1
    for i in range(8):
        reserved[size - 1 - i][8] = 1
        reserved[8][size - 1 - i] = 1
    matrix[size - 8][8] = 1
    reserved[size - 8][8] = 1

    # Version information, versions 7 and up.
    if version >= 7:
        bits = _bch_version(version)
        for i in range(18):
            bit = (bits >> i) & 1
            a, b = divmod(i, 3)
            matrix[size - 11 + b][a] = bit
            reserved[size - 11 + b][a] = 1
            matrix[a][size - 11 + b] = bit
            reserved[a][size - 11 + b] = 1


def _place_data(matrix, reserved, bits: list[int]) -> None:
    size = len(matrix)
    index = 0
    upward = True
    col = size - 1
    while col >= 1:
        if col == 6:  # the vertical timing pattern is skipped entirely
            col = 5
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if not reserved[row][c]:
                    matrix[row][c] = bits[index] if index < len(bits) else 0
                    index += 1
        upward = not upward
        col -= 2


def _apply_mask(matrix, reserved, mask: int) -> list[list[int]]:
    size = len(matrix)
    out = [row[:] for row in matrix]
    rule = _MASKS[mask]
    for r in range(size):
        for c in range(size):
            if not reserved[r][c] and rule(r, c):
                out[r][c] ^= 1
    return out


def _place_format(matrix, mask: int) -> None:
    size = len(matrix)
    bits = _bch_format(mask)
    # Two copies, so a damaged corner still yields a readable format. Bit 0
    # is the least significant; the ladder below is the standard's layout,
    # which is not symmetric — the first copy runs down column 8 and then
    # left along row 8, the second the other way round.
    for i in range(15):
        bit = (bits >> i) & 1
        if i < 6:
            matrix[i][8] = bit
        elif i == 6:
            matrix[7][8] = bit
        elif i == 7:
            matrix[8][8] = bit
        elif i == 8:
            matrix[8][7] = bit
        else:
            matrix[8][14 - i] = bit
        if i < 8:
            matrix[8][size - 1 - i] = bit
        else:
            matrix[size - 15 + i][8] = bit
    matrix[size - 8][8] = 1


def _penalty(matrix) -> int:
    size = len(matrix)
    score = 0

    # Rule 1: runs of five or more identical modules in a row or column.
    for line in list(matrix) + [list(col) for col in zip(*matrix)]:
        run, previous = 1, line[0]
        for value in line[1:]:
            if value == previous:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, previous = 1, value
        if run >= 5:
            score += 3 + (run - 5)

    # Rule 2: 2x2 blocks of one colour.
    for r in range(size - 1):
        for c in range(size - 1):
            block = (matrix[r][c], matrix[r][c + 1], matrix[r + 1][c], matrix[r + 1][c + 1])
            if block[0] == block[1] == block[2] == block[3]:
                score += 3

    # Rule 3: the finder-like 1:1:3:1:1 pattern with a light margin.
    needles = ((1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0), (0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1))
    for line in list(matrix) + [list(col) for col in zip(*matrix)]:
        for i in range(size - 10):
            window = tuple(line[i:i + 11])
            if window in needles:
                score += 40

    # Rule 4: deviation from an even split of dark and light.
    dark = sum(sum(row) for row in matrix)
    total = size * size
    score += 10 * (abs(dark * 100 // total - 50) // 5)
    return score


def encode(payload: str) -> list[list[int]]:
    """Encode `payload` and return the module matrix (1 = dark).

    Raises `ValueError` if the payload is not encodable in byte mode within
    this encoder's version range."""
    data = payload.encode("utf-8")
    if not data:
        # An empty symbol scans to an empty string, which reads as "the
        # scanner failed" to everyone holding the phone. Fail here instead.
        raise ValueError("nothing to encode: the payload is empty")
    version = _pick_version(len(data))
    codewords = _interleave(_bitstream(data, version), version)
    bits = [(byte >> shift) & 1 for byte in codewords for shift in range(7, -1, -1)]

    size = 17 + 4 * version
    base, reserved = _blank(size), _blank(size)
    _place_function_patterns(base, reserved, version)
    _place_data(base, reserved, bits)

    best, best_score = None, None
    for mask in range(8):
        candidate = _apply_mask(base, reserved, mask)
        _place_format(candidate, mask)
        score = _penalty(candidate)
        if best_score is None or score < best_score:
            best, best_score = candidate, score
    return best


# ------------------------------------------------------------ rendering

QUIET_ZONE = 4  # modules of margin the standard requires for a reliable scan


_ANSI_ON = "\033[40;97m"   # black background, bright-white foreground
_ANSI_OFF = "\033[0m"


def render_text(matrix: list[list[int]], *, plain: bool = False) -> str:
    """Render for a terminal.

    Two module rows per text row via half-block characters, because a
    terminal cell is roughly twice as tall as it is wide and a QR has to
    stay square to scan.

    Colour is set explicitly rather than inherited. A QR rendered in the
    terminal's own colours scans on a dark theme and fails on a light one
    (or the reverse), and "it works on my terminal" is exactly the bug a
    pairing flow cannot afford — so each line carries an explicit
    background and foreground and resets at the end. Scanners want dark
    modules on a light field; the inverse here is deliberate and standard
    for half-block rendering, where the *drawn* glyph is the light module.

    `plain=True` drops the escape codes and doubles each module into `##`
    (dark) or two spaces, for piping into a file or a log where ANSI would
    be noise. It assumes a light background, so scan from a light-themed
    viewer."""
    size = len(matrix)
    padded_width = size + 2 * QUIET_ZONE
    rows = (
        [[0] * padded_width for _ in range(QUIET_ZONE)]
        + [[0] * QUIET_ZONE + list(row) + [0] * QUIET_ZONE for row in matrix]
        + [[0] * padded_width for _ in range(QUIET_ZONE)]
    )
    if plain:
        return "\n".join("".join("##" if m else "  " for m in row) for row in rows)

    if len(rows) % 2:
        rows.append([0] * padded_width)
    # Foreground is white and background black, so a *light* module is the
    # drawn half-block and a dark module is bare background.
    glyphs = {(0, 0): "█", (1, 1): " ", (1, 0): "▄", (0, 1): "▀"}
    lines = []
    for i in range(0, len(rows), 2):
        body = "".join(glyphs[(t, b)] for t, b in zip(rows[i], rows[i + 1]))
        lines.append(f"{_ANSI_ON}{body}{_ANSI_OFF}")
    return "\n".join(lines)


def render_png(matrix: list[list[int]], *, scale: int = 8) -> bytes:
    """Render to PNG bytes — stdlib `zlib` and a hand-built file, because
    the no-dependency rule applies here too.

    Greyscale, 8-bit, filter type 0 on every row: the simplest legal PNG,
    and small enough that the simplicity costs nothing. Dark modules are
    black on white with the same quiet zone `render_text` uses, because a
    QR without one does not scan."""
    import struct
    import zlib

    if scale < 1:
        raise ValueError("scale must be at least 1")
    size = len(matrix)
    pixels = (size + 2 * QUIET_ZONE) * scale
    rows = bytearray()
    for y in range(pixels):
        my = y // scale - QUIET_ZONE
        rows.append(0)  # filter type: none
        for x in range(pixels):
            mx = x // scale - QUIET_ZONE
            dark = 0 <= my < size and 0 <= mx < size and matrix[my][mx]
            rows.append(0 if dark else 255)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", pixels, pixels, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


__all__ = ["encode", "render_png", "render_text", "QUIET_ZONE"]
