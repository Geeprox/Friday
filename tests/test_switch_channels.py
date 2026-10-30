"""Wireless channel modes come from stable integration bindings, never names."""
from types import SimpleNamespace
from homeplane.switch_channels import project


def records():
    registry={
        'switch.custom_name':dict(id='on-ref',device_id='device-one',platform='xiaomi_home',unique_id='xiaomi_home.imported_device_on_p_19_7'),
        'select.any_name':dict(id='mode-ref',device_id='device-one',platform='xiaomi_home',unique_id='xiaomi_home.imported_device_mode_p_19_82')}
    rows=[dict(entity_id='switch.custom_name',state='on',attributes={}),
          dict(entity_id='select.any_name',state='无线开关',attributes={'options':['有线和无线开关','无线开关']})]
    return registry,rows


def test_same_service_identity_distinguishes_wireless_from_relay_and_unknown():
    registry,rows=records()
    assert project(registry,rows)['switch.custom_name']['role']=='wireless'
    rows[1]['state']='有线和无线开关';assert project(registry,rows)['switch.custom_name']['role']=='relay'
    rows[1]['state']='unavailable';assert project(registry,rows)['switch.custom_name']['role']=='unknown'


def test_renaming_and_on_off_values_cannot_change_channel_role():
    registry,rows=records();rows[0]['state']='off';rows[0]['attributes']['friendly_name']='a real light'
    assert project(registry,rows)['switch.custom_name']['role']=='wireless'
    registry['select.any_name']['unique_id']='xiaomi_home.imported_device_mode_p_20_82'
    assert project(registry,rows)['switch.custom_name']['role']=='unknown'


def test_different_device_or_ambiguous_bindings_never_supply_wireless_evidence():
    registry,rows=records();registry['select.other']=dict(registry['select.any_name']);rows.append(dict(rows[1],entity_id='select.other'))
    assert project(registry,rows)['switch.custom_name']['role']=='unknown'
    registry,rows=records();registry['select.any_name']['device_id']='different'
    assert 'switch.custom_name' not in project(registry,rows)


def test_wireless_and_unknown_roles_never_admit_relay_writes():
    from homeplane.contracts import Entity,Binding
    from homeplane.ha_control import admitted_capabilities
    e=Entity(id='stable-id',name='any name',kind='switch',state_version='v1',physical_source_id='stable-id',
             bindings=[Binding(source='ha',external_id='switch.custom_name'),Binding(source='ha-device-registry',external_id='device-one')],capabilities=[])
    adapter=SimpleNamespace(_services=[dict(domain='switch',services={'turn_on':{},'turn_off':{}})],_control_attributes={})
    for role in ['wireless','unknown']:
        adapter._switch_channels={'switch.custom_name':{'role':role}}
        assert admitted_capabilities(adapter,e,SimpleNamespace(operations=['switch.set']))==[]
    adapter._switch_channels={'switch.custom_name':{'role':'relay'}}
    assert [c.name for c in admitted_capabilities(adapter,e,SimpleNamespace(operations=['switch.set']))]==['switch.set']
