"""Local frame protection; uncertain analysis never returns unprotected pixels."""

from __future__ import annotations

import hashlib
import math
import os
import stat
from pathlib import Path

MODEL_SHA256 = "c15d8273adad2d0a92f014cc69ab2d6c311a06777a55545f2c4eb46f51911f0f"
PERSON_MODEL_SHA256 = "f715cc2d99740d22d312777e20d9de2b2ecdc250155be8fd3752ce7e8b823521"
SENSITIVE = frozenset(
    {
        "BUTTOCKS_EXPOSED",
        "FEMALE_BREAST_EXPOSED",
        "FEMALE_GENITALIA_EXPOSED",
        "ANUS_EXPOSED",
        "MALE_GENITALIA_EXPOSED",
    }
)
LABELS = (
    "FEMALE_GENITALIA_COVERED",
    "FACE_FEMALE",
    "BUTTOCKS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_BREAST_EXPOSED",
    "ANUS_EXPOSED",
    "FEET_EXPOSED",
    "BELLY_COVERED",
    "FEET_COVERED",
    "ARMPITS_COVERED",
    "ARMPITS_EXPOSED",
    "FACE_MALE",
    "BELLY_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_COVERED",
    "FEMALE_BREAST_COVERED",
    "BUTTOCKS_COVERED",
)


def rectangle(value, width, height):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("privacy_invalid_rectangle")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
        raise ValueError("privacy_invalid_rectangle")
    x, y, w, h = value
    if w <= 0 or h <= 0 or x >= width or y >= height or x + w <= 0 or y + h <= 0:
        raise ValueError("privacy_invalid_rectangle")
    x1, y1 = max(0, math.floor(x)), max(0, math.floor(y))
    x2, y2 = min(width, math.ceil(x + w)), min(height, math.ceil(y + h))
    return x1, y1, x2 - x1, y2 - y1


def expanded(box, width, height, fraction=0.16):
    x, y, w, h = box
    return rectangle(
        [x - w * fraction, y - h * fraction, w * (1 + 2 * fraction), h * (1 + 2 * fraction)], width, height
    )


def contains(person, part):
    x, y, w, h = person
    px, py, pw, ph = part
    return x <= px and y <= py and x + w >= px + pw and y + h >= py + ph


class LocalDetector:
    """Pinned public NudeNet model, CPU only, no network/model downloader."""

    def __init__(self, model):
        import onnxruntime as ort

        ort.disable_telemetry_events()
        path = Path(model)

        def model_bytes(source, limit, expected):
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                    raise ValueError("privacy_model_unavailable")
                with os.fdopen(os.dup(fd), "rb") as reader:
                    data = reader.read(limit + 1)
                if len(data) > limit or hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError("privacy_model_unavailable")
                return data
            finally:
                os.close(fd)

        data = model_bytes(path, 14 * 1024 * 1024, MODEL_SHA256)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.log_severity_level = 4
        # Load exactly the hashed bytes, never reopen a replaceable model path.
        self.session = ort.InferenceSession(data, sess_options=options, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        self.people = None
        person_path = path.with_name("tiny-yolov3-11.onnx")
        try:
            person_data = model_bytes(person_path, 40 * 1024 * 1024, PERSON_MODEL_SHA256)
            self.people = ort.InferenceSession(
                person_data, sess_options=options, providers=["CPUExecutionProvider"]
            )
        except (OSError, ValueError):
            pass  # Missing person localization keeps sensitive frames fully protected.

    def detect_people(self, image):
        import numpy as np
        from PIL import Image

        if self.people is None:
            return []
        height, width = image.shape[:2]
        scale = min(416 / width, 416 / height)
        resized = Image.fromarray(image[:, :, ::-1]).resize(
            (int(width * scale), int(height * scale)), Image.Resampling.BICUBIC
        )
        padded = Image.new("RGB", (416, 416), (128, 128, 128))
        padded.paste(resized, ((416 - resized.width) // 2, (416 - resized.height) // 2))
        inputs = self.people.get_inputs()
        pixels = next(i.name for i in inputs if len(i.shape) == 4)
        shape = next(i.name for i in inputs if len(i.shape) == 2)
        boxes, scores, indices = self.people.run(
            None,
            {
                pixels: np.asarray(padded, dtype=np.float32).transpose(2, 0, 1)[None, :, :, :] / 255,
                shape: np.asarray([[height, width]], dtype=np.float32),
            },
        )
        if (
            not np.isfinite(boxes).all()
            or not np.isfinite(scores).all()
            or indices.size > 384
            or indices.size % 3
        ):
            raise ValueError("privacy_invalid_people")
        found = []
        for batch, label, index in indices.reshape(-1, 3):
            if batch != 0 or label != 0 or scores[batch, label, index] < 0.20:
                continue
            y1, x1, y2, x2 = boxes[batch, index]
            found.append([float(x1), float(y1), float(x2 - x1), float(y2 - y1)])
        return found

    def detect(self, image):
        import cv2
        import numpy as np

        height, width = image.shape[:2]
        side = max(width, height)
        padded = cv2.copyMakeBorder(image, 0, side - height, 0, side - width, cv2.BORDER_CONSTANT)
        blob = cv2.dnn.blobFromImage(padded, 1 / 255.0, (320, 320), swapRB=True, crop=False)
        output = self.session.run(None, {self.input: blob})[0]
        if (
            output.ndim != 3
            or output.shape[0] != 1
            or output.shape[1] != len(LABELS) + 4
            or output.shape[2] > 10000
        ):
            return None
        rows = output[0].T
        if not np.isfinite(rows).all():
            return None
        detections = []
        # Each sensitive channel is checked independently, so a simultaneous
        # larger face/clothing score cannot suppress a sensitive body part.
        for class_id, label in enumerate(LABELS):
            if label not in SENSITIVE:
                continue
            for row in rows[rows[:, class_id + 4] >= 0.18]:
                x, y, w, h = row[:4]
                if w <= 0 or h <= 0:
                    return None
                box = [
                    (float(x) - float(w) / 2) * side / 320,
                    (float(y) - float(h) / 2) * side / 320,
                    float(w) * side / 320,
                    float(h) * side / 320,
                ]
                # Padding detections are not real content; partly clipped
                # observations remain uncertain and are fully protected.
                if box[0] >= width or box[1] >= height:
                    continue
                detections.append({"class": label, "score": float(row[class_id + 4]), "box": box})
                if len(detections) > 128:
                    return None
        people = []
        if detections:
            people = self.detect_people(image)
        return {"parts": detections, "people": people}


class FrameProtection:
    """A missing/broken detector is whole-frame mosaic, never a raw fallback."""

    def __init__(self, detector=None):
        self.detector = detector
        self.hold_until = -1.0
        self.previous_time = None
        self.uncertain_frames = 0

    def regions(self, analysis, width, height, timestamp):
        full = [(0, 0, width, height)]
        if not math.isfinite(timestamp) or timestamp < 0:
            return full
        if self.previous_time is not None and timestamp < self.previous_time:
            self.hold_until = -1.0
        self.previous_time = timestamp
        try:
            if not isinstance(analysis, dict) or set(analysis) != {"parts", "people"}:
                return full
            parts, people = analysis["parts"], analysis["people"]
            if (
                not isinstance(parts, list)
                or not isinstance(people, list)
                or len(parts) > 128
                or len(people) > 32
            ):
                return full
            sensitive, uncertain = [], False
            for part in parts:
                if (
                    not isinstance(part, dict)
                    or set(part) != {"class", "score", "box"}
                    or part["class"] not in SENSITIVE
                ):
                    return full
                score = part["score"]
                if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                    return full
                box = rectangle(part["box"], width, height)
                if score >= 0.18:
                    sensitive.append(box)
                    uncertain |= score < 0.35
            if sensitive:
                self.hold_until = timestamp + 1.0
                if uncertain:
                    return full
                bodies = [rectangle(p, width, height) for p in people]
                selected = []
                for part in sensitive:
                    candidates = [p for p in bodies if contains(p, part)]
                    if len(candidates) != 1:
                        return full
                    selected.append(expanded(candidates[0], width, height))
                return selected
            return full if timestamp <= self.hold_until else []
        except (ValueError, TypeError, KeyError, OverflowError):
            return full

    def apply(self, image, timestamp):
        import cv2

        try:
            analysis = self.detector.detect(image) if self.detector else None
        except Exception:
            analysis = None
        height, width = image.shape[:2]
        protected = image.copy()
        regions = self.regions(analysis, width, height, timestamp)
        if regions == [(0, 0, width, height)]:
            self.uncertain_frames += 1
        for x, y, w, h in regions:
            roi = protected[y : y + h, x : x + w]
            tiny = cv2.resize(roi, (min(w, 8), min(h, 12)), interpolation=cv2.INTER_AREA)
            protected[y : y + h, x : x + w] = cv2.resize(tiny, (w, h), interpolation=cv2.INTER_NEAREST)
        return protected
