"""Runnable synthetic integration. Never resolves a real NAS or photo directory."""

import asyncio
import hashlib
import json
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .catalog import FamilyMediaCatalog
from .contracts import (
    FamilyMediaAssetRequest,
    FamilyMediaQuery,
    FamilyMediaRecipeRequest,
    FamilyMediaRecipeReview,
    FamilyMediaResolveRequest,
    FamilyMediaScanRequest,
)
from .fixture import SyntheticNASReader


async def demo():
    now = [datetime(2026, 10, 1, tzinfo=UTC)]
    source = SyntheticNASReader()
    original = b"SIMULATED photo original; no household pixels"
    source.put("SIMULATED-photo-a", original)
    source.put("SIMULATED-photo-duplicate", original)
    source.put("SIMULATED-video", b"SIMULATED video original", kind="video", closed=False)
    with tempfile.TemporaryDirectory(prefix="friday-family-SIMULATED-") as directory:
        connection = sqlite3.connect(Path(directory) / "catalog.sqlite")
        catalog = FamilyMediaCatalog(connection, source, simulated=True, clock=lambda: now[0])
        await catalog.scan(FamilyMediaScanRequest(expected_context=(await catalog.context()).fingerprint))
        now[0] += timedelta(seconds=3)
        result = await catalog.scan(
            FamilyMediaScanRequest(expected_context=(await catalog.context()).fingerprint)
        )
        page = await catalog.list_items(FamilyMediaQuery(expected_context=result.context.fingerprint))
        photo = next(item for item in page.items if item.media_kind == "image")
        asset = await catalog.asset_evidence(
            FamilyMediaAssetRequest(
                expected_context=page.context_fingerprint,
                asset_id=photo.asset_id,
            )
        )
        recipe = await catalog.preview_recipe(
            FamilyMediaRecipeRequest(
                expected_context=page.context_fingerprint,
                item_id=photo.item_id,
                expected_original_hash=photo.original_hash,
                operations=[
                    {"operation": "organize", "album": "SIMULATED family album"},
                    {"operation": "crop", "x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
                ],
            )
        )
        reviewed = await catalog.review_recipe(
            FamilyMediaRecipeReview(
                expected_context=recipe.context_fingerprint,
                recipe_id=recipe.recipe_id,
                decision="reviewed",
            )
        )
        plan = await catalog.prepare_original(
            FamilyMediaResolveRequest(
                expected_context=(await catalog.context()).fingerprint,
                item_id=photo.item_id,
            )
        )
        returned = await catalog.read_original(plan.plan_id, 0, len(original))
        assert returned == original
        assert photo.original_hash == hashlib.sha256(original).hexdigest()
        assert source.objects["SIMULATED-photo-a"]["bytes"] == original
        evidence = {
            "simulated": True,
            "items": [item.model_dump(mode="json") for item in page.items],
            "duplicate_originals": asset.original_count,
            "reviewed_recipe": reviewed.model_dump(mode="json"),
            "bounded_original_read_matches": returned == original,
            "checkpoint": result.checkpoint.model_dump(mode="json"),
            "media_copied": False,
            "original_mutated": False,
            "real_nas_accessed": False,
        }
        await catalog.close()
        connection.close()
        return evidence


if __name__ == "__main__":
    print(json.dumps(asyncio.run(demo()), indent=2))
