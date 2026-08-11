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


def test_cdp_failures_raise_instead_of_exiting() -> None:
    from dotbrave._base.cdp import CdpClient, CdpError
    import pytest

    client = CdpClient(1)  # nothing listening on port 1
    with pytest.raises(CdpError):
        client.list_targets()


def test_live_apply_translates_cdp_errors_into_a_recoverable_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    """A CDP failure must degrade to the offline path, not abort the run."""
    import json, pytest
    from dotbrave._base import cdp as cdp_mod
    from dotbrave._base import live_apply as shared_live
    from dotbrave._base.utils import Plan
    from dotbrave import live

    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir()
    prefs = {"foo": {"bar": 0}}
    prefs_path.write_text(json.dumps(prefs))

    class ExplodingClient:
        def __init__(self, port): pass
        def list_targets(self): raise cdp_mod.CdpError("boom")
        def create_page(self, url="about:blank"): raise cdp_mod.CdpError("boom")

    monkeypatch.setattr(live, "CdpClient", ExplodingClient)

    plan = Plan(
        namespace="settings",
        diff_lines=["changed"],
        apply_fn=lambda target: target["foo"].__setitem__("bar", 1),
        verify_fn=lambda _p: None,
    )

    with pytest.raises(shared_live.LiveApplyUnsupported):
        live.apply_live(9333, prefs_path, prefs, [plan])
