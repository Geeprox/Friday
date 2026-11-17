"""私有电视更新接口的离线回归。数据均为合成字节。"""

import hashlib
import importlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from integrations.ktv_public.nas_server import Server, Store, make_handler
from integrations.ktv_public.diagnose_tv_update import inspect, repair_modes
from integrations.ktv_public import tv_update_inbox
from integrations.ktv_public.tv_update_inbox import REQUEST_LIFETIME, TvUpdateInbox
from integrations.ktv_public.tv_updates import (
    APK_CONTENT_TYPE,
    MAX_APK_BYTES,
    UPDATE_APK_PATH,
    UPDATE_INFO_PATH,
    Release,
    TvUpdateError,
    TvUpdateStore,
)


ROOT = Path(__file__).resolve().parents[1]
AGENT_KEY = "z" * 43


def artifact(version, signer="a", body=None):
    if body is None:
        body = ("synthetic TV package " + str(version)).encode()
    return Release(version, "0." + str(version), len(body), hashlib.sha256(body).hexdigest(), signer * 64), body


@pytest.fixture
def updates(tmp_path):
    state = tmp_path.resolve() / "state"
    state.mkdir(mode=0o700)
    return TvUpdateStore(state)


def apk(updates, release):
    return updates.directory / (release.sha256 + ".apk")


def test_preflight_monotonic_signer_and_retention(updates):
    first, second, third = (artifact(version) for version in (71, 72, 73))
    assert updates.plan(first[0]) == "planned"
    assert not updates.directory.exists()
    assert updates.publish(*first) == "published"
    assert updates.publish(*first) == "already_current"
    with pytest.raises(TvUpdateError, match="bytes_mismatch"):
        updates.publish(second[0], first[1])
    with pytest.raises(TvUpdateError, match="signer_changed"):
        updates.publish(*artifact(72, signer="b"))
    with pytest.raises(TvUpdateError, match="version_must_increase"):
        updates.publish(*artifact(71, body=b"not the already verified bytes"))
    assert updates.publish(*second) == "published"
    assert updates.publish(*third) == "published"
    with pytest.raises(TvUpdateError, match="version_must_increase"):
        updates.publish(*second)
    assert sorted(updates.directory.glob("*.apk")) == sorted(apk(updates, entry[0]) for entry in (second, third))
    live, stream = updates.open_current()
    with stream:
        assert live == third[0] and stream.read() == third[1]


def test_current_stays_readable_during_interrupted_publish(updates, monkeypatch):
    first, second = artifact(71), artifact(72)
    updates.publish(*first)
    write = updates._write_state

    def interrupted(state):
        if state["current"] == second[0]:
            raise OSError("cancel before manifest switch")
        return write(state)

    with monkeypatch.context() as patch:
        patch.setattr(updates, "_write_state", interrupted)
        with pytest.raises(OSError, match="cancel"):
            updates.publish(*second)

    interrupted_output = updates.directory / ".tv-update-publish.part"
    interrupted_output.write_bytes(b"partial synthetic write")
    interrupted_output.chmod(0o600)
    reopened = TvUpdateStore(updates.state_dir)
    live, stream = reopened.open_current()
    with stream:
        assert live == first[0] and stream.read() == first[1]
        assert apk(reopened, second[0]).is_file()
        assert reopened.publish(*second) == "published"
        assert stream.seek(0) == 0 and stream.read() == first[1]
        assert not interrupted_output.exists()
    assert reopened.publish(*second) == "already_current"


def test_concurrent_publishers_serialize_monotonic_versions(updates):
    ready = threading.Barrier(2)

    def publish(package):
        ready.wait(timeout=5)
        try:
            return TvUpdateStore(updates.state_dir).publish(*package)
        except TvUpdateError as error:
            return str(error)

    first, second = artifact(71), artifact(72)
    with ThreadPoolExecutor(max_workers=2) as pool:
        smaller, larger = list(pool.map(publish, (first, second)))
    assert smaller in {"published", "tv_release_version_must_increase"}
    assert larger == "published"
    opened, stream = updates.open_current()
    with stream:
        assert opened == second[0] and stream.read() == second[1]


def test_retired_apk_cleanup_retries_after_commit(updates, monkeypatch):
    first, second, third = (artifact(version) for version in (71, 72, 73))
    updates.publish(*first)
    updates.publish(*second)

    def unavailable(_release):
        raise OSError("temporary cleanup failure")

    with monkeypatch.context() as patch:
        patch.setattr(updates, "_discard", unavailable)
        assert updates.publish(*third) == "published_cleanup_pending"
    assert apk(updates, first[0]).is_file()
    assert updates.publish(*third) == "already_current"
    assert not apk(updates, first[0]).exists()
    assert apk(updates, second[0]).is_file() and apk(updates, third[0]).is_file()


def test_invalid_release_refused_without_serving_previous(updates, tmp_path):
    first, second = artifact(71), artifact(72)
    updates.publish(*first)
    updates.publish(*second)
    current, stream = updates.open_current()
    with stream:
        assert current == second[0]
    file = apk(updates, second[0])
    file.write_bytes(b"X" * second[0].bytes)
    assert updates.open_current() is None
    file.unlink()
    file.symlink_to(apk(updates, first[0]))
    assert updates.open_current() is None
    assert apk(updates, first[0]).read_bytes() == first[1]
    updates.manifest.write_bytes(b'{"current":' + b"A" * 8193)
    assert updates.open_current() is None
    with pytest.raises(TvUpdateError, match="invalid_tv_release_state"):
        updates.publish(*artifact(73))


def test_apk_size_and_strict_types():
    with pytest.raises(TvUpdateError, match="invalid_tv_release"):
        Release(73, "0.73", MAX_APK_BYTES + 1, "a" * 64, "b" * 64)
    with pytest.raises(TvUpdateError, match="invalid_tv_release"):
        Release(True, "0.73", 12, "a" * 64, "b" * 64)
    with pytest.raises(TvUpdateError, match="invalid_tv_release"):
        Release(73, "0.73", True, "a" * 64, "b" * 64)


def test_nas_local_diagnostic_identifies_modes_without_modifying_artifact(updates):
    config = updates.state_dir.parent / "config.json"
    config.touch(mode=0o600)
    release, body = artifact(72)
    updates.publish(release, body)
    args = config, release.version_code, release.bytes, release.sha256
    assert inspect(*args)["code"] == "ready"
    local = apk(updates, release)
    local.chmod(0o664)
    with_directory = inspect(*args)
    assert with_directory["code"] == "nas_release_modes_not_private"
    assert with_directory["live_contract_code"] == "private_tv_release_file_required"
    assert with_directory["files"]["apk"]["mode"] == "0664"
    assert with_directory["current_matches_receipt"] and with_directory["apk_matches_receipt"]
    assert not with_directory["readable_by_live_contract"]
    assert local.stat().st_mode & 0o777 == 0o664
    assert local.read_bytes() == body


def test_nas_local_repair_requires_exact_owned_current_release(updates):
    config = updates.state_dir.parent / "config.json"
    config.touch(mode=0o600)
    release, body = artifact(72)
    updates.publish(release, body)
    local = apk(updates, release)
    local.chmod(0o664)
    updates.manifest.chmod(0o644)
    unrelated = updates.directory / "unrelated"
    unrelated.touch()
    unrelated.chmod(0o644)

    args = config, release.version_code, release.bytes, release.sha256
    assert repair_modes(config, 73, release.bytes, release.sha256)["code"] == "release_receipt_mismatch"
    alias = updates.directory / "outside-publication"
    os.link(local, alias)
    assert repair_modes(*args)["code"] == "nas_release_owned_entries_required"
    assert local.stat().st_mode & 0o777 == 0o664
    alias.unlink()

    repaired = repair_modes(*args)
    assert repaired["code"] == "ready" and repaired["private_modes_applied"]
    assert local.stat().st_mode & 0o777 == 0o600
    assert updates.manifest.stat().st_mode & 0o777 == 0o600
    assert unrelated.stat().st_mode & 0o777 == 0o644
    assert local.read_bytes() == body
    assert "private_modes_applied" not in repair_modes(*args)


def test_mounted_inbox_publishes_with_nas_owned_private_modes(updates):
    publisher = TvUpdateInbox(updates, AGENT_KEY)
    consumer = TvUpdateInbox(updates, AGENT_KEY)
    consumer.initialize()
    release, body = artifact(72)
    request = publisher.stage(release, body)
    assert publisher.stage(release, body) == request
    with pytest.raises(TvUpdateError, match="inbox_busy"):
        publisher.stage(*artifact(73))
    publisher.request.chmod(0o744)
    assert updates.open_current() is None
    assert consumer.consume_once()["state"] == "published"
    ack = publisher.wait_for_receipt(request, seconds=0)
    assert ack["sha256"] == release.sha256 and ack["version_code"] == release.version_code
    assert publisher.wait_for_receipt("0" * 32, seconds=0) is None
    assert not publisher.request.exists()
    assert consumer.directory.stat().st_mode & 0o777 == 0o700
    assert consumer.lock.stat().st_mode & 0o777 == 0o600
    assert consumer.ack.stat().st_mode & 0o777 == 0o600
    assert updates.directory.stat().st_mode & 0o777 == 0o700
    assert updates.manifest.stat().st_mode & 0o777 == 0o600
    assert apk(updates, release).stat().st_mode & 0o777 == 0o600


def test_inbox_rejects_wrong_authority_altered_body_and_other_signer(updates):
    consumer = TvUpdateInbox(updates, AGENT_KEY)
    consumer.initialize()
    first, second = artifact(72), artifact(73)
    updates.publish(*first)
    unauthorized = TvUpdateInbox(updates, "t" * 43)
    unapproved = unauthorized.stage(*second)
    assert consumer.consume_once() is None and not consumer.request.exists()
    assert unauthorized.wait_for_receipt(unapproved, seconds=0) is None

    tampered = consumer.stage(*second)
    with consumer.request.open("r+b") as stream:
        stream.seek(-1, os.SEEK_END)
        stream.write(b"x")
    assert consumer.consume_once()["code"] == "tv_release_bytes_mismatch"
    assert consumer.wait_for_receipt(tampered, seconds=0)["state"] == "refused"
    assert not consumer.request.exists()

    wrong_signer = consumer.stage(*artifact(73, signer="b"))
    assert consumer.consume_once()["code"] == "tv_release_signer_changed"
    assert consumer.wait_for_receipt(wrong_signer, seconds=0)["state"] == "refused"
    assert not consumer.request.exists()
    live, stream = updates.open_current()
    with stream:
        assert live == first[0] and stream.read() == first[1]


def test_nas_inbox_recovers_same_request_after_publisher_restart(updates, monkeypatch):
    inbox = TvUpdateInbox(updates, AGENT_KEY)
    inbox.initialize()
    release, body = artifact(72)
    staged = inbox.stage(release, body)
    write = tv_update_inbox._atomic_write

    def interrupt_receipt(path, *parts, **kwargs):
        if path == inbox.ack:
            raise OSError("synthetic stop after publish before reply")
        return write(path, *parts, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(tv_update_inbox, "_atomic_write", interrupt_receipt)
        with pytest.raises(OSError, match="synthetic stop"):
            inbox.consume_once()
    assert inbox.request.exists()
    assert inbox.wait_for_receipt(staged, seconds=0) is None
    restarted = TvUpdateInbox(TvUpdateStore(updates.state_dir), AGENT_KEY)
    restarted.initialize()
    assert restarted.consume_once()["state"] == "already_current"
    assert restarted.wait_for_receipt(staged, seconds=0)["state"] == "already_current"
    assert not restarted.request.exists()


def test_inbox_expiry_cleans_only_recognized_work_files(updates, monkeypatch):
    inbox = TvUpdateInbox(updates, AGENT_KEY)
    inbox.initialize()
    now = 10000
    monkeypatch.setattr(tv_update_inbox.time, "time", lambda: now)
    staged = inbox.stage(*artifact(72))
    leftovers = (inbox.stage_file, inbox.directory / ".receipt.part")
    unrelated = inbox.directory / "personal-file"
    for path in (*leftovers, unrelated):
        path.write_bytes(b"fixture file")
        os.utime(path, (now, now))
    now += REQUEST_LIFETIME + 1
    assert inbox.consume_once()["code"] == "tv_release_inbox_expired"
    assert inbox.wait_for_receipt(staged, seconds=0)["state"] == "refused"
    assert not inbox.request.exists() and not any(path.exists() for path in leftovers)
    assert unrelated.read_bytes() == b"fixture file"
    assert updates.open_current() is None


def test_fixed_authenticated_tv_download_and_guest_isolation(tmp_path):
    store = Store(tmp_path.resolve() / "nas", "https://guest.example.test:9443")
    server = Server(("127.0.0.1", 0), make_handler(store, AGENT_KEY, ROOT / "integrations/ktv_public/web"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tv = {"X-KTV-TV-Key": store.coordinator.tv_key}
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as client:
        try:
            for route in (UPDATE_INFO_PATH, UPDATE_APK_PATH):
                assert client.get(route, headers=tv).status_code == 404
            release, body = artifact(72)
            store.tv_updates.publish(release, body)
            for route in (UPDATE_INFO_PATH, UPDATE_APK_PATH):
                assert client.get(route).status_code == 404
                assert client.get(route, headers={"Authorization": "Bearer " + AGENT_KEY}).status_code == 404
                assert client.get(route, headers={"X-KTV-TV-Key": AGENT_KEY}).status_code == 401
                assert client.get("/ktv" + route, headers=tv).status_code == 404
                assert client.get(route, headers={**tv, "Origin": store.origin}).status_code == 403
                assert client.post(route, headers=tv).status_code == 405
            status = client.get(UPDATE_INFO_PATH, headers=tv)
            assert status.status_code == 200
            assert status.json() == release.public()
            assert "no-store" in status.headers["Cache-Control"]
            assert "certificate_sha256" not in status.json()
            recorded = '"' + release.sha256 + '"'
            assert client.get(UPDATE_APK_PATH, headers={**tv, "If-Match": '"' + "0" * 64 + '"'}).status_code == 412
            downloaded = client.get(UPDATE_APK_PATH, headers={**tv, "If-Match": recorded})
            assert downloaded.status_code == 200 and downloaded.content == body
            assert downloaded.headers["Content-Type"] == APK_CONTENT_TYPE
            assert downloaded.headers["ETag"] == recorded
            ranged = client.get(UPDATE_APK_PATH, headers={**tv, "Range": "bytes=1-4"})
            assert ranged.status_code == 206 and ranged.content == body[1:5]
            assert ranged.headers["Content-Range"] == f"bytes 1-4/{len(body)}"
            head = client.head(UPDATE_APK_PATH, headers=tv)
            assert head.status_code == 200 and not head.content and int(head.headers["Content-Length"]) == len(body)
            apk(store.tv_updates, release).write_bytes(b"X" * len(body))
            assert client.get(UPDATE_INFO_PATH, headers=tv).status_code == 404
            assert client.get(UPDATE_APK_PATH, headers=tv).status_code == 404
            assert client.get("/v1/status", headers=tv).status_code == 200
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            store.coordinator.close()


@pytest.mark.parametrize("legacy_audio_argument", [False, True])
def test_mac_command_preflights_main_without_audio_guard(updates, tmp_path, monkeypatch, capsys, legacy_audio_argument):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    command = importlib.import_module("publish_ktv_tv_update")
    (updates.state_dir / "state.sqlite").touch()
    release, body = artifact(72)
    worker_config = tmp_path / "nas.json"
    worker_config.write_text('{"agent_key":"' + AGENT_KEY + '"}')
    worker_config.chmod(0o600)
    header = {"version": release.version_name, "version_code": release.version_code}
    retained = tmp_path.resolve() / "retained"
    candidate = tmp_path.resolve() / "build"
    candidate.mkdir()
    (candidate / "friday-ktv-0.72.apk").write_bytes(body)
    monkeypatch.setattr(command, "validate_main_manifest", lambda _: header)

    environment = {"JAVA_HOME": "SYNTHETIC"}
    monkeypatch.setattr(command, "java_environment", lambda java: environment)

    def verify(role, build_dir, sdk, actual_environment, root):
        assert role == "main" and build_dir == candidate and actual_environment is environment
        assert not retained.exists()
        return SimpleNamespace(
            identity=header, body=body, receipt={"sha256": release.sha256},
            certificate_sha256=release.certificate_sha256,
        )

    monkeypatch.setattr(command, "verify_build", verify)
    args = ["--nas-state", str(updates.state_dir), "--build-dir", str(candidate)]
    if legacy_audio_argument:
        args += ["--audio-build-dir", str(retained)]
    assert command.main(args) == 0
    assert '"state": "planned"' in capsys.readouterr().out
    assert not updates.directory.exists()
    inbox = TvUpdateInbox(updates, AGENT_KEY)
    inbox.initialize()
    monkeypatch.setattr(TvUpdateInbox, "wait_for_receipt", lambda *args: inbox.consume_once())
    assert command.main([*args, "--worker-config", str(worker_config), "--apply"]) == 0
    capture = capsys.readouterr()
    assert '"state": "published"' in capture.out and not capture.err
    assert {"sha256", "version_code"} <= set(release.public())
    assert release.certificate_sha256 not in capture.out
    assert AGENT_KEY not in capture.out
    live, stream = updates.open_current()
    with stream:
        assert live == release and stream.read() == body
