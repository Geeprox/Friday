"""Opaque, durable source bindings for historical event review.

This digest is compatibility evidence only. It grants no read scope, identity,
causality, physical confirmation or device authority. Never derive it for an old
event from the current registry: the producer must record it at observation time.
"""

import hashlib
import json
from urllib.parse import urlsplit

from .contracts import Entity


def _label(value):
    if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    try:
        return len(value.encode('utf8')) <= 4096
    except UnicodeError:
        return False


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf8')).hexdigest()


def _endpoint(url):
    if not _label(url):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {'http', 'https'} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment):
            return None
        port = parts.port
        return [parts.scheme, parts.hostname.lower(),
                port if port is not None else (443 if parts.scheme == 'https' else 80), parts.path.rstrip('/')]
    except ValueError:
        return None


def _binding(entity):
    if (not isinstance(entity, Entity) or not _label(entity.id)
            or not _label(entity.physical_source_id) or len(entity.bindings) > 128):
        return None
    values = [(item.source, item.external_id) for item in entity.bindings]
    if any(not _label(source) or not _label(external) for source, external in values):
        return None
    if len(set(values)) != len(values):
        return None
    # Preserve the same entity + complete external-binding identity used by the
    # Action Gateway, adding the explicit physical source and registered room.
    return [entity.id, entity.physical_source_id, sorted(values)]


def entity_source_context(adapter, entity):
    """Return a stable 64-hex source digest, or None when identity is unqualified.

    HA must have a current qualified registry. A same-name/id replacement, source
    endpoint change or registered room move changes the digest. Boot, live values,
    credentials and event cursor never enter it. HA display names are excluded;
    Simulator evidence uses the canonical, explicitly simulated fixture definition
    in a separate namespace.
    """
    binding = _binding(entity)
    if binding is None:
        return None
    # Local imports avoid a module cycle with the two event producers.
    from .ha import HAReadOnlyAdapter

    if isinstance(adapter, HAReadOnlyAdapter):
        endpoint = _endpoint(adapter.url)
        if endpoint is None or not _label(adapter.instance_id) or adapter._registry_ok is not True or entity.simulated:
            return None
        external = [item.external_id for item in entity.bindings if item.source == 'ha']
        if len(external) != 1:
            return None
        registry = adapter._registry.get(external[0])
        if not isinstance(registry, dict) or registry.get('entity_id') != external[0]:
            return None
        stable = adapter._registry_identity(external[0])
        if not _label(stable):
            return None
        identity = adapter._identity(stable)
        if entity.id != identity or entity.physical_source_id != identity:
            return None
        device = registry.get('device_id')
        device_bindings = [item.external_id for item in entity.bindings if item.source == 'ha-device-registry']
        if device_bindings != ([device] if device is not None else []):
            return None
        registration = {key: registry.get(key) for key in ('id', 'unique_id', 'platform', 'device_id', 'area_id')}
        registration['device_area_id'] = adapter._devices.get(device, {}).get('area_id')
        if any(value is not None and not _label(value) for value in registration.values()):
            return None
        area = registration['area_id'] or registration['device_area_id']
        expected_room = adapter._areas.get(area, {}).get('name') or area
        if entity.room != expected_room:
            return None
        return _digest(['friday.event-source.v1', 'ha', endpoint, adapter.instance_id, binding, registration])

    from .simulator import Simulator

    if isinstance(adapter, Simulator):
        fixture_identity = getattr(adapter, 'source_fixture_identity', None)
        if (not entity.simulated or not _label(fixture_identity) or not _label(entity.kind)
                or entity.room is not None and not _label(entity.room)):
            return None
        measurements = getattr(adapter, 'measurements', None)
        candidates = [item for item in [*adapter.entities, *getattr(measurements, 'entities', [])]
                      if item.id == entity.id]
        if (len(candidates) != 1 or _binding(candidates[0]) != binding or not candidates[0].simulated
                or candidates[0].kind != entity.kind or candidates[0].room != entity.room):
            return None
        return _digest(['friday.event-source.v1', 'simulator', fixture_identity, binding,
                        entity.kind, entity.room])
    return None


def registered_room_context(adapter, room):
    """Bind an explicitly reviewed room to one registered source identity.

    Exact registry labels are accepted only when they identify one stable area.
    Duplicate names, unknown areas and unqualified registries remain unknown.
    This is metadata-only and never discovers, connects or grants authority.
    """
    if not _label(room) or len(room) > 128:
        return None
    from .ha import HAReadOnlyAdapter

    if isinstance(adapter, HAReadOnlyAdapter):
        endpoint = _endpoint(adapter.url)
        if endpoint is None or not _label(adapter.instance_id) or adapter._registry_ok is not True:
            return None
        areas = {key for key, value in adapter._areas.items()
                 if _label(key) and isinstance(value, dict) and value.get('area_id') == key
                 and (room == key or room == value.get('name'))}
        if len(areas) != 1:
            return None
        return _digest(['friday.registered-room.v1', 'ha', endpoint, adapter.instance_id, next(iter(areas))])

    from .simulator import Simulator

    if isinstance(adapter, Simulator):
        fixture = getattr(adapter, 'source_fixture_identity', None)
        measurements = getattr(adapter, 'measurements', None)
        if not _label(fixture) or not any(
                item.simulated and item.room == room
                for item in [*adapter.entities, *getattr(measurements, 'entities', [])]):
            return None
        return _digest(['friday.registered-room.v1', 'simulator', fixture, room])
    return None
