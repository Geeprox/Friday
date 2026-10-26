"""Bounded HA mDNS discovery. No subnet scan, HTTP probe, credentials or device writes.

Reference: https://developers.home-assistant.io/docs/api/instance_discovery/
Advertisements are candidates, not authentication or hardware verification.
"""
import asyncio
import hashlib
import ipaddress
import re
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

SERVICE = "_home-assistant._tcp.local."


def candidate(info):
    """Use advertised LAN A/AAAA records, never the potentially public TXT base_url."""
    if info.type != SERVICE or not 1 <= info.port <= 65535:
        return None
    addresses = []
    for raw in info.parsed_addresses()[:16]:
        try:
            address = ipaddress.ip_address(raw)
            if (address.is_private and not address.is_reserved and not address.is_loopback and not address.is_link_local
                    and not address.is_multicast and not address.is_unspecified):
                addresses.append(address)
        except ValueError:
            continue
    if not addresses:
        return None
    addresses = sorted(set(addresses), key=lambda a: (a.version, int(a)))
    properties = info.decoded_properties
    identity = properties.get("uuid") or properties.get("ha_uuid")
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", identity):
        identity = "mdns-" + hashlib.sha256(info.name.encode()).hexdigest()[:24]
    name = properties.get("location_name") or info.name.removesuffix("." + SERVICE)
    if not isinstance(name, str) or len(name) > 96 or any(ord(c) < 32 for c in name):
        name = "Home Assistant"
    base = urlsplit(properties.get("base_url") or "")
    # TLS is an advertised property; it does not certify connectivity or trust.
    scheme = "https" if base.scheme == "https" else "http"
    urls = [f"{scheme}://{'[' + str(a) + ']' if a.version == 6 else a}:{info.port}" for a in addresses]
    return {"instance_id": identity, "name": name, "url": urls[0], "addresses": urls,
            "source": "mdns", "authenticated": False}


async def browse(seconds=5):
    aio = AsyncZeroconf(ip_version=IPVersion.All)
    tasks, seen, removed, results = set(), set(), set(), {}
    browser = None

    async def resolve(name):
        try:
            info = AsyncServiceInfo(SERVICE, name)
            if await info.async_request(aio.zeroconf, 1500):
                result = candidate(info)
                if result:
                    results[name] = result
        except (OSError, TimeoutError, ValueError, TypeError):
            pass

    def changed(zeroconf, service_type, name, state_change):
        if state_change == ServiceStateChange.Removed:
            removed.add(name)
            return
        removed.discard(name)
        if name in seen or len(seen) >= 16 or len(name) > 256:
            return
        seen.add(name)
        task = asyncio.create_task(resolve(name))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    try:
        browser = AsyncServiceBrowser(aio.zeroconf, SERVICE, handlers=[changed])
        await asyncio.sleep(min(max(seconds, 0.1), 5))
    finally:
        if browser:
            await browser.async_cancel()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await aio.async_close()
    # Same UUID across interfaces is one instance; different UUIDs are never merged.
    instances = {}
    for name, result in results.items():
        if name in removed:
            continue
        previous = instances.get(result["instance_id"])
        if previous:
            urls = sorted(set(previous["addresses"] + result["addresses"]))
            previous["addresses"] = urls
        else:
            instances[result["instance_id"]] = result
    return sorted(instances.values(), key=lambda item: item["instance_id"])


class HADiscovery:
    def __init__(self, scanner=browse):
        self.scanner, self.report, self.checked = scanner, None, 0
        self.gate = asyncio.Lock()

    async def discover(self):
        async with self.gate:
            if self.report and time.monotonic() - self.checked < 60:
                return self.report
            try:
                async with asyncio.timeout(8):
                    instances = await self.scanner()
                status = "found" if instances else "not_found"
            except (OSError, TimeoutError):
                status, instances = "discovery_unavailable", []
            self.report = {"status": status, "instances": instances, "checked_at": datetime.now(UTC).isoformat(),
                           "selection_required": len(instances) > 1, "scope": SERVICE,
                           "device_writes": "disabled", "fallback": "manual_address",
                           "note": "Discovery does not authorize access; VLAN/mDNS filtering can hide instances."}
            self.checked = time.monotonic()
            return self.report
