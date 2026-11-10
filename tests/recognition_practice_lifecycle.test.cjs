/* Original lesson UI, inert illustration/audio/DOM ports, explicitly ordered replies. */
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),{webcrypto}=require('node:crypto');
const{Element}=require('./games_library_harness.cjs');
const walk=n=>[n,...n.children.flatMap(walk)],settle=()=>new Promise(setImmediate);
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const waiting=body=>({phase:'waiting',request_id:body.request_id,token:'a'.repeat(32),remaining:90,progress:0});
const ended=body=>({phase:'ended',request_id:body.request_id,remaining:0,progress:0});
function harness({send,sound=false,resume=async()=>{},cameras=[]}={}){
 const requests=[],events=new Map(),timers=new Map(),root=new Element('main');let nextTimer=0,notes=0,closed=0,navigated=null,disposed=0;
 const body=new Element('body'),invoker=new Element('button');invoker.textContent='体验OK';body.append(invoker,root);Object.defineProperty(invoker,'isConnected',{get:()=>body.contains(invoker)});
 class Audio{
  constructor(){this.state='running';this.currentTime=0;this.destination={};}
  resume(){return resume();}close(){this.state='closed';}
  createOscillator(){return{frequency:{},connect(){},start(){notes++;},stop(){}};}
  createGain(){return{gain:{setValueAtTime(){},linearRampToValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){}};}
 }
 const context=vm.createContext({document:{body,get activeElement(){return Element.active;},createElement:tag=>new Element(tag)},crypto:webcrypto,Uint8Array,Date,AbortSignal,console,
  AudioContext:Audio,cameras,MOTION_GUIDES:{'hand.ok':{name:'OK',instruction:'张开手掌，拇指与食指相触。'}},
  createMotionDiagram:()=>{const node=new Element('figure');node.setPaused=()=>{};node.dispose=()=>{disposed++;};return node;},
  setTimeout:fn=>{const id=++nextTimer;timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id),
  addEventListener:(type,fn)=>events.set(type,fn),removeEventListener:(type,fn)=>{if(events.get(type)===fn)events.delete(type);},
  fetch:async(path,options)=>{const call={path,body:JSON.parse(options.body),options};requests.push(call);const data=await (send?send(call):path.endsWith('/stop')?ended(call.body):waiting(call.body));return{ok:true,json:async()=>data};}});
 vm.runInContext(fs.readFileSync('homeplane/static/recognition-practice.js','utf8').replace(/^import.*$/gm,'').replace(/^export /gm,''),context);
 const mount=()=>{Element.active=invoker;context.root=root;const owner=vm.runInContext("mountPractice(root,{gesture:'hand.ok',cameras,go:destination=>navigate(destination),onClose:()=>didClose()})",context);walk(root).find(n=>n.tagName==='input').checked=sound;return owner;};
 context.navigate=destination=>{navigated=destination;};context.didClose=()=>{closed++;};
 const button=text=>walk(root).find(n=>n.tagName==='button'&&n.textContent===text);
 const owner=mount();
 return{root,owner,mount,requests,button,settle,events,timers,invoker,focused:()=>Element.active,notes:()=>notes,closed:()=>closed,disposed:()=>disposed,navigated:()=>navigated,
  tick(){const entries=[...timers];timers.clear();for(const[,fn]of entries)fn();}};
}

test('a lost start reply cancels with the original nonce without needing the token',async()=>{
 const h=harness({send:async call=>{if(call.path.endsWith('/start'))throw Error('SIMULATED lost reply');return ended(call.body);}});
 await h.button('开始体验').onclick();
 const[start,stop]=h.requests;assert(start.path.endsWith('/start'));assert(stop.path.endsWith('/stop'));
 assert.match(start.body.request_id,/^[a-f0-9]{64}$/);assert.equal(stop.body.request_id,start.body.request_id);assert.equal(stop.body.token,undefined);assert.equal(stop.options.keepalive,true);
 assert(h.root.textContent.includes('体验暂未开始'));assert(h.button('重新体验'));assert.equal(h.notes(),0);
 await h.button('收起').onclick();assert.equal(h.closed(),1);assert.equal(h.root.children.length,0);
});

test('closing while audio permission resumes prevents a later start and still owns cancellation',async()=>{
 const permission=deferred(),h=harness({sound:true,resume:()=>permission.promise});
 const begin=h.button('开始体验').onclick();await settle();assert.equal(h.requests.length,0);
 h.owner.dispose();await settle();assert.equal(h.requests.length,1);assert(h.requests[0].path.endsWith('/stop'));
 permission.resolve();await begin;assert(!h.requests.some(call=>call.path.endsWith('/start')));assert.equal(h.notes(),0);assert.equal(h.disposed(),1);
});

test('close waits for confirmed cancellation and a late reply never replaces the closed panel',async()=>{
 const reply=deferred(),stop=deferred(),h=harness({send:call=>call.path.endsWith('/start')?reply.promise:stop.promise});
 const begin=h.button('开始体验').onclick();await settle();const original=h.requests[0].body;
 const close=h.button('收起').onclick();await settle();assert.equal(h.closed(),0);assert(h.root.textContent.includes('正在结束体验'));
 stop.resolve(ended(original));await close;assert.equal(h.closed(),1);assert.equal(h.root.children.length,0);
 reply.resolve(waiting(original));await begin;assert.equal(h.root.children.length,0);assert.equal(h.notes(),0);
});

test('repeated blocked retries reuse one recovery action and update its destination',async()=>{
 let attempt=0;const h=harness({send:async call=>({phase:'blocked',reason:++attempt===2?'game_active':'camera_not_ready',request_id:call.body.request_id})});
 for(let i=0;i<3;i++){
  await h.button(i?'重新体验':'开始体验').onclick();
  const actions=walk(h.root).filter(n=>n.tagName==='button'&&!n.hidden&&['查看摄像头','前往家庭游戏'].includes(n.textContent));
  assert.equal(actions.length,1);actions[0].onclick();assert.equal(h.navigated(),i===1?'Games':'Cameras');
 }
 await h.button('收起').onclick();
});

test('unconfirmed cancellation retains the panel and retry ends the same session',async()=>{
 let fail=true;const h=harness({send:async call=>{if(call.path.endsWith('/stop')){if(fail)throw Error('SIMULATED offline');return ended(call.body);}return waiting(call.body);}});
 assert.equal(h.focused().tagName,'h2');await h.button('开始体验').onclick();await settle();assert.equal(h.focused(),h.button('结束体验'));await h.button('结束体验').onclick();
 assert.equal(h.closed(),0);assert(h.root.textContent.includes('尚未确认体验已结束'));assert(!h.root.textContent.includes('已恢复'));
 assert.equal(h.focused(),h.button('重试结束'));assert.equal(h.button('重新体验'),undefined);fail=false;await h.button('重试结束').onclick();assert.equal(h.closed(),1);assert.equal(h.focused(),h.invoker);
 const stops=h.requests.filter(call=>call.path.endsWith('/stop'));assert.equal(stops.length,2);assert.equal(stops[0].body.request_id,stops[1].body.request_id);
});

test('switching lessons waits for the previous cancellation before sending a new start',async()=>{
 const stop=deferred();let held=true;const h=harness({send:call=>call.path.endsWith('/stop')?(held?stop.promise:ended(call.body)):waiting(call.body)});
 await h.button('开始体验').onclick();await settle();h.owner.dispose();await settle();
 const next=h.mount(),begin=h.button('开始体验').onclick();await settle();assert.equal(h.requests.filter(call=>call.path.endsWith('/start')).length,1);
 held=false;stop.resolve(ended(h.requests[0].body));await begin;assert.equal(h.requests.filter(call=>call.path.endsWith('/start')).length,2);
 next.dispose();await settle();
});

test('success feedback is emitted once for an actual event, never for an accepted start',async()=>{
 let success=false;const h=harness({sound:true,send:call=>call.path.endsWith('/stop')?ended(call.body):success?{...waiting(call.body),phase:'success',event:{id:'SIMULATED-observed-event'}}:waiting(call.body)});
 await h.button('开始体验').onclick();await settle();assert.equal(h.notes(),0);
 success=true;h.tick();await settle();assert(h.root.textContent.includes('识别成功'));assert.equal(h.notes(),2);
 h.tick();await settle();assert.equal(h.notes(),2);assert(h.button('再试一次'));
 await h.button('结束体验').onclick();assert.equal(h.timers.size,0);
});

test('pagehide cancels a pending preparation and does not issue a start on return',async()=>{
 const permission=deferred(),h=harness({sound:true,resume:()=>permission.promise});
 const begin=h.button('开始体验').onclick();await settle();h.events.get('pagehide')();await settle();permission.resolve();await begin;
 assert(!h.requests.some(call=>call.path.endsWith('/start')));assert(h.root.textContent.includes('本次体验已结束'));
 h.owner.dispose();assert.equal(h.events.size,0);
});

test('a replacement panel can retry cancellation that failed during disposal',async()=>{
 let offline=false;const h=harness({send:call=>{if(call.path.endsWith('/stop')){if(offline)throw Error('SIMULATED offline');return ended(call.body);}return waiting(call.body);}});
 await h.button('开始体验').onclick();await settle();offline=true;h.owner.dispose();await settle();
 const next=h.mount();await h.button('开始体验').onclick();assert(h.button('重试结束'));assert.equal(h.closed(),0);
 const original=h.requests[0].body.request_id;offline=false;await h.button('重试结束').onclick();
 assert.equal(h.closed(),1);assert.equal(h.requests.at(-1).body.request_id,original);next.dispose();
});

test('returning from pagehide during a pending close leaves usable actions',async()=>{
 const stop=deferred();const h=harness({send:call=>call.path.endsWith('/stop')?stop.promise:waiting(call.body)});
 await h.button('开始体验').onclick();await settle();const close=h.button('结束体验').onclick();await settle();
 h.events.get('pagehide')();stop.resolve(ended(h.requests[0].body));await close;await settle();
 assert.equal(h.closed(),0);assert.equal(h.button('重新体验').disabled,false);assert.equal(h.button('收起').disabled,false);
 await h.button('收起').onclick();assert.equal(h.closed(),1);
});

test('recognition result stays first and the full camera name remains separately accessible',async()=>{
 const room='SIMULATED'+'UnbrokenRoomName'.repeat(5);let phase='seen';
 const h=harness({cameras:[{id:'SIMULATED-camera',room}],send:call=>call.path.endsWith('/stop')?ended(call.body):{...waiting(call.body),phase,camera:'SIMULATED-camera',event:phase==='success'?{id:'SIMULATED-event'}:null}});
 await h.button('开始体验').onclick();await settle();
 for(const[next,text]of [['seen','看到你了，跟着示意做动作'],['recognizing','识别中，继续保持'],['success','识别成功']]){
  phase=next;h.tick();await settle();
  const status=walk(h.root).find(n=>n.attrs.role==='status');assert.equal(status.textContent,text);
  const source=walk(h.root).find(n=>n.title===room);assert(source);assert.equal(source.textContent,room);assert.equal(source.hidden,false);
 }
 await h.button('结束体验').onclick();
});
