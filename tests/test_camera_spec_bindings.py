"""Camera references come from semantic imported specs, including ambiguity fences."""
import copy

import pytest

from homeplane.embodied.miot_spec import camera_properties, camera_property


def camera():
    def prop(i, name):
        return {'iid': i, 'type': f'urn:miot-spec-v2:property:{name}:synthetic',
                'access': ['read', 'write']}
    return {'spec': {'services': [
        {'iid': 31, 'type': 'urn:miot-spec-v2:service:camera-control:synthetic',
         'properties': [prop(9, 'on'), prop(19, 'human-tracking')]},
        {'iid': 43, 'type': 'urn:miot-spec-v2:service:ai-detection:synthetic',
         'properties': [prop(7, 'pet-tracking'), prop(11, 'human-close-up')]},
        {'iid': 57, 'type': 'urn:miot-spec-v2:service:camera-fav-area:synthetic',
         'properties': [prop(5, 'fav-area'), prop(13, 'active-fav-area')]},
        {'iid': 61, 'type': 'urn:miot-spec-v2:service:cruise:synthetic',
         'properties': [prop(23, 'cruise-switch')]},
        {'iid': 99, 'type': 'urn:miot-spec-v2:service:light:synthetic',
         'properties': [prop(1, 'on')]},
    ]}}


def test_camera_refs_use_semantic_service_and_imported_iids():
    refs = camera_properties(camera())
    assert refs == {'camera_power': (31, 9), 'human_tracking': (31, 19),
                    'pet_tracking': (43, 7), 'human_close_up': (43, 11),
                    'presets': (57, 5), 'active_preset': (57, 13), 'cruise': (61, 23)}


@pytest.mark.parametrize('fence', ['missing', 'duplicate_service', 'duplicate_property', 'write_missing', 'invalid_iid'])
def test_unknown_or_ambiguous_camera_capability_fails_closed(fence):
    selected = camera()
    service = selected['spec']['services'][0]
    if fence == 'missing':
        service['properties'].pop(0)
    elif fence == 'duplicate_service':
        selected['spec']['services'].append(copy.deepcopy(service))
    elif fence == 'duplicate_property':
        service['properties'].append(copy.deepcopy(service['properties'][0]))
    elif fence == 'write_missing':
        service['properties'][0]['access'] = ['read']
    else:
        service['properties'][0]['iid'] = True
    with pytest.raises(ValueError, match='miot_(capability_unavailable|spec_invalid)'):
        camera_properties(selected)


def test_read_only_shutter_check_needs_only_the_camera_power_capability():
    selected = camera()
    selected['spec']['services'] = selected['spec']['services'][:1]
    selected['spec']['services'][0]['properties'] = selected['spec']['services'][0]['properties'][:1]
    selected['spec']['services'][0]['properties'][0]['access'] = ['read']
    assert camera_property(selected, 'camera_power', 'read') == (31, 9)


@pytest.mark.parametrize("fence", ["property_iid_collision", "service_iid_collision", "access_string", "access_mapping"])
def test_malformed_spec_cannot_alias_controls_or_grant_permissions(fence):
    selected = camera()
    service = selected["spec"]["services"][0]
    if fence == "property_iid_collision":
        service["properties"][1]["iid"] = service["properties"][0]["iid"]
    elif fence == "service_iid_collision":
        selected["spec"]["services"][1]["iid"] = service["iid"]
    elif fence == "access_string":
        service["properties"][0]["access"] = "read,write"
    else:
        service["properties"][0]["access"] = {"read": False, "write": False}
    with pytest.raises(ValueError, match="miot_spec_invalid"):
        camera_properties(selected)
