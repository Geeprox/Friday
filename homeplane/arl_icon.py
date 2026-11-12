"""Extract the actual AppIcon's original 64 px pixels for ARL display metadata."""
import base64
import hashlib
from pathlib import Path
import plistlib
import struct
import zlib


def _resource():
    for parent in Path(__file__).resolve().parents:
        if parent.name == 'Contents' and parent.parent.suffix == '.app':
            info = plistlib.loads((parent / 'Info.plist').read_bytes())
            name = info.get('CFBundleIconFile')
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError('application_icon_unavailable')
            return parent / 'Resources' / (name if name.endswith('.icns') else name + '.icns')
    return Path(__file__).resolve().parents[1] / 'native/FridayDesktop/Resources/AppIcon/Friday.icns'


def application_icon(resource=None):
    """Keep the real ICNS source tag/hash; strip PNG metadata without resizing."""
    resource = Path(resource) if resource is not None else _resource()
    if resource.is_symlink() or not 8 <= resource.stat().st_size <= 4 * 1024 * 1024:
        raise ValueError('application_icon_unavailable')
    raw = resource.read_bytes()
    if raw[:4] != b'icns' or int.from_bytes(raw[4:8], 'big') != len(raw):
        raise ValueError('application_icon_invalid')
    at, members = 8, {}
    while at < len(raw):
        if at + 8 > len(raw):
            raise ValueError('application_icon_invalid')
        tag, size = raw[at:at+4], int.from_bytes(raw[at+4:at+8], 'big')
        if size < 8 or at + size > len(raw) or tag in members:
            raise ValueError('application_icon_invalid')
        members[tag] = raw[at+8:at+size]
        at += size
    tag = next((t for t in (b'icp6', b'ic12') if t in members), None)
    if tag is None:
        raise ValueError('application_icon_unavailable')
    png = members[tag]
    if png[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('application_icon_invalid')
    at, chunks = 8, []
    while at < len(png):
        if at + 12 > len(png):
            raise ValueError('application_icon_invalid')
        size = int.from_bytes(png[at:at+4], 'big')
        kind, end = png[at+4:at+8], at+12+size
        if end > len(png):
            raise ValueError('application_icon_invalid')
        body = png[at+8:end-4]
        if zlib.crc32(kind + body) & 0xffffffff != int.from_bytes(png[end-4:end], 'big'):
            raise ValueError('application_icon_invalid')
        if kind == b'IHDR' and body != struct.pack('>IIBBBBB', 64, 64, 8, 6, 0, 0, 0):
            raise ValueError('application_icon_invalid')
        if kind in {b'IHDR', b'IDAT', b'IEND'}:
            chunks.append(png[at:end])
        elif kind[0] & 32 == 0:
            raise ValueError('application_icon_invalid')
        at = end
    image = png[:8] + b''.join(chunks)
    if len(image) > 8192:
        raise ValueError('application_icon_budget_exceeded')
    return dict(schema_version=1, media_type='image/png',
                source_resource='CFBundleIconFile:' + tag.decode('ascii'),
                source_sha256=hashlib.sha256(raw).hexdigest(), sha256=hashlib.sha256(image).hexdigest(),
                byte_count=len(image), width=64, height=64, png_base64=base64.b64encode(image).decode('ascii'))
