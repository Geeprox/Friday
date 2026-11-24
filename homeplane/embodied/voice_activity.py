"""Local Silero neural speech activity; bounded recurrent state, CPU only."""

from functools import lru_cache

import numpy as np


@lru_cache(maxsize=1)
def _model_session():
    # 各镜头保留独立的循环状态，模型权重与执行会话共用。
    import onnxruntime as ort
    from homeplane.embodied.paths import MODELS

    ort.disable_telemetry_events()
    options = ort.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    return ort.InferenceSession(
        str(MODELS / "silero_vad.onnx"), sess_options=options, providers=["CPUExecutionProvider"])


class VoiceActivity:
    def __init__(self):
        _model_session()
        self.probability = 0.0
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.float32)
        self.probability = 0.0
        self.gain = 1.0

    def measure(self, pcm):
        if len(pcm) % 2:
            raise ValueError("unaligned_pcm")
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
        self.gain = min(16.0, max(1.0, 0.025 / max(rms, 1e-7)))
        self.pending = np.concatenate((self.pending, np.clip(x * self.gain, -0.98, 0.98)))
        probabilities = []
        session = _model_session()
        while len(self.pending) >= 512:
            chunk = self.pending[:512].reshape(1, 512)
            self.pending = self.pending[512:]
            window = np.concatenate((self.context, chunk), axis=1)
            try:
                output, self.state = session.run(
                    None, {"input": window, "state": self.state, "sr": np.array(16000, dtype=np.int64)}
                )
            except Exception:
                # 保留语音服务原有的故障退避与重建能力。
                _model_session.cache_clear()
                raise
            self.context = window[:, -64:]
            probabilities.append(float(output.reshape(-1)[0]))
        self.probability = float(np.mean(probabilities)) if probabilities else 0.0
        ratio = sum(p >= 0.5 for p in probabilities) / max(1, len(probabilities))
        return bool(ratio >= 0.3 and rms >= 0.00025), ratio, rms
