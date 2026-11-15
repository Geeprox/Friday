"""CPU 容器的启动、健康检查与正常停止；默认只运行隔离示例。"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT=Path('/opt/friday')
sys.path.insert(0,str(ROOT))
PYTHON='/opt/runtime/bin/python'
ARL_STATE=Path(os.environ.get('XDG_STATE_HOME','/opt/friday/data/state'))/'arl'
URL_FILE=ARL_STATE/'linux-settings-url.txt'


def layout():
    os.umask(0o077)
    data=Path(os.environ['FRIDAY_EMBODIED_DATA'])
    data.mkdir(parents=True,exist_ok=True)
    for name,target in [('runtime','/opt/runtime'),('vision-runtime','/opt/vision')]:
        link=data/name
        if not link.exists():link.symlink_to(target,target_is_directory=True)
        if link.resolve()!=Path(target):raise RuntimeError('runtime_path_conflict')
    cache=ROOT/'.artifacts/ktv-audio/separation';cache.mkdir(parents=True,exist_ok=True)
    link=cache/'cpu-runtime'
    if not link.exists():link.symlink_to('/opt/separation',target_is_directory=True)
    if link.resolve()!=Path('/opt/separation'):raise RuntimeError('separation_path_conflict')
    subprocess.run([PYTHON,'scripts/embodied/runtime.py','--build-portable'],check=True,stdout=subprocess.DEVNULL)
    subprocess.run([PYTHON,'scripts/embodied/runtime.py','--build-sound'],check=True,stdout=subprocess.DEVNULL)


def settings():
    from arl.linux_settings import SettingsServer
    server=SettingsServer(ARL_STATE,18745)
    url=server.origin+'/#'+server.token
    # 私有访问链接不写容器日志；操作者使用 settings-url 子命令读取。
    URL_FILE.write_text(url+'\n');URL_FILE.chmod(0o600)
    def stop(*_):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:server.serve_forever()
    finally:
        server.server_close();URL_FILE.unlink(missing_ok=True)


def web():
    import asyncio
    import sqlite3
    import uvicorn
    from homeplane.api import create_app
    from homeplane.simulator import Simulator
    async def run():
        profile=os.environ.get('FRIDAY_CONTAINER_PROFILE','simulator')
        if profile=='household':
            from homeplane.ha_configured import build
            from homeplane.settings import SettingsService
            from homeplane.people import PeopleCatalog
            state=ROOT/'data/household'
            if not (state/'use-ha-source').is_file():raise RuntimeError('household_source_not_imported')
            settings_service=SettingsService(state/'settings.json')
            adapter=await build(settings_service.path)
            # 在同一事件循环恢复已授权来源；连接失败不退回模拟家庭。
            people=PeopleCatalog(sqlite3.connect(state/'people.sqlite',check_same_thread=False))
            app=create_app(adapter,db_path=state/'traces.sqlite',settings_service=settings_service,
                people_catalog=people,background_enabled=True,console_port=8740)
        elif profile=='simulator':app=create_app(Simulator(),console_port=8740)
        else:raise ValueError('unknown_container_profile')
        await uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8740,
            proxy_headers=False,access_log=False)).serve()
    asyncio.run(run())


def health():
    from arl.client import Client
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8740/api/doctor',timeout=4) as response:
        if response.status!=200:raise RuntimeError('friday_unavailable')
        doctor=json.loads(response.read(32768))
        expected='ha-read-only' if os.environ.get('FRIDAY_CONTAINER_PROFILE')=='household' else 'simulator'
        if doctor.get('mode')!=expected:raise RuntimeError('unexpected_friday_mode')
        if expected=='ha-read-only' and doctor.get('ha_ingress',{}).get('transport')!='online':raise RuntimeError('ha_offline')
    url=URL_FILE.read_text().strip();base,token=url.split('/#',1)
    request=urllib.request.Request(base+'/api/status',headers={'Authorization':'Bearer '+token})
    with opener.open(request,timeout=4) as response:
        value=json.loads(response.read(32768))
        if not value['apple'] or any(item['state']!='unsupported' for item in value['apple']):raise RuntimeError('apple_boundary_changed')
    result=Client(ARL_STATE,'settings').rpc('health',{})
    if result.get('live') is not True or result.get('ready') is not True:raise RuntimeError('arl_not_ready')
    print(json.dumps({'healthy':True,'friday':expected,'arl_socket':True,'settings':'ready','hardware_verified':False}))


def supervise():
    layout();children=[];stopping=threading.Event();failure=False
    def stop(*_):stopping.set()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        for command in ([PYTHON,'-m','arl.linux_host','serve'],[PYTHON,__file__,'settings'],[PYTHON,__file__,'web']):
            children.append(subprocess.Popen(command,cwd=ROOT,start_new_session=True))
        # 应用仍只监听回环；独立容器网络的转发保留原始 Host/Origin，不信任代理头。
        address=socket.gethostbyname(socket.gethostname())
        if address.startswith('127.'):raise RuntimeError('container_interface_missing')
        for port in (8740,18745):
            children.append(subprocess.Popen(['socat',f'TCP4-LISTEN:{port},bind={address},reuseaddr,fork,max-children=16',
                f'TCP4:127.0.0.1:{port}'],start_new_session=True))
        print(json.dumps({'started':True,'profile':os.environ.get('FRIDAY_CONTAINER_PROFILE','simulator'),'settings_url_command':'settings-url'}),flush=True)
        while not stopping.wait(.5):
            if any(p.poll() is not None for p in children):failure=True;break
    finally:
        for process in reversed(children):
            if process.poll() is None:
                try:os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
        deadline=time.monotonic()+45
        for process in reversed(children):
            try:process.wait(timeout=max(.1,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.wait();failure=True
    return int(failure)


if __name__=='__main__':
    command=sys.argv[1] if len(sys.argv)>1 else 'serve'
    try:
        if command=='serve':raise SystemExit(supervise())
        elif command=='health':health()
        elif command=='settings':settings()
        elif command=='web':web()
        elif command=='settings-url':print(URL_FILE.read_text().strip())
        else:raise ValueError('unknown_container_command')
    except Exception as error:
        print(json.dumps({'error':type(error).__name__}),file=sys.stderr);raise SystemExit(1)
