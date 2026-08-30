"""QR encoder tests.

A QR encoder is unusual to test, because "looks like a QR code" is not a
property — a symbol with one wrong module is still a picture of a QR code
and simply will not scan. So the load-bearing tests here are the golden
matrices: three payloads whose encoded matrices were compared module by
module against an independent, widely-deployed encoder (Apple CoreImage's
`CIQRCodeGenerator` at error-correction level M) and found *identical*, and
whose rendered symbols were decoded back to the original bytes by `zbar`.

Neither of those tools is a dependency of this repository or of this
suite — the comparison was done once, out of band, and what is committed
here is its result: a fingerprint per payload. If a refactor moves a
single module, these fail.

The remaining tests cover the structure the standard fixes (finder
patterns, timing, the dark module), version selection at its boundaries,
and the rendering the CLI prints.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from caret_backend import qrcode  # noqa: E402


def fingerprint(matrix: list[list[int]]) -> str:
    return hashlib.sha256(
        "".join("".join(str(m) for m in row) for row in matrix).encode()
    ).hexdigest()


# payload -> (module count, sha256 of the matrix)
# Each verified byte-identical to CoreImage's encoder at level M.
GOLDEN = {
    "x": (21, "38232e7e263ef0ca3aae3d0041b2789c00f75ae8e9abe4213527fc1c9d9d7eca"),
    "https://docs.typewithcaret.com/connect/": (
        29,
        "ba0ec86c3793d539c025ea9c981c78facec93d0cfef45528c17fa6a739bc8cfc",
    ),
    "caret://pair?v=1&u=https%3A%2F%2Fcaret.example.ts.net&t=AAAA": (
        33,
        "002fc9fe1de9fed07db58b479171a8a920b5f4abd41289008940565dc5c0599d",
    ),
    # The connect payload the Caret app actually scans — the worked example
    # from the client contract, with its fake key.
    (
        "caret-connect:v1:eyJrIjoiY2FyZXRfZGVtb19rZXlfMTIzNDU2Nzg5MCIsInQiOiJhZ2Vu"
        "dCIsInUiOiJodHRwczovL2FnZW50LmV4YW1wbGUuY29tIn0"
    ): (
        45,
        "695b3dfc78f8761e6bea56aa850e96062e3cfb2497debc200293cecd237ecd2a",
    ),
}


class GoldenTests(unittest.TestCase):
    def test_known_payloads_encode_to_the_verified_matrices(self):
        for payload, (size, digest) in GOLDEN.items():
            with self.subTest(payload=payload[:32]):
                matrix = qrcode.encode(payload)
                self.assertEqual(len(matrix), size)
                self.assertEqual(fingerprint(matrix), digest)

    def test_encoding_is_deterministic(self):
        # Mask selection is a scored search; a tie broken by iteration order
        # would make the output depend on dict ordering. It must not.
        payload = "caret://pair?v=1&u=https%3A%2F%2Fa.example&t=" + "b" * 32
        self.assertEqual(qrcode.encode(payload), qrcode.encode(payload))


class StructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = qrcode.encode("caret://pair?v=1&u=https%3A%2F%2Fa.example&t=zz")
        self.size = len(self.matrix)

    def test_the_matrix_is_square_and_a_legal_version_size(self):
        for row in self.matrix:
            self.assertEqual(len(row), self.size)
        # versions 1..14 -> 21, 25, ... 73
        self.assertIn(self.size, range(21, 74, 4))

    def test_every_module_is_zero_or_one(self):
        self.assertEqual({m for row in self.matrix for m in row}, {0, 1})

    def test_the_three_finder_patterns_are_present(self):
        finder = [
            [1, 1, 1, 1, 1, 1, 1],
            [1, 0, 0, 0, 0, 0, 1],
            [1, 0, 1, 1, 1, 0, 1],
            [1, 0, 1, 1, 1, 0, 1],
            [1, 0, 1, 1, 1, 0, 1],
            [1, 0, 0, 0, 0, 0, 1],
            [1, 1, 1, 1, 1, 1, 1],
        ]
        corners = [(0, 0), (0, self.size - 7), (self.size - 7, 0)]
        for top, left in corners:
            with self.subTest(corner=(top, left)):
                block = [row[left : left + 7] for row in self.matrix[top : top + 7]]
                self.assertEqual(block, finder)

    def test_the_bottom_right_corner_has_no_finder(self):
        block = self.matrix[self.size - 7][self.size - 7 : self.size]
        self.assertNotEqual(block, [1] * 7)

    def test_the_timing_patterns_alternate(self):
        for i in range(8, self.size - 8):
            expected = 1 if i % 2 == 0 else 0
            self.assertEqual(self.matrix[6][i], expected, f"row timing at {i}")
            self.assertEqual(self.matrix[i][6], expected, f"col timing at {i}")

    def test_the_dark_module_is_dark(self):
        # Fixed by the standard at (4*version + 9, 8); always 1.
        self.assertEqual(self.matrix[self.size - 8][8], 1)

    def test_the_separators_around_the_finders_are_light(self):
        for i in range(8):
            self.assertEqual(self.matrix[7][i], 0)
            self.assertEqual(self.matrix[i][7], 0)


class VersionTests(unittest.TestCase):
    def test_the_symbol_grows_only_when_it_has_to(self):
        sizes = [len(qrcode.encode("a" * n)) for n in (1, 14, 15, 26, 27)]
        # v1 holds 14 bytes at level M, v2 holds 26.
        self.assertEqual(sizes, [21, 21, 25, 25, 29])

    def test_the_largest_supported_payload_encodes(self):
        matrix = qrcode.encode("a" * 362)
        self.assertEqual(len(matrix), 73)  # version 14

    def test_an_oversized_payload_is_a_clear_error_not_a_broken_symbol(self):
        with self.assertRaises(ValueError) as caught:
            qrcode.encode("a" * 363)
        self.assertIn("362", str(caught.exception))

    def test_an_empty_payload_is_refused(self):
        with self.assertRaises(ValueError):
            qrcode.encode("")

    def test_non_ascii_is_encoded_as_utf8_bytes(self):
        # Byte mode, so this is a length question, not an encoding question.
        self.assertEqual(len(qrcode.encode("é" * 7)), 21)  # 14 bytes -> v1


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = qrcode.encode("caret://pair?v=1&u=https%3A%2F%2Fa.example&t=z")

    def test_a_quiet_zone_is_rendered_or_no_scanner_will_find_it(self):
        rendered = qrcode.render_text(self.matrix, plain=True)
        lines = rendered.split("\n")
        # Half-block glyphs pack two module rows per line.
        self.assertGreaterEqual(len(lines), (len(self.matrix) + 2 * qrcode.QUIET_ZONE) // 2)
        self.assertEqual(qrcode.QUIET_ZONE, 4)
        self.assertEqual(lines[0].strip(), "")
        self.assertEqual(lines[-1].strip(), "")

    def test_plain_mode_emits_no_escape_sequences(self):
        self.assertNotIn("\033", qrcode.render_text(self.matrix, plain=True))

    def test_colour_mode_sets_and_resets_its_colours_on_every_line(self):
        # A line that sets a background and never resets it bleeds into the
        # rest of the terminal.
        for line in qrcode.render_text(self.matrix).split("\n"):
            self.assertTrue(line.startswith("\033["), line[:12])
            self.assertTrue(line.endswith("\033[0m"), line[-12:])

    def test_the_png_is_a_valid_greyscale_png_of_the_right_size(self):
        import struct

        png = qrcode.render_png(self.matrix, scale=4)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(png.endswith(b"IEND\xae\x42\x60\x82"))
        width, height, depth, colour = struct.unpack(">IIBB", png[16:26])
        expected = (len(self.matrix) + 2 * qrcode.QUIET_ZONE) * 4
        self.assertEqual((width, height), (expected, expected))
        self.assertEqual((depth, colour), (8, 0))  # 8-bit greyscale

    def test_the_png_pixels_match_the_matrix(self):
        import zlib

        scale, quiet = 3, qrcode.QUIET_ZONE
        png = qrcode.render_png(self.matrix, scale=scale)
        start = png.index(b"IDAT") + 4
        length = int.from_bytes(png[start - 8 : start - 4], "big")
        raw = zlib.decompress(png[start : start + length])
        size = len(self.matrix)
        stride = (size + 2 * quiet) * scale + 1  # +1 filter byte per row
        for my, mx in ((0, 0), (0, size - 1), (size - 1, 0), (6, 6)):
            y = (my + quiet) * scale + 1
            x = (mx + quiet) * scale + 1
            self.assertEqual(
                raw[y * stride + x] == 0,
                bool(self.matrix[my][mx]),
                f"module ({my},{mx})",
            )
        # The quiet zone must be white or no scanner locks on.
        self.assertEqual(raw[1], 255)

    def test_a_nonsense_scale_is_refused(self):
        with self.assertRaises(ValueError):
            qrcode.render_png(self.matrix, scale=0)

    def test_rendering_does_not_mutate_the_matrix(self):
        before = [row[:] for row in self.matrix]
        qrcode.render_text(self.matrix)
        qrcode.render_text(self.matrix, plain=True)
        qrcode.render_png(self.matrix)
        self.assertEqual(self.matrix, before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
