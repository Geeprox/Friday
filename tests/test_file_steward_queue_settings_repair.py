"""Concurrent/restarted owners over fixture bytes and temporary SQLite only."""

import asyncio
import copy
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from test_file_steward_extraction import slice_env as slice_env
from test_file_steward_settings import arl_snapshot

from homeplane.family_media import FamilyMediaCatalog
from homeplane.file_steward import FileSteward, StewardError
from homeplane.file_steward.contracts import StewardBudget
from homeplane.file_steward.extraction_queue import StewardQueueControl
from homeplane.file_steward.policy import StewardPolicyEdit
from homeplane.file_steward.settings_contracts import StewardSettingsEdit, StewardSettingsRequest


def second_owner(env):
    connection = sqlite3.connect(env.path)
    return connection, FileSteward(
        connection, env.catalog, clock=lambda: env.now[0], fixture_provider=env.adapter
    )


async def settings(env, owner=None):
    owner = owner or env.steward
    request = StewardSettingsRequest(expected_context=await env.context(), sample_item_id=env.item.item_id)
    return request, await owner.settings.query(request)


async def save(env, owner=None, **changes):
    owner = owner or env.steward
    request, view = await settings(env, owner)
    values = dict(
        **request.model_dump(),
        expected_fingerprint=view.fingerprint,
        watched=view.settings.watched,
        extraction_mode=view.settings.extraction_mode,
        budget=view.settings.budget,
        rule=view.rule,
    )
    values.update(changes)
    return await owner.settings.save(StewardSettingsEdit(**values))


async def control(env, owner, decision):
    entry = owner.queue.records()[0]
    _, view = await settings(env, owner)
    return await owner.queue.control(
        StewardQueueControl(
            expected_context=await env.context(),
            job_id=entry.job_id,
            expected_revision=entry.revision,
            expected_settings_fingerprint=view.fingerprint,
            decision=decision,
        )
    )


async def start_waiting(env):
    env.adapter.bridge.wait = asyncio.Event()
    prior_calls = len(env.adapter.bridge.calls)
    await env.steward.queue.activate_fixture(env.job.job_id, await env.context())
    task = asyncio.create_task(env.steward.queue.step_fixture(await env.context()))
    async with asyncio.timeout(1):
        while len(env.adapter.bridge.calls) == prior_calls:
            await asyncio.sleep(0)
    return task


def progress(owner):
    job = owner.jobs()[0]
    return (job.completed_pages, job.completed_units, job.cursor, job.source_pages)


@pytest.mark.parametrize("committed_pages", [0, 1])
@pytest.mark.parametrize("change", ["budget", "intent", "policy", "policy_aba"])
async def test_running_change_survives_restart_without_new_progress(slice_env, committed_pages, change):
    env = slice_env
    await save(env, watched=True, extraction_mode="pdf_text_first")
    await env.plan()
    if committed_pages:
        await env.tick()
    baseline = progress(env.steward)
    task = await start_waiting(env)
    connection, other = second_owner(env)
    try:
        if change == "budget":
            await save(env, other, budget=StewardBudget(source_bytes=1, text_chars=1, units=1, seconds=0.1))
        elif change == "intent":
            await save(env, other, watched=False, extraction_mode="metadata_only")
        else:
            _, view = await settings(env, other)
            await other.set_policy(
                StewardPolicyEdit(
                    expected_context=await env.context(),
                    rule=view.rule.model_copy(update={"dedup_review": not view.rule.dedup_review}),
                )
            )
            if change == "policy_aba":
                await other.set_policy(
                    StewardPolicyEdit(expected_context=await env.context(), rule=view.rule)
                )
                assert (await settings(env, other))[1].fingerprint == view.fingerprint
        env.adapter.bridge.wait.set()
        result = await task
        assert result.status == "held"
        assert result.reason == ("policy_changed" if change == "policy_aba" else "settings_changed")
        assert progress(env.steward) == baseline
        assert env.steward.jobs()[0].charged_source_bytes == (committed_pages + 1) * 1048576
        assert len(env.steward.page_outcomes(env.job.job_id)) == committed_pages
        assert len(env.steward.units(env.job.job_id)) == committed_pages
    finally:
        env.adapter.bridge.wait.set()
        if not task.done():
            await task
        connection.close()
    await env.reopen()
    assert progress(env.steward) == baseline
    assert env.steward.queue.records()[0].status == "held"
    assert await env.steward.queue.step_fixture(await env.context()) is None
    assert not env.reader.calls


@pytest.mark.parametrize("committed_pages", [0, 1])
@pytest.mark.parametrize("decision", ["cancel", "pause"])
@pytest.mark.parametrize("revoked", [False, True])
async def test_cancel_pause_after_tightening_preserves_operator_state_on_restart(
    slice_env, committed_pages, decision, revoked
):
    env = slice_env
    await env.plan()
    if committed_pages:
        await env.tick()
    baseline = progress(env.steward)
    task = await start_waiting(env)
    connection, other = second_owner(env)
    try:
        await save(env, other, budget=StewardBudget(source_bytes=1, text_chars=1))
        if revoked:
            env.adapter.source.allowed = False
        stopped = await control(env, other, decision)
        assert stopped.reason == ("operator_cancelled" if decision == "cancel" else "operator_paused")
        env.adapter.bridge.wait.set()
        result = await task
        assert result == stopped
        job = env.steward.jobs()[0]
        assert job.status == "paused" and job.reason == stopped.reason
        assert progress(env.steward) == baseline
        assert job.charged_source_bytes == (committed_pages + 1) * 1048576
    finally:
        env.adapter.bridge.wait.set()
        if not task.done():
            await task
        connection.close()
    await env.reopen()
    assert progress(env.steward) == baseline
    assert env.steward.queue.records()[0].reason == stopped.reason
    assert env.steward.jobs()[0].reason == stopped.reason
    assert (await control(env, env.steward, "resume_preview")).status == "held"
    assert await env.steward.queue.step_fixture(await env.context()) is None
    assert not env.reader.calls


@pytest.mark.parametrize("recover_expired_claim", [False, True])
async def test_old_result_cannot_replace_fresh_claim_after_cancel_or_recovery(
    slice_env, recover_expired_claim
):
    env = slice_env
    await env.plan()
    result_ready, release_old = asyncio.Event(), asyncio.Event()
    original = env.adapter.step

    async def delayed_old(job, budget):
        result = await original(job, budget)
        if job.attempts == 1:
            result_ready.set()
            await release_old.wait()
        return result

    env.adapter.step = delayed_old
    await env.steward.queue.activate_fixture(env.job.job_id, await env.context())
    old_task = asyncio.create_task(env.steward.queue.step_fixture(await env.context()))
    await asyncio.wait_for(result_ready.wait(), 1)
    if recover_expired_claim:
        env.now[0] += timedelta(seconds=7)
    connection, other = second_owner(env)
    try:
        if recover_expired_claim:
            assert other.queue.records()[0].status == "paused"
        else:
            await save(env, other, budget=StewardBudget(source_bytes=1024, text_chars=128))
            assert (await control(env, other, "cancel")).status == "cancelled"
            assert (await control(env, other, "resume_preview")).status == "held"
        await other.queue.activate_fixture(env.job.job_id, await env.context())
        assert (await other.queue.step_fixture(await env.context())).status == "paused"
        committed = other.jobs()[0].model_copy(deep=True)
        entry = other.queue.records()[0]
        assert committed.completed_pages == 1 and committed.cursor == "pdf-page-2"
        assert committed.attempts == 2
        assert committed.charged_source_bytes == 1048576 + (1048576 if recover_expired_claim else 1024)
        release_old.set()
        assert (await old_task) == entry
        assert other.jobs()[0] == committed
        assert len(other.units(committed.job_id)) == len(other.page_outcomes(committed.job_id)) == 1
    finally:
        release_old.set()
        if not old_task.done():
            await old_task
        connection.close()
    await env.reopen()
    assert env.steward.jobs()[0] == committed
    assert (await env.tick()).status == "needs_ocr"
    assert env.steward.jobs()[0].completed_pages == 2
    assert not env.reader.calls


class BeforeExecute:
    def __init__(self, connection, hook):
        self.connection, self.hook = connection, hook

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, *args):
        self.hook(statement)
        return self.connection.execute(statement, *args)


def complete_inline(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    coroutine.close()
    raise AssertionError("Synthetic metadata coroutine unexpectedly suspended")


async def test_configuration_saved_immediately_before_commit_lock_is_rechecked(slice_env):
    env = slice_env
    env.adapter.bridge.pages = ["Synthetic final page."]
    await env.plan()
    connection, other = second_owner(env)
    request, view = await settings(env, other)
    begins, saved = [], []

    def inject(statement):
        if statement == "BEGIN IMMEDIATE":
            begins.append(True)
            if env.adapter.bridge.calls and not saved:
                saved.append(
                    complete_inline(
                        other.settings.save(
                            StewardSettingsEdit(
                                **request.model_dump(),
                                expected_fingerprint=view.fingerprint,
                                watched=False,
                                extraction_mode="metadata_only",
                                budget=StewardBudget(source_bytes=1, text_chars=1),
                                rule=view.rule,
                            )
                        )
                    )
                )

    env.steward.connection = BeforeExecute(env.connection, inject)
    try:
        result = await env.tick()
        assert saved and result.status == "held" and result.reason == "settings_changed"
        assert progress(env.steward) == (0, 0, None, None)
        assert env.steward.jobs()[0].charged_source_bytes == 1048576
        assert not env.steward.units(env.job.job_id) and not env.steward.page_outcomes(env.job.job_id)
    finally:
        env.steward.connection = env.connection
        connection.close()


@pytest.mark.parametrize("committed_pages", [0, 1])
async def test_cancel_at_prewrite_preserves_final_page_progress_and_reason(slice_env, committed_pages):
    env = slice_env
    env.adapter.bridge.pages = (
        ["Synthetic first.", "Synthetic last."] if committed_pages else ["Synthetic only."]
    )
    await env.plan()
    if committed_pages:
        await env.tick()
    baseline = progress(env.steward)
    connection, other = second_owner(env)
    entry = other.queue.records()[0]
    expected = await env.context()
    injected = []

    def inject(statement):
        if statement == "SELECT COUNT(*) FROM steward_units" and not injected:
            claimed = other.queue.records()[0]
            assert claimed.revision > entry.revision and claimed.status == "claimed"
            injected.append(
                complete_inline(
                    other.queue.control(
                        StewardQueueControl(
                            expected_context=expected,
                            job_id=claimed.job_id,
                            expected_revision=claimed.revision,
                            expected_settings_fingerprint=claimed.settings_fingerprint,
                            decision="cancel",
                        )
                    )
                )
            )

    env.steward.connection = BeforeExecute(env.connection, inject)
    try:
        result = await env.tick()
        assert injected and result.status == "cancelled" and result.reason == "operator_cancelled"
        assert env.steward.jobs()[0].status == "paused"
        assert env.steward.jobs()[0].reason == "operator_cancelled"
        assert progress(env.steward) == baseline
        assert env.steward.jobs()[0].charged_source_bytes == (committed_pages + 1) * 1048576
    finally:
        env.steward.connection = env.connection
        connection.close()


async def test_cancel_queue_and_job_rollback_together_when_job_save_fails(slice_env):
    env = slice_env
    env.adapter.bridge.pages = ["Synthetic only page."]
    await env.plan()
    task = await start_waiting(env)
    connection, other = second_owner(env)
    entry, job = other.queue.records()[0], other.jobs()[0]
    original = other._save_job

    def fail_save(value):
        raise StewardError("synthetic_storage_failure")

    other._save_job = fail_save
    try:
        with pytest.raises(StewardError, match="synthetic_storage_failure"):
            await control(env, other, "cancel")
        assert other.queue.records()[0] == entry and other.jobs()[0] == job
        other._save_job = original
        env.adapter.bridge.wait.set()
        assert (await task).status == "done"
        assert len(other.units(job.job_id)) == len(other.page_outcomes(job.job_id)) == 1
    finally:
        other._save_job = original
        env.adapter.bridge.wait.set()
        if not task.done():
            await task
        connection.close()


@pytest.mark.parametrize("revocation", ["source", "session"])
async def test_revocation_remains_primary_when_budget_also_tightens(slice_env, revocation):
    env = slice_env
    await env.plan()
    task = await start_waiting(env)
    connection, other = second_owner(env)
    try:
        await save(env, other, budget=StewardBudget(source_bytes=1, text_chars=1))
        if revocation == "source":
            env.adapter.source.allowed = False
            reason = "steward_pdf_source_grant_denied"
        else:
            env.steward.session_status = lambda: {"state": "paused", "failure": "nas_auth_failed"}
            reason = "authentication_required"
        env.adapter.bridge.wait.set()
        result = await task
        assert result.reason == reason and env.steward.jobs()[0].reason == reason
        assert progress(env.steward) == (0, 0, None, None)
        assert env.steward.jobs()[0].charged_source_bytes == 1048576
    finally:
        env.adapter.bridge.wait.set()
        if not task.done():
            await task
        connection.close()
    await env.reopen()
    assert env.steward.queue.records()[0].reason == reason and env.steward.jobs()[0].reason == reason
    assert not env.reader.calls


async def test_commit_holding_writer_lock_precedes_concurrent_cancel(slice_env):
    env = slice_env
    env.adapter.bridge.pages = ["Synthetic only page."]
    await env.plan()
    ready, commit_locked, cancel_attempt = threading.Event(), threading.Event(), threading.Event()

    def cancel_worker():
        connection = sqlite3.connect(env.path, timeout=1)
        catalog = FamilyMediaCatalog(
            connection,
            env.reader,
            simulated=True,
            clock=lambda: env.now[0],
            indexed_kinds=("image", "video", "audio", "other"),
        )
        owner = FileSteward(connection, catalog, clock=lambda: env.now[0], fixture_provider=env.adapter)

        def note_cancel_begin(statement):
            if statement == "BEGIN IMMEDIATE":
                cancel_attempt.set()

        owner.connection = BeforeExecute(connection, note_cancel_begin)

        async def cancel_when_locked():
            ready.set()
            assert commit_locked.wait(1)
            entry = owner.queue.records()[0]
            request = StewardQueueControl(
                expected_context=(await catalog.context()).fingerprint,
                job_id=entry.job_id,
                expected_revision=entry.revision,
                expected_settings_fingerprint=entry.settings_fingerprint,
                decision="cancel",
            )
            try:
                await owner.queue.control(request)
            except StewardError as error:
                return error.code
            return "unexpected_cancel_success"

        try:
            return asyncio.run(cancel_when_locked())
        finally:
            connection.close()

    class CommitLockConnection(BeforeExecute):
        def execute(self, statement, *args):
            result = self.connection.execute(statement, *args)
            if statement == "BEGIN IMMEDIATE" and env.adapter.bridge.calls and not commit_locked.is_set():
                assert self.connection.in_transaction
                commit_locked.set()
                assert cancel_attempt.wait(1)
            return result

    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(cancel_worker)
        assert ready.wait(1)
        env.steward.connection = CommitLockConnection(env.connection, lambda _: None)
        try:
            result = await env.tick()
            assert result.status == "done"
            assert worker.result(timeout=2) == "steward_queue_terminal"
            assert cancel_attempt.is_set() and env.steward.jobs()[0].status == "done"
            assert env.steward.jobs()[0].charged_source_bytes == 1048576
            assert (
                len(env.steward.units(env.job.job_id)) == len(env.steward.page_outcomes(env.job.job_id)) == 1
            )
        finally:
            commit_locked.set()
            env.steward.connection = env.connection


@pytest.mark.parametrize("saved_settings", [False, True])
@pytest.mark.parametrize(
    "changes", [{"item_id": "a" * 64}, {"directory_ref": "fixture-other"}, {"status": "proposed"}]
)
async def test_cas_rebind_keeps_entire_policy_and_settings_atomic(slice_env, saved_settings, changes):
    env = slice_env
    if saved_settings:
        await save(env, watched=True, extraction_mode="pdf_text_first")
    request, view = await settings(env)
    original_settings = env.connection.execute("SELECT id,body FROM steward_settings").fetchall()
    connection, other = second_owner(env)
    original = env.steward.settings.query
    injected = []
    rebound = view.rule.model_copy(update={**changes, "label": "Synthetic rebound"})

    async def query_then_rebind(request):
        value = await original(request)
        if not injected:
            await other.set_policy(StewardPolicyEdit(expected_context=request.expected_context, rule=rebound))
            injected.append(True)
        return value

    env.steward.settings.query = query_then_rebind
    try:
        with pytest.raises(StewardError, match="policy_rebound"):
            await env.steward.settings.save(
                StewardSettingsEdit(
                    **request.model_dump(),
                    expected_fingerprint=view.fingerprint,
                    watched=False,
                    extraction_mode="metadata_only",
                    budget=StewardBudget(source_bytes=1),
                    rule=view.rule,
                )
            )
        assert env.connection.execute("SELECT id,body FROM steward_settings").fetchall() == original_settings
        assert next(rule for rule in other.policies() if rule.rule_id == rebound.rule_id) == rebound
    finally:
        env.steward.settings.query = original
        connection.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_ref", "foreign-fixture"),
        ("source_ref", None),
        ("config_generation", 99),
        ("config_generation", True),
        ("config_generation", "1"),
        ("credential_ref", ""),
        ("credential_generation", "A" * 32),
        ("config_fingerprint", "bad"),
        ("root_descriptor_digest", "bad"),
        ("unexpected", True),
    ],
)
async def test_arl_scope_structure_and_current_admission_pin(slice_env, field, value):
    env = slice_env
    snapshot = arl_snapshot(env)
    snapshot["scope"][field] = value
    env.steward.session_status = lambda: snapshot
    session = (await env.steward.context())["session"]
    assert session["connected"] is not True and session["readable"] is not True
    assert not session["content_grant_provided"] and session["writable"] == "unsupported"
    assert not env.reader.calls


@pytest.mark.parametrize("state", ["revoked", "cancelled"])
@pytest.mark.parametrize("scope", [None, {}, "old_generation"])
async def test_arl_revocation_has_priority_over_scope_uncertainty(slice_env, state, scope):
    env = slice_env
    snapshot = arl_snapshot(env)
    snapshot["credential"]["state"] = state
    if scope == "old_generation":
        snapshot["scope"]["config_generation"] += 1
    else:
        snapshot["scope"] = copy.deepcopy(scope)
    env.steward.session_status = lambda: snapshot
    session = (await env.steward.context())["session"]
    assert session["reason"] == "authentication_required" and session["state"] == "paused"
    assert session["connected"] is not True and session["readable"] is not True
    assert not session["content_grant_provided"]


async def test_arl_stop_can_advance_manager_generation_without_reclassifying_as_online(slice_env):
    env = slice_env
    snapshot = arl_snapshot(
        env,
        state="stopped",
        config_generation=env.reader.generation + 1,
        connection_epoch=None,
        reachable=None,
        authenticated=None,
        readable=None,
    )
    env.steward.session_status = lambda: snapshot
    session = (await env.steward.context())["session"]
    assert snapshot["scope"]["config_generation"] < snapshot["connection"]["config_generation"]
    assert session["state"] == "stopped" and session["reason"] is None and session["label"] == "来源待连接"
    assert session["connected"] is not True and session["readable"] is not True
    assert not session["content_grant_provided"]
