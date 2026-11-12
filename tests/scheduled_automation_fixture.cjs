// Original automation workspace with inert DOM/API. No browser or device I/O.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const all=n=>[n,...n.children.flatMap(all)],settle=()=>new Promise(setImmediate);
function fixture({zone='America/New_York',existing=true,rule={},capabilities=null,beforeRequest=null,controlId='light',sensorId='sensor',triggerCapability='binary_sensor.state',recordOverride=null,trialTrigger=null,modeReport=null,planReceipt=null,trialReceipt=null,attentionId=null,acknowledge=null}={}){
 let focused,planned=null,applied=0,sequence=0,statusSink=null;
 class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.dataset={};this.listeners={};this._text='';this._value=null;this.disabled=false;this.hidden=false;this.checked=false;}
  set textContent(v){this._text=String(v);this.children=[];}
  get textContent(){return this._text+this.children.map(n=>n.textContent).join(' ');}
  set value(v){this._value=String(v);}
  get value(){return this._value??(this.tagName==='select'?this.options[0]?.value||'':'');}
  get options(){return this.tagName==='select'?this.children:[];}
  get isConnected(){return this===body||!!this.parent?.isConnected;}
  setAttribute(k,v){this.attrs[k]=String(v);if(k==='value')this.value=v;}
  getAttribute(k){return this.attrs[k]??null;}
  append(...items){for(const n of items){if(n.parent)n.remove();n.parent=this;this.children.push(n);}}
  insertBefore(node,before){if(node.parent)node.remove();const index=before?this.children.indexOf(before):this.children.length;node.parent=this;this.children.splice(index,0,node);}
  contains(node){return all(this).includes(node);}
  querySelector(selector){return all(this).find(n=>n.tagName===selector)||null;}
  replaceChildren(...items){for(const n of this.children)n.parent=null;this.children=[];this._text='';this.append(...items);}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null;}
  addEventListener(type,fn){(this.listeners[type]??=[]).push(fn);}
  fire(type){const event={currentTarget:this,target:this,preventDefault(){}};let result;for(let node=this;node;node=['input','change','click'].includes(type)?node.parent:null){event.currentTarget=node;for(const fn of node.listeners[type]||[])fn(event);const value=node['on'+type]?.(event);if(node===this)result=value;}return result;}
  showModal(){this.open=true;}
  close(){this.open=false;}
  focus(){focused=this;}
 }
 const body=new Element('body'),root=new Element('main');body.append(root);
 const control={id:controlId,name:'SYNTHETIC light',capabilities:[{name:'light.set',access:'write',support:'supported'}]};
 if(capabilities)control.capabilities=capabilities;
 const data={devices:[{room:'SYNTHETIC room',controls:[control],readings:[{id:sensorId,name:'SYNTHETIC contact'}]}]};
 const snapshot={entities:[{id:sensorId,name:'SYNTHETIC contact',kind:'binary_sensor',capabilities:[{name:triggerCapability+'.transition',access:'event',support:'supported'}]}]};
 const record=recordOverride?structuredClone(recordOverride):{id:'record',fingerprint:'a'.repeat(64),enabled:true,rule:{id:'SYNTHETIC-timer',revision:1,intent:'SYNTHETIC daily light',trigger:{type:'schedule',at:'22:00',weekdays:[1,2,3,4,5],timezone:'Asia/Shanghai'},actions:[{id:'one',type:'device.set',target:'light',operation:'light.set',desired:{power:'on'}}],conditions:[],execution:{preferred_backend:'home_core',max_event_age_ms:3000},simulated:false}};
 Object.assign(record.rule,structuredClone(rule));
 const calls=[];
 const api=async(path,request)=>{
  calls.push({path,body:request&&JSON.parse(JSON.stringify(request))});
  if(beforeRequest)await beforeRequest(path,request);
  if(path==='/api/rules/core/records')return{available:true,fingerprint:'b'.repeat(64),records:existing?[structuredClone(record)]:[]};
  if(path==='/api/embodied/scenes')return{scenes:[]};
  if(path==='/api/rules/core/context')return{fingerprint:'b'.repeat(64),household_timezone:zone};
  if(path==='/api/rules/core/plans'){planned=JSON.parse(JSON.stringify(request));return planReceipt||{id:'plan',status:'validated'};}
  if(path==='/api/rules/core/plans/trial')return trialReceipt||{
   id:'plan',status:'awaiting_approval',approval_digest:'c'.repeat(64),start_paused:Boolean(planned.start_paused),
   new:{id:'new-record',enabled:false,rule:{...planned.rule,trigger:trialTrigger||planned.rule.trigger}},
  };
  if(path==='/api/rules/core/plans/apply'){
   applied++;
   return{id:'plan',status:'verified',approval_digest:'c'.repeat(64),new:{id:'new-record',enabled:!planned.start_paused}};
  }
  if(path.startsWith('/api/home/automations/records/attention/')&&path.endsWith('/acknowledge'))return acknowledge?acknowledge(path,request):{status:'acknowledged',id:path.split('/').at(-2),device_actions:false};
  throw Error('Unreviewed synthetic API path '+path);
 };
 const documentEvents=new EventTarget();
 const context=vm.createContext({document:{body,createElement:t=>new Element(t),get activeElement(){return focused;},
  addEventListener:documentEvents.addEventListener.bind(documentEvents),removeEventListener:documentEvents.removeEventListener.bind(documentEvents),dispatchEvent:documentEvents.dispatchEvent.bind(documentEvents)},
  Intl,Date,JSON,structuredClone,crypto:{randomUUID:()=> 'SYNTHETIC-uuid-'+(++sequence)},
  catalogSourceName:source=>({mijia:'米家',geek:'极客版'})[source],
  mountAutomationCatalog(){},mountAutomationMode(root,options){const sink=options.onStatus;statusSink=sink;if(modeReport)sink?.(modeReport);return{dispose(){if(statusSink===sink)statusSink=null;}};}});
 const source=fs.readFileSync('homeplane/static/automations.js','utf8').replace(/^import .*;$/gm,'').replace(/export /g,'');
 context.mountTaskAttention=vm.runInContext('(function(){'+fs.readFileSync('homeplane/static/task-attention.js','utf8').replace(/export /g,'')+';return mountTaskAttention;})()',context);
 const goCalls=[],toasts=[];vm.runInContext(source,context);const owner=context.mountAutomations(root,{api,data,snapshot,go(...args){goCalls.push(args);},toast(...args){toasts.push(args);},legacyRules(){},attentionId});
 const by=(tag,text)=>all(body).find(n=>n.tagName===tag&&n.textContent===text);
 const label=text=>all(body).find(n=>n.attrs['aria-label']===text);
 return{body,root,owner,by,label,calls,record,context,goCalls,toasts,get focused(){return focused;},description:context.scheduleDescription,
  modeStatus(report){statusSink?.(report);},get planned(){return planned;},get applied(){return applied;}};
}
async function main(){
 {
  const f=fixture();await settle();f.by('button','自动运行').fire('click');
  assert(f.root.textContent.includes('周一至周五 22:00'));assert(!f.root.textContent.includes('触发设备待核对'));
  f.by('button','编辑').fire('click');await settle();
  assert.equal(f.label('触发方式').value,'schedule');assert.equal(f.label('执行时间').value,'22:00');
  f.label('执行时间').value='23:15';f.label('周六').checked=true;
  await all(f.body).find(n=>n.tagName==='form').fire('submit');
  assert.equal(f.planned.rule.trigger.timezone,'Asia/Shanghai','editing preserves the explicitly approved clock zone');
  assert.equal(f.planned.rule.trigger.at,'23:15');assert.deepEqual(f.planned.rule.trigger.weekdays,[1,2,3,4,5,6]);
  assert.equal(f.applied,0,'a preview does not enable an edited task');
  await f.by('button','确认保存并启用').fire('click');assert.equal(f.applied,1);f.owner.dispose();
 }
 {
  const f=fixture({existing:false});await settle();f.by('button','新建自动化').fire('click');await settle();
  const kind=f.label('触发方式');assert.equal(kind.options.find(n=>n.value==='schedule').disabled,false);
  kind.value='schedule';kind.fire('change');f.label('执行时间').value='20:30';f.label('自动化名称').value='SYNTHETIC new timer';
  f.label('动作设备').value='light';f.label('动作设备').fire('change');
  await all(f.body).find(n=>n.tagName==='form').fire('submit');
  assert.equal(f.planned.rule.trigger.timezone,'America/New_York');assert.equal(f.planned.rule.trigger.type,'schedule');
  assert.deepEqual(f.planned.rule.trigger.weekdays,[1,2,3,4,5,6,7]);
  f.owner.dispose();
 }
 {
  const f=fixture({zone:null,existing:false});await settle();f.by('button','新建自动化').fire('click');await settle();
  assert.equal(f.label('触发方式').options.find(n=>n.value==='schedule').disabled,true);
  f.label('触发方式').value='schedule';f.label('触发方式').fire('change');
  await all(f.body).find(n=>n.tagName==='form').fire('submit');
  assert.equal(f.planned,null);assert(f.body.textContent.includes('家庭时区尚未就绪'));f.owner.dispose();
 }
 {
  const f=fixture({existing:false});await settle();f.by('button','新建自动化').fire('click');await settle();
  f.label('触发设备').value='sensor';f.label('触发设备').fire('change');
  f.label('动作设备').value='light';f.label('动作设备').fire('change');f.label('只在指定时段运行').checked=true;
  await all(f.body).find(n=>n.tagName==='form').fire('submit');
  assert.equal(f.planned.rule.conditions[0].timezone,'America/New_York','time conditions also use family metadata');f.owner.dispose();
 }
 console.log('Original automation workspace: schedule list, editable days/time, exact preview approval, family timezone and missing-source refusal: OK');
}
module.exports={fixture,all,settle};
if(require.main===module)main().catch(error=>{console.error(error);process.exitCode=1;});
