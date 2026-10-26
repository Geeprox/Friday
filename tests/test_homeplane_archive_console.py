"""SIMULATED DOM contracts. Real-browser rendering is recorded separately."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable for Console DOM smoke")
def test_archive_reference_races_and_explicit_unreviewed_setup():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),nodes=new Map();
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.attrs={};this._text='';this.value='';this.isConnected=true;this.checked=false;}
 set textContent(v){this._text=String(v);this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent??'').join('');}
 set innerHTML(_){throw Error('Unsafe household HTML');}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='id')nodes.set(String(v),this);if(k==='value')this.value=String(v);}
 append(...c){this.children.push(...c);if(this.tag==='select'&&!this.value&&this.children.length)this.value=this.children[0].value;}
 replaceChildren(...c){this._text='';this.children=[];this.append(...c);}
 addEventListener(k,v){this.handlers[k]=v;}
 removeAttribute(k){delete this.attrs[k];}
}
for(const id of ['navigation','developer-mode','mode','snapshot-time','page-title','content','notification','refresh']){const n=new Element('div');n.setAttribute('id',id);}
const stamp=new Date().toISOString(),span={start:'2026-09-30T10:00:00Z',end:'2026-09-30T10:02:00Z'};
const fixture={mode:'simulator',generated_at:stamp,entities:[],devices:[],states:[],events:[],observations:[],rules:[],limitations:[]};
const cfg={config:{nas_path:'/SIMULATED/archive'},credentials:{},checks:{}};
let context={fingerprint:'a'.repeat(64),simulated:true,configured:true,cameras:[{id:'SIMULATED-camera',name:'SIMULATED camera',example_ranges:{ready:span,pending:span,expired:span}}],folders:[],scan:null};
let posts=[],pending=null,late=false,resultSourceChange=false;
const response=value=>({ok:true,json:async()=>value});
const fetch=async(path,options={})=>{
 if(path==='/api/archive/context')return response(context);
 if(path==='/api/settings')return response(cfg);
 if(path==='/api/settings/ha/status')return response({configured:false,credential_available:false});
 if(path==='/api/settings/actions/status')return response({configured:false});
 if(options.method==='POST'){
  const body=JSON.parse(options.body);posts.push([path,body]);
  if(path==='/api/archive/resolve'){
   assert.deepEqual(Object.keys(body).sort(),['camera_id','expected_context','time_range']);
   const report={context_fingerprint:context.fingerprint,camera_id:body.camera_id,time_range:body.time_range,status:'ready',simulated:context.simulated,segments:[],gaps:[],causal_relation:'unknown'};
   if(late)return await new Promise(resolve=>pending=()=>resolve(response(report)));
   if(resultSourceChange)context={...context,fingerprint:'b'.repeat(64),simulated:false};
   return response(report);
  }
  if(path==='/api/archive/scan'){
   context={...context,fingerprint:'d'.repeat(64),folders:[{folder_ref:'e'.repeat(64),label:'SIMULATED directory',scan_complete:true,sample_filenames:['00_20260930100000_20260930100200.mp4']}],scan:{indexed_this_scan:1}};
   return response(context);
  }
  if(path==='/api/archive/cameras'){
   context={...context,fingerprint:'f'.repeat(64),cameras:[{...body,id:'SIMULATED-persistent-camera'}]};return response(context);
  }
  throw Error('Unexpected write '+path);
 }
 if(path==='/api/settings/miloco/models')return response({status:'service_unavailable',catalog:[]});
 return response(fixture);
};
const env=vm.createContext({document:{getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag)},fetch,location:{hash:''},history:{replaceState(){}},Date,JSON,URL,console,setInterval:()=>1});
vm.runInContext(fs.readFileSync('homeplane/static/app.js','utf8'),env);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const change=(id,value)=>{const n=nodes.get(id);n.value=value;(n.handlers.input??n.handlers.change)();};
const submit=()=>nodes.get('archive-lookup-form').handlers.submit({preventDefault(){}});
(async()=>{
 await settle();vm.runInContext("go('Activity')",env);await settle();assert.equal(posts.length,0,'opening does not read NAS or infer');
 await submit();assert.match(nodes.get('archive-result').textContent,/simulated/);assert.match(nodes.get('archive-result').textContent,/未读取真实原片/);
 change('archive-start','2026-09-30T18:00:01');late=true;const request=submit();await settle();assert(pending);
 change('archive-end','2026-09-30T18:01:59');pending();await request;
 assert.equal(nodes.get('archive-result').textContent,'','edited query discards late answer');
 const leaving=submit();await settle();vm.runInContext("go('Home');go('Activity')",env);await settle();pending();await leaving;
 assert(!nodes.get('archive-result').textContent.includes('原片引用可定位'),'leaving and returning does not revive late answer');
 late=false;resultSourceChange=true;await submit();assert.match(nodes.get('archive-result').textContent,/归档来源或映射已变化/);
 context={...context,fingerprint:'c'.repeat(64),simulated:false,cameras:[]};
 vm.runInContext("selectedConnection='settings-nas';go('Settings')",env);await settle();
 assert(!posts.some(([p])=>p==='/api/archive/scan'),'settings does not scan automatically');
 assert.equal(nodes.get('settings-archive-timing').checked,false);assert.equal(nodes.get('settings-archive-completion').checked,false);
 await nodes.get('settings-archive-scan').handlers.click();await settle();
 change('settings-archive-folder','e'.repeat(64));change('settings-archive-name','SIMULATED camera');
 await nodes.get('settings-archive-form').handlers.submit({preventDefault(){}});await settle();
 const body=posts.at(-1)[1];assert.equal(body.timing_reviewed,false);assert.equal(body.clock_offset_seconds,null);assert.equal(body.clock_uncertainty_seconds,null);
 assert.equal(body.completion_convention,'unreviewed');assert.equal(body.camera_id,null);assert(!('path' in body));
 assert.match(nodes.get('settings-archive-status').textContent,/对应关系已保存/);
 assert(posts.every(([p])=>p.startsWith('/api/archive/')),'no device, model, mount or external write');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=Path(__file__).parents[1], text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
