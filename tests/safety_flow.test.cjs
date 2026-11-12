// Original overview and event views with synthetic DOM/transport, never devices.
const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const all=n=>[n,...n.children.flatMap(all)],settle=()=>new Promise(setImmediate);
const event=(id,kind='sound_review',review='unreviewed')=>({id:id.repeat(32),kind,sound_kind:id==='a'?'crying':'glass_break',at:1800000000,
 simulated:false,camera:'cam_1111111111',camera_name:'合成客厅',message:'合成事件 '+id,review,announcement:{status:'not_requested'},frames:[]});
function fixture(initial=[event('b'),event('a')]){
 let focused,events=structuredClone(initial),tick,recording=null,workerStatus='running',supervision=null,holdRetry=false,heldRetry,postureReview=null;
 let holdStatus=false,heldStatus,historyFailure=false;
 const recordingReads=[];
 class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.dataset={};this.disabled=false;this.hidden=false;this._text='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(n=>n.textContent).join(' ');}
  set className(value){this.attrs.class=String(value);}
  get className(){return this.attrs.class||'';}
  setAttribute(k,v){this.attrs[k]=String(v);if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=String(v);}
  getAttribute(k){return this.attrs[k]??null;}
  removeAttribute(k){delete this.attrs[k];}
  append(...nodes){for(const n of nodes){this.children.push(n);n.parent=this;}}
  replaceChildren(...nodes){this.children=[];this._text='';this.append(...nodes);}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null;}
  contains(n){return all(this).includes(n);}
  focus(){focused=this;}
  scrollIntoView(){}
  querySelectorAll(selector){return all(this).slice(1).filter(n=>selector.split(',').some(s=>s.startsWith('.')?(n.attrs.class||'').split(' ').includes(s.slice(1)):n.tagName===s));}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 }
 const root=new Element('main'),head=new Element('head'),calls=[],navigations=[],opened=[];
 const policy={enabled:true,announce:false,visual_review:false,sound_review:true,revision:1};
 const fetch=async(path,options={})=>{
  const body=options.body&&JSON.parse(options.body);calls.push({path,body});let data;
  if(path.includes('/recordings'))return new Promise(resolve=>recordingReads.push(data=>resolve({ok:true,json:async()=>data})));
  if(path.endsWith('/retry')){if(holdRetry)return new Promise(resolve=>heldRetry=()=>resolve({ok:true,json:async()=>supervision}));return{ok:true,json:async()=>supervision};}
  if(path.endsWith('/posture')){assert.equal(body.expected_revision,policy.revision);policy.revision++;postureReview={...postureReview,camera:body.camera,state:body.camera?'waiting':'off'};data={camera:body.camera,revision:policy.revision};}
  else if(path.endsWith('/review')){const id=path.split('/').at(-2),current=events.find(e=>e.id===id);current.review=body.review;data=current;}
  else if(path.endsWith('/events')){if(historyFailure)return{ok:false,status:503};data={events};}
  else if(path==='/api/embodied/safety'){
   data={policy,worker:{status:workerStatus,cameras:[],recording},supervision,visual_review:{available:false},sound_review:{available:false},capabilities:[],posture_review:postureReview};
   if(holdStatus)return new Promise(resolve=>heldStatus=ok=>resolve({ok,status:ok?200:503,json:async()=>structuredClone(data)}));
  }
  else if(path==='/api/embodied/status')data={configured:true,owned:true,state:'live',safety_recording:recording};
  else data={};
  const snapshot=structuredClone(data);return{ok:true,json:async()=>snapshot};
 };
 const documentEvents=new EventTarget();
 const document={body:root,head,hidden:false,createElement:tag=>new Element(tag),getElementById:id=>all(head).find(n=>n.attrs.id===id),get activeElement(){return focused;},
  addEventListener:documentEvents.addEventListener.bind(documentEvents),removeEventListener:documentEvents.removeEventListener.bind(documentEvents)};
 const context=vm.createContext({document,fetch,Date,JSON,console,AbortController,Blob,URL,
  setInterval(fn){tick=fn;return 1;},clearInterval(){},setTimeout(){return 1;},clearTimeout(){}});
 const labels=fs.readFileSync('homeplane/static/safety-event-labels.js','utf8').replace(/export /g,'');
 Object.assign(context,vm.runInContext('(function(){'+labels+';return {soundNames:soundReviewNames,isOutputTest:isSafetyOutputTest,eventTitle:safetyEventTitle,safetyEventTitle,isRapidMovementReview};})()',context));
 for(const file of ['homeplane/static/arl-status.js','homeplane/static/safety.js','homeplane/static/home-status.js'])vm.runInContext(fs.readFileSync(file,'utf8').replace(/^import .*;$/gm,'').replace(/export /g,''),context);
 const go=(...args)=>navigations.push(args),api=async(path)=>{const r=await fetch(path);return r.json();};
 const by=(tag,text)=>all(root).find(n=>n.tagName===tag&&n.textContent===text),rows=()=>all(root).filter(n=>n.attrs.class==='safety-event');
 return{root,calls,navigations,opened,context,by,rows,get focused(){return focused;},get events(){return events;},
  mount(options={}){return context.mountSafety(root,{go,onOpenRecording:(out,item)=>{out.replaceChildren();if(item)opened.push(item);},...options});},
  home(devices=[]){return context.mountHomeStatus(root,{api,go,devices});},
  tick:async()=>{tick();await settle();},resolveRecording:(data,index=-1)=>recordingReads.at(index)(data),
  holdStatus(){holdStatus=true;},resolveStatus(ok){holdStatus=false;heldStatus(ok);},historyFailure(value){historyFailure=value;},
  recording(value){recording=value;},
  posture(value){postureReview=value;},
  monitoring(state,owner){workerStatus=state;supervision=owner;},holdRetry(){holdRetry=true;},resolveRetry(){heldRetry();},
 };
}

test('a home incident keeps its exact identity through navigation into the review view',async()=>{
 const f=fixture(),home=f.home();await settle();
 all(f.root).find(n=>n.attrs.class==='home-attention-item'&&n.textContent.includes('疑似哭声')).onclick();
 assert.equal(f.navigations[0][0],'Safety');assert.equal(f.navigations[0][1]?.eventId,'a'.repeat(32));home.dispose();
 const view=f.mount({initialEventId:f.navigations[0][1].eventId});await settle();
 assert.equal(f.by('h2','疑似哭声')?.textContent,'疑似哭声');assert.equal(f.rows().find(n=>n.attrs['aria-current']==='true')?.textContent.includes('疑似哭声'),true);
 assert.equal(f.calls.some(c=>c.path.endsWith('/recordings')||c.path.endsWith('/test')||c.body),false);view.dispose();
});

test('监测状态未返回或失败时，链接仍打开已存提醒并确认查看',async()=>{
 const f=fixture(),id=f.events[1].id;
 f.holdStatus();
 const view=f.mount({initialEventId:id});await settle();
 const heading=f.by('h2','疑似哭声');
 assert(heading);assert.equal(f.focused,heading);
 assert.equal(f.by('button','开启监测').disabled,true);
 await f.tick();
 assert.equal(f.calls.filter(call=>call.path==='/api/embodied/safety').length,1);
 await f.by('button','我已查看').onclick();
 assert.equal(f.events[1].review,'acknowledged');
 assert(f.by('button','已查看'));
 f.resolveStatus(false);await settle();
 assert(f.by('p','暂时无法读取监测状态。'));
 assert(f.by('h2','疑似哭声'));
 await f.tick();
 assert.equal(f.by('button','暂停监测').disabled,false);
 assert(f.by('h2','疑似哭声'));
 view.dispose();
});

test('记录读取失败保留已打开的提醒，刷新记录只读取记录接口',async()=>{
 const f=fixture(),view=f.mount({initialEventId:f.events[1].id});await settle();
 f.historyFailure(true);const previous=f.calls.length;
 await f.by('button','刷新记录').onclick();
 assert.deepEqual(f.calls.slice(previous).map(call=>call.path),['/api/embodied/safety/events']);
 assert(f.by('p','暂时无法刷新，显示上次读取的提醒。'));
 assert(f.by('h2','疑似哭声'));
 assert.equal(f.by('button','暂停监测').disabled,false);
 view.dispose();
});

test('sustained infant cry remains distinguishable on home, event list and detail',async()=>{
 const sustained={...event('a'),sound_evidence:{pattern:'sustained_infant_cry',corroborating_windows:3,audio_seconds:6}};
 const legacy={...event('c'),sound_kind:'crying'};
 const f=fixture([sustained,legacy]),home=f.home();await settle();
 const notice=all(f.root).find(n=>n.attrs.class==='home-attention-item'&&n.textContent.includes('疑似持续婴儿哭声'));
 assert(notice);notice.onclick();assert.equal(f.navigations[0][1].eventId,sustained.id);home.dispose();
 const view=f.mount({initialEventId:sustained.id});await settle();
 assert.ok(f.by('h2','疑似持续婴儿哭声'));
 assert(f.rows().some(row=>row.children[0].textContent==='疑似哭声'));
 await f.by('button','我已查看').onclick();
 assert.equal(f.events.find(e=>e.id===sustained.id).review,'acknowledged');
 view.dispose();
});

test('verified sound-search views join the same incident in capture order and retain review focus',async()=>{
 const sound={...event('a'),frames:[{ordinal:0,at:1800000000}]};
 const f=fixture([sound]),view=f.mount({initialEventId:sound.id});
 await settle();
 assert(f.by('h3','关键帧'));assert(!f.by('h3','环视画面'));
 const recording=f.by('button','查看附近录像');recording.focus();
 f.events[0].frames.push({ordinal:1,at:1800000011},{ordinal:2,at:1800000006});
 f.events[0].sound_search_views=[{step:2,ordinal:1},{step:1,ordinal:2}];
 await f.tick();
 assert(f.by('h3','异响时的画面'));assert(f.by('h3','环视画面'));
 const grids=all(f.root).filter(n=>n.attrs.class==='safety-frames');
 assert.deepEqual(grids.map(grid=>all(grid).filter(n=>n.tagName==='img').map(n=>n.attrs.src)),
  [[0],[2,1]].map(group=>group.map(i=>'/api/embodied/safety/events/'+sound.id+'/frames/'+i)));
 assert.equal(f.focused,f.by('button','查看附近录像'));
 assert(!f.calls.some(call=>call.path.endsWith('/recordings')||call.body));
 view.dispose();
});

test('声音与环视画面按各自时刻回看，迟到的另一段回看不替换当前选择',async()=>{
 const sound={...event('a'),frames:[{ordinal:0,at:1800000000,recording_anchor:true},
  {ordinal:1,at:1800000006,recording_anchor:true},{ordinal:2,at:1800000018,recording_anchor:true}],
  sound_search_views:[{step:1,ordinal:1},{step:2,ordinal:2}]};
 const f=fixture([sound]),view=f.mount({initialEventId:sound.id});await settle();
 const frame=ordinal=>all(f.root).find(n=>n.attrs['data-recording-frame']===String(ordinal));
 assert(frame(0)&&frame(1)&&frame(2));
 assert(!f.calls.some(call=>call.path.includes('/recordings')));
 frame(1).onclick();await settle();frame(2).onclick();await settle();
 const path='/api/embodied/safety/events/'+sound.id+'/recordings?ordinal=';
 assert.deepEqual(f.calls.filter(call=>call.path.includes('/recordings')).map(call=>call.path),[path+'1',path+'2']);
 assert.equal(frame(2).attrs['aria-pressed'],'true');assert.equal(frame(1).attrs['aria-pressed'],'false');
 const latest={camera_name:'示例环视录像',request:{time_range:{start:'selected'}}};
 f.resolveRecording({event_id:sound.id,frame_ordinal:2,recordings:[latest]});await settle();
 f.resolveRecording({event_id:sound.id,frame_ordinal:1,recordings:[{camera_name:'旧视角'}]},0);await settle();
 assert.deepEqual(f.opened,[latest]);
 frame(2).focus();f.events[0].review='acknowledged';await f.tick();
 assert.equal(f.focused,frame(2));assert.equal(frame(2).attrs['aria-pressed'],'true');
 view.dispose();
});

test('a replaced posture camera needs a fresh selection and keeps its field through refresh',async()=>{
 const f=fixture([]),id='cam_'+'a'.repeat(10);f.posture({camera:id,state:'source_changed',cameras:[{id,name:'照护镜头'}]});
 const view=f.mount();await settle();
 const select=all(f.root).find(n=>n.attrs.id==='safety-posture-camera');
 assert.equal(select.value,'previous');assert.equal(select.children[1].disabled,true);
 select.focus();select.value=id;select.onchange();await settle();
 assert.deepEqual(f.calls.find(c=>c.path.endsWith('/posture')).body,{camera:id,expected_revision:1});
 assert.equal(select.value,id);assert.equal(f.focused,select);
 assert(f.by('p','等待这台摄像头的清晰画面。'));
 const options=[...select.children];await f.tick();assert.deepEqual(select.children,options);
 select.value='';select.onchange();await settle();assert.equal(f.calls.filter(c=>c.path.endsWith('/posture')).at(-1).body.camera,null);
 view.dispose();
});

test('趴卧提醒分开两次观察，并按所选画面的采集时间回看',async()=>{
 const prone={...event('a','visual_review'),visual_kind:'prone_posture',basis:'same_track_two_local_windows',
  frames:Array.from({length:8},(_,ordinal)=>({ordinal,at:1800000000+ordinal,recording_anchor:ordinal===0||ordinal===4}))};
 const f=fixture([prone]),home=f.home();await settle();
 const notice=all(f.root).find(n=>n.attrs.class==='home-attention-item'&&n.textContent.includes('疑似趴卧'));
 assert(notice);notice.onclick();assert.equal(f.navigations[0][1].eventId,prone.id);home.dispose();
 const view=f.mount({initialEventId:prone.id});await settle();
 assert(f.by('h2','疑似趴卧'));assert(f.by('h3','关键帧'));
 assert(f.by('h4','首次观察')&&f.by('h4','再次观察'));
 const frames=all(f.root).filter(n=>n.attrs.class==='safety-frames');
 const firstPair=frames.slice(0,2).flatMap(group=>all(group).filter(n=>n.tagName==='img').map(n=>n.attrs.src));
 assert.deepEqual(firstPair,[0,4].map(index=>'/api/embodied/safety/events/'+prone.id+'/frames/'+index));
 assert.equal(all(frames[2]).filter(n=>n.tagName==='img').length,6);
 assert(!all(frames[2]).some(n=>n.attrs['data-recording-frame']));
 all(frames[0]).find(n=>n.attrs['data-recording-frame']==='0').onclick();await settle();
 assert.equal(f.calls.at(-1).path,'/api/embodied/safety/events/'+prone.id+'/recordings?ordinal=0');
 const replay={request:{time_range:{start:'first-window'}}};
 f.resolveRecording({event_id:prone.id,frame_ordinal:0,recordings:[replay]});await settle();
 assert.deepEqual(f.opened,[replay]);
 assert(!f.root.textContent.includes('未请求播报'));assert(!f.calls.some(c=>c.path.endsWith('/test')));
 await f.by('button','我已查看').onclick();assert.equal(f.events[0].review,'acknowledged');view.dispose();
});

test('short rapid movement opens its exact four-frame sequence while legacy reviews keep their title',async()=>{
 const movement={...event('a','visual_review'),visual_kind:'rapid_movement',basis:'same_pair_rapid_window',
  frames:Array.from({length:4},(_,ordinal)=>({ordinal,at:1800000000+ordinal*.25}))};
 const legacy={...event('b','visual_review'),visual_kind:'rapid_movement',basis:'two_disjoint_local_windows'};
 const f=fixture([movement,legacy]),home=f.home();await settle();
 const notice=all(f.root).find(n=>n.attrs.class==='home-attention-item'&&n.textContent.includes('快速搬动'));
 assert(notice);notice.onclick();assert.equal(f.navigations[0][1].eventId,movement.id);home.dispose();
 const view=f.mount({initialEventId:f.navigations[0][1].eventId});await settle();
 assert(f.by('h2','快速搬动'));assert(f.rows().some(n=>n.children[0].textContent==='画面待复核'));
 const sequence=all(f.root).find(n=>n.dataset.sequence==='rapid');
 assert.deepEqual(sequence.children.map(n=>n.children[1].textContent),['动作前 · 0.00 秒','动作前 · 0.25 秒','动作中 · 0.50 秒','动作后段 · 0.75 秒']);
 assert.deepEqual(sequence.children.map(n=>n.children[0].attrs.src),[0,1,2,3].map(i=>'/api/embodied/safety/events/'+movement.id+'/frames/'+i));
 assert(!f.by('p','未请求播报'));
 await f.by('button','标记误报').onclick();assert.equal(f.events[0].review,'false_alarm');view.dispose();
});

test('expanded home attention includes every counted item and keeps its expanded label after refresh',async()=>{
 const f=fixture([]),devices=Array.from({length:7},(_,i)=>({id:'device'+i,name:'合成设备'+i,room:'合成房间',available:false}));
 const home=f.home(devices);await settle();
 assert.equal(all(f.root).filter(n=>n.attrs.class==='home-attention-item').length,7);
 f.by('button','展开 7 项提醒').onclick();await home.refresh();
 assert.equal(f.by('button','收起提醒')?.attrs['aria-expanded'],'true');home.dispose();
});

test('direct review opens an unreviewed event while mobile drilldown remains an explicit action',async()=>{
 const f=fixture([event('b','sound_review','acknowledged'),event('a')]),view=f.mount();await settle();
 assert.ok(f.by('h2','疑似哭声'));assert.equal(all(f.root).find(n=>n.attrs.class==='safety-page').dataset.detailOpen,'false');
 f.rows().find(n=>n.textContent.includes('疑似哭声')).onclick();assert.equal(all(f.root).find(n=>n.attrs.class==='safety-page').dataset.detailOpen,'true');
 f.by('button','全部提醒').onclick();assert.equal(all(f.root).find(n=>n.attrs.class==='safety-page').dataset.detailOpen,'false');
 assert.equal(f.focused,f.rows().find(n=>n.attrs['aria-current']==='true'));assert.equal(f.calls.some(c=>c.body),false);view.dispose();
});

test('a missing linked event never silently shows a different incident',async()=>{
 const f=fixture(),selected=[],view=f.mount({initialEventId:'c'.repeat(32),onEventSelected:id=>selected.push(id)});await settle();
 assert.ok(f.by('h2','这条提醒已不在记录中'));assert.equal(f.by('h2','疑似哭声'),undefined);assert.equal(f.by('h2','疑似破碎声'),undefined);
 f.by('button','查看近期提醒').onclick();assert.ok(f.by('h2','疑似破碎声'));assert.equal(selected.at(-1),'b'.repeat(32));view.dispose();
});

test('recording failure opens monitoring without inventing an incident and recovery stays quiet',async()=>{
 const f=fixture([]);f.recording({state:'recovering',pending:1});const home=f.home();await settle();
 assert(!all(f.root).some(n=>n.attrs.class==='home-attention-item'));
 f.recording({state:'unavailable',dropped:1});await home.refresh();
 const notice=all(f.root).find(n=>n.attrs.class==='home-attention-item');assert(notice?.textContent.includes('有提醒未能保存'));
 notice.onclick();assert.deepEqual(f.navigations.at(-1),['Safety']);home.dispose();
 const view=f.mount();await settle();assert(f.root.textContent.includes('有提醒未能保存，请检查本机存储。'));
 f.recording({state:'recovering',pending:1});await f.tick();assert(f.root.textContent.includes('提醒正在保存，稍后自动重试。'));
 f.recording({state:'unavailable',unconfirmed:1});await f.tick();assert(f.root.textContent.includes('有提醒的保存结果尚未确认，请查看记录并检查本机存储。'));
 assert(!f.calls.some(c=>c.path.endsWith('/test')||c.body));view.dispose();
});

test('returning to the mobile list retires a delayed recording lookup',async()=>{
 const f=fixture(),view=f.mount({initialEventId:'a'.repeat(32)});await settle();
 f.by('button','查看附近录像').onclick();await settle();f.by('button','全部提醒').onclick();
 f.resolveRecording({recordings:[{camera_name:'合成录像',request:{}}]});await settle();
 assert.equal(f.opened.length,0);view.dispose();
});

test('reviewing the selected event updates the original action and removes that exact home notice',async()=>{
 const f=fixture(),view=f.mount({initialEventId:'a'.repeat(32)});await settle();
 await f.by('button','我已查看').onclick();assert.equal(f.events.find(e=>e.id==='a'.repeat(32)).review,'acknowledged');assert.equal(f.by('button','已查看').attrs['aria-pressed'],'true');
 assert.ok(f.by('h2','疑似哭声'));view.dispose();const home=f.home();await settle();
 const notices=all(f.root).filter(n=>n.attrs.class==='home-attention-item');assert.equal(notices.length,1);assert.match(notices[0].textContent,/疑似破碎声/);home.dispose();
});

test('review timestamps follow the captured evidence time and retain legacy record-time fallback',async()=>{
 for(const captured of [1799999820,null]){
  const row={...event('a'),captured_at:captured},f=fixture([row]),view=f.mount();await settle();
  const expected=new Date((captured??row.at)*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'});
  assert.ok(f.by('p','合成客厅 · '+expected));assert.ok(f.rows()[0].textContent.includes(expected));view.dispose();
 }
});

test('an unreachable worker uses the actual owner recovery state and one explicit retry',async()=>{
 const f=fixture([]),supervision={policy:{revision:7,enabled:true,recognition:true},failure:'service_unavailable',recovery:{automatic:true,in_progress:false,can_retry:true,retry_after_seconds:30}};
 f.monitoring('unavailable',supervision);const view=f.mount();await settle();
 assert(f.root.textContent.includes('正在恢复监测'));assert(!f.root.textContent.includes('监测已开启，等待清晰'));
 assert.equal(f.calls.some(c=>c.body),false);f.holdRetry();const retry=f.by('button','重试连接');assert(retry);
 retry.focus();const pending=retry.onclick();await settle();assert.equal(retry.disabled,true);assert.equal(f.focused.attrs.role,'status');await retry.onclick();
 assert.deepEqual(f.calls.filter(c=>c.path.endsWith('/retry')).map(c=>c.body),[{expected_revision:7}]);
 supervision.recovery.can_retry=false;f.resolveRetry();await pending;assert(!f.root.textContent.includes('监测正常'));
 f.monitoring('running',null);await f.tick();assert(f.root.textContent.includes('等待清晰'));assert.equal(retry.hidden,true);view.dispose();
});

test('a missing worker without an automatic owner cannot masquerade as waiting for a camera',async()=>{
 for(const state of ['unavailable','not_running']){
  const f=fixture([]);f.monitoring(state,null);const view=f.mount();await settle();
  assert(!f.root.textContent.includes('监测已开启，等待清晰'));assert(f.by('button','查看摄像头'));assert.equal(f.calls.some(c=>c.body),false);view.dispose();
 }
});

test('a saved global pause cannot expose a reconnect action or claim monitoring is active',async()=>{
 const f=fixture([]);f.monitoring('not_running',{policy:{revision:9,enabled:false,recognition:false},state:'paused',recovery:{automatic:false,can_retry:false}});
 const view=f.mount();await settle();assert(f.root.textContent.includes('监测已暂停'));
 const retry=f.by('button','重试连接');assert(!retry||retry.hidden);assert.equal(f.calls.some(c=>c.body),false);view.dispose();
});

test('leaving during explicit recovery ignores its late acknowledgement and does not refresh the retired page',async()=>{
 const f=fixture([]);f.monitoring('unavailable',{policy:{revision:2,enabled:true,recognition:true},failure:'service_unavailable',recovery:{automatic:true,can_retry:true}});
 const view=f.mount();await settle();f.holdRetry();const pending=f.by('button','重试连接').onclick();await settle();
 view.dispose();const count=f.calls.length;f.resolveRetry();await pending;assert.equal(f.calls.length,count);assert.equal(f.root.children.length,0);
});

function navigationFixture(){
 const source=fs.readFileSync('homeplane/static/app.js','utf8'),routes=source.slice(source.indexOf('function safetyEventId('),source.indexOf('\nfunction button(')),selected=[];
 const location={hash:'#home'},history={replaceState(_state,_unused,hash){location.hash=hash;}};
 let disposed=0;
 const context=vm.createContext({location,history,URLSearchParams,pages:['Home','Safety','Devices','Settings','Activity'],page:'Home',requestedSafetyEvent:null,requestedRecordingCamera:null,
  archiveVersion:0,archiveSetupVersion:0,lastTrace:null,stopArchivePlayer(){},render(){},refreshHaStatus(){},$:()=>null,
  experiencePage:'Home',experienceView:{selectEvent:id=>selected.push(id),dispose(){disposed++;}}});
 vm.runInContext(routes,context);return{context,location,selected,disposed:()=>disposed};
}

test('the real Console router preserves an event on reload and can select another event on the same page',()=>{
 const f=navigationFixture();f.context.go('Safety',{eventId:'a'.repeat(32)});
 assert.equal(f.location.hash,'#safety?event='+'a'.repeat(32));assert.equal(f.context.pageLocation().eventId,'a'.repeat(32));
 f.context.go('Safety',{eventId:'b'.repeat(32)});assert.equal(f.selected.at(-1),'b'.repeat(32));
 f.context.rememberSafetyEvent(null);assert.equal(f.location.hash,'#safety');assert.equal(f.context.pageLocation().eventId,null);
});

test('invalid event links cannot become a route or carry event data into another page',()=>{
 const f=navigationFixture();f.location.hash='#safety?event=%3Cimg%3E';assert.equal(f.context.pageLocation().eventId,null);
 f.context.go('Devices',{eventId:'a'.repeat(32)});assert.equal(f.location.hash,'#devices');assert.equal(f.context.pageLocation().eventId,null);
 f.context.rememberSafetyEvent('a'.repeat(32));assert.equal(f.location.hash,'#devices');
});

test('camera recording links survive reload and returning to all recordings retires the scoped view',()=>{
 const f=navigationFixture(),camera='cam_1111111111';
 f.context.go('Activity',{recordingCamera:camera});
 assert.equal(f.location.hash,'#activity?camera='+camera);
 assert.equal(f.context.pageLocation().recordingCamera,camera);
 f.context.go('Activity');assert.equal(f.location.hash,'#activity');assert.equal(f.disposed(),1);
 f.location.hash='#activity?camera=../other';assert.equal(f.context.pageLocation().recordingCamera,null);
 f.location.hash='#safety?camera='+camera;assert.equal(f.context.pageLocation().recordingCamera,null);
});
