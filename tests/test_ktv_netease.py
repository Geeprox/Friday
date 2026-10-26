import base64
import json
import subprocess
from unittest.mock import Mock

import pytest

from scripts import ktv_netease


def test_encoding_matches_recovered_client_parser_and_known_pause_uri():
    assert ktv_netease.build_uri("pause") == "orpheus://eyJjbWQiOiJwYXVzZSJ9"
    uri = ktv_netease.build_uri("play", "123456")
    assert json.loads(base64.b64decode(uri.removeprefix("orpheus://"))) == {
        "cmd": "play", "type": "song", "id": "123456"
    }


def test_default_cli_only_prepares_and_title_is_not_in_uri(monkeypatch, capsys):
    run = Mock(side_effect=AssertionError("default must not launch any app"))
    monkeypatch.setattr(ktv_netease.subprocess, "run", run)
    assert ktv_netease.main(["play", "--song-id", "123456", "--title", "本机展示标题"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "prepared" and value["execution_requested"] is False
    assert value["display_title"] == "本机展示标题"
    assert "本机展示标题" not in base64.b64decode(value["uri"].removeprefix("orpheus://")).decode()
    run.assert_not_called()


@pytest.mark.parametrize("action,song_id", [
    ("download", "123"), ("pause", "123"), ("play", None), ("play", "0"),
    ("play", "12\n"), ("play", "123;touch x"), ("play", "１２３"),
    ("play", "9223372036854775808"), ("play", True),
])
def test_unsupported_commands_or_bad_identity_never_launch(monkeypatch, action, song_id):
    run = Mock(side_effect=AssertionError("invalid input must not launch"))
    monkeypatch.setattr(ktv_netease.subprocess, "run", run)
    with pytest.raises(ValueError):
        ktv_netease.control(action, song_id, execute=True)
    run.assert_not_called()


def test_execution_uses_fixed_bundle_and_argv_but_never_claims_playback(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(ktv_netease.subprocess, "run", run)
    value = ktv_netease.control("resume", execute=True, title="PRIVATE_LOCAL_TITLE")
    args, kwargs = run.call_args
    assert args[0] == ["/usr/bin/open", "-b", "com.netease.163music", ktv_netease.build_uri("resume")]
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["timeout"] == 5
    assert value["status"] == "requested" and value["playback_confirmed"] is False
    assert "PRIVATE_LOCAL_TITLE" not in json.dumps(value) and "uri" not in value


@pytest.mark.parametrize("outcome,expected_status,expected_code", [
    (subprocess.CompletedProcess([], 1), "unknown", "launcher_failed"),
    (subprocess.TimeoutExpired("SECRET_UPSTREAM_TEXT", 5), "unknown", "launch_timeout"),
    (OSError("SECRET_UPSTREAM_TEXT"), "unavailable", "launcher_unavailable"),
])
def test_launch_failures_return_fixed_metadata_without_retry_or_upstream_text(
    monkeypatch, outcome, expected_status, expected_code
):
    run = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
    monkeypatch.setattr(ktv_netease.subprocess, "run", run)
    value = ktv_netease.control("pause", execute=True)
    assert value["status"] == expected_status and value["code"] == expected_code
    assert value["playback_confirmed"] is False
    assert "SECRET_UPSTREAM_TEXT" not in json.dumps(value)
    assert run.call_count == 1
