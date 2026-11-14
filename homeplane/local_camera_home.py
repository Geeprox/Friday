"""已配置的本机相机可以在家电来源暂不可用时独立运行。"""

from datetime import UTC, datetime

from .contracts import Snapshot


class LocalCameraHome:
    def health(self):
        return {'mode': 'local-cameras', 'transport': 'offline', 'simulated': False}

    async def snapshot(self):
        return Snapshot(mode='local-cameras', version='local-cameras', generated_at=datetime.now(UTC),
                        entities=[], observations=[], states=[], rules=[], events=[],
                        limitations=['appliance_provider_unavailable'])

    async def close(self):
        pass
