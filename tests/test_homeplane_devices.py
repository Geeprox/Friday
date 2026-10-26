"""Synthetic physical-device projection; not household hardware certification."""
from homeplane.contracts import Binding, Capability, Entity
from homeplane.devices import project_ha


def entity(ident, kind="light", *, device="simulated-registry", room="Simulated Room"):
    return Entity(id=ident, name="Simulated same name", kind=kind, room=room, physical_source_id=ident,
                  bindings=[Binding(source="ha-device-registry", external_id=device)],
                  capabilities=[Capability(name=kind + ".state", access="read", support="supported")], simulated=True)


def test_group_functions_keep_independent_measurement_sources_and_raw_bindings():
    functions = [entity("simulated-power"), entity("simulated-battery", "sensor"), entity("simulated-temperature", "sensor")]
    devices = project_ha(functions, {"simulated-registry":{"name":"Simulated lamp", "identifiers":[["xiaomi_home","cn_123456789"]]}}, {}, "simulated-instance")
    assert len(devices) == 1 and devices[0].identity_basis == "xiaomi_identifier"
    assert set(devices[0].function_ids) == {e.id for e in functions}
    assert len({e.physical_source_id for e in functions}) == 3
    assert devices[0].name == "Simulated lamp"
    assert len(devices[0].capabilities) == 2


def test_same_names_different_devices_not_merged_and_identifier_survives_rename():
    first = entity("simulated-a", device="simulated-a")
    second = entity("simulated-b", device="simulated-b")
    registry = {"simulated-a":{"identifiers":[["xiaomi_home","cn_123"]]}, "simulated-b":{}}
    devices = project_ha([first, second], registry, {}, "simulated-instance")
    assert len(devices) == 2
    before = next(d.id for d in devices if "simulated-a" in d.function_ids)
    first.name, first.room = "Renamed simulated device", "Another simulated room"
    registry["simulated-a"]["name_by_user"] = "Renamed simulated device"
    after = project_ha([first], registry, {}, "simulated-instance")
    renamed = next(d for d in after if first.id in d.function_ids)
    assert renamed.id == before and renamed.name == first.name
    other_instance = project_ha([first], registry, {}, "simulated-other-instance")
    assert next(d.id for d in other_instance if first.id in d.function_ids) == before


def test_opaque_identifiers_are_instance_scoped_and_unbound_not_guessed():
    e = entity("simulated-function")
    registry = {"simulated-registry":{"identifiers":[["xiaomi_home","unknown-format"]]}}
    a = project_ha([e], registry, {}, "simulated-one")[0]
    b = project_ha([e], registry, {}, "simulated-two")[0]
    assert a.id != b.id and a.identity_basis == "ha_device_registry"
    assert project_ha([e], {}, {}, "simulated-one") == []


def test_conflicting_rooms_retained_and_service_not_physical_card():
    functions = [entity("simulated-a", "sun"), entity("simulated-b", "sun", room="Simulated Other")]
    d = project_ha(functions, {"simulated-registry":{"entry_type":"service", "area_id":"simulated-area"}},
                   {"simulated-area":{"name":"Simulated Device Area"}}, "simulated-one")[0]
    assert d.category == "service" and d.room == "Simulated Device Area"
    assert any("disagree" in reason for reason in d.limitations)
    assert functions[1].room == "Simulated Other"


def test_registered_device_without_exposed_functions_is_visible_but_not_online():
    d = project_ha([], {"simulated-device":{"name":"Simulated no-state lamp","identifiers":[["xiaomi_home","cn_999"]]}}, {}, "simulated")[0]
    assert d.function_ids == [] and d.capabilities == []
    assert any("not availability" in reason for reason in d.limitations)


def test_same_upstream_device_identity_has_one_card_but_keeps_all_bindings():
    a, b = entity("simulated-a", device="a"), entity("simulated-b", device="b")
    registry = {key:{"identifiers":[["xiaomi_home","cn_888"]]} for key in ["a","b"]}
    devices = project_ha([a,b], registry, {}, "simulated")
    assert len(devices) == 1 and devices[0].function_ids == [a.id,b.id]
    assert len([binding for binding in devices[0].bindings if binding.source == "ha-device-registry"]) == 2
    assert a.physical_source_id != b.physical_source_id
