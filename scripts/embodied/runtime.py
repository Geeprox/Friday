"""Friday optional runtime: bounded readiness and project-local native build.

Usage: python scripts/embodied/runtime.py --check | --build-native | --build-sound
Credentials and model weights are operator-local data, never packaged in the App.
"""
import argparse
import hashlib
import json
import subprocess
import datetime
import os
import shutil
import uuid
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
                optional_sound=dict(installed=(DATA/'bin/friday-sound').is_file(),
                                    build_command='python3 scripts/embodied/runtime.py --build-sound',
                                    inference_attempted=False),
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


def build_sound():
    """Explicit optional runtime installation; bounded intermediates use the user cache."""
    import fcntl
    project = str(ROOT.resolve())
    digest = hashlib.sha256(project.encode()).hexdigest()[:12]
    run = (Path.home()/'Library/Caches/CodexBuilds.noindex'/f'{ROOT.name}-{digest}'/
           'swift-macos/sound/release'/uuid.uuid4().hex)
    run.mkdir(parents=True, mode=0o700)
    source = ROOT/'native/EmbodiedVision/SoundWorker.swift'
    command = ['xcrun','swiftc','-O','-module-cache-path',str(run/'modules'),str(source),
               '-o',str(run/'friday-sound')]
    manifest = dict(source_project=project,toolchain='Swift/macOS',purpose='optional local sound worker',
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),status='building',
        command=command,source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    with (run/'active.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        (run/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        try:
            result = subprocess.run(command,env={**os.environ,'TMPDIR':str(run)},
                                    text=True,capture_output=True,timeout=180)
            (run/'compile.log').write_text((result.stdout+result.stderr)[-64000:])
            if result.returncode:raise RuntimeError('sound_build_failed')
            output = DATA/'bin';output.mkdir(parents=True,exist_ok=True)
            candidate = output/('.friday-sound-'+uuid.uuid4().hex)
            try:
                shutil.copyfile(run/'friday-sound',candidate)
                candidate.chmod(0o755)
                os.replace(candidate,output/'friday-sound')
            finally:candidate.unlink(missing_ok=True)
            manifest.update(status='built',binary_sha256=hashlib.sha256((output/'friday-sound').read_bytes()).hexdigest(),
                            output=str(output/'friday-sound'))
        except BaseException:
            manifest['status']='failed'
            raise
        finally:
            for name in ('modules',):
                if (run/name).exists():shutil.rmtree(run/name)
            (run/'friday-sound').unlink(missing_ok=True)
            manifest.update(ended_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                retention='bounded compile log and manifest; review in14 days or when superseded',
                temporary_outputs_removed=True)
            (run/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
            fcntl.flock(lock,fcntl.LOCK_UN)
            (run/'active.lock').unlink(missing_ok=True)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--build-native', action='store_true')
    parser.add_argument('--build-sound', action='store_true')
    args = parser.parse_args()
    print(json.dumps(build_sound() if args.build_sound else build() if args.build_native else check(), ensure_ascii=False))
