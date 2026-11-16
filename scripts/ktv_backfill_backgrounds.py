"""为本机已制备并可核实的曲目补齐背景。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import stat
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from homeplane.intelligence.backgrounds import SongBackgrounds
from homeplane.intelligence.ktv_background_context import SongBackgroundContext
from homeplane.intelligence.runtime import IntelligenceRuntime
from homeplane.intelligence.settings import default_directory, private_path
from homeplane.ktv_source_api import LocalSourceAPI
from homeplane.ktv_worker import BACKGROUND_PREFETCH_COUNT


def prepared_rows(config):
    private_path(config)
    info = config.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 32768:
        raise ValueError("private_ktv_config_required")
    runtime = Path(json.loads(config.read_text())["root"])
    private_path(runtime)
    valid = []
    for state in sorted((runtime / "jobs").glob("*/state.json"))[:256]:
        try:
            if state.resolve() != state or not state.is_file() or state.stat().st_size > 1_048_576:
                continue
            row = json.loads(state.read_text())
            if (row.get("state") == "ready" and not row.get("simulated")
                    and re.fullmatch(r"[1-9][0-9]{0,17}", str(row.get("song_id", "")))):
                valid.append(row)
        except (OSError, ValueError, AttributeError):
            continue
    return runtime, sorted(valid, key=lambda row: row.get("updated", 0), reverse=True)


async def run(args):
    root, jobs = prepared_rows(args.config.absolute())
    runtime = IntelligenceRuntime(None, default_directory())
    config = runtime.settings.read()
    resolver = SongBackgroundContext(runtime)
    source = LocalSourceAPI(root / "source-account", args.catalog_package.resolve(strict=True))
    backgrounds = SongBackgrounds(runtime, recording=source.public_lyric_recording, max_parallel=args.parallel,
                                  context_resolver=resolver, context_timeout=22, debug=args.debug_backgrounds,
                                  cloud_metadata=source.cloud_background_metadata, include_mv=True)
    items, cloud = {}, []
    counts = Counter()
    for row in jobs:
        try:
            identifier = backgrounds.identifier_for(row, model=config.image_model)
            if identifier in items:
                continue
            cached = backgrounds.cached(identifier)
            if cached:
                counts["cached"] += 1
                items[identifier] = None
            elif row.get("audio_origin") == "cloud":
                items[identifier] = None
                cloud.append((identifier, row))
            else:
                items[identifier] = (row, None)
        except (OSError, ValueError):
            counts["invalid"] += 1
    if args.verify_cloud or args.apply:
        # 云盘只提供显示的歌名和歌手，有公开来源时再补发行与创作资料。
        gate = asyncio.Semaphore(2)

        async def verify(key, row):
            async with gate:
                song_id = str(row["song_id"])
                metadata = source.cloud_background_metadata(song_id)
                if metadata is None:
                    metadata = await asyncio.to_thread(source.public_cloud_background, song_id)
                if metadata:
                    items[key] = (row, metadata)
                    counts["verified_cloud"] += 1
                else:
                    counts["unverified_private"] += 1

        await asyncio.gather(*(verify(key, row) for key, row in cloud))
    else:
        counts["unverified_private"] += len(cloud)
    pending = [(key, *row) for key, row in items.items() if row is not None]
    usage = runtime.ledger.status(config)
    budget = None if args.debug_backgrounds else max(0, config.daily_images - usage["used"]["images"])
    if not args.apply:
        return {"eligible": len(pending), "daily_remaining": budget, "parallel": args.parallel, **counts}
    selected = pending[:budget]
    report = {"eligible": len(pending), "deferred_for_budget": len(pending) - len(selected),
              "parallel": args.parallel, "launch_limit": len(selected)}
    if not config.cloud_images:
        return {**report, "reason": "cloud_images_disabled"}
    try:
        for offset in range(0, len(selected), args.parallel):
            batch = selected[offset:offset + args.parallel]
            for _, row, metadata in batch:
                backgrounds.request(row, public_metadata=metadata)
            await asyncio.gather(*list(backgrounds.tasks.values()), return_exceptions=True)
            for identifier, row, metadata in batch:
                resolved = backgrounds.identifier_for(row, public=metadata)
                if backgrounds.cached(resolved):
                    counts["generated"] += 1
                else:
                    code = backgrounds.failures.get(identifier, ("generation_pending",))[0]
                    counts[code] += 1
    finally:
        await backgrounds.close()
    return {**report, **counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--catalog-package", required=True, type=Path)
    parser.add_argument("--parallel", type=int, default=BACKGROUND_PREFETCH_COUNT, choices=range(1, 25))
    parser.add_argument("--verify-cloud", action="store_true", help="预检时也用公开曲库核对云盘录音")
    parser.add_argument("--debug-backgrounds", action="store_true", help="调试生图时跳过 Friday 的每日上限")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
