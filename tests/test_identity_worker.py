"""Actual worker protocol and original face rules with a synthetic provider."""

import base64
import secrets
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from homeplane.people.identity_session import IdentityPeer
from homeplane.people.identity_worker import frame_arguments, serve
from homeplane.people.live import FaceIdentityPipeline, IdentityStore
from homeplane.recognition.artifacts import SFACE_SHA256
from homeplane.recognition.contracts import Embedding


def test_original_pipeline_enrollment_and_discard_use_the_fixed_worker_protocol(tmp_path, monkeypatch):
    private = tmp_path / "identity"
    private.mkdir(mode=0o700)
    path = private / "identities.sqlite"
    parent, child = socket.socketpair()
    peer = IdentityPeer.__new__(IdentityPeer)
    peer.connection, peer.nonce, peer.sequence = parent, secrets.token_hex(16), 0
    provider = SimpleNamespace(status=SimpleNamespace(state="ready"), close=Mock())
    provider.detect = Mock(
        side_effect=lambda _: SimpleNamespace(
            rows=[
                (
                    150.0,
                    140.0,
                    80.0,
                    90.0,
                    170.0,
                    165.0,
                    210.0,
                    165.0,
                    190.0,
                    185.0,
                    175.0,
                    210.0,
                    205.0,
                    210.0,
                    0.99,
                )
            ]
        )
    )
    provider.embed_slot = lambda *_: SimpleNamespace(
        embedding=Embedding("face", SFACE_SHA256, (1.0,) + (0.0,) * 127, False)
    )
    monkeypatch.setattr(cv2, "Laplacian", lambda *_: SimpleNamespace(var=lambda: 100.0))

    def create(value):
        store = IdentityStore(path)
        pipeline = FaceIdentityPipeline(store, value["camera"], value["session"], private)
        pipeline.provider, pipeline.reason = provider, "synthetic"
        return pipeline, store

    errors = []

    def run():
        try:
            serve(child, pipeline_factory=create)
        except EOFError:
            pass
        except BaseException as error:
            errors.append(type(error).__name__)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert (
            peer.call(
                "init", dict(camera="camera", session="source", database=str(path), models=str(private)), 0.5
            )["reason"]
            == "synthetic"
        )
        jpeg = cv2.imencode(".jpg", np.zeros((720, 1280, 3), np.uint8))[1].tobytes()
        frame = dict(
            jpeg=base64.b64encode(jpeg).decode(),
            people=[dict(id=1, box=dict(x=0, y=0, w=1, h=1))],
            faces=[],
            moving=False,
            sample=True,
        )
        result = None
        expired = peer.call("frame", dict(frame, captured_at=time.monotonic() - 2), 0.5)
        assert expired["rows"] == [] and expired["diagnostics"]["frames"] == 0
        provider.detect.assert_not_called()
        for index in range(3):
            if index:
                time.sleep(0.51)
            result = peer.call("frame", dict(frame, captured_at=time.monotonic()), 0.5)
        assert result and result["rows"][0]["enroll_ready"] is True
        enrolled = peer.call(
            "enroll", dict(track="1", person="synthetic_person", expires=time.monotonic() + 1), 0.5
        )
        assert enrolled["status"] == "enrolled" and enrolled["samples"] == 3
        peer.call("discard", {}, 0.5)
        with pytest.raises(ValueError, match="identity_samples_required"):
            peer.call("enroll", dict(track="1", person="synthetic_person", expires=time.monotonic() + 1), 0.5)
    finally:
        parent.close()
        worker.join(timeout=2)
    assert not worker.is_alive() and not errors
    provider.close.assert_called_once()
    store = IdentityStore(path)
    try:
        assert "synthetic_person" in store.registered()
        assert not any(row["present"] for row in store.presence())
    finally:
        store.close()


@pytest.mark.parametrize(
    "edit",
    [
        dict(captured_at=float("nan")),
        dict(captured_at=0),
        dict(sample=1),
        dict(jpeg="not-jpeg"),
        dict(people=[{}] * 17),
        dict(faces=[{}] * 33),
        dict(unexpected="private"),
    ],
)
def test_worker_refuses_unbounded_or_stale_frames_before_any_model_call(edit):
    value = dict(
        jpeg=base64.b64encode(b"\xff\xd8SYNTHETIC\xff\xd9").decode(),
        people=[],
        faces=[],
        moving=False,
        sample=True,
        captured_at=time.monotonic(),
    )
    value.update(edit)
    with pytest.raises(ValueError):
        frame_arguments(value)
