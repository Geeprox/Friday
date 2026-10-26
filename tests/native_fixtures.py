"""Explicit synthetic OpenCV API protocol; no activation/artifact read or model."""

from pathlib import Path
from uuid import uuid4

import numpy as np

from homeplane.recognition.artifacts import LocalArtifact
from homeplane.recognition.contracts import Status
from homeplane.recognition.face import OpenCVFaceProvider
from homeplane.recognition_consumer import NativeMetadataOwnerContext, RecognitionConsumer
from homeplane.recognition_contracts import FaceFrameProvenance, FaceSlotQuality, RecognitionSession


class Clock:
    def __init__(self):
        self.now = 10.

    def __call__(self):
        return self.now


def row(x=16., y=16.):
    return (x, y, 80., 80., x+20., y+25., x+60., y+25., x+40., y+40.,
        x+25., y+60., x+55., y+60., .99)


class DetectorProtocolFixture:
    def __init__(self, rows):
        self.response = np.array(rows, dtype=np.float32)
        self.calls = 0
        self.last_owned = None

    def setInputSize(self, size):
        self.size = size

    def detect(self, image):
        self.calls += 1
        self.last_owned = image  # Observe post-completion zeroing; synthetic pixels only.
        return len(self.response), self.response


class RecognizerProtocolFixture:
    def __init__(self):
        self.calls = 0
        self.rows = []
        self.entered = self.release = None
        self.after = None
        self.fail = False

    def alignCrop(self, image, bbox):
        assert bbox.shape == (15,) and bbox.dtype == np.float32
        self.calls += 1
        self.rows.append(float(bbox[0]))
        return np.zeros((112, 112, 3), dtype=np.uint8)

    def feature(self, aligned):
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(2.)
        if self.after is not None:
            self.after()
        if self.fail:
            raise RuntimeError('synthetic_provider_detail_must_not_escape')
        return np.ones((1, 128), dtype=np.float32)  # Predetermined synthetic vector, no model inference.


def qualified_slots(image, detections):
    return tuple(FaceSlotQuality(evidence='synthetic_contract_fixture',
        evidence_reference='explicit_synthetic_quality', qualified=True,
        pose_ok=True, sharpness_ok=True, occlusion_ok=True) for _ in detections.rows)


def setup(*, rows=None, qualities=qualified_slots, seconds=2.):
    clock = Clock()
    session = RecognitionSession(session_id=str(uuid4()), generation=1,
        owner_operation='synthetic_v3_fixture', camera_id='synthetic-camera', modality='face',
        deadline_monotonic=100., seconds=seconds, simulated=True)
    provenance = FaceFrameProvenance(detector_model_sha256='b'*64, embedding_model_sha256='a'*64,
        evidence='synthetic_contract_fixture', artifact_evidence='synthetic_contract_fixture',
        runtime_version='synthetic_contract_fixture')
    # Inert path records only: no files are created, read, verified or activated.
    provider = OpenCVFaceProvider(LocalArtifact(Path('/tmp/synthetic-no-file-yunet.onnx'), 'b'*64),
        LocalArtifact(Path('/tmp/synthetic-no-file-sface.onnx'), 'a'*64), enabled=True)
    detector, recognizer = DetectorProtocolFixture(rows or (row(), row(180., 50.))), RecognizerProtocolFixture()
    provider._np, provider._detector, provider._recognizer = np, detector, recognizer
    provider._simulated = True
    provider.status = Status('ready', 'detection_and_embedding')
    context = NativeMetadataOwnerContext(session, provider, provenance, qualities, _fixture_consent=True)
    consumer = RecognitionConsumer(_owner_context=context, clock=clock)
    assert consumer.active
    return consumer, context, clock, detector, recognizer


async def send(consumer, clock, t=0., *, value=7, source=1790000000000, shape=(240, 320, 3)):
    clock.now = 10.+t
    image = np.full(shape, value, dtype=np.uint8)
    await consumer.video('synthetic-camera', image, 0, source_timestamp=source, received_monotonic=clock.now)
    return image
