"""`apply --skip NAMESPACE`: không dựng plan cho namespace đó.

Đây là hợp đồng cho việc chia đôi quyền sở hữu với Nix: Nix ghi [pwa],
nên CLI phải được bảo đừng đụng vào -- chứ không dựa vào việc nó tình cờ
thấy no-diff.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotbrave import browser as brave_pkg


@pytest.fixture
def prefs(tmp_path: Path) -> tuple[Path, dict]:
    profile = tmp_path / "Default"
    profile.mkdir()
    data = {"brave": {"tabs": {"vertical_tabs_enabled": False}}}
    p = profile / "Preferences"
    p.write_text(json.dumps(data))
    return p, data


def test_skip_pwa_builds_no_pwa_plan(prefs):
    path, data = prefs
    doc = {
        "settings": {"brave.tabs.vertical_tabs_enabled": True},
        "pwa": {"urls": ["https://example.com"]},
    }
    plans = brave_pkg._build_plans(path, data, doc, skip=("pwa",))
    assert [p.namespace for p in plans] == ["settings"]


def test_skip_is_repeatable(prefs):
    path, data = prefs
    doc = {
        "settings": {"brave.tabs.vertical_tabs_enabled": True},
        "pwa": {"urls": ["https://example.com"]},
    }
    plans = brave_pkg._build_plans(
        path, data, doc, skip=("pwa", "settings")
    )
    assert plans == []


def test_no_skip_builds_every_present_namespace(prefs):
    path, data = prefs
    doc = {
        "settings": {"brave.tabs.vertical_tabs_enabled": True},
        "pwa": {"urls": ["https://example.com"]},
    }
    plans = brave_pkg._build_plans(path, data, doc)
    assert sorted(p.namespace for p in plans) == ["pwa", "settings"]


def test_cli_rejects_unknown_namespace(tmp_path):
    from dotbrave.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["apply", "--skip", "nonsense", str(tmp_path / "b.toml")]
        )
