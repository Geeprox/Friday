"""Actual-layout import boundaries, geometry, placement preservation and CAS."""

import importlib.util
import json
import math
import os
import struct
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from homeplane.home_layout import Layout, LayoutError, LayoutStore, layout_from_baked, normalize_layout


def fixed(*, device=None):
    return Layout(
        source={"kind": "friday_layout"},
        rooms=[
            {
                "id": "room_a",
                "name": "客厅",
                "polygon": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
            }
        ],
        devices=[device] if device else [],
    )


def bitmap(rows, *, bpp=32, top_down=False):
    height, width = len(rows), len(rows[0])
    stride = ((width * bpp + 31) // 32) * 4
    pixels = bytearray()
    for row in rows if top_down else reversed(rows):
        for red in row:
            pixels += bytes((255, 105, red)) + (b"\xff" if bpp == 32 else b"")
        pixels += b"\0" * (stride - width * (bpp // 8))
    return (
        struct.pack("<2sIHHI", b"BM", 54 + len(pixels), 0, 0, 54)
        + struct.pack(
            "<IiiHHIIiiII", 40, width, -height if top_down else height, 1, bpp, 0, len(pixels), 0, 0, 0, 0
        )
        + pixels
    )


def baked(rows, names=None, **options):
    return layout_from_baked(
        {
            "floorId": "actual-floor",
            "floorSha1": "a" * 40,
            "floorName": "一楼",
            "textureWidth": len(rows[0]),
            "textureHeight": len(rows),
            "roomGrayMap": {"room_a": 1 / 255},
        },
        bitmap(rows, **options),
        names or {"rooms": {"room_a": "客厅"}},
    )


@pytest.mark.parametrize("bpp,top_down", [(24, False), (32, False), (32, True)])
def test_bitmap_recovers_concave_actual_cells_without_rectangular_guess(bpp, top_down):
    layout = baked([[1, 1, 0, 0], [1, 1, 0, 0], [1, 1, 1, 1], [0, 0, 0, 0]], bpp=bpp, top_down=top_down)
    ring = layout.rooms[0].polygon
    assert len(ring) == 6
    assert set(map(tuple, ring)) == {(0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (1.0, 0.5), (1.0, 0.75), (0.0, 0.75)}
    assert layout.source["coordinate_basis"] == "normalized_xz_relative"
    assert layout.source["units"] == "relative"
    assert layout.devices == []


def test_bitmap_hole_is_retained_and_is_not_usable_as_a_device_position():
    layout = baked([[1, 1, 1], [1, 0, 1], [1, 1, 1]])
    assert len(layout.rooms[0].holes) == 1
    value = layout.model_dump()
    value["devices"] = [{"id": "did_1", "room_id": "room_a", "position": [0.5, 0.5]}]
    with pytest.raises(ValidationError, match="outside_room"):
        Layout.model_validate(value)


def test_reimport_preserves_placed_points_by_id_and_tracks_unplaced_devices(tmp_path):
    store = LayoutStore(tmp_path / "layout.json")
    initial = store.apply(fixed(device={"id": "did_1", "room_id": "room_a", "position": [0.2, 0.3]}), 0)
    incoming = fixed().model_dump()
    incoming["unplaced_devices"] = [
        {"id": "did_1", "room_id": "room_a"},
        {"id": "did_2", "room_id": "room_a"},
    ]
    preview = store.preview(incoming)
    assert preview["preserved_device_ids"] == ["did_1"]
    assert preview["unplaced_devices"] == [{"id": "did_2", "room_id": "room_a", "reason": "missing_position"}]
    second = store.apply(incoming, initial.revision)
    assert second.devices[0].position == [0.2, 0.3]
    assert second.unplaced_devices[0].id == "did_2"
    assert store.current() == second
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    assert os.stat(store.previous_path).st_mode & 0o777 == 0o600
    assert Layout.model_validate_json(store.previous_path.read_text()) == initial


def test_changed_imported_point_has_clear_conflict_and_cannot_silently_overwrite(tmp_path):
    store = LayoutStore(tmp_path / "layout.json")
    old = store.apply(fixed(device={"id": "did_1", "room_id": "room_a", "position": [0.2, 0.3]}), 0)
    changed = fixed(device={"id": "did_1", "room_id": "room_a", "position": [0.8, 0.9]})
    assert store.preview(changed)["conflicts"][0]["previous"]["position"] == [0.2, 0.3]
    with pytest.raises(LayoutError, match="layout_import_conflict") as error:
        store.apply(changed, old.revision)
    assert error.value.details["conflicts"][0]["incoming"]["position"] == [0.8, 0.9]
    assert store.current() == old
    saved = store.apply(changed, old.revision, accept_device_changes=True)
    assert saved.devices[0].position == [0.8, 0.9]


def test_explicit_user_move_and_restore_are_revision_fenced(tmp_path):
    store = LayoutStore(tmp_path / "layout.json")
    store.apply(fixed(), 0)
    moved = store.replace_devices([{"id": "did_1", "room_id": "room_a", "position": [0.7, 0.4]}], 1)
    assert moved.revision == 2
    with pytest.raises(LayoutError, match="revision_conflict"):
        store.replace_devices([], 1)
    restored = store.restore(2)
    assert restored.revision == 3 and restored.devices == []
    assert store.current() == restored


def test_omitted_explicit_placement_is_unplaced_not_fabricated_at_centroid(tmp_path):
    store = LayoutStore(tmp_path / "layout.json")
    store.apply(fixed(device={"id": "did_1", "room_id": "room_a", "position": [0.1, 0.2]}), 0)
    result = store.replace_devices([], 1)
    assert result.devices == []
    assert result.unplaced_devices[0].id == "did_1"


def test_lost_room_or_shrunk_geometry_cannot_discard_old_point(tmp_path):
    store = LayoutStore(tmp_path / "layout.json")
    old = store.apply(fixed(device={"id": "did_1", "room_id": "room_a", "position": [0.8, 0.9]}), 0)
    value = fixed().model_dump()
    value["rooms"][0]["polygon"] = [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]]
    assert store.preview(value)["conflicts"][0]["kind"] == "device_room_geometry_changed"
    with pytest.raises(LayoutError, match="import_conflict"):
        store.apply(value, 1, accept_device_changes=True)
    assert store.current() == old


def test_compare_and_swap_across_independent_store_instances(tmp_path):
    path = tmp_path / "layout.json"
    LayoutStore(path).apply(fixed(), 0)
    barrier = threading.Barrier(2)
    results = []

    def update():
        barrier.wait()
        try:
            results.append(LayoutStore(path).apply(fixed(), 1).revision)
        except LayoutError as error:
            results.append(error.code)

    threads = [threading.Thread(target=update) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(map(str, results)) == ["2", "layout_revision_conflict"]


@pytest.mark.parametrize(
    "position",
    [[math.nan, 0.5], [math.inf, 0.5], [-0.1, 0.5], [1.1, 0.5], [True, 0.5], [0.5], [0.5, 0.5, 0.5]],
)
def test_non_finite_out_of_bounds_and_malformed_positions_are_rejected(position):
    with pytest.raises(ValidationError):
        fixed(device={"id": "did_1", "room_id": "room_a", "position": position})


@pytest.mark.parametrize(
    "polygon",
    [
        [[0.0, 0.0], [1.0, 1.0], [1.0, 0.0], [0.0, 1.0]],
        [[0.0, 0.0], [0.5, 0.5], [1.0, 1.0]],
        [[0.0, 0.0], [1.0, 0.0], [1.0, 0.0], [0.0, 1.0]],
    ],
)
def test_self_intersecting_degenerate_and_duplicate_polygons_are_rejected(polygon):
    value = fixed().model_dump()
    value["rooms"][0]["polygon"] = polygon
    with pytest.raises(ValidationError):
        normalize_layout(value)


def test_metadata_cannot_hide_nonfinite_unbounded_or_unknown_fields():
    for extra in ({"normalization": {"span": math.inf}}, {"floor_name": "x" * 161}, {"token": "secret"}):
        value = fixed().model_dump()
        value["source"].update(extra)
        with pytest.raises((ValidationError, LayoutError)):
            normalize_layout(value)


def world_room(identity, name, rings):
    return {
        "id": identity,
        "name": name,
        "worldPositions": [[{"x": x, "y": 0, "z": z} for x, z in ring] for ring in rings],
    }


def test_floormap_normalizes_all_rooms_uniformly_and_uses_actual_device_coordinates():
    source = {
        "version": 3,
        "floors": [
            {
                "id": "floor1",
                "floorName": "一楼",
                "Rooms": [
                    world_room(
                        "room_a", "客厅", [[[-10.0, 20.0], [10.0, 20.0], [10.0, 30.0], [-10.0, 30.0]]]
                    ),
                ],
                "Devices": [
                    {
                        "did": "physical-123",
                        "name": "不能按这名字猜房间",
                        "hostId": "not-a-room",
                        "position": {"x": 0.0, "y": 999.0, "z": 25.0},
                    },
                    {"did": "unplaced-456"},
                ],
            }
        ],
    }
    layout = normalize_layout(source)
    assert layout.rooms[0].polygon == [[0.0, 0.25], [1.0, 0.25], [1.0, 0.75], [0.0, 0.75]]
    assert layout.devices[0].id == "physical-123"
    assert layout.devices[0].room_id == "room_a"
    assert layout.devices[0].position == [0.5, 0.5]
    assert layout.unplaced_devices[0].id == "unplaced-456"
    assert layout.source["normalization"]["span"] == 20.0


def test_floormap_ring_containment_preserves_holes_and_disconnected_parts():
    rings = [
        [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]],
        [[1.0, 1.0], [3.0, 1.0], [3.0, 3.0], [1.0, 3.0]],
        [[5.0, 0.0], [6.0, 0.0], [6.0, 1.0], [5.0, 1.0]],
    ]
    value = {"floors": [{"Rooms": [world_room("room_a", "客厅", list(reversed(rings)))], "Devices": []}]}
    layout = normalize_layout(value)
    assert len(layout.rooms[0].holes) == 1
    assert len(layout.rooms[0].parts) == 1
    assert not layout.rooms[0].contains([2 / 6, 3 / 6])


def test_shared_room_boundary_is_ambiguous_and_does_not_guess():
    value = {
        "floors": [
            {
                "Rooms": [
                    world_room("room_a", "同名", [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]]),
                    world_room("room_b", "同名", [[[1.0, 0.0], [2.0, 0.0], [2.0, 1.0], [1.0, 1.0]]]),
                ],
                "Devices": [{"did": "did_1", "position": {"x": 1.0, "z": 0.5}}],
            }
        ]
    }
    layout = normalize_layout(value)
    assert not layout.devices
    assert layout.unplaced_devices[0].reason == "ambiguous_room"


def test_malformed_bitmap_missing_names_and_unknown_labels_are_rejected():
    for data in (b"bad", bitmap([[1]])[:-1]):
        with pytest.raises(LayoutError):
            layout_from_baked({"roomGrayMap": {"room_a": 1 / 255}}, data, {"room_a": "客厅"})
    with pytest.raises(LayoutError, match="name_missing"):
        baked([[1]], names={"other": "客厅"})
    with pytest.raises(LayoutError, match="unmapped_bitmap"):
        baked([[1, 2]])


def test_symlink_store_is_refused_and_read_does_not_create_state(tmp_path):
    store = LayoutStore(tmp_path / "new" / "layout.json")
    assert store.current() is None and not store.path.parent.exists()
    real = tmp_path / "real.json"
    real.write_text(fixed().model_dump_json())
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(LayoutError, match="symlink_refused"):
        LayoutStore(link).apply(fixed(), 0)


def test_cli_repeat_import_outputs_fixed_json_and_retains_placement(tmp_path, capsys):
    script = Path(__file__).resolve().parents[1] / "scripts/embodied/import_home_layout.py"
    spec = importlib.util.spec_from_file_location("layout_import_script", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = {"floorId": "floor", "textureWidth": 2, "textureHeight": 2, "roomGrayMap": {"room_a": 1 / 255}}
    floor, texture, names, output = [
        tmp_path / name for name in ("floor.json", "texture.bmp", "names.json", "layout.json")
    ]
    floor.write_text(json.dumps(data))
    texture.write_bytes(bitmap([[1, 1], [1, 1]]))
    names.write_text(json.dumps({"room_a": "客厅"}))
    args = [
        "--baked-json",
        str(floor),
        "--texture",
        str(texture),
        "--room-names",
        str(names),
        "--output",
        str(output),
    ]
    assert module.main(args) == 0
    placed = LayoutStore(output).replace_devices(
        [{"id": "did_1", "room_id": "room_a", "position": [0.2, 0.4]}], 1
    )
    assert placed.revision == 2
    assert module.main(args) == 0
    saved = LayoutStore(output).current()
    assert saved.schema_version == 1 and saved.revision == 3 and saved.devices[0].position == [0.2, 0.4]
    assert "did_1" in json.loads(capsys.readouterr().out.splitlines()[-1])["preserved_device_ids"]


def test_android_reader_refuses_paths_outside_exact_mijia_bake_directory(monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/embodied/import_home_layout.py"
    spec = importlib.util.spec_from_file_location("layout_import_scope_script", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/adb")
    for path in (
        "/data/data/com.xiaomi.smarthome",
        module.BAKE_ROOT + "/../../memory",
        module.BAKE_ROOT + "/not-a-uuid",
    ):
        with pytest.raises(LayoutError, match="directory_scope"):
            module.read_android_bake("5f214d2d", path)


def test_real_mijia_floor_bake_matches_previously_extracted_exact_boundaries():
    base = Path(__file__).resolve().parents[1] / ".artifacts/product-refactor/model-analysis"
    source = base / "raw/floor_data.json"
    if not source.exists():
        pytest.skip("Private real-home evidence is not included in the source distribution")
    floor = json.loads(source.read_text(encoding="utf-8-sig"))
    layout = layout_from_baked(
        floor, (base / "raw/texture.bmp").read_bytes(), json.loads((base / "room-names.json").read_text())
    )
    previous = json.loads((base / "actual-room-footprints.json").read_text())
    assert len(layout.rooms) == 6
    exact = {room["roomId"]: room for room in previous["rooms"]}
    for room in layout.rooms:
        assert room.name == exact[room.id]["name"]
        assert set(map(tuple, room.polygon)) == set(map(tuple, exact[room.id]["normalizedBoundaryLoops"][0]))
    assert not layout.devices
