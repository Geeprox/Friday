"""Synthetic room membership and status regressions; no household IO."""
from copy import deepcopy

import pytest

from homeplane.home_layout import Device, Layout
from homeplane.home_spatial import Membership, from_mijia, project


def layout():
    return Layout(source={'kind':'friday_layout','floor_id':'simulated-floor'},rooms=[
        {'id':'r1','name':'房间一','polygon':[[0,0],[.4,0],[.4,.4],[0,.4]]},
        {'id':'r2','name':'房间二','polygon':[[.6,0],[1,0],[1,.4],[.6,.4]]}])


def test_room_membership_uses_upstream_ids_with_renamed_areas_and_no_positions():
    house=layout()
    homes=[{'home_id':'synthetic-home','rooms':[
        {'id':'r1','name':'旧名称','devices':['100']},
        {'id':'r2','name':'另一旧名称','devices':['200']}]}]
    membership=from_mijia(homes,house,'cn')
    devices=[{'id':'opaque-device','room':'家庭前缀 任意旧名称',
              'bindings':[{'source':'xiaomi-identity','external_id':'cn_100'}]}]
    before=deepcopy(devices)
    result=project(devices,house,membership)
    assert result[0]['room']=='房间一' and result[0]['room_id']=='r1'
    assert result[0]['id']==devices[0]['id'] and result[0]['bindings']==devices[0]['bindings']
    assert devices==before and not house.devices and not house.unplaced_devices


def test_unknown_identity_never_joins_by_suffix_or_device_name():
    d={'id':'other-house-device','room':'别的家庭 房间一','bindings':[]}
    assert project([d],layout(),None)[0]['room_id'] is None


def test_ambiguous_homes_and_conflicting_membership_are_refused():
    home={'home_id':'one','rooms':[{'id':'r1','devices':['100']},{'id':'r2','devices':[]}]}
    with pytest.raises(ValueError,match='home_ambiguous'):
        from_mijia([home,dict(home,home_id='two')],layout(),'cn')
    home['rooms'][1]['devices']=['100']
    with pytest.raises(ValueError,match='room_ambiguous'):
        from_mijia([home],layout(),'cn')


def test_duplicate_labels_never_choose_first_room():
    house=layout();house.rooms[1].name=house.rooms[0].name
    assert project([{'id':'x','room':'房间一','bindings':[]}],house,None)[0]['room_id'] is None


def test_saved_explicit_device_position_takes_precedence_without_moving_it():
    house=layout();house.devices=[Device(
        id='cn_100',room_id='r2',position=[.7,.2])]
    member=Membership(floor_id='simulated-floor',home_id='synthetic-home',
                      device_rooms={'xiaomi-identity:cn_100':'r1'})
    result=project([{'id':'opaque','room':'Room','bindings':[{'source':'xiaomi-identity','external_id':'cn_100'}]}],house,member)
    assert result[0]['room_id']=='r2' and house.devices[0].position==[.7,.2]
