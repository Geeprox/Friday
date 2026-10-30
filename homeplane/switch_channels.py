"""Join a wall-switch channel to its imported HA/MIoT mode property.

Never classify from a household name, model, entity-name coordinates or on/off
state. The integration's registry unique IDs carry the MIoT service identity.
"""
import re

PROPERTY=re.compile(r'^(.*)_(on|mode)_p_([1-9][0-9]*)_([1-9][0-9]*)$')
WIRELESS={'无线开关','wireless switch','wireless_switch','wireless'}
WIRED={'有线和无线开关','有线开关','wired and wireless switch','wired_and_wireless_switch','wired switch','wired'}


def normalized(value):return value.strip().lower() if isinstance(value,str) else ''


def project(registry,rows):
    values={r['entity_id']:r for r in rows}
    modes={};on=[]
    for external,record in registry.items():
        if record.get('platform')!='xiaomi_home' or not record.get('device_id'):continue
        match=PROPERTY.fullmatch(record.get('unique_id')or'')
        if not match:continue
        key=(record['device_id'],match[1],match[3])
        if match[2]=='mode' and external.startswith('select.'):modes.setdefault(key,[]).append(external)
        elif match[2]=='on' and external.startswith('switch.'):on.append((external,key))
    configurable={record.get('device_id')for external,record in registry.items() if external.startswith('select.') and {normalized(v)for v in values.get(external,{}).get('attributes',{}).get('options',[])}&WIRELESS}
    result={}
    for external,key in on:
        candidates=modes.get(key,[])
        if not candidates:
            if key[0] in configurable:result[external]=dict(role='unknown',reason='channel_mode_binding_missing')
            continue
        if len(candidates)!=1:
            result[external]=dict(role='unknown',reason='ambiguous_mode_binding');continue
        mode=values.get(candidates[0],{});options={normalized(v)for v in mode.get('attributes',{}).get('options',[])}
        # Non-switch operating modes (e.g. fan auto/manual) do not carry this meaning.
        if options and not options&WIRELESS:continue
        value=normalized(mode.get('state'));role='wireless' if value in WIRELESS else 'relay' if value in WIRED else 'unknown'
        result[external]=dict(role=role,mode_entity=candidates[0],mode=mode.get('state'),
            mode_registry_id=registry[candidates[0]].get('id'),basis='registered_miot_service_mode',
            reason=None if role!='unknown' else 'mode_unavailable')
    return result
