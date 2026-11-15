"""已配置的本机相机可以在家电来源暂不可用时独立运行。"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from .contracts import Snapshot


class LocalCameraHome:
    def __init__(self, *, mijia_selected=False, timezone: Callable[[], str | None] | None = None):
        self.mijia_selected = mijia_selected
        self.timezone = timezone
        self._boot = str(uuid4())

    async def household_timezone(self):
        return self.timezone() if self.mijia_selected and self.timezone is not None else None

    def health(self):
        return {'mode': 'local-cameras', 'transport': 'offline', 'simulated': False}

    async def snapshot(self):
        return Snapshot(mode='local-cameras', version='local-cameras', generated_at=datetime.now(UTC),
                        entities=[], observations=[], states=[], rules=[], events=[],
                        limitations=['appliance_provider_unavailable'])

    async def close(self):
        pass
