"""Metadata-only settings and ARL public evidence; no credentials/connections/source IO."""

import copy

import httpx
import pytest
from fastapi import FastAPI
from test_file_steward_extraction import slice_env as slice_env

from homeplane.file_steward import StewardError
from homeplane.file_steward.api import create_router
from homeplane.file_steward.policy import StewardPolicyEdit
from homeplane.file_steward.settings_contracts import StewardSettingsEdit, StewardSettingsRequest


async def request(env):
    return StewardSettingsRequest(expected_context=await env.context(), sample_item_id=env.item.item_id)


def edit(request, view, **changes):
    values = dict(
        **request.model_dump(),
        expected_fingerprint=view.fingerprint,
        watched=True,
        extraction_mode="pdf_text_first",
        budget=view.settings.budget,
        rule=view.rule,
    )
    values.update(changes)
    return StewardSettingsEdit(**values)


async def test_directory_save_is_persistent_bound_and_has_four_independent_switches(slice_env):
    env = slice_env
    query = await request(env)
    view = await env.steward.settings.query(query)
    assert not view.saved and view.settings.directory_ref == "fixture-downloads"
    assert (await env.steward.settings.query(query)).fingerprint == view.fingerprint
    rule = view.rule.model_copy(
        update={
            "rename_preview": True,
            "move_preview": False,
            "structure_preview": False,
            "dedup_review": True,
        }
    )
    saved = await env.steward.settings.save(edit(query, view, rule=rule))
    assert (
        saved.saved and saved.activation == "held" and not saved.extraction_started and not saved.scan_started
    )
    assert saved.settings.revision == 1 and saved.rule.rename_preview and not saved.rule.move_preview
    await env.reopen()
    reopened = await env.steward.settings.query(await request(env))
    assert reopened.fingerprint == saved.fingerprint and reopened.rule == saved.rule
    assert not env.reader.calls and not env.adapter.source.calls


async def test_old_form_conflicts_after_save_and_policy_external_change(slice_env):
    env = slice_env
    query = await request(env)
    view = await env.steward.settings.query(query)
    saved = await env.steward.settings.save(edit(query, view))
    with pytest.raises(StewardError, match="settings_changed"):
        await env.steward.settings.save(edit(query, view, watched=False))
    await env.steward.set_policy(
        StewardPolicyEdit(
            expected_context=query.expected_context, rule=saved.rule.model_copy(update={"dedup_review": True})
        )
    )
    with pytest.raises(StewardError, match="settings_changed"):
        await env.steward.settings.save(edit(query, saved))


@pytest.mark.parametrize(
    "field,value",
    [
        ("directory_ref", "arbitrary-private-dir"),
        ("source_ref", "other-source"),
        ("connection_epoch", "other-epoch"),
        ("config_generation", 9),
        ("item_id", "a" * 64),
        ("rule_id", "forged-rule"),
    ],
)
async def test_settings_cannot_turn_known_item_into_other_directory_authority(slice_env, field, value):
    env = slice_env
    query = await request(env)
    view = await env.steward.settings.query(query)
    with pytest.raises(StewardError, match="scope_changed"):
        await env.steward.settings.save(edit(query, view, rule=view.rule.model_copy(update={field: value})))
    assert not env.steward.policies() and not env.reader.calls


async def test_file_exception_and_existing_directory_rule_are_reused_not_overwritten(slice_env):
    env = slice_env
    query = await request(env)
    view = await env.steward.settings.query(query)
    existing = view.rule.model_copy(
        update={"rule_id": "existing-dir-policy", "label": "既有下载目录", "dedup_review": True}
    )
    exception = existing.model_copy(
        update={
            "rule_id": "fixture-file-exception",
            "item_id": env.item.item_id,
            "rename_preview": False,
            "dedup_review": False,
        }
    )
    for rule in (existing, exception):
        await env.steward.set_policy(StewardPolicyEdit(expected_context=query.expected_context, rule=rule))
    reused = await env.steward.settings.query(query)
    assert reused.rule.rule_id == existing.rule_id and reused.effective_policy.source == "file_exception"
    saved = await env.steward.settings.save(
        edit(query, reused, rule=reused.rule.model_copy(update={"rename_preview": True}))
    )
    assert len(env.steward.policies()) == 2
    assert next(rule for rule in env.steward.policies() if rule.item_id) == exception
    assert saved.effective_policy.source == "file_exception" and not saved.effective_policy.rename_preview


async def test_source_rebind_does_not_inherit_old_settings_or_accept_old_form(slice_env):
    env = slice_env
    query = await request(env)
    view = await env.steward.settings.query(query)
    await env.steward.settings.save(edit(query, view))
    env.reader.epoch = "SIMULATED-epoch-2"
    with pytest.raises(StewardError, match="context_changed"):
        await env.steward.settings.save(edit(query, view))
    assert not env.reader.calls


def arl_snapshot(env, **connection_changes):
    connection = {
        "schema_version": 1,
        "source_ref": env.reader.source_ref,
        "config_generation": env.reader.generation,
        "connection_epoch": env.reader.epoch,
        "configured": True,
        "enabled": True,
        "state": "online",
        "reachable": True,
        "authenticated": True,
        "readable": True,
        "failure": None,
        "retry_in_seconds": None,
        "checked_at": "2026-10-02T13:00:00Z",
    }
    connection.update(connection_changes)
    return {
        "schema_version": 1,
        "service": "arl.nas.read-session.v1",
        "scope": {
            "source_ref": env.reader.source_ref,
            "credential_ref": "fixture-alias",
            "credential_generation": "a" * 32,
            "config_fingerprint": "b" * 64,
            "root_descriptor_digest": "c" * 64,
            "config_generation": env.reader.generation,
        },
        "credential_reference_saved": True,
        "credential": {
            "schema_version": 1,
            "service": "arl.nas.read-session.v1",
            "state": "resolved",
            "failure": None,
            "attempts_used": 1,
            "attempts_remaining": 0,
            "credential_available": True,
        },
        "connection": connection,
        "writable": {"state": "unsupported", "checked_at": None},
        "steward_content_grant": "not_provided",
    }


async def test_arl_public_evidence_never_creates_content_or_write_grant(slice_env):
    env = slice_env
    env.steward.session_status = lambda: arl_snapshot(env)
    result = (await env.steward.context())["session"]
    assert result["connected"] and result["readable"] is True
    assert result["configuration_saved"] and result["credential_reference_saved"]
    assert result["writable"] == "unsupported" and not result["content_grant_provided"]
    await env.plan()
    await env.steward.queue.activate_fixture(env.job.job_id, await env.context())
    assert (await env.steward.queue.step_fixture(await env.context())).reason == "content_grant_required"
    assert not env.reader.calls and not env.adapter.source.calls


async def test_arl_saved_readable_writable_and_binding_are_independent(slice_env):
    env = slice_env
    env.steward.session_status = lambda: arl_snapshot(env, readable=None)
    result = (await env.steward.context())["session"]
    assert result["connected"] and result["readable"] is None and result["label"] == "已连接 · 待读取核对"
    env.steward.session_status = lambda: arl_snapshot(env, config_generation=99)
    result = (await env.steward.context())["session"]
    assert result["reason"] == "binding_changed" and result["readable"] is None
    env.steward.session_status = lambda: arl_snapshot(env, state="paused", failure="nas_auth_failed")
    result = (await env.steward.context())["session"]
    assert result["reason"] == "authentication_required" and not result["retry_credentials"]
    bad = copy.deepcopy(arl_snapshot(env))
    bad["service"] = "wrong-service"
    env.steward.session_status = lambda: bad
    assert (await env.steward.context())["session"]["reason"] == "handoff_unsupported"


async def test_settings_http_is_local_metadata_and_has_no_executor_route(slice_env):
    env = slice_env
    app = FastAPI()
    app.include_router(create_router(env.steward))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        query = await request(env)
        view = await client.post("/api/files/settings/query", json=query.model_dump(mode="json"))
        assert view.status_code == 200 and view.json()["activation"] == "held"
        snapshot = await env.steward.settings.query(query)
        response = await client.post(
            "/api/files/settings/preview", json=edit(query, snapshot).model_dump(mode="json")
        )
        assert response.status_code == 200 and response.json()["saved"]
        assert (await client.get("/api/files/queue")).json() == []
        for path in ("queue/run", "settings/connect", "run", "scan", "execute", "rollback", "upload"):
            assert (await client.post("/api/files/" + path, json={})).status_code == 404
    assert not env.reader.calls and not env.adapter.source.calls
