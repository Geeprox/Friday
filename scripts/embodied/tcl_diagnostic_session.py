"""Bounded TCL diagnostic session. No install, shell, or settings-write commands.

T-Cast framing from DusKing1/ha-tcl-tv (MIT).
"""
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

def selected_host():
    import ipaddress
    value = os.environ.get('TV_DIAGNOSTIC_HOST', '')
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError('set_TV_DIAGNOSTIC_HOST_to_your_selected_TV') from None
    if not address.is_private or address.is_loopback or address.is_multicast or address.is_unspecified:
        raise ValueError('selected_TV_must_be_on_your_private_network')
    return str(address)

HOST = selected_host()
ROOT = Path(__file__).resolve().parents[2]/'.artifacts/embodied-migration/tv'
KEY = b'tnscreentnscreen'.hex()
IV = '1234567890abcdef1234567890abcdef'

def cipher(data, decrypt=False):
    args=['/usr/bin/openssl','enc','-aes-128-cbc','-K',KEY,'-iv',IV]
    if decrypt: args.append('-d')
    p=subprocess.run(args,input=data,capture_output=True,timeout=3)
    if p.returncode: raise ValueError('cipher error')
    return p.stdout

def exact(sock,n):
    data=b''
    while len(data)<n:
        chunk=sock.recv(n-len(data))
        if not chunk: raise ConnectionError('TV closed session')
        data+=chunk
    return data

class Session:
    def __init__(self):
        self.sock=socket.create_connection((HOST,6553),timeout=5)
        self.sock.settimeout(5)
        self.encrypted=False
        self.identity='codex-'+uuid.uuid4().hex[:12]
        self.send(f'159>>Codex TV diagnostic>>1>>{self.identity}>>1')
        reply=self.until('159>>')
        f=reply.split('>>')
        self.encrypted=f[6]=='1'
        print(json.dumps({'model':f[1],'firmware':f[3],'encrypted':self.encrypted}),flush=True)

    def send(self,text):
        body=text.encode()
        if self.encrypted: body=cipher(body)
        self.sock.sendall(struct.pack('>I',len(body))+body)

    def receive(self):
        size=struct.unpack('>I',exact(self.sock,4))[0]
        if not 0<size<1048576: raise ValueError('invalid frame')
        body=exact(self.sock,size)
        if len(body)%16==0:
            try:
                plain=cipher(body,True)
                if plain[:1].isdigit(): return plain.decode(errors='replace')
            except ValueError: pass
        return body.decode(errors='replace')

    def until(self,prefix):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            reply=self.receive()
            if reply.startswith(prefix): return reply
        raise TimeoutError('expected response missing')

    def package_info(self,pkg):
        allowed={'com.android.settings','com.android.tv.settings','com.tcl.settings','com.tcl.MultiScreenInteraction_TV'}
        if pkg not in allowed: raise ValueError('package not allowed')
        self.send('223>>PACKAGEINFO>>'+pkg)
        reply=self.until('223>>PACKAGEINFO>>')
        f=reply.split('>>')
        print(json.dumps({'requested':pkg,'reply_fields':f[:8]},ensure_ascii=False),flush=True)
        return f

    def screenshot(self):
        self.send('225>>')
        f=self.until('225>>').split('>>')
        if len(f)<3 or f[1]!='0':
            print(json.dumps({'screenshot_reply':f}),flush=True)
            return
        u=urllib.parse.urlsplit(f[2])
        if u.scheme!='http' or u.hostname!=HOST: raise ValueError('unexpected screenshot host')
        (ROOT/'tv-screenshot-url.txt').write_text(f[2])
        print(json.dumps({'screenshot_port':u.port}),flush=True)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f[2],timeout=4) as r: data=r.read(16*1024*1024+1)
            if len(data)>16*1024*1024: raise ValueError('image too large')
            path=ROOT/('tv-'+str(time.time_ns())+'.png')
            path.write_bytes(data)
            print(json.dumps({'image_path':str(path),'bytes':len(data)}),flush=True)
            return path
        except OSError as e:
            print(json.dumps({'screenshot_fetch_error':type(e).__name__,'detail':str(e)[:160]}),flush=True)

    def run(self):
        # Normal SDK post-connect identity notification; no setting is changed.
        self.send(f'160>>{self.identity}>>Codex TV diagnostic')
        if '--open-interaction' in sys.argv:
            self.send('223>>OPEN>>com.tcl.MultiScreenInteraction_TV')
            time.sleep(1)
        if '--open-settings' in sys.argv:
            self.send('223>>OPEN>>com.tcl.settings')
            time.sleep(0.7)
        if '--wake-projection' in sys.argv:
            self.send('258>>0')
            time.sleep(0.8)
        self.screenshot()
        for pkg in ['com.android.settings','com.android.tv.settings','com.tcl.settings','com.tcl.MultiScreenInteraction_TV']:
            try: self.package_info(pkg)
            except (OSError,TimeoutError) as e:
                print(json.dumps({'package':pkg,'error':type(e).__name__}),flush=True)
        self.sock.close()

if __name__=='__main__':
    Session().run()
