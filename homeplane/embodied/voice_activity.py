"""Local Silero neural speech activity; bounded recurrent state, CPU only."""

import numpy as np


class VoiceActivity:
    def __init__(self):
        import onnxruntime as ort

        ort.disable_telemetry_events()
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        from homeplane.embodied.paths import MODELS

        model = MODELS / "silero_vad.onnx"
        self.session = ort.InferenceSession(
            str(model), sess_options=options, providers=["CPUExecutionProvider"]
        )
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
        while len(self.pending) >= 512:
            chunk = self.pending[:512].reshape(1, 512)
            self.pending = self.pending[512:]
            window = np.concatenate((self.context, chunk), axis=1)
            output, self.state = self.session.run(
                None, {"input": window, "state": self.state, "sr": np.array(16000, dtype=np.int64)}
            )
            self.context = window[:, -64:]
            probabilities.append(float(output.reshape(-1)[0]))
        self.probability = float(np.mean(probabilities)) if probabilities else 0.0
        ratio = sum(p >= 0.5 for p in probabilities) / max(1, len(probabilities))
        return bool(ratio >= 0.3 and rms >= 0.00025), ratio, rms
