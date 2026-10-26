"""Import actual Mi Home geometry into Friday's persistent, repeatable layout.

Examples:
  python scripts/embodied/import_home_layout.py --baked-json floor_data.json \
    --texture texture.bmp --room-names room-names.json --output home-layout.json
  python scripts/embodied/import_home_layout.py --floormap-json floor-map.json \
    --output home-layout.json --preview
  python scripts/embodied/import_home_layout.py --android-serial DEVICE \
    --room-names room-names.json --output home-layout.json

Android acquisition reads only the two selected BakeData floor files. It never
launches an app, modifies the phone, reads memory diagnostics, or uses account
material. Existing device points survive later bitmap imports by stable IDs.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pydantic import ValidationError  # noqa: E402

from homeplane.home_layout import (  # noqa: E402
    MAX_BYTES,
    LayoutError,
    LayoutStore,
    layout_from_baked,
    normalize_layout,
)

BAKE_ROOT = "/sdcard/Android/data/com.xiaomi.smarthome/files/BakeData/floors"
FLOOR_ID = re.compile(r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$")


def read_bytes(path, limit=MAX_BYTES):
    target = Path(path)
    if target.is_symlink():
        raise LayoutError("layout_source_symlink_refused")
    with target.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise LayoutError("layout_input_budget")
    return value


def read_json(path):
    return json.loads(read_bytes(path).decode("utf-8-sig"))


def _adb(adb, serial, *arguments, limit=MAX_BYTES):
    # All remote path arguments are constructed from the fixed root and a UUID.
    # Output is consumed in bounded chunks so a changing file cannot grow a
    # capture beyond its announced size budget.
    with subprocess.Popen(
        [adb, "-s", serial, *arguments], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as process:
        try:
            import selectors
            import time

            chunks, size = [], 0
            deadline = time.monotonic() + 30
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LayoutError("layout_android_timeout")
                    if not selector.select(remaining):
                        raise LayoutError("layout_android_timeout")
                    chunk = process.stdout.read1(min(65536, limit + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise LayoutError("layout_android_output_budget")
                    chunks.append(chunk)
            if process.wait(timeout=max(0.1, deadline - time.monotonic())):
                raise LayoutError("layout_android_read_failed")
            return b"".join(chunks)
        except BaseException:
            process.kill()
            process.wait(timeout=5)
            raise


def read_android_bake(serial, bake_directory=BAKE_ROOT, floor_id=None):
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", serial):
        raise LayoutError("layout_invalid_android_serial")
    adb = shutil.which("adb")
    if not adb:
        raise LayoutError("layout_adb_unavailable")
    folder = bake_directory.rstrip("/")
    if folder != BAKE_ROOT:
        if not folder.startswith(BAKE_ROOT + "/") or not FLOOR_ID.fullmatch(folder[len(BAKE_ROOT) + 1 :]):
            raise LayoutError("layout_android_directory_scope")
        selected = folder.rsplit("/", 1)[1]
        if floor_id is not None and floor_id != selected:
            raise LayoutError("layout_android_floor_conflict")
    else:
        names = _adb(adb, serial, "shell", "ls", "-1", BAKE_ROOT, limit=16384).decode().splitlines()
        if len(names) > 64:
            raise LayoutError("layout_android_floor_budget")
        candidates = sorted(name for name in names if FLOOR_ID.fullmatch(name))
        if floor_id is None:
            if len(candidates) != 1:
                raise LayoutError("layout_android_floor_selection_required", {"floor_ids": candidates})
            selected = candidates[0]
        elif floor_id not in candidates:
            raise LayoutError("layout_android_floor_not_found")
        else:
            selected = floor_id
        folder += "/" + selected
    floor_raw = _adb(adb, serial, "exec-out", "cat", folder + "/floor_data.json", limit=MAX_BYTES)
    texture = _adb(adb, serial, "exec-out", "cat", folder + "/texture.bmp", limit=8 * MAX_BYTES)
    floor = json.loads(floor_raw.decode("utf-8-sig"))
    if floor.get("floorId") != selected:
        raise LayoutError("layout_android_floor_identity_mismatch")
    return floor, texture


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--baked-json", type=Path)
    inputs.add_argument("--floormap-json", "--layout-json", dest="floormap_json", type=Path)
    inputs.add_argument("--android-serial")
    inputs.add_argument("--restore", action="store_true")
    parser.add_argument("--texture", type=Path)
    parser.add_argument("--room-names", type=Path)
    parser.add_argument("--bake-directory", default=BAKE_ROOT)
    parser.add_argument("--floor-id")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument(
        "--accept-device-changes",
        action="store_true",
        help="Explicitly accept changed imported device positions shown in a preview.",
    )
    args = parser.parse_args(argv)
    try:
        store = LayoutStore(args.output)
        current = store.current()
        expected = (
            args.expected_revision
            if args.expected_revision is not None
            else (current.revision if current else 0)
        )
        if args.restore:
            if args.preview:
                raise LayoutError("layout_restore_preview_unsupported")
            saved = store.restore(expected)
            print(json.dumps({"saved": str(args.output), "revision": saved.revision, "restored": True}))
            return 0
        if args.baked_json or args.android_serial:
            if args.room_names is None or (args.baked_json and args.texture is None):
                raise LayoutError("layout_baked_inputs_required")
            if args.android_serial:
                floor, texture = read_android_bake(args.android_serial, args.bake_directory, args.floor_id)
            else:
                floor, texture = read_json(args.baked_json), read_bytes(args.texture, limit=8 * MAX_BYTES)
            layout = layout_from_baked(floor, texture, read_json(args.room_names))
        else:
            layout = normalize_layout(read_json(args.floormap_json))
        preview = store.preview(layout)
        if args.preview:
            print(json.dumps(preview, ensure_ascii=False))
            return 0
        saved = store.apply(layout, expected, accept_device_changes=args.accept_device_changes)
        print(
            json.dumps(
                {
                    "saved": str(args.output),
                    "revision": saved.revision,
                    "rooms": len(saved.rooms),
                    "placed_devices": len(saved.devices),
                    "unplaced_devices": [d.model_dump() for d in saved.unplaced_devices],
                    "preserved_device_ids": preview["preserved_device_ids"],
                    "units": "relative",
                },
                ensure_ascii=False,
            )
        )
        return 0
    except ValidationError as error:
        print(
            json.dumps(
                {
                    "error": "layout_invalid",
                    "issues": [
                        {"location": list(issue["loc"]), "message": issue["msg"]} for issue in error.errors()
                    ],
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    except (LayoutError, OSError, UnicodeError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        print(
            json.dumps(
                {
                    "error": getattr(error, "code", "layout_import_failed"),
                    "details": getattr(error, "details", {}),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
