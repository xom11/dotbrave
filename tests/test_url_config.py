"""URL config fetch hardening: HTTPS-only, sha256 pin, size cap.

Covers ``_base/orchestrator._load_toml_from_url`` and ``load_toml_source``.
The real network is never hit -- ``urllib.request.urlopen`` is
monkeypatched to return a canned BytesIO response so the tests are
deterministic and offline.
"""
from __future__ import annotations

import hashlib
import io

import pytest

from dotbrave._base import orchestrator as orch


_OK_TOML = b'[settings]\n"foo.bar" = true\n'


class _FakeResponse:
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_urlopen(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    def fake(url, timeout=10):
        return _FakeResponse(data)

    monkeypatch.setattr(orch.urllib.request, "urlopen", fake)


def test_http_refused_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The url scheme guard fires before urlopen is called."""
    def boom(url, timeout=10):
        raise AssertionError("urlopen should not be called for refused http")

    monkeypatch.setattr(orch.urllib.request, "urlopen", boom)
    with pytest.raises(SystemExit, match="refusing to fetch config over plain http"):
        orch._load_toml_from_url("http://example.com/cfg.toml")


def test_http_allowed_with_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_urlopen(monkeypatch, _OK_TOML)
    doc = orch._load_toml_from_url(
        "http://example.com/cfg.toml", allow_http=True
    )
    assert doc == {"settings": {"foo.bar": True}}


def test_https_works(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_urlopen(monkeypatch, _OK_TOML)
    doc = orch._load_toml_from_url("https://example.com/cfg.toml")
    assert doc == {"settings": {"foo.bar": True}}


def test_sha256_match_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_urlopen(monkeypatch, _OK_TOML)
    digest = hashlib.sha256(_OK_TOML).hexdigest()
    doc = orch._load_toml_from_url(
        "https://example.com/cfg.toml", expect_sha256=digest
    )
    assert doc == {"settings": {"foo.bar": True}}


def test_sha256_match_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_urlopen(monkeypatch, _OK_TOML)
    digest = hashlib.sha256(_OK_TOML).hexdigest().upper()
    doc = orch._load_toml_from_url(
        "https://example.com/cfg.toml", expect_sha256=digest
    )
    assert doc == {"settings": {"foo.bar": True}}


def test_sha256_mismatch_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_urlopen(monkeypatch, _OK_TOML)
    with pytest.raises(SystemExit, match="sha256 mismatch"):
        orch._load_toml_from_url(
            "https://example.com/cfg.toml",
            expect_sha256="0" * 64,
        )


def test_size_cap_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """A response larger than _MAX_URL_CONFIG_BYTES must be refused.

    The implementation reads MAX+1 bytes and complains if it got more
    than MAX -- so the response only needs to be MAX+1 bytes long.
    """
    big = b"a" * (orch._MAX_URL_CONFIG_BYTES + 1)
    _stub_urlopen(monkeypatch, big)
    with pytest.raises(SystemExit, match="exceeds"):
        orch._load_toml_from_url("https://example.com/big.toml")


def test_size_cap_at_limit_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A response exactly at the limit (and parseable) is fine."""
    payload = b"# pad\n" * 1000 + _OK_TOML
    payload = payload[: orch._MAX_URL_CONFIG_BYTES]
    _stub_urlopen(monkeypatch, payload)
    # It just needs to not raise the size error; TOML parse may or may
    # not succeed depending on truncation, so we tolerate either branch.
    try:
        orch._load_toml_from_url("https://example.com/edge.toml")
    except SystemExit as e:
        assert "exceeds" not in str(e), "size error fired at exactly the limit"


def test_load_toml_source_passes_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_toml_source must forward allow_http / expect_sha256 to the
    URL loader (regression guard if the keyword names drift)."""
    captured: dict = {}

    def fake_url_loader(url, *, allow_http, expect_sha256):
        captured["url"] = url
        captured["allow_http"] = allow_http
        captured["expect_sha256"] = expect_sha256
        return {}

    monkeypatch.setattr(orch, "_load_toml_from_url", fake_url_loader)
    orch.load_toml_source(
        "https://example.com/x.toml",
        allow_http=True,
        expect_sha256="abc",
    )
    assert captured == {
        "url": "https://example.com/x.toml",
        "allow_http": True,
        "expect_sha256": "abc",
    }
