"""DOM contract smoke test; real-browser integration is tested separately.

The tiny DOM records nodes/events to exercise our actual script without a JS package
manager. It intentionally does not claim browser rendering/accessibility coverage.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable for Console DOM smoke")
def test_core_rule_review_window_rollback_and_late_preview():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),nodes=new Map();
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.attrs={};this._text='';this.value='';this.isConnected=true;}
 set textContent(v){this._text=String(v);this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent??'').join('');}
 set innerHTML(_){throw Error('Unsafe household HTML');}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='id')nodes.set(String(v),this);if(k==='value')this.value=String(v);}
 append(...c){for(const n of c){n.parent=this;this.children.push(n);}if(this.tag==='select'&&!this.value&&this.children.length)this.value=this.children[0].value;}
 replaceChildren(...c){this._text='';this.children=[];this.append(...c);}
 addEventListener(k,v){this.handlers[k]=v;}
 remove(){this.isConnected=false;if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}
}
for(const id of ['navigation','developer-mode','mode','snapshot-time','page-title','content','notification','refresh']){const n=new Element('div');n.setAttribute('id',id);}
const now=new Date().toISOString(),cap={name:'light.set',access:'write',support:'supported',minimum:1,maximum:255};
const fixture={mode:'ha-read-only',version:'SIMULATED',generated_at:now,action_targets:['SIMULATED-lamp'],
 entities:[{id:'SIMULATED-door',name:'SIMULATED door',kind:'binary_sensor',capabilities:[{name:'binary_sensor.state.transition',access:'event',support:'supported'}]},
 {id:'SIMULATED-lamp',name:'SIMULATED lamp',kind:'light',state_version:'SIMULATED-state',capabilities:[cap]}],
 devices:[{id:'SIMULATED-physical-lamp',name:'SIMULATED physical lamp',kind:'light',function_ids:['SIMULATED-lamp'],capabilities:[cap]},{id:'SIMULATED-physical-door',name:'SIMULATED physical door',kind:'binary_sensor',function_ids:['SIMULATED-door'],capabilities:[]}],
 states:[],rules:[],observations:[],events:[],limitations:[]};
let plan=null,records=[],posts=[],pending=null,late=false,sequence=0,backendBoot='SIMULATED-boot';
const report=()=>({available:true,owner_available:true,boot_id:backendBoot,fingerprint:'a'.repeat(64),records,plans:plan?[plan]:[]});
const state={available:true,mode_revision_valid:false,modes:{},runs:[],pending_count:0};
const response=value=>({ok:true,json:async()=>value});
const fetch=async(path,options={})=>{
 if(path==='/api/rules/core/records')return response(report());
 if(path==='/api/rules/core/runtime')return response(state);
 if(options.method==='POST'){
  const body=JSON.parse(options.body);posts.push([path,body]);
  if(path==='/api/rules/core/plans'){
   assert.equal(body.expected_context,'a'.repeat(64));assert.equal(body.rule.simulated,false);
   assert.equal(body.rule.execution.preferred_backend,'home_core');assert.equal(body.rule.trigger.capability,'binary_sensor.state');
   assert.deepEqual(body.rule.actions[0].desired,{power:'on',brightness:8});
   plan={id:'SIMULATED-plan',boot_id:'SIMULATED-boot',context_fingerprint:'a'.repeat(64),status:'validated',expires_at:new Date(Date.now()+300000).toISOString(),approval_digest:'b'.repeat(64),new:{id:'SIMULATED-record',rule:body.rule},old:null};
   if(late)return await new Promise(resolve=>{pending=()=>resolve(response(plan));});
  }else if(path==='/api/rules/core/plans/trial')plan={...plan,status:'awaiting_approval',trial:{outcome:'matched',simulated:true}};
  else if(path==='/api/rules/core/plans/apply'){assert.equal(body.approval_digest,'b'.repeat(64));plan={...plan,status:'verified'};records=[{...plan.new,enabled:true}];}
  else if(path==='/api/rules/core/plans/rollback'){plan={...plan,status:'rolled_back'};records=[{...plan.new,enabled:false}];}
  else if(path==='/api/rules/core/runtime/mode'){state.mode_revision_valid=true;state.modes=body.mode==='unknown'?{}:{'home.sleep':{value:false,valid_until:new Date(Date.now()+60000).toISOString()},'home.manual':{value:false,valid_until:new Date(Date.now()+60000).toISOString()}};return response(state);}
  else throw Error('Unexpected real execution route '+path);
  return response(plan);
 }
 return response(fixture);
};
const context=vm.createContext({document:{getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag)},fetch,location:{hash:''},history:{replaceState(){}},crypto:{randomUUID:()=> 'SIMULATED-key-'+(++sequence)},Date,JSON,console,setInterval:()=>1});
vm.runInContext(fs.readFileSync('homeplane/static/app.js','utf8'),context);
const settle=()=>new Promise(resolve=>setImmediate(resolve)),content=nodes.get('content');
const change=(id,value)=>{const n=nodes.get(id);n.value=value;(n.handlers.input??n.handlers.change)();};
const click=async id=>{assert(!nodes.get(id).disabled);await nodes.get(id).handlers.click();await settle();};
(async()=>{
 await settle();vm.runInContext("go('Automations')",context);await settle();
 assert.match(content.textContent,/本机习惯/);assert.match(content.textContent,/未开启/);assert.equal(posts.length,0);
 change('core-rule-name','SIMULATED arrival');change('core-rule-trigger-device','SIMULATED-physical-door');change('core-rule-trigger','SIMULATED-door');
 change('core-rule-device','SIMULATED-physical-lamp');change('core-rule-value','on');change('core-rule-brightness','8');
 await nodes.get('core-rule-form').handlers.submit({preventDefault(){}});await settle();
 assert.match(content.textContent,/等待模拟验证/);assert(!nodes.get('core-rule-apply'));
 await click('core-rule-trial');assert.match(content.textContent,/simulated/);assert.match(content.textContent,/等待确认安装/);
 await click('core-rule-apply');assert.match(content.textContent,/本机配置已核对/);assert.match(content.textContent,/未开启/);
 await click('core-rule-mode-awake');assert.match(content.textContent,/清醒测试至/);
 backendBoot='SIMULATED-restarted';await click('core-rule-mode-unknown');assert.match(content.textContent,/未开启/);
 await click('core-rule-rollback');assert.match(content.textContent,/已停用 · 版本保留/);assert.match(content.textContent,/配置已恢复/);
 backendBoot='SIMULATED-boot';late=true;change('core-rule-name','SIMULATED pending');
 const job=nodes.get('core-rule-form').handlers.submit({preventDefault(){}});await settle();assert(pending);
 change('core-rule-name','SIMULATED changed');pending();await job;await settle();
 assert(!content.textContent.includes('等待模拟验证'),'late preview cannot reinstall an obsolete draft');
 assert(posts.every(([path])=>path.startsWith('/api/rules/core/')),'no device command or foreign rule write');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=Path(__file__).parents[1], text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable for Console DOM smoke")
def test_rule_text_rejects_stale_preview_and_late_text_response():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),nodes=new Map();
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.attrs={};this._text='';this.value='';this.isConnected=true;}
 set textContent(v){this._text=String(v);this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent??'').join('');}
 set innerHTML(_){throw Error('Unsafe HTML');}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='id')nodes.set(String(v),this);}
 append(...children){for(const child of children){child.parent=this;this.children.push(child);}}
 replaceChildren(...children){this._text='';this.children=[];this.append(...children);}
 addEventListener(k,v){this.handlers[k]=v;}
 remove(){this.isConnected=false;if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}
 scrollIntoView(){}
}
for(const id of ['navigation','developer-mode','mode','snapshot-time','page-title','content','notification','refresh']){const n=new Element('div');n.setAttribute('id',id);}
const trigger={type:'state_transition',entity:'door.simulated',from_value:'closed',to_value:'open'};
const light={id:'SIMULATED-light-on',type:'light.set',power:'on',target:'light.simulated'};
const ac={id:'SIMULATED-ac-on',type:'climate.set',power:'on',target:'climate.simulated'};
const oldRule={id:'SIMULATED-arrival',intent:'SIMULATED arrival',trigger,actions:[light,ac]};
const plan={id:'SIMULATED-plan',status:'validated',old:{rule:oldRule},new_rule:{...oldRule,actions:[light]},history:[]};
const fixture={mode:'simulator',version:'SIMULATED',generated_at:new Date().toISOString(),entities:[],devices:[],states:[],observations:[],events:[],rules:[],limitations:[]};
const scope={available:true,simulated:true,fingerprint:'a'.repeat(64)};
let reply='planned',pending=null,lastRequest=null,writes=0;
const response=value=>({ok:true,json:async()=>value});
const fetch=async(path,options={})=>{
 if(path==='/api/rules/text/context')return response(scope);
 if(path==='/api/rules/managed'||path==='/api/rules/runtime')return response({available:false});
 if(path==='/api/rules/text/plan'){
  lastRequest=JSON.parse(options.body);
  if(reply==='pending')return await new Promise(resolve=>{pending=resolve;});
  return response(reply==='planned'?{status:'planned',context:scope,plan}:{status:'unsupported',context:scope,code:'structured_planner_unavailable'});
 }
 if(options.method==='POST')writes++;
 return response(fixture);
};
const context=vm.createContext({document:{getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag)},fetch,location:{hash:''},history:{replaceState(){}},crypto:{randomUUID:()=> 'SIMULATED-key'},Date,JSON,console,setInterval:()=>1});
vm.runInContext(fs.readFileSync('homeplane/static/app.js','utf8'),context);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const content=nodes.get('content');
const type=text=>{const input=nodes.get('rule-text-input');input.value=text;input.handlers.input();};
const submit=async()=>{nodes.get('rule-text-form').handlers.submit({preventDefault(){}});await settle();};
(async()=>{
 await settle();vm.runInContext("go('Automations')",context);await settle();
 type('以后回家不开空调');await submit();await settle();
 assert.match(content.textContent,/等待模拟验证/);assert.match(content.textContent,/模拟触发/);
 assert.match(content.textContent,/触发：door.simulated 从 closed 变为 open/);
 assert.equal(lastRequest.text,'以后回家不开空调');assert.equal(writes,0);
 type('以后回家不开空调，但是热天要保留制冷');
 assert(!content.textContent.includes('等待模拟验证'),'changed text removes old actionable preview');
 reply='unsupported';await submit();
 assert.match(content.textContent,/规划能力尚未接通/);assert(!content.textContent.includes('模拟触发'));
 reply='pending';type('以后回家不开空调');await submit();assert(pending);
 type('以后回家不开灯');pending(response({status:'planned',context:scope,plan}));await settle();await settle();
 assert(!content.textContent.includes('等待模拟验证'),'late response cannot restore the preview for old text');
 assert.equal(writes,0,'text preview never executes or deploys');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=Path(__file__).parents[1], text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable for Console DOM smoke")
def test_physical_device_plan_execution_and_unknown_readonly_reconcile():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),nodes=new Map();
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.handlers={};this._text='';this.value='';this.isConnected=true;}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent??'').join('');}
 set innerHTML(_){throw Error('Unsafe source HTML');}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='id')nodes.set(String(v),this);if(k==='value')this.value=String(v);}
 append(...children){this.children.push(...children);if(this.tag==='select'&&!this.value)this.value=this.children[0].value;}
 replaceChildren(...children){this._text='';this.children=children;}
 addEventListener(k,handler){this.handlers[k]=handler;}
 scrollIntoView(){}
}
for(const id of ['navigation','developer-mode','mode','snapshot-time','page-title','content','notification','refresh']){const n=new Element('div');n.setAttribute('id',id);}
const cap={name:'light.set',access:'write',support:'supported',minimum:1,maximum:255};
const stamp=new Date().toISOString();
const fixture={mode:'ha-read-only',version:'SIMULATED-global',generated_at:stamp,action_targets:['ha:simulated:light'],
 entities:[{id:'ha:simulated:light',name:'Simulated Lamp power',kind:'light',state_version:'SIMULATED-target-version',room:'Simulated Room',bindings:[],capabilities:[cap]},
 {id:'ha:simulated:number',name:'Simulated Lamp brightness function',kind:'number',room:'Simulated Room',bindings:[],capabilities:[]}],
 devices:[{id:'device:simulated:lamp',name:'Simulated Lamp',kind:'light',category:'device',room:'Simulated Room',function_ids:['ha:simulated:light','ha:simulated:number'],capabilities:[cap],bindings:[]}],
 states:[{id:'ha:simulated:light:light.state',value:'off',quality:'healthy',reasons:['latched_state_observed_not_new_transition'],as_of:stamp,valid_until:new Date(Date.now()+60000).toISOString()}],observations:[],events:[],rules:[],limitations:[]};
let request=null,trace=null,executeCount=0,reconcileCount=0,sequence=0;
const fetch=async(path,options={})=>{
 if(path==='/api/actions/plan'){request=JSON.parse(options.body);trace={action_id:'SIMULATED-action',route:request.operation?'ha-scoped-device':'ha-scoped-light',status:'planned',request,real_write_attempted:false,events:[]};return{ok:true,json:async()=>trace};}
 if(path==='/api/actions/execute'){executeCount++;assert.deepEqual(JSON.parse(options.body),{plan_id:'SIMULATED-action'});trace={...trace,status:'unknown_outcome',real_write_attempted:true};return{ok:true,json:async()=>trace};}
 if(path==='/api/actions/reconcile'){reconcileCount++;trace={...trace,status:'reported_match',physically_confirmed:false};return{ok:true,json:async()=>trace};}
 return{ok:true,json:async()=>fixture};
};
const context=vm.createContext({document:{getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag)},fetch,location:{hash:''},history:{replaceState(){}},crypto:{randomUUID:()=> 'SIMULATED-key-'+(++sequence)},Date,JSON,console,setInterval:()=>1});
vm.runInContext(fs.readFileSync('homeplane/static/app.js','utf8'),context);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const all=node=>[node,...node.children.flatMap(all)];
(async()=>{
 await settle();vm.runInContext("go('Devices')",context);
 const content=nodes.get('content');
 assert.equal(all(content).filter(n=>n.attrs['data-entity-id']).length,1,'one physical lamp card, two raw functions');
 assert.match(content.textContent,/1 个设备/);assert.match(content.textContent,/Simulated Lamp brightness function/);
 nodes.get('action-power').value='on';nodes.get('action-brightness').value='8';
 await nodes.get('action-form').handlers.submit({preventDefault(){}});
 assert.equal(request.target,'ha:simulated:light');assert.equal(request.expected_state_version,'SIMULATED-target-version');
 assert(!('origin' in request));assert.equal(executeCount,0,'preview does not send');
 assert.match(content.textContent,/真实最低档与无闪烁尚未核验/);
 await nodes.get('execute-plan').handlers.click();assert.equal(executeCount,1);assert(nodes.get('execute-plan').disabled);
 assert.match(content.textContent,/结果待核对/);assert(!all(content).some(n=>n.attrs.id==='execute-plan'));
 await nodes.get('reconcile-plan').handlers.click();assert.equal(reconcileCount,1);assert.equal(executeCount,1);
 assert.match(content.textContent,/尚未独立确认物理效果/);
 vm.runInContext("go('Developer')",context);assert.match(content.textContent,/设备来源/);
 fixture.entities.push({id:'ha:simulated:ac-temp',name:'SIMULATED AC auxiliary temperature',kind:'sensor',room:'Simulated Room',bindings:[],capabilities:[]});
 fixture.states.unshift({id:'ha:simulated:ac-temp:temperature',value:null,quality:'faulty',as_of:stamp,reasons:['upstream_unavailable']});
 fixture.entities.push({id:'ha:simulated:climate',name:'SIMULATED AC',kind:'climate',state_version:'SIMULATED-ac-version',room:'Simulated Room',bindings:[],capabilities:[{name:'climate.mode',access:'write',support:'supported',choices:['off','cool']},{name:'climate.temperature',access:'write',support:'supported',minimum:16,maximum:30,step:.5}]});
 fixture.devices.push({id:'device:simulated:ac',name:'SIMULATED AC',kind:'climate',category:'device',room:'Simulated Room',function_ids:['ha:simulated:climate'],capabilities:fixture.entities.at(-1).capabilities,bindings:[]});
 fixture.devices.at(-1).function_ids.unshift('ha:simulated:ac-temp');
 fixture.action_targets.push('ha:simulated:climate');fixture.states.push({id:'ha:simulated:climate:climate.state',value:'off',quality:'healthy',as_of:stamp,valid_until:new Date(Date.now()+60000).toISOString(),reasons:[]});
 vm.runInContext("go('Devices')",context);
 assert.match(all(content).find(n=>n.attrs['data-entity-id']==='device:simulated:ac').textContent,/关闭/,'unavailable auxiliary temperature does not hide available climate latch');
 const climateState=fixture.states.at(-1);climateState.quality='faulty';climateState.value=null;
 fixture.states[0].quality='healthy';fixture.states[0].value=24;fixture.states[0].unit='°C';
 vm.runInContext("render()",context);
 assert.match(all(content).find(n=>n.attrs['data-entity-id']==='device:simulated:ac').textContent,/暂不可确认/,'healthy auxiliary reading does not conceal unavailable actuator');
 climateState.quality='healthy';climateState.value='off';
 vm.runInContext("render()",context);
 await all(content).find(n=>n.attrs['data-entity-id']==='device:simulated:ac').handlers.click();
 const operation=nodes.get('device-action-operation');operation.value='climate.temperature';operation.handlers.change();
 nodes.get('device-action-value').value='24.5';nodes.get('device-action-value').handlers.input();
 vm.runInContext("render()",context);
 assert.equal(nodes.get('device-action-operation').value,'climate.temperature','quality expiry rerender preserves selected operation');
 assert.equal(nodes.get('device-action-value').value,'24.5','quality expiry rerender preserves typed target');
 await nodes.get('device-action-form').handlers.submit({preventDefault(){}});
 assert.equal(request.target,'ha:simulated:climate');assert.equal(request.operation,'climate.temperature');assert.deepEqual(request.desired,{temperature:24.5});
 assert.equal(request.expected_state_version,'SIMULATED-ac-version');assert.equal(executeCount,1,'device preview does not auto execute');
 assert.equal(nodes.get('device-action-value').value,'24.5','preview preserves typed parameter');
 assert.match(content.textContent,/点击执行才会操作这台设备/);
 console.log('PASS simulated physical projection -> typed light and AC plans -> explicit execution -> readonly reconcile');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable for Console DOM smoke")
def test_console_quality_dry_run_and_disconnection():
    root = Path(__file__).resolve().parents[1]
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const elements = new Map();
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.handlers = {}; this._text = ''; this.value = ''; this.checked = false; this.isConnected = true; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent ?? '').join(''); }
  set innerHTML(v) { throw Error('Unsafe HTML insertion'); }
  setAttribute(k,v) { this.attrs[k] = String(v); if(k === 'id') elements.set(String(v), this); if(k === 'value') this.value = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  append(...nodes) { this.children.push(...nodes); if(this.tag === 'select' && !this.value) this.value = this.children[0].value; }
  prepend(...nodes) { this.children.unshift(...nodes); }
  replaceChildren(...nodes) { this._text = ''; this.children = [...nodes]; }
  addEventListener(event,fn) { this.handlers[event] = fn; }
  scrollIntoView() {}
}
for(const id of ['navigation','developer-mode','mode','snapshot-time','page-title','content','notification','refresh']) { const n = new Element('div'); n.setAttribute('id',id); }
const sourceName = '<img src=x onerror="throw Error(1)">';
const stamp = new Date().toISOString();
const fixture = {mode:'simulator',version:'fixture-version-7',generated_at:stamp,
 entities:[{id:'light.fixture',name:sourceName,kind:'light',room:'Living',physical_source_id:'simulated:light',bindings:[],simulated:true,capabilities:[{name:'light.set',access:'write',support:'supported',minimum:2,maximum:254,minimum_verified:true,no_bright_flash_verified:true,verification:'simulated'}]},
 {id:'sensor.temperature',name:'Room temperature',kind:'sensor',room:'Living',physical_source_id:'simulated:temp',bindings:[],capabilities:[],simulated:true}],
 states:[{id:'sensor.temperature:temperature',value:28,unit:'°C',quality:'stale',reasons:['sample too old'],selected_source:'simulated:test',as_of:stamp,simulated:true}], observations:[],events:[],rules:[{id:'opaque-rule',source:'simulated:geek',normalization:'opaque',coverage_note:'Unknown graph semantics',readable:false,writable:false,simulated:true,raw:'{"unfamiliar":true}',fingerprint:'f'.repeat(64)}],limitations:['No real hardware verified']};
let disconnected = false, request = null, storedTrace = null, fetchedTrace = false, ageTick = null, clock = Date.now(), snapshotRequests = 0, homeQueries = 0;
const doctorReport={ha_ingress:{continuous_subscription:true,observation_gap:false,diagnostic_journal_available:true},capabilities:{}};
class TestDate extends Date { static now() { return clock; } }
const fetch = async (path,options={}) => {
 if(disconnected) throw Error('Offline test');
 if(path === '/api/doctor') return {ok:true,json:async()=>doctorReport};
 if(path === '/api/query') {
  homeQueries++;assert.deepEqual(JSON.parse(options.body),{kind:'state',rooms:['Living'],limit:20,capabilities:['temperature','humidity','light.state','climate.state','cover.state','lock.state','fan.state','contact.transition']});
  return {ok:true,json:async()=>({kind:'state',status:'ok',simulated:true,snapshot_at:stamp,next_cursor:null,
   items:[{kind:'state',id:'sensor.temperature:temperature',entity_id:'sensor.temperature',name:'Room temperature',capability:'temperature',value:homeQueries===1 ? null : 26.75,quality:homeQueries===1 ? 'stale' : 'healthy',reasons:homeQueries===1 ? ['measurement_expired'] : [],source_times:[],unit:'°C',valid_until:new Date(clock+5000).toISOString()}],
   permission_scope:{requested_rooms:['Living']},coverage:'selected_snapshot_only_not_physical_confirmation',limitations:[],execution_authority:false})};
 }
 if(path === '/api/snapshot') snapshotRequests++;
 if(path === '/api/actions/dry-run') { request = JSON.parse(options.body); storedTrace={action_id:'fixture-trace',status:'planned',code:'dry_run_only',reasons:['No device write'],route:'dry-run',real_write_attempted:false,request,events:[{event_id:'fixture-action-event',kind:'action-result',payload:{action_id:'fixture-trace'},source:'simulated:action',wall_time:stamp,simulated:true}],simulated:true}; return {ok:true,json:async()=>storedTrace}; }
 if(path === '/api/traces/fixture-trace') { fetchedTrace=true; return {ok:true,json:async()=>storedTrace}; }
 return {ok:true,json:async()=>fixture};
};
const context = vm.createContext({document:{getElementById:id=>elements.get(id),createElement:tag=>new Element(tag)},fetch,location:{hash:''},history:{replaceState(){}},crypto:{randomUUID:()=> 'fixture-uuid'},Date:TestDate,JSON,console,setInterval:(callback,delay)=>{assert.equal(delay,10000);ageTick=callback;return 1;}});
vm.runInContext(fs.readFileSync('homeplane/static/app.js','utf8'),context);
const settle = () => new Promise(resolve=>setImmediate(resolve));
function navigate(name) { vm.runInContext('go(' + JSON.stringify(name) + ')',context); }
function descendants(node) { return [node,...node.children.flatMap(descendants)]; }
(async()=>{
 await settle();
 const content = elements.get('content');
 assert.match(elements.get('mode').textContent,/示例家庭/);
 descendants(content).find(n=>n.attrs['data-entity-id']==='sensor.temperature').handlers.click();
 assert.match(content.textContent,/暂不可确认/);
 assert(!content.textContent.includes('28 °C'),'Untrusted temperature cannot appear as current');
 assert.equal(descendants(content).find(n=>n.attrs['data-quality']==='stale').attrs['data-state-id'],'sensor.temperature:temperature');
 navigate('Developer'); elements.get('scenario').value='stale-temperature'; await elements.get('load-scenario').handlers.click();
 assert.equal(elements.get('scenario').value,'stale-temperature'); assert.equal(elements.get('loaded-scenario').textContent,'stale-temperature');
 navigate('Automations'); assert.match(content.textContent,/Unknown graph semantics/); assert.match(content.textContent,/ffffffff/);
 navigate('Devices'); assert.match(content.textContent,/<img src=x/); assert.equal(descendants(content).filter(n=>n.tag==='img').length,0);
 descendants(content).find(n=>n.attrs['data-entity-id']==='light.fixture').handlers.click();
 elements.get('action-power').value='on';
 elements.get('action-brightness').value='2';
 await elements.get('action-form').handlers.submit({preventDefault(){}});
 assert.equal(request.expected_version,'fixture-version-7'); assert.equal(request.desired.power,'on'); assert.equal(request.desired.brightness,2);
 assert.equal(request.origin,'dry-run'); assert.equal(request.actor,'console');
 assert.equal(elements.get('action-power').value,'on','Preview must preserve the power draft');
 assert.equal(elements.get('action-brightness').value,'2','Preview must preserve the brightness draft');
 assert(new Date(request.expires_at)>new Date()); assert(new Date(request.expires_at)-new Date()<=30000);
 assert.match(content.textContent,/"real_write_attempted": false/);
 navigate('Activity');
 assert.match(content.textContent,/action-result/, 'New Trace event must be visible without refreshing snapshot');
 const viewTrace=descendants(content).find(n=>n.tag==='button' && n.attrs['data-action-id']==='fixture-trace'); assert(viewTrace);
 await viewTrace.handlers.click(); assert(fetchedTrace); assert.match(content.textContent,/"real_write_attempted": false/);
 fixture.events.push(
   {event_id:'SIMULATED-state-edge',kind:'state-transition',source:'ha',source_seq:'SIMULATED-connection:1',wall_time:stamp,simulated:true,payload:{entity_id:'light.fixture',from:'off',to:'on',trigger_eligible:true}},
   {event_id:'SIMULATED-gap',kind:'observation-gap',source:'ha',wall_time:stamp,simulated:true,payload:{reason:'SIMULATED-disconnect'}},
   {event_id:'SIMULATED-reconcile',kind:'state-snapshot',source:'ha',wall_time:stamp,simulated:true,payload:{reconciled_after_gap:true,trigger_eligible:false,event_replay:false}});
 navigate('Activity');
 assert.match(content.textContent,/设备状态变化/);assert.match(content.textContent,/off → on/);
 assert.match(content.textContent,/实物效果尚未确认/);
 assert.match(content.textContent,/缺口期间发生了什么仍未知/);
 assert.match(content.textContent,/不会补触发断连期间的动作/);
 navigate('System');await settle();assert.match(content.textContent,/持续接收状态变化/);
 doctorReport.ha_ingress.observation_gap=true;navigate('System');await settle();assert.match(content.textContent,/连接缺口/);
 fixture.mode='ha-read-only';navigate('Devices');const readsBefore=snapshotRequests;ageTick();await settle();assert.equal(snapshotRequests,readsBefore,'automatic refresh must preserve drafts');
 navigate('Activity');ageTick();await settle();assert.equal(snapshotRequests,readsBefore+1,'viewing HA activity refreshes read-only state');
 fixture.mode='simulator';
 fixture.states[0].quality='healthy'; fixture.states[0].value=28; fixture.states[0].valid_until=new Date(clock+5000).toISOString();
 navigate('Home'); descendants(content).find(n=>n.attrs['data-entity-id']==='sensor.temperature').handlers.click(); assert(descendants(content).some(n=>n.attrs['data-quality']==='healthy'));
 const originalAsOf=fixture.states[0].as_of, originalGenerated=fixture.generated_at;
 const beforeTick=content.children[0]; ageTick(); assert.equal(content.children[0],beforeTick,'Unchanged age check must not rerender');
 clock+=10000; ageTick();
 assert.equal(fixture.states[0].quality,'stale'); assert.equal(fixture.states[0].value,null);
 assert.equal(fixture.states[0].as_of,originalAsOf); assert.equal(fixture.generated_at,originalGenerated);
 assert.match(content.textContent,/snapshot_validity_expired/); assert.match(content.textContent,/读数已过期/); assert(!content.textContent.includes('暂不可确认 °C')); assert(!content.textContent.includes('null °C'));
 const expiredReading=descendants(content).find(n=>n.attrs.class==='reading'); assert.equal(expiredReading.textContent,'暂不可确认');
 assert.match(content.textContent,/1 项/);
 const afterTick=content.children[0]; ageTick(); assert.equal(content.children[0],afterTick);
 // Canonical room data keeps uncertainty separate from physical device inventory.
 fixture.derived_entities=[{id:'room.fixture',name:'Fixture room temperature',room:'Living',capabilities:[]}];
 const canonical={id:'room.fixture:temperature',value:24.5,value_interval:[24.3,24.7],unit:'°C',quality:'healthy',
   reasons:['relative_calibrated_fallback_not_absolute_truth'],calibration_version:'SIMULATED-relative-v1',
   selected_source:'simulated:ac',excluded_sources:{'simulated:temp':'fallback_calibration_required'},
   valid_until:new Date(clock+5000).toISOString(),as_of:stamp,simulated:true};
 fixture.states.push(canonical);
 fixture.quality_sources=[{entity_id:'sensor.temperature',physical_source_id:'simulated:temp',quality:'stale',
   placement:'occupied_zone',installation_height_m:1.2,context:'ambient',location:'Living',
   reasons:['battery_reading_stale_or_unknown'],simulated:true}];
 navigate('Home');
 const measurement=descendants(content).find(n=>n.attrs['data-canonical-id']==='room.fixture');
 assert(measurement); assert.match(measurement.textContent,/24.3–24.7 °C · 推算/);
 assert.match(measurement.textContent,/合成样本/); assert.match(measurement.textContent,/电量记录已过期/);
 assert(!descendants(content).some(n=>n.attrs['data-entity-id']==='room.fixture'));
 canonical.quality='unknown'; canonical.value=null; canonical.value_interval=[24.5,29.2];canonical.reasons=['peer_disagreement_unresolved'];
 navigate('Home'); const disputed=descendants(content).find(n=>n.attrs['data-canonical-id']==='room.fixture');
 assert.match(disputed.textContent,/24.5–29.2 °C · 读数不一致/);
 canonical.quality='healthy';canonical.value=24.5;canonical.value_interval=[24.3,24.7];canonical.reasons=[];
 clock+=10000; ageTick();
 const expired=descendants(content).find(n=>n.attrs['data-canonical-id']==='room.fixture');
 assert.equal(descendants(expired).find(n=>n.attrs.class==='reading').textContent,'暂不可确认');
 // A large household is bounded, filterable, searchable, and never inserts source HTML.
 fixture.entities.push(...Array.from({length:60},(_,i)=>({id:'sensor.extra.'+i,name:'Extra device '+i,room:'Other',kind:'sensor',capabilities:[],bindings:[]})));
 navigate('Devices');
 descendants(content).find(n=>n.tag==='button'&&n.textContent==='全部状态').handlers.click();
 assert.equal(descendants(content).filter(n=>n.attrs['data-entity-id']).length,24);
 const roomButtons=descendants(content).find(n=>n.attrs['aria-label']==='筛选房间');
 roomButtons.children.find(n=>n.textContent==='Other').handlers.click();
 assert(!descendants(content).some(n=>n.attrs['data-entity-id']==='light.fixture'));
 elements.get('device-search').value='Extra device 42'; elements.get('device-search').handlers.change();
 assert.equal(descendants(content).filter(n=>n.attrs['data-entity-id']).length,1);
 assert(descendants(content).some(n=>n.attrs['data-entity-id']==='sensor.extra.42'));
 elements.get('device-search').value='missing fixture device'; elements.get('device-search').handlers.change();
 assert.match(content.textContent,/没有找到设备/);
 vm.runInContext("deviceQuery=''; selectedRoom='*'",context);
 disconnected=true; await elements.get('refresh').handlers.click();
 assert.match(elements.get('mode').textContent,/连接中断/);
 navigate('Home'); navigate('Devices');
 assert.match(content.textContent,/连接恢复后可预览调整/);
 assert(!descendants(content).some(n=>n.attrs.id==='action-form'));
 navigate('Ask Home'); assert.match(content.textContent,/指定房间查看常用状态/);
 const actionBeforeQuery=request;disconnected=false;
 await elements.get('home-query-form').handlers.submit({preventDefault(){}});
 assert.equal(homeQueries,1);assert.equal(request,actionBeforeQuery,'read query cannot issue an action');
 assert.match(content.textContent,/示例数据 · simulated/);assert.match(content.textContent,/读数已过期/);
 assert.match(content.textContent,/硬件测量时间未知/);assert(!content.textContent.includes('28 °C'));
 assert('hidden' in elements.get('home-query-more').attrs);
 await elements.get('home-query-form').handlers.submit({preventDefault(){}});
 assert.match(content.textContent,/26.75 °C/);
 clock+=10000;ageTick();assert(!content.textContent.includes('26.75 °C'));
 assert.match(content.textContent,/读数已过期/);assert.equal(homeQueries,2,'local expiry must not poll queries or actions');
 console.log('PASS: stale quality, safe source text, opaque Graph, versioned expiring dry-run, Trace and disconnected navigation');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", script], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable for Console DOM smoke")
def test_console_settings_configuration_auth_and_probe_boundaries():
    root = Path(__file__).resolve().parents[1]
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const elements = new Map();
class Element {
 constructor(tag) { this.tag=tag; this.children=[]; this.attrs={}; this.handlers={}; this._text=''; this.value=''; this.isConnected=true; }
 set textContent(v) { this._text=String(v); this.children=[]; }
 get textContent() { return this._text+this.children.map(n=>n.textContent??'').join(''); }
 set innerHTML(_) { throw Error('Unsafe HTML insertion'); }
 setAttribute(k,v) { this.attrs[k]=String(v); if(k==='id') elements.set(String(v),this); if(k==='value') this.value=String(v); }
 removeAttribute(k) {delete this.attrs[k];}
 append(...nodes) { this.children.push(...nodes); if(this.tag==='select'&&!this.value) this.value=this.children[0].value; }
 replaceChildren(...nodes) { this._text=''; this.children=[...nodes]; }
 addEventListener(event,fn) { this.handlers[event]=fn; }
}
for(const id of ['navigation','developer-mode','mode','snapshot-time','page-title','content','notification','refresh']) { const n=new Element('div'); n.setAttribute('id',id); }
const fixture={mode:'simulator',version:'fixture-v1',generated_at:new Date().toISOString(),entities:[],states:[],events:[],rules:[],observations:[],limitations:[]};
const config={ha_url:'http://ha.local:8123',ha_instance:'Home',nas_url:'smb://nas.local/archive',nas_path:null,camera_service_url:'http://127.0.0.1:8001',camera_id_configured:true,camera_id_masked:'••••1234',geek_url:'http://gateway.local'};
const calls=[]; let snapshotUnavailable=true, redirect=null, credentialFailure=false, ageTick=null, haAuthorized=false;
let geekSession={status:'stopped',session_live:false,inventory_stale:true},geekStatusDeferred=false,releaseGeekStatus=null;
const modelReport={status:'ready',catalog:[{preset:'mimo-flash',label:'SIMULATED MiMo',input_cny_per_million:1,output_cny_per_million:2,saved:false,key_configured:false,active:false,media_fit:'native_miloco_adapter'}]};
const measurementReport={realm:'a'.repeat(64),revision:'SIMULATED-r1',simulated:true,omitted_count:0,sources:[{
 source_key:'b'.repeat(64),binding_fingerprint:'c'.repeat(64),name:'SIMULATED thermometer',entity_id:'SIMULATED-temp',room:'SIMULATED Room',configured:false,binding_valid:true,profile:null,excluded:false,state:{quality:'unknown',reasons:['hardware_sample_time_unknown']}}]};
Object.assign(measurementReport.sources[0],{identity_fingerprint:'d'.repeat(64),representativeness:'room_reference',simulated:true});
measurementReport.sources.push({source_key:'e'.repeat(64),binding_fingerprint:'f'.repeat(64),identity_fingerprint:'0'.repeat(64),name:'SIMULATED AC return air',entity_id:'SIMULATED-ac',room:'SIMULATED Room',configured:true,binding_valid:true,profile:{placement:'return_air',context:'fan-only',reporting:'periodic_sample',ttl_seconds:300},representativeness:'contextual_air',simulated:true});
measurementReport.groups=[{entity:'SIMULATED-room',name:'SIMULATED room temperature',location:'SIMULATED Room',primary_key:'b'.repeat(64),source_keys:['b'.repeat(64),'e'.repeat(64)],managed:false,binding_valid:false,calibration_status:'none',state:{quality:'unknown',value:null,reasons:[]}}];
let calibrationPreviews=0,calibrationApprovals=0,measurementConflict=false;
const realNow=Date.now;let elapsed=0;Date.now=()=>realNow()+elapsed;
const lifecycle = {};
let mdnsInstances = [{instance_id:'simulated-discovered-a',name:'Simulated HA',url:'http://192.168.10.40:8123',authenticated:false}];
let resumeSnapshot; let snapshotStalled = true;
const fetch=async(path,options={})=>{
 calls.push({path,method:options.method??'GET',body:options.body?JSON.parse(options.body):null});
 let result;
 if(path==='/api/snapshot') { if(snapshotStalled) { await new Promise(resolve=>{resumeSnapshot=resolve;}); snapshotStalled=false; } if(snapshotUnavailable) throw Error('No household source'); result=fixture; }
 else if(path==='/api/settings') {
  if(options.method==='PUT') { const patch=JSON.parse(options.body); assert(!('config' in patch)); assert(!('camera_id_masked' in patch)); assert(!('camera_id_configured' in patch)); assert(!('passcode' in patch)); Object.assign(config,patch); delete config.camera_id; }
  result={config:{...config},checks:{},credentials:{secret_values_returned:false,geek:{credential_available:true}},geek_background_enabled:false};
 }
 else if(path==='/api/settings/ha/status') result={configured:Boolean(config.ha_url),credential_available:haAuthorized&&Boolean(config.ha_url)};
 else if(path==='/api/settings/ha/discover') result={status:mdnsInstances.length?'found':'not_found',instances:mdnsInstances};
 else if(path==='/api/settings/ha/connect') result={authorization_url:'http://ha.local:8123/auth/authorize?state=fixture'};
 else if(path==='/api/settings/source/simulator'||path==='/api/settings/ha/activate') { snapshotUnavailable=false; result={...fixture,mode:path.endsWith('activate')?'ha':'simulator'}; }
 else if(path==='/api/settings/nas/probe') result={status:'mount_required',readable:false,open_url:'smb://nas.local/archive'};
 else if(path==='/api/settings/camera/probe') result={status:'bound_stream_unverified',account_bound:true};
 else if(path==='/api/settings/camera/media') result={status:'not_requested'};
 else if(path==='/api/settings/miloco/models') result=modelReport;
 else if(path==='/api/settings/miloco/models/save') {assert.equal(elements.get('settings-model-key').value,'');modelReport.catalog[0].saved=true;modelReport.catalog[0].key_configured=true;result={...modelReport,status:'saved_inactive'};}
 else if(path==='/api/settings/miloco/models/probe') result={status:'text_probe_pass',vision_verified:false};
 else if(path==='/api/settings/geek/credential') { assert.equal(elements.get('settings-geek-passcode').value,''); if(credentialFailure) throw Error('request contained test-private-passcode'); result={credential_available:true,passcode:'must-not-be-rendered'}; }
 else if(path==='/api/settings/geek/probe') result={status:'readonly_inventory_collected',simulated:false,writes_attempted:false,graph_semantics:'opaque',device_count:8,graph_count:4,scope_count:1,passcode:'must-not-be-rendered'};
 else if(path==='/api/settings/geek/background/install') result={installed:true,enabled:false,current_connection_preserved:true,background:{status:'blocked',background_service_live:true,keep_connected:false}};
 else if(path==='/api/settings/geek/connect') {geekSession={status:'online',session_live:true,inventory_stale:false,graph_count:4,last_change:{added:0,removed:0,modified:1},passcode:'must-not-be-rendered'};result=geekSession;}
 else if(path==='/api/settings/geek/disconnect') {geekSession={status:'stopped',session_live:false,inventory_stale:true};result=geekSession;}
 else if(path==='/api/settings/geek/session') {result=geekSession;if(geekStatusDeferred)await new Promise(resolve=>{releaseGeekStatus=resolve;});}
 else if(path==='/api/settings/arl/status') result={status:'not_running',ready:false};
 else if(path==='/api/settings/arl/connect') result={status:'ready',ready:true,runtime_version:'0.1.0',providers:[{name:'codex_app_server',availability:'disabled'}]};
 else if(path==='/api/settings/arl/probe') result={status:'sdk_roundtrip_pass',inference_attempted:false,device_write_attempted:false};
 else if(path==='/api/settings/measurements') result=measurementReport;
 else if(path==='/api/settings/measurements/group') {
  const data=JSON.parse(options.body);assert.equal(data.expected_revision,measurementReport.revision);assert.equal(data.members.length,2);assert(!('samples' in data));
  measurementReport.revision+='-group';Object.assign(measurementReport.groups[0],{managed:true,binding_valid:true});result=measurementReport;
 }
 else if(path==='/api/settings/measurements/calibration/preview') {
  const data=JSON.parse(options.body);assert.equal(data.expected_revision,measurementReport.revision);assert(!('samples' in data));calibrationPreviews++;
  result={candidate_id:'SIMULATED-preview',fingerprint:'a'.repeat(64),expires_at:new Date(Date.now()+120000).toISOString(),installed:false,
   calibration:{bias:2,residual_bound:.1,sample_count:13,reference_minimum:23.3,reference_maximum:24.5,relative_only:true,simulated:true}};
 }
 else if(path==='/api/settings/measurements/calibration/approve') {
  const data=JSON.parse(options.body);assert.equal(data.reviewed_relative_only,true);assert(!('calibration' in data));calibrationApprovals++;
  measurementReport.revision+='-approved';measurementReport.groups[0].calibration_status='active';result=measurementReport;
 }
 else if(path.startsWith('/api/settings/measurements/')) {
  if(measurementConflict) return {ok:false,status:409,json:async()=>({code:'source_conflict'})};
  const data=JSON.parse(options.body);assert.equal(data.expected_revision,measurementReport.revision);
  assert.equal(data.source_key,measurementReport.sources[0].source_key);assert(!('token' in data));
  measurementReport.revision+='-next';
  const source=measurementReport.sources[0];
  if(path.endsWith('profile')) {assert.equal(data.fields.placement,'SIMULATED bedside');source.configured=true;source.profile={...data.fields,context:data.fields.expected_context};source.state={quality:'suspect',reasons:['recovery_requires_distinct_samples_and_window']};}
  if(path.endsWith('exclude')) {source.excluded=true;source.excluded_until=new Date(Date.now()+3600000).toISOString();source.state={quality:'unknown',reasons:['temporarily_excluded']};}
  if(path.endsWith('resume')) {source.excluded=false;source.state={quality:'suspect',reasons:['recovery_requires_distinct_samples_and_window']};}
  result=measurementReport;
 }
 else throw Error('Unexpected Settings endpoint '+path);
 return {ok:true,json:async()=>result};
};
const context=vm.createContext({document:{getElementById:id=>elements.get(id),createElement:tag=>new Element(tag)},fetch,location:{hash:'#settings',assign:value=>redirect=value},history:{replaceState(){}},crypto:{randomUUID:()=> 'uuid'},URL,Date,JSON,console,setInterval:fn=>{ageTick=fn;},addEventListener:(name,fn)=>{lifecycle[name]=fn;}});
vm.runInContext(fs.readFileSync('homeplane/static/app.js','utf8'),context);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const get=id=>{assert(elements.has(id),'Missing '+id);return elements.get(id);};
const submit=id=>get(id).handlers.submit({preventDefault(){}});
const click=id=>get(id).handlers.click();
function descendants(node) { return [node,...node.children.flatMap(descendants)]; }
(async()=>{
 await settle(); await settle();
 const content=get('content');
 assert.match(content.textContent,/读取家里的设备与房间/,'Settings must work without a household snapshot');
 assert(resumeSnapshot, 'Household fetch is deliberately stalled while Settings is already usable');
 resumeSnapshot(); await settle(); await settle();
 assert.equal(get('settings-geek-passcode').attrs.type,'password');
 assert.equal(get('settings-camera-id').value,'');
 assert.match(content.textContent,/留空不更改/);
 assert(!calls.some(c=>c.path.endsWith('/probe')),'Initial render must not initiate probes');
 assert.equal(get('settings-model-probe').disabled,true);
 assert.equal(get('settings-model-key').attrs.type,'password');
 get('settings-model-key').value='SIMULATED-private-model-key';await submit('settings-model-form');
 assert.deepEqual(calls.at(-1).body,{preset:'mimo-flash',api_key:'SIMULATED-private-model-key'});
 assert.equal(get('settings-model-key').value,'');assert(!content.textContent.includes('SIMULATED-private-model-key'));
 assert.match(get('settings-model-status').textContent,/未启用感知/);
 await click('settings-model-probe');assert.match(get('settings-model-status').textContent,/视觉与家庭场景尚未验证/);
 assert(!calls.some(c=>c.path.includes('activate')&&c.path.includes('miloco')));
 assert(!content.textContent.includes("Hardware: unverified"));
 assert.match(get('settings-ha-result').textContent,/登录记录未就绪|尚未授权/);
 // Simulated OAuth succeeds while another cached Console tab retains the old status.
 // Returning to the tab refreshes saved configuration and auth without deleting drafts.
 haAuthorized=true; config.ha_url='http://192.168.50.40:8123';
 get('settings-geek-passcode').value='unsent-password-draft';
 lifecycle.pageshow(); await settle(); await settle();
 assert.equal(get('settings-ha-url').value,'http://192.168.50.40:8123');
 assert.match(get('settings-ha-result').textContent,/本机已有登录记录/);
 assert.equal(get('settings-geek-passcode').value,'unsent-password-draft');
 get('settings-geek-passcode').value='';
 get('settings-ha-instance').value='Unsaved label';
 haAuthorized=false; lifecycle.focus(); await settle(); await settle();
 assert.equal(get('settings-ha-instance').value,'Unsaved label');
 assert.match(get('settings-ha-result').textContent,/登录记录未就绪|尚未授权/);
 get('settings-ha-instance').value=config.ha_instance; haAuthorized=true;
 await click('refresh'); assert.match(get('settings-ha-result').textContent,/本机已有登录记录/);
 await click('settings-ha-connect'); assert.equal(redirect,'http://ha.local:8123/auth/authorize?state=fixture');
 get('settings-ha-url').value='http://changed-ha.local:8123';
 const beforeConnect=calls.length; await click('settings-ha-connect'); assert.equal(calls.length,beforeConnect);
 assert.match(get('settings-ha-result').textContent,/请先保存更改/);
 await submit('settings-ha-form'); assert.equal(config.ha_url,'http://changed-ha.local:8123');
 await click('settings-ha-status'); assert.match(get('settings-ha-result').textContent,/本机已有登录记录/);
 await click('settings-nas-probe'); assert.match(get('settings-nas-result').textContent,/需要挂载目录/);
 const share=descendants(get('settings-nas-result')).find(n=>n.tag==='a'); assert.equal(share.attrs.href,'smb://nas.local/archive');
 get('settings-nas-kind').value='path'; get('settings-nas-kind').handlers.change(); get('settings-nas-path').value='/Volumes/archive';
 assert.equal(get('settings-nas-url').disabled,true);
 const beforeNas=calls.length; await click('settings-nas-probe'); assert.equal(calls.length,beforeNas);
 await submit('settings-nas-form'); const nasSave=calls.findLast(c=>c.path==='/api/settings'&&c.method==='PUT').body; assert.equal(nasSave.nas_url,null); assert.equal(nasSave.nas_path,'/Volumes/archive');
 await submit('settings-camera-form'); assert(!('camera_id' in calls.findLast(c=>c.path==='/api/settings'&&c.method==='PUT').body),'Blank camera ID must preserve saved ID');
 get('settings-camera-id').value='new-camera-id'; await submit('settings-camera-form'); assert.equal(calls.findLast(c=>c.path==='/api/settings'&&c.method==='PUT').body.camera_id,'new-camera-id'); assert.equal(get('settings-camera-id').value,'');
 get('settings-camera-url').value='http://127.0.0.1:9001'; await submit('settings-camera-form'); assert.equal(get('settings-camera-login').attrs.href,'http://127.0.0.1:9001');
 await click('settings-camera-probe'); assert.match(get('settings-camera-result').textContent,/账号：已绑定/);
 get('settings-geek-passcode').value='test-private-passcode'; await submit('settings-geek-credential-form');
 assert.equal(calls.at(-1).body.passcode,'test-private-passcode'); assert.equal(get('settings-geek-passcode').value,'');
 assert(!content.textContent.includes('test-private-passcode')); assert(!content.textContent.includes('must-not-be-rendered'));
 credentialFailure=true; get('settings-geek-passcode').value='test-private-passcode'; await submit('settings-geek-credential-form'); assert.equal(get('settings-geek-passcode').value,''); assert(!content.textContent.includes('test-private-passcode'));
 assert.equal(get('settings-geek-credential').open,false,'saved PIN should be collapsed');
 await click('settings-geek-probe'); assert.match(get('settings-geek-session-result').textContent,/持续同步已停止/);
 assert(!calls.some(call=>call.path==='/api/settings/geek/probe'),'refresh never opens another PIN handshake');
 assert(!vm.runInContext('JSON.stringify(settingsData)',context).includes('test-private-passcode')); assert(!vm.runInContext('JSON.stringify(settingsData)',context).includes('must-not-be-rendered'));
 await click('settings-geek-connect');assert.match(get('settings-geek-session-result').textContent,/持续只读同步/);assert.match(get('settings-geek-session-result').textContent,/修改 1/);assert(!get('settings-geek-session-result').textContent.includes('must-not-be-rendered'));
 assert(!elements.has('settings-geek-background'),'one connection entry, no competing background button');
 geekSession={...geekSession,connection_host:'background_service',background_service_live:true,keep_connected:true};await vm.runInContext('refreshGeekSession()',context);assert.match(get('settings-geek-session-result').textContent,/关闭窗口仍继续/);
 geekSession.session_live=false;await vm.runInContext('refreshGeekSession()',context);assert.match(get('settings-geek-session-result').textContent,/状态已过期/);
 geekSession={status:'blocked',session_live:false,failure_code:'gateway_error_frame',passcode:'must-not-be-rendered'};await vm.runInContext('refreshGeekSession()',context);assert.match(get('settings-geek-session-result').textContent,/原因暂不可确认/);assert(!get('settings-geek-session-result').textContent.includes('must-not-be-rendered'));
 geekSession={status:'stopped',session_live:false,background_last_attempt:{outcome:'failed',checked_at:new Date().toISOString(),failure_phase:'websocket_connect',failure_code:'network_unreachable'}};
 await vm.runInContext('refreshGeekSession()',context);assert.match(get('settings-geek-session-result').textContent,/上次后台启用未完成/);assert.match(get('settings-geek-session-result').textContent,/本地网络权限/);assert.match(get('settings-geek-session-result').textContent,/配对码保持不变/);
 geekStatusDeferred=true;const oldStatus=vm.runInContext('refreshGeekSession()',context);await settle();assert(releaseGeekStatus);
 await click('settings-geek-connect');releaseGeekStatus();await oldStatus;geekStatusDeferred=false;
 assert.match(get('settings-geek-session-result').textContent,/持续只读同步/);assert(!get('settings-geek-session-result').textContent.includes('上次后台启用未完成'),'late poll must not overwrite a new connection');
 await click('settings-geek-disconnect');assert.match(get('settings-geek-session-result').textContent,/持续同步已停止/);
 await click('settings-simulator'); assert.match(get('mode').textContent,/示例家庭/);
 await click('settings-ha-activate'); assert.match(get('mode').textContent,/家庭已连接 · 只读/);
 // A late snapshot must expose the pause control without destroying Settings drafts.
 assert.equal(get('settings-actions-pause').disabled,true);
 fixture.mode='ha-read-only';fixture.action_targets=['SIMULATED-study-only'];
 get('settings-ha-instance').value='SIMULATED-unsaved-label';
 await click('refresh');
 assert.equal(get('settings-actions-pause').disabled,false);
 assert.match(get('settings-source-result').textContent,/设备受控/);
 assert.match(get('settings-ha-result').textContent,/已授权设备能力/);
 assert.equal(get('settings-ha-instance').value,'SIMULATED-unsaved-label');
 get('settings-ha-instance').value=config.ha_instance;
 // A household aging tick must not destroy a partially entered password or config draft.
 fixture.states=[{quality:'healthy',valid_until:'2000-01-01T00:00:00Z',reasons:[],value:1}];
 get('settings-geek-passcode').value='unsent-draft'; const beforeTick=get('settings-geek-passcode'); ageTick(); assert.equal(get('settings-geek-passcode'),beforeTick); assert.equal(get('settings-geek-passcode').value,'unsent-draft');
 // First-time users need no manual address entry. Discovery itself must not save or log in.
 config.ha_url=null; config.ha_instance=null;
 const beforeDiscovery=calls.length;
 vm.runInContext('settingsData=null; render()',context); await settle(); await settle();
 assert.equal(get('settings-ha-url').value,'http://192.168.10.40:8123');
 assert.equal(config.ha_url,null);
 assert(!calls.slice(beforeDiscovery).some(c=>c.path==='/api/settings/ha/connect'||c.method==='PUT'));
 await click('settings-ha-connect');
 assert.equal(config.ha_url,'http://192.168.10.40:8123');
 assert.equal(calls.at(-2).method,'PUT'); assert.equal(calls.at(-1).path,'/api/settings/ha/connect');
 mdnsInstances.push({instance_id:'simulated-discovered-b',name:'Same display name',url:'http://192.168.10.41:8123',authenticated:false});
 config.ha_url=null; config.ha_instance=null;
 vm.runInContext('settingsData=null; render()',context); await settle(); await settle();
 assert.equal(get('settings-ha-url').value,'','Multiple HA instances cannot be silently selected');
 const choice=get('settings-ha-discovered-instance'); choice.value='1'; choice.handlers.change();
 assert.equal(get('settings-ha-url').value,'http://192.168.10.41:8123');
 await click('settings-ha-connect'); assert.equal(config.ha_url,'http://192.168.10.41:8123');
 // Explicit discovery must preserve an already saved endpoint and unsaved URL draft.
 get('settings-ha-url').value='http://unsaved-ha.local:8123';
 await click('settings-ha-discover'); assert.equal(get('settings-ha-url').value,'http://unsaved-ha.local:8123');
 for(const endpoint of ['/api/settings','/api/settings/ha/connect','/api/settings/ha/status','/api/settings/ha/activate','/api/settings/source/simulator','/api/settings/nas/probe','/api/settings/camera/probe','/api/settings/geek/credential','/api/settings/geek/session']) assert(calls.some(c=>c.path===endpoint),'Missing API '+endpoint);
 assert.match(get('settings-arl-result').textContent,/ARL 尚未运行/);
 assert(!calls.some(c=>c.path==='/api/settings/arl/connect'),'opening Settings never starts shared runtime');
 await click('settings-arl-connect'); assert.match(get('settings-arl-result').textContent,/ARL 已连接/);
 assert.match(get('settings-arl-result').textContent,/codex_app_server · 尚未启用/);
 await click('settings-arl-probe'); assert.match(get('settings-arl-result').textContent,/共享连接往返检查通过/);
 assert(!calls.some(c=>c.path.includes('model.complete')),'SDK check never runs a model');
 vm.runInContext("selectedConnection='settings-measurements';render()",context);await settle();await settle();
 const modelDraft=get('settings-model-key');modelDraft.value='SIMULATED-unsent-model-draft';
 get('settings-measurement-placement').value='SIMULATED bedside';get('settings-measurement-context').value='ambient';
 await submit('settings-measurement-form');await settle();await settle();
 assert.match(get('settings-measurement-status').textContent,/配置已保存/);
 assert.equal(get('settings-model-key'),modelDraft);assert.equal(modelDraft.value,'SIMULATED-unsent-model-draft');
 await click('settings-measurement-exclude');await settle();await settle();
 assert.match(get('settings-measurements-result').textContent,/临时排除/);
 await click('settings-measurement-resume');await settle();await settle();
 assert.match(get('settings-measurement-status').textContent,/重新观察/);
 measurementConflict=true;await submit('settings-measurement-form');await settle();await settle();
 assert.match(get('settings-measurement-status').textContent,/配置已变化/);
 measurementConflict=false;
 assert.equal(get('settings-measurement-group-save').attrs.form,'settings-measurement-group-form','outside submit button must bind its form');
 await submit('settings-measurement-group-form');await settle();await settle();
 assert.match(get('settings-measurement-group-status').textContent,/主备来源已存/);
 const previewButton=()=>descendants(get('settings-measurements-result')).find(n=>n.attrs['data-calibration-preview']);
 await previewButton().handlers.click();await settle();
 assert.equal(calibrationPreviews,1);assert.equal(calibrationApprovals,0,'preview never installs');
 assert.match(get('settings-measurements-result').textContent,/13 对合格测量/);
 const approvalButton=()=>descendants(get('settings-measurements-result')).find(n=>n.attrs['data-calibration-approval']);
 elapsed=121000;ageTick();assert(approvalButton().disabled);assert.match(get('settings-measurements-result').textContent,/预览已过期/);
 elapsed=0;await previewButton().handlers.click();await approvalButton().handlers.click();await settle();await settle();
 assert.equal(calibrationApprovals,1);assert.match(get('settings-measurement-group-status').textContent,/相对校准已安装/);
 assert.equal(get('settings-model-key'),modelDraft);assert.equal(modelDraft.value,'SIMULATED-unsent-model-draft');
 assert(!calls.some(c=>c.path.startsWith('/api/actions/execute')));
 console.log('PASS: Settings recovery, flat configuration patches, OAuth, mount, private credentials and explicit ARL discovery/probe');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", script], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable for Console DOM smoke")
def test_arl_review_is_explicit_default_off_and_edits_drop_late_candidates():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),nodes=new Map();
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.attrs={};this._text='';this.value='';this.isConnected=true;}
 set textContent(v){this._text=String(v);this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent??'').join('');}
 set innerHTML(_){throw Error('Unsafe HTML');}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='id')nodes.set(String(v),this);}
 append(...children){for(const child of children){child.parent=this;this.children.push(child);}}
 replaceChildren(...children){this._text='';this.children=[];this.append(...children);}
 addEventListener(k,v){this.handlers[k]=v;}
 remove(){this.isConnected=false;if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}
 scrollIntoView(){}
}
for(const id of ['navigation','developer-mode','mode','snapshot-time','page-title','content','notification','refresh']){const n=new Element('div');n.setAttribute('id',id);}
const fixture={mode:'simulator',version:'SIMULATED',generated_at:new Date().toISOString(),entities:[],devices:[],states:[],observations:[],events:[],rules:[],limitations:[]};
const light={id:'SIMULATED-light-on',type:'light.set',power:'on',target:'light.simulated'},ac={id:'SIMULATED-ac-on',type:'climate.set',power:'on',target:'climate.simulated'};
const rule={id:'SIMULATED-arrival',intent:'SIMULATED arrival',trigger:{entity:'SIMULATED-door',from_value:'closed',to_value:'open'},actions:[light,ac]};
const plan={id:'SIMULATED-plan',status:'validated',old:{rule},new_rule:{...rule,actions:[light]},history:[]};
let enabled=false,pending=null,delayReview=false,sequence=0,posts=[],delayAnalysis=false;
const scope=()=>({available:true,simulated:true,fingerprint:'a'.repeat(64),analysis_enabled:enabled});
const response=value=>({ok:true,json:async()=>value});
const fetch=async(path,options={})=>{
 if(path==='/api/rules/analysis/context')return response(scope());
 if(path==='/api/rules/text/context')return response({available:false});
 if(path==='/api/rules/managed')return response({available:false,records:[{id:'SIMULATED-record',rule,enabled:true}]});
 if(path==='/api/rules/runtime')return response({available:false});
 if(options.method==='POST'){
  const body=JSON.parse(options.body);posts.push([path,body]);
  if(path==='/api/rules/analysis/reviews'){
   assert.equal(body.expected_context,'a'.repeat(64));
   const review={id:'SIMULATED-review-'+(++sequence),approval_digest:'b'.repeat(64),context:scope(),expires_at:new Date(Date.now()+60000).toISOString(),model:'SIMULATED-model',request:{text:body.text},choices:[{rule_label:'SIMULATED <script>unsafe label</script>',action_label:'SIMULATED AC',retained_actions:['SIMULATED lamp']}]};
   if(delayReview)return new Promise(resolve=>{pending=()=>resolve(response(review));});
   return response(review);
  }
  if(path==='/api/rules/analysis/analyze'){
   assert.deepEqual(Object.keys(body).sort(),['approval_digest','review_id']);
   const result={status:'planned',plan,context:scope(),baseline_comparison:'matched',summary:'SIMULATED candidate'};
   if(delayAnalysis)return new Promise(resolve=>{pending=()=>resolve(response(result));});
   return response(result);
  }
  if(path==='/api/rules/analysis/cancel')return response({status:'rejected',code:'analysis_cancelled_before_call'});
  throw Error('Unexpected effect path '+path);
 }
 return response(fixture);
};
const context=vm.createContext({document:{getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag)},fetch,location:{hash:''},history:{replaceState(){}},crypto:{randomUUID:()=> 'SIMULATED-key-'+(++sequence)},Date,JSON,console,setInterval:()=>1});
vm.runInContext(fs.readFileSync('homeplane/static/app.js','utf8'),context);
const settle=()=>new Promise(resolve=>setImmediate(resolve)),content=nodes.get('content');
const type=text=>{const n=nodes.get('rule-analysis-input');n.value=text;n.handlers.input();};
const preview=async()=>{await nodes.get('rule-analysis-form').handlers.submit({preventDefault(){}});await settle();};
(async()=>{
 await settle();vm.runInContext("go('Automations')",context);await settle();
 nodes.get('rule-analysis').open=true;nodes.get('rule-analysis').handlers.toggle();
 vm.runInContext('render()',context);await settle();assert(nodes.get('rule-analysis').open);
 type('SIMULATED remove AC');await preview();
 assert(nodes.get('rule-analysis-analyze').disabled);assert.match(content.textContent,/现有 Codex 账号/);
 assert.match(content.textContent,/尚未发送/);assert.match(content.textContent,/<script>unsafe label<\/script>/);
 assert(!posts.some(([p])=>p.endsWith('/analyze')));
 type('SIMULATED new request');await settle();assert(posts.some(([p])=>p.endsWith('/cancel')));
 enabled=true;vm.runInContext('render()',context);await settle();await preview();
 await nodes.get('rule-analysis-analyze').handlers.click();await settle();
 assert.match(content.textContent,/候选已转成变更预览/);assert.equal(vm.runInContext('lastRulePlan.new_rule.actions.length',context),1);
 type('SIMULATED changed after result');assert.equal(vm.runInContext('lastRulePlan',context),null);
 delayReview=true;const job=nodes.get('rule-analysis-form').handlers.submit({preventDefault(){}});await settle();assert(pending);
 type('SIMULATED changed during preview');pending();await job;await settle();
 assert.equal(vm.runInContext('ruleAnalysisReview',context),null);assert.equal(vm.runInContext('lastRulePlan',context),null);
 delayReview=false;await preview();delayAnalysis=true;
 const inference=nodes.get('rule-analysis-analyze').handlers.click();await settle();
 type('SIMULATED changed during analysis');pending();await inference;await settle();
 assert.equal(vm.runInContext('lastRulePlan',context),null);
 assert(posts.every(([p])=>p.startsWith('/api/rules/analysis/')),'no apply/service/actor enrollment path');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=Path(__file__).parents[1], text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='Node unavailable for Console DOM smoke')
def test_console_opaque_geek_inventory_refresh_and_late_response():
    root = Path(__file__).resolve().parents[1]
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const elements=new Map();
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.handlers={};this._text='';this.isConnected=true;}
 set textContent(v){this._text=String(v);this.children=[];}
 get textContent(){return this._text+this.children.map(n=>n.textContent??'').join('');}
 set innerHTML(_){throw Error('unsafe HTML');}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='id')elements.set(String(v),this);}
 getAttribute(k){return this.attrs[k];}
 removeAttribute(k){delete this.attrs[k];}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this._text='';this.children=[...nodes];}
 addEventListener(k,v){this.handlers[k]=v;}
}
for(const id of ['navigation','developer-mode','mode','snapshot-time','page-title','content','notification','refresh']){const e=new Element('div');e.setAttribute('id',id);}
const get=id=>elements.get(id),fixture={mode:'simulator',version:'simulated',generated_at:new Date().toISOString(),entities:[],states:[],events:[],rules:[],observations:[],limitations:[]};
let report={status:'ready',fingerprint:'c'.repeat(64),observed_at:new Date().toISOString(),session:{status:'online',session_live:true,inventory_stale:false,checked_at:new Date().toISOString(),gap:false},graphs:[{id:'simulated-graph',name:'simulated <script>name',source_enabled:true,fingerprint:'a'.repeat(64),raw_sha256:'b'.repeat(64),nodes:'sensitive-never-render',passcode:'secret-never-render'}]};
let deferred=null,calls=0,agingTimer=null;
const context=vm.createContext({console,Date,Map,Set,JSON,Number,String,Array,Object,Promise,AbortSignal,
 document:{getElementById:get,createElement:tag=>new Element(tag),hidden:false},
 location:{hash:'#automations',origin:'http://127.0.0.1:8740'},history:{replaceState(){}},setInterval(fn){agingTimer=fn;},
 fetch:async(path)=>{let result;if(path==='/api/snapshot')result=fixture;else if(path==='/api/settings/geek/inventory'){calls++;result=deferred?await deferred:report;}else result={};return {ok:true,json:async()=>result};}
});
vm.runInContext(fs.readFileSync('homeplane/static/app.js','utf8'),context);
const flush=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
(async()=>{
 await flush();assert(calls>0);assert.match(get('geek-rule-inventory-result').textContent,/simulated <script>name/);
 assert.match(get('geek-rule-inventory-result').textContent,/来源标记.*启用/);
 assert(!get('content').textContent.includes('sensitive-never-render'));assert(!get('content').textContent.includes('secret-never-render'));
 const row=get('geek-rule-inventory-result').children[1];row.children[2].open=true;await vm.runInContext('refreshGeekInventory()',context);assert.equal(get('geek-rule-inventory-result').children[1],row);assert(row.children[2].open);
 fixture.states.push({quality:'healthy',value:20,valid_until:new Date(Date.now()-1000).toISOString(),reasons:[]});agingTimer();await flush();assert.equal(fixture.states[0].quality,'stale');assert.equal(fixture.states[0].value,null);assert.equal(get('geek-rule-inventory-result').children[1],row);assert(row.children[2].open);
 report={...report,status:'stale'};await vm.runInContext('refreshGeekInventory()',context);assert.match(get('geek-rule-inventory-result').textContent,/过期/);assert(!get('geek-rule-inventory-result').textContent.includes('simulated <script>name'));
 report={...report,status:'integrity_failed'};await get('geek-rule-inventory-refresh').handlers.click();assert.match(get('geek-rule-inventory-result').textContent,/完整性/);
 report={...report,status:'ready',session:{...report.session,session_live:false}};await vm.runInContext('refreshGeekInventory()',context);assert(!get('geek-rule-inventory-result').textContent.includes('simulated <script>name'));
 let release;deferred=new Promise(resolve=>release=resolve);const pending=vm.runInContext('refreshGeekInventory()',context);await flush();
 const old=get('geek-rule-inventory-result');vm.runInContext("page='Devices';render()",context);release({...report,status:'ready',session:{...report.session,session_live:true}});await pending;
 assert(!old.textContent.includes('simulated <script>name'));assert(!get('content').textContent.includes('simulated <script>name'));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(['node', '-e', script], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
