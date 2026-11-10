import importlib.util
import sys
from pathlib import Path


def _icons():
    path = Path(__file__).resolve().parents[1] / "windows" / "tray" / "render_icons.py"
    spec = importlib.util.spec_from_file_location("render_icons", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pixel(module, ico: Path, size: int, x: int, y: int):
    png = module.png_from_ico(ico.read_bytes(), size)
    width, height, rows = module.decode_png(png)
    assert (width, height) == (size, size)
    return rows[y][x]


def test_tray_icons_match_favicon_colors(tmp_path):
    module = _icons()
    module.write_tray_icons(tmp_path)
    idle = module.parse_mark(module.FAVICON)
    recording = module.parse_mark(module.FAVICON_RECORDING)

    assert idle.fill == "#4d63d9"
    assert recording.fill == "#cc2f68"
    assert module.parse_hex(idle.dot) == (255, 255, 255)
    assert module.STOPPED_FILL == "#9aa0b4"

    # 32px の画素中心 (1.5, 16.5) は角丸の地、(16.5, 16.5) は白円の中。
    assert _pixel(module, tmp_path / "idle.ico", 32, 1, 16) == (0x4D, 0x63, 0xD9, 255)
    assert _pixel(module, tmp_path / "idle.ico", 32, 16, 16) == (255, 255, 255, 255)
    assert _pixel(module, tmp_path / "idle.ico", 32, 0, 0) == (0, 0, 0, 0)
    assert _pixel(module, tmp_path / "recording.ico", 32, 1, 16) == (0xCC, 0x2F, 0x68, 255)
    assert _pixel(module, tmp_path / "recording.ico", 32, 16, 16) == (255, 255, 255, 255)
    assert _pixel(module, tmp_path / "stopped.ico", 32, 1, 16) == (0x9A, 0xA0, 0xB4, 255)
    assert _pixel(module, tmp_path / "stopped.ico", 32, 16, 16) == (255, 255, 255, 255)

    for size in (16, 48):
        assert _pixel(module, tmp_path / "idle.ico", size, size // 2, size // 2)[3] == 255
