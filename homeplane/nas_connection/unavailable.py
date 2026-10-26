"""Cached fail-closed reader when private NAS state cannot be composed."""

from .contracts import NASConnectionError


class UnavailableNASReader:
    def __init__(self, status):
        self.cached_status = status

    def status(self):
        return self.cached_status()

    async def list_page(self, *args, **kwargs):
        raise NASConnectionError("nas_unavailable")

    async def stat(self, *args, **kwargs):
        raise NASConnectionError("nas_unavailable")

    async def read_range(self, *args, **kwargs):
        raise NASConnectionError("nas_unavailable")
