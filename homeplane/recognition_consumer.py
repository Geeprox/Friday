"""Decoded callbacks to reviewed provider leaves, without enrollment or authority.

The only injected owner context in this packet is explicitly synthetic. Production
admission has no issuer; wire JSON cannot create an owner context or a provider.
"""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from .recognition.contracts import Candidate, Embedding, VerificationPolicy, compare_candidates
from .recognition.face import FaceDetections, FaceQuality, OpenCVFaceProvider
from .recognition.speaker import SpeakerQuality
from .recognition_contracts import (
    FaceFrameMetadata,
    FaceFrameProvenance,
    FaceSlotMetadata,
    FaceSlotQuality,
    NativeFaceResult,
    ObservationQuality,
    ObservationTiming,
    ReceivedFrameTiming,
    RecognitionObservation,
    RecognitionProvenance,
    RecognitionResult,
    RecognitionSession,
    RecognitionStatus,
    observation_id,
)
from .recognition_timing import CaptureStamp, finite_number
from .recognition_tracking import Detection, ObservationTracker

# Keep Python/native ownership alive across cancellation until the owning process
# exits or completion is positively observed. Worker finalization checks this map.
_OWNED_RECOGNITION_CALLS = {}
MIN_TRACK_INTERVAL = .05


@dataclass(frozen=True)
class SuppliedFaceBatch:
    """Trusted synthetic per-box output; no image, provider or clock conversion.

    Only this explicit owner seam supplies per-face vectors in this proposal.
    A production detector's pixel rows and SDK timestamp are not this contract.
    """
    detections: tuple[Detection, ...] = field(repr=False)
    capture: CaptureStamp | None = None
    shot_id: str | None = None
    reset: bool = False

    def __post_init__(self):
        if (not isinstance(self.detections, tuple) or len(self.detections) > 8
                or any(not isinstance(d, Detection) for d in self.detections)
                or self.capture is not None and not isinstance(self.capture, CaptureStamp)
                or type(self.reset) is not bool
                or self.shot_id is not None and (not isinstance(self.shot_id, str)
                    or not 1 <= len(self.shot_id) <= 64
                    or not all(c.isascii() and (c.isalnum() or c in "_-") for c in self.shot_id))):
            raise ValueError("invalid_supplied_face_batch")
        if self.capture is not None:
            CaptureStamp(self.capture.monotonic, self.capture.clock_id)
        for detection in self.detections:
            Detection(detection.box, detection.confidence,
                ObservationQuality.model_validate(detection.quality.model_dump()), detection.embedding)
            if detection.embedding is not None:
                e = detection.embedding
                Embedding(e.modality, e.model_sha256, e.values, e.simulated)


@dataclass(frozen=True)
class SyntheticOwnerContext:
    session: RecognitionSession
    provider: object = field(repr=False)
    quality: FaceQuality | SpeakerQuality = field(repr=False)
    provenance: RecognitionProvenance
    policy: VerificationPolicy | None = field(default=None, repr=False)
    candidates: tuple[Candidate, ...] = field(default=(), repr=False)
    _people_owner: object = field(default=None, repr=False)
    _supplied_faces: bool = field(default=False, repr=False)

    def admits(self, session):
        return (self.session == session and session.simulated is True
                and type(self._supplied_faces) is bool
                and (not self._supplied_faces or session.modality == "face"
                    and self._people_owner is not None and not self.candidates)
                and isinstance(self.quality, FaceQuality if session.modality == "face" else SpeakerQuality)
                and isinstance(self.provenance, RecognitionProvenance)
                and self.provenance.provider == ("opencv_yunet_sface" if session.modality == "face" else "sherpa_wespeaker"))


@dataclass(frozen=True)
class NativeMetadataOwnerContext:
    """V3 source context. Native admission has no issuer and is always denied.

    Only an explicit synthetic API fixture may exercise the exact producer path.
    Source/runtime/quality fields retain that fixture label in every output.
    """
    session: RecognitionSession
    provider: OpenCVFaceProvider = field(repr=False)
    provenance: FaceFrameProvenance
    quality_source: object = field(default=None, repr=False)
    _fixture_consent: bool = field(default=False, repr=False)
    _people_owner: None = field(default=None, init=False, repr=False)
    _supplied_faces: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        if (not isinstance(self.session, RecognitionSession) or self.session.modality != "face"
                or not isinstance(self.provider, OpenCVFaceProvider)
                or not isinstance(self.provenance, FaceFrameProvenance)
                or self.quality_source is not None and not callable(self.quality_source)
                or type(self._fixture_consent) is not bool):
            raise ValueError("invalid_native_metadata_context")
        RecognitionSession.model_validate(self.session.model_dump())
        FaceFrameProvenance.model_validate(self.provenance.model_dump())

    def admits(self, session):
        # There is deliberately no native_source_metadata admission constructor.
        provider = self.provider
        return (self._fixture_consent is True and session == self.session and session.simulated is True
                and self.provenance.evidence == "synthetic_contract_fixture"
                and provider.enabled is True and provider._simulated is True
                and provider.status.state == "ready" and provider._detector is not None and provider._np is not None
                and provider.detector_artifact is not None
                and provider.detector_artifact.sha256 == self.provenance.detector_model_sha256
                and (self.provenance.embedding_model_sha256 is None or provider.recognizer_artifact is not None
                    and provider.recognizer_artifact.sha256 == self.provenance.embedding_model_sha256))


class RecognitionConsumer:
    def __init__(self, *, _owner_context=None, clock=time.monotonic):
        self.clock = clock
        self.context = None
        self.session = None
        self.generation = 0
        self.active = False
        self._executor = self._call = None
        self._audio = []
        self._samples = 0
        self._video_seen = False
        self.observation = None
        self._report = RecognitionStatus()
        self._feature_sink = self._feature_message = None
        self._feature_index = 0
        self._last_feature_time = self._feature_started_time = None
        self._tracking_lock = threading.RLock()
        self._tracker = None
        self._tracking_revoked = False
        self._tracking_started = self.clock()
        self._tracking_frames = 0
        self._last_tracking_time = None
        self._sampled_tracks = {}
        self._tracking_observation_ready = False
        self._supplied_finishing = False
        self._native_mode = isinstance(_owner_context, NativeMetadataOwnerContext)
        self._native_frame = None
        self._native_finished = False
        self._native_admission_kind = "unavailable"
        self._native_attempts = 0
        self._native_seen = set()
        self._native_last_received = self._native_source = None
        if _owner_context is not None:
            if self._native_mode:
                self.context, self.session = _owner_context, _owner_context.session
                self.generation = self.session.generation
                admitted = _owner_context.admits(self.session)
                self._native_admission_kind = ("synthetic_contract_fixture" if admitted and self.session.simulated
                    else "native_source_metadata" if admitted else "unavailable")
                self.active = admitted and self._tracking_started < self.session.deadline_monotonic
                self._set("running" if self.active else "expired" if admitted else "unavailable",
                    "running" if self.active else "expired" if admitted else "owner_admission_unavailable")
            elif (not isinstance(_owner_context, SyntheticOwnerContext)
                    or not _owner_context.admits(_owner_context.session)):
                self._report = RecognitionStatus(state="unavailable", reason="owner_admission_unavailable")
            else:
                self.context, self.session = _owner_context, _owner_context.session
                self.generation = self.session.generation
                self.active = self.clock() < self.session.deadline_monotonic
                self._set("running" if self.active else "expired", "running" if self.active else "expired")
                if _owner_context._people_owner is not None:
                    from .people_feature_bridge import SyntheticPeopleBatchOwner
                    owner = _owner_context._people_owner
                    if (not isinstance(owner, SyntheticPeopleBatchOwner)
                            or not owner.admits(self.session, self.context.provenance)
                            or owner.policy != self.context.policy):
                        self.invalidate("stale_generation")
                    else:
                        self._feature_sink = owner.make_sink()
                        if _owner_context._supplied_faces:
                            self._tracker = ObservationTracker(self.session, self.context.provenance,
                                started_monotonic=self._tracking_started)

    def _set(self, state, reason):
        self._report = RecognitionStatus(state=state, reason=reason,
            simulated=self.session.simulated if self.session else False)

    def status(self):
        # Cached only: no artifacts, dependency imports, source stats or media.
        return self._report

    def _clear_audio(self):
        for chunk in self._audio:
            chunk.fill(0)
        self._audio.clear()
        self._samples = 0

    def _effective_deadline(self):
        deadline = self.session.deadline_monotonic
        if self.context._supplied_faces or self._native_mode:
            deadline = min(deadline, self._tracking_started+self.session.seconds)
        return deadline

    def _expire_if_due(self):
        if self.active and self.session is not None and self.clock() >= self._effective_deadline():
            self.invalidate("expired")
            return True
        return False

    def accepts(self, did, channel, modality):
        if not self.active or self.session is None:
            return False
        if self.clock() >= self._effective_deadline():
            self.invalidate("expired")
            return False
        if self._native_mode and not self.context.admits(self.session):
            self.invalidate("stale_generation")
            return False
        if self._feature_sink is not None and not self.context._people_owner.admits(self.session, self.context.provenance):
            self.invalidate()
            return False
        return (did == self.session.camera_id and channel == self.session.channel
                and modality == self.session.modality and self.generation == self.session.generation)

    def invalidate(self, reason="stale_generation"):
        with self._tracking_lock:
            self.active = False
            self.generation += 1
            self._clear_audio()
            self._tracking_revoked = True
            self._native_frame = None
            self._native_seen.clear()
            if self._tracker is not None and (self._call is None or self._call.done()):
                self._tracker.close()
            if self._feature_sink is not None:
                self._feature_sink.revoke()
                self._feature_message = None
            self._set("expired" if reason == "expired" else "unavailable", reason)

    def _async_owned_call(self):
        pending = asyncio.wrap_future(self._call)
        # Every shielded waiter can be canceled independently; consume late
        # wrapper exceptions without canceling or releasing the actual call.
        pending.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        return pending

    async def _run(self, operation, *, feature_index=None):
        if self._call is not None and (not self._call.done() or self._feature_sink is None and not self._native_mode):
            return
        generation = self.generation
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recognition-owned")
            _OWNED_RECOGNITION_CALLS[id(self)] = self
        self._call = self._executor.submit(operation)
        pending = self._async_owned_call()
        try:
            evidence = await asyncio.shield(pending)
            if self._native_mode and evidence.reason == "stale_generation" and self.active:
                self.invalidate("expired" if self.clock() >= self._effective_deadline() else "native_failed")
                return
            if (self.active and generation == self.generation
                    and self.clock() < self._effective_deadline()):
                if feature_index in (None, 0):
                    self._report = evidence
                if feature_index in (None, 0) and self.observation is not None and evidence.reason in {"embedding_only", "similarity_evidence_only",
                        "ambiguous", "below_threshold", "candidate_unqualified", "policy_unqualified"}:
                    self.observation = self.observation.model_copy(update={"embedding_produced": True})
            elif self.active:
                self.invalidate("expired")
        except asyncio.CancelledError:
            if self._native_mode:
                self.invalidate("stopped")
            raise  # A canceled waiter is not proof the native call completed.
        except Exception:
            if self.active:
                if self.context._supplied_faces or self._native_mode:
                    self.invalidate("native_failed")
                    return
                if self._feature_sink is not None:
                    self._feature_sink.revoke()
                self._set("unavailable", "native_failed")

    def _observe(self, source_timestamp):
        self.observation = RecognitionObservation(observation_id=observation_id(self.session),
            modality=self.session.modality, provenance=self.context.provenance,
            quality=ObservationQuality(qualified=self.context.quality.qualified, **asdict(self.context.quality)),
            timing=ObservationTiming(observed_monotonic=float(self.clock()), source_timestamp=source_timestamp))

    def _evidence(self, observed, *, feature_observation=None):
        if observed.status.state != "ready":
            return RecognitionStatus(state="unknown" if observed.status.state in {"unknown", "ambiguous"} else "unavailable",
                reason="provider_unavailable", simulated=self.session.simulated)
        if observed.embedding.simulated != self.session.simulated:
            return RecognitionStatus(state="unknown", reason="simulation_mismatch", simulated=self.session.simulated)
        if observed.embedding.model_sha256 != self.context.provenance.embedding_model_sha256:
            return RecognitionStatus(state="unknown", reason="model_mismatch", simulated=self.session.simulated)
        policy = self.context.policy
        if policy is None or not policy.calibrated:
            return RecognitionStatus(state="unavailable", reason="policy_unqualified", simulated=self.session.simulated)
        if (observed.embedding.modality, observed.embedding.model_sha256) != (policy.modality, policy.model_sha256):
            return RecognitionStatus(state="unknown", reason="model_mismatch", simulated=self.session.simulated)
        if feature_observation is not None:
            if not self.context._people_owner.admits(self.session, self.context.provenance):
                self._feature_sink.revoke()
                return RecognitionStatus(state="unavailable", reason="stale_generation", simulated=self.session.simulated)
            self._feature_sink.observe(self.session,
                feature_observation.model_copy(update={"embedding_produced": True}), observed.embedding)
        if not self.context.candidates:
            return RecognitionStatus(state="ready", reason="embedding_only", simulated=self.session.simulated)
        evidence = compare_candidates(observed, self.context.candidates, policy)
        mapping = {"model_mismatch": "model_mismatch", "simulation_provenance_mismatch": "simulation_mismatch",
            "threshold_unqualified": "policy_unqualified", "below_calibrated_threshold": "below_threshold",
            "candidate_margin_insufficient": "ambiguous", "similarity_evidence_only": "similarity_evidence_only"}
        return RecognitionStatus(state=evidence.status.state,
            reason=mapping.get(evidence.status.reason, "candidate_unqualified"), simulated=self.session.simulated)

    async def video(self, did, frame, channel, *, source_timestamp=None, received_monotonic=None):
        if not self.accepts(did, channel, "face"):
            return
        if self._native_mode:
            await self._native_video(frame, source_timestamp, received_monotonic)
            return
        if self.context._supplied_faces:
            await self._supplied_video(frame, source_timestamp)
            return
        batch = self._feature_sink is not None
        if batch:
            from .recognition_feature_handoff import MAX_FEATURE_SAMPLES, MIN_SAMPLE_INTERVAL
            now = self.clock()
            if (self._feature_started_time is not None and now - self._feature_started_time > self.session.seconds
                    or self._feature_index >= MAX_FEATURE_SAMPLES
                    or self._call is not None and not self._call.done()
                    or self._last_feature_time is not None and now - self._last_feature_time < MIN_SAMPLE_INTERVAL):
                return
        elif self._video_seen:
            return
        self._video_seen = True
        # NumPy is imported only inside an admitted callback. No native SDK imports.
        import numpy as np
        if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3
                or frame.shape[2] != 3 or not 0 < frame.shape[0] <= 720 or not 0 < frame.shape[1] <= 1280):
            self._set("unknown", "invalid_video")
            return
        if self.observation is None:
            self._observe(source_timestamp)
        if not self.context.quality.qualified:
            self._set("unknown", "quality_unqualified")
            return
        feature_index = self._feature_index if batch else None
        feature_observation = None
        if batch:
            from .recognition_feature_handoff import feature_observation_id
            feature_observation = RecognitionObservation(observation_id=feature_observation_id(self.session, feature_index),
                modality="face", frame_sequence=feature_index, provenance=self.context.provenance,
                quality=self.observation.quality,
                timing=ObservationTiming(observed_monotonic=float(self.clock()), source_timestamp=source_timestamp))
            if self._feature_started_time is None:
                self._feature_started_time = self.clock()
            self._feature_index += 1
            self._last_feature_time = self.clock()
        owned = np.array(frame, dtype=np.uint8, order="C", copy=True)
        def operation():
            try:
                detections = self.context.provider.detect(owned)
                return self._evidence(self.context.provider.embed(owned, detections, self.context.quality),
                    feature_observation=feature_observation)
            finally:
                owned.fill(0)
        await self._run(operation, feature_index=feature_index)

    async def _native_video(self, frame, source_timestamp, received_monotonic):
        """Actual detect→slot→embed path on the existing owned frame/executor.

        V3 carries receive provenance, not capture. No People/identity pipeline.
        Embedding vectors are inspected for binding then dropped within this call.
        """
        from .recognition_feature_handoff import feature_observation_id
        now = self.clock()
        if self._supplied_finishing or self._call is not None and not self._call.done():
            return
        if not finite_number(now) or not finite_number(received_monotonic):
            self.invalidate("native_failed")
            return
        if self._tracking_frames >= 128:
            self.invalidate("expired")
            return
        if self._last_tracking_time is not None and 0 <= now-self._last_tracking_time < MIN_TRACK_INTERVAL:
            return
        try:
            timing = ReceivedFrameTiming(received_monotonic=float(received_monotonic),
                observation_monotonic=float(now), source_timestamp=source_timestamp,
                source_discontinuity=(source_timestamp is not None and self._native_source is not None
                    and type(source_timestamp) is int and source_timestamp < self._native_source))
            if (received_monotonic < self._tracking_started
                    or self._native_last_received is not None and received_monotonic <= self._native_last_received):
                raise ValueError("receive_clock_discontinuity")
        except ValueError:
            self.invalidate("native_failed")
            return
        import numpy as np
        if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3
                or frame.shape[2] != 3 or not 1 <= frame.shape[0] <= 720 or not 1 <= frame.shape[1] <= 1280):
            self.invalidate("native_failed")
            return
        seq, generation = self._tracking_frames, self.generation
        self._tracking_frames += 1
        self._last_tracking_time = now
        self._native_last_received, self._native_source = received_monotonic, source_timestamp
        owned = np.array(frame, dtype=np.uint8, order="C", copy=True)

        def eligible():
            observed = self.clock()
            return (finite_number(observed) and received_monotonic <= observed
                    and observed-received_monotonic <= .25
                    and self.active and not self._tracking_revoked and generation == self.generation
                    and observed < self._effective_deadline() and self.context.admits(self.session))

        def operation():
            try:
                if not eligible():
                    return RecognitionStatus(state="unavailable", reason="stale_generation", simulated=self.session.simulated)
                provider, provenance = self.context.provider, self.context.provenance
                detections = provider.detect(owned)
                if not isinstance(detections, FaceDetections):
                    raise ValueError("invalid_native_detection_record")
                if detections.status.state != "ready":
                    self._native_frame = None
                    return RecognitionStatus(state="unknown", reason="provider_unavailable", simulated=self.session.simulated)
                height, width = owned.shape[:2]
                if (detections.simulated != self.session.simulated or not isinstance(detections.rows, tuple)
                        or len(detections.rows) > 8 or detections.frame_digest != provider._frame_digest(owned)
                        or any(not provider._row_valid(row, width, height) for row in detections.rows)):
                    raise ValueError("invalid_native_detection_binding")
                source = self.context.quality_source
                qualities = (tuple(FaceSlotQuality() for _ in detections.rows) if source is None
                    else source(owned, detections))
                if not isinstance(qualities, tuple) or len(qualities) != len(detections.rows):
                    raise ValueError("invalid_slot_quality_count")
                qualities = tuple(FaceSlotQuality.model_validate(q.model_dump()) for q in qualities)
                expected_quality = "synthetic_contract_fixture" if self.session.simulated else "native_measured"
                if any(q.evidence not in ("unavailable", expected_quality) for q in qualities):
                    raise ValueError("quality_provenance_binding")
                slots = []
                sample_frame = self._last_feature_time is None or now-self._last_feature_time >= .25
                for slot, (row, quality) in enumerate(zip(detections.rows, qualities, strict=True)):
                    if not eligible():
                        break
                    key = detections.frame_digest, slot
                    state = "quality_unqualified" if not quality.qualified else "unavailable"
                    if (quality.qualified and provenance.embedding_model_sha256 is not None
                            and provenance.evidence in {"native_verified", "synthetic_contract_fixture"}):
                        if key in self._native_seen:
                            state = "duplicate"
                        elif self._native_attempts >= 4 or not sample_frame:
                            state = "budget_exhausted"
                        else:
                            self._native_seen.add(key)
                            self._native_attempts += 1
                            self._last_feature_time = now
                            observed = None
                            try:
                                observed = provider.embed_slot(owned, detections, slot,
                                    FaceQuality(quality.pose_ok, quality.sharpness_ok, quality.occlusion_ok))
                                e = observed.embedding
                                state = "provider_unavailable"
                                if e is not None:
                                    Embedding(e.modality, e.model_sha256, e.values, e.simulated)
                                    state = ("model_mismatch" if e.modality != "face"
                                        or e.model_sha256 != provenance.embedding_model_sha256
                                        else "simulation_mismatch" if e.simulated != self.session.simulated
                                        else "produced" if observed.status.state == "ready" else "provider_unavailable")
                            finally:
                                observed = e = None
                    slots.append(FaceSlotMetadata(observation_id=feature_observation_id(self.session, seq, slot),
                        face_slot=slot, box=(row[0]/width, row[1]/height, row[2]/width, row[3]/height),
                        confidence=row[14], quality=quality, embedding_state=state))
                metadata = FaceFrameMetadata(frame_sequence=seq, frame_ref=feature_observation_id(self.session, seq, 0),
                    width=width, height=height, timing=ReceivedFrameTiming(
                        received_monotonic=timing.received_monotonic, observation_monotonic=float(self.clock()),
                        source_timestamp=timing.source_timestamp, source_discontinuity=timing.source_discontinuity),
                    slots=slots)
                produced = any(s.embedding_state == "produced" for s in slots)
                report = RecognitionStatus(state="ready" if produced else "unknown",
                    reason="embedding_only" if produced else "no_input", simulated=self.session.simulated)
                # Validate the complete result, including IPC byte budget, before publishing.
                result = NativeFaceResult(session=self.session, status=report, provenance=provenance,
                    admission_kind=self._native_admission_kind, window_started_monotonic=self._tracking_started,
                    frame=metadata)
                from .recognition_feature_protocol import encode_worker_output
                encode_worker_output(result)
                with self._tracking_lock:
                    if eligible():
                        self._native_frame = metadata
                        return report
                return RecognitionStatus(state="unavailable", reason="stale_generation", simulated=self.session.simulated)
            finally:
                owned.fill(0)
                with self._tracking_lock:
                    if self._tracking_revoked:
                        self._native_frame = None
                        self._native_seen.clear()
        await self._run(operation)

    async def _supplied_video(self, batch, source_timestamp):
        """Finite scheduling hook on the same callback and owned executor.

        Drop busy/too-soon batches; no second reader, provider, executor or queue.
        Tracking continues after feature admission caps. No identity matching.
        """
        from .recognition_feature_handoff import (
            MAX_FEATURE_SAMPLES,
            MIN_SAMPLE_INTERVAL,
            feature_observation_id,
        )
        now = float(self.clock())
        if self._supplied_finishing:
            return
        if (not isinstance(batch, SuppliedFaceBatch) or self._tracker is None):
            self.invalidate("native_failed")
            return
        if now >= self._effective_deadline():
            self.invalidate("expired")
            return
        if self._tracking_frames >= 128:
            self.invalidate("expired")
            return
        if self._call is not None and not self._call.done():
            return
        if self._last_tracking_time is not None and 0 <= now-self._last_tracking_time < MIN_TRACK_INTERVAL:
            return
        try:
            SuppliedFaceBatch(batch.detections, batch.capture, batch.shot_id, batch.reset)
            timing = ObservationTiming(observed_monotonic=now, source_timestamp=source_timestamp)
        except ValueError:
            self.invalidate("native_failed")
            return
        seq, generation = self._tracking_frames, self.generation
        self._tracking_frames += 1
        self._last_tracking_time = now

        def operation():
            try:
                with self._tracking_lock:
                    if (not self.active or self._tracking_revoked or generation != self.generation
                            or not self.context._people_owner.admits(self.session, self.context.provenance)):
                        return RecognitionStatus(state="unavailable", reason="stale_generation", simulated=True)
                tracked = None
                if batch.capture is None:
                    # Worker receipt time is not capture time. Break continuity
                    # but preserve cumulative frame/birth budgets on the tracker.
                    self._tracker.reset_continuity()
                else:
                    tracked = self._tracker.update(self.session, batch.detections, timing, batch.capture,
                        now_monotonic=float(self.clock()), shot_id=batch.shot_id, reset=batch.reset)
                observations = []
                for slot, detection in enumerate(batch.detections):
                    embedding = detection.embedding
                    produced = (embedding is not None and embedding.modality == "face" and embedding.simulated is True
                                and embedding.model_sha256 == self.context.provenance.embedding_model_sha256)
                    observation = RecognitionObservation(observation_id=feature_observation_id(self.session, seq, slot),
                        frame_sequence=seq, face_slot=slot, modality="face", provenance=self.context.provenance,
                        quality=detection.quality, timing=timing, embedding_produced=produced)
                    if tracked is not None:
                        observation = self._tracker.bind_observation(self.session, observation, tracked,
                            now_monotonic=float(self.clock()), detection_index=slot)
                    observations.append(observation)
                with self._tracking_lock:
                    if (not self.active or self._tracking_revoked or generation != self.generation
                            or self.clock() >= self._effective_deadline()
                            or not self.context._people_owner.admits(self.session, self.context.provenance)):
                        return RecognitionStatus(state="unavailable", reason="stale_generation", simulated=True)
                    if self.observation is None and observations:
                        # Existing result stays one canonical window observation;
                        # detailed frame/slot IDs travel in the feature message.
                        self.observation = observations[0].model_copy(update={"observation_id": observation_id(self.session)})
                        self._tracking_observation_ready = (self._tracker._qualified(batch.detections[0]) is None
                                                           and batch.detections[0].embedding is not None)
                    may_sample = (self._last_feature_time is None or now-self._last_feature_time >= MIN_SAMPLE_INTERVAL)
                    admitted = False
                    if may_sample:
                        for observation, detection in zip(observations, batch.detections, strict=True):
                            embedding = detection.embedding
                            if (self._feature_index >= MAX_FEATURE_SAMPLES
                                    or self._tracker._qualified(detection) is not None or embedding is None
                                    or observation.track_id is not None and self._sampled_tracks.get(observation.track_id, 0) >= 3):
                                continue
                            self._feature_sink.observe(self.session, observation, embedding)
                            self._feature_index += 1
                            if observation.track_id is not None:
                                self._sampled_tracks[observation.track_id] = self._sampled_tracks.get(observation.track_id, 0)+1
                            admitted = True
                    if admitted:
                        self._last_feature_time = now
                    selected = self.observation
                    ready = (self._tracking_observation_ready and selected is not None
                             and selected.quality.qualified and selected.embedding_produced
                             and self._feature_index > 0)
                    return RecognitionStatus(state="ready" if ready else "unknown",
                        reason="embedding_only" if ready else "no_input", simulated=True)
            finally:
                if self._tracking_revoked:
                    self._tracker.close()
        await self._run(operation, feature_index=0)

    async def audio(self, did, frame, channel, *, source_timestamp=None):
        if not self.accepts(did, channel, "speaker") or self._call is not None:
            return
        import numpy as np
        if (not isinstance(frame, np.ndarray) or frame.dtype != np.int16 or frame.ndim != 1
                or not 0 < frame.size <= 128000):
            self._clear_audio()
            self._set("unknown", "invalid_audio")
            self.active = False
            return
        self._observe(source_timestamp)
        if not self.context.quality.qualified:
            self._clear_audio()
            self._set("unknown", "quality_unqualified")
            self.active = False
            return
        limit = int(self.session.seconds * 16000)
        if self._samples + frame.size > limit:
            self._clear_audio()
            self._set("unknown", "audio_window_overflow")
            self.active = False
            return
        self._audio.append(np.array(frame, dtype=np.int16, order="C", copy=True))
        self._samples += frame.size
        if self._samples == limit:
            await self.flush_audio()

    async def flush_audio(self):
        if not self.active or self.session is None or self.session.modality != "speaker" or self._call is not None:
            return
        if not self.accepts(self.session.camera_id, 0, "speaker"):
            return
        if self._samples < 32000:
            self._clear_audio()
            self._set("unknown", "short_audio")
            return
        import numpy as np
        pcm = np.concatenate(self._audio).astype(np.float32)
        pcm /= np.float32(32768)
        self._clear_audio()
        def operation():
            try:
                return self._evidence(self.context.provider.embed(pcm, 16000, self.context.quality))
            finally:
                pcm.fill(0)
        await self._run(operation)

    async def finish(self):
        if self._native_mode:
            self._supplied_finishing = True
            self._expire_if_due()
            if self._call is not None:
                try:
                    await asyncio.shield(self._async_owned_call())
                except asyncio.CancelledError:
                    self.invalidate("stopped")
                    raise
                except Exception:
                    if not self._expire_if_due():
                        self.invalidate("native_failed")
            self._expire_if_due()
            if self._report.state == "running":
                self._set("unknown", "no_input")
            self.active = False
            self._native_finished = True
            self._native_seen.clear()
            return self.private_worker_result()
        if self.context is not None and self.context._supplied_faces:
            self._supplied_finishing = True
        self._expire_if_due()
        if self.context is not None and self.context._supplied_faces and self._call is not None and not self._call.done():
            try:
                await asyncio.shield(self._async_owned_call())
            except asyncio.CancelledError:
                self.invalidate("stopped")
                raise
            except Exception:
                if not self._expire_if_due():
                    self.invalidate("native_failed")
            self._expire_if_due()
        await self.flush_audio()
        self._expire_if_due()
        if self._feature_sink is not None and self.active and not self.context._people_owner.admits(self.session, self.context.provenance):
            self.invalidate()
        if self._feature_sink is not None and self.active and self._executor is not None:
            from .recognition_feature_protocol import FeatureMetadataMessage
            generation = self.generation
            observations = list(self._feature_sink.metadata_observations())
            if observations and not self._expire_if_due():
                self._call = self._executor.submit(self._feature_sink.finish, self.context._people_owner.operation)
                try:
                    metadata = await asyncio.shield(self._async_owned_call())
                    self._expire_if_due()
                    if (self.active and generation == self.generation
                            and self.context._people_owner.admits(self.session, self.context.provenance)
                            and self.clock() < self._effective_deadline()
                            and not self._expire_if_due()):
                        self._feature_message = FeatureMetadataMessage(schema_version=2, session=self.session, metadata=metadata,
                            observations=observations)
                except asyncio.CancelledError:
                    self._feature_sink.revoke()
                    raise
                except Exception:
                    self._feature_sink.revoke()
                    if self.active:
                        self._set("unavailable", "provider_unavailable")
        self._expire_if_due()
        if self._report.state == "running":
            self._set("unknown", "no_input")
        self.active = False
        self._clear_audio()
        if self._tracker is not None:
            self._tracker.close()
        self._sampled_tracks.clear()
        return RecognitionResult(session=self.session, status=self._report, observation=self.observation) if self.session else None

    def private_worker_result(self, *, status=None):
        if not self._native_mode:
            return RecognitionResult(session=self.session, status=status or self._report, observation=self.observation)
        expired = self.clock() >= self._effective_deadline() and self._report.reason != "owner_admission_unavailable"
        if expired:
            self._native_frame = None
            self._set("expired", "expired")
        report = status or self._report
        if expired and report.state != "unavailable":
            report = self._report  # A status captured before cleanup cannot extend the window.
        return NativeFaceResult(session=self.session, status=report, provenance=self.context.provenance,
            admission_kind=self._native_admission_kind,
            window_started_monotonic=self._tracking_started if self._native_admission_kind != "unavailable" else None,
            frame=self._native_frame if report.state not in {"expired", "unavailable"} else None)

    async def close(self):
        with self._tracking_lock:
            self.active = False
            self._tracking_revoked = True
            if self._native_mode and not self._native_finished:
                self._native_frame = None
            self._native_seen.clear()
            self._clear_audio()
            if self._feature_sink is not None:
                self._feature_sink.revoke()
        if self._call is not None:
            try:
                await asyncio.shield(self._async_owned_call())
            except Exception:
                # The native future completed with an error; ownership may close.
                pass
        if self._tracker is not None:
            self._tracker.close()
        self._sampled_tracks.clear()
        if self._executor is not None:
            # Positive completion above permits close; never wait on a canceled wrapper.
            self.context.provider.close()
            self._executor.shutdown(wait=False)
            _OWNED_RECOGNITION_CALLS.pop(id(self), None)
            self._executor = None
        if self.session is None:
            self._report = RecognitionStatus()
