"""Explicit simulated mDNS records. No household discovery in the test suite."""
import asyncio
import socket

import pytest
from zeroconf import ServiceInfo

from homeplane.discovery import SERVICE, HADiscovery, candidate


def advertised(addresses=("192.168.10.40",), uuid="simulated-fixture", base="http://public.example:8123"):
    return ServiceInfo(SERVICE, "Simulated." + SERVICE,
                       addresses=[socket.inet_pton(socket.AF_INET6 if ":" in a else socket.AF_INET, a)
                                  for a in addresses], port=8123,
                       properties={"uuid": uuid, "base_url": base, "location_name": "Simulated"},
                       server="simulated.local.")


def test_discovery_uses_lan_address_not_public_base_url_and_does_not_authorize():
    item = candidate(advertised())
    assert item["url"] == "http://192.168.10.40:8123"
    assert item["instance_id"] == "simulated-fixture"
    assert item["authenticated"] is False
    assert "public.example" not in str(item)


@pytest.mark.parametrize("addresses", [("8.8.8.8",), ("127.0.0.1",), ("169.254.2.3",), ("0.0.0.0",)])
def test_discovery_rejects_non_lan_candidates(addresses):
    assert candidate(advertised(addresses)) is None


def test_discovery_ipv6_and_invalid_identity_are_explicit():
    item = candidate(advertised(("fd00::40",), uuid="invalid\nidentifier", base="https://example.com"))
    assert item["url"] == "https://[fd00::40]:8123"
    assert item["instance_id"].startswith("mdns-")
    assert "invalid" not in str(item)


@pytest.mark.asyncio
async def test_discovery_preserves_multiple_instances_and_shares_bounded_probe():
    calls = []
    async def scanner():
        calls.append(1)
        await asyncio.sleep(0)
        return [candidate(advertised(uuid="simulated-a")),
                candidate(advertised(("192.168.10.41",), uuid="simulated-b"))]
    service = HADiscovery(scanner)
    a, b = await asyncio.gather(service.discover(), service.discover())
    assert len(calls) == 1
    assert a == b and a["selection_required"] is True
    assert len(a["instances"]) == 2
    assert a["device_writes"] == "disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("SIMULATED-private-error"), TimeoutError("SIMULATED-timeout")])
async def test_discovery_failure_is_sanitized_and_manual_fallback_available(error):
    async def scanner():
        raise error
    result = await HADiscovery(scanner).discover()
    assert result["status"] == "discovery_unavailable"
    assert result["fallback"] == "manual_address"
    assert "SIMULATED" not in str(result)


@pytest.mark.asyncio
async def test_no_mdns_is_not_treated_as_no_ha_exists():
    async def scanner():
        return []
    result = await HADiscovery(scanner).discover()
    assert result["status"] == "not_found" and "VLAN" in result["note"]
