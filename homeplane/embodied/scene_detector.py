"""Small local COCO detector. Images never leave the host."""

import time

import cv2
import numpy as np
import onnxruntime as ort


class SceneDetector:
    def __init__(self, model):
        ort.disable_telemetry_events()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        self.session = ort.InferenceSession(
            str(model), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.input = self.session.get_inputs()[0].name

    def detect(self, jpeg):
        began = time.monotonic()
        image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return {"objects": [], "scene_ms": 0}
        height, width = image.shape[:2]
        scale = min(640 / width, 640 / height)
        resized = cv2.resize(image, (round(width * scale), round(height * scale)))
        dh = (640 - resized.shape[0]) // 2
        dw = (640 - resized.shape[1]) // 2
        canvas = np.full((640, 640, 3), 114, np.uint8)
        canvas[dh : dh + resized.shape[0], dw : dw + resized.shape[1]] = resized
        blob = np.ascontiguousarray(canvas[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32) / 255
        prediction = self.session.run(None, {self.input: blob})[0][0].T
        objects = []
        for class_id, name, label in [
            (0, "person", "人体"),
            (56, "chair", "椅子"),
            (57, "sofa", "沙发"),
            (59, "soft_furniture", "软体家具"),
            (60, "table", "桌子"),
        ]:
            threshold = 0.45 if class_id == 0 else 0.20
            rows = prediction[prediction[:, 4 + class_id] > threshold]
            boxes = []
            scores = []
            for row in rows:
                cx, cy, w, h = row[:4]
                x = (cx - w / 2 - dw) / scale
                y = (cy - h / 2 - dh) / scale
                boxes.append([float(x), float(y), float(w / scale), float(h / scale)])
                scores.append(float(row[4 + class_id]))
            keep = cv2.dnn.NMSBoxes(boxes, scores, threshold, 0.45)
            for i in np.asarray(keep).flatten():
                x, y, w, h = boxes[int(i)]
                x1 = max(0, x / width)
                y1 = max(0, y / height)
                x2 = min(1, (x + w) / width)
                y2 = min(1, (y + h) / height)
                if x2 > x1 and y2 > y1:
                    objects.append(
                        {
                            "class": name,
                            "name": label,
                            "confidence": round(scores[int(i)], 3),
                            "box": {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1},
                            "partial": x1 < 0.005 or y1 < 0.005 or x2 > 0.995 or y2 > 0.995,
                        }
                    )
        return {"objects": objects, "scene_ms": round((time.monotonic() - began) * 1000, 1)}
