"""Synthetic contract tests only: no optional native imports or real model bytes."""

import hashlib
import importlib
import math
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from homeplane.recognition.artifacts import LocalArtifact, speaker_metadata
from homeplane.recognition.contracts import (
    Candidate,
    Embedding,
    EmbeddingResult,
    Status,
    VerificationPolicy,
    compare_candidates,
)
from homeplane.recognition.face import FaceQuality, OpenCVFaceProvider
from homeplane.recognition.speaker import SherpaSpeakerProvider, SpeakerQuality


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def string_field(number, value):
    value = value.encode() if isinstance(value, str) else value
    return varint(number << 3 | 2) + varint(len(value)) + value


def metadata_bytes(entries):
    # This contains only synthetic protobuf metadata, not a usable model graph.
    prefix = b"\x08\x08" + string_field(7, b"SYNTHETIC-GRAPH-NOT-A-MODEL")
    return prefix + b"".join(string_field(14, string_field(1, k) + string_field(2, v)) for k, v in entries)


GOOD_METADATA = [("framework", "wespeaker"), ("sample_rate", "16000"), ("output_dim", "256"),
                 ("normalize_samples", "0"), ("language", "English")]
FACE_QUALITY = FaceQuality(True, True, True)
SPEAKER_QUALITY = SpeakerQuality(True, True, True)
ROW = (10, 10, 100, 100, 30, 35, 80, 35, 55, 60, 35, 85, 75, 85, 0.99)


class FakeDetector:
    def __init__(self):
        self.response = np.array([ROW], dtype=np.float32)
        self.sizes = []
        self.calls = 0

    def setInputSize(self, size):
        self.sizes.append(size)

    def detect(self, image):
        self.calls += 1
        return 1, self.response


class FakeRecognizer:
    def __init__(self):
        self.calls = 0
        self.output = np.ones((1, 128), dtype=np.float32)

    def alignCrop(self, image, bbox):
        assert bbox.shape == (15,) and bbox.dtype == np.float32
        self.calls += 1
        return np.zeros((112, 112, 3), dtype=np.uint8)

    def feature(self, aligned):
        return self.output


class FakeCV:
    def __init__(self):
        self.detector, self.recognizer = FakeDetector(), FakeRecognizer()
        self.dnn = SimpleNamespace(DNN_BACKEND_OPENCV=3, DNN_TARGET_CPU=0)
        self.FaceDetectorYN = SimpleNamespace(create=self.create_detector)
        self.FaceRecognizerSF = SimpleNamespace(create=self.create_recognizer)
        self.creations = []

    def create_detector(self, *, model, config, input_size, score_threshold, nms_threshold,
                        top_k, backend_id, target_id):
        assert config == "" and input_size == (320, 320) and top_k == 64
        assert score_threshold == 0.9 and nms_threshold == 0.3
        assert backend_id == 3 and target_id == 0
        self.creations.append(("YuNet", model))
        return self.detector

    def create_recognizer(self, *, model, config, backend_id, target_id):
        assert config == "" and backend_id == 3 and target_id == 0
        self.creations.append(("SFace", model))
        return self.recognizer


class FakeStream:
    def __init__(self):
        self.samples = None
        self.finished = False

    def accept_waveform(self, *, sample_rate, waveform):
        assert sample_rate == 16000 and waveform.dtype == np.float32
        self.samples = waveform

    def input_finished(self):
        self.finished = True


class FakeExtractor:
    dim = 256

    def __init__(self):
        self.streams = []
        self.ready = True
        self.output = np.ones(256, dtype=np.float32)

    def create_stream(self):
        stream = FakeStream()
        self.streams.append(stream)
        return stream

    def is_ready(self, stream):
        assert stream.finished
        return self.ready

    def compute(self, stream):
        assert stream.finished and stream.samples is not None
        return self.output


class FakeSherpa:
    def __init__(self):
        self.extractor = FakeExtractor()
        self.calls = []
        self.valid = True

    def SpeakerEmbeddingExtractorConfig(self, *, model, num_threads, debug, provider):
        assert num_threads == 1 and debug is False and provider == "cpu"
        self.calls.append(model)
        return SimpleNamespace(validate=lambda: self.valid)

    def SpeakerEmbeddingExtractor(self, config):
        return self.extractor


class RecognitionContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.face_artifact = self.artifact("synthetic-face.onnx", b"SYNTHETIC-NOT-A-MODEL")
        self.speaker_artifact = self.artifact("synthetic-speaker.onnx", metadata_bytes(GOOD_METADATA))
        self.image = np.full((200, 320, 3), 127, dtype=np.uint8)
        self.audio = np.full(32000, 0.1, dtype=np.float32)

    def tearDown(self):
        self.temp.cleanup()

    def artifact(self, name, data):
        target = self.root / name
        target.write_bytes(data)
        return LocalArtifact(target, hashlib.sha256(data).hexdigest())

    def face(self, recognition=True):
        fake = FakeCV()
        provider = OpenCVFaceProvider(self.face_artifact, self.face_artifact if recognition else None,
                                      enabled=True)
        self.assertEqual(provider.activate(_module=fake).state, "ready")
        return provider, fake

    def speaker(self):
        fake = FakeSherpa()
        provider = SherpaSpeakerProvider(self.speaker_artifact, enabled=True)
        self.assertEqual(provider.activate(_module=fake).state, "ready")
        return provider, fake

    def test_import_and_default_off_never_load_native_modules_or_artifacts(self):
        initial_modules = {name: name in sys.modules for name in ("cv2", "sherpa_onnx")}
        with patch("importlib.import_module", side_effect=AssertionError("native import")), patch.object(
                LocalArtifact, "verified_path", side_effect=AssertionError("artifact read")):
            importlib.reload(sys.modules["homeplane.recognition.face"])
            importlib.reload(sys.modules["homeplane.recognition.speaker"])
            face = OpenCVFaceProvider(self.face_artifact, self.face_artifact)
            voice = SherpaSpeakerProvider(self.speaker_artifact)
            self.assertEqual(face.activate().state, "disabled")
            self.assertEqual(voice.activate().state, "disabled")
            self.assertEqual(face.detect(self.image).status.state, "disabled")
            self.assertEqual(voice.embed(self.audio, 16000, SPEAKER_QUALITY).status.state, "disabled")
        self.assertEqual(initial_modules, {name: name in sys.modules for name in initial_modules})

    def test_explicit_activation_missing_weights_and_dependency_fail_closed(self):
        for provider in (OpenCVFaceProvider(enabled=True), SherpaSpeakerProvider(enabled=True)):
            self.assertEqual(provider.activate().reason, "weights_not_configured")
        for provider in (OpenCVFaceProvider(self.face_artifact, enabled=True),
                         SherpaSpeakerProvider(self.speaker_artifact, enabled=True)):
            with patch("importlib.import_module", side_effect=ImportError("secret path")):
                if isinstance(provider, OpenCVFaceProvider):
                    # Public model pin is separately checked; synthetic bytes
                    # are admitted only with an injected synthetic module.
                    fake = FakeCV()
                    self.assertEqual(provider.activate(_module=fake).reason, "optional_dependency_missing")
                else:
                    self.assertEqual(provider.activate().reason, "optional_dependency_missing")
                self.assertNotIn("secret", repr(provider.status))

    def test_artifact_hash_symlink_and_budget_rejected_before_native_constructor(self):
        wrong = LocalArtifact(self.face_artifact.path, "a" * 64)
        fake = FakeCV()
        self.assertEqual(OpenCVFaceProvider(wrong, enabled=True).activate(_module=fake).state, "unavailable")
        self.assertFalse(fake.creations)
        link = self.root / "link.onnx"
        link.symlink_to(self.face_artifact.path)
        with self.assertRaisesRegex(ValueError, "artifact_symlink"):
            LocalArtifact(link, self.face_artifact.sha256).verified_path()
        large = self.root / "large.onnx"
        with large.open("wb") as source:
            source.truncate(81 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "artifact_size_or_type"):
            LocalArtifact(large, "a" * 64).verified_path()

    def test_face_contract_detect_align_extract_and_no_embedding_repr(self):
        provider, fake = self.face()
        detections = provider.detect(self.image)
        self.assertEqual(detections.status.state, "ready")
        result = provider.embed(self.image, detections, FACE_QUALITY)
        self.assertEqual(result.status.state, "ready")
        self.assertTrue(result.embedding.simulated)
        self.assertEqual(len(result.embedding.values), 128)
        self.assertEqual(fake.detector.sizes, [(320, 200)])
        self.assertEqual(fake.recognizer.calls, 1)
        self.assertNotIn("values=", repr(result.embedding))
        self.assertNotIn("rows=", repr(detections))
        self.assertNotIn("frame_digest=", repr(detections))

    def test_face_detection_and_recognition_availability_are_separate(self):
        provider, _ = self.face(recognition=False)
        detections = provider.detect(self.image)
        self.assertEqual(detections.status.state, "ready")
        self.assertEqual(provider.embed(self.image, detections, FACE_QUALITY).status.reason,
                         "recognition_weights_not_configured")

    def test_face_invalid_input_never_reaches_native(self):
        provider, fake = self.face()
        for image in (np.zeros((721, 20, 3), dtype=np.uint8), self.image.astype(np.float32),
                      self.image[:, ::2], np.zeros((30, 30), dtype=np.uint8)):
            self.assertEqual(provider.detect(image).status.state, "unknown")
        self.assertEqual(fake.detector.calls, 0)

    def test_face_empty_bad_geometry_nan_and_overbudget_outputs(self):
        provider, fake = self.face()
        for response, state in ((None, "unknown"), (np.zeros((0, 15)), "unknown"),
                                (np.zeros((1, 14)), "unknown"), (np.tile(ROW, (9, 1)), "ambiguous"),
                                (np.array([[math.nan] + list(ROW[1:])]), "unknown"),
                                (np.array([[-1] + list(ROW[1:])]), "unknown")):
            fake.detector.response = response
            self.assertEqual(provider.detect(self.image).status.state, state)

    def test_face_multiple_low_quality_small_or_wrong_frame_never_extracts(self):
        provider, fake = self.face()
        fake.detector.response = np.tile(ROW, (2, 1))
        detections = provider.detect(self.image)
        self.assertEqual(provider.embed(self.image, detections, FACE_QUALITY).status.state, "ambiguous")
        fake.detector.response = np.array([ROW])
        detections = provider.detect(self.image)
        self.assertEqual(provider.embed(self.image, detections, FaceQuality()).status.state, "unknown")
        changed = self.image.copy()
        changed[0, 0, 0] = 1
        self.assertEqual(provider.embed(changed, detections, FACE_QUALITY).status.reason, "detection_frame_mismatch")
        small = list(ROW)
        small[2] = 32
        fake.detector.response = np.array([small])
        self.assertEqual(provider.embed(self.image, provider.detect(self.image), FACE_QUALITY).status.state, "unknown")
        self.assertEqual(fake.recognizer.calls, 0)

    def test_face_same_bytes_different_geometry_rejected_before_alignment(self):
        provider, fake = self.face()
        detections = provider.detect(self.image)
        reshaped = self.image.reshape(160, 400, 3)
        self.assertEqual(self.image.tobytes(), reshaped.tobytes())
        self.assertNotEqual(self.image.shape, reshaped.shape)
        self.assertNotEqual(self.image.strides, reshaped.strides)
        self.assertTrue(reshaped.flags.c_contiguous)
        self.assertTrue(provider._row_valid(detections.rows[0], 400, 160))
        result = provider.embed(reshaped, detections, FACE_QUALITY)
        self.assertEqual(result.status.state, "unknown")
        self.assertEqual(result.status.reason, "detection_frame_mismatch")
        self.assertEqual(fake.recognizer.calls, 0)

    def test_face_identical_geometry_copy_and_fresh_reshaped_detection_are_valid(self):
        provider, fake = self.face()
        detections = provider.detect(self.image)
        copied = self.image.copy()
        self.assertEqual(copied.shape, self.image.shape)
        self.assertEqual(copied.dtype, self.image.dtype)
        self.assertEqual(copied.strides, self.image.strides)
        self.assertEqual(provider.embed(copied, detections, FACE_QUALITY).status.state, "ready")
        reshaped = self.image.reshape(160, 400, 3)
        fresh = provider.detect(reshaped)
        result = provider.embed(reshaped, fresh, FACE_QUALITY)
        self.assertEqual(result.status.state, "ready")
        self.assertTrue(result.embedding.simulated)
        self.assertEqual(fake.detector.sizes, [(320, 200), (400, 160)])
        self.assertEqual(fake.recognizer.calls, 2)

    def test_face_dtype_and_noncontiguous_stride_changes_reject_before_alignment(self):
        provider, fake = self.face()
        detections = provider.detect(self.image)
        changed_dtype = self.image.view(np.int8)
        changed_stride = self.image[:, ::-1]
        for changed in (changed_dtype, changed_stride):
            self.assertEqual(changed.shape, self.image.shape)
            self.assertEqual(changed.tobytes(), self.image.tobytes())
            result = provider.embed(changed, detections, FACE_QUALITY)
            self.assertEqual(result.status.state, "unknown")
            self.assertEqual(result.status.reason, "invalid_or_oversized_bgr_image")
        self.assertEqual(fake.recognizer.calls, 0)

    def test_face_native_failure_and_invalid_embedding_remain_explicit(self):
        provider, fake = self.face()
        for value in (np.ones((1, 127)), np.full((1, 128), np.nan), np.zeros((1, 128))):
            fake.recognizer.output = value
            self.assertEqual(provider.embed(self.image, provider.detect(self.image), FACE_QUALITY).status.state, "unknown")
        with patch.object(fake.detector, "detect", side_effect=RuntimeError("private content")):
            result = provider.detect(self.image)
            self.assertEqual(result.status.reason, "native_detection_failed")
            self.assertNotIn("private", repr(result))

    def test_speaker_metadata_preflight_missing_wrong_duplicate_truncated_and_budget(self):
        payloads = [metadata_bytes([]), metadata_bytes([("framework", "other")]),
                    metadata_bytes(GOOD_METADATA + [("output_dim", "256")]), b"\x72\xff",
                    string_field(14, b"a" * 4097), b"\x00", b"\x0b", b"\x09" + struct.pack("d", 1)[:-1]]
        for index, data in enumerate(payloads):
            artifact = self.artifact(f"bad-{index}.onnx", data)
            fake = FakeSherpa()
            status = SherpaSpeakerProvider(artifact, enabled=True).activate(_module=fake)
            self.assertEqual(status.state, "unavailable")
            self.assertFalse(fake.calls)
        self.assertEqual(speaker_metadata(str(self.speaker_artifact.path)), dict(GOOD_METADATA))

    def test_all_mandatory_sherpa_metadata_and_constructor_validation(self):
        for index in range(len(GOOD_METADATA)):
            artifact = self.artifact(f"missing-{index}.onnx", metadata_bytes(
                GOOD_METADATA[:index] + GOOD_METADATA[index + 1:]))
            fake = FakeSherpa()
            self.assertEqual(SherpaSpeakerProvider(artifact, enabled=True).activate(_module=fake).reason,
                             "speaker_metadata_unqualified")
            self.assertFalse(fake.calls)
        for extra in ([("feature_normalize_type", "unsupported")], [("sample_rate", "48000")]):
            entries = [(k, v) for k, v in GOOD_METADATA if k not in dict(extra)] + extra
            artifact = self.artifact("wrong-meta.onnx", metadata_bytes(entries))
            self.assertEqual(SherpaSpeakerProvider(artifact, enabled=True).activate(_module=FakeSherpa()).state,
                             "unavailable")
        fake = FakeSherpa()
        fake.valid = False
        self.assertEqual(SherpaSpeakerProvider(self.speaker_artifact, enabled=True).activate(_module=fake).reason,
                         "native_configuration_invalid")
        fake.valid = True
        fake.extractor.dim = 512
        self.assertEqual(SherpaSpeakerProvider(self.speaker_artifact, enabled=True).activate(_module=fake).reason,
                         "embedding_dimension_mismatch")

    def test_speaker_exact_constructor_stream_and_embedding_contract(self):
        provider, fake = self.speaker()
        result = provider.embed(self.audio, 16000, SPEAKER_QUALITY)
        self.assertEqual(result.status.state, "ready")
        self.assertEqual(result.embedding.modality, "speaker")
        self.assertEqual(len(result.embedding.values), 256)
        self.assertTrue(result.embedding.simulated)
        self.assertEqual(len(fake.extractor.streams), 1)
        self.assertTrue(fake.extractor.streams[0].finished)

    def test_speaker_short_overlap_silence_clip_nonfinite_shape_and_rate_do_not_infer(self):
        provider, fake = self.speaker()
        bad_inputs = [(self.audio[:16000], 16000, SPEAKER_QUALITY),
                      (np.zeros(128001, dtype=np.float32), 16000, SPEAKER_QUALITY),
                      (self.audio, 48000, SPEAKER_QUALITY), (self.audio, 16000.0, SPEAKER_QUALITY),
                      (self.audio, 16000, SpeakerQuality(True, False, True)),
                      (self.audio, 16000, SpeakerQuality()),
                      (np.zeros(32000, dtype=np.float32), 16000, SPEAKER_QUALITY),
                      (np.ones(32000, dtype=np.float32), 16000, SPEAKER_QUALITY),
                      (np.full(32000, np.inf, dtype=np.float32), 16000, SPEAKER_QUALITY),
                      (np.full(32000, 1.1, dtype=np.float32), 16000, SPEAKER_QUALITY),
                      (self.audio.reshape(16000, 2), 16000, SPEAKER_QUALITY),
                      (self.audio.astype(np.float64), 16000, SPEAKER_QUALITY)]
        for samples, rate, quality in bad_inputs:
            self.assertEqual(provider.embed(samples, rate, quality).status.state, "unknown")
        self.assertFalse(fake.extractor.streams)

    def test_speaker_readiness_dimension_and_native_exceptions(self):
        provider, fake = self.speaker()
        fake.extractor.ready = False
        self.assertEqual(provider.embed(self.audio, 16000, SPEAKER_QUALITY).status.reason,
                         "insufficient_speech_features")
        fake.extractor.ready = True
        for value in (np.ones(255), np.full(256, np.nan), np.zeros(256)):
            fake.extractor.output = value
            self.assertEqual(provider.embed(self.audio, 16000, SPEAKER_QUALITY).status.state, "unknown")
        with patch.object(fake.extractor, "compute", side_effect=RuntimeError("private audio")):
            result = provider.embed(self.audio, 16000, SPEAKER_QUALITY)
            self.assertEqual(result.status.reason, "native_embedding_failed")
            self.assertNotIn("private", repr(result))

    def test_provider_busy_close_and_reactivation(self):
        face, _ = self.face()
        speaker, _ = self.speaker()
        face._gate.acquire()
        speaker._gate.acquire()
        try:
            self.assertEqual(face.detect(self.image).status.reason, "provider_busy")
            self.assertEqual(speaker.embed(self.audio, 16000, SPEAKER_QUALITY).status.reason, "provider_busy")
        finally:
            face._gate.release()
            speaker._gate.release()
        face.close()
        speaker.close()
        self.assertEqual(face.detect(self.image).status.state, "disabled")
        self.assertEqual(speaker.embed(self.audio, 16000, SPEAKER_QUALITY).status.state, "disabled")
        self.assertIsNone(face._recognizer)
        self.assertIsNone(speaker._extractor)

    def test_verification_open_set_unknown_ambiguous_and_model_binding(self):
        fingerprint = "a" * 64
        vector = Embedding("face", fingerprint, (1.0,) + (0.0,) * 127)
        result = EmbeddingResult(Status("ready", "synthetic"), vector)
        a = Candidate("synthetic_a", vector)
        policy = VerificationPolicy("face", fingerprint, 0.8, 0.1)
        self.assertEqual(compare_candidates(result, (a,), policy).status.reason, "threshold_unqualified")
        policy = VerificationPolicy("face", fingerprint, 0.8, 0.1, True, "synthetic_calibration")
        matched = compare_candidates(result, (a,), policy)
        self.assertEqual(matched.reference, "synthetic_a")
        self.assertFalse(matched.security_authentication)
        self.assertTrue(matched.simulated)
        other = Candidate("synthetic_b", Embedding("face", fingerprint, (0.0, 1.0) + (0.0,) * 126))
        self.assertEqual(compare_candidates(result, (other,), policy).status.state, "unknown")
        tie = Candidate("synthetic_tie", vector)
        self.assertEqual(compare_candidates(result, (a, tie), policy).status.state, "ambiguous")
        self.assertIsNone(compare_candidates(result, (a, tie), policy).reference)
        mismatch = Candidate("synthetic_mismatch", Embedding("face", "b" * 64, vector.values))
        self.assertEqual(compare_candidates(result, (a, mismatch), policy).status.reason, "model_mismatch")
        production = Candidate("production", Embedding("face", fingerprint, vector.values, False))
        self.assertEqual(compare_candidates(result, (production,), policy).status.reason,
                         "simulation_provenance_mismatch")
        self.assertEqual(compare_candidates(result, (a, a), policy).status.reason, "duplicate_candidate")
        self.assertEqual(compare_candidates(result, (), policy).status.state, "unknown")
        self.assertEqual(compare_candidates(EmbeddingResult(Status("unknown", "quality")), (a,), policy).status.state,
                         "unknown")

    def test_embedding_and_policy_invalid_numeric_values_rejected(self):
        for vector in ((0.0,) * 128, (math.inf,) + (1.0,) * 127, (True,) + (1.0,) * 127,
                       (1e7,) + (1.0,) * 127, (1.0,) * 127):
            with self.assertRaises(ValueError):
                Embedding("face", "a" * 64, vector)
        for threshold, margin in ((math.nan, 0.1), (1.1, 0.1), (0.8, 0), (0.8, math.inf), (True, 0.1)):
            with self.assertRaises(ValueError):
                VerificationPolicy("face", "a" * 64, threshold, margin)
        with self.assertRaises(ValueError):
            VerificationPolicy("face", "a" * 64, 0.8, 0.1, True)
        with self.assertRaises(ValueError):
            SherpaSpeakerProvider(source_sha256="b" * 64)

    def test_no_embedding_on_unready_result_and_bounded_candidate_inputs(self):
        vector = Embedding("face", "a" * 64, (1.0,) * 128)
        with self.assertRaises(ValueError):
            EmbeddingResult(Status("ready", "synthetic"))
        with self.assertRaises(ValueError):
            EmbeddingResult(Status("unknown", "synthetic"), vector)
        policy = VerificationPolicy("face", "a" * 64, 0.8, 0.1, True, "synthetic")
        result = EmbeddingResult(Status("ready", "synthetic"), vector)
        candidates = tuple(Candidate(f"synthetic_{i}", vector) for i in range(33))
        self.assertEqual(compare_candidates(result, candidates, policy).status.state, "unknown")
        self.assertEqual(compare_candidates(result, (None,), policy).status.reason, "invalid_candidate")
        with self.assertRaises(ValueError):
            Status("ready", "unsafe private path")


if __name__ == "__main__":
    unittest.main()
