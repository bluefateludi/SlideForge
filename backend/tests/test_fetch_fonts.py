"""Font builds must verify cached files and fail visibly on network/corruption errors."""

import hashlib
import importlib.util
import io
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_fonts.py"
spec = importlib.util.spec_from_file_location("fetch_fonts", SCRIPT)
fonts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fonts)

DATA = b"verified font fixture"
DIGEST = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def font_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fonts, "FONTS_DIR", tmp_path)
    monkeypatch.setattr(fonts.time, "sleep", lambda _: None)
    return tmp_path


def test_verified_cached_font_does_not_access_network(font_dir, monkeypatch):
    (font_dir / "font.otf").write_bytes(DATA)
    request = Mock(side_effect=AssertionError("unexpected download"))
    monkeypatch.setattr(fonts.urllib.request, "urlopen", request)

    fonts.fetch("font.otf", "https://example.test/font", DIGEST)

    request.assert_not_called()


def test_corrupt_cache_is_replaced_only_with_verified_download(font_dir, monkeypatch):
    (font_dir / "font.otf").write_bytes(b"corrupt")
    request = Mock(return_value=io.BytesIO(DATA))
    monkeypatch.setattr(fonts.urllib.request, "urlopen", request)

    fonts.fetch("font.otf", "https://example.test/font", DIGEST)

    assert (font_dir / "font.otf").read_bytes() == DATA
    assert not (font_dir / "font.otf.tmp").exists()
    request.assert_called_once_with("https://example.test/font", timeout=30)


def test_network_failure_has_bounded_retries_and_no_stale_font(font_dir, monkeypatch):
    (font_dir / "font.otf").write_bytes(b"corrupt")
    request = Mock(side_effect=TimeoutError("network timeout"))
    monkeypatch.setattr(fonts.urllib.request, "urlopen", request)

    with pytest.raises(TimeoutError, match="network timeout"):
        fonts.fetch("font.otf", "https://example.test/font", DIGEST)

    assert request.call_count == fonts.DOWNLOAD_ATTEMPTS
    assert not (font_dir / "font.otf").exists()
    assert not (font_dir / "font.otf.tmp").exists()


def test_checksum_failure_never_publishes_font(font_dir, monkeypatch):
    monkeypatch.setattr(
        fonts.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"wrong content")
    )

    with pytest.raises(ValueError, match="SHA-256"):
        fonts.fetch("font.otf", "https://example.test/font", DIGEST)

    assert not list(font_dir.iterdir())


def test_download_recovers_after_transient_failure(font_dir, monkeypatch):
    request = Mock(side_effect=[TimeoutError("temporary"), io.BytesIO(DATA)])
    monkeypatch.setattr(fonts.urllib.request, "urlopen", request)

    fonts.fetch("font.otf", "https://example.test/font", DIGEST)

    assert request.call_count == 2
    assert (font_dir / "font.otf").read_bytes() == DATA


@pytest.mark.parametrize("allow_missing,expected", [(False, 1), (True, 0)])
def test_missing_fonts_require_explicit_degradation(
    font_dir, monkeypatch, capsys, allow_missing, expected
):
    monkeypatch.setattr(fonts, "FONTS", {"font.otf": {"url": "unused", "sha256": DIGEST}})
    monkeypatch.setattr(fonts, "fetch", Mock(side_effect=TimeoutError("unavailable")))

    assert fonts.main(["--allow-missing"] if allow_missing else []) == expected
    output = capsys.readouterr()
    assert "fonts ready" not in output.out
    assert ("缺字体降级" in output.err) == allow_missing
