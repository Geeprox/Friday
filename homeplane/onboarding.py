"""Passive setup inventory. Reading this report never discovers or starts a service."""
from pydantic import BaseModel, ConfigDict, Field


class SetupConfiguration(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    ha_url: str | None = Field(default=None, max_length=512)
    ha_instance: str | None = Field(default=None, max_length=96)
    camera_service_url: str | None = Field(default=None, max_length=512)
    camera_id: str | None = Field(default=None, max_length=128)


def setup_report(settings, health, *, camera_configured=False, desktop=False, control_enabled=False):
    config = settings.get_public()['config']
    return {
        'desktop': desktop,
        'ha_url': config.get('ha_url'),
        'ha_instance': config.get('ha_instance'),
        'home_readable': health.get('transport') == 'online' and not health.get('simulated'),
        'simulated': health.get('simulated') is True,
        'camera_configured': camera_configured,
        'camera_service_url': config.get('camera_service_url'),
        'camera_id_configured': config.get('camera_id_configured', False),
        'camera_media_source': config.get('camera_media_source'),
        'control_enabled': control_enabled,
        'device_writes': 'unchanged',
    }
