"""DOM lifecycle contract; actual browser proof is kept separately."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")
def test_redacted_download_failed_policy_retry_and_late_navigation():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),nodes=new Map();
const downloads=[],urls=new Set(),timers=[],posts=[];
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.attrs={};this._text='';this.isConnected=true;}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(child=>child.textContent??'').join('');}
 set innerHTML(_){throw Error('Unsafe HTML');}
 setAttribute(key,value){this.attrs[key]=String(value);if(key==='id')nodes.set(String(value),this);}
 append(...items){for(const item of items){item.parent=this;this.children.push(item);}}
 replaceChildren(...items){this._text='';this.children=[];this.append(...items);}
 addEventListener(key,value){this.handlers[key]=value;}
 remove(){this.isConnected=false;if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);}
 click(){if(this.tag==='a')downloads.push(this.attrs);else return this.handlers.click?.();}
}
for(const id of ['navigation','developer-mode','mode','snapshot-time','page-title','content','notification','refresh']){const node=new Element('div');node.setAttribute('id',id);}
const fixture={mode:'simulator',version:'SIMULATED',generated_at:new Date().toISOString(),entities:[],devices:[],states:[],rules:[],observations:[],events:[],limitations:[]};
let policyValid=true,reply='success',pending=null;
const policy=()=>({redacted:true,server_retention_seconds:0,max_bytes:65536,automatic_upload:!policyValid});
const response=value=>({ok:true,json:async()=>value});
const zip=()=>({ok:true,headers:{get:()=> 'application/zip'},blob:async()=>({size:2400,type:'application/zip'})});
const fetch=async(path,options={})=>{
 if(path==='/api/diagnostics')return response(policy());
 if(path==='/api/diagnostics/export'){
  posts.push(JSON.parse(options.body));assert.equal(options.method,'POST');
  if(reply==='pending')return await new Promise(resolve=>{pending=resolve;});
  return reply==='busy'?{ok:false,status:429}:zip();
 }
 if(options.method==='POST')throw Error('Unexpected device/provider write '+path);
 return response(path==='/api/doctor'?{capabilities:{}}:path==='/api/decision/providers'?{}:fixture);
};
const context=vm.createContext({document:{getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag)},fetch,
 location:{hash:''},history:{replaceState(){}},Date,JSON,console,setInterval:()=>1,setTimeout:callback=>timers.push(callback),
 URL:{createObjectURL:()=>{const url='blob:SIMULATED-'+urls.size;urls.add(url);return url;},revokeObjectURL:url=>urls.delete(url)}});
vm.runInContext(fs.readFileSync('homeplane/static/app.js','utf8'),context);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const go=async()=>{vm.runInContext("go('System')",context);await settle();};
(async()=>{
 await settle();await go();assert.equal(nodes.get('diagnostics-export-download').disabled,false);
 await nodes.get('diagnostics-export-download').handlers.click();
 assert.equal(downloads.length,1);assert.equal(downloads[0].download,'friday-diagnostics.zip');
 assert.deepEqual(posts,[{redacted:true}]);assert.equal(urls.size,1);timers.shift()();assert.equal(urls.size,0);
 assert.match(nodes.get('diagnostics-export-status').textContent,/已交给浏览器下载/);
 reply='busy';await nodes.get('diagnostics-export-download').handlers.click();
 assert.match(nodes.get('diagnostics-export-status').textContent,/稍后重试/);assert.equal(nodes.get('diagnostics-export-download').disabled,false);
 reply='pending';const job=nodes.get('diagnostics-export-download').handlers.click();await settle();assert(pending);
 vm.runInContext("go('Devices')",context);await settle();pending(zip());await job;
 assert.equal(downloads.length,1,'navigation cancels a late download');assert.equal(urls.size,0);
 policyValid=false;await go();assert.equal(nodes.get('diagnostics-export-download').disabled,true);
 assert.match(nodes.get('diagnostics-export-status').textContent,/暂不可用/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", script], cwd=Path(__file__).parents[1], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
