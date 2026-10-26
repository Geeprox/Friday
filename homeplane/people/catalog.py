"""Revision-fenced, non-destructive local people metadata; synthetic admission only."""

import json
import secrets
import sqlite3
import threading
from functools import wraps

from .clustering import FaceInput, digest, proposals
from .contracts import (
    Cluster,
    Correct,
    Edit,
    Merge,
    Name,
    PeoplePage,
    Person,
    ReviewVoice,
    Run,
    Sample,
    Split,
    VoiceLink,
)

MAX_SAMPLES, MAX_PEOPLE, MAX_LINKS, MAX_UNDO, MAX_BLOCKED = 512, 128, 256, 32, 4096


class PeopleError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return call


class PeopleCatalog:
    """Use an owner-supplied, dedicated local SQLite connection.

    No NAS reader, filesystem/media operations, inference or authority objects
    exist here. Embeddings are used during reindex and never enter SQLite/API.
    """

    def __init__(self, connection: sqlite3.Connection, *, _cluster_factory=None):
        self.connection = connection
        self._lock = threading.RLock()
        self._cluster_factory = _cluster_factory
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS people_state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS people_undo (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO people_state VALUES (1,?)",
                (
                    json.dumps(
                        {
                            "schema_version": 1,
                            "revision": 0,
                            "samples": {},
                            "people": {},
                            "clusters": [],
                            "annotations": {},
                            "blocked": [],
                            "voice_links": {},
                            "voice_evidence": {},
                            "last_run": None,
                        }
                    ),
                ),
            )

    def _read(self):
        state = json.loads(self.connection.execute("SELECT body FROM people_state WHERE id=1").fetchone()[0])
        if state["schema_version"] != 1:
            raise PeopleError("people_schema_unavailable")
        return state

    def _page(self, state):
        counts = {}
        for cluster in state["clusters"]:
            if cluster["person_id"]:
                counts[cluster["person_id"]] = counts.get(cluster["person_id"], 0) + len(
                    cluster["sample_ids"]
                )
        return PeoplePage(
            revision=state["revision"],
            people=[
                Person(**person, sample_count=counts.get(pid, 0))
                for pid, person in sorted(state["people"].items())
            ],
            clusters=[Cluster(**value) for value in state["clusters"]],
            samples=[Sample(**value) for _, value in sorted(state["samples"].items())],
            voice_links=[VoiceLink(**value) for _, value in sorted(state["voice_links"].items())],
            last_run=Run(**state["last_run"]) if state["last_run"] else None,
            undo_available=self.connection.execute("SELECT 1 FROM people_undo LIMIT 1").fetchone()
            is not None,
        )

    @serialized
    def page(self):
        return self._page(self._read())

    def register_profile(self, name, expected_revision, person_id=None, *, _before_commit=None):
        """Explicit live enrollment reuses this catalog's names and durable IDs."""
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40 or any(ord(c) < 32 for c in name):
            raise PeopleError("people_invalid_name")
        selected = []

        def operation(state):
            if person_id is not None:
                person = state["people"].get(person_id)
                if not person or person["merged_into"] or person["name"] != name.strip():
                    raise PeopleError("people_person_not_found")
                selected.append(person_id)
                if _before_commit is not None:
                    _before_commit(person_id, self.connection)
                return
            if any(p["name"] and p["name"].casefold() == name.strip().casefold() and not p["merged_into"]
                   for p in state["people"].values()):
                raise PeopleError("people_name_conflict")
            pid = self._person(state)
            state["people"][pid]["name"] = name.strip()
            selected.append(pid)
            if _before_commit is not None:
                _before_commit(pid, self.connection)

        self._change(expected_revision, operation, undo=False)
        return selected[0]

    @serialized
    def _change(self, expected_revision, operation, *, undo=True):
        if type(expected_revision) is not int or expected_revision < 0:
            raise PeopleError("people_invalid_revision")
        # A dedicated connection lets BEGIN IMMEDIATE fence parallel writers.
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            state = self._read()
            if state["revision"] != expected_revision:
                raise PeopleError("people_revision_conflict")
            before = json.dumps(state, sort_keys=True)
            operation(state)
            state["revision"] += 1
            if undo:
                self.connection.execute("INSERT INTO people_undo(body) VALUES (?)", (before,))
                self.connection.execute(
                    "DELETE FROM people_undo WHERE id NOT IN (SELECT id FROM people_undo ORDER BY id DESC LIMIT ?)",
                    (MAX_UNDO,),
                )
            else:
                self.connection.execute("DELETE FROM people_undo")
            # Validate public finite projection before committing any mutation.
            result = self._page(state)
            self.connection.execute(
                "UPDATE people_state SET body=? WHERE id=1", (json.dumps(state, sort_keys=True),)
            )
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise

    @staticmethod
    def _person(state):
        if len(state["people"]) >= MAX_PEOPLE:
            raise PeopleError("people_person_capacity")
        pid = "person_" + secrets.token_hex(12)
        state["people"][pid] = {"person_id": pid, "name": None, "merged_into": None}
        return pid

    @staticmethod
    def _cluster(state, cid):
        for cluster in state["clusters"]:
            if cluster["cluster_id"] == cid:
                return cluster
        raise PeopleError("people_cluster_not_found")

    @staticmethod
    def _put_clusters(state, groups):
        clusters = []
        for ids, representatives in groups:
            ids = sorted(ids)
            anchors = {state["annotations"].get(sid, {}).get("person_id") for sid in ids} - {None}
            if len(anchors) > 1:
                raise PeopleError("people_label_conflict")
            pid = next(iter(anchors), None)
            sample_states = {state["samples"][sid]["state"] for sid in ids}
            status = (
                "ambiguous"
                if "ambiguous" in sample_states
                else "unknown"
                if sample_states == {"unknown"}
                else "named"
                if pid and state["people"][pid]["name"]
                else "candidate"
            )
            clusters.append(
                {
                    "cluster_id": digest(["friday-cluster/1", *ids]),
                    "person_id": pid,
                    "sample_ids": ids,
                    "representatives": representatives[:3],
                    "state": status,
                }
            )
        state["clusters"] = sorted(clusters, key=lambda c: c["cluster_id"])

    def reindex(self, inputs: tuple[FaceInput, ...], policy, *, expected_revision):
        """Trusted synthetic input, never an HTTP upload or activation operation."""

        def operation(state):
            annotations = state["annotations"]
            try:
                groups, statuses = proposals(
                    inputs,
                    policy,
                    annotations,
                    {frozenset(pair) for pair in state["blocked"]},
                    _factory=self._cluster_factory,
                )
            except ValueError as error:
                raise PeopleError(str(error)) from None
            incoming = {}
            for value in inputs:
                try:
                    sample = Sample(
                        sample_id=value.sample_id,
                        source_digest=value.source_digest,
                        frame_ref=value.frame_ref,
                        face_slot=value.face_slot,
                        camera_ref=value.camera_ref,
                        track_ref=value.track_ref,
                        observation_ref=value.observation_ref,
                        observed_monotonic=float(value.observed_monotonic)
                        if value.observed_monotonic is not None
                        else None,
                        model_sha256=value.embedding.model_sha256,
                        state=statuses[value.sample_id],
                        active=True,
                    )
                except ValueError:
                    raise PeopleError("people_invalid_sample") from None
                incoming[sample.sample_id] = sample.model_dump()
            if len(set(state["samples"]) | set(incoming)) > MAX_SAMPLES:
                raise PeopleError("people_sample_capacity")
            old_membership = [(c["person_id"], c["sample_ids"]) for c in state["clusters"]]
            for sample in state["samples"].values():
                sample["active"] = False
            state["samples"].update(incoming)
            self._put_clusters(state, groups)
            if old_membership != [(c["person_id"], c["sample_ids"]) for c in state["clusters"]]:
                self._invalidate_links(state)
            state["last_run"] = Run(
                model_sha256=policy.model_sha256,
                threshold=float(policy.accept_similarity),
                ambiguity_margin=float(policy.ambiguity_margin),
                policy_reference=policy.calibration_reference,
                input_count=len(inputs),
                backend="synthetic-contract-fake" if self._cluster_factory else "scikit-learn",
            ).model_dump()

        return self._change(expected_revision, operation, undo=False)

    @staticmethod
    def _invalidate_links(state):
        # Membership corrections change the meaning of previous speaker evidence.
        # Keep rejection/unlink decisions durable; require new evidence otherwise.
        for link in state["voice_links"].values():
            if link["state"] in ("candidate", "confirmed", "stale"):
                link["state"] = "stale"
                state["voice_evidence"].pop(link["link_id"], None)

    def name(self, request: Name):
        def operation(state):
            cluster = self._cluster(state, request.cluster_id)
            if cluster["state"] in ("unknown", "ambiguous"):
                raise PeopleError("people_sample_review_required")
            pid = cluster["person_id"]
            if any(
                p["name"]
                and p["name"].casefold() == request.name.casefold()
                and key != pid
                and not p["merged_into"]
                for key, p in state["people"].items()
            ):
                raise PeopleError("people_name_conflict")
            pid = pid or self._person(state)
            state["people"][pid]["name"] = request.name
            for sid in cluster["sample_ids"]:
                state["annotations"].setdefault(sid, {})["person_id"] = pid
            self._put_clusters(state, [(c["sample_ids"], c["representatives"]) for c in state["clusters"]])

        return self._change(request.expected_revision, operation)

    def merge(self, request: Merge):
        def operation(state):
            if len(set(request.cluster_ids)) != len(request.cluster_ids):
                raise PeopleError("people_duplicate_cluster")
            selected = [self._cluster(state, cid) for cid in request.cluster_ids]
            pids = {c["person_id"] for c in selected} - {None}
            if len(pids) > 1 and request.target_person_id not in pids:
                raise PeopleError("people_merge_target_required")
            if request.target_person_id and request.target_person_id not in pids:
                raise PeopleError("people_merge_target_invalid")
            pid = request.target_person_id or next(iter(pids), None) or self._person(state)
            ids = sorted({sid for c in selected for sid in c["sample_ids"]})
            if len(ids) > 256:
                raise PeopleError("people_cluster_capacity")
            for sid in ids:
                state["annotations"].setdefault(sid, {})["person_id"] = pid
                state["samples"][sid]["state"] = "candidate"
            for old in pids - {pid}:
                state["people"][old]["merged_into"] = pid
                # Update historical anchors too, preserving labels as aliases.
                for annotation in state["annotations"].values():
                    if annotation.get("person_id") == old:
                        annotation["person_id"] = pid
            state["blocked"] = [pair for pair in state["blocked"] if not set(pair) <= set(ids)]
            groups = [(c["sample_ids"], c["representatives"]) for c in state["clusters"] if c not in selected]
            self._put_clusters(state, [*groups, (ids, ids[:3])])
            self._invalidate_links(state)

        return self._change(request.expected_revision, operation)

    def split(self, request: Split):
        def operation(state):
            cluster = self._cluster(state, request.cluster_id)
            selected = set(request.sample_ids)
            if len(selected) != len(request.sample_ids) or not selected < set(cluster["sample_ids"]):
                raise PeopleError("people_split_subset_required")
            retained = set(cluster["sample_ids"]) - selected
            if len(state["blocked"]) + len(selected) * len(retained) > MAX_BLOCKED:
                raise PeopleError("people_correction_capacity")
            pid = self._person(state)
            for sid in selected:
                state["annotations"].setdefault(sid, {})["person_id"] = pid
                state["samples"][sid]["state"] = "candidate"
            # Anchor the retained group too so ambiguous earlier samples stay separated.
            old_pid = cluster["person_id"] or self._person(state)
            for sid in retained:
                state["annotations"].setdefault(sid, {})["person_id"] = old_pid
            state["blocked"] += [sorted((a, b)) for a in sorted(selected) for b in sorted(retained)]
            groups = [(c["sample_ids"], c["representatives"]) for c in state["clusters"] if c != cluster]
            self._put_clusters(
                state,
                [*groups, (sorted(selected), sorted(selected)[:3]), (sorted(retained), sorted(retained)[:3])],
            )
            self._invalidate_links(state)

        return self._change(request.expected_revision, operation)

    def correct(self, request: Correct):
        def operation(state):
            if request.sample_id not in state["samples"]:
                raise PeopleError("people_sample_not_found")
            annotation = state["annotations"].setdefault(request.sample_id, {})
            if request.operation == "move":
                person = state["people"].get(request.person_id)
                if not person or person["merged_into"]:
                    raise PeopleError("people_person_not_found")
                annotation.update(person_id=request.person_id, excluded=False)
            elif request.operation == "separate":
                annotation.update(person_id=self._person(state), excluded=False)
            elif request.operation == "exclude":
                annotation["excluded"] = True
            else:
                annotation["excluded"] = False
            state["samples"][request.sample_id]["state"] = (
                "excluded" if annotation.get("excluded") else "candidate"
            )
            buckets = {}
            for cluster in state["clusters"]:
                for sid in cluster["sample_ids"]:
                    if sid == request.sample_id or state["annotations"].get(sid, {}).get("excluded"):
                        continue
                    key = state["annotations"].get(sid, {}).get("person_id") or cluster["cluster_id"]
                    buckets.setdefault(key, []).append(sid)
            sample = state["samples"][request.sample_id]
            if sample["active"] and not annotation.get("excluded"):
                key = annotation.get("person_id") or request.sample_id
                buckets.setdefault(key, []).append(request.sample_id)
            self._put_clusters(state, [(sorted(ids), sorted(ids)[:3]) for ids in buckets.values()])
            self._invalidate_links(state)

        return self._change(request.expected_revision, operation)

    @serialized
    def undo(self, request: Edit):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._read()
            if current["revision"] != request.expected_revision:
                raise PeopleError("people_revision_conflict")
            row = self.connection.execute(
                "SELECT id,body FROM people_undo ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                raise PeopleError("people_undo_unavailable")
            previous = json.loads(row[1])
            previous["revision"] = current["revision"] + 1
            self.connection.execute("DELETE FROM people_undo WHERE id=?", (row[0],))
            result = self._page(previous)
            self.connection.execute(
                "UPDATE people_state SET body=? WHERE id=1", (json.dumps(previous, sort_keys=True),)
            )
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise

    def propose_voice(self, evidence, *, expected_revision):
        """Trusted synthetic evidence only; no timing-overlap or request-JSON evidence."""
        from .recognition_bridge import SpeakerAgreement

        if not isinstance(evidence, SpeakerAgreement) or not evidence.qualified:
            raise PeopleError("people_speaker_evidence_unqualified")

        def operation(state):
            cluster = next((c for c in state["clusters"] if evidence.face_sample_id in c["sample_ids"]), None)
            if not cluster or not cluster["person_id"] or cluster["state"] != "named":
                raise PeopleError("people_named_face_required")
            pid = cluster["person_id"]
            lid = digest(["friday-voice-link/1", pid, evidence.voice_ref, evidence.model_sha256])
            if lid not in state["voice_links"] and len(state["voice_links"]) >= MAX_LINKS:
                raise PeopleError("people_voice_capacity")
            competing = [
                link
                for link in state["voice_links"].values()
                if link["voice_ref"] == evidence.voice_ref
                and link["person_id"] != pid
                and link["state"] not in ("rejected", "unlinked")
                and bool(state["voice_evidence"].get(link["link_id"]))
            ]
            link = state["voice_links"].setdefault(
                lid,
                VoiceLink(
                    link_id=lid,
                    person_id=pid,
                    voice_ref=evidence.voice_ref,
                    state="stale",
                    agreement_count=0,
                    independent_windows=0,
                ).model_dump(),
            )
            if link["state"] in ("rejected", "unlinked"):
                raise PeopleError("people_voice_decision_retained")
            windows = state["voice_evidence"].setdefault(lid, {})
            # Duplicate/replayed window IDs cannot increase evidence.
            if evidence.window_id in windows:
                if windows[evidence.window_id] != evidence.event_id:
                    raise PeopleError("people_voice_window_conflict")
            elif len(windows) >= 32:
                raise PeopleError("people_voice_evidence_capacity")
            else:
                windows[evidence.window_id] = evidence.event_id
            distinct = len(set(windows.values()))
            link.update(agreement_count=len(windows), independent_windows=distinct)
            if competing:
                for item in competing:
                    item["state"] = "stale"
                link["state"] = "stale"
            elif len(windows) >= 3 and distinct >= 3 and link["state"] != "confirmed":
                link["state"] = "candidate"

        return self._change(expected_revision, operation, undo=False)

    def review_voice(self, request: ReviewVoice):
        def operation(state):
            link = state["voice_links"].get(request.link_id)
            if not link:
                raise PeopleError("people_voice_not_found")
            if request.decision == "confirm":
                if link["state"] != "candidate":
                    raise PeopleError("people_voice_review_not_ready")
                if any(
                    other["voice_ref"] == link["voice_ref"]
                    and other["person_id"] != link["person_id"]
                    and other["state"] == "confirmed"
                    for other in state["voice_links"].values()
                ):
                    raise PeopleError("people_voice_conflict")
                link["state"] = "confirmed"
            else:
                link["state"] = "rejected" if request.decision == "reject" else "unlinked"

        return self._change(request.expected_revision, operation)
