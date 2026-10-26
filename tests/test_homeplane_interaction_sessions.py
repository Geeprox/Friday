"""Private fixture bindings test isolation; no real biometric/ASR qualification."""

import time
from dataclasses import replace

import pytest

from friday.core import Evidence
from homeplane.attention_policy import attention_policy
from homeplane.interaction_contracts import AttentionAdmission, InteractionBinding
from homeplane.interaction_sessions import InteractionSessions


def bound(**changes):
    values = dict(
        input_kind="ambient", session_id="owner-session", room_id="客厅", camera_id="living-room",
        map_revision="map-1", source_revision="source-1", authority_revision="authority-1",
        boot_id="boot-1", generation=1, subject_ref="fixture-A", track_ref="track-1",
        track_epoch="epoch-1", associated=True, speaker_count=1,
    )
    values.update(changes)
    return InteractionBinding(**values)


def admitted(binding=None, **changes):
    binding = binding or bound()
    values = dict(
        producer_ref="fixture-only-producer", source_revision=binding.source_revision,
        speaker_ref=binding.subject_ref, track_ref=binding.track_ref, track_epoch=binding.track_epoch,
        room_id=binding.room_id, camera_id=binding.camera_id, captured_at=time.time(),
        expires_at=time.time() + 30, source_kind="fixture", final_transcript=True,
        addressed_to_system=True, quality_ok=True, gaze="unavailable", pointing_admitted=True,
    )
    values.update(changes)
    return AttentionAdmission(**values)


@pytest.mark.parametrize("change", [
    {"subject_ref": "fixture-B"}, {"subject_ref": None, "associated": False},
    {"speaker_count": 2}, {"room_id": "卧室"}, {"track_epoch": "recycled-epoch-2"},
    {"camera_id": "other"}, {"map_revision": "map-2"}, {"source_revision": "source-2"},
    {"authority_revision": "revoked"}, {"boot_id": "boot-2"}, {"generation": 2},
])
def test_every_binding_boundary_drops_focus_and_late_commit(change):
    sessions = InteractionSessions()
    binding = bound()
    first = sessions.resolve(binding)
    assert sessions.commit(first.fence, "light-A", status="reported_match", ttl_seconds=15)
    assert sessions.resolve(binding).target_id == "light-A"
    assert sessions.resolve(replace(binding, **change)).target_id is None
    assert not sessions.commit(first.fence, "light-A", status="reported_match", ttl_seconds=15)
    assert sessions.resolve(binding).target_id is None


def test_console_is_server_session_scoped_and_cannot_claim_person():
    sessions = InteractionSessions()
    console = bound(input_kind="console_text", subject_ref=None, track_ref=None, track_epoch=None,
                    associated=False, speaker_count=0)
    first = sessions.resolve(console)
    sessions.commit(first.fence, "lamp", status="reported_match", ttl_seconds=15)
    assert sessions.resolve(replace(console, session_id="second-console")).target_id is None
    assert sessions.resolve(console).target_id == "lamp"
    with pytest.raises(ValueError, match="console_is_not_person_evidence"):
        replace(console, subject_ref="client-label")


def test_finite_ttl_invalidation_and_eviction_cannot_revive_reference():
    now = [100.0]
    sessions = InteractionSessions(clock=lambda: now[0], max_sessions=2)
    first = sessions.resolve(bound(deadline_mono=110))
    assert sessions.commit(first.fence, "lamp", status="reported_match", ttl_seconds=5)
    now[0] = 106
    assert sessions.resolve(bound(deadline_mono=110)).target_id is None
    sessions.invalidate(session_id="owner-session")
    assert not sessions.commit(first.fence, "lamp", status="reported_match", ttl_seconds=5)
    active = sessions.resolve(bound(session_id="a"))
    sessions.resolve(bound(session_id="b"))
    sessions.resolve(bound(session_id="c"))
    assert len(sessions._slots) == 2
    assert not sessions.current(active.fence)
    now[0] = 500
    assert sessions.resolve(bound(deadline_mono=110)).fence is None


@pytest.mark.parametrize("status", ["planned", "rejected", "unknown_outcome", "accepted", "failed"])
def test_selection_ack_or_unknown_result_never_establishes_focus(status):
    sessions = InteractionSessions()
    first = sessions.resolve(bound())
    assert not sessions.commit(first.fence, "lamp", status=status, ttl_seconds=10)
    assert sessions.latest is None


@pytest.mark.parametrize("gaze", ["unavailable", "not_looking"])
def test_qualified_addressed_command_does_not_require_gaze_pointing_or_wake(gaze):
    binding = bound()
    decision = attention_policy(
        evidence=Evidence(live_speaker=True, people_count=1, attention=.99),
        admission=admitted(binding, gaze=gaze, pointing_admitted=False), binding=binding,
        explicit_command=True, wake=False, wake_required=False, threshold=.85, max_age_seconds=12,
        allow_simulated=True,
    )
    assert decision.admitted and decision.gaze == gaze and decision.producer_state == "simulated"


@pytest.mark.parametrize("patch", [
    {"source_kind": "tv"}, {"source_kind": "playback"}, {"source_kind": "unknown"},
    {"final_transcript": False}, {"addressed_to_system": False}, {"quality_ok": False},
    {"track_epoch": "different-epoch"}, {"captured_at": time.time() - 40},
])
def test_negative_admission_never_enters_action_chain(patch):
    binding = bound()
    decision = attention_policy(
        evidence=Evidence(live_speaker=True, people_count=1, attention=.99),
        admission=admitted(binding, **patch), binding=binding,
        explicit_command=True, wake=False, wake_required=False, threshold=.85, max_age_seconds=12,
        allow_simulated=True,
    )
    assert not decision.admitted


def test_legacy_booleans_and_fixture_cannot_qualify_default_production_consumer():
    binding = bound()
    values = dict(evidence=Evidence(live_speaker=True, people_count=1, facing_camera=True, attention=.99),
                  binding=binding, explicit_command=True, wake=True, wake_required=False,
                  threshold=.85, max_age_seconds=12)
    assert not attention_policy(admission=None, **values).admitted
    assert not attention_policy(admission=admitted(binding), **values).admitted
    assert not attention_policy(admission=admitted(binding), allow_simulated=True,
                                **{**values, "wake_required": True, "wake": False}).admitted
