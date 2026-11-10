"""Build a separate debug visual fixture; never install, launch or stop Friday.

The opt-in preview bundle bypasses the household service before its delegate
does any filesystem/registration work. Open the returned bundle through the UI.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess

REPO=Path(__file__).resolve().parents[3]
NATIVE=REPO/'native/FridayDesktop'

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build',action='store_true')
    parser.add_argument('--hold',action='store_true')
    parser.add_argument('--task-id',default='manual-design-preview')
    args=parser.parse_args()
    if not args.task_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in args.task_id):
        parser.error('invalid task id')
    slug=REPO.name+'-'+hashlib.sha256(str(REPO.resolve()).encode()).hexdigest()[:12]
    root=Path.home()/'Library/Caches/CodexBuilds.noindex'/slug/'swift-macos/design-preview'/args.task_id
    bundle=root/'Friday Design Preview.app'
    command=['xcrun','swift','build','--package-path',str(NATIVE),'--scratch-path',str(root/'swift-build'),
             '--cache-path',str(root/'swift-cache'),'--config-path',str(root/'swift-config'),
             '--security-path',str(root/'swift-security'),'--disable-automatic-resolution',
             '-Xswiftc','-module-cache-path','-Xswiftc',str(root/'module-cache'),'-c','debug']
    if not args.build:
        print(json.dumps({'root':str(root),'bundle':str(bundle),'command':command,'launch':False,'install':False},indent=2));return
    root.mkdir(parents=True,exist_ok=True)
    with (root/'active.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        manifest=root/'manifest.json'
        if manifest.exists() and json.loads(manifest.read_text()).get('source_project')!=str(REPO):
            raise ValueError('foreign preview directory')
        state={'source_project':str(REPO),'toolchain':'SwiftPM macOS debug','purpose':'Isolated no-service visual fixture',
               'created_at':datetime.datetime.now(datetime.UTC).isoformat(),'status':'building','pid':os.getpid(),
               'command':command,'bundle':str(bundle),'retention':'Remove compiled fixture/bundle after visual acceptance; keep small receipt.'}
        manifest.write_text(json.dumps(state,indent=2)+'\n')
        try:
            env={**os.environ,'TMPDIR':str(root/'temporary'),'CLANG_MODULE_CACHE_PATH':str(root/'clang-cache'),
                 'SWIFTPM_MODULECACHE_OVERRIDE':str(root/'module-cache')}
            (root/'temporary').mkdir(exist_ok=True)
            output=subprocess.run(command,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            (root/'build.log').write_text(output.stdout[-100000:])
            if output.returncode:
                print('\n'.join(line for line in output.stdout.splitlines() if 'error:' in line and len(line)<1000));raise RuntimeError('preview build failed')
            bin_path=subprocess.check_output([*command,'--show-bin-path'],env=env,text=True).strip()
            executable=Path(bin_path)/'Friday'
            assert executable.is_file() and executable.resolve().is_relative_to(root)
            if bundle.exists():shutil.rmtree(bundle)
            (bundle/'Contents/MacOS').mkdir(parents=True)
            (bundle/'Contents/Resources').mkdir()
            shutil.copy2(executable,bundle/'Contents/MacOS/Friday')
            shutil.copy2(NATIVE/'Resources/AppIcon/Friday.icns',bundle/'Contents/Resources/Friday.icns')
            info={'CFBundleIdentifier':'com.friday.home.designpreview','CFBundleExecutable':'Friday',
                  'CFBundleName':'Friday Design Preview','CFBundlePackageType':'APPL','CFBundleVersion':'1',
                  'CFBundleShortVersionString':'0.0.0','CFBundleIconFile':'Friday.icns',
                  'LSMinimumSystemVersion':'14.0','NSPrincipalClass':'NSApplication','FridayDesignPreviewScene':'home'}
            (bundle/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
            subprocess.run(['codesign','--force','--deep','--sign','-',str(bundle)],check=True,capture_output=True)
            state.update(status='ready',actual_binary=str(executable.resolve()),
                         binary_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
                         swift_sources={str(p.relative_to(REPO)):hashlib.sha256(p.read_bytes()).hexdigest()
                                        for d in ['App','Views','Models','Services','Support'] for p in (NATIVE/d).rglob('*.swift')})
            manifest.write_text(json.dumps(state,indent=2)+'\n')
            print('Preview ready:',bundle,flush=True)
            print('Actual compiler output:',executable.resolve(),flush=True)
            if args.hold:
                while True:signal.pause()
        except KeyboardInterrupt:
            pass
        finally:
            state.update(status='stopped',ended_at=datetime.datetime.now(datetime.UTC).isoformat())
            manifest.write_text(json.dumps(state,indent=2)+'\n')
            fcntl.flock(lock,fcntl.LOCK_UN)
    (root/'active.lock').unlink(missing_ok=True)

if __name__=='__main__':main()
