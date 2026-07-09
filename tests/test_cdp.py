from __future__ import annotations

import json
from pathlib import Path

from dotbrave._base import cdp


def test_remember_devtools_port_records_profile_and_port(tmp_path: Path) -> None:
    cdp.remember_devtools_port(tmp_path, "Profile 1", 9444)

    data = json.loads((tmp_path / ".dotbrave.live.json").read_text())

    assert data == {"profile": "Profile 1", "port": 9444}


def test_find_devtools_port_reads_dotbrave_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / ".dotbrave.live.json").write_text(
        json.dumps({"profile": "Default", "port": 9444})
    )
    monkeypatch.setattr(cdp, "devtools_endpoint_alive", lambda port: port == 9444)

    assert cdp.find_devtools_port(tmp_path, "Default") == 9444


def test_find_devtools_port_ignores_other_profile_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / ".dotbrave.live.json").write_text(
        json.dumps({"profile": "Profile 1", "port": 9444})
    )
    monkeypatch.setattr(cdp, "devtools_endpoint_alive", lambda _port: True)

    assert cdp.find_devtools_port(tmp_path, "Default") is None


def test_find_devtools_port_ignores_stale_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / ".dotbrave.live.json").write_text(
        json.dumps({"profile": "Default", "port": 9444})
    )
    monkeypatch.setattr(cdp, "devtools_endpoint_alive", lambda _port: False)

    assert cdp.find_devtools_port(tmp_path, "Default") is None
