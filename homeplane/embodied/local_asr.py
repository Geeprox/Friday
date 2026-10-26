"""Offline ASR backends. Model paths are task-owned; audio is never persisted."""

import sys

from homeplane.embodied.paths import DATA, MODELS


class LocalASR:
    def __init__(self, kind="sensevoice"):
        self.kind = kind
        if kind == "sensevoice":
            # Both runtimes are CPython 3.12 on this Mac. Reuse task-local wheels,
            # keeping the existing Friday environment unchanged.
            task_packages = (
                DATA / f"runtime/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
            )
            if str(task_packages) not in sys.path:
                sys.path.append(str(task_packages))
            import sherpa_onnx

            folder = MODELS / "sensevoice-int8"
            self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(folder / "model.int8.onnx"),
                tokens=str(folder / "tokens.txt"),
                num_threads=2,
                language="zh",
                use_itn=True,
                debug=False,
                provider="cpu",
            )
            self.label = "local_sensevoice_int8_cpu"
        elif kind == "whisper":
            import mlx_whisper

            self.recognizer = mlx_whisper
            self.label = "local_whisper_turbo_q4_mlx"
        else:
            raise ValueError("asr_backend_not_allowed")

    def transcribe(self, samples):
        if self.kind == "sensevoice":
            stream = self.recognizer.create_stream()
            stream.accept_waveform(16000, samples)
            self.recognizer.decode_stream(stream)
            result = stream.result
            return result.text.strip(), {
                "speech_event": getattr(result, "event", None),
                "detected_language": getattr(result, "lang", None),
            }
        import numpy as np

        r = self.recognizer.transcribe(
            samples,
            path_or_hf_repo=str(MODELS / "whisper-large-v3-turbo-4bit"),
            language="zh",
            condition_on_previous_text=False,
            temperature=0.0,
            verbose=None,
            initial_prompt="Friday、客厅、窗帘、梦幻帘。",
        )
        segments = r.get("segments", [])
        return r["text"].strip(), {
            "avg_logprob": float(np.mean([x.get("avg_logprob", -1) for x in segments])) if segments else None,
            "no_speech_prob": max([x.get("no_speech_prob", 0) for x in segments], default=1.0),
        }
