import shutil
import subprocess
from pathlib import Path


def test_tray_text_logic(tmp_path):
    root = Path(__file__).resolve().parents[1] / "windows" / "tray"
    compiler = shutil.which("g++") or shutil.which("clang++")
    assert compiler, "g++ or clang++ is required to test the tray text logic"
    exe = tmp_path / "textutil_test"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(root),
            str(root / "src" / "textutil.cpp"),
            str(root / "textutil_test.cpp"),
            "-o",
            str(exe),
        ],
        check=True,
    )
    subprocess.run([str(exe)], check=True)
