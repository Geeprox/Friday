"""One exact worker feature adapter and revision-fenced metadata-only catalog seam.

No vector transport, model, template store, generic bus, enrollment or live issuer.
The injected owner below admits explicit synthetic source consent only.
"""
import copy
import hashlib
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .people.catalog import MAX_SAMPLES, PeopleCatalog, PeopleError
from .people.clustering import FaceInput, digest, proposals
from .people.contracts import Run, Sample
from .recognition.contracts import VerificationPolicy
from .recognition_contracts import RecognitionProvenance, RecognitionSession
from .recognition_feature_handoff import (
    EphemeralFeatureSink,
    FeatureGroupProposal,
    FeatureHandoffError,
    feature_observation_id,
)
from .recognition_feature_protocol import FeatureMetadataMessage

MAX_PEOPLE_DATABASE_BYTES = 16 * 1024 * 1024


def observation_refs(session, observation):
    frame_id = feature_observation_id(session, observation.frame_sequence, 0)
    frame = digest(["friday-feature-frame/1", session.camera_id, str(session.channel),
        session.owner_operation, frame_id, observation.provenance.model_dump_json()])
    camera = "camera_" + hashlib.sha256(session.camera_id.encode()).hexdigest()[:24]
    source = digest(["friday-feature-source/1", frame])
    sample_id = digest(["friday-face-sample/1", observation.observation_id, frame, str(observation.face_slot)])
    return source, frame, camera, sample_id


def worker_face_input(session, feature):
    source, frame, camera, _ = observation_refs(session, feature.observation)
    return FaceInput(source, frame, feature.observation.face_slot, feature.embedding, qualified=True, camera_ref=camera,
        track_ref=feature.observation.track_id, observation_ref=feature.observation.observation_id,
        observed_monotonic=feature.observation.timing.observed_monotonic)


class SyntheticPeopleBatchOwner:
    """Owner-only synthetic source scope, never supplied by public request JSON."""
    def __init__(self, session, provenance, policy, *, base_people_revision, annotations=None, blocked=(),
                 _synthetic_consent=False, _cluster_factory=None, clock=time.monotonic):
        if (not isinstance(session, RecognitionSession) or session.modality != "face" or session.simulated is not True
                or not isinstance(provenance, RecognitionProvenance) or provenance.provider != "opencv_yunet_sface"
                or not isinstance(policy, VerificationPolicy) or policy.modality != "face" or not policy.calibrated
                or policy.model_sha256 != provenance.embedding_model_sha256
                or _synthetic_consent is not True or type(base_people_revision) is not int or base_people_revision < 0):
            raise FeatureHandoffError("feature_owner_binding_unqualified")
        self.session, self.provenance, self.policy = session, provenance, policy
        self.base_people_revision = base_people_revision
        self._annotations = copy.deepcopy(annotations or {})
        self._blocked = tuple(tuple(pair) for pair in blocked)
        if len(self._annotations) > MAX_SAMPLES or len(self._blocked) > 4096:
            raise FeatureHandoffError("feature_review_budget")
        self._factory, self._clock = _cluster_factory, clock
        self._lock = threading.RLock()
        self._revoked = self._consumed = False
        self._catalog = None
        self._sink = None

    @classmethod
    def from_catalog(cls, catalog, session, provenance, policy, *, _synthetic_consent=False,
                     _cluster_factory=None, clock=time.monotonic):
        if not isinstance(catalog, PeopleCatalog):
            raise FeatureHandoffError("feature_catalog_required")
        with catalog._lock:
            state = catalog._read()
            owner = cls(session, provenance, policy, base_people_revision=state["revision"],
                annotations=state["annotations"], blocked=state["blocked"], _synthetic_consent=_synthetic_consent,
                _cluster_factory=_cluster_factory, clock=clock)
            owner._catalog = catalog
            return owner

    def admits(self, session, provenance):
        with self._lock:
            return (not self._revoked and not self._consumed and session == self.session
                and provenance == self.provenance and self._clock() < self.session.deadline_monotonic)

    def revoke(self):
        with self._lock:
            self._revoked = True
            if self._sink is not None:
                self._sink.revoke()

    def make_sink(self):
        with self._lock:
            if not self.admits(self.session, self.provenance) or self._sink is not None:
                raise FeatureHandoffError("feature_scope_revoked")
            self._sink = EphemeralFeatureSink(self.session, self.provenance,
                base_people_revision=self.base_people_revision, clock=self._clock)
            return self._sink

    def operation(self, features):
        """Run the real People module; only its optional library factory is synthetic in tests."""
        if not self.admits(self.session, self.provenance):
            raise FeatureHandoffError("feature_scope_revoked")
        inputs = tuple(worker_face_input(self.session, feature) for feature in features)
        partitions, states = proposals(inputs, self.policy, self._annotations,
            {frozenset(pair) for pair in self._blocked}, _factory=self._factory)
        observation_by_sample = {value.sample_id: value.observation_ref for value in inputs}
        output = []
        seen = set()
        for members, representatives in partitions:
            seen.update(members)
            statuses = {states[sid] for sid in members}
            state = "ambiguous" if "ambiguous" in statuses else "unknown" if statuses == {"unknown"} else "candidate"
            # A named singleton is still insufficient automatic cluster evidence.
            if len(members) == 1 and state == "candidate":
                state = "unknown"
            output.append(FeatureGroupProposal(observation_ids=[observation_by_sample[sid] for sid in members],
                representative_ids=[observation_by_sample[sid] for sid in representatives], state=state))
        # Excluded samples retain metadata for a durable correction but remain outside clusters.
        for value in inputs:
            if value.sample_id not in seen:
                output.append(FeatureGroupProposal(observation_ids=[value.observation_ref],
                    representative_ids=[value.observation_ref], state="unknown"))
        return output

    def apply(self, catalog, message):
        """Parent-only metadata admission, fenced again after vector disposal."""
        if (not isinstance(catalog, PeopleCatalog) or catalog is not self._catalog
                or not isinstance(message, FeatureMetadataMessage)):
            raise FeatureHandoffError("feature_catalog_required")
        # Revalidate mutable nested proposal models; no unvalidated model_copy input.
        message = FeatureMetadataMessage.model_validate_json(message.model_dump_json())
        with self._lock:
            if (not self.admits(message.session, message.metadata.provenance)
                    or message.metadata.base_people_revision != self.base_people_revision):
                raise FeatureHandoffError("feature_scope_revoked")
            def operation(state):
                if not self.admits(message.session, message.metadata.provenance):
                    raise FeatureHandoffError("feature_scope_revoked")
                if state["annotations"] != self._annotations or tuple(tuple(x) for x in state["blocked"]) != self._blocked:
                    raise PeopleError("people_revision_conflict")
                observations = {o.observation_id: o for o in message.observations}
                states = {sid: group.state for group in message.metadata.groups for sid in group.observation_ids}
                incoming, by_observation = {}, {}
                for oid, observation in observations.items():
                    source, frame, camera, sid = observation_refs(message.session, observation)
                    by_observation[oid] = sid
                    status = "excluded" if state["annotations"].get(sid, {}).get("excluded") else states[oid]
                    sample = Sample(sample_id=sid, source_digest=source, frame_ref=frame, face_slot=observation.face_slot,
                        camera_ref=camera, track_ref=observation.track_id, observation_ref=oid,
                        observed_monotonic=observation.timing.observed_monotonic,
                        model_sha256=observation.provenance.embedding_model_sha256, state=status, active=True)
                    previous = state["samples"].get(sid)
                    if previous and previous["model_sha256"] != sample.model_sha256:
                        raise PeopleError("people_model_mismatch")
                    incoming[sid] = sample.model_dump()
                if len(set(state["samples"]) | set(incoming)) > MAX_SAMPLES:
                    raise PeopleError("people_sample_capacity")
                old = [(c["person_id"], c["sample_ids"]) for c in state["clusters"]]
                for sample in state["samples"].values():
                    sample["active"] = False
                state["samples"].update(incoming)
                groups = []
                for group in message.metadata.groups:
                    members = [by_observation[oid] for oid in group.observation_ids
                        if incoming[by_observation[oid]]["state"] != "excluded"]
                    if not members:
                        continue
                    representatives = [by_observation[oid] for oid in group.representative_ids
                        if by_observation[oid] in members]
                    groups.append((members, representatives or members[:1]))
                catalog._put_clusters(state, groups)
                if old != [(c["person_id"], c["sample_ids"]) for c in state["clusters"]]:
                    catalog._invalidate_links(state)
                state["last_run"] = Run(model_sha256=self.policy.model_sha256,
                    threshold=float(self.policy.accept_similarity), ambiguity_margin=float(self.policy.ambiguity_margin),
                    policy_reference=self.policy.calibration_reference, input_count=len(observations),
                    backend="synthetic-contract-fake" if self._factory else "scikit-learn").model_dump()
            try:
                page = catalog._change(self.base_people_revision, operation, undo=False)
            except sqlite3.Error:
                raise PeopleError("people_storage_unavailable") from None
            self._consumed = True
            return page


def close_people_catalog(catalog):
    # API lifespan awaits owned worker shutdown first, then drains review lock.
    with catalog._lock:
        catalog.connection.close()


def qualify_people_catalog(catalog):
    """Validate an owner-supplied dedicated connection; never open a source path."""
    from .store import local_database_path
    if not isinstance(catalog, PeopleCatalog):
        raise PeopleError("people_catalog_required")
    with catalog._lock:
        connection = catalog.connection
        if connection.in_transaction:
            raise PeopleError("people_dedicated_connection_required")
        # Sync routes and lifespan close use other threads; verify the supplied
        # connection permits that without constructing or replacing it here.
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                executor.submit(lambda: list(connection.execute("PRAGMA database_list"))).result()
            except Exception:
                raise PeopleError("people_cross_thread_connection_required") from None
        databases = list(connection.execute("PRAGMA database_list"))
        if len(databases) != 1 or databases[0][1] != "main":
            raise PeopleError("people_dedicated_connection_required")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {"people_state", "people_undo"}:
            raise PeopleError("people_dedicated_connection_required")
        filename = databases[0][2]
        if filename:
            path = local_database_path(Path(filename))
            info = path.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PeopleError("people_private_database_required")
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        max_pages = MAX_PEOPLE_DATABASE_BYTES // page_size
        if connection.execute("PRAGMA page_count").fetchone()[0] > max_pages:
            raise PeopleError("people_storage_capacity")
        connection.execute(f"PRAGMA max_page_count={max_pages}")
        connection.execute("PRAGMA journal_size_limit=1048576")
        connection.execute("PRAGMA secure_delete=ON")
