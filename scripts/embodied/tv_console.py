"""One persistent, bounded television navigation session; no shell or install."""
import json
import queue
import select
import shutil
import sys
import threading
import time
from pathlib import Path
from tcl_diagnostic_session import Session

ROOT=Path(__file__).resolve().parents[2]/'.artifacts/embodied-migration/tv'
KEYS={'up':11,'down':12,'left':13,'right':14,'ok':15,'back':16,'menu':18,'home':19}
s=Session()
s.sock.settimeout(15)
s.send(f'160>>{s.identity}>>Codex TV diagnostic')
# REA of this exact TV APK shows HTTP startup is asynchronous after 159;
# its successful startup callback emits 223 GET, so wait for it before 225.
s.send('223>>GET')
ready=s.until('223>>GET>>')
print(json.dumps({'file_service_ready':True,'app_list_bytes':len(ready.encode())}),flush=True)
s.sock.settimeout(3)
commands=queue.Queue()
def read_input():
    for line in sys.stdin:
        try: commands.put(json.loads(line))
        except Exception: print(json.dumps({'input_error':'expected JSON'}),flush=True)
    commands.put({'op':'stop'})
threading.Thread(target=read_input,daemon=True).start()
last_beat=time.monotonic()

def screenshot():
    p=s.screenshot()
    if p:
        shutil.copyfile(p,ROOT/'tv-console-latest.png')

def keys(names):
    if not isinstance(names,list) or not 1<=len(names)<=20 or any(x not in KEYS for x in names):
        raise ValueError('expected 1-20 named remote keys')
    for name in names:
        s.send('149>>'+str(KEYS[name]))
        time.sleep(0.12)

def open_settings():
    intent={'action':'android.intent.action.MAIN','category':'android.intent.category.LAUNCHER',
            'component':{'pkg':'com.tcl.settings','cls':'com.tcl.settings.ui.MainActivity'}}
    s.send('263>>1>>'+json.dumps(intent,separators=(',',':')))
    time.sleep(0.7)

try:
    screenshot()
    print(json.dumps({'console':'ready'}),flush=True)
    while True:
        try: command=commands.get(timeout=0.2)
        except queue.Empty: command=None
        if command:
            op=command.get('op')
            if op=='stop': break
            if op=='screenshot': screenshot()
            elif op=='key' and command.get('key') in KEYS:
                s.send('149>>'+str(KEYS[command['key']]))
                time.sleep(0.4)
                screenshot()
            elif op=='keys':
                keys(command.get('keys'))
                time.sleep(0.4)
                screenshot()
            elif op=='open_games':
                intent={'action':'android.intent.action.MAIN','category':'android.intent.category.LAUNCHER','component':{'pkg':'local.tv.camera','cls':'local.tv.camera.MainActivity'}}
                s.send('263>>1>>'+json.dumps(intent,separators=(',',':')));time.sleep(.7);screenshot()
            elif op=='open_settings':
                open_settings()
                screenshot()
            elif op=='open_device_info':
                # This path has been visually verified on this exact TV build.
                open_settings()
                keys(['down']*8+['right','down','ok'])
                time.sleep(0.4)
                screenshot()
            else: print(json.dumps({'input_error':'unsupported operation'}),flush=True)
        if time.monotonic()-last_beat>=4:
            s.send('150>>')
            last_beat=time.monotonic()
        if select.select([s.sock],[],[],0)[0]:
            s.receive()
finally:
    s.sock.close()
    print(json.dumps({'console':'closed'}),flush=True)
