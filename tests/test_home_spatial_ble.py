from homeplane.devices import project_ha, xiaomi_identifier


def test_ble_identity_is_reversible_and_region_scoped():
    assert xiaomi_identifier('cn_blt_3_synthetic123')=='cn_blt.3.synthetic123'
    assert xiaomi_identifier('cn_blt.3.synthetic123')=='cn_blt.3.synthetic123'
    assert xiaomi_identifier('de_blt_3_synthetic123')=='de_blt.3.synthetic123'
    assert xiaomi_identifier('cn_123456789')=='cn_123456789'
    assert xiaomi_identifier('untrusted_device_label') is None
    assert xiaomi_identifier('cn_blt_3_https://invalid') is None


def test_adding_ble_room_identity_preserves_existing_device_id():
    old=project_ha([],{'registry':{'name':'温湿度计','identifiers':[]}}, {},'synthetic-instance')[0]
    current=project_ha([],{'registry':{'name':'温湿度计','identifiers':[
        ['xiaomi_home','cn_blt_3_synthetic123']]}}, {},'synthetic-instance')[0]
    assert current.id==old.id
    assert any(b.source=='xiaomi-identity' and b.external_id=='cn_blt.3.synthetic123'
               for b in current.bindings)
