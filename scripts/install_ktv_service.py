"""Install/update this user's existing KTV launch agent with on-demand sockets."""
from __future__ import annotations
import argparse,json,os,plistlib,shutil,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from homeplane.ktv_service import local_host
from homeplane.ktv_startup import definition


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',type=local_host,required=True)
    parser.add_argument('--plan',action='store_true')
    args=parser.parse_args()
    if args.host=='127.0.0.1':parser.error('explicit_lan_host_required')
    for name in ('config.json','nas.json'):
        p=ROOT/'data/ktv'/name
        if p.resolve()!=p or p.stat().st_mode&0o077:parser.error('private_configuration_required')
    target=Path.home()/'Library/LaunchAgents/local.friday.ktv.plist'
    spec=definition(ROOT,args.host)
    if args.plan:
        print(json.dumps({'label':spec['Label'],'existing':target.is_file(),'socket_count':2,'port':8769,'no_new_remote_api':True}));return
    backup=ROOT/'.artifacts/ktv-startup';backup.mkdir(exist_ok=True,mode=0o700)
    if target.exists():shutil.copy2(target,backup/('launch-agent-before-'+str(time.time_ns())+'.plist'))
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
        previous=sorted(backup.glob('launch-agent-before-*.plist'))
        if previous:
            shutil.copy2(previous[-1],target)
            for _ in range(10):
                if subprocess.run(['launchctl','bootstrap',domain,str(target)],capture_output=True).returncode==0:break
                time.sleep(.5)
        raise
    print(json.dumps({'installed':True,'system_owned_sockets':True,'audio_started':False}))

if __name__=='__main__':main()
