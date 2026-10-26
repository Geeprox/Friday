"""Persistent, revision-fenced home geometry imported from actual Mi Home data.

Coordinates are a uniformly normalized XZ plane in relative units. A missing
device position remains missing. Names never establish device or room identity.
The module has no service, cloud, image-library, or device-control dependency.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import stat
import struct
import tempfile
import threading
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_BYTES = 2 * 1024 * 1024
MAX_ROOMS, MAX_DEVICES, MAX_VERTICES = 64, 1024, 8192
EPSILON = 1e-10
Ref = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.:-]{1,128}$")]
Coordinate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Point = Annotated[list[Coordinate], Field(min_length=2, max_length=2)]
Ring = Annotated[list[Point], Field(min_length=3, max_length=512)]


class LayoutError(ValueError):
    def __init__(self, code: str, details: dict | None = None):
        self.code, self.details = code, details or {}
        super().__init__(code)


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(point, a, b):
    return abs(_cross(a, b, point)) <= EPSILON and all(
        min(a[i], b[i]) - EPSILON <= point[i] <= max(a[i], b[i]) + EPSILON for i in (0, 1)
    )


def _intersects(a, b, c, d):
    ab_c, ab_d, cd_a, cd_b = _cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b)
    if ((ab_c > EPSILON and ab_d < -EPSILON) or (ab_c < -EPSILON and ab_d > EPSILON)) and (
        (cd_a > EPSILON and cd_b < -EPSILON) or (cd_a < -EPSILON and cd_b > EPSILON)
    ):
        return True
    return any((_on_segment(p, x, y) for p, x, y in ((c, a, b), (d, a, b), (a, c, d), (b, c, d))))


def _area(ring):
    return (
        sum(
            p[0] * ring[(i + 1) % len(ring)][1] - ring[(i + 1) % len(ring)][0] * p[1]
            for i, p in enumerate(ring)
        )
        / 2
    )


def _inside(point, ring, *, boundary=True):
    result = False
    x, y = point
    for i, a in enumerate(ring):
        b = ring[(i + 1) % len(ring)]
        if _on_segment(point, a, b):
            return boundary
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            result = not result
    return result


def _clean_ring(value):
    if len(value) > 3 and value[0] == value[-1]:
        value = value[:-1]
    if len({tuple(p) for p in value}) != len(value) or abs(_area(value)) <= EPSILON:
        raise ValueError("layout_degenerate_polygon")
    for i, a in enumerate(value):
        b = value[(i + 1) % len(value)]
        for j in range(i + 1, len(value)):
            if j == i + 1 or (i == 0 and j == len(value) - 1):
                continue
            if _intersects(a, b, value[j], value[(j + 1) % len(value)]):
                raise ValueError("layout_self_intersecting_polygon")
    return value


def _rings_intersect(first, second):
    return any(
        _intersects(a, first[(i + 1) % len(first)], b, second[(j + 1) % len(second)])
        for i, a in enumerate(first)
        for j, b in enumerate(second)
    )


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Region(Contract):
    polygon: Ring
    holes: list[Ring] = Field(default_factory=list, max_length=16)

    @field_validator("polygon")
    @classmethod
    def polygon_valid(cls, value):
        return _clean_ring(value)

    @field_validator("holes")
    @classmethod
    def holes_valid(cls, values):
        return [_clean_ring(value) for value in values]

    @model_validator(mode="after")
    def validate_holes(self):
        for index, hole in enumerate(self.holes):
            if not all(_inside(p, self.polygon, boundary=False) for p in hole) or _rings_intersect(
                hole, self.polygon
            ):
                raise ValueError("layout_hole_outside_polygon")
            for other in self.holes[:index]:
                if _rings_intersect(hole, other) or _inside(hole[0], other) or _inside(other[0], hole):
                    raise ValueError("layout_overlapping_holes")
        return self

    def contains(self, point, *, boundary=True):
        return _inside(point, self.polygon, boundary=boundary) and not any(
            _inside(point, hole, boundary=True) for hole in self.holes
        )


class Room(Region):
    id: Ref
    name: str = Field(min_length=1, max_length=80)
    parts: list[Region] = Field(default_factory=list, max_length=16)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("layout_invalid_room_name")
        return value

    @model_validator(mode="after")
    def disjoint_parts(self):
        regions = [self, *self.parts]
        for i, region in enumerate(regions):
            for other in regions[:i]:
                if _rings_intersect(region.polygon, other.polygon) or (
                    Region.contains(region, other.polygon[0], boundary=False)
                    or Region.contains(other, region.polygon[0], boundary=False)
                ):
                    raise ValueError("layout_overlapping_room_parts")
        return self

    def contains(self, point, *, boundary=True):
        return super().contains(point, boundary=boundary) or any(
            part.contains(point, boundary=boundary) for part in self.parts
        )


class Device(Contract):
    id: Ref
    room_id: Ref
    position: Point


class UnplacedDevice(Contract):
    id: Ref
    room_id: Ref | None = None
    reason: Literal["missing_position", "unknown_room", "ambiguous_room", "outside_room"] = "missing_position"


_SOURCE_KEYS = {
    "kind",
    "floor_id",
    "floor_sha1",
    "floor_name",
    "coordinate_basis",
    "units",
    "content_sha256",
    "texture_sha256",
    "resolution",
    "normalization",
    "geometry",
    "missing",
}


class Layout(Contract):
    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0, le=2**53 - 1)
    source: dict[str, Any]
    rooms: list[Room] = Field(min_length=1, max_length=MAX_ROOMS)
    devices: list[Device] = Field(default_factory=list, max_length=MAX_DEVICES)
    unplaced_devices: list[UnplacedDevice] = Field(default_factory=list, max_length=MAX_DEVICES)

    @model_validator(mode="before")
    @classmethod
    def bound_vertices_before_geometry_work(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get("rooms"), list):
            return value
        total = 0
        for room in value["rooms"]:
            if isinstance(room, Room):
                total += sum(
                    len(region.polygon) + sum(map(len, region.holes)) for region in [room, *room.parts]
                )
            elif isinstance(room, dict):
                for region in (
                    [room, *room.get("parts", [])] if isinstance(room.get("parts", []), list) else [room]
                ):
                    if isinstance(region, dict):
                        rings = [region.get("polygon", [])]
                        holes = region.get("holes", [])
                        if isinstance(holes, list):
                            rings.extend(holes)
                        total += sum(len(ring) for ring in rings if isinstance(ring, list))
            if total > MAX_VERTICES:
                raise ValueError("layout_vertex_budget")
        return value

    @field_validator("source")
    @classmethod
    def bounded_source(cls, value):
        if set(value) - _SOURCE_KEYS or len(value) > 16:
            raise ValueError("layout_invalid_source_fields")
        value = dict(value)
        if value.get("kind") not in {"mijia_baked", "mijia_floormap_v3", "friday_layout"}:
            raise ValueError("layout_invalid_source_kind")
        value.setdefault("coordinate_basis", "normalized_xz_relative")
        value.setdefault("units", "relative")
        if value["coordinate_basis"] != "normalized_xz_relative" or value["units"] != "relative":
            raise ValueError("layout_invalid_coordinate_basis")

        def visit(item, depth=0):
            if depth > 3:
                raise ValueError("layout_source_budget")
            if item is None or type(item) in {bool, int}:
                return
            if isinstance(item, float):
                if not math.isfinite(item):
                    raise ValueError("layout_non_finite_source")
            elif isinstance(item, str):
                if len(item) > 160 or any(ord(c) < 32 for c in item):
                    raise ValueError("layout_source_budget")
            elif isinstance(item, list) and len(item) <= 32:
                for child in item:
                    visit(child, depth + 1)
            elif isinstance(item, dict) and len(item) <= (16 if depth == 0 else 8):
                for key, child in item.items():
                    if not isinstance(key, str) or len(key) > 40:
                        raise ValueError("layout_source_budget")
                    visit(child, depth + 1)
            else:
                raise ValueError("layout_source_budget")

        visit(value)
        if len(json.dumps(value, allow_nan=False).encode()) > 4096:
            raise ValueError("layout_source_budget")
        for field, length in (("floor_sha1", 40), ("content_sha256", 64), ("texture_sha256", 64)):
            digest = value.get(field)
            if digest is not None and (
                not isinstance(digest, str)
                or len(digest) != length
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("layout_invalid_source_digest")
        return value

    @model_validator(mode="after")
    def unique_references_and_positions(self):
        rooms = {room.id: room for room in self.rooms}
        if len(rooms) != len(self.rooms):
            raise ValueError("layout_duplicate_room_id")
        devices = [device.id for device in self.devices]
        unplaced = [device.id for device in self.unplaced_devices]
        if len(set(devices + unplaced)) != len(devices) + len(unplaced):
            raise ValueError("layout_duplicate_device_id")
        vertices = sum(
            len(region.polygon) + sum(map(len, region.holes))
            for room in self.rooms
            for region in [room, *room.parts]
        )
        if vertices > MAX_VERTICES:
            raise ValueError("layout_vertex_budget")
        for device in self.devices:
            if device.room_id not in rooms:
                raise ValueError("layout_device_room_missing")
            if not rooms[device.room_id].contains(device.position):
                raise ValueError("layout_device_outside_room")
        return self


def _regions_from_rings(rings):
    """Containment determines holes; array order never determines their meaning."""
    cleaned = [_clean_ring(ring) for ring in rings]
    for i, ring in enumerate(cleaned):
        for other in cleaned[:i]:
            if _rings_intersect(ring, other):
                raise LayoutError("layout_intersecting_rings")
    parents = []
    for i, ring in enumerate(cleaned):
        candidates = [
            (abs(_area(other)), j)
            for j, other in enumerate(cleaned)
            if i != j and _inside(ring[0], other, boundary=False)
        ]
        parents.append(min(candidates)[1] if candidates else None)

    def depth(index):
        result, seen = 0, set()
        while parents[index] is not None:
            if index in seen:
                raise LayoutError("layout_invalid_ring_containment")
            seen.add(index)
            index = parents[index]
            result += 1
        return result

    regions = []
    for i, ring in enumerate(cleaned):
        if depth(i) % 2 == 0:
            regions.append(
                {"polygon": ring, "holes": [other for j, other in enumerate(cleaned) if parents[j] == i]}
            )
    return sorted(regions, key=lambda region: abs(_area(region["polygon"])), reverse=True)


def _finite_number(value):
    if type(value) not in (float, int) or abs(value) > 1e12 or not math.isfinite(value):
        raise LayoutError("layout_invalid_coordinate")
    return float(value)


def _world_point(value):
    if isinstance(value, dict) and "x" in value and "z" in value:
        return [_finite_number(value["x"]), _finite_number(value["z"])]
    if isinstance(value, list) and len(value) == 3:
        return [_finite_number(value[0]), _finite_number(value[2])]
    raise LayoutError("layout_invalid_world_position")


def _json_budget(value):
    try:
        if len(json.dumps(value, allow_nan=False).encode()) > MAX_BYTES:
            raise LayoutError("layout_input_budget")
    except (TypeError, ValueError, RecursionError) as error:
        if isinstance(error, LayoutError):
            raise
        raise LayoutError("layout_invalid_json") from error


def normalize_layout(value: dict | Layout) -> Layout:
    """Accept a fixed Friday layout or a complete Mi Home FloorMapV3 object.

    FloorMapV3 imports the first floor. Vector3 x/z values share one uniform
    normalization. Devices bind by an explicit valid room ID, or by a unique
    geometric containment result; neither device nor room names are compared.
    """
    if isinstance(value, Layout):
        return Layout.model_validate(value.model_dump())
    if not isinstance(value, dict):
        raise LayoutError("layout_object_required")
    _json_budget(value)
    if "schema_version" in value or "source" in value:
        return Layout.model_validate(value)
    structure = value.get("houseStructure", value)
    if not isinstance(structure, dict):
        raise LayoutError("layout_structure_required")
    floors = structure.get("floors")
    if not isinstance(floors, list) or not 1 <= len(floors) <= MAX_ROOMS:
        raise LayoutError("layout_floors_required")
    floor = floors[0]
    if not isinstance(floor, dict):
        raise LayoutError("layout_invalid_floor")
    raw_rooms = floor.get("Rooms", floor.get("rooms"))
    if not isinstance(raw_rooms, list) or not 1 <= len(raw_rooms) <= MAX_ROOMS:
        raise LayoutError("layout_rooms_required")
    room_rows, all_points = [], []
    for raw in raw_rooms:
        if not isinstance(raw, dict):
            raise LayoutError("layout_invalid_room")
        rings = raw.get("worldPositions")
        if not isinstance(rings, list) or not 1 <= len(rings) <= 32:
            raise LayoutError("layout_room_polygons_required")
        if isinstance(rings[0], dict) or (
            isinstance(rings[0], list)
            and len(rings[0]) == 3
            and all(type(v) in (int, float) for v in rings[0])
        ):
            rings = [rings]  # Earlier single-ring exports are also admitted.
        parsed = []
        for ring in rings:
            if not isinstance(ring, list) or not 3 <= len(ring) <= 512:
                raise LayoutError("layout_invalid_polygon")
            points = [_world_point(point) for point in ring]
            parsed.append(points)
            all_points.extend(points)
        if len(all_points) > MAX_VERTICES:
            raise LayoutError("layout_vertex_budget")
        room_rows.append((raw, parsed))
    min_x, max_x = min(p[0] for p in all_points), max(p[0] for p in all_points)
    min_z, max_z = min(p[1] for p in all_points), max(p[1] for p in all_points)
    span = max(max_x - min_x, max_z - min_z)
    if span <= EPSILON:
        raise LayoutError("layout_degenerate_bounds")
    origin = [(max_x + min_x - span) / 2, (max_z + min_z - span) / 2]

    def transform(point):
        transformed = [(point[i] - origin[i]) / span for i in (0, 1)]
        return [0.0 if -EPSILON <= v <= 0 else 1.0 if 1 <= v <= 1 + EPSILON else v for v in transformed]

    rooms = []
    for raw, rings in room_rows:
        regions = _regions_from_rings([[transform(p) for p in ring] for ring in rings])
        identity = raw.get("id") or raw.get("uniqueId")
        if identity is None or isinstance(identity, bool):
            raise LayoutError("layout_room_id_required")
        rooms.append(Room(id=str(identity), name=raw.get("name", ""), **regions[0], parts=regions[1:]))
    raw_devices = floor.get("Devices", floor.get("devices", []))
    if not isinstance(raw_devices, list) or len(raw_devices) > MAX_DEVICES:
        raise LayoutError("layout_device_budget")
    devices, unplaced = [], []
    for raw in raw_devices:
        if not isinstance(raw, dict):
            raise LayoutError("layout_invalid_device")
        identity = raw.get("did") or raw.get("id")
        if identity is None or isinstance(identity, bool):
            raise LayoutError("layout_device_id_required")
        identity = str(identity)
        explicit_room = raw.get("room_id", raw.get("roomId"))
        if explicit_room is not None:
            explicit_room = str(explicit_room)
        if raw.get("position") is None:
            unplaced.append(UnplacedDevice(id=identity, room_id=explicit_room))
            continue
        point = transform(_world_point(raw["position"]))
        matches = [
            room
            for room in rooms
            if room.contains(point) and (explicit_room is None or room.id == explicit_room)
        ]
        if len(matches) == 1 and all(0 <= coordinate <= 1 for coordinate in point):
            devices.append(Device(id=identity, room_id=matches[0].id, position=point))
        else:
            reason = (
                "ambiguous_room"
                if len(matches) > 1
                else (
                    "unknown_room"
                    if explicit_room and explicit_room not in {r.id for r in rooms}
                    else "outside_room"
                )
            )
            unplaced.append(UnplacedDevice(id=identity, room_id=explicit_room, reason=reason))
    return Layout(
        source={
            "kind": "mijia_floormap_v3",
            "floor_id": str(floor.get("id", "")),
            "floor_name": str(floor.get("floorName", "")),
            "geometry": "room_footprints",
            "normalization": {"origin": origin, "span": span},
            "content_sha256": hashlib.sha256(
                json.dumps(value, sort_keys=True, allow_nan=False).encode()
            ).hexdigest(),
            "missing": ["wall_dimensions", "openings", "furniture"],
        },
        rooms=rooms,
        devices=devices,
        unplaced_devices=unplaced,
    )


def _bitmap_red(data: bytes):
    if not isinstance(data, bytes) or len(data) > 8 * MAX_BYTES or len(data) < 54 or data[:2] != b"BM":
        raise LayoutError("layout_invalid_bitmap")
    (offset,) = struct.unpack_from("<I", data, 10)
    dib, width, height, planes, bpp, compression = struct.unpack_from("<IiiHHI", data, 14)
    if dib < 40 or dib > 124 or planes != 1 or bpp not in (24, 32) or compression != 0:
        raise LayoutError("layout_unsupported_bitmap")
    if not 1 <= width <= 1024 or not 1 <= abs(height) <= 1024 or offset < 14 + dib:
        raise LayoutError("layout_bitmap_budget")
    stride = ((width * bpp + 31) // 32) * 4
    if offset + stride * abs(height) > len(data):
        raise LayoutError("layout_truncated_bitmap")
    rows = []
    for y in range(abs(height)):
        source_y = abs(height) - 1 - y if height > 0 else y
        begin = offset + source_y * stride
        rows.append(list(data[begin + 2 : begin + width * (bpp // 8) : bpp // 8]))
    return width, abs(height), rows


def _cell_boundaries(labels, target):
    height, width = len(labels), len(labels[0])
    edges = defaultdict(list)
    count = 0
    for y, row in enumerate(labels):
        for x, label in enumerate(row):
            if label != target:
                continue
            count += 1
            if y == 0 or labels[y - 1][x] != target:
                edges[(x, y)].append((x + 1, y))
            if x == width - 1 or labels[y][x + 1] != target:
                edges[(x + 1, y)].append((x + 1, y + 1))
            if y == height - 1 or labels[y + 1][x] != target:
                edges[(x + 1, y + 1)].append((x, y + 1))
            if x == 0 or labels[y][x - 1] != target:
                edges[(x, y + 1)].append((x, y))
            if len(edges) > MAX_VERTICES * 8:
                raise LayoutError("layout_bitmap_complexity_budget")
    loops = []
    while edges:
        start = current = min(edges)
        ring = []
        while True:
            ring.append(current)
            following = edges[current].pop()
            if not edges[current]:
                del edges[current]
            current = following
            if current == start:
                break
        simple = [
            list(p) for i, p in enumerate(ring) if abs(_cross(ring[i - 1], p, ring[(i + 1) % len(ring)])) > 0
        ]
        if len(simple) > 512:
            raise LayoutError("layout_vertex_budget")
        loops.append(simple)
    return loops, count


def layout_from_baked(floor: dict, texture: bytes, room_names: dict) -> Layout:
    """Decode the actual Mi Home red-channel room map using the Python stdlib."""
    _json_budget(floor)
    _json_budget(room_names)
    if not isinstance(floor, dict) or not isinstance(room_names, dict):
        raise LayoutError("layout_object_required")
    gray_map = floor.get("roomGrayMap")
    if not isinstance(gray_map, dict) or not 1 <= len(gray_map) <= MAX_ROOMS:
        raise LayoutError("layout_room_gray_map_required")
    names = room_names.get("rooms", room_names)
    if not isinstance(names, dict):
        raise LayoutError("layout_room_names_required")
    width, height, labels = _bitmap_red(texture)
    if floor.get("textureWidth") != width or floor.get("textureHeight") != height:
        raise LayoutError("layout_bitmap_dimensions_mismatch")
    ids = {}
    for room_id, gray in gray_map.items():
        index = _finite_number(gray) * 255
        label = round(index)
        if not 1 <= label <= 255 or abs(index - label) > 0.001 or label in ids:
            raise LayoutError("layout_invalid_room_gray_map")
        ids[label] = str(room_id)
    if set(label for row in labels for label in row) - {0, *ids}:
        raise LayoutError("layout_unmapped_bitmap_room")
    rooms = []
    span = max(width, height)
    offset = [(span - width) / 2, (span - height) / 2]
    for label, room_id in ids.items():
        name = names.get(room_id)
        if isinstance(name, dict):
            name = name.get("name")
        if not isinstance(name, str) or not name.strip():
            raise LayoutError("layout_room_name_missing", {"room_id": room_id})
        loops, count = _cell_boundaries(labels, label)
        if not count:
            raise LayoutError("layout_room_pixels_missing", {"room_id": room_id})
        regions = _regions_from_rings(
            [[[(p[i] + offset[i]) / span for i in (0, 1)] for p in ring] for ring in loops]
        )
        polygon_area = sum(
            abs(_area(region["polygon"])) - sum(abs(_area(h)) for h in region["holes"]) for region in regions
        )
        if not math.isclose(polygon_area * span * span, count, abs_tol=1e-6):
            raise LayoutError("layout_bitmap_geometry_mismatch")
        rooms.append(Room(id=room_id, name=name, **regions[0], parts=regions[1:]))
    return Layout(
        source={
            "kind": "mijia_baked",
            "floor_id": str(floor.get("floorId", "")),
            "floor_sha1": floor.get("floorSha1"),
            "floor_name": str(floor.get("floorName", "")),
            "content_sha256": hashlib.sha256(json.dumps(floor, sort_keys=True).encode()).hexdigest(),
            "texture_sha256": hashlib.sha256(texture).hexdigest(),
            "resolution": [width, height],
            "normalization": {"origin": [-offset[0], -offset[1]], "span": span},
            "geometry": "room_footprints",
            "missing": ["wall_dimensions", "openings", "furniture", "device_positions"],
        },
        rooms=rooms,
    )


class LayoutStore:
    """One private JSON document, CAS writes, and one atomically saved rollback.

    Imports merge stable device IDs. Missing placements preserve the old points;
    changed placements require an explicit conflict acceptance or replace_devices.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.previous_path = self.path.with_name(self.path.stem + ".previous" + self.path.suffix)
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._thread_lock = threading.RLock()

    @staticmethod
    def _read(path):
        if path.is_symlink():
            raise LayoutError("layout_symlink_refused")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise LayoutError("layout_regular_file_required")
                raw = stream.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return None
        if len(raw) > MAX_BYTES:
            raise LayoutError("layout_input_budget")
        return Layout.model_validate_json(raw)

    def current(self) -> Layout | None:
        return self._read(self.path)

    @contextlib.contextmanager
    def _locked(self):
        with self._thread_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.is_symlink() or self.previous_path.is_symlink() or self._lock_path.is_symlink():
                raise LayoutError("layout_symlink_refused")
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    @staticmethod
    def _expected(current, revision):
        if type(revision) is not int or revision < 0:
            raise LayoutError("layout_invalid_revision")
        actual = current.revision if current else 0
        if revision != actual:
            raise LayoutError(
                "layout_revision_conflict", {"expected_revision": revision, "current_revision": actual}
            )

    @staticmethod
    def _merge(current, incoming):
        if current is None:
            return incoming, [], []
        previous = {device.id: device for device in current.devices}
        devices = {device.id: device for device in incoming.devices}
        unplaced = {device.id: device for device in incoming.unplaced_devices}
        rooms = {room.id: room for room in incoming.rooms}
        conflicts, preserved = [], []
        for identity, old in previous.items():
            new = devices.get(identity)
            if new is not None and old != new:
                conflicts.append(
                    {
                        "kind": "device_position_changed",
                        "device_id": identity,
                        "previous": old.model_dump(),
                        "incoming": new.model_dump(),
                    }
                )
                continue
            if old.room_id not in rooms or not rooms[old.room_id].contains(old.position):
                conflicts.append(
                    {
                        "kind": "device_room_geometry_changed",
                        "device_id": identity,
                        "previous": old.model_dump(),
                        "incoming": new.model_dump() if new else None,
                    }
                )
                continue
            if new is None:
                devices[identity] = old
                unplaced.pop(identity, None)
                preserved.append(identity)
        for old in current.unplaced_devices:
            if old.id not in devices and old.id not in unplaced:
                unplaced[old.id] = old
        value = incoming.model_dump()
        value["devices"] = [device.model_dump() for device in devices.values()]
        value["unplaced_devices"] = [device.model_dump() for device in unplaced.values()]
        # An orphaned old point is not silently inserted into an invalid room.
        return Layout.model_validate(value), conflicts, preserved

    @staticmethod
    def _diff(current, incoming, merged, conflicts, preserved):
        def changes(old_rows, new_rows):
            old = {row.id: row.model_dump() for row in old_rows}
            new = {row.id: row.model_dump() for row in new_rows}
            return {
                "added": sorted(new.keys() - old.keys()),
                "changed": sorted(key for key in old.keys() & new.keys() if old[key] != new[key]),
                "removed": sorted(old.keys() - new.keys()),
            }

        return {
            "base_revision": current.revision if current else 0,
            "rooms": changes(current.rooms if current else [], incoming.rooms),
            "devices": changes(current.devices if current else [], merged.devices),
            "preserved_device_ids": sorted(preserved),
            "conflicts": conflicts,
            "unplaced_devices": [device.model_dump() for device in merged.unplaced_devices],
            "can_apply": not conflicts,
            "layout": merged.model_dump(),
        }

    def preview(self, layout: Layout | dict) -> dict:
        incoming, current = normalize_layout(layout), self.current()
        merged, conflicts, preserved = self._merge(current, incoming)
        return self._diff(current, incoming, merged, conflicts, preserved)

    @staticmethod
    def _atomic_write(path, layout):
        raw = layout.model_dump_json(indent=2).encode() + b"\n"
        if len(raw) > MAX_BYTES:
            raise LayoutError("layout_output_budget")
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if path.is_symlink():
                raise LayoutError("layout_symlink_refused")
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _save(self, current, layout):
        value = layout.model_dump()
        value["revision"] = (current.revision if current else 0) + 1
        result = Layout.model_validate(value)
        if current is not None:
            self._atomic_write(self.previous_path, current)
        self._atomic_write(self.path, result)
        return result

    def apply(
        self, layout: Layout | dict, expected_revision: int, *, accept_device_changes: bool = False
    ) -> Layout:
        incoming = normalize_layout(layout)
        with self._locked():
            current = self.current()
            self._expected(current, expected_revision)
            merged, conflicts, preserved = self._merge(current, incoming)
            diff = self._diff(current, incoming, merged, conflicts, preserved)
            if conflicts and (
                not accept_device_changes or any(c["kind"] != "device_position_changed" for c in conflicts)
            ):
                raise LayoutError("layout_import_conflict", diff)
            return self._save(current, merged)

    def replace_devices(self, devices: list[Device | dict], expected_revision: int) -> Layout:
        """Explicit user placement edit. The supplied list is the complete placed set."""
        if not isinstance(devices, list) or len(devices) > MAX_DEVICES:
            raise LayoutError("layout_device_budget")
        with self._locked():
            current = self.current()
            self._expected(current, expected_revision)
            if current is None:
                raise LayoutError("layout_not_imported")
            placed = [
                device if isinstance(device, Device) else Device.model_validate(device) for device in devices
            ]
            ids = {device.id for device in placed}
            unplaced = {device.id: device for device in current.unplaced_devices if device.id not in ids}
            for old in current.devices:
                if old.id not in ids:
                    unplaced[old.id] = UnplacedDevice(id=old.id, room_id=old.room_id)
            value = current.model_dump()
            value["devices"] = [device.model_dump() for device in placed]
            value["unplaced_devices"] = [device.model_dump() for device in unplaced.values()]
            return self._save(current, Layout.model_validate(value))

    def restore(self, expected_revision: int) -> Layout:
        with self._locked():
            current = self.current()
            self._expected(current, expected_revision)
            previous = self._read(self.previous_path)
            if previous is None:
                raise LayoutError("layout_previous_unavailable")
            return self._save(current, previous)
