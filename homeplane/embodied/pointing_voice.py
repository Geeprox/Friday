"""把同一镜头的标定指向和已准入语音绑定到具体设备。"""

import re
from dataclasses import dataclass, field


POINTING_HOLD_SECONDS = 3.0
POINTING_FOLLOWUP_SECONDS = 6.0
POINTING_DISPATCH_SECONDS = 1.5
RELATIVE_BRIGHTNESS = {'brighten': 26, 'dim': -26}

_OBJECT = r'(?P<device>这盏灯|这个灯|这扇(?:窗)?帘|这个窗帘|这个(?:设备)?)'
_POWER = r'(?P<power>打开|开启|开一下|开|关掉|关闭|关上|关)'
_BRIGHTNESS = r'(?P<brightness>亮一点|亮点|亮一些|暗一点|暗点|暗一些)'
_PREFIX = r'(?:(?:请你|请|麻烦你|麻烦|你帮我|帮我|给我)\s*)*'
_NEGATIVE = r'(?P<negative>(?:先|暂时)?(?:别|不要|不用|不必)\s*)?'
_SUFFIX = r'(?:\s*(?:一下|吧|谢谢))*'
_PATTERNS = tuple(re.compile(_PREFIX + _NEGATIVE + pattern + _SUFFIX) for pattern in (
    _POWER + r'\s*' + _OBJECT,
    r'(?:把)?\s*' + _OBJECT + r'\s*(?:给我|调)?\s*(?:' + _POWER + '|' + _BRIGHTNESS + ')',
))
_REFERENCE = re.compile(_OBJECT)
_CONTROL = re.compile(_POWER + '|' + _BRIGHTNESS)


@dataclass(frozen=True, slots=True)
class PointingCommand:
    action: str
    kind: str | None = None


def pointing_command(body):
    """复合句或转述中的指代没有单一设备动作。"""
    body = (body or '').strip().rstrip('。.!！').strip()
    if not body or len(body) > 120:
        return None
    for pattern in _PATTERNS:
        match = pattern.fullmatch(body)
        if match is None:
            continue
        fields = match.groupdict()
        if fields['negative']:
            return PointingCommand('no_op')
        device = fields['device']
        kind = 'light' if device.endswith('灯') else 'cover' if device.endswith('帘') else None
        level = fields.get('brightness')
        if level:
            return PointingCommand('brighten' if level.startswith('亮') else 'dim', kind or 'light')
        return PointingCommand('close' if fields['power'].startswith('关') else 'open', kind)
    if _REFERENCE.search(body) and _CONTROL.search(body):
        return PointingCommand('clarify')
    return None


@dataclass(frozen=True, slots=True)
class PointingTarget:
    binding: str
    revision: int
    target: str
    owner: int | str
    sequence: int
    observed_mono: float = field(compare=False)


@dataclass(frozen=True, slots=True)
class PointingFrame:
    target: PointingTarget
    observed_mono: float
    single_person: bool


@dataclass(frozen=True, slots=True)
class PointingReply:
    sequence: int
    source: tuple[str, str]
    view_revision: int
    voice_revision: int
    expires: float


@dataclass(frozen=True, slots=True)
class PointingSpeech:
    target: PointingTarget
    single_person: bool
    reply: PointingReply | None = None
