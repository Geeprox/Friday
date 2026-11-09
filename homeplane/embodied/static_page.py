"""Version the viewer entry assets so an update cannot reuse an old stylesheet."""

import hashlib
import re

from starlette.responses import FileResponse
from starlette.staticfiles import NotModifiedResponse, StaticFiles

ENTRY = re.compile(
    r"""(?P<attribute>(?:src|href)=["'])(?P<url>/assets/(?P<name>[a-zA-Z0-9_.-]+\.(?:js|css)))(?=["'])"""
)


def static_page(path):
    html = path.read_text()

    def version(match):
        asset = path.parent / match["name"]
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()[:16]
        return match["attribute"] + match["url"] + "?v=" + digest

    return ENTRY.sub(version, html)


def static_asset(path, request_headers):
    response = FileResponse(path, stat_result=path.stat(), headers={"Cache-Control": "no-cache"})
    if StaticFiles(directory=path.parent, check_dir=False).is_not_modified(response.headers, request_headers):
        return NotModifiedResponse(response.headers)
    return response
