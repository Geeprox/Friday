"""CPU 复核子进程只消费已授权 JPEG，不联网、不保存图像、不控制设备。"""

import argparse
import base64
import gc
import io
import json
import math
import os
import resource
import sys
from contextlib import redirect_stdout
from pathlib import Path

from .cpu_review import MEMORY_LIMIT, verify
from .temporal_model import WINDOW_SPAN, VisualResult
from .temporal_worker import review_prompt


def images_from_request(request):
    from PIL import Image
    if (type(request) is not dict or set(request) not in ({'frames'}, {'frames', 'review_prone'})
            or 'review_prone' in request and request['review_prone'] is not True
            or type(request['frames']) is not list or len(request['frames']) != 4):
        raise ValueError('invalid_window')
    images, stamps = [], []
    for frame in request['frames']:
        if (type(frame) is not dict or set(frame) != {'t', 'jpeg'}
                or type(frame['t']) not in (int, float) or not math.isfinite(frame['t'])
                or not 0 <= frame['t'] <= WINDOW_SPAN or stamps and frame['t'] <= stamps[-1]
                or type(frame['jpeg']) is not str):
            raise ValueError('invalid_window')
        raw = base64.b64decode(frame['jpeg'], validate=True)
        if not 0 < len(raw) <= 512*1024:
            raise ValueError('invalid_frame')
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != 'JPEG' or image.width*image.height > 20_000_000:
                raise ValueError('invalid_frame')
            image = image.convert('RGB')
            image.thumbnail((256, 256))
            images.append(image.copy())
        stamps.append(frame['t'])
    return images, stamps


def main(model):
    from .camera_guardian import exit_with_owner
    exit_with_owner(int(os.environ.get('FRIDAY_REVIEW_OWNER_PID', '0')))
    os.environ['HF_HUB_OFFLINE'] = os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    verify(model, hashes=True)
    # 限制整个子进程的地址空间；不足时退出，不占用无限内存或自动转云端。
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
    with redirect_stdout(sys.stderr):
        import torch
        from transformers import AutoProcessor, AutoModelForMultimodalLM
        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
        processor = AutoProcessor.from_pretrained(str(model), local_files_only=True, trust_remote_code=False)
        loaded = AutoModelForMultimodalLM.from_pretrained(str(model), local_files_only=True,
                    trust_remote_code=False, dtype=torch.float32, attn_implementation='eager').eval()
    print(json.dumps(dict(version=1, ready=True)), flush=True)
    for line in iter(lambda: sys.stdin.buffer.readline(3*1024**2+1), b''):
        if len(line) > 3*1024**2 or not line.endswith(b'\n'):
            raise ValueError('invalid_window')
        request = json.loads(line)
        images, stamps = images_from_request(request)
        content = [{'type': 'text', 'text': review_prompt(request.get('review_prone') is True)}]
        for stamp, image in zip(stamps, images):
            content += [{'type': 'text', 'text': f't={stamp:.3f}s'}, {'type': 'image', 'image': image}]
        with redirect_stdout(sys.stderr), torch.inference_mode():
            inputs = processor.apply_chat_template([{'role': 'user', 'content': content}],
                tokenize=True, add_generation_prompt=True, enable_thinking=False,
                return_dict=True, return_tensors='pt')
            outputs = loaded.generate(**inputs, max_new_tokens=128, do_sample=False)
            text = processor.decode(outputs[0][inputs['input_ids'].shape[-1]:], skip_special_tokens=True).strip()
        result = VisualResult.model_validate_json(text)
        print(json.dumps(dict(version=1, result=result.model_dump(),
                              peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024)), flush=True)
        del images, stamps, content, image, inputs, outputs, text, result, line, request
        gc.collect()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    main(parser.parse_args().model)
