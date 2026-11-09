"""Private pipe worker. Only this process imports the optional ONNX scene model."""

import base64
import json
import os
import re
import sys

from .scene_protocol import MAX_JPEG, MAX_REQUEST, scene_result, source_context


def open_detector(model):
    # Set before importing OpenCV/ORT. No files, network, model downloads or media logs.
    os.environ['OPENCV_IO_MAX_IMAGE_PIXELS'] = str(4096 * 4096)
    from .scene_detector import SceneDetector

    return SceneDetector(model)


def serve(reader, writer, model, *, detector_factory=open_detector):
    """The factory is trusted test injection; it cannot be selected by a message."""
    detector, nonce, sequence = None, None, 0
    while True:
        raw = reader.readline(MAX_REQUEST + 1)
        if not raw or len(raw) > MAX_REQUEST or not raw.endswith(b'\n'):
            return
        try:
            request = json.loads(raw)
            if (not isinstance(request, dict)
                    or set(request) != {'v', 'nonce', 'sequence', 'action', 'context', 'value'}
                    or type(request['v']) is not int or request['v'] != 1
                    or type(request['sequence']) is not int or request['sequence'] != sequence + 1
                    or not isinstance(request['nonce'], str)
                    or not re.fullmatch('[a-f0-9]{32}', request['nonce'])
                    or nonce is not None and request['nonce'] != nonce):
                return
            nonce, sequence = request['nonce'], request['sequence']
            action, value, context = (request[k] for k in ('action', 'value', 'context'))
            if action == 'init' and sequence == 1 and value == {} and context is None:
                try:
                    detector = detector_factory(model)
                except Exception:
                    result, reason = None, 'scene_model_unavailable'
                else:
                    result, reason = {}, None
            elif action == 'frame' and detector is not None:
                source_context(context)
                if (not isinstance(value, dict) or set(value) != {'jpeg'}
                        or not isinstance(value['jpeg'], str)
                        or len(value['jpeg']) > (MAX_JPEG + 2) // 3 * 4):
                    return
                jpeg = base64.b64decode(value['jpeg'], validate=True)
                if not 0 < len(jpeg) <= MAX_JPEG:
                    return
                try:
                    result = scene_result(detector.detect(jpeg))
                except Exception:
                    result, reason = None, 'scene_frame_unavailable'
                else:
                    reason = None
            else:
                return
            reply = dict(v=1, nonce=nonce, sequence=sequence, action=action, context=context,
                         ok=reason is None, result=result, reason=reason)
            writer.write((json.dumps(reply, separators=(',', ':'), allow_nan=False) + '\n').encode())
            writer.flush()
            if reason is not None:
                return
        except (ValueError, TypeError, KeyError, OSError):
            return


if __name__ == '__main__':
    if len(sys.argv) == 2:
        serve(sys.stdin.buffer, sys.stdout.buffer, sys.argv[1])
