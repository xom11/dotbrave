"""Tests for `dotbrave init`."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "dotbrave", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )


def test_init_stdout() -> None:
    """init with no -o prints a valid template to stdout."""
    r = _run("init")
    assert r.returncode == 0
    assert "[shortcuts]" in r.stdout
    assert "[settings]" in r.stdout
    # pwa is commented out by default
    assert "# [pwa]" in r.stdout
    # default filename placeholder
    assert "brave.toml" in r.stdout


def test_init_output_file(tmp_path: Path) -> None:
    """init -o writes the template to the given file."""
    dest = tmp_path / "my.toml"
    r = _run("init", "-o", str(dest))
    assert r.returncode == 0
    assert f"wrote {dest}" in r.stdout
    content = dest.read_text(encoding="utf-8")
    assert "[shortcuts]" in content
    assert "[settings]" in content
    # the apply instruction references the filename
    assert "my.toml" in content


def test_init_refuses_overwrite(tmp_path: Path) -> None:
    """init -o refuses to clobber an existing file."""
    dest = tmp_path / "existing.toml"
    dest.write_text("already here", encoding="utf-8")
    r = _run("init", "-o", str(dest))
    assert r.returncode != 0
    assert "already exists" in r.stderr
    # original content untouched
    assert dest.read_text(encoding="utf-8") == "already here"


def test_init_template_is_valid_toml() -> None:
    """The generated template (with comment-only sections) parses as TOML."""
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib  # type: ignore[no-redef]

    r = _run("init")
    assert r.returncode == 0
    doc = tomllib.loads(r.stdout)
    # All three sections should parse; shortcuts and settings are empty
    # (all entries are commented), pwa header is commented so not present.
    assert isinstance(doc.get("shortcuts", {}), dict)
    assert isinstance(doc.get("settings", {}), dict)
