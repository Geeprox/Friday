"""Household-owned visual semantics, independent of source geometry and authority."""
import json
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .embodied.paths import PRIVATE

Fixture = Literal['main_light', 'pendant', 'led_strip', 'downlight', 'spotlight',
                  'accent_light', 'ceiling_vent', 'wall_unit', 'power_supply', 'generic']


class Style(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fixture: Fixture


class Profile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    climate_style: Literal['auto', 'ceiling_vent', 'wall_unit'] = 'auto'
    overrides: dict[str, Style] = Field(default_factory=dict, max_length=512)


def load() -> Profile:
    path = PRIVATE / 'home-presentation.json'
    if not path.exists():
        return Profile()
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError('presentation_profile_unavailable')
    return Profile.model_validate_json(path.read_bytes())


def save(profile: Profile, expected_revision: int) -> Profile:
    import fcntl
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(PRIVATE / 'home-presentation.lock', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = load()
        if current.revision != expected_revision:
            raise ValueError('presentation_profile_changed')
        if any(not 1 <= len(key) <= 256 for key in profile.overrides):
            raise ValueError('presentation_target_invalid')
        updated = profile.model_copy(update={'revision': current.revision + 1})
        payload = updated.model_dump_json()
        if len(payload.encode()) > 65536:
            raise ValueError('presentation_profile_too_large')
        path = PRIVATE / 'home-presentation.json'
        temporary = path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return updated
    finally:
        os.close(lock)


def appearance(kind, name, profile: Profile, identities=()):
    override = next((profile.overrides[i].fixture for i in identities if i in profile.overrides), None)
    if override:
        fixture = override
    elif kind == 'climate':
        fixture = profile.climate_style if profile.climate_style != 'auto' else 'generic'
    elif kind == 'light':
        fixture = ('power_supply' if '电源' in name else 'led_strip' if '灯带' in name
                   else 'downlight' if '筒灯' in name else 'spotlight' if '射灯' in name
                   else 'pendant' if '吊灯' in name else 'accent_light' if any(x in name for x in ('台灯', '壁灯', '氛围'))
                   else 'main_light')
    else:
        fixture = 'generic'
    role = {'main_light': 'main', 'pendant': 'main', 'led_strip': 'strip',
            'downlight': 'downlight', 'spotlight': 'spotlight', 'accent_light': 'accent'}.get(fixture)
    return dict(fixture=fixture, lighting_role=role, emits_light=role is not None,
                mount='ceiling' if fixture in {'ceiling_vent', 'main_light', 'pendant', 'downlight', 'spotlight'} else 'surface',
                label={'ceiling_vent': '中央空调 · 吊顶出风口', 'wall_unit': '壁挂空调',
                       'power_supply': '照明供电', 'led_strip': '灯带', 'downlight': '筒灯',
                       'spotlight': '射灯', 'main_light': '主灯', 'pendant': '吊灯',
                       'accent_light': '氛围灯'}.get(fixture, '设备'))


def project(device: dict, profile: Profile) -> dict:
    identities = [device['id'], *[b['external_id'] for b in device.get('bindings', [])]]
    visual = appearance(device['kind'], device['name'], profile, identities)
    controls = []
    for control in device['controls']:
        controls.append(dict(control, presentation=appearance(control['kind'], control['name'], profile,
                                                               [control['id'], *identities])))
    if not any(i in profile.overrides for i in identities):
        if len(controls) == 1:
            visual = controls[0]['presentation']
        elif len(controls) > 1:
            visual = appearance('switch', device['name'], profile)
    return dict(device, presentation=visual, controls=controls)
