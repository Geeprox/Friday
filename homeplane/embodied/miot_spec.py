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
    for service in spec.get('services', []):
        if not isinstance(service, dict) or not isinstance(service.get('properties', []), list):
            raise ValueError('miot_spec_invalid')
        for prop in service.get('properties', []):
            if not isinstance(prop, dict) or not isinstance(prop.get('type', ''), str):
                raise ValueError('miot_spec_invalid')
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
    if (not prop or access not in prop.get('access', [])
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


def camera_properties(camera):
    props = properties(camera.get('spec', {}))
    aliases = {'cruise': 'cruise', 'human_tracking': 'human-tracking',
               'pet_tracking': 'pet-tracking', 'human_close_up': 'human-close-up',
               'camera_power': 'on', 'presets': 'preset-list', 'active_preset': 'preset-id'}
    reads = {key: property_ref(props, name, 'read') for key, name in aliases.items()}
    for key, name in aliases.items():
        if key != 'presets':
            property_ref(props, name, 'write')
    return reads
