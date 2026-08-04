"""`apply --skip NAMESPACE`: không dựng plan cho namespace đó.

Đây là hợp đồng cho việc chia đôi quyền sở hữu với Nix: Nix ghi [pwa],
nên CLI phải được bảo đừng đụng vào -- chứ không dựa vào việc nó tình cờ
thấy no-diff.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dotbrave import browser as brave_pkg
from dotbrave._base import orchestrator


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prefs(tmp_path: Path) -> tuple[Path, dict]:
    profile = tmp_path / "Default"
    profile.mkdir()
    data = {"brave": {"tabs": {"vertical_tabs_enabled": False}}}
    p = profile / "Preferences"
    p.write_text(json.dumps(data))
    return p, data


@pytest.fixture
def prefs_root(tmp_path: Path) -> Path:
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(
        json.dumps({"brave": {"tabs": {"vertical_tabs_enabled": False}}})
    )
    return tmp_path


def _apply_args(prefs_root: Path, config: Path, **kw) -> argparse.Namespace:
    base = dict(
        profile_root=prefs_root,
        profile="Default",
        config=str(config),
        dry_run=True,
        unattended=True,
        skip=["pwa"],
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _cmd_apply(args: argparse.Namespace) -> None:
    orchestrator.cmd_apply(
        args,
        display_name="Brave",
        running_fn=lambda: False,
        find_cmdline_fn=lambda: None,
        restart_fn=lambda c: c,
        build_plans_fn=brave_pkg._build_plans,
    )


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


def test_cli_accepts_known_namespaces_and_is_repeatable(tmp_path):
    from dotbrave.cli import build_parser

    args = build_parser().parse_args(
        [
            "apply",
            "--skip",
            "settings",
            "--skip",
            "pwa",
            str(tmp_path / "b.toml"),
        ]
    )
    assert args.skip == ["settings", "pwa"]


def test_cli_rejects_unknown_namespace(tmp_path):
    from dotbrave.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["apply", "--skip", "nonsense", str(tmp_path / "b.toml")]
        )


# ---------------------------------------------------------------------------
# "Bo qua het" khac han "config khong co bang nao"
# ---------------------------------------------------------------------------

def test_skipping_every_present_table_is_not_an_error(
    prefs_root, tmp_path, capsys
):
    """Config chi co [pwa] + `--skip pwa`: khong con gi de lam, nhung day
    khong phai loi -- Nix so huu bang do. Phai bao ro va exit 0, dung nhu
    hop dong `--unattended` da hua trong help text va README."""
    cfg = tmp_path / "pwaonly.toml"
    cfg.write_text('[pwa]\nurls = ["https://example.com/"]\n')

    _cmd_apply(_apply_args(prefs_root, cfg))

    out = capsys.readouterr().out
    assert "[pwa]" in out
    assert "skip" in out.lower()
    # Khong duoc doi lai thanh "config khong co bang nao".
    assert "no [shortcuts], [settings] or [pwa] table" not in out


def test_config_with_no_known_table_still_errors(prefs_root, tmp_path):
    """Truong hop that su rong van phai bao loi nhu cu -- day la loi go
    nham ten bang, khong phai chia so huu."""
    cfg = tmp_path / "nothing.toml"
    cfg.write_text('[nonsense]\nx = 1\n')

    with pytest.raises(SystemExit) as excinfo:
        _cmd_apply(_apply_args(prefs_root, cfg))

    assert "no [shortcuts], [settings] or [pwa] table" in str(excinfo.value)


def test_skipping_a_table_that_is_absent_still_errors(prefs_root, tmp_path):
    """`--skip pwa` tren mot config khong he co [pwa]: van la config rong,
    van phai bao loi. Co la khong the noi 'da bo qua' mot thu khong ton tai."""
    cfg = tmp_path / "nothing.toml"
    cfg.write_text('[nonsense]\nx = 1\n')

    with pytest.raises(SystemExit) as excinfo:
        _cmd_apply(_apply_args(prefs_root, cfg, skip=["pwa", "settings"]))

    assert "no [shortcuts], [settings] or [pwa] table" in str(excinfo.value)


def test_cli_exits_zero_when_every_table_is_skipped(tmp_path):
    """Lop CLI that, khong phai goi ham: exit code phai la 0. Day chinh la
    lenh ma home-manager activation chay."""
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(json.dumps({}))
    cfg = tmp_path / "pwaonly.toml"
    cfg.write_text('[pwa]\nurls = ["https://example.com/"]\n')

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [
            sys.executable, "-m", "dotbrave", "apply",
            "--unattended", "--dry-run", "--skip", "pwa",
            "--profile-root", str(tmp_path), str(cfg),
        ],
        capture_output=True, text=True, env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
