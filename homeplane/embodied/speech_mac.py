"""使用 macOS 当前系统语音与声音输出播报。"""

import asyncio
import os
import sys
import threading

from .local_child import LocalChild


SAY = '/usr/bin/say'
_SLOTS = threading.BoundedSemaphore(1)


def available():
    return sys.platform == 'darwin' and os.access(SAY, os.X_OK)


async def play(text, *, guard):
    if not available():
        return dict(status='unavailable', reason='mac_speech_unavailable')
    await guard.checkpoint()
    child = LocalChild((SAY, '-f', '-'), _SLOTS)
    try:
        process = await child.start()
        # 空管道无播报内容。发送前再次核对当前提示和输出选择。
        await guard.checkpoint()
        guard.dispatch()
        process.stdin.write(text.encode('utf-8'))
        process.stdin.close()
        code = await process.wait()
        guard.check()
        return dict(status='accepted' if code == 0 else 'unconfirmed',
                    backend_completed=code == 0, physical_audio_verified=False)
    finally:
        await child.close()
        if child.reaper is not None:
            await asyncio.shield(child.reaper)
