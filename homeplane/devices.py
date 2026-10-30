"""Physical-device projection above functional entities, without source averaging.

HA Registry identifiers are upstream identity evidence, not hardware certification.
No name matching, no unbound-entity-to-physical-device guess, no hub-based sensor merge.
"""
import hashlib
import json
import re

from .contracts import Binding, PhysicalDevice

PRIORITY = ("light", "climate", "humidifier", "water_heater", "cover", "vacuum", "fan", "camera", "media_player",
            "lock", "sensor", "binary_sensor", "switch")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def xiaomi_identifier(value):
    """Known Xiaomi registry identities, including HA's escaped BLE DID form."""
    if not isinstance(value,str):return None
    if re.fullmatch(r'[a-z]{2}_[0-9]{1,32}',value):return value
    ble=re.fullmatch(r'([a-z]{2})_blt[._]([0-9])[._]([a-z0-9]{1,64})',value)
    if ble:return f'{ble[1]}_blt.{ble[2]}.{ble[3]}'
    return None


def project_ha(entities, device_registry, areas, instance_id):
    groups = {}
    for entity in entities:
        device = next((b.external_id for b in entity.bindings if b.source == "ha-device-registry"), None)
        if device and device in device_registry:
            groups.setdefault(device, []).append(entity)
    projected = []
    for registry_id, metadata in device_registry.items():
        functions = groups.get(registry_id, [])
        identifiers = metadata.get("identifiers", [])
        if not isinstance(identifiers, (list, tuple)):
            identifiers = []
        # Observed Xiaomi Home format: region_numeric-did, e.g. cn_123456789.
        # Other namespaces/opaque formats remain instance-scoped HA Registry identities.
        xiaomi = sorted({xiaomi_identifier(item[1]) for item in identifiers
                         if isinstance(item, (list, tuple)) and len(item) == 2
                         and item[0] == "xiaomi_home" and isinstance(item[1], str)
                         and xiaomi_identifier(item[1]) is not None})
        known_xiaomi = len(xiaomi) == 1
        # BLE identities add a room-join binding without changing the device ID
        # already used by saved positions and selections in earlier versions.
        numeric_xiaomi = known_xiaomi and bool(re.fullmatch(r'[a-z]{2}_[0-9]+',xiaomi[0]))
        identity = "device:xiaomi:" + _digest(xiaomi[0]) if numeric_xiaomi else "device:ha:" + _digest([instance_id, registry_id])
        functions = sorted(functions, key=lambda e: (PRIORITY.index(e.kind) if e.kind in PRIORITY else len(PRIORITY), e.id))
        primary = functions[0] if functions else None
        fallback_name = primary.name if primary else "Unnamed registered device"
        name = metadata.get("name_by_user") or metadata.get("name") or fallback_name
        name = name if isinstance(name, str) and name else fallback_name
        area = metadata.get("area_id")
        room = areas.get(area, {}).get("name") or (primary.room if primary else None)
        bindings = [Binding(source="ha-device-registry", external_id=registry_id)]
        if known_xiaomi:
            bindings.append(Binding(source="xiaomi-identity", external_id=xiaomi[0]))
        caps = {(c.name, c.access): c for e in functions for c in e.capabilities}
        limits = ["Device grouping does not establish independent measurement sources or physical verification."]
        if not functions:
            limits.append("Registered device has no exposed current-state functions; presence is not availability.")
        if len({e.room for e in functions if e.room}) > 1:
            limits.append("Function areas disagree; device area used for display, raw function areas retained.")
        category = "service" if metadata.get("entry_type") == "service" or (functions and all(e.kind in {"sun", "conversation", "event"} for e in functions)) else "device"
        projected.append(PhysicalDevice(id=identity, name=name, kind=primary.kind if primary else "device", room=room,
                                       function_ids=[e.id for e in functions], bindings=bindings,
                                       capabilities=list(caps.values()), category=category,
                                       identity_basis="xiaomi_identifier" if known_xiaomi else "ha_device_registry",
                                       limitations=limits))
    unique = {}
    for device in projected:
        if device.id not in unique:
            unique[device.id] = device
            continue
        previous = unique[device.id]
        bindings = {(b.source, b.external_id): b for b in [*previous.bindings, *device.bindings]}
        caps = {(c.name, c.access): c for c in [*previous.capabilities, *device.capabilities]}
        unique[device.id] = previous.model_copy(update={
            "function_ids":sorted(set(previous.function_ids + device.function_ids)),
            "bindings":list(bindings.values()), "capabilities":list(caps.values()),
            "limitations":list(dict.fromkeys([*previous.limitations, *device.limitations,
                "Multiple registry entries share an upstream identity; measurement sources remain distinct."]))})
    return sorted(unique.values(), key=lambda d: d.id)


def project_simulated(entities):
    return [PhysicalDevice(id="device:" + e.id, name=e.name, kind=e.kind, room=e.room,
                           function_ids=[e.id], bindings=e.bindings, capabilities=e.capabilities,
                           identity_basis="simulated", simulated=True) for e in entities]
