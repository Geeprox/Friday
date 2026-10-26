"""YuNet detection and SFace embeddings via explicit OpenCV public APIs."""

import hashlib
import importlib
import math
import struct
import threading
from dataclasses import dataclass, field

from .artifacts import SFACE_SHA256, YUNET_SHA256, LocalArtifact
from .contracts import Embedding, EmbeddingResult, Status


@dataclass(frozen=True)
class FaceQuality:
    pose_ok: bool | None = None
    sharpness_ok: bool | None = None
    occlusion_ok: bool | None = None

    @property
    def qualified(self):
        return all(v is True for v in (self.pose_ok, self.sharpness_ok, self.occlusion_ok))


@dataclass(frozen=True)
class FaceDetections:
    status: Status
    rows: tuple[tuple[float, ...], ...] = field(default=(), repr=False)
    frame_digest: str | None = field(default=None, repr=False)
    simulated: bool = True


class OpenCVFaceProvider:
    """No capture, model discovery, identity templates or initialization on import.

    activate() is the only native model-loading boundary. The _module parameter
    permits trusted synthetic contract fakes, never a request-selected backend.
    """

    def __init__(self, detector: LocalArtifact | None = None, recognizer: LocalArtifact | None = None,
                 *, enabled: bool = False):
        if type(enabled) is not bool:
            raise ValueError("invalid_enabled_flag")
        self.detector_artifact, self.recognizer_artifact = detector, recognizer
        self.enabled = enabled
        self.status = Status("unavailable", "activation_required") if enabled else Status("disabled", "default_off")
        self._detector = self._recognizer = self._np = None
        self._simulated = True
        self._gate = threading.Lock()

    def close(self):
        with self._gate:
            self._detector = self._recognizer = self._np = None
            self.status = Status("disabled", "closed")

    def activate(self, *, _module=None) -> Status:
        with self._gate:
            self._detector = self._recognizer = self._np = None
            if not self.enabled:
                self.status = Status("disabled", "default_off")
                return self.status
            if self.detector_artifact is None:
                self.status = Status("unavailable", "weights_not_configured")
                return self.status
            try:
                if _module is None and (self.detector_artifact.sha256 != YUNET_SHA256
                        or (self.recognizer_artifact and self.recognizer_artifact.sha256 != SFACE_SHA256)):
                    self.status = Status("unavailable", "selected_model_fingerprint_mismatch")
                    return self.status
                detection_path = self.detector_artifact.verified_path()
                recognition_path = self.recognizer_artifact.verified_path() if self.recognizer_artifact else None
                cv = _module if _module is not None else importlib.import_module("cv2")
                np = importlib.import_module("numpy")
                detector = cv.FaceDetectorYN.create(
                    model=detection_path, config="", input_size=(320, 320), score_threshold=0.9,
                    nms_threshold=0.3, top_k=64, backend_id=cv.dnn.DNN_BACKEND_OPENCV,
                    target_id=cv.dnn.DNN_TARGET_CPU,
                )
                recognizer = None
                if recognition_path:
                    recognizer = cv.FaceRecognizerSF.create(
                        model=recognition_path, config="", backend_id=cv.dnn.DNN_BACKEND_OPENCV,
                        target_id=cv.dnn.DNN_TARGET_CPU,
                    )
                if detector is None or (recognition_path and recognizer is None):
                    raise ValueError("native_constructor_failed")
                self._detector, self._recognizer, self._np = detector, recognizer, np
                self._simulated = _module is not None
                self.status = Status("ready", "detection_and_embedding" if recognizer else "detection_only")
            except (OSError, ValueError):
                self.status = Status("unavailable", "artifact_or_native_configuration_invalid")
            except ImportError:
                self.status = Status("unavailable", "optional_dependency_missing")
            except Exception:
                self.status = Status("unavailable", "native_initialization_failed")
            return self.status

    def _image_valid(self, image):
        np = self._np
        return (np is not None and isinstance(image, np.ndarray) and image.dtype == np.uint8
                and image.ndim == 3 and image.shape[2] == 3 and image.flags.c_contiguous
                and 1 <= image.shape[0] <= 720 and 1 <= image.shape[1] <= 1280)

    @staticmethod
    def _row_valid(row, width, height):
        if len(row) != 15 or any(not math.isfinite(v) for v in row):
            return False
        x, y, w, h = row[:4]
        return (0 <= x < width and 0 <= y < height and w > 0 and h > 0
                and x + w <= width and y + h <= height and 0 <= row[14] <= 1
                and all(0 <= row[i] < width and 0 <= row[i + 1] < height for i in range(4, 14, 2)))

    @staticmethod
    def _frame_digest(image):
        """Bind pixel interpretation as well as bytes, independent of storage address.

        Call only after _image_valid: three-dimensional contiguous uint8 BGR.
        An identical copy is valid; reshaping the same buffer is a different frame.
        """
        digest = hashlib.sha256(b"friday-face-frame/2\x00")
        digest.update(struct.pack(">6q", *image.shape, *image.strides))
        digest.update(image.dtype.str.encode("ascii") + b"\x00")
        digest.update(memoryview(image))
        return digest.hexdigest()

    def detect(self, bgr_image) -> FaceDetections:
        if not self._gate.acquire(blocking=False):
            return FaceDetections(Status("unavailable", "provider_busy"))
        try:
            if self.status.state != "ready":
                return FaceDetections(self.status)
            if not self._image_valid(bgr_image):
                return FaceDetections(Status("unknown", "invalid_or_oversized_bgr_image"))
            height, width = bgr_image.shape[:2]
            self._detector.setInputSize((width, height))
            _, native = self._detector.detect(bgr_image)
            if native is None:
                return FaceDetections(Status("unknown", "no_face"))
            if native.ndim != 2 or native.shape[1] != 15:
                return FaceDetections(Status("unknown", "invalid_detection_output"))
            if native.shape[0] > 8:
                return FaceDetections(Status("ambiguous", "face_count_budget"))
            rows = tuple(tuple(float(v) for v in row) for row in native)
            if any(not self._row_valid(row, width, height) for row in rows):
                return FaceDetections(Status("unknown", "invalid_detection_output"))
            rows = tuple(row for row in rows if row[14] >= 0.9)
            digest = self._frame_digest(bgr_image)
            return FaceDetections(Status("ready", "face_detection_only"), rows, digest, self._simulated) if rows else (
                FaceDetections(Status("unknown", "no_qualified_face")))
        except Exception:
            return FaceDetections(Status("unavailable", "native_detection_failed"))
        finally:
            self._gate.release()

    def embed_slot(self, bgr_image, detections: FaceDetections, face_slot: int,
                   quality: FaceQuality) -> EmbeddingResult:
        """Select one exact bounded row, retaining the existing digest/model/lock gate.

        No second detection/model load/image copy. Slot is local to this frame.
        The caller owns the frame across the serialized call and its cancellation.
        """
        if (not isinstance(detections, FaceDetections) or not isinstance(detections.rows, tuple)
                or not 1 <= len(detections.rows) <= 8 or type(face_slot) is not int
                or not 0 <= face_slot < len(detections.rows) or not isinstance(quality, FaceQuality)):
            return EmbeddingResult(Status("unknown", "invalid_face_slot"))
        selected = FaceDetections(detections.status, (detections.rows[face_slot],),
            detections.frame_digest, detections.simulated)
        return self.embed(bgr_image, selected, quality)

    def embed(self, bgr_image, detections: FaceDetections, quality: FaceQuality) -> EmbeddingResult:
        if not self._gate.acquire(blocking=False):
            return EmbeddingResult(Status("unavailable", "provider_busy"))
        try:
            if self.status.state != "ready":
                return EmbeddingResult(self.status)
            if self._recognizer is None:
                return EmbeddingResult(Status("unavailable", "recognition_weights_not_configured"))
            if detections.status.state != "ready":
                return EmbeddingResult(detections.status)
            if len(detections.rows) != 1:
                return EmbeddingResult(Status("ambiguous", "single_face_required"))
            if not self._image_valid(bgr_image):
                return EmbeddingResult(Status("unknown", "invalid_or_oversized_bgr_image"))
            if self._frame_digest(bgr_image) != detections.frame_digest:
                return EmbeddingResult(Status("unknown", "detection_frame_mismatch"))
            if detections.simulated != self._simulated:
                return EmbeddingResult(Status("unknown", "simulation_provenance_mismatch"))
            row = detections.rows[0]
            height, width = bgr_image.shape[:2]
            if not self._row_valid(row, width, height) or row[14] < 0.9 or min(row[2:4]) < 64:
                return EmbeddingResult(Status("unknown", "face_geometry_or_size_unqualified"))
            if not quality.qualified:
                return EmbeddingResult(Status("unknown", "face_quality_unqualified"))
            aligned = self._recognizer.alignCrop(bgr_image, self._np.array(row, dtype=self._np.float32))
            if not self._image_valid(aligned) or aligned.shape != (112, 112, 3):
                return EmbeddingResult(Status("unknown", "invalid_alignment_output"))
            native = self._recognizer.feature(aligned)
            if native.shape != (1, 128):
                return EmbeddingResult(Status("unknown", "invalid_embedding_output"))
            values = tuple(float(v) for v in native[0])
            embedding = Embedding("face", self.recognizer_artifact.sha256, values, self._simulated)
            return EmbeddingResult(Status("ready", "face_embedding_only"), embedding)
        except ValueError:
            return EmbeddingResult(Status("unknown", "invalid_embedding_output"))
        except Exception:
            return EmbeddingResult(Status("unavailable", "native_embedding_failed"))
        finally:
            self._gate.release()
