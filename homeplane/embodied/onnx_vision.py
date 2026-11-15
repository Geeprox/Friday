"""OpenCV Zoo 模型的 CPU 适配；不加载 MediaPipe 原生运行库。"""

from types import SimpleNamespace as Row


class CpuNet:
    """保留上游网络接口，明确限制为 CPU 并约束线程数量。"""

    def __init__(self, path):
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), sess_options=options,
                                           providers=['CPUExecutionProvider'])
        self.input_name = self.session.get_inputs()[0].name
        # OpenCV 按输出层名称排序；ORT 的图声明顺序可能不同。
        self.outputs = sorted((v.name for v in self.session.get_outputs()),
                              key=lambda name: name.replace(':0', ''))

    def setPreferableBackend(self, backend):
        if backend != 0:
            raise ValueError('vision_requires_cpu')

    def setPreferableTarget(self, target):
        if target != 0:
            raise ValueError('vision_requires_cpu')

    def setInput(self, blob):
        self.blob = blob

    def getUnconnectedOutLayersNames(self):
        return self.outputs

    def forward(self, names):
        return self.session.run(names, {self.input_name: self.blob})


class Pose:
    def __init__(self, folder):
        from .vendor.opencv_zoo.mp_persondet import MPPersonDet
        from .vendor.opencv_zoo.mp_pose import MPPose
        # 保留上游候选池，先合并重叠框，再限制后续关键点推理数量。
        self.detector = MPPersonDet(str(folder/'person_detection_mediapipe_2023mar.onnx'),
                                    scoreThreshold=.65)
        self.landmarker = MPPose(str(folder/'pose_estimation_mediapipe_2023mar.onnx'),
                                confThreshold=.65)

    def detect(self, rgb):
        import numpy as np
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        height, width = rgb.shape[:2]
        rows = []
        for person in self.detector.infer(bgr)[:6]:
            # 上游预处理会原位调整关键点，传独立副本避免污染检测结果。
            result = self.landmarker.infer(bgr, person.copy())
            if result is None:
                continue
            landmarks = result[1][:33]
            rows.append([Row(x=float(p[0]/width), y=float(p[1]/height),
                             visibility=float(p[3]), presence=float(p[4])) for p in landmarks])
        return Row(pose_landmarks=rows)

    def close(self):
        self.detector = self.landmarker = None


class Hand:
    def __init__(self, folder):
        from .vendor.opencv_zoo.mp_palmdet import MPPalmDet
        from .vendor.opencv_zoo.mp_handpose import MPHandPose
        self.detector = MPPalmDet(str(folder/'palm_detection_mediapipe_2023feb.onnx'),
                                 scoreThreshold=.75)
        self.landmarker = MPHandPose(str(folder/'handpose_estimation_mediapipe_2023feb.onnx'),
                                    confThreshold=.75)

    def detect(self, rgb):
        import numpy as np
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        height, width = rgb.shape[:2]
        hands, sides = [], []
        for palm in self.detector.infer(bgr)[:4]:
            result = self.landmarker.infer(bgr, palm.copy())
            if result is None or not np.all(np.isfinite(result)):
                continue
            points = result[4:67].reshape(21, 3)
            handedness = float(result[130])
            hands.append([Row(x=float(p[0]/width), y=float(p[1]/height)) for p in points])
            sides.append([Row(score=max(handedness, 1-handedness),
                              category_name='Right' if handedness >= .5 else 'Left')])
        return Row(hand_landmarks=hands, handedness=sides)

    def close(self):
        self.detector = self.landmarker = None
