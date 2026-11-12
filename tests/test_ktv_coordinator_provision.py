"""Synthetic offline provisioning; no real configs, services, NAS, TV or audio."""
from __future__ import annotations

import hashlib
import importlib
import json
import plistlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    helper = importlib.import_module("prepare_ktv_coordinator")
    builder = importlib.import_module("build_ktv_tv")
    root = tmp_path.resolve()
    public = root / "data/ktv/nas.json"
    public.parent.mkdir(parents=True)
    public.write_text(json.dumps({"endpoint": "https://guest.example.invalid/ktv", "agent_key": "a" * 43,
                                  "kept_setting": "synthetic"}))
    public.chmod(0o600)
    plist = root / ".artifacts/input/local.friday.ktv.plist"
    plist.parent.mkdir(parents=True)
    definition = {"Label": "local.friday.ktv", "ProgramArguments": [str(root / ".venv/bin/python"),
        str(root / "scripts/ktv_server.py"), "serve", "--host", "10.77.0.2", "--port", "9876",
        "--public-config", str(public), "--allow-download", "--launchd-sockets"],
        "WorkingDirectory": str(root), "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 3,
        "Sockets": {name: {"SockFamily": "IPv4", "SockType": "stream", "SockNodeName": host,
                            "SockServiceName": "9876"} for name, host in (("KTVLAN", "10.77.0.2"), ("KTVLocal", "127.0.0.1"))}}
    plist.write_bytes(plistlib.dumps(definition))
    plist.chmod(0o644)
    return SimpleNamespace(root=root, public=public, plist=plist, definition=definition,
                           output=root / ".artifacts/candidate", helper=helper, builder=builder)


def prepare(fixture, **kwargs):
    return fixture.helper.prepare(fixture.public, fixture.plist, "http://10.77.0.3:19089/ktv",
                                  fixture.output, root=fixture.root, **kwargs)


def test_prepare_is_non_destructive_private_and_does_not_start_anything(fixture, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("preparation must never execute commands"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("preparation must never spawn a process"))
    before = {p: p.read_bytes() for p in (fixture.public, fixture.plist)}
    result = prepare(fixture)
    assert result == {"prepared": True, "files": 6, "existing_tv_key_reused": False, "installed": False,
                      "services_started": False, "device_contacted": False, "secrets_printed": False}
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert fixture.output.stat().st_mode & 0o777 == 0o700
    assert len(list(fixture.output.iterdir())) == 6
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in fixture.output.iterdir())
    worker = json.loads((fixture.output / "mac-nas.json").read_text())
    tv = json.loads((fixture.output / "tv-connection.json").read_text())
    assert worker["coordination"] == "nas" and worker["media_origin"] == "http://10.77.0.2:9876"
    assert worker["agent_key"] == "a" * 43 and worker["kept_setting"] == "synthetic"
    assert tv["origin"] == "http://10.77.0.3:19089/ktv" and tv["role"] == "nas"
    assert tv["token"] != worker["agent_key"]
    assert (fixture.output / "nas-tv-key").read_text().strip() == tv["token"]
    draft = plistlib.loads((fixture.output / "mac-launchd.plist").read_bytes())
    assert draft["Sockets"] == fixture.definition["Sockets"] and draft["KeepAlive"] == fixture.definition["KeepAlive"]
    args = draft["ProgramArguments"]
    assert args[args.index("--host") + 1] == "10.77.0.2" and args[args.index("--port") + 1] == "9876"
    assert args[args.index("--public-config") + 1] == str(fixture.output / "mac-nas.json")
    instructions = (fixture.output / "DEPLOYMENT.md").read_text()
    assert "preserve it" in instructions and "no-clobber" in instructions and "never runs launchctl" in instructions
    receipt = json.loads((fixture.output / "prepare-result.json").read_text())
    assert not receipt["nas_existing_key_checked"] and not receipt["installed"]
    for name, sha in receipt["files_sha256"].items():
        assert hashlib.sha256((fixture.output / name).read_bytes()).hexdigest() == sha


def test_existing_tv_key_is_reused_exactly_and_never_regenerated(fixture, monkeypatch):
    existing = fixture.root / ".artifacts/input/nas-tv-key"
    existing.write_text("b" * 43 + "\n")
    existing.chmod(0o600)
    monkeypatch.setattr(fixture.helper.secrets, "token_urlsafe", lambda *a: pytest.fail("explicit existing key must be preserved"))
    prepare(fixture, existing_tv_key=existing)
    assert (fixture.output / "nas-tv-key").read_bytes() == existing.read_bytes()
    assert json.loads((fixture.output / "prepare-result.json").read_text())["existing_tv_key_reused"]


@pytest.mark.parametrize("target", ["directory", "file", "symlink"])
def test_existing_outputs_are_never_overwritten(fixture, target):
    if target == "directory":
        fixture.output.mkdir()
    elif target == "file":
        fixture.output.write_bytes(b"keep")
    else:
        fixture.output.symlink_to(fixture.public)
    before = fixture.public.read_bytes()
    with pytest.raises(ValueError):
        prepare(fixture)
    assert fixture.public.read_bytes() == before
    if target == "file":
        assert fixture.output.read_bytes() == b"keep"


@pytest.mark.parametrize("mutation", ["duplicate_host", "wrong_port", "wrong_socket", "public_host", "loopback_host", "mismatched_config", "shell_interpreter"])
def test_unknown_or_conflicting_service_targets_fail_closed_before_output(fixture, mutation):
    value = fixture.definition
    args = value["ProgramArguments"]
    if mutation == "duplicate_host":
        args += ["--host", "10.77.0.2"]
    elif mutation == "wrong_port":
        args[args.index("--port") + 1] = "80"
    elif mutation == "wrong_socket":
        value["Sockets"]["KTVLAN"]["SockNodeName"] = "10.77.0.4"
    elif mutation in {"public_host", "loopback_host"}:
        args[args.index("--host") + 1] = "198.51.100.2" if mutation == "public_host" else "127.0.0.1"
    elif mutation == "mismatched_config":
        args[args.index("--public-config") + 1] = str(fixture.root / "data/other.json")
    else:
        args[0] = "/bin/sh"
    fixture.plist.write_bytes(plistlib.dumps(value))
    with pytest.raises(ValueError):
        prepare(fixture)
    assert not fixture.output.exists()


def test_private_sources_and_output_boundaries_are_enforced(fixture):
    fixture.public.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        prepare(fixture)
    fixture.public.chmod(0o600)
    fixture.output = fixture.root / "public-candidate"
    with pytest.raises(ValueError, match="ignored_private"):
        prepare(fixture)
    assert not fixture.output.exists()


@pytest.mark.parametrize("value", ["http://nas.example.invalid:19089/ktv", "http://198.51.100.2:19089/ktv", "http://10.77.0.3:80/ktv", "https://user@example.invalid/ktv", "https://nas.example.invalid/../admin", "https://nas.example.invalid/%2fadmin", "https://nas.example.invalid/ktv?q=1", "https://nas.example.invalid/ktv#private", "https://[::1]/ktv"])
def test_nas_endpoint_restrictions_match_the_tv_client(fixture, value):
    with pytest.raises(ValueError):
        fixture.helper.validate_origin(value)
    assert fixture.helper.validate_origin("https://NAS.example.invalid/ktv/") == "https://nas.example.invalid/ktv"


def test_builder_private_nas_source_is_scoped_and_fingerprint_bound(fixture):
    prepare(fixture)
    config = fixture.output / "tv-connection.json"
    profile = fixture.builder.connection_profile(nas_connection_config=config, root=fixture.root)
    assert profile["role"] == "nas" and profile["fingerprint"]
    java = fixture.builder.generated_setup_defaults(profile)
    assert 'ROLE="nas"' in java and profile["fingerprint"] in java and profile["token"] in java
    assert fixture.builder.connection_profile(root=fixture.root) is None
    with pytest.raises(ValueError):
        fixture.builder.connection_profile(connection_config=fixture.public, nas_connection_config=config, root=fixture.root)
    with pytest.raises(ValueError):
        fixture.builder.connection_profile(nas_connection_config=config, host="10.77.0.2", root=fixture.root)
    value = json.loads(config.read_text())
    value["origin"] += "/changed"
    config.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="private_nas"):
        fixture.builder.connection_profile(nas_connection_config=config, root=fixture.root)


def test_public_or_non_private_nas_config_cannot_enter_an_apk(fixture):
    prepare(fixture)
    config = fixture.output / "tv-connection.json"
    config.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        fixture.builder.connection_profile(nas_connection_config=config, root=fixture.root)
    public = fixture.root / "connection.json"
    public.write_bytes(config.read_bytes())
    public.chmod(0o600)
    with pytest.raises(ValueError, match="ignored_private"):
        fixture.builder.connection_profile(nas_connection_config=public, root=fixture.root)


def test_profile_canonical_fixture_matches_android(fixture):
    assert fixture.helper.profile_fingerprint("nas", "https://nas.example.invalid/ktv", "x" * 43) == "2f31dbf920e06f5d55687db99eab9be275809e7ce33c52b34b7bb11d5cd39b84"


def test_nas_build_must_reuse_existing_private_apk_signing_identity(fixture):
    output = fixture.root / ".artifacts/signing"
    output.mkdir()
    fixture.builder.existing_nas_signing(output, None)
    with pytest.raises(ValueError, match="existing_apk_signing"):
        fixture.builder.existing_nas_signing(output, {"role": "nas"})
    for name in ("development-signing.p12", "development-signing.password"):
        path = output / name
        path.write_bytes(b"synthetic existing material")
        path.chmod(0o600)
    fixture.builder.existing_nas_signing(output, {"role": "nas"})
    (output / "development-signing.password").chmod(0o644)
    with pytest.raises(ValueError, match="existing_apk_signing"):
        fixture.builder.existing_nas_signing(output, {"role": "nas"})


def test_cli_output_contains_no_credentials_or_household_addresses(fixture, monkeypatch, capsys):
    monkeypatch.setattr(fixture.helper, "ROOT", fixture.root)
    monkeypatch.setattr(sys, "argv", ["prepare_ktv_coordinator.py", "--public-config", str(fixture.public),
        "--launchd-plist", str(fixture.plist), "--nas-origin", "http://10.77.0.3:19089/ktv", "--output", str(fixture.output)])
    assert fixture.helper.main() == 0
    output = capsys.readouterr().out
    assert "10.77." not in output and "a" * 43 not in output
    assert json.loads(output)["secrets_printed"] is False
