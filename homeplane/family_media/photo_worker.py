"""单次照片处理子进程；长度前缀协议，不接受路径、网址或可执行命令。"""

import json
import os
import resource
import struct
import sys
from pathlib import Path


def _write_all(stream, value):
    view = memoryview(value)
    while view:
        count = stream.write(view[:1048576])
        if not count:
            raise BrokenPipeError()
        view = view[count:]


def _audit(event, args):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
        raise PermissionError("photo_effect_refused")
    if event == "open":
        mode = args[1] or ""
        flags = args[2] or 0
        if any(v in mode for v in "wax+") or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            raise PermissionError("photo_file_write_refused")


def main():
    os.umask(0o077)
    os.nice(10)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    # 原图不落盘；第三方库的输出与协议分开，避免错误文字污染媒体通道。
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    with open(os.devnull, "wb") as null:
        os.dup2(null.fileno(), sys.stdout.fileno())
    sys.addaudithook(_audit)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from homeplane.family_media.photo_codec import PhotoCodecError, render_photo
    from homeplane.family_media.photo_contracts import PhotoWorkRequest
    try:
        length = sys.stdin.buffer.read(4)
        if len(length) != 4 or not 0 < struct.unpack(">I", length)[0] <= 16384:
            raise PhotoCodecError("photo_protocol_invalid")
        count = struct.unpack(">I", length)[0]
        header = sys.stdin.buffer.read(count)
        if len(header) != count:
            raise PhotoCodecError("photo_protocol_invalid")
        request = PhotoWorkRequest.model_validate_json(header)
        payload = sys.stdin.buffer.read(request.input_bytes)
        if len(payload) != request.input_bytes or sys.stdin.buffer.read(1):
            raise PhotoCodecError("photo_protocol_invalid")
        result, encoded = render_photo(payload, request)
        response = json.dumps({"ok": True, "result": result.model_dump(mode="json")}).encode()
        _write_all(output, struct.pack(">I", len(response)) + response)
        _write_all(output, encoded)
    except Exception as error:
        code = error.code if isinstance(error, PhotoCodecError) else "photo_processing_failed"
        response = json.dumps({"ok": False, "code": code}).encode()
        _write_all(output, struct.pack(">I", len(response)) + response)
    finally:
        output.close()


if __name__ == "__main__":
    main()
