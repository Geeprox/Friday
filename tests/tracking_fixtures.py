"""Explicitly imported fixtures for only this finite tracking test suite."""

from uuid import uuid4

import pytest

from homeplane.recognition.contracts import Embedding
from homeplane.recognition_contracts import (
    ObservationQuality,
    ObservationTiming,
    RecognitionProvenance,
    RecognitionSession,
)
from homeplane.recognition_timing import CaptureStamp
from homeplane.recognition_tracking import Detection, ObservationTracker, TrackingPolicy


def vector(axis=0, *, sha="a"*64, simulated=True, modality="face"):
    size = 128 if modality == "face" else 256
    return Embedding(modality, sha, tuple(1. if i == axis else 0. for i in range(size)), simulated)


def quality(**changes):
    values = dict(qualified=True, pose_ok=True, sharpness_ok=True, occlusion_ok=True)
    values.update(changes)
    return ObservationQuality(**values)


def detection(x=.2, *, y=.2, width=.2, height=.2, embedding=None, q=None, confidence=.95):
    return Detection((x, y, width, height), confidence, q or quality(), embedding)


@pytest.fixture
def scope():
    session = RecognitionSession(session_id=str(uuid4()), generation=1, owner_operation="synthetic_track",
        camera_id="synthetic-camera", modality="face", deadline_monotonic=18., seconds=8., simulated=True)
    provenance = RecognitionProvenance(provider="opencv_yunet_sface", embedding_model_sha256="a"*64,
        detector_model_sha256="b"*64)
    return session, provenance


def tracker(scope, **changes):
    return ObservationTracker(*scope, started_monotonic=10., policy=TrackingPolicy(**changes))


def update(tracker, values, t=0., *, capture=None, clock_id="capture_A", source=None, **kw):
    timing = ObservationTiming(observed_monotonic=10.+t, source_timestamp=source)
    return tracker.update(tracker.session, tuple(values), timing,
        CaptureStamp(100.+t if capture is None else capture, clock_id), now_monotonic=10.+t, **kw)
