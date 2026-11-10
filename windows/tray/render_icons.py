"""favicon.svg と同じ図形のトレイ用 ICO を、標準ライブラリだけで書く。"""

from __future__ import annotations

import struct
import sys
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FAVICON = ROOT / "src" / "live_recorder" / "web" / "static" / "favicon.svg"
FAVICON_RECORDING = ROOT / "src" / "live_recorder" / "web" / "static" / "favicon-recording.svg"
STOPPED_FILL = "#9aa0b4"
SIZES = (16, 32, 48)


@dataclass(frozen=True)
class Mark:
    view: float
    x: float
    y: float
    width: float
    height: float
    rx: float
    fill: str
    cx: float
    cy: float
    r: float
    dot: str


def parse_hex(value: str) -> tuple[int, int, int]:
    text = value.strip().lower()
    if text.startswith("#"):
        text = text[1:]
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(value)
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def parse_mark(path: Path) -> Mark:
    root = ET.parse(path).getroot()
    ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
    rect = root.find(f"{ns}rect")
    circle = root.find(f"{ns}circle")
    if rect is None or circle is None:
        raise ValueError(path)
    view = float(root.get("viewBox", "0 0 32 32").split()[2])
    return Mark(
        view=view,
        x=float(rect.get("x", "0")),
        y=float(rect.get("y", "0")),
        width=float(rect.get("width", "0")),
        height=float(rect.get("height", "0")),
        rx=float(rect.get("rx", "0")),
        fill=rect.get("fill") or "",
        cx=float(circle.get("cx", "0")),
        cy=float(circle.get("cy", "0")),
        r=float(circle.get("r", "0")),
        dot=circle.get("fill") or "",
    )


def _inside_round_rect(px: float, py: float, mark: Mark) -> bool:
    left, top = mark.x, mark.y
    right, bottom = left + mark.width, top + mark.height
    if px < left or py < top or px > right or py > bottom:
        return False
    rx = mark.rx
    cx = min(max(px, left + rx), right - rx)
    cy = min(max(py, top + rx), bottom - rx)
    dx, dy = px - cx, py - cy
    return dx * dx + dy * dy <= rx * rx


def _inside_circle(px: float, py: float, mark: Mark) -> bool:
    dx, dy = px - mark.cx, py - mark.cy
    return dx * dx + dy * dy <= mark.r * mark.r


def sample(mark: Mark, size: int, x: int, y: int) -> tuple[int, int, int, int]:
    scale = mark.view / size
    px = (x + 0.5) * scale
    py = (y + 0.5) * scale
    if _inside_circle(px, py, mark):
        red, green, blue = parse_hex(mark.dot)
        return red, green, blue, 255
    if _inside_round_rect(px, py, mark):
        red, green, blue = parse_hex(mark.fill)
        return red, green, blue, 255
    return 0, 0, 0, 0


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def render_png(mark: Mark, size: int) -> bytes:
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw.extend(sample(mark, size, x, y))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def write_ico(path: Path, images: list[tuple[int, bytes]]) -> None:
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = bytearray()
    payload = bytearray()
    offset = 6 + 16 * len(images)
    for size, png in images:
        byte = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", byte, byte, 0, 0, 1, 32, len(png), offset)
        payload += png
        offset += len(png)
    path.write_bytes(header + bytes(entries) + bytes(payload))


def png_from_ico(data: bytes, size: int) -> bytes:
    if data[:4] != b"\x00\x00\x01\x00":
        raise ValueError("ico")
    count = struct.unpack_from("<H", data, 4)[0]
    want = 0 if size >= 256 else size
    for index in range(count):
        entry = 6 + 16 * index
        if data[entry] != want:
            continue
        length, offset = struct.unpack_from("<II", data, entry + 8)
        blob = data[offset : offset + length]
        if blob.startswith(b"\x89PNG\r\n\x1a\n"):
            return blob
    raise ValueError(size)


def decode_png(data: bytes) -> tuple[int, int, list[list[tuple[int, int, int, int]]]]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("png")
    pos = 8
    width = height = None
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack_from(">I", data, pos)[0]
        tag = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, bit, color, comp, filt, inter = struct.unpack(">IIBBBBB", chunk)
            if (bit, color, comp, filt, inter) != (8, 6, 0, 0, 0):
                raise ValueError("unsupported png")
        elif tag == b"IDAT":
            idat.extend(chunk)
        elif tag == b"IEND":
            break
    if width is None or height is None:
        raise ValueError("png")
    raw = zlib.decompress(bytes(idat))
    stride = width * 4
    rows: list[list[tuple[int, int, int, int]]] = []
    cursor = 0
    for _ in range(height):
        if raw[cursor] != 0:
            raise ValueError("filter")
        cursor += 1
        row = raw[cursor : cursor + stride]
        cursor += stride
        pixels: list[tuple[int, int, int, int]] = []
        for x in range(width):
            start = x * 4
            pixels.append((row[start], row[start + 1], row[start + 2], row[start + 3]))
        rows.append(pixels)
    return width, height, rows


def render_sizes(mark: Mark, sizes: tuple[int, ...] = SIZES) -> list[tuple[int, bytes]]:
    return [(size, render_png(mark, size)) for size in sizes]


def write_tray_icons(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    idle = parse_mark(FAVICON)
    recording = parse_mark(FAVICON_RECORDING)
    stopped = replace(idle, fill=STOPPED_FILL)
    write_ico(out / "idle.ico", render_sizes(idle))
    write_ico(out / "recording.ico", render_sizes(recording))
    write_ico(out / "stopped.ico", render_sizes(stopped))


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "build"
    write_tray_icons(out)


if __name__ == "__main__":
    main()
