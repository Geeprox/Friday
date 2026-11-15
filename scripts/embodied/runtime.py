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
import sys
import platform
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
DATA = Path(os.environ.get('FRIDAY_EMBODIED_DATA', ROOT/'data/embodied')).absolute()


def environment_missing(python, modules):
    """仅探测依赖元数据，不导入模型库或启动推理。"""
    if not python.is_file():
        return list(modules)
    code = ("import importlib.util,json; print(json.dumps([name for name in "
            +repr(modules)+" if importlib.util.find_spec(name) is None]))")
    try:
        result = subprocess.run([str(python), '-I', '-c', code], check=True,
                                capture_output=True, text=True, timeout=10)
        missing = json.loads(result.stdout)
        if not isinstance(missing, list) or not set(missing) <= set(modules):
            return list(modules)
        return missing
    except (OSError, ValueError, subprocess.SubprocessError):
        return list(modules)


def check(profile='full'):
    expected = ['runtime/bin/python', 'bin/friday-vision', 'models/yolov8n.onnx',
                'private/config.json', 'private/miot-session.json', 'private/viewer-key', 'private/control-key']
    linux = sys.platform.startswith('linux')
    modules = ['miot', 'cv2', 'numpy', 'av', 'onnxruntime']
    if profile == 'full':
        modules += ['sherpa_onnx']
        expected += ['models/silero_vad.onnx', 'models/sensevoice-int8/model.int8.onnx',
                     'models/sensevoice-int8/tokens.txt']
        if sys.platform == 'darwin':
            expected += ['models/decision/rl_agent_config.json']
    if linux:
        from homeplane.embodied.portable_models import ONNX_MODELS
        expected += ['vision-runtime/bin/python'] + ['models/opencv-zoo/'+n for n in ONNX_MODELS]
    missing = [p for p in expected if not (DATA/p).is_file()]
    dependencies = environment_missing(DATA/'runtime/bin/python', modules)
    vision_cpu = None
    if linux:
        dependencies += ['vision:'+m for m in environment_missing(DATA/'vision-runtime/bin/python',
                         ['numpy', 'PIL', 'onnxruntime', 'cv2'])]
        vision_cpu = dict(ready=not any(d.startswith('vision:') for d in dependencies),
                          avx_required=False, backend='onnx_cpu')
        if profile == 'full':
            dependencies += [p for p in ('espeak-ng', 'ffmpeg') if shutil.which(p) is None]
    return dict(ready=not missing and not dependencies, readiness_scope='prerequisites_only',
                missing=missing, missing_dependencies=dependencies, profile=profile, platform=sys.platform,
                architecture=platform.machine(),
                vision_backend='onnx_cpu' if linux else 'apple_vision',
                vision_cpu=vision_cpu,
                unsupported=['macos_desktop_app', 'apple_system_services'] if linux else [],
                optional_sound=dict(installed=(DATA/'bin/friday-sound').is_file(),
                                    build_command='python3 scripts/embodied/runtime.py --build-sound',
                                    inference_attempted=False),
                optional_review=dict(backend='transformers_cpu' if linux else 'mlx',
                                     performance_verified=False),
                optional_motor=dict(backend='fixed_linux_sdk' if linux else 'fixed_macos_sdk',
                                    hardware_verified=False),
                optional_separation=dict(backend='onnx_cpu' if linux else 'mlx',
                                         performance_verified=False),
                quiet=(DATA/'private/voice-paused').exists(), camera_opened=False,
                microphone_opened=False, inference_attempted=False, hardware_verified=False)


def build():
    if sys.platform != 'darwin':
        raise ValueError('apple_vision_requires_macos_use_build_portable')
    output = DATA/'bin'
    output.mkdir(parents=True, exist_ok=True)
    cache = ROOT/'.artifacts/embodied-migration/clang-cache'
    cache.mkdir(parents=True, exist_ok=True)
    subprocess.run(['xcrun', 'swiftc', '-O', '-module-cache-path', str(cache),
                    str(ROOT/'native/EmbodiedVision/VisionWorker.swift'), '-o', str(output/'friday-vision')], check=True)
    subprocess.run(['xcrun', 'clang', '-dynamiclib', '-O2',
                    str(ROOT/'native/EmbodiedVision/LocalCameraSockets.c'), '-o', str(output/'camera-sockets.dylib')], check=True)
    return dict(native={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()})


def build_portable(*, sound=False):
    """只安装项目本地入口，不下载模型、不启动摄像头或识别进程。"""
    if not sys.platform.startswith('linux'):
        raise ValueError('portable_runtime_requires_linux')
    python = DATA/'vision-runtime/bin/python'
    if not python.is_file():
        raise ValueError('portable_python_not_installed')
    if any(c in str(python) for c in ('\n', '\r', ' ')):
        raise ValueError('portable_python_shebang_path_invalid')
    output = DATA/'bin'
    output.mkdir(parents=True, exist_ok=True)
    name = 'friday-sound' if sound else 'friday-vision'
    module = 'portable_sound' if sound else 'portable_vision'
    target = output/name
    temporary = output/('.'+name+'-'+uuid.uuid4().hex)
    source = (f'#!{python}\n'
              'import sys\n'
              f'sys.path.insert(0, {str(ROOT)!r})\n'
              f'from homeplane.embodied.{module} import main\n'
              'raise SystemExit(main())\n')
    try:
        with temporary.open('x') as stream:
            stream.write(source)
        temporary.chmod(0o755)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(backend='mediapipe' if sound else 'onnx_cpu', installed=True, inference_attempted=False,
                camera_opened=False, model_downloaded=False)


def build_sound():
    """Explicit optional runtime installation; bounded intermediates use the user cache."""
    if sys.platform.startswith('linux'):
        return build_portable(sound=True)
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
    parser.add_argument('--build-portable', action='store_true')
    parser.add_argument('--profile', choices=['full', 'gestures'], default='full')
    args = parser.parse_args()
    if sum((args.build_sound, args.build_native, args.build_portable, args.check)) > 1:
        parser.error('一次选择一种操作')
    print(json.dumps(build_portable() if args.build_portable else build_sound() if args.build_sound
                     else build() if args.build_native else check(args.profile), ensure_ascii=False))
