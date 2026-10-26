"""Source relationships and bounded, reviewed relative calibration.

No client samples, actuator, model transport, or independent-hardware claims.
Only the parent measurement service's current qualified RAM history is learned.
"""

import time
from datetime import timedelta
from uuid import uuid4

from .measurement_profiles import MeasurementGroup
from .measurement_settings_contracts import (
    CalibrationPreview,
    GroupMember,
    MeasurementGroupView,
    SavedCalibration,
    SavedMeasurement,
    SavedMeasurementGroup,
)
from .simulator import Simulator


def identity_material(entity, snapshot):
    if snapshot.mode == "simulator" and entity.simulated:
        return ["simulated", entity.physical_source_id]
    devices = [
        d
        for d in snapshot.devices
        if entity.id in d.function_ids and not d.simulated and d.identity_basis == "xiaomi_identifier"
    ]
    if len(devices) != 1:
        return None
    identifiers = sorted(b.external_id for b in devices[0].bindings if b.source == "xiaomi-identity")
    return ["upstream_xiaomi_identifiers", identifiers] if len(identifiers) == 1 else None


def contextual_device(entity, snapshot):
    return (
        entity.kind == "climate"
        or any(c.name == "climate.set" for c in entity.capabilities)
        or any(entity.id in d.function_ids and d.kind == "climate" for d in snapshot.devices)
    )


class MeasurementEnrollment:
    def __init__(self, owner):
        self.owner = owner
        self.candidates = {}
        self.approval_carry = None

    def identity(self, entity, snapshot):
        from .measurement_settings import digest

        value = identity_material(entity, snapshot)
        return digest(value) if value else None

    def role(self, adapter, snapshot, entity, saved):
        if contextual_device(entity, snapshot):
            return "contextual_air"
        if saved and saved.edited:
            return saved.representativeness
        if isinstance(adapter, Simulator):
            return adapter.measurements.reference_roles.get(entity.physical_source_id, "unreviewed")
        return "unreviewed"

    def _check(self, adapter, snapshot, request):
        realm = self.owner._realm(adapter, snapshot)
        config = self.owner._config(realm)
        if request.realm != realm or request.expected_revision != config.revision:
            raise ValueError("source_conflict")
        return realm, config

    def group_fingerprint(self, saved, profiles):
        from .measurement_settings import digest

        return digest(
            [saved.model_dump(mode="json"), [(s, profiles[s].fingerprint()) for s in saved.group.sources]]
        )

    def group_valid(self, adapter, snapshot, saved, profiles, config):
        from .measurement_settings import binding_fingerprint, source_key

        candidates, _ = self.owner._inventory(snapshot)
        entities = {source_key(e): e for e in candidates}
        records = {r.source_key: r for r in config.sources}
        physical = []
        for member in saved.members:
            entity = entities.get(member.source_key)
            if entity is None or binding_fingerprint(entity) != member.binding_fingerprint:
                return False
            current = self.identity(entity, snapshot)
            if current != member.identity_fingerprint or len(saved.members) > 1 and current is None:
                return False
            physical.append(current)
            if entity.room != saved.group.location or entity.physical_source_id not in profiles:
                return False
            if (
                entity.physical_source_id == saved.group.primary
                and self.role(adapter, snapshot, entity, records.get(member.source_key)) != "room_reference"
            ):
                return False
        return (len(saved.members) == 1 or len(set(physical)) == len(physical)) and (
            set(saved.group.sources) == {entities[m.source_key].physical_source_id for m in saved.members}
        )

    def edit_group(self, adapter, snapshot, request):
        from .measurement_settings import binding_fingerprint, digest, source_key

        realm, config = self._check(adapter, snapshot, request)
        candidates, _ = self.owner._inventory(snapshot)
        inventory = {source_key(e): e for e in candidates}
        records = {r.source_key: r for r in config.sources}
        fixture = self.owner._fixture(adapter)
        physical, identities, room, members = [], [], None, []
        for member in request.members:
            entity = inventory.get(member.source_key)
            if entity is None or binding_fingerprint(entity) != member.binding_fingerprint:
                raise ValueError("source_conflict")
            identity = self.identity(entity, snapshot)
            if member.identity_fingerprint != identity:
                raise ValueError("source_conflict")
            if len(request.members) > 1 and identity is None:
                raise ValueError("independent_source_identity_required")
            if entity.room is None or room is not None and entity.room != room:
                raise ValueError("measurement_group_room_mismatch")
            room = entity.room
            physical.append(entity.physical_source_id)
            identities.append(identity)
            previous = records.get(member.source_key)
            if previous and previous.binding_fingerprint != member.binding_fingerprint:
                raise ValueError("source_conflict")
            profile = (
                previous.profile
                if previous
                else fixture.profiles.get(entity.physical_source_id)
                if fixture
                else None
            )
            if profile is None or not (previous and previous.edited or isinstance(adapter, Simulator)):
                raise ValueError("measurement_position_review_required")
            role = self.role(adapter, snapshot, entity, previous)
            if member.source_key == request.primary_key and role != "room_reference":
                raise ValueError("room_reference_position_required")
            if previous is None:
                records[member.source_key] = SavedMeasurement(
                    source_key=member.source_key,
                    binding_fingerprint=member.binding_fingerprint,
                    profile=profile,
                    representativeness=role,
                )
            members.append(GroupMember.model_validate(member.model_dump()))
        if (
            len(set(physical)) != len(physical)
            or len(request.members) > 1
            and len(set(identities)) != len(identities)
        ):
            raise ValueError("aliased_measurement_sources")
        primary = inventory[request.primary_key].physical_source_id
        existing = next((g.group for g in config.groups if g.group.location == room), None)
        if existing is None and fixture:
            existing = next((g for g in fixture.groups.values() if g.location == room), None)
        group = MeasurementGroup(
            entity=existing.entity if existing else "measurement:room:" + digest([realm, room])[:24],
            name=request.name.strip(),
            location=room,
            primary=primary,
            sources=physical,
        )
        saved = SavedMeasurementGroup(
            group=group,
            members=members,
            independence_basis="single_source"
            if len(members) == 1
            else "simulated_fixture"
            if snapshot.mode == "simulator"
            else "upstream_xiaomi_identifiers",
        )
        groups = [g for g in config.groups if g.group.location != room] + [saved]
        if (
            len(set(fixture.profiles if fixture else {}) | {r.profile.source for r in records.values()}) > 32
            or len(set(fixture.groups if fixture else {}) | {g.group.entity for g in groups}) > 16
        ):
            raise ValueError("measurement_profile_budget")
        updated = config.model_copy(
            update={
                "revision": str(uuid4()),
                "sources": list(records.values()),
                "groups": groups,
                "calibrations": [c for c in config.calibrations if c.group_entity != group.entity],
            }
        )
        self.owner.persist(realm, config.revision, updated, snapshot, "group", group.entity)
        self.candidates.clear()

    def _prune(self, now):
        mono = time.monotonic()
        self.candidates = {
            k: v
            for k, v in self.candidates.items()
            if v["preview"].created_at <= now < v["preview"].expires_at and mono - v["mono"] < 120
        }

    def _learn(self, adapter, snapshot, request, config, *, version):
        from .measurement_settings import source_key

        saved = next((g for g in config.groups if g.group.entity == request.group_entity), None)
        engine = self.owner.engine
        if saved is None or engine is None or saved.group.entity not in engine.groups:
            raise ValueError("managed_measurement_group_required")
        if not self.group_valid(adapter, snapshot, saved, engine.profiles, config):
            raise ValueError("measurement_group_binding_changed")
        group_state = next((s for s in snapshot.states if s.id == saved.group.entity + ":temperature"), None)
        if group_state and "peer_disagreement_unresolved" in group_state.reasons:
            raise ValueError("calibration_requires_healthy_comparable_sources")
        candidates, _ = self.owner._inventory(snapshot)
        entity = next((e for e in candidates if source_key(e) == request.source_key), None)
        if entity is None:
            raise ValueError("source_conflict")
        sources = (saved.group.primary, entity.physical_source_id)
        if any(
            engine.source_states[s].quality != "healthy"
            or any(r.startswith(("battery_", "rssi_")) for r in engine.source_states[s].reasons)
            for s in sources
        ):
            raise ValueError("calibration_requires_healthy_comparable_sources")
        learned = engine.learn_relative(
            saved.group.entity, entity.physical_source_id, snapshot.generated_at, version=version
        )
        refs = [
            s.reference
            for key in (learned.reference, learned.source)
            for s in engine.history[key].samples
            if s.qualified
            and snapshot.generated_at - timedelta(seconds=engine.profiles[key].ttl_seconds)
            <= s.at
            <= snapshot.generated_at
        ]
        return saved, learned, refs

    def preview(self, adapter, snapshot, request):
        from .measurement_settings import digest

        realm, config = self._check(adapter, snapshot, request)
        self._prune(snapshot.generated_at)
        if len(self.candidates) >= 16:
            raise ValueError("calibration_candidate_budget")
        saved, learned, refs = self._learn(
            adapter, snapshot, request, config, version="relative-" + str(uuid4())
        )
        proof = digest(
            [
                self.group_fingerprint(saved, self.owner.engine.profiles),
                learned.model_dump(mode="json", exclude={"valid_until", "version"}),
                refs,
            ]
        )
        preview = CalibrationPreview(
            candidate_id=str(uuid4()),
            fingerprint=proof,
            realm=realm,
            revision=config.revision,
            group_entity=saved.group.entity,
            created_at=snapshot.generated_at,
            expires_at=snapshot.generated_at + timedelta(seconds=120),
            calibration=learned,
            evidence_refs=refs[-32:],
        )
        self.candidates[preview.candidate_id] = {
            "preview": preview,
            "request": request,
            "epoch": self.owner.epoch,
            "mono": time.monotonic(),
        }
        return preview

    def approve(self, adapter, snapshot, request):
        from .measurement_settings import digest

        realm, config = self._check(adapter, snapshot, request)
        self._prune(snapshot.generated_at)
        candidate = self.candidates.get(request.candidate_id)
        if candidate is None or candidate["epoch"] != self.owner.epoch:
            raise ValueError("calibration_candidate_expired")
        preview = candidate["preview"]
        if (
            preview.realm != realm
            or preview.revision != config.revision
            or preview.fingerprint != request.fingerprint
        ):
            raise ValueError("source_conflict")
        saved, learned, refs = self._learn(
            adapter, snapshot, candidate["request"], config, version=preview.calibration.version
        )
        proof = digest(
            [
                self.group_fingerprint(saved, self.owner.engine.profiles),
                learned.model_dump(mode="json", exclude={"valid_until", "version"}),
                refs,
            ]
        )
        if proof != preview.fingerprint:
            raise ValueError("calibration_evidence_changed")
        # Use the reviewed expiry and values. Approval cannot silently extend it.
        active = SavedCalibration(
            group_entity=saved.group.entity,
            group_fingerprint=self.group_fingerprint(saved, self.owner.engine.profiles),
            calibration=preview.calibration,
            approved_at=snapshot.generated_at,
        )
        calibrations = [
            c
            for c in config.calibrations
            if (c.group_entity, c.calibration.source) != (active.group_entity, active.calibration.source)
        ] + [active]
        updated = config.model_copy(update={"revision": str(uuid4()), "calibrations": calibrations})
        self.owner.persist(
            realm, config.revision, updated, snapshot, "calibration-approval", saved.group.entity
        )
        self.approval_carry = (realm, updated.revision, self.owner.signature, self.owner.engine)
        self.candidates.clear()

    def views(self, adapter, snapshot, config, profiles):
        from .measurement_settings import source_key

        candidates, _ = self.owner._inventory(snapshot)
        keys = {e.physical_source_id: source_key(e) for e in candidates}
        fixture = self.owner._fixture(adapter)
        groups = {g.entity: g for g in fixture.groups.values()} if fixture else {}
        groups.update({g.group.entity: g.group for g in config.groups})
        result = []
        for group in groups.values():
            managed = next((g for g in config.groups if g.group.entity == group.entity), None)
            valid = bool(managed and self.group_valid(adapter, snapshot, managed, profiles, config))
            active = next((c for c in config.calibrations if c.group_entity == group.entity), None)
            status = "none"
            reasons = []
            if managed and not valid:
                reasons.append("measurement_group_binding_changed")
            if active:
                status = (
                    "expired"
                    if active.calibration.valid_until <= snapshot.generated_at
                    else "active"
                    if valid and active.group_fingerprint == self.group_fingerprint(managed, profiles)
                    else "invalid"
                )
                if status != "active":
                    reasons.append("calibration_scope_expired_or_changed")
            result.append(
                MeasurementGroupView(
                    entity=group.entity,
                    name=group.name,
                    location=group.location,
                    primary_key=keys.get(group.primary),
                    source_keys=[keys[s] for s in group.sources if s in keys],
                    managed=managed is not None,
                    binding_valid=valid,
                    independence_basis=managed.independence_basis
                    if managed
                    else "simulated_fixture_unmanaged",
                    state=next((s for s in snapshot.states if s.id == group.entity + ":temperature"), None),
                    calibration_status=status,
                    calibration=active.calibration if active else None,
                    reasons=reasons,
                )
            )
        return result

    def close(self):
        self.candidates.clear()
        self.approval_carry = None
