"""Install/update this user's existing KTV launch agent with on-demand sockets."""
from __future__ import annotations
import argparse,json,os,plistlib,shutil,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from homeplane.ktv_service import local_host
from homeplane.ktv_startup import definition, installed_workspace


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',type=local_host,required=True)
    parser.add_argument('--plan',action='store_true')
    parser.add_argument('--installed-root',type=Path,help='Reuse the installation that owns this registered worktree')
    args=parser.parse_args()
    if args.host=='127.0.0.1':parser.error('explicit_lan_host_required')
    try:
        installed=installed_workspace(ROOT,args.installed_root)
    except ValueError as error:
        parser.error(str(error))
    for name in ('config.json','nas.json'):
        p=installed/'data/ktv'/name
        if p.resolve()!=p or not p.is_file() or p.stat().st_uid!=os.getuid() or p.stat().st_mode&0o077:
            parser.error('private_configuration_required')
    target=Path.home()/'Library/LaunchAgents/local.friday.ktv.plist'
    spec=definition(ROOT,args.host,installed_root=installed)
    if args.plan:
        print(json.dumps({'label':spec['Label'],'existing':target.is_file(),'socket_count':2,'port':8769,
                          'reuses_installed_workspace':installed!=ROOT,'no_new_remote_api':True}));return
    backup=installed/'.artifacts/ktv-startup';backup.mkdir(exist_ok=True,mode=0o700)
    previous=None
    if target.exists():
        previous=backup/('launch-agent-before-'+str(time.time_ns())+'.plist')
        shutil.copy2(target,previous)
    service=f'gui/{os.getuid()}/local.friday.ktv';domain=f'gui/{os.getuid()}'
    loaded=subprocess.run(['launchctl','print',service],capture_output=True).returncode==0
    if loaded:
        subprocess.run(['launchctl','bootout',service],check=True)
        deadline=time.monotonic()+30
        while subprocess.run(['launchctl','print',service],capture_output=True).returncode==0:
            if time.monotonic()>=deadline:raise ValueError('previous_service_still_exiting')
            time.sleep(.2)
        time.sleep(.5)
    target.parent.mkdir(parents=True,exist_ok=True)
    pending=target.with_suffix('.pending');pending.write_bytes(plistlib.dumps(spec));pending.chmod(0o600);pending.replace(target)
    try:
        subprocess.run(['launchctl','bootstrap',domain,str(target)],check=True)
    except Exception:
        if previous is not None:
            shutil.copy2(previous,target)
            for _ in range(10):
                if subprocess.run(['launchctl','bootstrap',domain,str(target)],capture_output=True).returncode==0:break
                time.sleep(.5)
        raise
    print(json.dumps({'installed':True,'system_owned_sockets':True,'audio_started':False,
                      'rollback_plist':str(previous) if previous is not None else None}))

if __name__=='__main__':main()
