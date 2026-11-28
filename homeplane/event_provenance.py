"""Opaque, durable source bindings for historical event review.

This digest is compatibility evidence only. It grants no read scope, identity,
causality, physical confirmation or device authority. Never derive it for an old
event from the current registry: the producer must record it at observation time.
"""

import hashlib
import json
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

from .contracts import Entity


def valid_label(value):
    if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    try:
        return len(value.encode('utf8')) <= 4096
    except UnicodeError:
        return False


def source_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf8')).hexdigest()


def ha_endpoint(url):
    if not valid_label(url):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {'http', 'https'} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment):
            return None
        port = parts.port
        return [parts.scheme, parts.hostname.lower(),
                port if port is not None else (443 if parts.scheme == 'https' else 80), parts.path.rstrip('/')]
    except ValueError:
        return None


def entity_binding(entity):
    if (not isinstance(entity, Entity) or not valid_label(entity.id)
            or not valid_label(entity.physical_source_id) or len(entity.bindings) > 128):
        return None
    values = [(item.source, item.external_id) for item in entity.bindings]
    if any(not valid_label(source) or not valid_label(external) for source, external in values):
        return None
    if len(set(values)) != len(values):
        return None
    # Preserve the same entity + complete external-binding identity used by the
    # Action Gateway, adding the explicit physical source and registered room.
    return [entity.id, entity.physical_source_id, sorted(values)]


class SourceContextProvider(ABC):
    """由已接入的来源提供观察当时的身份材料。"""

    @abstractmethod
    def _entity_source_context(self, entity, binding):
        """根据已校验的实体绑定核对当前来源。"""

    @abstractmethod
    def _registered_room_context(self, room):
        """从当前来源核对已校验的房间标识。"""


def entity_source_context(adapter, entity):
    """读取实体来源的持久摘要，来源或实体无法核对时返回空值。"""
    binding = entity_binding(entity)
    if binding is None or not isinstance(adapter, SourceContextProvider):
        return None
    return adapter._entity_source_context(entity, binding)


def registered_room_context(adapter, room):
    """读取由当前合格登记支持的房间身份摘要。"""
    if not valid_label(room) or len(room) > 128 or not isinstance(adapter, SourceContextProvider):
        return None
    return adapter._registered_room_context(room)
