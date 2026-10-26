"""Lost responses must reconcile the entire closed NAS configuration contract."""

import json
import re
from pathlib import Path

import pytest

from homeplane.nas_connection import NASConnectionConfig
from tests.test_homeplane_nas_onboarding_console import (
    test_synthetic_nas_console_consumer as run_console_fixture,
)


def test_console_canonical_fields_and_defaults_match_runtime_model():
    source = (Path(__file__).parents[1] / "homeplane/static/app.js").read_text()
    defaults = json.loads(re.search(r"const configDefaults=Object.freeze\((\{.*?\})\);", source, re.S)[1])
    required = json.loads(re.search(r"const configRequired=Object.freeze\((\[.*?\])\);", source, re.S)[1])
    schema = NASConnectionConfig.model_json_schema()
    assert defaults == {key: value["default"] for key, value in schema["properties"].items() if "default" in value}
    assert set(required) == set(schema["required"])
    assert set(defaults) | set(required) == set(schema["properties"])
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("field", [*NASConnectionConfig.model_fields, "unknown_option"])
def test_every_configuration_field_change_requires_explicit_review(field):
    run_console_fixture("reconcile_changed:" + field)


@pytest.mark.parametrize("field", [
    "schema_version", "root", "enabled", "require_encryption", "sftp_root_confined",
    "connect_timeout_seconds", "operation_timeout_seconds", "health_interval_seconds",
    "backoff_initial_seconds", "backoff_max_seconds",
])
def test_explicit_null_is_not_an_omitted_nonnullable_default(field):
    run_console_fixture("reconcile_null:" + field)


@pytest.mark.parametrize("case", [
    "missing_required", "boolean_string", "number_string", "nullable_type", "unknown_proto",
])
def test_unknown_or_invalid_representation_never_claims_match(case):
    run_console_fixture("reconcile_invalid:" + case)


def test_unknown_field_already_present_in_both_configs_is_not_silently_ignored():
    run_console_fixture("reconcile_unknown_existing")


@pytest.mark.parametrize("case", ["first_save", "omitted", "nullable_sftp", "reordered", "numeric"])
def test_declared_defaults_normalize_without_changing_save_connect_separation(case):
    run_console_fixture("reconcile_defaults:" + case)


def test_lost_unaccepted_save_allows_explicit_cas_retry_but_rejects_late_duplicate():
    run_console_fixture("reconcile_lost_unaccepted")


@pytest.mark.parametrize("operation", ["resume", "disable"])
def test_lost_connect_or_disconnect_with_changed_config_requires_review_without_draft(operation):
    run_console_fixture("reconcile_lost_operation:" + operation)
