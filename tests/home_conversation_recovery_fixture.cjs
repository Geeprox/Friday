// Actual widget and app lifecycle functions, synthetic DOM/transport only.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {webcrypto} = require('node:crypto');
const copyDOM = require('./copy_layout_dom.cjs');
const sourceRoot = path.resolve(process.argv[2]);
const widgetSource = fs.readFileSync(path.join(sourceRoot,'homeplane/static/home-conversation.js'),'utf8');
const appSource = fs.readFileSync(path.join(sourceRoot,'homeplane/static/app.js'),'utf8');
let htmlWrites = 0;
class Element {
  constructor(tag) {this.tagName=tag.toUpperCase();this.children=[];this.attributes={};this.listeners=new Map();this._text='';this.parentNode=null;this.disabled=false;this.value='';this.dataset={};this.hidden=false;this.open=false;}
  set textContent(v) {this._text=String(v);this.replaceChildren();}
  get textContent() {return this._text+this.children.map(c=>c.textContent).join('');}
  set innerHTML(_) {htmlWrites++;throw Error('No evidence HTML execution');}
  setAttribute(k,v) {this.attributes[k]=String(v);}
  getAttribute(k) {return this.attributes[k]??null;}
  removeAttribute(k) {delete this.attributes[k];}
  get lastElementChild() {return this.children.at(-1);}
  get isConnected() {return this===this.ownerDocument?.body||!!this.parentNode?.isConnected;}
  append(...nodes) {for(const n of nodes){n.remove();n.parentNode=this;this.children.push(n);}}
  replaceChildren(...nodes) {for(const n of this.children)n.parentNode=null;this.children=[];this.append(...nodes);}
  remove() {if(this.parentNode){this.parentNode.children=this.parentNode.children.filter(n=>n!==this);this.parentNode=null;}}
  addEventListener(t,f) {const a=this.listeners.get(t)??[];a.push(f);this.listeners.set(t,a);}
  removeEventListener(t,f) {this.listeners.set(t,(this.listeners.get(t)??[]).filter(x=>x!==f));}
  fire(t,fields={}) {if(t==='click'&&this.disabled)return;for(const f of this.listeners.get(t)??[])f({preventDefault(){},key:'',ctrlKey:false,metaKey:false,...fields});}
  querySelector(selector) {return this.querySelectorAll(selector)[0]??null;}
  focus() {this.ownerDocument.activeElement=this;}
  pause() {} load() {} scrollIntoView() {}
}
function environment() {
  const clock={now:1700000000000};class ClockDate extends Date {static now(){return clock.now;}}
  const document={createElement:t=>new Element(t),activeElement:null};document.body=new Element('body');
  const timers=new Set();const interfaces=copyDOM.install(Element,document,document.body);
  const forbidden=()=>{throw Error('No real network or persistent text authorized');};
  const context=vm.createContext({console,document,Date:ClockDate,TextEncoder,AbortController,crypto:webcrypto,
    ...interfaces,setInterval:f=>(timers.add(f),f),clearInterval:f=>timers.delete(f),
    addEventListener(){},removeEventListener(){},fetch:forbidden,localStorage:{getItem:forbidden,setItem:forbidden},sessionStorage:{getItem:forbidden,setItem:forbidden}});
  vm.runInContext(fs.readFileSync(path.join(sourceRoot,'homeplane/static/copy-layout.js'),'utf8'),context);
  let source=widgetSource.replace(/^import '\.\/copy-layout.js';\n/,'').replace('export function mount','function mount').replace('export const HomeConversation','const HomeConversation');
  vm.runInContext(source+'\nglobalThis.ActualWidget = HomeConversation;',context);
  const host=document.createElement('main'), original=document.createElement('section');original.textContent='Original structured query';host.append(original);document.body.append(host);
  return {clock,document,timers,context,host,original,Widget:context.ActualWidget,tick(){for(const f of [...timers])f();}};
}
const settled=async()=>{for(let n=0;n<12;n++)await Promise.resolve();};
const response=(e,request={},extra={})=>({session_id:request.session_id??'SIMULATED:session-a',turn_id:request.turn_id??'bootstrap:1',operation_id:request.operation_id??'bootstrap:1',code:'flow_ready',phase:'result',input_kind:'console_text',caption:'合成结果',expires_at:new Date(e.clock.now+60000).toISOString(),asr_admission:'unavailable',visual_admission:'unavailable',native_graph:'unavailable',nas_lease:'closed',output_audibility:'unavailable',...extra});
function deferred() {let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
function widgetRig(draft='') {
  const e=environment(),sent=[],cancelled=[],waiting=[];
  const client={send(r){sent.push(JSON.parse(JSON.stringify(r)));const d=deferred();waiting.push(d);return d.promise;},cancel(r){cancelled.push(JSON.parse(JSON.stringify(r)));return Promise.resolve(response(e,r,{phase:'cancelled',code:'flow_cancelled',caption:'本次请求已停止'}));}};
  const widget=e.Widget.mount(e.host,{sourceToken:'SIMULATED:source-a',client,initialDraft:draft});widget.update({response:response(e)});
  return {...e,widget,sent,cancelled,waiting};
}
const root=e=>e.host.querySelector('.hc-root');
const field=e=>root(e).querySelector('[data-hc="input"]');
const type=(e,text)=>{field(e).value=text;field(e).fire('input');};
const submit=e=>root(e).querySelector('form').fire('submit');
const button=(e,name)=>root(e).querySelector(`[data-hc="${name}"]`);
const enabled=(e,name)=>root(e).querySelectorAll(`[data-hc="${name}"]`).filter(x=>!x.disabled);
function copyChecks(e) {
  const name=root(e).querySelector('[data-hc="state"]').textContent.trim(),description=root(e).querySelector('[data-hc="state-description"]').textContent.trim();
  assert(name.length>=2&&name.length<=4,name);assert(description.length<=20,description);
}
const preview=(e,r)=>response(e,r,{phase:'preview',code:'flow_preview',plan_id:'SIMULATED:plan',approval_digest:'a'.repeat(64),domain:{plan:{id:'SIMULATED:plan',status:'awaiting_approval',expires_at:new Date(e.clock.now+60000).toISOString(),old:{id:'SIMULATED:rule',fingerprint:'b'.repeat(64),rule:{actions:[{id:'SIMULATED:action',type:'light.set',target:'SIMULATED:lamp'}]}},new_rule:{actions:[]}}}});
let checks=0;
async function test(name,run) {await run();checks++;console.log('PASS '+name);}
(async()=>{
  await test('unsent plain draft survives source replacement without sending',async()=>{
    const e=widgetRig('SIMULATED-initial');type(e,'  SIMULATED-edited原文  ');e.widget.update({sourceToken:'SIMULATED:source-b',response:response(e,{session_id:'SIMULATED:session-b'})});
    assert.equal(field(e).value,'  SIMULATED-edited原文  ');assert.equal(e.widget.getDraft(),field(e).value);assert.equal(e.sent.length,0);copyChecks(e);e.widget.dispose();assert.equal(e.timers.size,0);
  });
  await test('failed send restores editing locally and explicit resubmit uses fresh IDs',async()=>{
    const e=widgetRig();type(e,'  SIMULATED-原文  ');submit(e);assert.equal(field(e).value,'');e.waiting.shift().reject(Error('SYNTHETIC failure'));await settled();
    assert(root(e).textContent.includes('请求结果尚未确认'));copyChecks(e);assert.equal(e.widget.getDraft(),'  SIMULATED-原文  ');
    const count=e.sent.length+e.cancelled.length;button(e,'edit').fire('click');assert.equal(e.sent.length+e.cancelled.length,count);assert.equal(field(e).value,'  SIMULATED-原文  ');assert.equal(e.document.activeElement,field(e));
    const old=e.sent[0];submit(e);const next=e.sent[1];for(const key of ['turn_id','operation_id','idempotency_key'])assert.notEqual(next[key],old[key]);assert(!('approval_digest'in next)&&!('plan_id'in next)&&!('expected_context'in next));e.waiting.shift().resolve(response(e,next));await settled();e.widget.dispose();
  });
  await test('a delayed failure cannot overwrite a newer draft even if a click is forced',async()=>{
    const e=widgetRig();type(e,'SIMULATED-old');submit(e);type(e,'SIMULATED-new draft');e.waiting.shift().reject(Error('SYNTHETIC'));await settled();
    const edit=button(e,'edit');assert(edit.disabled);edit.disabled=false;edit.fire('click');assert.equal(field(e).value,'SIMULATED-new draft');assert.equal(e.widget.getDraft(),'SIMULATED-new draft');assert.equal(e.sent.length,1);e.widget.dispose();
  });
  await test('fixed session expiry clears approvals and retains plain original text',async()=>{
    const e=widgetRig();type(e,'SIMULATED-preview original');submit(e);e.waiting.shift().resolve(preview(e,e.sent[0]));await settled();assert.equal(enabled(e,'approve').length,1);
    e.clock.now+=60001;e.tick();assert.equal(enabled(e,'approve').length,0);assert.equal(field(e).value,'SIMULATED-preview original');assert(root(e).textContent.includes('已过期'));assert(button(e,'send').disabled);assert.equal(e.sent.length,1);copyChecks(e);e.widget.dispose();
  });
  await test('turn response never extends the original bootstrap deadline',async()=>{
    const e=widgetRig();type(e,'SIMULATED-query');submit(e);e.waiting.shift().resolve(response(e,e.sent[0],{expires_at:new Date(e.clock.now+600000).toISOString()}));await settled();e.clock.now+=60001;e.tick();assert(button(e,'send').disabled);assert(root(e).textContent.includes('已过期'));e.widget.dispose();
  });
  await test('server session rejection immediately renders the editable expired state',async()=>{
    const e=widgetRig();type(e,'SIMULATED-recover');submit(e);e.waiting.shift().resolve(response(e,e.sent[0],{phase:'unavailable',code:'flow_session_unavailable'}));await settled();assert.equal(field(e).value,'SIMULATED-recover');assert(root(e).textContent.includes('已过期'));assert(button(e,'send').disabled);e.widget.dispose();
  });
  await test('old source success or error cannot paint a new session or replace draft',async()=>{
    for(const failed of [false,true]){const e=widgetRig();type(e,'SIMULATED-old');submit(e);const old=e.waiting.shift();type(e,'SIMULATED-new');e.widget.update({sourceToken:'SIMULATED:B',response:response(e,{session_id:'SIMULATED:B'})});
      if(failed)old.reject(Error('SYNTHETIC-late'));else old.resolve(preview(e,e.sent[0]));await settled();assert.equal(field(e).value,'SIMULATED-new');assert.equal(enabled(e,'approve').length,0);assert(!root(e).textContent.includes('待核对'));assert.equal(e.sent.length,1);e.widget.dispose();}
  });
  await test('Escape and disposal retain captured text and fence late completion',async()=>{
    const e=widgetRig();type(e,'SIMULATED-stopped');submit(e);const old=e.waiting.shift();root(e).fire('keydown',{key:'Escape'});await settled();old.resolve(preview(e,e.sent[0]));await settled();assert.equal(enabled(e,'approve').length,0);assert.equal(e.widget.getDraft(),'SIMULATED-stopped');
    const draft=e.widget.getDraft();e.widget.dispose();assert.equal(e.widget.getDraft(),'');assert.equal(e.host.children.length,1);assert.equal(e.host.children[0],e.original);const next=e.Widget.mount(e.host,{sourceToken:null,client:{},initialDraft:draft,recovery:'expired'});assert.equal(field(e).value,draft);next.dispose();assert.equal(e.timers.size,0);
  });
  function appRig() {
    const e=environment(),calls=[],boots=[],contexts=[],turns=[];
    const api=(route,options={})=>{const request=options.body?JSON.parse(options.body):{};calls.push({route,request});
      if(route==='/api/home-flow/bootstrap'){const d=deferred();boots.push(d);return d.promise;}
      if(route==='/api/home-flow/context'){const d=deferred();d.signal=options.signal;contexts.push(d);return d.promise;}
      if(route==='/api/home-flow/turn'){const d=deferred();turns.push(d);return d.promise;}
      if(route==='/api/home-flow/cancel')return Promise.resolve(response(e,request,{phase:'cancelled',code:'flow_cancelled'}));
      if(route==='/api/home-flow/dispose')return Promise.resolve({code:'flow_disposed'});
      throw Error('Unexpected route '+route);};
    e.context.api=api;e.context.el=(tag,text,attrs={})=>{const n=e.document.createElement(tag);if(text!=null)n.textContent=text;for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
    e.context.loadHomeConversation=()=>Promise.resolve({HomeConversation:e.Widget});
    const begin=appSource.indexOf('function retainHomeConversationDraft() {'),end=appSource.indexOf('function render() {',begin);assert(begin>=0&&end>begin);
    const functions=appSource.slice(begin,end).replace("await import('/static/home-conversation.js')","await loadHomeConversation()");
    vm.runInContext("let page='Ask Home';let homeConversationView=null,homeConversationSlot=null,homeConversationSession=null;let homeConversationVersion=0,homeConversationBoot=null,homeConversationCheck=null;let homeConversationDraft='';\n"+functions+"\nglobalThis.AppRecovery={mount:mountHomeConversation,check:checkHomeConversation,clear:clearHomeConversation,session:()=>homeConversationSession,draft:()=>homeConversationDraft,page:value=>{page=value;}};",e.context);
    return {...e,App:e.context.AppRecovery,calls,boots,contexts,turns};
  }
  const restart=e=>e.host.querySelector('[data-home-flow="bootstrap"]');
  const turnCount=e=>e.calls.filter(x=>x.route==='/api/home-flow/turn').length;
  async function bootApp(e,id='SIMULATED:app-a') {e.App.mount(e.host);await settled();e.boots.shift().resolve(response(e,{session_id:id}));await settled();assert.equal(e.App.session(),id);}
  await test('actual app bootstrap failure remains editable and restart keeps edits made while waiting',async()=>{
    const e=appRig();e.App.mount(e.host);await settled();type(e,'SIMULATED-before');e.boots.shift().reject(Error('SYNTHETIC-bootstrap'));await settled();assert(root(e).textContent.includes('待确认'));assert.equal(field(e).value,'SIMULATED-before');restart(e).fire('click');await settled();type(e,'SIMULATED-during-start');e.boots.shift().resolve(response(e,{session_id:'SIMULATED:new'}));await settled();assert.equal(field(e).value,'SIMULATED-during-start');assert.equal(e.App.session(),'SIMULATED:new');assert.equal(turnCount(e),0);e.App.clear();
  });
  await test('actual context expiry removes old approvals but keeps draft for explicit fresh bootstrap',async()=>{
    const e=appRig();await bootApp(e);type(e,'SIMULATED-preview');submit(e);const req=e.calls.find(x=>x.route==='/api/home-flow/turn').request;e.turns.shift().resolve(preview(e,req));await settled();assert.equal(enabled(e,'approve').length,1);
    const check=e.App.check();e.contexts.shift().resolve({code:'flow_session_unavailable'});await check;await settled();assert.equal(e.App.session(),null);assert.equal(field(e).value,'SIMULATED-preview');assert.equal(enabled(e,'approve').length,0);
    restart(e).fire('click');await settled();e.boots.shift().resolve(response(e,{session_id:'SIMULATED:fresh'}));await settled();assert.equal(e.App.session(),'SIMULATED:fresh');assert.equal(field(e).value,'SIMULATED-preview');assert.equal(turnCount(e),1);assert.equal(enabled(e,'approve').length,0);e.App.clear();
  });
  await test('actual old context success/error and ABA cannot invalidate a new bootstrap',async()=>{
    for(const failed of [false,true]){const e=appRig();await bootApp(e);type(e,'SIMULATED-draft');const oldCheck=e.App.check(),old=e.contexts.shift();restart(e).fire('click');await settled();e.boots.shift().resolve(response(e,{session_id:'SIMULATED:app-a'}));await settled();if(failed)old.reject(Error('SYNTHETIC'));else old.resolve({code:'flow_session_unavailable'});await oldCheck;await settled();assert.equal(e.App.session(),'SIMULATED:app-a');assert.equal(field(e).value,'SIMULATED-draft');assert(!button(e,'send').disabled);assert.equal(turnCount(e),0);e.App.clear();}
  });
  await test('restart releases an old context read and its late finally cannot release the new owner',async()=>{
    for(const boundary of ['restart','page-return'])for(const failed of [false,true]) {
      const e=appRig();await bootApp(e);type(e,'SIMULATED-draft');
      const oldCheck=e.App.check(),old=e.contexts.shift();assert(!old.signal.aborted);
      if(boundary==='restart')restart(e).fire('click');
      else {e.App.page('Home');e.App.clear();e.App.page('Ask Home');e.App.mount(e.host);}
      await settled();assert(old.signal.aborted);e.boots.shift().resolve(response(e,{session_id:'SIMULATED:restart'}));await settled();
      const freshCheck=e.App.check(),fresh=e.contexts.shift();assert(fresh);assert(!fresh.signal.aborted);
      assert.equal(e.calls.filter(x=>x.route==='/api/home-flow/context').length,2);
      if(failed)old.reject(Error('SYNTHETIC-uncooperative-old-read'));else old.resolve({code:'flow_session_unavailable'});
      await oldCheck;await settled();assert.equal(e.App.session(),'SIMULATED:restart');assert.equal(field(e).value,'SIMULATED-draft');
      await e.App.check();assert.equal(e.calls.filter(x=>x.route==='/api/home-flow/context').length,2);
      fresh.resolve({code:'flow_current'});await freshCheck;await settled();
      const next=e.App.check(),nextRead=e.contexts.shift();assert(nextRead);nextRead.resolve({code:'flow_current'});await next;
      assert.equal(e.calls.filter(x=>x.route==='/api/home-flow/context').length,3);assert.equal(turnCount(e),0);e.App.clear();
    }
  });
  await test('actual page leave/return preserves window draft and disposes only the late old bootstrap',async()=>{
    const e=appRig();e.App.mount(e.host);await settled();type(e,'SIMULATED-window draft');const old=e.boots.shift();e.App.page('Home');e.App.clear();assert.equal(e.App.draft(),'SIMULATED-window draft');e.App.page('Ask Home');e.App.mount(e.host);await settled();type(e,'SIMULATED-new edit');e.boots.shift().resolve(response(e,{session_id:'SIMULATED:new-page'}));await settled();old.resolve(response(e,{session_id:'SIMULATED:old-page'}));await settled();assert.equal(e.App.session(),'SIMULATED:new-page');assert.equal(field(e).value,'SIMULATED-new edit');assert(e.calls.some(x=>x.route==='/api/home-flow/dispose'&&x.request.session_id==='SIMULATED:old-page'));assert.equal(turnCount(e),0);e.App.clear();
  });
  assert.equal(htmlWrites,0);console.log(JSON.stringify({passed:checks,source_root:sourceRoot,actual_widget_and_app_functions:true,HTML_writes:0,real_network_persistent_storage_model_device_calls:0}));
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
