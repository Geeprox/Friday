"""Join an imported house to devices by saved upstream identities.

Room labels are presentation, never device authority. Missing positions remain
missing; membership does not create a point in the floor plan.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Membership(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal[1] = 1
    floor_id: str = Field(min_length=1, max_length=128)
    home_id: str = Field(min_length=1, max_length=128)
    device_rooms: dict[str, str] = Field(default_factory=dict, max_length=2048)


def from_mijia(homes: list[dict], layout, region: str) -> Membership:
    """Select one home by its imported room IDs, never a household name."""
    room_ids = {r.id for r in layout.rooms}
    candidates = [h for h in homes if room_ids <= {str(r['id']) for r in h['rooms']}]
    if len(candidates) != 1 or not region.isalpha() or len(region) != 2:
        raise ValueError('spatial_home_ambiguous')
    home = candidates[0]
    members = {}
    for room in home['rooms']:
        room_id = str(room['id'])
        if room_id not in room_ids:
            continue
        for did in room['devices']:
            key = f'xiaomi-identity:{region}_{did}'
            if key in members and members[key] != room_id:
                raise ValueError('spatial_room_ambiguous')
            members[key] = room_id
    return Membership(floor_id=str(layout.source.get('floor_id', '')),
                      home_id=str(home['home_id']), device_rooms=members)


def load(path: Path, layout) -> Membership | None:
    if (layout is None or not path.exists() or path.is_symlink()
            or path.stat().st_size > 262144):
        return None
    value = Membership.model_validate_json(path.read_bytes())
    if value.schema_version != 1 or value.floor_id != str(layout.source.get('floor_id', '')):
        return None
    if not set(value.device_rooms.values()) <= {r.id for r in layout.rooms}:
        return None
    return value


def project(devices: list[dict], layout, membership: Membership | None) -> list[dict]:
    if layout is None:
        return devices
    rooms = {r.id: r for r in layout.rooms}
    located = {p.id: p.room_id for p in [*layout.devices, *layout.unplaced_devices]
               if p.room_id in rooms}
    result = []
    for device in devices:
        bindings = device.get('bindings', [])
        identities = [device['id'], *[b['external_id'] for b in bindings]]
        placed = {located[key] for key in identities if key in located}
        imported = {membership.device_rooms[key] for key in [f'friday:{device["id"]}',
                    *[f'{b["source"]}:{b["external_id"]}' for b in bindings]]
                    if membership and key in membership.device_rooms}
        candidates = placed or imported
        # Exact unique labels cover non-Mi Home sources assigned to the same
        # imported room. Prefix/suffix and fuzzy names are deliberately refused.
        if not candidates:
            candidates = {r.id for r in layout.rooms if r.name == device['room']}
        room_id = next(iter(candidates)) if len(candidates) == 1 else None
        result.append(dict(device, source_room=device['room'], room_id=room_id,
                           room=rooms[room_id].name if room_id else device['room']))
    return result
