from urllib.parse import unquote

from live_recorder.web.disposition import attachment_disposition


def test_japanese_download_name_fits_in_a_header():
    name = "tiktok_@9ri_pi_20261111_044027_ゲスト出勤_seg01.mp4"
    header = attachment_disposition(name)
    header.encode("latin-1")
    fallback, encoded = header.split("filename*=UTF-8''", 1)
    assert fallback.startswith('attachment; filename="')
    assert "ゲスト" not in fallback
    assert unquote(encoded) == name


def test_download_name_drops_header_breaks():
    name = 'bad"name\r\nX-Injected: 1.srt'
    header = attachment_disposition(name)
    header.encode("latin-1")
    assert "\r" not in header and "\n" not in header
    fallback, encoded = header.split("filename*=UTF-8''", 1)
    assert 'filename="bad_name__X-Injected: 1.srt"' in fallback
    assert unquote(encoded) == name
