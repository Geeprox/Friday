"""Friday optional runtime: bounded readiness and project-local native build.

Usage: python scripts/embodied/runtime.py --check | --build-native
Credentials and model weights are operator-local data, never packaged in the App.
"""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT/'data/embodied'


def check():
    expected = ['runtime/bin/python', 'bin/friday-vision', 'models/yolov8n.onnx',
                'models/silero_vad.onnx', 'models/sensevoice-int8/model.int8.onnx',
                'models/sensevoice-int8/tokens.txt', 'models/decision/rl_agent_config.json',
                'private/config.json', 'private/miot-session.json', 'private/viewer-key']
    missing = [p for p in expected if not (DATA/p).is_file()]
    return dict(ready=not missing, missing=missing,
                quiet=(DATA/'private/voice-paused').exists(), camera_opened=False,
                microphone_opened=False, inference_attempted=False)


def build():
    output = DATA/'bin'
    output.mkdir(parents=True, exist_ok=True)
    cache = ROOT/'.artifacts/embodied-migration/clang-cache'
    cache.mkdir(parents=True, exist_ok=True)
    subprocess.run(['xcrun', 'swiftc', '-O', '-module-cache-path', str(cache),
                    str(ROOT/'native/EmbodiedVision/VisionWorker.swift'), '-o', str(output/'friday-vision')], check=True)
    subprocess.run(['xcrun', 'clang', '-dynamiclib', '-O2',
                    str(ROOT/'native/EmbodiedVision/LocalCameraSockets.c'), '-o', str(output/'camera-sockets.dylib')], check=True)
    return dict(native={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--build-native', action='store_true')
    args = parser.parse_args()
    print(json.dumps(build() if args.build_native else check(), ensure_ascii=False))
