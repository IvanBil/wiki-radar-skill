"""One-page A4 PDF with an embedded Noto Sans file. Standard library only."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _tables(font: bytes) -> dict[str, tuple[int, int]]:
    num = struct.unpack_from(">H", font, 4)[0]
    tables: dict[str, tuple[int, int]] = {}
    off = 12
    for _ in range(num):
        tag, _checksum, toff, length = struct.unpack_from(">4sIII", font, off)
        tables[tag.decode("latin1")] = (toff, length)
        off += 16
    return tables


def _slice(font: bytes, tables: dict[str, tuple[int, int]], tag: str) -> bytes:
    off, length = tables[tag]
    return font[off : off + length]


def _cmap(font: bytes, tables: dict[str, tuple[int, int]]) -> dict[int, int]:
    data = _slice(font, tables, "cmap")
    num = struct.unpack_from(">H", data, 2)[0]
    chosen: tuple[int, int, bytes] | None = None
    for i in range(num):
        platform, encoding, offset = struct.unpack_from(">HHI", data, 4 + i * 8)
        sub = data[offset:]
        fmt = struct.unpack_from(">H", sub, 0)[0]
        score = 0
        if fmt == 12:
            score = 4
        elif fmt == 4 and platform == 3 and encoding == 1:
            score = 3
        elif fmt == 4 and platform == 0:
            score = 2
        elif fmt == 4:
            score = 1
        if chosen is None or score > chosen[0]:
            chosen = (score, fmt, sub)
    if chosen is None or chosen[0] == 0:
        return {}
    mapping: dict[int, int] = {}
    fmt, sub = chosen[1], chosen[2]
    if fmt == 12:
        groups = struct.unpack_from(">I", sub, 12)[0]
        pos = 16
        for _ in range(groups):
            start, end, glyph = struct.unpack_from(">III", sub, pos)
            for code in range(start, end + 1):
                mapping[code] = glyph + (code - start)
            pos += 12
        return mapping
    seg_count = struct.unpack_from(">H", sub, 6)[0] // 2
    end_at = 14
    ends = list(struct.unpack_from(f">{seg_count}H", sub, end_at))
    start_at = end_at + seg_count * 2 + 2
    starts = list(struct.unpack_from(f">{seg_count}H", sub, start_at))
    delta_at = start_at + seg_count * 2
    deltas = list(struct.unpack_from(f">{seg_count}h", sub, delta_at))
    offset_at = delta_at + seg_count * 2
    offsets = list(struct.unpack_from(f">{seg_count}H", sub, offset_at))
    for i, (start, end) in enumerate(zip(starts, ends)):
        if start == 0xFFFF:
            continue
        for code in range(start, end + 1):
            if offsets[i] == 0:
                mapping[code] = (code + deltas[i]) & 0xFFFF
            else:
                pos = offset_at + i * 2 + offsets[i] + (code - start) * 2
                glyph = struct.unpack_from(">H", sub, pos)[0]
                mapping[code] = ((glyph + deltas[i]) & 0xFFFF) if glyph else 0
    return mapping


def _font_metrics(font: bytes) -> dict:
    tables = _tables(font)
    head = _slice(font, tables, "head")
    hhea = _slice(font, tables, "hhea")
    units = struct.unpack_from(">H", head, 18)[0]
    x_min, y_min, x_max, y_max = struct.unpack_from(">hhhh", head, 36)
    ascender, descender = struct.unpack_from(">hh", hhea, 4)
    n_metrics = struct.unpack_from(">H", hhea, 34)[0]
    hmtx_raw = _slice(font, tables, "hmtx")
    advances = []
    for i in range(n_metrics):
        advances.append(struct.unpack_from(">H", hmtx_raw, i * 4)[0])
    cap = ascender
    if "OS/2" in tables:
        os2 = _slice(font, tables, "OS/2")
        version = struct.unpack_from(">H", os2, 0)[0]
        if version >= 2 and len(os2) >= 90:
            cap = struct.unpack_from(">h", os2, 88)[0]
    return {
        "cmap": _cmap(font, tables),
        "units": units,
        "bbox": (x_min, y_min, x_max, y_max),
        "ascent": ascender,
        "descent": descender,
        "cap": cap,
        "advances": advances,
        "n_metrics": n_metrics,
    }


class Font:
    def __init__(self, path: Path):
        self.data = path.read_bytes()
        self.metrics = _font_metrics(self.data)

    def gid(self, code: int) -> int:
        return self.metrics["cmap"].get(code, 0)

    def advance(self, code: int) -> int:
        advances = self.metrics["advances"]
        gid = self.gid(code)
        if not advances:
            return self.metrics["units"]
        if gid < len(advances):
            return advances[gid]
        return advances[-1]

    def width(self, text: str, size: float) -> float:
        units = self.metrics["units"] or 1000
        return sum(self.advance(ord(ch)) for ch in text) * size / units


def _wrap(font: Font, text: str, size: float, width: float) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else current + " " + word
        if font.width(trial, size) <= width:
            current = trial
            continue
        if current:
            lines.append(current)
        if font.width(word, size) <= width:
            current = word
            continue
        chunk = ""
        for ch in word:
            trial_chunk = chunk + ch
            if chunk and font.width(trial_chunk, size) > width:
                lines.append(chunk)
                chunk = ch
            else:
                chunk = trial_chunk
        current = chunk
    if current:
        lines.append(current)
    return lines


def _pdf_string(text: str) -> str:
    return "".join(f"{ord(ch):04X}" for ch in text)


class Page:
    def __init__(self, regular: Font, bold: Font):
        self.regular = regular
        self.bold = bold
        self.ops: list[str] = []
        self.used: set[int] = set()
        self.y = 812.0

    def gap(self, amount: float) -> None:
        self.y -= amount

    def text(self, value: str, size: float, bold: bool = False, color: tuple[float, float, float] = (0.13, 0.13, 0.13), width: float = 527) -> None:
        font = self.bold if bold else self.regular
        name = "F2" if bold else "F1"
        for line in _wrap(font, value, size, width):
            self.y -= size
            for ch in line:
                self.used.add(ord(ch))
            r, g, b = color
            self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg")
            self.ops.append("BT")
            self.ops.append(f"/{name} {size:.2f} Tf")
            self.ops.append(f"1 0 0 1 34 {self.y:.2f} Tm")
            self.ops.append(f"<{_pdf_string(line)}> Tj")
            self.ops.append("ET")
            self.y -= size * 0.35
        self.y -= 2

    def ascii_line(self, value: str, size: float = 8, color: tuple[float, float, float] = (0.33, 0.33, 0.33)) -> None:
        safe = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        self.y -= size
        r, g, b = color
        self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg")
        self.ops.append("BT")
        self.ops.append(f"/F3 {size:.2f} Tf")
        self.ops.append(f"1 0 0 1 34 {self.y:.2f} Tm")
        self.ops.append(f"({safe}) Tj")
        self.ops.append("ET")
        self.y -= 3

    def rect(self, x: float, y: float, w: float, h: float, fill: tuple[float, float, float]) -> None:
        r, g, b = fill
        self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg")
        self.ops.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")

    def line(self, x1: float, y1: float, x2: float, y2: float, color: tuple[float, float, float], width: float = 1.2) -> None:
        r, g, b = color
        self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} RG")
        self.ops.append(f"{width:.2f} w")
        self.ops.append(f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def polyline(self, points: list[tuple[float, float]], color: tuple[float, float, float]) -> None:
        if len(points) < 2:
            return
        r, g, b = color
        parts = [f"{r:.3f} {g:.3f} {b:.3f} RG", "1.4 w", f"{points[0][0]:.2f} {points[0][1]:.2f} m"]
        parts.extend(f"{x:.2f} {y:.2f} l" for x, y in points[1:])
        parts.append("S")
        self.ops.append(" ".join(parts))


def _widths(font: Font, codes: set[int]) -> str:
    units = font.metrics["units"] or 1000
    items = []
    for code in sorted(codes):
        width = round(font.advance(code) * 1000 / units)
        items.append(f"{code} [{width}]")
    return " ".join(items)


def _cid_map(font: Font, limit: int = 0x052F) -> bytes:
    out = bytearray((limit + 1) * 2)
    for code, gid in font.metrics["cmap"].items():
        if 0 <= code <= limit:
            struct.pack_into(">H", out, code * 2, gid & 0xFFFF)
    return bytes(out)


def _plain_stream(data: bytes) -> bytes:
    return f"<< /Length {len(data)} >>\nstream\n".encode("ascii") + data + b"\nendstream"


def _stream(data: bytes) -> bytes:
    comp = zlib.compress(data)
    return f"<< /Length {len(comp)} /Filter /FlateDecode >>\nstream\n".encode("ascii") + comp + b"\nendstream"


def build_pdf(regular: Font, bold: Font, draw) -> bytes:
    page = Page(regular, bold)
    draw(page)
    content = "\n".join(page.ops).encode("ascii")
    objects: list[bytes] = [b""]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R /F2 11 0 R /F3 17 0 R >> >> >>"
    )
    objects.append(_plain_stream(content))
    # 5-10 regular, 11-16 bold, 17 Helvetica.
    objects.extend(_pack_font(regular, "NotoSans", page.used, 6))
    objects.extend(_pack_font(bold, "NotoSansBold", page.used, 12))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    return _serialize(objects)


def _pack_font(font: Font, name: str, codes: set[int], descriptor_id: int) -> list[bytes]:
    x_min, y_min, x_max, y_max = font.metrics["bbox"]
    gid_id = descriptor_id + 1
    file_id = descriptor_id + 2
    cid_id = descriptor_id + 3
    unicode_id = descriptor_id + 4
    type0 = (
        f"<< /Type /Font /Subtype /Type0 /BaseFont /{name} /Encoding /Identity-H "
        f"/DescendantFonts [{cid_id} 0 R] /ToUnicode {unicode_id} 0 R >>"
    ).encode("ascii")
    descriptor = (
        f"<< /Type /FontDescriptor /FontName /{name} /Flags 32 "
        f"/FontBBox [{x_min} {y_min} {x_max} {y_max}] /ItalicAngle 0 "
        f"/Ascent {font.metrics['ascent']} /Descent {font.metrics['descent']} "
        f"/CapHeight {font.metrics['cap']} /StemV 80 /FontFile2 {file_id} 0 R >>"
    ).encode("ascii")
    gid_map = _stream(_cid_map(font))
    file_obj = _stream(font.data)
    cid = (
        f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{name} "
        f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        f"/FontDescriptor {descriptor_id} 0 R /CIDToGIDMap {gid_id} 0 R /DW 500 /W [{_widths(font, codes)}] >>"
    ).encode("ascii")
    cmap = (
        b"/CIDInit /ProcSet findresource begin\n12 dict begin begincmap\n"
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        b"/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        b"1 begincodespacerange\n<0000> <052F>\nendcodespacerange\n"
        b"1 beginbfrange\n<0000> <052F> <0000>\nendbfrange\n"
        b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
    )
    return [type0, descriptor, gid_map, file_obj, cid, _stream(cmap)]


def _serialize(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects[1:], start=1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n".encode("ascii"))
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects)}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer << /Size {len(objects)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


def write_pdf(path: Path, regular_path: Path, bold_path: Path, draw) -> None:
    regular = Font(regular_path)
    bold = Font(bold_path)
    path.write_bytes(build_pdf(regular, bold, draw))
