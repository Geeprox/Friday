"""只验证本轮新建的隔离容器；随机端口和匿名卷，不访问家庭部署。"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


class Sandbox:
    def __init__(self, image):
        self.image=image
        self.owner=uuid.uuid4().hex
        self.container=None
        self.ports={}

    def docker(self,*args,timeout=90):
        return subprocess.check_output(['docker',*args],text=True,timeout=timeout).strip()

    def owned(self):
        if not self.container:
            raise RuntimeError('test_container_not_created')
        label=self.docker('inspect','--format','{{index .Config.Labels "org.friday.verification"}}',self.container)
        if label!=self.owner:
            raise RuntimeError('test_container_owner_changed')

    def create(self):
        # 先解析本地不可变 ID，测试期间即使标签改变也不换镜像。
        image=self.docker('image','inspect','--format','{{.Id}}',self.image)
        self.container=self.docker('create','--name','friday-verify-'+self.owner,
            '--label','org.friday.verification='+self.owner,'--platform','linux/amd64','--init',
            '--memory','6g','--cpus','2','--pids-limit','256','--cap-drop','ALL',
            '--security-opt','no-new-privileges:true','--stop-timeout','60',
            '-e','FRIDAY_CONTAINER_PROFILE=simulator','-v','/opt/friday/data','-v','/opt/friday/.artifacts',
            '-p','127.0.0.1::8740','-p','127.0.0.1::18745',image)
        self.docker('start',self.container)
        self.wait()
        self.refresh_ports()

    def refresh_ports(self):
        for port in (8740,18745):
            mappings=json.loads(self.docker('inspect','--format','{{json .NetworkSettings.Ports}}',self.container))
            rows=mappings[str(port)+'/tcp']
            if len(rows)!=1 or rows[0]['HostIp']!='127.0.0.1':raise RuntimeError('test_port_not_loopback')
            self.ports[port]=int(rows[0]['HostPort'])

    def wait(self):
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            state=json.loads(self.docker('inspect','--format','{{json .State}}',self.container))
            if not state['Running']:raise RuntimeError('test_container_exited')
            if state.get('Health',{}).get('Status')=='healthy':return
            time.sleep(1)
        raise TimeoutError('test_container_health_timeout')

    def execute(self,*args):
        self.owned()
        return self.docker('exec',self.container,*args,timeout=40)

    def fetch(self,port,path='/',*,headers=None,data=None):
        # 随机宿主端口仅用于传输，保留应用原有的严格 Host/Origin 校验。
        headers={'Host':f'127.0.0.1:{port}',**(headers or {})}
        request=urllib.request.Request(f'http://127.0.0.1:{self.ports[port]}'+path,headers=headers,data=data)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request,timeout=10) as response:return response.status,response.read(131072)
        except urllib.error.HTTPError as error:return error.code,error.read(131072)

    def token(self):
        base,token=self.execute('python','/opt/container/entrypoint.py','settings-url').split('/#',1)
        assert base=='http://127.0.0.1:18745'
        return token

    def restart(self):
        self.owned()
        self.docker('stop','--time','60',self.container)
        state=json.loads(self.docker('inspect','--format','{{json .State}}',self.container))
        assert state['ExitCode']==0 and not state['OOMKilled']
        self.docker('start',self.container)
        self.wait()
        self.refresh_ports()

    def close(self):
        if self.container:
            self.owned()
            # 仅回收本轮拥有的容器及其匿名卷，没有 Compose 项目或家庭卷入口。
            self.docker('rm','--force','--volumes',self.container)
            self.container=None


def verify(box):
    health=json.loads(box.execute('python','/opt/container/entrypoint.py','health'))
    assert health['healthy'] is True and health['hardware_verified'] is False and health['friday']=='simulator'
    status,raw=box.fetch(8740,'/api/doctor')
    assert status==200 and json.loads(raw)['mode']=='simulator'
    assert box.fetch(8740)[0]==200
    assert box.fetch(8740,headers={'Host':'untrusted.invalid'})[0]==400
    token=box.token()
    assert box.fetch(18745)[0]==200
    assert box.fetch(18745,'/api/status')[0]==401
    auth={'Authorization':'Bearer '+token}
    status,raw=box.fetch(18745,'/api/status',headers=auth)
    settings=json.loads(raw)
    assert status==200 and settings['apple'] and all(x['state']=='unsupported' for x in settings['apple'])
    assert token.encode() not in raw
    for header in ({'Origin':'https://untrusted.invalid'},{'Host':'untrusted.invalid'}):
        assert box.fetch(18745,'/api/status',headers={**auth,**header})[0]==403
    dependencies={}
    for name,python,imports in (
        ('main','/opt/runtime/bin/python','import av,cv2,numpy,sherpa_onnx,onnxruntime'),
        ('vision','/opt/vision/bin/python','import cv2,onnxruntime'),
        ('separation','/opt/separation/bin/python','import onnxruntime,soundfile')):
        dependencies[name]=box.execute(python,'-B','-c',imports+';assert "CPUExecutionProvider" in onnxruntime.get_available_providers();print(onnxruntime.__version__)')
    temporary=1 if settings['scheduler']['max_parallel']!=1 else 2
    status,raw=box.fetch(18745,'/api/scheduler',headers={**auth,'Origin':'http://127.0.0.1:18745','Content-Type':'application/json'},data=json.dumps({'max_parallel':temporary}).encode())
    assert status==200 and json.loads(raw)['saved'] is True
    box.restart()
    current=box.token()
    assert current!=token and box.fetch(18745,'/api/status',headers=auth)[0]==401
    status,raw=box.fetch(18745,'/api/status',headers={'Authorization':'Bearer '+current})
    assert status==200 and json.loads(raw)['scheduler']['max_parallel']==temporary
    return dict(passed=True,friday_http='simulator',arl_socket='healthy',settings_http='authenticated',
        foreign_origin_and_host='rejected',apple_services='unsupported',cpu_dependencies=dependencies,
        restart=dict(graceful_exit_code=0,settings_persisted=True,old_token_rejected=True),
        model_inference_attempted=False,hardware_verified=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    box=Sandbox(args.image)
    try:
        box.create()
        result=verify(box)
    finally:
        box.close()
    result['isolated_resources_removed']=True
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
