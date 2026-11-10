"""Only synthetic registry rows; no config, camera, microphone or model access."""
import ast
from copy import deepcopy
from pathlib import Path

from homeplane.embodied.camera_provenance import (
    bind_capture_packet, camera_source_context, configured_camera_sources,
)

CAMERA = {'id': 'cam_1111111111', 'name': 'SYNTHETIC camera', 'region': 'cn',
          'device': {'did': 'SYNTHETIC-device'}}


def test_identity_is_stable_across_labels_and_changes_with_physical_source():
    original = camera_source_context(CAMERA)
    assert len(original) == 64
    assert camera_source_context(dict(CAMERA, name='renamed', room='another room')) == original
    for replacement in (dict(CAMERA, id='cam_2222222222'), dict(CAMERA, region='us'),
                        dict(CAMERA, device={'did': 'SYNTHETIC-other-device'})):
        assert camera_source_context(replacement) != original
    for invalid in (None, {}, dict(CAMERA, device={}), dict(CAMERA, region='../cn')):
        assert camera_source_context(invalid) is None


def test_actual_capture_encoder_stamps_its_selected_source_without_registry_reread():
    tree = ast.parse(Path('homeplane/embodied/capture_worker.py').read_text())
    encoded = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'encoded')
    selected = deepcopy(CAMERA)
    emitted = []
    scope = dict(selected=selected, bind_capture_packet=bind_capture_packet, owner_camera=[],capture_session='a'*32,
                 rate=lambda _: 0, raw_times=[], decode_times=[], emit=emitted.append)
    exec(compile(ast.Module(body=[encoded], type_ignores=[]), '<actual capture callback>', 'exec'), scope)
    scope['encoded']({'type': 'frame', 'received_at': 1, 'jpeg': 'SYNTHETIC'})
    packet = emitted[0]
    assert packet['camera_source_context'] == camera_source_context(selected)
    assert packet['camera_source_name'] == CAMERA['name']
    assert packet['capture_session'] == 'a'*32
    assert selected['device']['did'] not in repr(packet)
    assert packet['jpeg'] == 'SYNTHETIC'


def test_metadata_catalog_rejects_duplicates_and_synthetic_declarations(monkeypatch):
    from homeplane.embodied import paths
    config = {'cameras': [deepcopy(CAMERA)]}
    monkeypatch.setattr(paths, 'configuration', lambda: config)
    assert configured_camera_sources() == [dict(id=CAMERA['id'], name=CAMERA['name'],
                                              source_context=camera_source_context(CAMERA))]
    config['cameras'].append(dict(CAMERA, id='cam_2222222222'))
    assert configured_camera_sources() == []  # A second alias is not another physical source.
    config['cameras'] = [deepcopy(CAMERA), dict(CAMERA, device={'did': 'SYNTHETIC-other'})]
    assert configured_camera_sources() == []  # Duplicate registered ID.
    config['cameras'] = [deepcopy(CAMERA)]
    config['simulated'] = True
    assert configured_camera_sources() == []
