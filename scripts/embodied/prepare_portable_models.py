"""显式下载固定视觉模型；逐项校验后保存，不启动模型或家庭服务。"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from homeplane.embodied.paths import MODELS as MODEL_ROOT
from homeplane.embodied.portable_models import MODELS, ONNX_MODELS


def verify_file(path, digest, size):
    """拒绝替换文件和软链接；所有清单文件均校验固定 SHA-256。"""
    if path.is_symlink() or path.stat().st_size != size:
        raise ValueError('portable_model_pin_mismatch')
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
            raise ValueError('portable_model_pin_mismatch')


def prepare(folder, *, download=False, manifest=MODELS):
    folder = Path(folder)
    if folder.is_symlink():
        raise ValueError('portable_model_directory_invalid')
    if download:
        folder.mkdir(parents=True, exist_ok=True)
    missing = []
    for name, (url, digest, size) in manifest.items():
        try:
            verify_file(folder/name, digest, size)
            continue
        except FileNotFoundError:
            pass
        if not download:
            missing.append(name)
            continue
        # 现存且摘要不匹配的文件由 verify 拒绝，不覆盖操作者提供的其他模型。
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as stream:
                temporary = Path(stream.name)
                count, observed = 0, hashlib.sha256()
                with urllib.request.urlopen(url, timeout=30) as response:
                    while block := response.read(1024*1024):
                        count += len(block)
                        if count > size:
                            raise ValueError('portable_model_pin_mismatch')
                        observed.update(block)
                        stream.write(block)
                if count != size or observed.hexdigest() != digest:
                    raise ValueError('portable_model_pin_mismatch')
                stream.flush()
                os.fsync(stream.fileno())
            # 不覆盖并发准备进程刚写入的文件。
            try:
                os.link(temporary, folder/name)
            except FileExistsError:
                verify_file(folder/name, digest, size)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return dict(ready=not missing, missing=missing, inference_attempted=False, camera_opened=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--sound', action='store_true', help='可选 MediaPipe YAMNet 声音模型')
    parser.add_argument('--review', action='store_true', help='可选 CPU 复核模型，下载约 4.6 GB')
    args = parser.parse_args()
    if args.review:
        revision = '15852e8c16360a2fea060d615a32b45270f8a8fc'
        pins = json.loads((Path(__file__).resolve().parents[2]/'homeplane/embodied/cpu-review-model.json').read_text())
        manifest = {name: (f'https://huggingface.co/Qwen/Qwen3.5-2B/resolve/{revision}/{name}',
                           pin['sha256'], pin['size']) for name, pin in pins.items()}
        result = prepare(MODEL_ROOT/'qwen3.5-2b-cpu', download=args.download, manifest=manifest)
    else:
        result = (prepare(MODEL_ROOT/'mediapipe', download=args.download,
                          manifest={'yamnet.tflite': MODELS['yamnet.tflite']}) if args.sound else
                  prepare(MODEL_ROOT/'opencv-zoo', download=args.download, manifest=ONNX_MODELS))
    print(json.dumps(result, ensure_ascii=False))
