"""从公开源码快照构建 CPU 镜像；排除 Git 历史、忽略数据与本机凭据。"""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def source_state(root):
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    dirty = bool(subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip())
    return dict(revision=revision,dirty=dirty)


def stage(friday,arl,output, *, release=False):
    friday_state, arl_state = source_state(friday), source_state(arl)
    lock = json.loads((friday/'docker/runtime-lock.json').read_text())
    if release and (friday_state['dirty'] or arl_state['dirty'] or arl_state['revision'] != lock['arl']['revision']):
        raise ValueError('release_source_not_locked')
    export=json.loads(subprocess.check_output(['python3',str(friday/'scripts/export_publication.py'),'--output',str(output/'Friday')],text=True))
    print(json.dumps(export),flush=True)
    # ARL 只打包运行所需的 Python、协议快照、许可证和构建元数据。
    names=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=arl).decode().split('\0')
    manifest={}
    for name in sorted(set(names)):
        relative=Path(name)
        if not name or not (relative.parts[0]=='arl' or name in ('pyproject.toml','README.md','LICENSE','NOTICE') or name in ('docs/mio-tools.json','docs/service-connections.json')):continue
        source=arl/relative
        if source.is_symlink() or not source.is_file():raise ValueError('unsafe_build_source')
        target=output/'ARL'/relative;target.parent.mkdir(parents=True,exist_ok=True)
        data=source.read_bytes();target.write_bytes(data);manifest[name]=hashlib.sha256(data).hexdigest()
    target=output/'container';target.mkdir()
    for name in ('entrypoint.py','camera_sdk.py','camera-sdk-lock.json'):
        shutil.copy2(ROOT/'docker'/name,target/name)
    shutil.copytree(ROOT/'docker/licenses',target/'licenses')
    shutil.copy2(ROOT/'docker/Dockerfile',output/'Dockerfile')
    subprocess.run(['python3',str(friday/'scripts/audit_publication.py'),'--root',str(output/'Friday')],check=True)
    # 发布锁同时约束 Git 版本和实际打入镜像的 ARL 文件，工作区只能用于本地候选。
    if release and manifest != lock['arl']['files']:
        raise ValueError('arl_build_input_mismatch')
    (output/'build-inputs.json').write_text(json.dumps(dict(schema_version=1,platform='linux/amd64',
        friday=friday_state,arl=arl_state,friday_source_sha256=export['source_sha256'],arl_files=manifest,
        camera_sdk=False,camera_sdk_bootstrap=dict(mode='first_start',
            lock=json.loads((ROOT/'docker/camera-sdk-lock.json').read_text())),private_data_included=False),indent=2)+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--friday',type=Path,default=ROOT)
    parser.add_argument('--arl',type=Path,default=ROOT.parent/'ARL')
    parser.add_argument('--tag',default='friday-linux-cpu:local')
    parser.add_argument('--output',type=Path,help='仅准备全新的公开构建上下文，不构建镜像')
    parser.add_argument('--release',action='store_true',help='要求干净 Git 工作区与固定 ARL 来源')
    parser.add_argument('--camera-sdk',type=Path,help='可选：已取得的固定 Linux x86_64 摄像头 SDK wheel')
    args=parser.parse_args()
    parent=ROOT/'.artifacts/linux-container';parent.mkdir(parents=True,exist_ok=True)
    if args.release and args.camera_sdk:
        parser.error('公开发布输入不打包条件性摄像头 SDK')
    if args.output:
        if args.camera_sdk:
            parser.error('--output 仅支持不含条件性 SDK 的公开上下文')
        args.output.mkdir(parents=True,exist_ok=False)
        stage(args.friday.resolve(),args.arl.resolve(),args.output.resolve(),release=args.release)
        return 0
    with tempfile.TemporaryDirectory(prefix='context-',dir=parent) as temporary:
        context=Path(temporary);stage(args.friday.resolve(),args.arl.resolve(),context,release=args.release)
        if args.camera_sdk is not None:
            wheel=args.camera_sdk
            name='miloco_miot-2026.9.11-py3-none-manylinux_2_28_x86_64.whl'
            if wheel.is_symlink() or wheel.name!=name or hashlib.sha256(wheel.read_bytes()).hexdigest()!='146fc1b51f6434a78e467fff46e9e4e8c5548d38f37378124f5013f41188a6eb':
                raise ValueError('camera_sdk_pin_mismatch')
            shutil.copy2(wheel,context/name)
            receipt=json.loads((context/'build-inputs.json').read_text())
            receipt['camera_sdk']={'file':name,'sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'distribution':'local_only'}
            receipt['camera_sdk_bootstrap']['mode']='disabled'
            (context/'build-inputs.json').write_text(json.dumps(receipt,indent=2)+'\n')
            shutil.copy2(ROOT/'docker/camera-constraints.txt',context/'camera-constraints.txt')
            with (context/'Dockerfile').open('a') as dockerfile:
                dockerfile.write('\nUSER root\nCOPY '+name+' /opt/sdk/'+name+'\n'
                    'COPY camera-constraints.txt /opt/sdk/camera-constraints.txt\n'
                    'RUN --mount=type=cache,target=/root/.cache/uv uv pip install --python /opt/runtime/bin/python '
                    '--constraint /opt/sdk/camera-constraints.txt /opt/sdk/'+name+' && uv pip check --python /opt/runtime/bin/python\n'
                    'USER friday\nENV FRIDAY_CAMERA_SDK=disabled\n')
        shutil.copy2(context/'build-inputs.json',parent/'build-inputs.json')
        return subprocess.run(['docker','build','--platform','linux/amd64','--label','org.opencontainers.image.source=https://github.com/Geeprox/Friday','--label','org.opencontainers.image.revision='+source_state(args.friday)['revision'],'--tag',args.tag,str(context)]).returncode


if __name__=='__main__':raise SystemExit(main())
