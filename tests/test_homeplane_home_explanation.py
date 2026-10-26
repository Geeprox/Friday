"""Original domain consumers over synthetic local records; no live media/IO.

The synthetic reader guard verifies composition only. It is not the F5 media
route and cannot qualify household NAS, decoded original video or audibility.
"""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from homeplane.archive_index import ArchiveIndex
from homeplane.archive_playback import ArchivePlayer
from homeplane.contracts import Event
from homeplane.core import HomeCore
from homeplane.home_explanation import HomeExplanation
from homeplane.home_flow_contracts import FlowRequest, TrustedFlowContext
from homeplane.home_flow_ports import ArchivePorts, QueryPorts
from homeplane.query import HomeQueries
from homeplane.query_contracts import ReadScope
from homeplane.settings import SettingsService
from homeplane.simulator import Simulator
from homeplane.store import TraceStore

NOW = datetime(2026, 10, 2, 1, tzinfo=UTC)
CAMERA = "simulated:archive:living"


class Clock:
    wall, mono = NOW, 1000.0

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


class SyntheticReaderGuard:
    """Finite private capability substitute; never starts a reader or service."""
    def __init__(self, rig):
        self.rig, self.bindings = rig, {}

    def bind(self, plan, request, context, *, deadline_mono):
        self.bindings[plan.plan_id] = (request, context, deadline_mono)
        return True

    def revoke(self, plan_id, context):
        self.rig.player.revoke(plan_id)
        self.bindings.pop(plan_id, None)

    def check(self, plan_id):
        request, context, deadline = self.bindings[plan_id]
        try:
            context.read_scope()
            if self.rig.clock.mono >= deadline or not self.rig.live.content or not self.rig.live.metadata:
                raise ValueError("synthetic_content_lease_expired")
            self.rig.player.valid(self.rig.player.leases[plan_id])
        except Exception:
            self.revoke(plan_id, context)
            raise


@pytest.fixture
async def rig(tmp_path):
    clock = Clock()
    store = TraceStore(tmp_path / "SIMULATED-F3.sqlite", wall_now=lambda: clock.wall)
    adapter = Simulator(wall_now=lambda: clock.wall, monotonic=lambda: clock.mono)
    lamp = next(e for e in adapter.entities if e.id == "light.hall")
    adapter.entities.append(lamp.model_copy(update={"id": "light.bedroom", "name": "卧室灯", "room": "bedroom",
                                                  "physical_source_id": "SIMULATED-bedroom-light"}))
    core = HomeCore(adapter, store)
    query = HomeQueries(core, wall_now=lambda: clock.wall, monotonic=lambda: clock.mono)
    live = SimpleNamespace(metadata=False, content=False, mapping=True, source="source-v1", cancelled=False)
    scopes = {"A": ReadScope(subject="console", revision="SIMULATED-A", expires_at=NOW + timedelta(minutes=5),
                             boundary="loopback_console", rooms=frozenset({"bedroom"}),
                             history=True, permitted=True),
              "B": ReadScope(subject="console", revision="SIMULATED-B", expires_at=NOW + timedelta(minutes=5),
                             boundary="loopback_console", rooms=frozenset({"hall"}),
                             history=True, permitted=True)}
    calls, returned, archive_calls, runner_calls = [], [], [], []
    original_read = query.read

    async def observed_read(request, scope, **kwargs):
        calls.append(request)
        result = await original_read(request, scope, **kwargs)
        returned.append(result.model_dump(mode="json"))
        return result

    query.read = observed_read

    async def synthetic_probe(root, operation, payload):
        assert operation == "playback-info"
        runner_calls.append(operation)
        return {"status": "readable", "media_metadata_valid": True, "media_start_seconds": 0,
                "size_bytes": 128, "duration_seconds": 60, "signature": "SIMULATED",
                "previous_keyframe_seconds": 0, "indexed_keyframes_seconds": [0],
                "audio_track": False, "codecs": ["SIMULATED"]}

    index = ArchiveIndex(store, SettingsService(tmp_path / "SIMULATED-settings.json"), lambda: True,
                         runner=synthetic_probe, clock=lambda: clock.wall)
    original_resolve = index.resolve

    async def observed_resolve(request):
        archive_calls.append(request)
        return await original_resolve(request)

    index.resolve = observed_resolve
    player = ArchivePlayer(index, monotonic=lambda: clock.mono)

    async def forbidden_trace(*args):
        pytest.fail("opaque causation/correlation must not become arbitrary trace access")

    ports = ArchivePorts(
        index, player, verify_metadata=lambda c, r: live.metadata and r.camera_id == CAMERA,
        verify_content=lambda c, r: live.content and r.camera_id == CAMERA,
        content_deadline=lambda c, r: clock.mono + 5 if live.content else None,
        cameras_for_room=lambda c, room: (CAMERA,) if live.mapping and room == "bedroom" else (),
    )
    service = HomeExplanation(QueryPorts(query, read_trace=forbidden_trace), ports,
                              room_aliases={"卧室": "bedroom", "玄关": "hall"})
    value = SimpleNamespace(clock=clock, store=store, adapter=adapter, core=core, query=query, live=live,
                            scopes=scopes, index=index, player=player, service=service, ports=ports,
                            calls=calls, returned=returned, archive_calls=archive_calls, runner_calls=runner_calls)

    def context(session="A", **kwargs):
        return TrustedFlowContext(
            input_kind="console_text", session_id=session, source_revision=live.source,
            authority_revision=scopes[session].revision, boot_id="SIMULATED-boot", generation=0,
            deadline_mono=clock.mono + 60, check_current=lambda c: c.source_revision == live.source,
            cancelled=lambda: live.cancelled, current_scope=lambda: scopes[session],
            household_timezone="Asia/Shanghai", monotonic=lambda: clock.mono, wall_now=lambda: clock.wall,
            **kwargs,
        )

    value.context = context
    yield value
    await service.close()
    await player.close()
    await index.close()
    query.close()
    store.close()


def turn(rig, *, session="A", serial="1", **kwargs):
    selected = kwargs.pop("operation_id", None)
    current = rig.service.sessions.get(session)
    if selected is None:
        selected = (current.operation_id if current and (kwargs.get("expected_context") or kwargs.get("kind") == "cancel")
                    else "op:" + serial)
    return FlowRequest(session_id=session, turn_id="turn:" + serial, operation_id=selected,
                       idempotency_key="key:" + serial, lane="why", **kwargs)


def event(rig, *, entity="light.bedroom", offset=-30, cause=None, reason=None, kind="state-transition"):
    stamp = rig.clock.wall + timedelta(seconds=offset)
    item = Event(source="SIMULATED-source", kind=kind, correlation_id="SIMULATED-correlation",
                 causation_id=cause, wall_time=stamp, received_at=stamp + timedelta(seconds=1),
                 time_basis="upstream_event", monotonic_time=rig.clock.mono + offset,
                 boot_id="SIMULATED-event-boot", simulated=True,
                 payload={"entity_id": entity, "from": "off", "to": "on", **({"reason": reason} if reason else {})})
    rig.store.append_events([item])
    return item


def question(rig, **kwargs):
    return turn(rig, text="卧室灯为什么亮了", start=rig.clock.wall - timedelta(seconds=60),
                end=rig.clock.wall, **kwargs)


@pytest.mark.asyncio
async def test_actual_console_clarifies_time_then_consumes_original_query_and_keeps_truth(rig):
    record = event(rig, cause="SIMULATED-recorded-parent")
    context = rig.context()
    first = await rig.service.answer(turn(rig, text="凌晨卧室灯为什么亮了"), context)
    assert first.phase == "clarify" and not rig.calls
    response = await rig.service.answer(turn(rig, serial="2", expected_context=first.context_token,
                                            start=NOW - timedelta(minutes=1), end=NOW), context)
    assert response.phase == "result" and response.input_kind == "console_text"
    assert response.domain["query"] == rig.returned[-1]
    assert response.domain["query"]["simulated"] and not response.domain["query"]["execution_authority"]
    row = response.domain["query"]["items"][0]
    assert row["id"] == record.event_id and row["hardware_sample_time"] is None
    assert row["event_time"] != row["observed_at"] and row["query_observed_at"] != row["event_time"]
    assert row["causation_status"] == "recorded_by_source_not_independently_verified"
    assert response.domain["explanation"]["level"] == "source_recorded"
    assert response.domain["archive_status"]["reason"] == "archive_metadata_permission_required"
    assert response.domain["trace_status"] == {"status": "held", "reason": "scoped_action_trace_unavailable"}
    assert response.asr_admission == response.visual_admission == "unavailable" and response.nas_lease == "closed"
    assert rig.core.actions is None and rig.adapter.managed_actions == [] and not rig.player.leases


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,expected", [("source", "source_recorded"), ("correlation", "temporal_only"),
    ("opaque", "external_or_unknown"), ("gap", "history_gap"), ("empty", "no_matching_records")])
async def test_four_evidence_levels_and_empty_history_are_not_conflated(rig, scenario, expected):
    if scenario != "empty":
        event(rig, cause="SIMULATED-parent" if scenario == "source" else None,
              reason="external_rule_opaque" if scenario == "opaque" else None,
              kind="observation-gap" if scenario == "gap" else "state-transition")
    if scenario == "correlation":
        event(rig, offset=-20)
    response = await rig.service.answer(question(rig), rig.context())
    assert response.domain["explanation"]["level"] == expected
    assert response.domain["explanation"]["complete_household_history"] is False
    assert response.domain["query"]["coverage"] == "bounded_local_journal_not_complete_household_history"
    if scenario == "empty":
        assert "不能据此判断没有发生" in response.caption
    assert not rig.adapter.managed_actions and not rig.archive_calls


@pytest.mark.asyncio
async def test_cross_room_entity_session_evidence_and_self_grants_are_refused(rig):
    bedroom = event(rig)
    event(rig, entity="light.hall")
    first = await rig.service.answer(question(rig), rig.context())
    crossed = await rig.service.answer(turn(rig, session="B", text="玄关灯为什么亮了", evidence_ref=bedroom.event_id,
                                           start=NOW - timedelta(minutes=1), end=NOW), rig.context("B"))
    assert crossed.phase == "refused" and crossed.domain["query"]["items"] == []
    wrong_room = await rig.service.answer(turn(rig, serial="2", text="灯为什么亮了", room_id="hall",
                                              start=NOW - timedelta(minutes=1), end=NOW), rig.context())
    assert wrong_room.code == "outside_read_scope" and not wrong_room.domain["query"]["items"]
    assert set(wrong_room.domain) == {"query", "explanation", "archive", "archive_status", "trace_status"}
    wrong_token = await rig.service.prepare_playback(turn(rig, session="B", serial="3", kind="playback",
                                                          expected_context=first.context_token, evidence_ref="a" * 64),
                                                      rig.context("B"))
    assert wrong_token.phase == "unavailable" and not rig.runner_calls
    raw = question(rig).model_dump()
    with pytest.raises(ValueError):
        FlowRequest.model_validate({**raw, "scope": {"all_home": True}, "trusted": True})


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "cancel", "epoch", "deadline"])
async def test_real_query_await_withholds_late_evidence_and_cursor(rig, change, monkeypatch):
    for offset in range(-50, -25):
        event(rig, offset=offset)
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = rig.core.snapshot

    async def paused_snapshot():
        entered.set()
        await release.wait()
        return await snapshot()

    monkeypatch.setattr(rig.core, "snapshot", paused_snapshot)
    context = rig.context()
    task = asyncio.create_task(rig.service.answer(question(rig), context))
    await entered.wait()
    if change == "revoke":
        rig.scopes["A"] = replace(rig.scopes["A"], permitted=False)
    elif change == "cancel":
        cancelled = await rig.service.cancel(turn(rig, serial="stop", kind="cancel"), context)
        assert cancelled.phase == "cancelled"
    elif change == "epoch":
        rig.live.source = "source-new-epoch"
    else:
        rig.clock.advance(61)
    release.set()
    response = await task
    assert response.phase in {"refused", "cancelled"} and response.domain is None
    assert not rig.query.pages and rig.service.sessions["A"].next_cursor is None
    assert not rig.adapter.managed_actions


@pytest.mark.asyncio
async def test_original_page_cursor_is_consumed_only_by_explicit_same_selection(rig):
    for offset in range(-50, -19):
        event(rig, offset=offset)
    context = rig.context()
    first = await rig.service.answer(question(rig), context)
    page = next(c for c in first.choices if c.label == "读取下一页依据")
    second = await rig.service.answer(turn(rig, serial="2", candidate_id=page.id,
                                           expected_context=first.context_token), context)
    assert len(first.domain["query"]["items"]) == 20 and len(second.domain["query"]["items"]) == 11
    assert first.domain["query"]["snapshot_ref"] == second.domain["query"]["snapshot_ref"]
    assert rig.calls[-1].cursor == first.domain["query"]["next_cursor"]
    assert not second.domain["query"]["next_cursor"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,offset", [("ready", -60), ("pending", 0), ("expired", -180),
                                          ("missing", -300), ("unavailable", -60)])
async def test_original_archive_states_compose_same_window_without_live_qualification(rig, status, offset):
    anchor = rig.index.sim_anchor
    rig.clock.advance(65)
    rig.live.metadata = True
    if status == "unavailable":
        rig.index.simulate(rig.index.context()["fingerprint"], "offline")
    event(rig, offset=(anchor + timedelta(seconds=offset + 30) - rig.clock.wall).total_seconds())
    request = turn(rig, text="卧室灯为什么亮了", start=anchor + timedelta(seconds=offset),
                   end=anchor + timedelta(seconds=offset + 60))
    response = await rig.service.answer(request, rig.context())
    archive = response.domain["archive"]
    assert archive["status"] == status and archive["simulated"] is True
    assert response.domain["query"]["items"] and response.domain["archive_status"]["status"] == status
    assert rig.archive_calls[-1].time_range.start == request.start
    assert not archive["media_copied"] and not archive["device_write_attempted"]
    assert archive["causal_relation"] == "unknown" and response.nas_lease == "metadata_only"
    assert not rig.runner_calls and not rig.player.leases


@pytest.mark.asyncio
async def test_missing_camera_mapping_requires_explicit_choice_and_no_nas_grant_never_resolves(rig):
    event(rig)
    rig.live.mapping = False
    context = rig.context()
    closed = await rig.service.answer(question(rig), context)
    assert closed.domain["query"]["items"] and not rig.archive_calls
    rig.live.metadata = True
    select = await rig.service.answer(question(rig, serial="2"), context)
    assert select.domain["archive"] is None and not rig.archive_calls
    choice = select.domain["archive_status"]["choices"][0]
    selected = await rig.service.answer(turn(rig, serial="3", camera_id=choice["id"],
                                             expected_context=select.context_token), context)
    assert selected.domain["archive"]["status"] == "ready" and len(rig.archive_calls) == 1


@pytest.mark.asyncio
async def test_archive_failure_or_metadata_revoke_keeps_home_explanation(rig, monkeypatch):
    event(rig, cause="SIMULATED-parent")
    rig.live.metadata = True

    async def failed(request):
        raise OSError("SIMULATED-private-upstream-error")

    monkeypatch.setattr(rig.index, "resolve", failed)
    response = await rig.service.answer(question(rig), rig.context())
    assert response.phase == "result" and response.domain["query"]["items"]
    assert response.domain["archive"] is None and "SIMULATED-private" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_default_reader_guard_holds_explicit_click_without_player_prepare(rig):
    event(rig)
    rig.live.metadata = rig.live.content = True
    context = rig.context()
    answer = await rig.service.answer(question(rig), context)
    ref = answer.domain["archive"]["segments"][0]["media_ref"]
    response = await rig.service.prepare_playback(turn(rig, serial="2", kind="playback", evidence_ref=ref,
                                                      expected_context=answer.context_token), context)
    assert response.code == "archive_content_reader_guard_unavailable" and response.domain is None
    assert not rig.runner_calls and not rig.player.leases


@pytest.mark.asyncio
async def test_explicit_click_uses_original_player_clipped_window_and_cancel_revoke(rig):
    event(rig)
    rig.live.metadata = rig.live.content = True
    guard = SyntheticReaderGuard(rig)
    rig.service.archive = replace(rig.ports, reader_guard=guard)
    context = rig.context()
    answer = await rig.service.answer(turn(rig, text="卧室灯为什么亮了", start=NOW - timedelta(seconds=55),
                                           end=NOW - timedelta(seconds=5)), context)
    assert not rig.runner_calls
    ref = answer.domain["archive"]["segments"][0]["media_ref"]
    forged = await rig.service.prepare_playback(turn(rig, serial="2", kind="playback", evidence_ref="a" * 64,
                                                    expected_context=answer.context_token), context)
    assert forged.code == "archive_reference_unavailable" and not rig.runner_calls
    playback = await rig.service.prepare_playback(turn(rig, serial="3", kind="playback", evidence_ref=ref,
                                                       expected_context=answer.context_token), context)
    plan = playback.domain["playback"]
    assert plan == rig.player.leases[plan["plan_id"]].plan.model_dump(mode="json")
    assert plan["stream_url"] == "/api/archive/media/" + plan["plan_id"]
    assert plan["start_offset_seconds"] == 5 and plan["end_offset_seconds"] == 55
    assert plan["simulated"] and not plan["decoder_verified_by_server"] and not plan["hardware_clock_verified"]
    assert playback.nas_lease == "metadata_only" and playback.expires_at < rig.player.leases[plan["plan_id"]].plan.expires_at
    cancelled = await rig.service.cancel(turn(rig, serial="stop", kind="cancel"), context)
    assert cancelled.phase == "cancelled" and not rig.player.leases and not guard.bindings


@pytest.mark.asyncio
async def test_content_expiration_and_late_revocation_revoke_original_lease(rig, monkeypatch):
    event(rig)
    rig.live.metadata = rig.live.content = True
    guard = SyntheticReaderGuard(rig)
    rig.service.archive = replace(rig.ports, reader_guard=guard)
    context = rig.context()
    answer = await rig.service.answer(question(rig), context)
    ref = answer.domain["archive"]["segments"][0]["media_ref"]
    prepared = await rig.service.prepare_playback(turn(rig, serial="2", kind="playback", evidence_ref=ref,
                                                       expected_context=answer.context_token), context)
    rig.clock.advance(6)
    with pytest.raises(ValueError):
        guard.check(prepared.domain["playback"]["plan_id"])
    assert not rig.player.leases
    prepare = rig.player.prepare

    async def revoke_after_original(request):
        plan = await prepare(request)
        rig.live.content = False
        return plan

    monkeypatch.setattr(rig.player, "prepare", revoke_after_original)
    result = await rig.service.prepare_playback(turn(rig, serial="3", kind="playback", evidence_ref=ref,
                                                    expected_context=answer.context_token), context)
    assert result.domain is None and not rig.player.leases and not guard.bindings


@pytest.mark.asyncio
async def test_long_home_window_does_not_expand_archive_or_restore_expired_session(rig):
    event(rig)
    rig.live.metadata = True
    context = rig.context()
    response = await rig.service.answer(turn(rig, text="卧室灯为什么亮了", start=NOW - timedelta(hours=3), end=NOW), context)
    assert response.domain["query"]["items"] and response.domain["archive"] is None
    assert response.domain["archive_status"]["reason"] == "archive_narrower_window_required" and not rig.archive_calls
    rig.clock.advance(61)
    expired = await rig.service.answer(turn(rig, serial="2", expected_context=response.context_token,
                                            start=NOW - timedelta(minutes=1), end=NOW), rig.context())
    assert expired.phase == "refused" and expired.code == "flow_expired" and expired.domain is None


@pytest.mark.asyncio
async def test_missing_timezone_and_explicit_room_device_choices_use_scoped_original_devices(rig):
    missing = await rig.service.answer(question(rig), replace(rig.context(), household_timezone=None))
    assert missing.code == "household_timezone_required" and not rig.calls
    rig.scopes["B"] = replace(rig.scopes["B"], rooms=frozenset({"hall", "bedroom"}))
    lamp = next(e for e in rig.adapter.entities if e.id == "light.bedroom")
    rig.adapter.entities.append(lamp.model_copy(update={"id": "light.bedside", "name": "床头灯"}))
    context = rig.context("B")
    rooms = await rig.service.answer(turn(rig, session="B", text="灯为什么亮了",
                                         start=NOW - timedelta(minutes=1), end=NOW), context)
    assert rooms.code == "why_room_selection_required" and not rig.calls
    room = next(c for c in rooms.choices if c.label == "卧室")
    devices = await rig.service.answer(turn(rig, session="B", serial="2", candidate_id=room.id,
                                           expected_context=rooms.context_token), context)
    assert devices.code == "why_device_selection_required" and devices.context_token != rooms.context_token
    device = next(c for c in devices.choices if c.label == "床头灯")
    event(rig, entity="light.bedside")
    answer = await rig.service.answer(turn(rig, session="B", serial="3", candidate_id=device.id,
                                          expected_context=devices.context_token), context)
    assert answer.domain["query"]["permission_scope"]["requested_entities"] == ["light.bedside"]
    assert answer.domain["query"]["items"][0]["entity_refs"] == ["light.bedside"]
    assert len(rig.calls) == 3 and not rig.adapter.managed_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["turn_id", "idempotency_key"])
async def test_reuse_of_each_request_identity_cannot_reissue_domain_reads(rig, field):
    request = question(rig)
    await rig.service.answer(request, rig.context())
    retry = question(rig, serial="2").model_copy(update={field: getattr(request, field)})
    result = await rig.service.answer(retry, rig.context())
    assert result.code == "flow_duplicate_turn" and len(rig.calls) == 2


@pytest.mark.asyncio
async def test_stale_question_token_cannot_prepare_same_segment_in_a_new_window(rig):
    rig.live.metadata = rig.live.content = True
    rig.service.archive = replace(rig.ports, reader_guard=SyntheticReaderGuard(rig))
    context = rig.context()
    old = await rig.service.answer(question(rig), context)
    new = await rig.service.answer(turn(rig, serial="2", text="卧室灯为什么亮了",
                                       start=NOW - timedelta(seconds=40), end=NOW - timedelta(seconds=5)), context)
    assert old.context_token != new.context_token
    ref = old.domain["archive"]["segments"][0]["media_ref"]
    assert ref == new.domain["archive"]["segments"][0]["media_ref"]
    result = await rig.service.prepare_playback(turn(rig, serial="3", kind="playback", evidence_ref=ref,
                                                    expected_context=old.context_token), context)
    assert result.code == "flow_reference_context_changed" and not rig.runner_calls and not rig.player.leases


@pytest.mark.asyncio
async def test_scope_loss_at_entry_revokes_an_already_prepared_original_lease(rig):
    rig.live.metadata = rig.live.content = True
    guard = SyntheticReaderGuard(rig)
    rig.service.archive = replace(rig.ports, reader_guard=guard)
    context = rig.context()
    answer = await rig.service.answer(question(rig), context)
    ref = answer.domain["archive"]["segments"][0]["media_ref"]
    result = await rig.service.prepare_playback(turn(rig, serial="2", kind="playback", evidence_ref=ref,
                                                    expected_context=answer.context_token), context)
    assert result.domain["playback"] and rig.player.leases
    rig.scopes["A"] = replace(rig.scopes["A"], permitted=False)
    denied = await rig.service.answer(question(rig, serial="3"), context)
    assert denied.domain is None and not rig.player.leases and not guard.bindings
    assert rig.service.sessions["A"].invalidated and not rig.service.sessions["A"].segments


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["metadata", "scope", "epoch", "deadline"])
async def test_original_archive_await_keeps_home_only_for_metadata_failure(rig, change, monkeypatch):
    for offset in range(-50, -25):
        event(rig, offset=offset)
    rig.live.metadata = True
    entered, release = asyncio.Event(), asyncio.Event()
    resolve = rig.index.resolve

    async def delayed(request):
        entered.set()
        await release.wait()
        return await resolve(request)

    monkeypatch.setattr(rig.index, "resolve", delayed)
    context = rig.context()
    task = asyncio.create_task(rig.service.answer(question(rig), context))
    await entered.wait()
    if change == "metadata":
        rig.live.metadata = False
    elif change == "scope":
        rig.scopes["A"] = replace(rig.scopes["A"], history=False)
    elif change == "epoch":
        rig.live.source = "SIMULATED-next-source"
    else:
        rig.clock.advance(4)
    release.set()
    response = await task
    if change in {"metadata", "deadline"}:
        assert response.phase == "result" and response.domain["query"]["items"]
        assert response.domain["archive"] is None and not rig.player.leases
        assert response.domain["archive_status"]["reason"] == (
            "archive_metadata_permission_revoked" if change == "metadata" else "archive_metadata_deadline")
    else:
        assert response.domain is None and rig.service.sessions["A"].next_cursor is None
        assert rig.service.sessions["A"].invalidated


@pytest.mark.asyncio
async def test_cancel_waits_for_a_late_original_player_plan_and_revokes_it(rig, monkeypatch):
    rig.live.metadata = rig.live.content = True
    guard = SyntheticReaderGuard(rig)
    rig.service.archive = replace(rig.ports, reader_guard=guard)
    context = rig.context()
    answer = await rig.service.answer(question(rig), context)
    ref = answer.domain["archive"]["segments"][0]["media_ref"]
    entered = asyncio.Event()
    prepare = rig.player.prepare

    async def late(request):
        plan = await prepare(request)
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return plan

    monkeypatch.setattr(rig.player, "prepare", late)
    task = asyncio.create_task(rig.service.prepare_playback(turn(rig, serial="2", kind="playback",
                                evidence_ref=ref, expected_context=answer.context_token), context))
    await entered.wait()
    assert rig.player.leases
    cancelled = await rig.service.cancel(turn(rig, serial="stop", kind="cancel"), context)
    result = await task
    assert cancelled.phase == result.phase == "cancelled"
    assert result.domain is None and not rig.player.leases and not guard.bindings


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("subject_ref", "SIMULATED-person-B"), ("track_epoch", "epoch:2"),
                                        ("map_revision", "map:2"), ("generation", 1)])
async def test_private_person_track_map_generation_changes_cannot_inherit_why(rig, field, value):
    context = replace(rig.context(), input_kind="simulated_fixture", subject_ref="SIMULATED-person-A",
                      track_ref="SIMULATED-track", track_epoch="epoch:1", map_revision="map:1")
    answer = await rig.service.answer(question(rig), context)
    changed = replace(context, **{field: value})
    result = await rig.service.answer(turn(rig, serial="2", expected_context=answer.context_token), changed)
    assert result.code == "flow_context_changed" and result.domain is None
    assert len(rig.calls) == 2 and rig.service.sessions["A"].invalidated


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["打开卧室灯为什么亮了", "开启卧室灯为什么亮了", "启动卧室灯为什么亮了",
    "执行开灯为什么亮了", "卧室与玄关灯为什么亮了", "卧室和玄关灯为什么亮了", "开卧室灯为什么亮了"])
async def test_write_prefix_and_multiple_target_clauses_never_enter_query_even_with_selected_id(rig, text):
    request = turn(rig, text=text, device_id="light.bedroom", start=NOW - timedelta(minutes=1), end=NOW)
    response = await rig.service.answer(request, rig.context())
    assert response.phase == "refused" and not rig.calls and not rig.archive_calls
    assert rig.core.actions is None and not rig.adapter.managed_actions


@pytest.mark.asyncio
async def test_public_selected_device_cannot_silently_override_a_different_named_question(rig):
    lamp = next(e for e in rig.adapter.entities if e.id == "light.bedroom")
    rig.adapter.entities.append(lamp.model_copy(update={"id": "light.bedside", "name": "床头灯"}))
    response = await rig.service.answer(turn(rig, text="卧室床头灯为什么亮了", device_id="light.bedroom",
                                           start=NOW - timedelta(minutes=1), end=NOW), rig.context())
    assert response.code == "why_device_selection_required" and response.domain is None
    assert len(rig.calls) == 1 and rig.calls[0].kind == "devices"


@pytest.mark.asyncio
async def test_actual_f2_shaped_same_operation_time_camera_and_original_prepare(rig):
    event(rig, cause="SIMULATED-parent")
    rig.live.metadata = rig.live.content = True
    rig.live.mapping = False
    guard = SyntheticReaderGuard(rig)
    rig.service.archive = replace(rig.ports, reader_guard=guard)
    context = rig.context()
    first = await rig.service.answer(turn(rig, text="凌晨卧室灯为什么亮了"), context)
    timed = await rig.service.answer(turn(rig, serial="2", operation_id=first.operation_id,
                                         expected_context=first.context_token,
                                         start=NOW - timedelta(minutes=1), end=NOW), context)
    choice = timed.domain["archive_status"]["choices"][0]
    selected = await rig.service.answer(turn(rig, serial="3", operation_id=first.operation_id,
                                            expected_context=timed.context_token, camera_id=choice["id"]), context)
    ref = selected.domain["archive"]["segments"][0]["media_ref"]
    clicked = await rig.service.prepare_playback(turn(rig, serial="4", kind="playback",
        operation_id=first.operation_id, expected_context=selected.context_token, evidence_ref=ref), context)
    assert first.phase == "clarify" and timed.domain["query"] == rig.returned[1]
    assert first.operation_id == timed.operation_id == selected.operation_id == clicked.operation_id
    assert len({first.context_token, timed.context_token, selected.context_token}) == 3
    assert clicked.domain["playback"]["media_ref"] == ref and clicked.nas_lease == "metadata_only"
    await rig.service.cancel(turn(rig, serial="stop", kind="cancel", operation_id=first.operation_id), context)
    assert not rig.player.leases and not guard.bindings


@pytest.mark.asyncio
async def test_new_question_and_stale_old_cancel_preserve_new_operation_and_fixed_session_ttl(rig):
    event(rig)
    rig.live.metadata = rig.live.content = True
    guard = SyntheticReaderGuard(rig)
    rig.service.archive = replace(rig.ports, reader_guard=guard)
    context = rig.context()
    old = await rig.service.answer(question(rig), context)
    await rig.service.cancel(turn(rig, serial="stop-old", kind="cancel", operation_id=old.operation_id), context)
    rig.clock.advance(10)
    new = await rig.service.answer(question(rig, serial="2"), rig.context())
    assert new.phase == "result" and new.expires_at == old.expires_at
    ref = new.domain["archive"]["segments"][0]["media_ref"]
    prepared = await rig.service.prepare_playback(turn(rig, serial="3", kind="playback",
        operation_id=new.operation_id, expected_context=new.context_token, evidence_ref=ref), rig.context())
    plan_id = prepared.domain["playback"]["plan_id"]
    late = await rig.service.cancel(turn(rig, serial="late-old", kind="cancel", operation_id=old.operation_id), context)
    assert late.phase == "cancelled" and plan_id in rig.player.leases and plan_id in guard.bindings
    resurrected = await rig.service.answer(question(rig, serial="4", operation_id=old.operation_id), context)
    assert resurrected.code == "flow_operation_finished" and plan_id in rig.player.leases
    rig.service.invalidate("A")
    assert not rig.player.leases and not guard.bindings
    blocked = await rig.service.answer(question(rig, serial="5"), rig.context())
    assert blocked.phase == "cancelled" and blocked.domain is None


@pytest.mark.asyncio
async def test_same_operation_requires_current_token_and_cannot_borrow_another_operation_reference(rig):
    old = await rig.service.answer(question(rig), rig.context())
    missing = await rig.service.answer(question(rig, serial="2", operation_id=old.operation_id), rig.context())
    assert missing.code == "exact_why_reference_required"
    crossed = await rig.service.answer(turn(rig, serial="3", operation_id="op:other",
        expected_context=old.context_token, candidate_id="SIMULATED-candidate"), rig.context())
    assert crossed.code == "flow_operation_reference_mismatch" and len(rig.calls) == 2


@pytest.mark.asyncio
async def test_late_retired_original_player_plan_cleans_only_its_old_operation(rig, monkeypatch):
    rig.live.metadata = rig.live.content = True
    guard = SyntheticReaderGuard(rig)
    rig.service.archive = replace(rig.ports, reader_guard=guard)
    context = rig.context()
    old = await rig.service.answer(question(rig), context)
    old_ref = old.domain["archive"]["segments"][0]["media_ref"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = rig.player.prepare
    old_plans = []

    async def delayed(request):
        plan = await original(request)
        old_plans.append(plan.plan_id)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return plan

    monkeypatch.setattr(rig.player, "prepare", delayed)
    old_task = asyncio.create_task(rig.service.prepare_playback(turn(rig, serial="2", kind="playback",
        operation_id=old.operation_id, expected_context=old.context_token, evidence_ref=old_ref), context))
    await entered.wait()
    newer = await rig.service.answer(question(rig, serial="3"), context)
    monkeypatch.setattr(rig.player, "prepare", original)
    new_ref = newer.domain["archive"]["segments"][0]["media_ref"]
    new_plan = await rig.service.prepare_playback(turn(rig, serial="4", kind="playback",
        operation_id=newer.operation_id, expected_context=newer.context_token, evidence_ref=new_ref), context)
    new_id = new_plan.domain["playback"]["plan_id"]
    release.set()
    late = await old_task
    assert late.phase == "cancelled" and late.domain is None
    assert all(plan_id not in rig.player.leases for plan_id in old_plans)
    assert new_id in rig.player.leases and new_id in guard.bindings and not rig.service.retired


@pytest.mark.asyncio
async def test_close_waits_for_retired_plan_after_current_session_is_pruned(rig, monkeypatch):
    event(rig)
    rig.live.metadata = rig.live.content = True
    guard = SyntheticReaderGuard(rig)
    rig.service.archive = replace(rig.ports, reader_guard=guard)
    context = rig.context()
    old = await rig.service.answer(question(rig), context)
    old_ref = old.domain["archive"]["segments"][0]["media_ref"]
    original = rig.player.prepare
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(request):
        plan = await original(request)
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return plan

    monkeypatch.setattr(rig.player, "prepare", delayed)
    old_task = asyncio.create_task(rig.service.prepare_playback(turn(rig, serial="2", kind="playback",
        operation_id=old.operation_id, expected_context=old.context_token, evidence_ref=old_ref), context))
    close_task = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        newer = await rig.service.answer(question(rig, serial="3"), context)
        assert newer.phase == "result" and newer.operation_id != old.operation_id
        rig.clock.advance(61)
        request_b = turn(rig, session="B", serial="new-B", text="玄关灯为什么亮了",
                         start=rig.clock.wall - timedelta(seconds=60), end=rig.clock.wall)
        response_b = await rig.service.answer(request_b, rig.context("B"))
        assert response_b.phase == "result"
        assert "A" not in rig.service.sessions and "A" in rig.service.retired
        assert not old_task.done() and len(rig.player.leases) == 1
        close_task = asyncio.create_task(rig.service.close())
        for _ in range(3):
            await asyncio.sleep(0)
        close_returned_early, old_finished_early = close_task.done(), old_task.done()
    finally:
        release.set()
        await asyncio.gather(*([old_task, close_task] if close_task else [old_task]))
    assert not close_returned_early and not old_finished_early
    assert old_task.result().phase == "cancelled" and old_task.result().domain is None
    assert not rig.player.leases and not guard.bindings
    assert not rig.service.sessions and not rig.service.retired
