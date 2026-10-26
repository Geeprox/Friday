"""Install one locally built TV demo APK through TCL's normal prompt.

T-Cast INSTALL framing follows pengchujin/aerial-tv/tools/tcast-push.py.
"""
import argparse
import hashlib
import http.server
import json
import math
import secrets
import socket
import threading
import time
from pathlib import Path
from tcl_diagnostic_session import Session, HOST

p=argparse.ArgumentParser()
p.add_argument('apk')
p.add_argument('--package',required=True)
p.add_argument('--activity',required=True)
p.add_argument('--label',default='TV Diagnostics')
p.add_argument('--version',default='1')
p.add_argument('--no-launch',action='store_true',help='Update the installed viewer without switching television content')
args=p.parse_args()
apk=Path(args.apk).resolve(strict=True)
if args.package!='local.tv.camera' or args.activity!='local.tv.camera.MainActivity':raise ValueError('viewer_only')
if not apk.is_relative_to(Path(__file__).resolve().parents[2]/'.artifacts/embodied-migration/tv'):raise ValueError('project_build_required')
if '>>' in args.label or any(ord(c)<32 for c in args.label):
    raise ValueError('Invalid label')
body=apk.read_bytes()
token=secrets.token_urlsafe(24)
request_path='/'+token+'/tv-diagnostics.apk'

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path!=request_path or self.client_address[0]!=HOST:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type','application/vnd.android.package-archive')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        print(json.dumps({'apk_served_bytes':len(body)}),flush=True)
    def log_message(self,*_): pass

# Ask the OS which local interface routes to the explicitly selected TV.
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
    route.connect((HOST, 6553))
    local_host = route.getsockname()[0]
server=http.server.ThreadingHTTPServer((local_host,0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
url=f'http://{local_host}:{server.server_port}'+request_path
session=Session()
path_md5=hashlib.md5(url.encode()).hexdigest()
command='>>'.join(['223','INSTALL',args.package,args.label,url,'2',args.version,
                   str(max(1,math.ceil(len(body)/1048576))),'','','',path_md5])
session.send(command)
session.sock.settimeout(1)
deadline=time.monotonic()+120
last_beat=time.monotonic()
outcome='timeout'
try:
    while time.monotonic()<deadline:
        try:
            reply=session.receive()
        except socket.timeout:
            reply=''
        f=reply.split('>>')
        if len(f)>=5 and f[0]=='223' and f[2]==args.package:
            print(json.dumps({'operation':f[1],'package':args.package,'status':f[4]}),flush=True)
            if f[1]=='INSTALL' and f[4]=='denied':
                outcome='denied'; break
            if f[1]=='PROGRESS' and f[4]=='2':
                outcome='failed'; break
            if (f[1]=='PROGRESS' and f[4]=='5') or (f[1]=='INSTALL' and f[4]=='installed'):
                outcome='installed'
                intent={'action':'android.intent.action.MAIN','category':'android.intent.category.LAUNCHER',
                        'component':{'pkg':args.package,'cls':args.activity}}
                if not args.no_launch:
                    session.send('263>>1>>'+json.dumps(intent,separators=(',',':')))
                    print(json.dumps({'launch_requested':args.activity}),flush=True)
                break
        if time.monotonic()-last_beat>4:
            session.send('150>>')
            last_beat=time.monotonic()
finally:
    session.sock.close()
    server.shutdown()
    print(json.dumps({'outcome':outcome}),flush=True)
