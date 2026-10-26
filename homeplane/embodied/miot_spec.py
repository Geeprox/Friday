"""Resolve property IDs from imported MIoT specifications, never device models.

Unknown or ambiguous specifications are read-only. Importing this module performs
no discovery, login or device IO. All account material stays in private storage.
"""
from __future__ import annotations

from .paths import configuration


def properties(spec):
    if not isinstance(spec, dict) or not isinstance(spec.get('services', []), list):
        raise ValueError('miot_spec_invalid')
    result = {}
    service_ids = set()
    for service in spec.get('services', []):
        if not isinstance(service, dict) or not isinstance(service.get('properties', []), list):
            raise ValueError('miot_spec_invalid')
        siid = service.get('iid')
        if type(siid) is not int or not 1 <= siid <= 65535 or siid in service_ids:
            raise ValueError('miot_spec_invalid')
        service_ids.add(siid)
        property_ids = set()
        for prop in service.get('properties', []):
            if not isinstance(prop, dict) or not isinstance(prop.get('type', ''), str):
                raise ValueError('miot_spec_invalid')
            piid, access = prop.get('iid'), prop.get('access')
            if (
                type(piid) is not int or not 1 <= piid <= 65535 or piid in property_ids
                or type(access) is not list or any(
                    type(item) is not str or item not in {'read', 'write', 'notify'} for item in access
                ) or len(access) != len(set(access))
            ):
                raise ValueError('miot_spec_invalid')
            property_ids.add(piid)
            urn = prop.get('type', '').split(':')
            if len(urn) < 4 or urn[2] != 'property':
                continue
            name = urn[3]
            if name in result:
                result[name] = None  # Never guess between duplicate services.
            else:
                result[name] = {**prop, 'siid': service.get('iid'), 'piid': prop.get('iid')}
    return result


def property_ref(props, name, access):
    prop = props.get(name)
    rights = prop.get('access') if isinstance(prop, dict) else None
    if (not prop or type(rights) is not list
            or any(type(item) is not str or item not in {'read', 'write', 'notify'} for item in rights)
            or len(rights) != len(set(rights)) or access not in rights
            or any(type(prop.get(k)) is not int or not 1 <= prop[k] <= 65535 for k in ('siid', 'piid'))):
        raise ValueError('miot_capability_unavailable')
    return prop['siid'], prop['piid']


def cover_binding(config=None):
    config = configuration() if config is None else config
    covers = config.get('miot_covers', {})
    if not isinstance(covers, dict):
        raise ValueError('cover_selection_required')
    selected = config.get('selected_cover')
    if selected is None and len(covers) == 1:
        selected = next(iter(covers))
    if selected not in covers:
        raise ValueError('cover_selection_required')
    binding = covers[selected]
    if not isinstance(binding, dict) or not str(binding.get('did', '')).strip():
        raise ValueError('cover_selection_required')
    props = properties(binding.get('spec', {}))
    refs = {name: property_ref(props, name, access) for name, access in (
        ('current-position', 'read'), ('target-position', 'read'), ('fault', 'read'), ('motor-control', 'write'))}
    refs['target-position'] = property_ref(props, 'target-position', 'write')
    for name in ('current-position', 'target-position'):
        if props[name].get('value-range') != [0, 100, 1]:
            raise ValueError('cover_position_range_unsupported')
    commands = {}
    for item in props['motor-control'].get('value-list', []):
        name = str(item.get('description', '')).lower()
        action = {'open': 'open', 'up': 'open', 'close': 'close', 'down': 'close', 'stop': 'stop'}.get(name)
        if action and type(item.get('value')) is int:
            if action in commands:
                raise ValueError('cover_motor_values_ambiguous')
            commands[action] = item['value']
    if set(commands) != {'open', 'close', 'stop'}:
        raise ValueError('cover_motor_values_unavailable')
    return {**binding, 'refs': refs, 'commands': commands,
            'name': str(binding.get('name') or '窗帘')[:128], 'room': str(binding.get('room') or '')[:80]}


def optional_cover():
    try:
        return cover_binding()
    except (OSError, ValueError, KeyError, TypeError):
        return None


_CAMERA_CAPABILITIES = {
    'cruise': ('cruise', 'cruise-switch'),
    'human_tracking': ('camera-control', 'human-tracking'),
    'pet_tracking': ('ai-detection', 'pet-tracking'),
    'human_close_up': ('ai-detection', 'human-close-up'),
    'camera_power': ('camera-control', 'on'),
    'presets': ('camera-fav-area', 'fav-area'),
    'active_preset': ('camera-fav-area', 'active-fav-area'),
}


def camera_property(camera, capability, access):
    service_name, property_name = _CAMERA_CAPABILITIES[capability]
    spec = camera.get('spec', {})
    if not isinstance(spec, dict) or not isinstance(spec.get('services', []), list):
        raise ValueError('miot_spec_invalid')
    properties(spec)  # Validate all IIDs and permissions before selecting a service.
    # The same property name (for example "on") may occur in several services.
    # Require the semantic service as well as the property; never guess an IID.
    services = []
    for service in spec.get('services', []):
        if not isinstance(service, dict) or not isinstance(service.get('type', ''), str):
            raise ValueError('miot_spec_invalid')
        urn = service.get('type', '').split(':')
        if len(urn) >= 4 and urn[2] == 'service' and urn[3] == service_name:
            services.append(service)
    if len(services) != 1:
        raise ValueError('miot_capability_unavailable')
    return property_ref(properties({'services': services}), property_name, access)


def camera_properties(camera):
    reads = {key: camera_property(camera, key, 'read') for key in _CAMERA_CAPABILITIES}
    for key in _CAMERA_CAPABILITIES:
        if key != 'presets':
            camera_property(camera, key, 'write')
    return reads
