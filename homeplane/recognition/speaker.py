"""WeSpeaker ResNet34-LM embeddings through sherpa-onnx; no ASR/diarization."""

import importlib
import threading
from dataclasses import dataclass

from .artifacts import WESPEAKER_SOURCE_SHA256, LocalArtifact, speaker_metadata
from .contracts import Embedding, EmbeddingResult, Status


@dataclass(frozen=True)
class SpeakerQuality:
    speech_present: bool | None = None
    single_speaker: bool | None = None
    acoustic_quality_ok: bool | None = None

    @property
    def qualified(self):
        return all(v is True for v in (self.speech_present, self.single_speaker, self.acoustic_quality_ok))


class SherpaSpeakerProvider:
    def __init__(self, artifact: LocalArtifact | None = None, *, enabled: bool = False,
                 source_sha256: str = WESPEAKER_SOURCE_SHA256):
        if type(enabled) is not bool or source_sha256 != WESPEAKER_SOURCE_SHA256:
            raise ValueError("invalid_speaker_configuration")
        self.artifact, self.enabled = artifact, enabled
        # artifact.sha256 binds exact runtime bytes, including reviewed metadata
        # adaptation. source_sha256 binds the selected upstream ResNet34-LM.
        self.source_sha256 = source_sha256
        self.status = Status("unavailable", "activation_required") if enabled else Status("disabled", "default_off")
        self._extractor = self._np = None
        self._simulated = True
        self._gate = threading.Lock()

    def close(self):
        with self._gate:
            self._extractor = self._np = None
            self.status = Status("disabled", "closed")

    def activate(self, *, _module=None) -> Status:
        with self._gate:
            self._extractor = self._np = None
            if not self.enabled:
                self.status = Status("disabled", "default_off")
                return self.status
            if self.artifact is None:
                self.status = Status("unavailable", "weights_not_configured")
                return self.status
            try:
                path = self.artifact.verified_path()
                metadata = speaker_metadata(path)
                required = {"framework": "wespeaker", "sample_rate": "16000", "output_dim": "256",
                            "normalize_samples": "0", "language": "English"}
                if any(metadata.get(k) != v for k, v in required.items()):
                    self.status = Status("unavailable", "speaker_metadata_unqualified")
                    return self.status
                if metadata.get("feature_normalize_type", "") not in ("", "global-mean"):
                    self.status = Status("unavailable", "speaker_metadata_unqualified")
                    return self.status
                sherpa = _module if _module is not None else importlib.import_module("sherpa_onnx")
                config = sherpa.SpeakerEmbeddingExtractorConfig(
                    model=path, num_threads=1, debug=False, provider="cpu")
                if not config.validate():
                    self.status = Status("unavailable", "native_configuration_invalid")
                    return self.status
                extractor = sherpa.SpeakerEmbeddingExtractor(config)
                if extractor.dim != 256:
                    self.status = Status("unavailable", "embedding_dimension_mismatch")
                    return self.status
                self._extractor = extractor
                self._np = importlib.import_module("numpy")
                self._simulated = _module is not None
                self.status = Status("ready", "speaker_embedding_only")
            except (OSError, ValueError):
                self.status = Status("unavailable", "artifact_or_metadata_invalid")
            except ImportError:
                self.status = Status("unavailable", "optional_dependency_missing")
            except Exception:
                self.status = Status("unavailable", "native_initialization_failed")
            return self.status

    def embed(self, pcm_float32, sample_rate: int, quality: SpeakerQuality) -> EmbeddingResult:
        if not self._gate.acquire(blocking=False):
            return EmbeddingResult(Status("unavailable", "provider_busy"))
        try:
            if self.status.state != "ready":
                return EmbeddingResult(self.status)
            np = self._np
            if (type(sample_rate) is not int or sample_rate != 16000
                    or not isinstance(pcm_float32, np.ndarray) or pcm_float32.dtype != np.float32
                    or pcm_float32.ndim != 1 or not pcm_float32.flags.c_contiguous
                    or not 2 * 16000 <= pcm_float32.size <= 8 * 16000):
                return EmbeddingResult(Status("unknown", "invalid_or_short_audio"))
            if not np.isfinite(pcm_float32).all() or (np.abs(pcm_float32) > 1).any():
                return EmbeddingResult(Status("unknown", "invalid_audio_range"))
            if not quality.qualified:
                return EmbeddingResult(Status("unknown", "speaker_quality_unqualified"))
            if (np.abs(pcm_float32) >= 0.99).mean() > 0.05 or not np.any(pcm_float32):
                return EmbeddingResult(Status("unknown", "clipped_or_silent_audio"))
            stream = self._extractor.create_stream()
            stream.accept_waveform(sample_rate=sample_rate, waveform=pcm_float32)
            stream.input_finished()
            if not self._extractor.is_ready(stream):
                return EmbeddingResult(Status("unknown", "insufficient_speech_features"))
            native = self._extractor.compute(stream)
            if len(native) != 256:
                return EmbeddingResult(Status("unknown", "invalid_embedding_output"))
            embedding = Embedding("speaker", self.artifact.sha256, tuple(float(v) for v in native), self._simulated)
            return EmbeddingResult(Status("ready", "speaker_embedding_only"), embedding)
        except ValueError:
            return EmbeddingResult(Status("unknown", "invalid_embedding_output"))
        except Exception:
            return EmbeddingResult(Status("unavailable", "native_embedding_failed"))
        finally:
            self._gate.release()
