"""SIMULATED DOM lifecycle, with no media, model or household commands."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")
def test_explicit_media_start_stop_and_late_result_rejection():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),nodes=new Map(),calls=[];
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.attrs={};this._text='';this.value='';this.isConnected=true;}
 set textContent(v){this._text=String(v);this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent??'').join('');}
 set innerHTML(_){throw Error('Unsafe HTML');}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='id')nodes.set(String(v),this);if(k==='value')this.value=String(v);}
 getAttribute(k){return this.attrs[k];}
 removeAttribute(k){delete this.attrs[k];}
 append(...c){this.children.push(...c);if(this.tag==='select'&&!this.value&&this.children.length)this.value=this.children[0].value;}
 replaceChildren(...c){this._text='';this.children=[];this.append(...c);}
 addEventListener(k,v){this.handlers[k]=v;}
}
for(const id of ['navigation','developer-mode','mode','snapshot-time','page-title','content','notification','refresh']){const n=new Element('div');n.setAttribute('id',id);}
const fixture={mode:'simulator',version:'SIMULATED',generated_at:new Date().toISOString(),entities:[],devices:[],states:[],events:[],rules:[],observations:[],limitations:[]};
const config={camera_service_url:'http://127.0.0.1:1810',camera_id_configured:true,camera_media_source:'miloco_service',ha_url:null};
let report={status:'not_requested',simulated:true},delayed=false,release=null;
const response=v=>({ok:true,json:async()=>v});
const fetch=async(path,options={})=>{
 calls.push([path,options.method??'GET',options.body?JSON.parse(options.body):null]);
 if(path==='/api/settings'){if(options.method==='PUT')Object.assign(config,JSON.parse(options.body));return response({config:{...config},checks:{},credentials:{}});}
 if(path==='/api/settings/camera/media'){
  if(options.method==='POST'){assert.deepEqual(JSON.parse(options.body),{seconds:6});report={status:'running',simulated:true};}
  const current=report;if(delayed&&!options.method)await new Promise(resolve=>release=()=>resolve());
  return response(current);
 }
 if(path==='/api/settings/camera/media/stop'){report={status:'stopped',simulated:true};return response(report);}
 if(options.method==='POST')throw Error('Unexpected device/provider action '+path);
 if(path==='/api/settings/miloco/models')return response({status:'service_unavailable',catalog:[]});
 if(path==='/api/settings/geek/session')return response({status:'stopped'});
 if(path==='/api/settings/ha/status')return response({configured:false});
 if(path==='/api/settings/ha/discover')return response({instances:[]});
 if(path==='/api/settings/actions/status')return response({configured:false});
 if(path==='/api/settings/arl/status')return response({status:'not_running'});
 return response(fixture);
};
const context=vm.createContext({document:{getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag)},fetch,location:{hash:'#settings'},history:{replaceState(){}},Date,JSON,URL,console,setInterval:()=>1});
vm.runInContext(fs.readFileSync('homeplane/static/app.js','utf8'),context);
const settle=()=>new Promise(resolve=>setImmediate(resolve)),node=id=>nodes.get(id),click=async id=>{assert(!node(id).disabled);await node(id).handlers.click();await settle();};
(async()=>{
 await settle();await settle();assert(!calls.some(([,method])=>method==='POST'),'viewing settings must not start media');
 assert.match(node('settings-camera-media-result').textContent,/尚未验证/);assert(node('settings-camera-media-stop').disabled);
 const draft=node('settings-model-key');draft.value='SIMULATED-unsent-key';
 await click('settings-camera-media-start');assert(node('settings-camera-media-start').disabled);assert(!node('settings-camera-media-stop').disabled);
 assert.match(node('settings-camera-media-result').textContent,/正在验证/);
 report={status:'decoded_sync_unknown',simulated:true,video:{status:'decoded',decoded_frames:20},audio:{status:'decoded',decoded_frames:30},checked_at:new Date().toISOString()};
 await click('settings-camera-media-refresh');assert.match(node('settings-camera-media-result').textContent,/解码通过/);assert.match(node('settings-camera-media-result').textContent,/同步：无法核验/);
 assert.equal(node('settings-model-key'),draft);assert.equal(draft.value,'SIMULATED-unsent-key');
 report={...report,status:'expired'};await click('settings-camera-media-refresh');assert.match(node('settings-camera-media-result').textContent,/已过期/);
 const source=node('settings-camera-source');const beforeChoice=calls.length;source.value='miot_sdk';source.handlers.change();
 assert(node('settings-camera-url').disabled&&node('settings-camera-login').hidden&&node('settings-camera-probe').hidden);
 assert.equal(calls.length,beforeChoice,'choosing source must not start camera or authenticate');
 await click('settings-camera-media-start');assert.match(node('settings-camera-result').textContent,/请先保存/);
 await node('settings-camera-form').handlers.submit({preventDefault(){}});await settle();assert.equal(config.camera_media_source,'miot_sdk');
 assert(node('settings-camera-login').hidden,'saving direct mode must not offer stopped service login');
 report={status:'decoded_sync_unknown',provider:'miot_sdk',simulated:true,video:{status:'decoded',decoded_frames:20},audio:{status:'decoded',decoded_frames:30},checked_at:new Date().toISOString()};
 await click('settings-camera-media-refresh');assert.match(node('settings-camera-media-result').textContent,/源时钟尚未标定/);
 assert(!node('settings-camera-media-result').textContent.includes('音频未提供源时间戳'));

 await click('settings-camera-media-start');
 report={status:'failed',simulated:true,failure:'decode_failed'};delayed=true;
 const pending=vm.runInContext('refreshCameraMedia()',context);await settle();assert(release);
 await click('settings-camera-media-stop');release();await pending;delayed=false;
 assert.match(node('settings-camera-media-result').textContent,/已停止/);assert(!node('settings-camera-media-result').textContent.includes('未通过'),'late poll cannot override stop');
 delayed=true;const leaving=vm.runInContext('refreshCameraMedia()',context);await settle();vm.runInContext("go('Home')",context);release();await leaving;delayed=false;
 assert(!node('content').textContent.includes('音视频验证未通过'),'late response cannot revive settings');
 assert(calls.filter(([,method])=>method==='POST').every(([path])=>path.startsWith('/api/settings/camera/media')));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", script], cwd=Path(__file__).parents[1], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
