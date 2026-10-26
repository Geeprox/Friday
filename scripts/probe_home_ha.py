#!/usr/bin/env python3
"""Authorized read-only probe of the one explicitly configured HA instance.

Never scans networks or invokes services. Existing OAuth refresh may POST only /auth/token.
Private output is ignored by Git, chmod 0600, household-sensitive, seven-day retention.
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from friday.home import Credentials  # noqa: E402
from homeplane.ha import HAReadOnlyAdapter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "data/homeplane/ha-snapshot.json"
REPORT = ROOT / "docs/evidence/ha-live-probe.json"


def write_json(path, value, *, private=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.chmod(temporary, 0o600 if private else 0o644)
    temporary.replace(path)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def office_candidates(snapshot, raw_states):
    rows = {row["entity_id"]: row for row in raw_states}
    result = []
    for entity in snapshot.entities:
        if entity.kind != "light":
            continue
        text = " ".join([entity.name, entity.room or "", *(b.external_id for b in entity.bindings if b.source == "ha")]).lower()
        if not any(word in text for word in ("office", "study", "书房", "辦公", "办公")):
            continue
        entity_id = next((b.external_id for b in entity.bindings if b.source == "ha"), None)
        if entity_id is None:
            continue
        row = rows.get(entity_id, {})
        attrs = row.get("attributes", {})
        result.append({"entity_id": entity_id, "canonical_id": entity.id, "name": entity.name, "room": entity.room,
                       "state": row.get("state"), "attributes": {k: attrs[k] for k in (
                           "supported_features", "supported_color_modes", "color_mode", "brightness", "min_mireds",
                           "max_mireds", "min_color_temp_kelvin", "max_color_temp_kelvin", "color_temp_kelvin"
                       ) if k in attrs}})
    return result


async def probe(config_path=ROOT / "friday.home.json"):
    report = {"checked_at": datetime.now(UTC).isoformat(), "simulated": False,
              "operation": "authorized_configured_ha_read_only_probe", "status": "BLOCKED",
              "real_write_attempted": False, "hardware_behavior_verified": False,
              "sensitivity": "redacted", "private_evidence_retention_days": 7}
    try:
        if not config_path.is_file():
            raise LookupError("configured_ha_file_missing")
        config = json.loads(config_path.read_text())
        url = config.get("ha_url")
        if not isinstance(url, str) or not url:
            raise LookupError("configured_ha_url_missing")
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise LookupError("configured_ha_url_invalid")
        settings = SimpleNamespace(ha_url=url, ha_token_env=config.get("ha_token_env", "FRIDAY_HA_TOKEN"))
        env_token = bool(os.environ.get(settings.ha_token_env))
        credential_path = ROOT / "data/credentials/home-assistant.json"
        if not env_token and not credential_path.is_file():
            raise LookupError("ha_token_environment_and_existing_oauth_file_missing")
        report["credential_source"] = "configured_environment" if env_token else "existing_oauth_file"
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=15) as client:
            credentials = Credentials(settings, client)
            credentials.path = credential_path
            if not credentials.connected():
                raise LookupError("existing_credentials_do_not_match_configured_ha")
            token = await credentials.token()
            adapter = HAReadOnlyAdapter(url, token, "configured-" + digest(url)[:16], client=client)
            configuration = await adapter._get("/api/config")
            snapshot = await adapter.snapshot()
            states = await adapter._get("/api/states")
            candidates = office_candidates(snapshot, states)
            report.update(status="PASS" if adapter.health()["transport"] == "online" else "BLOCKED",
                          ha_version=configuration.get("version"), entity_count=len(snapshot.entities),
                          state_count=len(states), health=adapter.health(), office_light_candidate_count=len(candidates),
                          office_light_candidates=[{"entity_id_sha256": digest(c["entity_id"]),
                                                    "state": c["state"], "attributes": c["attributes"]} for c in candidates],
                          validation="real HA read protocol only; no light writes or hardware effects tested")
            write_json(PRIVATE, {"captured_at": report["checked_at"], "sensitivity": "household",
                                 "delete_after": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
                                 "snapshot": snapshot.model_dump(mode="json"), "states": states,
                                 "office_light_candidates": candidates}, private=True)
    except LookupError as exc:
        report["reason"] = str(exc)
    except httpx.HTTPStatusError as exc:
        report["reason"] = "ha_http_status"
        report["http_status"] = exc.response.status_code
    except Exception as exc:
        report["reason"] = "probe_failed"
        report["error_type"] = type(exc).__name__
    write_json(REPORT, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(probe()))
