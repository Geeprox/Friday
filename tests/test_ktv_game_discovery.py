"""Installed-owner discovery uses private metadata only, never executes a job."""
import json
import os
import plistlib
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homeplane.ktv_game_api import LAUNCH_AGENT_LABEL, create_router

TOKEN = "SYNTHETIC_OWNER_" + "x" * 32


@pytest.fixture
def files(tmp_path):
    work = tmp_path / "owner-workspace"
    config = work / "data/ktv/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"token": TOKEN}))
    config.chmod(0o600)
    bundle = tmp_path / "Application.app/Contents/Resources/product/app"
    bundle.mkdir(parents=True)
    agent = tmp_path / "LaunchAgents/local.friday.ktv.plist"
    agent.parent.mkdir()
    job = {"Label": LAUNCH_AGENT_LABEL, "WorkingDirectory": str(work),
           "ProgramArguments": ["/usr/bin/python3", str(work / "scripts/ktv_server.py"), "serve"]}

    def save():
        agent.write_bytes(plistlib.dumps(job))
        agent.chmod(0o644)
    save()
    return SimpleNamespace(config=config, bundle=bundle, agent=agent, job=job, save=save)


def request(files, *, explicit=None):
    seen = []

    def upstream(value):
        seen.append(value)
        return httpx.Response(200, json={"active": False})
    app = FastAPI()
    app.include_router(create_router(explicit, launch_agent_path=files.agent, development_root=files.bundle,
                                    transport=httpx.MockTransport(upstream)))
    with TestClient(app, base_url="http://127.0.0.1:8740", client=("127.0.0.1", 9000)) as client:
        response = client.get("/api/games/ktv/manage/room")
    return response, seen


def test_packaged_app_without_data_uses_existing_job_working_directory(files):
    assert not (files.bundle / "data").exists()
    response, seen = request(files)
    assert response.status_code == 200 and len(seen) == 1
    assert seen[0].headers["X-KTV-Key"] == TOKEN
    assert TOKEN not in response.text and str(files.config) not in response.text


@pytest.mark.parametrize("inline", [False, True])
def test_explicit_installed_config_parameter_is_followed_without_executing_args(files, inline):
    files.job.pop("WorkingDirectory")
    files.job["ProgramArguments"] += ["--config=" + str(files.config)] if inline else ["--config", str(files.config)]
    files.save()
    response, seen = request(files)
    assert response.status_code == 200 and len(seen) == 1


def test_development_fallback_and_explicit_override_are_preserved(files):
    files.agent.unlink()
    fallback = files.bundle / "data/ktv/config.json"
    fallback.parent.mkdir(parents=True)
    fallback.write_text(json.dumps({"token": TOKEN}))
    fallback.chmod(0o600)
    assert request(files)[0].status_code == 200
    files.job["Label"] = "untrusted.job"
    files.save()
    assert request(files, explicit=files.config)[0].status_code == 200
    response, seen = request(files)
    assert response.status_code == 503 and not seen, "An invalid installed job must not select the fallback"


@pytest.mark.parametrize("change", ["label", "not_list", "short", "script", "action", "duplicate", "missing_value", "unknown", "wrong_port", "traversal", "no_working_directory"])
def test_invalid_job_arguments_fail_closed(files, change):
    args = files.job["ProgramArguments"]
    if change == "label":
        files.job["Label"] = "different.service"
    elif change == "not_list":
        files.job["ProgramArguments"] = "do not execute"
    elif change == "short":
        files.job["ProgramArguments"] = ["/usr/bin/python3"]
    elif change == "script":
        args[1] = "/tmp/unknown.py"
    elif change == "action":
        args[2] = "init"
    elif change == "duplicate":
        args += ["--config", str(files.config), "--config", str(files.config)]
    elif change == "missing_value":
        args += ["--config"]
    elif change == "unknown":
        args += ["--run-arbitrary-command", "ignored"]
    elif change == "wrong_port":
        args += ["--port", "19999"]
    elif change == "traversal":
        args += ["--config", "../elsewhere.json"]
    else:
        files.job.pop("WorkingDirectory")
    files.save()
    response, seen = request(files)
    assert response.status_code == 503 and not seen
    assert TOKEN not in response.text and str(files.config) not in response.text


@pytest.mark.parametrize("change", ["writable", "hardlink", "symlink", "corrupt", "oversize", "owner"])
def test_unsafe_plist_is_refused(files, change, monkeypatch):
    if change == "writable":
        files.agent.chmod(0o666)
    elif change == "hardlink":
        os.link(files.agent, files.agent.with_name("second.plist"))
    elif change == "symlink":
        saved = files.agent.with_name("saved.plist")
        files.agent.rename(saved)
        files.agent.symlink_to(saved)
    elif change == "corrupt":
        files.agent.write_bytes(b"invalid plist")
    elif change == "oversize":
        files.agent.write_bytes(b"x" * 65537)
    else:
        uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    response, seen = request(files)
    assert response.status_code == 503 and not seen


@pytest.mark.parametrize("change", ["hardlink", "symlink", "writable", "readonly", "missing"])
def test_discovered_config_retains_strict_private_file_checks(files, change):
    if change == "hardlink":
        os.link(files.config, files.config.with_name("second.json"))
    elif change == "symlink":
        saved = files.config.with_name("saved.json")
        files.config.rename(saved)
        files.config.symlink_to(saved)
    elif change == "missing":
        files.config.unlink()
    else:
        files.config.chmod(0o644 if change == "writable" else 0o400)
    response, seen = request(files)
    assert response.status_code == 503 and not seen


def test_missing_job_and_app_data_reports_unconfigured_without_owner_request(files):
    files.agent.unlink()
    response, seen = request(files)
    assert response.status_code == 503 and response.json() == {"detail": "ktv_not_configured"} and not seen
