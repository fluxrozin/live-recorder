import types

from live_recorder.versions import tool_version, version_from_tiktoklive


def test_ffmpeg_keeps_debian_revision():
    raw = "ffmpeg version 5.1.9-0+deb12u1 Copyright (c) 2000-2022 the FFmpeg developers"
    assert tool_version(raw) == "5.1.9-0+deb12u1"


def test_ffprobe_ignores_following_lines():
    raw = "ffprobe version 5.1.9-0+deb12u1 Copyright (c) 2007-2022 the FFmpeg developers\nbuilt with gcc 12"
    assert tool_version(raw) == "5.1.9-0+deb12u1"


def test_tool_version_empty():
    assert tool_version("") == ""
    assert tool_version("   \n") == ""


def test_unrecognized_tool_output_is_kept():
    assert tool_version("streamlink 8.6.3") == "streamlink 8.6.3"


def test_tiktoklive_version_module_is_not_stringified():
    package = types.ModuleType("TikTokLive")
    version_module = types.ModuleType("TikTokLive.__version__")
    version_module.__version__ = "7.0.1"
    package.__version__ = version_module
    assert version_from_tiktoklive(package) == "7.0.1"


def test_tiktoklive_string_version_is_kept():
    package = types.SimpleNamespace(__version__="7.0.1")
    assert version_from_tiktoklive(package) == "7.0.1"
