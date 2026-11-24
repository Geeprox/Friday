const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),{webcrypto}=require('node:crypto');
const web=path.join(__dirname,'../integrations/ktv_public/web');
const source=fs.readFileSync(path.join(web,'app.js'),'utf8').replace('join();document.addEventListener','document.addEventListener');
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const firstRoom='11111111-1111-4111-8111-111111111111';
const secondRoom='22222222-2222-4222-8222-222222222222';
function currentRoomReply(){
 return {room:{room:secondRoom,key:'b'.repeat(43)},
  guest:{id:'33333333-3333-4333-8333-333333333333',key:'d'.repeat(64),nickname:'新场朋友',avatar:1},
  status:{server_time_ms:Date.now(),mac_online:true,songs:[],theme:'content',queue_revision:0,
   playback:{online:false,current:'',played:[],state:'idle'},controls:{control_revision:0}}};
}
const harnessTimers=new Set();
test.afterEach(()=>{for(const timer of harnessTimers)clearTimeout(timer);harnessTimers.clear();});
function harnessTimeout(callback,delay){
 const timer=setTimeout(()=>{harnessTimers.delete(timer);callback();},delay);
 harnessTimers.add(timer);
 return timer;
}
function memoryStorage(){
 const values=new Map();
 return {getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key),clear:()=>values.clear()};
}
function harness({validRoom=false,deferFrames=false,roomId=firstRoom,invite='a'.repeat(43),localStorage,sessionStorage=memoryStorage(),locks}={}){
 const nodes=new Map(),trace=[],listeners=new Map(),frames=[];
 const events=name=>({addEventListener(type,callback){const key=name+':'+type;if(!listeners.has(key))listeners.set(key,new Set());listeners.get(key).add(callback);}});
 const emit=(name,type,event={})=>{for(const callback of [...(listeners.get(name+':'+type)||[])])callback(event);};
 class Node{constructor(id='',tagName='div'){this.id=id;this.tagName=tagName.toUpperCase();this.children=[];this.dataset={};this.style={setProperty(name,value){this[name]=value;}};this.value='';this.textContent='';this.hidden=false;this.isConnected=true;this.classList={add(){},remove(){},toggle(){},contains:value=>(this.className||'').split(/\s+/).includes(value)};this.attributes={};this.parentElement=null;}
  get firstElementChild(){return this.children[0]||null;}get childElementCount(){return this.children.length;}
  append(...nodes){for(const n of nodes){if(n.parentElement)n.parentElement.removeChild(n);this.children.push(n);n.parentElement=this;}}replaceChildren(...n){for(const old of this.children)old.parentElement=null;this.children=[];this.append(...n);}removeChild(n){const at=this.children.indexOf(n);if(at>=0)this.children.splice(at,1);n.parentElement=null;return n;}replaceWith(n){const parent=this.parentElement;if(parent){parent.insertBefore(n,this);parent.removeChild(this);}}setAttribute(k,v){this.attributes[k]=v;}getAttribute(k){return this.attributes[k];}querySelector(q){return q==='button'?(this.button??=new Node()):null;}querySelectorAll(){return [];}closest(){return null;}addEventListener(){}focus(){document.activeElement=this;}insertBefore(n,before=null){if(n.parentElement)n.parentElement.removeChild(n);const at=before?this.children.indexOf(before):-1;this.children.splice(at<0?this.children.length:at,0,n);n.parentElement=this;}}
 const element=id=>{if(!nodes.has(id))nodes.set(id,new Node(id));return nodes.get(id);};
 const document={...events('document'),hidden:false,body:new Node('body'),getElementById:element,createElement:name=>new Node('',name),querySelector:q=>q==='dialog[open]'?null:element(q),querySelectorAll:()=>[],activeElement:null};
 class Ear{constructor(clock,changed){this.clock=clock;this.changed=changed;this.stops=0;this.syncs=0;this.version=0;this.alignedGeneration=null;this.buffer=null;this.context={state:'running',decodeAudioData:async bytes=>({duration:5,marker:new Uint8Array(bytes)[0]})};}
  start(loader){trace.push('start');this.controller=new AbortController();return Promise.resolve().then(()=>loader(this.controller.signal)).then(bytes=>{this.buffer={duration:5,marker:new Uint8Array(bytes)[0]};return this.buffer;});}
  stop(){this.stops++;this.controller?.abort();this.buffer=null;this.calibrating=false;}sync(){if(this.calibrating)return false;this.syncs++;return true;}volume(){}
  setManualAdjustment(value){this.clock.adjustment=value;}}
 class Clock{constructor(){this.adjustment=0;this.generation=0;}observe(){return true;}}
 const context=vm.createContext({document,location:{hash:validRoom?`#r=${roomId}&k=${invite}`:'',pathname:'/'},localStorage,sessionStorage,navigator:{locks},history:{state:null,replaceState(value){this.state=value;},pushState(value){this.state=value;},back(){}},window:{...events('window'),scrollY:0,scrollTo(v){this.scrollY=v.top||0;}},URLSearchParams,AbortController,crypto:webcrypto,performance,setTimeout:harnessTimeout,clearTimeout,setInterval:()=>0,clearInterval(){},requestAnimationFrame:fn=>deferFrames?frames.push(fn):fn(),FridayGuideClock:Clock,FridayEarMonitor:Ear,FridayPartyAudio:require(path.join(web,'party-audio.js')),FridayAddQueue:require(path.join(web,'add-queue.js')),FridayStatusFeed:require(path.join(web,'status-feed.js')),fetch:async()=>{throw Error('unexpected fetch');}});
 vm.runInContext(source,context);
 const run=code=>vm.runInContext(code,context);
 run('var actualSetControl=setControl,actualApi=api,storedIdentity=identity;api=async()=>({});setControl=async()=>{controls.original=0;};identity={key:"fixture"};playback={online:true,current:"song-a",played:[],state:"playing",guide:{sha256:"a".repeat(64),bytes:2048,duration_ms:5000,reference:{kind:"accompaniment",sha256:"b".repeat(64),bytes:2048,duration_ms:5000}}};earEnabled=true;controls.original=0;');
 return {context,run,nodes,trace,element,emit,flushFrames(){while(frames.length)frames.shift()();}};
}

test('guide and app scripts share globals without duplicate declarations',()=>{
 new vm.Script(fs.readFileSync(path.join(web,'guide.js'),'utf8')+'\n'+source);
});

test('scan stays on automatic recovery after a long outage',async()=>{
 const h=harness({validRoom:true}),timers=[];
 h.context.setTimeout=(run,delay)=>{timers.push({run,delay});return timers.length;};
 h.run('identity=null;joinFailures=4;api=async()=>{throw Object.assign(new Error(),{code:"request_timeout"});}');
 await h.run('join()');
 assert.equal(timers.at(-1).delay,8000);
 assert.equal(h.element('my-name').textContent,'正在加入…');
 h.run('document.hidden=true');
 timers.at(-1).run();
 await flush();
 assert.equal(timers.length,1);
});

test('phone distinguishes a retained room from incomplete TV and worker readiness',()=>{
 const h=harness();
 h.context.state={mac_online:true,readiness:{state:'degraded',ready:false,gates:{link_tv:true,tv_audio:true,link_worker:false}}};
 assert.match(h.run('readinessHint(state)'),/歌曲准备服务正在连接/);
 h.context.state.readiness.gates={link_tv:false};assert.match(h.run('readinessHint(state)'),/电视暂未连接/);
 h.context.state.readiness.gates={link_tv:true,tv_audio:false};assert.equal(h.run('readinessHint(state)'),'');
 h.context.state.readiness.gates.qr=false;assert.equal(h.run('readinessHint(state)'),'');
 h.context.state.readiness.gates.nas_media=false;assert.match(h.run('readinessHint(state)'),/正在准备本场/);
 h.context.state.readiness.ready=true;assert.equal(h.run('readinessHint(state)'),'');
 assert.doesNotMatch(h.run('readinessHint({mac_online:false})'),/房间.*离线|本场.*结束/);
});
test('background readiness checks stay out of an active performance',()=>{
 const h=harness();
 h.context.state={mac_online:false,readiness:{state:'degraded',ready:false,gates:{link_tv:true,tv_playback:true,
  tv_media:false,link_worker:false,nas_media:false}},playback:{online:true,current:'song',state:'playing'}};
 assert.equal(h.run('readinessHint(state)'),'');
 h.context.state.playback.state='paused';assert.equal(h.run('readinessHint(state)'),'');
 h.context.state.readiness.gates.link_tv=false;assert.match(h.run('readinessHint(state)'),/电视暂未连接/);
});

test('an old load finally cannot clear the new loading state or stop its monitor',async()=>{
 const h=harness(),old=deferred(),fresh=deferred();h.context.old=old;h.context.fresh=fresh;
 h.run('ear.start=()=>old.promise');const first=h.run('loadGuide()');
 h.run('playback.current="song-b";ear.start=()=>fresh.promise');const second=h.run('loadGuide()');
 const stops=h.run('ear.stops');old.resolve({});await first;
 assert.equal(h.run('earLoading'),true);assert.equal(h.run('earSong'),'song-b');assert.equal(h.run('ear.stops'),stops);
 h.run('invalidateGuide({disable:true})');fresh.resolve({});await second;
});

test('search and queue remain distinct while preserving query and scroll',()=>{
 const h=harness();h.run('mode="find";selected=[{id:"queued",song_id:"123",title:"已点歌曲",state:"ready"}];songs=[{song_id:"456",title:"搜索结果"}];searchQuery="保留搜索词";$("query").value=searchQuery;window.scrollY=90;render()');
 assert.equal(h.element('results').children.length,1);assert.equal(h.element('search').hidden,false);
 h.run('setView("queue")');assert.equal(h.element('search').hidden,true);assert.equal(h.element('results').children[0].className,'queue-group pending');
 h.run('window.scrollY=35;setView("find")');assert.equal(h.element('query').value,'保留搜索词');assert.equal(h.context.window.scrollY,90);assert.equal(h.element('results').children.length,1);
});


test('music unmute restores the last nonzero room level across a page reload without crossing rooms',()=>{
 const h=harness(),values=new Map();h.context.storage={getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value)};
 assert.equal(h.run('var musicA=new RoomMusicVolume("room-a",storage);musicA.toggle(37)'),0);
 assert.equal(h.run('new RoomMusicVolume("room-a",storage).toggle(0)'),37);
 assert.equal(h.run('new RoomMusicVolume("room-b",storage).toggle(0)'),70);
 values.set('friday-ktv-music-level:room-c','101');
 assert.equal(h.run('new RoomMusicVolume("room-c",storage).toggle(0)'),70);
 assert.equal(h.run('var unavailable=new RoomMusicVolume("room-d",{getItem(){throw Error()},setItem(){throw Error()}});unavailable.remember(29);unavailable.toggle(0)'),29);
});

test('music mute cancels a pending slider request so it cannot unmute later',async()=>{
 const h=harness();h.run('var writes=[];setControl=async value=>{writes.push(value)};controls.music_gain=52;playback.online=true;changeGain("music","music_gain",43,20);toggleMusic()');
 await new Promise(resolve=>setTimeout(resolve,45));
 assert.equal(h.run('writes.length'),1);assert.equal(h.run('writes[0].music_gain'),0);
 h.run('toggleMusic()');assert.equal(h.run('writes[1].music_gain'),43);
});

test('a mute POST receipt cannot clear pending UI against the preceding applied revision',async()=>{
 const h=harness(),ack=deferred();h.context.ack=ack;
 h.run('setControl=actualSetControl;api=async()=>({revision:3});waitForTV=()=>ack.promise;updateGuide=()=>{};updatePartyAudio=()=>{};controls={...controls,music_gain:50,control_revision:2};playback.applied_revision=2;');
 const request=h.run('changeGain("music","music_gain",0,0)');await flush();h.run('updatePlayer()');
 assert.equal(h.run('controls.control_revision'),3);assert.equal(h.run('pendingControls.music_gain'),0);
 assert.equal(h.element('music-mute-button').getAttribute('aria-pressed'),'mixed');
 h.run('playback.applied_revision=3');ack.resolve();await request;
 assert.equal(h.run('pendingControls.music_gain'),undefined);assert.equal(h.element('music-mute-button').getAttribute('aria-pressed'),'true');
});


test('API preserves service status and Retry-After for the durable add worker',async()=>{
 const h=harness({validRoom:true});h.context.fetch=async()=>({ok:false,status:429,headers:{get:()=>"12"},json:async()=>({code:"rate_limited"})});
 const error=await h.run('actualApi("songs",{song_id:"1"})').catch(e=>e);
 assert.equal(error.code,'rate_limited');assert.equal(error.status,429);assert.equal(error.retryAfterMs,12000);
 h.context.fetch=async()=>({ok:false,status:503,headers:{get:()=>null},json:async()=>{throw Error('HTML gateway error');}});
 const unavailable=await h.run('actualApi("status")').catch(e=>e);assert.equal(unavailable.code,'service_unavailable');assert.equal(unavailable.status,503);
});

test('cancelling a guide control aborts its HTTP and lets a later music control leave the serialized queue',async()=>{
 const h=harness({validRoom:true}),requests=[];let rejectFirst;
 h.context.fetch=async(url,options)=>{
  const payload=JSON.parse(options.body);requests.push({payload,signal:options.signal});
  if(payload.original===0)return new Promise((resolve,reject)=>{rejectFirst=reject;options.signal.addEventListener('abort',()=>reject(Object.assign(new Error(),{name:'AbortError'})),{once:true});});
  return {ok:true,status:200,json:async()=>({revision:2})};
 };
 h.run('api=actualApi;setControl=actualSetControl;waitForTV=async()=>{};updatePlayer=()=>{};var aborter=new AbortController();controls.original=1;controls.music_gain=70;');
 const first=h.run('setControl({original:0},{signal:aborter.signal,propagate:true})').catch(error=>error.code);
 const second=h.run('setControl({music_gain:38})');
 try{
  await flush();assert.equal(requests.length,1);h.run('aborter.abort()');assert.equal(requests[0].signal.aborted,true);
  assert.equal(await first,'cancelled');await second;assert.equal(requests.length,2);assert.deepEqual(requests[1].payload,{music_gain:38});
  assert.equal(h.run('controls.original'),1);assert.equal(h.run('controls.music_gain'),38);
 }finally{rejectFirst?.(Object.assign(new Error(),{name:'AbortError'}));}
});

test('music mute leaves immediately and a deferred slider receipt cannot restore its gain',async()=>{
 const h=harness({validRoom:true}),slider=deferred(),sent=deferred();
 const requests=[];
 h.context.muteApi=(operation,payload)=>{
  requests.push({operation,payload:Object.fromEntries(Object.entries(payload))});
  if(payload.music_gain===48){sent.resolve();return slider.promise;}
  return Promise.resolve({revision:2,music_mute_revision:1});
 };
 h.run('api=muteApi;setControl=actualSetControl;waitForTV=async()=>{};updatePlayer=()=>{};controls.music_mute_revision=0');
 const earlier=h.run('setControl({music_gain:48})');
 await sent.promise;
 const queued=h.run('setControl({music_gain:49})');
 await h.run('setControl({music_gain:0})');
 assert.deepEqual(requests.map(r=>r.payload),[{music_gain:48,music_mute_revision:0},{music_gain:0}]);
 slider.resolve({revision:1,music_mute_revision:0});
 await Promise.all([earlier,queued]);
 assert.equal(h.run('controls.music_gain'),0);
 assert.equal(h.run('controls.music_mute_revision'),1);
 assert.equal(requests.length,2);
});

test('a late JSON control receipt after cancellation cannot update local controls',async()=>{
 const h=harness({validRoom:true}),body=deferred(),entered=deferred();let signal;
 h.context.fetch=async(url,options)=>{signal=options.signal;return {ok:true,status:200,json:()=>{entered.resolve();return body.promise;}};};
 h.run('api=actualApi;setControl=actualSetControl;waitForTV=async()=>{};updatePlayer=()=>{};var aborter=new AbortController();controls.original=1;');
 const pending=h.run('setControl({original:0},{signal:aborter.signal,propagate:true})').catch(error=>error.code);
 await entered.promise;h.run('aborter.abort()');assert.equal(signal.aborted,true);body.resolve({revision:8});
 assert.equal(await pending,'cancelled');assert.equal(h.run('controls.original'),1);assert.equal(h.run('controls.control_revision'),0);
});

test('aborting response JSON is cancellation rather than a connection failure',async()=>{
 const h=harness({validRoom:true}),entered=deferred();
 h.context.fetch=async(url,options)=>({ok:true,status:200,json:()=>new Promise((resolve,reject)=>{entered.resolve();options.signal.addEventListener('abort',()=>reject(Object.assign(new Error(),{name:'AbortError'})),{once:true});})});
 h.run('var aborter=new AbortController();');
 const pending=h.run('actualApi("controls",{original:0},"",{signal:aborter.signal})').catch(error=>error.code);
 await entered.promise;h.run('aborter.abort()');assert.equal(await pending,'cancelled');
});

test('successful reconnection removes only its connection notice and retains business/search feedback',async()=>{
 const h=harness({validRoom:true});h.run('earEnabled=false;identity=null;api=async()=>{throw Object.assign(new Error(),{code:"service_unavailable",status:503})};message("这首的版本暂不可用")');
 await h.run('refresh()');assert.equal(h.element('connection-status').textContent,'');
 h.run('lastStatusSuccess=roomResumeAt=performance.now()-13000');
 await h.run('refresh()');assert.match(h.element('connection-status').textContent,/正在重连/);
 h.run('api=async()=>({mac_online:true,songs:[],playback:{online:true,current:"",played:[],state:"idle",mic_muted:true},controls:{},theme:"content"})');
 await h.run('refresh()');assert.equal(h.element('connection-status').hidden,true);assert.equal(h.element('connection-status').textContent,'');
 assert.equal(h.element('message').textContent,'这首的版本暂不可用');assert.equal(h.run('online'),true);assert.equal(h.element('queue-count').textContent,0);
});

test('a transient events failure or a late aborted status read never replaces the last healthy view',async()=>{
 const h=harness({validRoom:true}),old=deferred(),next=deferred(),signals=[];
 h.context.probe=(op,data,query,opt)=>{signals.push(opt.signal);return signals.length===1?old.promise:next.promise;};
 h.run('earEnabled=false;api=probe;lastStatusSuccess=performance.now();statusFailure({code:"request_timeout"},"events")');
 const first=h.run('refresh()'),shared=h.run('refresh()');assert.equal(first,shared);assert.equal(signals.length,1);
 assert.equal(h.element('connection-status').textContent,'');
 h.run('document.hidden=true;stopRoomStatus();document.hidden=false;roomResumeAt=performance.now()');
 const second=h.run('refresh()');assert.equal(signals[0].aborted,true);
 old.resolve({mac_online:false,songs:[],playback:{online:false,current:"",played:[],state:"idle"}});
 assert.equal(await first,false);
 next.resolve({mac_online:true,songs:[],playback:{online:true,current:"",played:[],state:"idle"},controls:{},theme:'content'});
 assert.equal(await second,true);assert.equal(h.run('online'),true);
});

test('rapid song taps enqueue serially and retain visible status until actual receipts',async()=>{
 const h=harness({validRoom:true}),first=deferred(),second=deferred(),requests=[];
 h.context.fakeSend=(operation,payload)=>{if(operation==='status')return Promise.resolve({mac_online:true,songs:[],playback:{online:true,current:"",played:[],state:"idle",mic_muted:true},controls:{},theme:"content"});requests.push(payload);return requests.length===1?first.promise:second.promise;};
 h.run('earEnabled=false;identity={id:"22222222-2222-4222-8222-222222222222",key:"fixture"};api=fakeSend;online=true;initAddQueue();add({song_id:"1001",title:"第一首",source:"song"});add({song_id:"1002",title:"第二首",source:"song"});add({song_id:"1001",title:"第一首",source:"song"})');
 await flush();assert.equal(requests.length,1);assert.equal(h.run('addSnapshot.items.length'),2);assert.equal(h.run('selected.length'),0);assert.equal(h.element('pending-adds').hidden,false);
 first.resolve({id:'33333333-3333-4333-8333-333333333333',state:'queued'});await flush();await flush();assert.equal(requests.length,2);assert.equal(h.run('addSnapshot.items.length'),1);
 second.resolve({id:'44444444-4444-4444-8444-444444444444',state:'queued'});await flush();await flush();assert.equal(h.run('addSnapshot.items.length'),0);assert.equal(h.element('pending-adds').hidden,true);
 h.run('addQueue.close()');
});


test('home has one search entry and independent secondary pages with source filters only in search',()=>{
 const h=harness();h.run('render()');
 assert.equal(h.element('search-entry').hidden,false);assert.equal(h.element('source-picker').hidden,true);assert.equal(h.element('search').hidden,true);assert.equal(h.element('discover').hidden,false);
 h.element('search-entry').onclick();assert.equal(h.run('mode'),'find');assert.equal(h.element('page-title').textContent,'搜索');assert.equal(h.element('source-picker').hidden,false);assert.equal(h.element('discover').hidden,true);
 h.element('queue-tab').onclick();assert.equal(h.run('mode'),'queue');assert.equal(h.element('page-title').textContent,'已点歌曲');assert.equal(h.element('source-picker').hidden,true);
 h.run('setView("home")');assert.equal(h.element('search-entry').hidden,false);assert.equal(h.element('page-heading').hidden,true);
});

test('withdrawing an owned song ignores stale deduplication and allows an immediate new request',async()=>{
 const h=harness({validRoom:true,localStorage:memoryStorage()}),original={
  id:'33333333-3333-4333-8333-333333333333',song_id:'11',title:'待唱歌曲',source:'song',state:'queued',
  guest_id:'44444444-4444-4444-8444-444444444444'
 };
 const requests=[];
 h.context.original=original;
 h.context.send=async (operation,payload)=>{
  requests.push({operation,payload});
  if(payload.request_id)return {id:'55555555-5555-4555-8555-555555555555',state:'queued'};
  return {withdrawn:true,id:original.id,queue_removed:true,queue_revision:12};
 };
 h.run('identity={id:original.guest_id,key:"guest",nickname:"本人",avatar:1};selected=[original];playback.current="";queueRevision=11;api=send;refresh=()=>Promise.resolve(false);activateSong(original)');
 await flush();await flush();
 assert.equal(requests.length,1);
 assert.equal(requests[0].operation,'songs');
 assert.equal(requests[0].payload.withdraw_id,original.id);
 assert.equal(h.element('toast').textContent,'已撤回点歌');
 assert.equal(h.run('selected.length'),0);
 h.run('applyStatus({songs:[original],queue_revision:11})');
 assert.equal(h.run('selected.length'),0);
 assert.equal(h.run('addQueue.has(original)'),false);
 h.run('activateSong(original)');await flush();
 assert.equal(requests.length,2);
 assert.equal(requests[1].payload.song_id,original.song_id);
 assert.ok(requests[1].payload.request_id);
 h.run('addQueue.close()');
});

test('only a claim-aware TV enables withdrawal of ready songs and a new refusal explains the change',async()=>{
 const h=harness({validRoom:true}),dialog=h.element('song-dialog'),withdraw=h.element('withdraw-song'),requests=[];
 dialog.showModal=()=>{dialog.open=true;};
 dialog.close=()=>{dialog.open=false;};
 dialog.querySelector=()=>h.element('song-action-title');
 const guest='44444444-4444-4444-8444-444444444444';
 const ready={id:'33333333-3333-4333-8333-333333333333',song_id:'11',title:'待唱歌曲',
  source:'song',state:'ready',lyrics_pending:true,guest_id:guest};
 const preparing={...ready,id:'55555555-5555-4555-8555-555555555555',song_id:'22',state:'separating'};
 const other={...ready,id:'66666666-6666-4666-8666-666666666666',song_id:'33',state:'queued',guest_id:'other'};
 h.context.roomStatus={songs:[ready,preparing,other],queue_revision:3,
  playback:{online:true,current:'',played:[],state:'idle'},controls:{}};
 h.context.guest=guest;
 h.context.send=async(operation,payload)=>{
  requests.push({operation,payload});
  throw Object.assign(new Error(),{status:409,code:'song_start_unavailable'});
 };
 h.run('earEnabled=false;identity={id:guest,key:"fixture"};api=send;refresh=()=>Promise.resolve(false);applyStatus(roomStatus);songActions(selected[0])');
 assert.equal(withdraw.hidden,true);
 assert.equal(h.run('canWithdraw(selected[1])'),true);
 assert.equal(h.run('canWithdraw(selected[2])'),false);
 h.run('activateSong(selected[0])');withdraw.onclick();await flush();
 assert.equal(requests.length,0);
 h.run('applyStatus({...roomStatus,queue_withdraw_ready:"true"})');
 assert.equal(withdraw.hidden,true);
 h.run('applyStatus({...roomStatus,queue_withdraw_ready:true})');
 assert.equal(withdraw.hidden,false);
 h.run('applyStatus({...roomStatus,queue_revision:2,queue_withdraw_ready:false})');
 assert.equal(withdraw.hidden,false);
 withdraw.onclick();await flush();
 assert.equal(requests.length,1);
 assert.equal(requests[0].payload.withdraw_id,ready.id);
 assert.equal(h.element('song-error').textContent,'电视暂不支持撤回已就绪的歌曲。');
 h.run('applyStatus(roomStatus)');
 assert.equal(withdraw.hidden,true);
 h.run('addQueue.close()');
});


test('search entry keeps input focus after animation frames and returning home focuses its entry',()=>{
 const h=harness({deferFrames:true});h.run('render()');h.element('search-entry').onclick();
 assert.equal(h.context.document.activeElement.id,'query');h.flushFrames();assert.equal(h.context.document.activeElement.id,'query');
 h.run('setView("queue");setView("home")');h.flushFrames();assert.equal(h.context.document.activeElement.id,'queue-tab');
 h.run('setView("find");setView("home")');assert.equal(h.context.document.activeElement.id,'search-entry');
});

test('clearing a search invalidates its in-flight result rather than repopulating old songs',async()=>{
 const h=harness(),response=deferred();h.context.response=response;
 h.run('mode="find";api=()=>response.promise;$("query").value="旧查询"');const first=h.run('findSongs()');
 h.run('$("query").value=""');await h.run('findSongs()');assert.equal(h.run('searchBusy'),false);
 response.resolve({songs:[{song_id:'123',title:'过时结果'}],has_more:true,next_offset:30});await first;
 assert.equal(h.run('songs.length'),0);assert.equal(h.run('searchQuery'),'');assert.equal(h.run('searchMore'),false);
});

test('typing a different query removes visible results before debounce and ignores obsolete pages',async()=>{
 const h=harness(),oldPage=deferred(),second=deferred(),latest=deferred(),started=deferred();
 h.context.searchApi=async(operation,body,query)=>{
  const params=new URLSearchParams(query);
  if(params.get("offset")==="30")return oldPage.promise;
  if(params.get("q")==="第二个"){started.resolve();return second.promise;}
  if(params.get("q")==="第三个")return latest.promise;
  return {songs:[{song_id:"1",title:"旧版结果",cover:"https://example.test/cover.jpg"}],has_more:true,next_offset:30};
 };
 h.run('mode="find";api=searchApi;$("query").value="第一个"');await h.run('findSongs()');
 assert.equal(h.element('results').children.length,1);
 const loadingPage=h.run('appendNextPage()');
 h.run('$("query").value="第二个";$("query").oninput({isComposing:false})');
 assert.equal(h.element('results').children.length,0);
 assert.equal(h.element('search-page-end').hidden,true);
 assert.equal(h.element('empty').hidden,true);
 await started.promise;
 h.run('$("query").value="第三个";$("query").oninput({isComposing:false})');
 const loadingLatest=h.run('findSongs()');
 oldPage.resolve({songs:[{song_id:"2",title:"过时第二页"}],has_more:true,next_offset:60});
 second.resolve({songs:[{song_id:"3",title:"过时结果"}],has_more:false});
 await loadingPage;await flush();
 assert.equal(h.element('results').children.length,0);
 latest.resolve({songs:[{song_id:"4",title:"当前结果"}],has_more:false});
 await loadingLatest;
 assert.equal(h.run('songs[0].title'),'当前结果');
 assert.equal(h.element('results').children.length,1);
});

test('secondary search paginates the selected source and preserves results through queue navigation',async()=>{
 const h=harness(),requests=[];h.context.searchApi=async(operation,body,query)=>{
  requests.push([operation,query]);return requests.length===1?{songs:[{source:'mv',song_id:'1',title:'首个MV'}],has_more:true,next_offset:30}:{songs:[{source:'mv',song_id:'1',title:'首个MV'},{source:'mv',song_id:'2',title:'第二个MV'}],has_more:false,next_offset:60};
 };
 h.run('mode="find";source="mv";api=searchApi;$("query").value="歌手"');await h.run('findSongs()');await h.run('findSongs(true)');
 assert.match(requests[0][1],/source=mv&offset=0/);assert.match(requests[1][1],/source=mv&offset=30/);assert.equal(h.run('songs.length'),2);
 h.run('setView("queue");setView("find")');assert.equal(h.run('songs.length'),2);assert.equal(h.element('query').value,'歌手');assert.equal(h.element('source-picker').hidden,false);
});

test('search renders the first page while warming the next and reuses that response when scrolling',async()=>{
 const h=harness(),next=deferred(),requests=[];
 h.context.searchApi=async(operation,body,query,options)=>{
  requests.push({operation,query,priority:options.priority});
  if(operation==='artwork')return {covers:{}};
  if(query.includes('offset=30'))return next.promise;
  return {songs:[{song_id:'1',title:'首屏',artwork:'https://p1.music.126.net/one.jpg'}],next_offset:30,has_more:true};
 };
 h.run('mode="find";api=searchApi;$("query").value="测试歌手"');
 await h.run('findSongs()');
 assert.equal(h.run('songs[0].title'),'首屏');
 assert.equal(requests.filter(r=>r.operation==='search').length,2);
 assert.equal(requests.filter(r=>r.operation==='search')[1].priority,'low');
 const scrolling=h.run('appendNextPage()');assert.equal(h.element('page-progress').hidden,false);
 next.resolve({songs:[{song_id:'1',title:'重复'},{song_id:'2',title:'第二页'}],next_offset:60,has_more:false});
 await scrolling;
 assert.deepEqual(Array.from(h.run('songs.map(s=>s.title)')),['首屏','第二页']);
 assert.equal(requests.filter(r=>r.operation==='search').length,2);
 await h.run('findSongs()');
 assert.equal(requests.filter(r=>r.operation==='search').length,2);
});

test('queue reordering after secondary navigation keeps the current and sung songs outside pending moves',()=>{
 const h=harness();h.run('selected=[{id:"current",state:"ready"},{id:"a",state:"ready"},{id:"missing",state:"ready",lyric_state:-1},{id:"b",state:"ready"},{id:"sung",state:"ready"},{id:"prep",state:"acquiring"}];playback.current="current";playback.played=["sung"];var ordered=null;saveOrder=ids=>{ordered=ids;};setView("queue");moveSong("prep","top")');
 assert.deepEqual(Array.from(h.run('ordered')),['current','prep','missing','a','sung','b']);
 h.run('ordered=null;moveSong("sung","top");moveSong("current","top");moveSong("missing","top")');assert.equal(h.run('ordered'),null);
});

test('late artwork metadata does not render over a queue drag',async()=>{
 const h=harness();
 h.run(`mode="queue";selected=[{id:"existing",song_id:"101",title:"测试"}];drag={};
  artworkPending.add("101");api=async()=>({covers:{},tracks:{"101":{album:"合成专辑"}}});
  var renderCalls=0;render=()=>renderCalls++;`);
 await h.run('enrichArtwork()');
 assert.equal(h.run('renderCalls'),0);
 assert.equal(h.run('selected[0].album'),'合成专辑');
 assert.equal(h.run('drag=null;renderQueueStatus()'),true);
 assert.equal(h.run('renderCalls'),1);
});

test('a failed reorder preserves an independently acknowledged withdrawal',async()=>{
 const h=harness(),pending=deferred();
 h.context.pendingReorder=pending;
 h.run(`mode="queue";queueRevision=5;selected=[{id:"a"},{id:"b"},{id:"c"}];
  api=()=>pendingReorder.promise;render=()=>{};failure=()=>{};refresh=async()=>true;`);
 const saving=h.run('saveOrder(["c","a","b"])');
 h.run('selected=selected.filter(song=>song.id!=="a");queueRevision=6');
 assert.equal(h.run('JSON.stringify(mergeQueueOrder(["a","c","b"]))'),'["c","b"]');
 pending.reject(Object.assign(new Error(),{code:"queue_changed"}));
 await saving;
 assert.equal(h.run('JSON.stringify(selected.map(song=>song.id))'),'["b","c"]');
 assert.equal(h.run('queueRevision'),6);
});

test('track choice updates immediately and skips a replaced unsent choice without dropping its final receipt',async()=>{
 const h=harness(),first=deferred(),receipt=deferred();
 h.context.writes=[];h.context.first=first;h.context.receipt=receipt;
 h.run('earEnabled=false;setControl=actualSetControl;controls.original=0;playback.applied_revision=0;api=async (_,value)=>{writes.push(value.original);return writes.length===1?first.promise:{revision:2};};waitForTV=()=>receipt.promise;');
 const original=h.run('chooseSoundMode(1)');
 assert.equal(h.element('mode-original').getAttribute('aria-pressed'),'true');
 assert.equal(h.element('mode-original').attributes['aria-busy'],undefined);
 await flush();
 const backing=h.run('chooseSoundMode(0)');const replaced=h.run('chooseSoundMode(1)');const final=h.run('chooseSoundMode(0)');
 assert.equal(h.element('mode-instrumental').getAttribute('aria-pressed'),'true');
 first.resolve({revision:1});await flush();
 assert.deepEqual(h.context.writes,[1,0]);
 assert.equal(h.run('modeIntent.value'),0);
 h.run('playback.applied_revision=2');receipt.resolve();
 await Promise.all([original,backing,replaced,final]);
 assert.equal(h.run('modeIntent'),null);
 assert.equal(h.element('mode-instrumental').getAttribute('aria-pressed'),'true');
});

test('automatic room recovery cancels queued old controls and lets new controls leave immediately',async()=>{
 const h=harness({validRoom:true}),held=deferred();h.context.held=held;h.context.roomReply=currentRoomReply();
 h.run('earEnabled=false;setControl=actualSetControl;waitForTV=async()=>{};startStatusFeed=loadDiscover=initAddQueue=refresh=()=>{};controlWrites=held.promise;var writes=[];api=async(operation,value)=>{if(operation==="current")return roomReply;writes.push({room,value});return {revision:1};};');
 const volume=h.run('setControl({music_gain:13})');
 const mode=h.run('setControl({original:1})');
 await h.run('enterCurrentRoom(true)');
 assert.equal(h.run('modeIntent'),null);
 assert.equal(h.element('music-value').getAttribute('aria-busy'),'false');
 await h.run('setControl({music_gain:61})');
 assert.equal(h.run('writes.length'),1);
 held.resolve();await Promise.all([volume,mode]);
 assert.equal(h.run('writes.length'),1);
 assert.equal(h.run('writes[0].room'),'22222222-2222-4222-8222-222222222222');
 assert.equal(h.run('writes[0].value.music_gain'),61);
 assert.equal(h.run('controls.music_gain'),61);
});

test('automatic room recovery aborts an old TV confirmation wait',async()=>{
 const h=harness({validRoom:true});h.context.roomReply=currentRoomReply();
 h.run('earEnabled=false;setControl=actualSetControl;startStatusFeed=loadDiscover=initAddQueue=refresh=()=>{};lastStatusSuccess=performance.now();api=async(operation)=>operation==="current"?roomReply:{revision:3};');
 const waiting=h.run('setControl({original:1},{propagate:true})').catch(error=>error.code);
 await flush();assert.equal(h.run('receiptListeners.size'),1);
 await h.run('enterCurrentRoom(true)');
 assert.equal(await waiting,'cancelled');
 assert.equal(h.run('receiptListeners.size'),0);
 assert.equal(h.run('modeIntent'),null);
 assert.equal(h.element('sound-error').textContent,'');
});

test('automatic room recovery cancels the old transport command without disabling the new room',async()=>{
 const h=harness({validRoom:true});h.context.roomReply=currentRoomReply();
 h.run('earEnabled=false;startStatusFeed=loadDiscover=initAddQueue=refresh=()=>{};lastStatusSuccess=performance.now();api=async(operation)=>operation==="current"?roomReply:{seq:3};');
 const old=h.run('sendCommand("pause")');
 await flush();assert.equal(h.run('commandBusy'),true);
 assert.equal(h.run('receiptListeners.size'),1);
 await h.run('enterCurrentRoom(true)');await old;
 assert.equal(h.run('commandBusy'),false);
 assert.equal(h.run('receiptListeners.size'),0);
 assert.equal(h.element('play-toggle').getAttribute('aria-busy'),'false');
});

test('pending thumbnails back off until actual artwork arrives and respect Retry-After',async()=>{
 const h=harness(),timers=[];let clock=1000;
 h.context.performance={now:()=>clock};
 h.context.setTimeout=(run,delay)=>{timers.push({run,delay});return timers.length;};
 h.run('earEnabled=false;mode="find";artworkPending.add("1");api=async()=>({covers:{},pending:["1"]});');
 await h.run('enrichArtwork()');
 const retry=async()=>{const timer=timers.at(-1);clock+=timer.delay;timer.run();await flush();};
 for(let i=0;i<3;i++)await retry();
 assert.deepEqual(timers.map(timer=>timer.delay),[650,1300,2600,5200]);
 h.run('api=async()=>{throw Object.assign(new Error(),{code:"rate_limited",retryAfterMs:12000});}');
 await retry();assert.equal(timers.at(-1).delay,12000);
 h.run('api=async()=>({covers:{"1":"data:image/jpeg;base64,AAAA"},pending:[]});');
 await retry();
 assert.equal(h.run('artworkPending.size'),0);
 assert.equal(h.run('artworkDelay'),650);
 assert.equal(h.run('searchArtwork.get("1")'),'data:image/jpeg;base64,AAAA');
});


test('browser back restores the search secondary page without adding another history entry',()=>{
 const h=harness();let pushes=0;h.context.history.pushState=function(value){this.state=value;pushes++;};
 h.run('setView("find");source="cloud";$("query").value="保留云盘搜索";window.scrollY=120;setView("queue")');
 assert.equal(pushes,2);h.emit('window','popstate',{state:{ktvView:'find'}});
 assert.equal(h.run('mode'),'find');assert.equal(pushes,2);assert.equal(h.context.window.scrollY,120);assert.equal(h.element('query').value,'保留云盘搜索');assert.equal(h.element('source-picker').hidden,false);
});

test('the mobile sound sheet exposes track and music controls without vocal controls',()=>{
 const html=fs.readFileSync(path.join(web,'index.html'),'utf8');
 const sheet=html.match(/<dialog id="sound-dialog"[\s\S]*?<\/dialog>/)[0];
 assert.doesNotMatch(html,/麦克风|人声音效|id="mute-button"|id="mic-|id="vocal-/);
 assert.match(sheet,/id="mode-instrumental"[\s\S]*id="music-mute-button"[\s\S]*id="guide-toggle"/);
});

test('a scanned invite joins without a gesture or writable browser storage',async()=>{
 const h=harness({validRoom:true}),writes=[];
 h.context.joinApi=async(operation,value)=>{writes.push([operation,value]);return {key:'opaque',nickname:'自动加入',avatar:1};};
 h.run('identity=null;api=joinApi;sessionStorage.setItem=()=>{throw Error("private storage")};refresh=()=>{};startStatusFeed=()=>{};loadDiscover=()=>{};initAddQueue=()=>{}');
 await h.run('join()');
 assert.equal(writes.length,1);assert.equal(writes[0][0],'join');assert.match(writes[0][1].guest_key,/^[a-f0-9]{64}$/);
 assert.equal(h.element('my-name').textContent,'自动加入');
 assert.equal(h.element('search-entry').hidden,false);
});

function joinServer(){
 const requests=[],guests=new Map();
 return {requests,guests,reply:async(room,operation,payload,options)=>{
  assert.equal(operation,'join');
  requests.push({room,payload:Object.fromEntries(Object.entries(payload)),existing:options.guestKey});
  const known=guests.get(room+':'+options.guestKey)||guests.get(room+':'+payload.guest_key);
  if(known)return known;
  const value={id:'guest-'+guests.size,key:payload.guest_key,...(payload.profile||{nickname:'月亮贝壳',avatar:4})};
  guests.set(room+':'+value.key,value);
  return value;
 }};
}
function scannedWebView(server,options={}){
 const h=harness({validRoom:true,...options});
 h.context.joinApi=(operation,payload,_query,options)=>server.reply(h.run('room'),operation,payload,options);
 h.run('identity=storedIdentity;api=joinApi;refresh=()=>{};startStatusFeed=()=>{};loadDiscover=()=>{};initAddQueue=()=>{}');
 return h;
}

test('fresh and concurrent scanned pages recover one guest and keep chosen profile across rooms',async()=>{
 const storage=memoryStorage(),server=joinServer();let tail=Promise.resolve();
 const locks={request(_key,callback){const result=tail.then(callback);tail=result;return result;}};
 const settings={localStorage:storage,locks};
 const first=scannedWebView(server,settings),parallel=scannedWebView(server,settings);
 await Promise.all([first.run('join()'),parallel.run('join()')]);
 const seed=storage.getItem('friday-ktv-guest-seed-v1'),guest=first.run('identity.key');
 assert.match(seed,/^[a-f0-9]{64}$/);assert.notEqual(seed,guest);
 assert.equal(server.requests.length,2);assert.equal(server.guests.size,1);
 assert.equal(parallel.run('identity.id'),first.run('identity.id'));
 first.run('identity={...identity,nickname:"偏爱的名字",avatar:9};saveIdentity({prefer:true})');
 server.guests.set(firstRoom+':'+guest,{id:first.run('identity.id'),key:guest,nickname:'偏爱的名字',avatar:9});
 const rescanned=scannedWebView(server,{...settings,invite:'b'.repeat(43)});
 await rescanned.run('join()');
 assert.equal(rescanned.run('identity.key'),guest);
 const other=scannedWebView(server,{...settings,roomId:'22222222-2222-4222-8222-222222222222'});
 await other.run('join()');
 assert.notEqual(other.run('identity.key'),guest);
 assert.equal(other.element('my-name').textContent,'偏爱的名字');
 assert.equal(other.run('identity.avatar'),9);
 assert.equal(server.requests.length,4);
 assert.deepEqual(Object.keys(server.requests.at(-1).payload).sort(),['device_key','guest_key','profile','snapshot']);
 assert.ok(server.requests.every(request=>request.payload.device_key===seed));
 storage.clear();
 const cleared=scannedWebView(server,settings);await cleared.run('join()');
 assert.notEqual(cleared.run('identity.key'),guest);
});

test('an older guest credential is recovered through the one join and retained for a new WebView',async()=>{
 const storage=memoryStorage(),session=memoryStorage(),server=joinServer(),key='g'.repeat(43);
 const identity={id:'older-guest',key,nickname:'原昵称',avatar:7};
 server.guests.set(firstRoom+':'+key,identity);
 session.setItem('friday-ktv-guest:'+firstRoom,JSON.stringify(identity));
 const old=scannedWebView(server,{localStorage:storage,sessionStorage:session});
 await old.run('join()');
 const fresh=scannedWebView(server,{localStorage:storage});
 await fresh.run('join()');
 assert.equal(server.requests.length,2);assert.ok(server.requests.every(r=>r.existing===key));
 assert.equal(fresh.run('identity.id'),identity.id);
 assert.equal(fresh.element('my-name').textContent,'原昵称');
});

test('blocked persistent storage uses the session and a rejected join sends no fallback POST',async()=>{
 const server=joinServer(),session=memoryStorage(),blocked={getItem(){throw Error('storage denied');},setItem(){throw Error('storage denied');}};
 const first=scannedWebView(server,{localStorage:blocked,sessionStorage:session});await first.run('join()');
 first.run('identity={...identity,nickname:"潮汐轻唱",avatar:8};saveIdentity({prefer:true})');
 const reloaded=scannedWebView(server,{localStorage:blocked,sessionStorage:session});await reloaded.run('join()');
 assert.equal(reloaded.run('identity.key'),first.run('identity.key'));
 assert.equal(server.requests.at(-1).payload.profile.nickname,'潮汐轻唱');
 const noStorage=scannedWebView(server,{localStorage:blocked,sessionStorage:blocked});await noStorage.run('join()');
 assert.notEqual(noStorage.run('identity.key'),first.run('identity.key'));
 let calls=0;noStorage.context.joinApi=async()=>{calls++;throw Object.assign(Error(),{code:'invalid_request'});};
 noStorage.run('api=joinApi');
 await noStorage.run('join()');
 assert.equal(calls,1);
});

test('the player reports playback even when the TV reports vocal protection',()=>{
 const h=harness();h.run('earEnabled=false;selected=[{id:playback.current,title:"当前歌曲",nickname:"点歌的朋友"}];playback.safety_latched=true;playback.audio_capabilities={sfx:true,voice_protection:{state:"failed"}};updatePlayer()');
 assert.equal(h.element('player-status').textContent,'正在唱');
});

test('failed songs no longer count as upcoming and warmup can cut into the actual queue',()=>{
 const h=harness();h.run('earEnabled=false;playback.current="";mode="queue";selected=[{id:"a",song_id:"1",title:"失败",state:"failed"},{id:"b",song_id:"2",title:"下首",state:"ready"},{id:"c",song_id:"3",title:"待制备",state:"acquiring"}];render();updatePlayer()');
 assert.equal(h.element('queue-count').textContent,2);
 assert.equal(h.element('queue-total').textContent,'3 首');
 assert.equal(h.element('queue-total').hidden,false);
 assert.equal(h.element('results').children[0].className,'queue-group pending');
 assert.equal(h.element('results').children[0].children[0].tagName,'H2');
 assert.equal(h.element('results').children[1].className,'queue-group preparing');
 assert.equal(h.element('results').children[2].className,'queue-group failed');
 assert.equal(h.element('now-title').textContent,'接下来：下首');
 assert.equal(h.element('skip-button').disabled,false);
 h.run('playback.state="idle";updatePlayer()');
 assert.equal(h.element('player-status').textContent,'待播放');
});

test('a cached page shell can use the current queue and song actions',()=>{
 const h=harness(),page=h.element('page-heading'),legacy=h.element('legacy-heading'),total=h.element('count');
 legacy.className='list-heading';legacy.append(total);legacy.remove=()=>h.element('main').removeChild(legacy);
 h.element('main').append(legacy);page.append(h.element('view-queue'));
 h.context.document.getElementById=id=>id==='queue-total'?(total.id===id?total:null):id==='song-availability'?null:h.element(id);
 h.run('mode="queue";selected=[{id:"b",song_id:"2",title:"下首",state:"ready"}];render();actionSong=selected[0];refreshSongActions(true)');
 assert.equal(page.firstElementChild,total);
 assert.equal(total.id,'queue-total');
 assert.equal(total.textContent,'1 首');
 assert.equal(legacy.parentElement,null);
 assert.equal(h.element('sing-now').disabled,false);
});

test('terminal lyric failures retain the order without looking busy or enabling an unavailable next song',()=>{
 const h=harness();h.run('earEnabled=false;playback={online:true,current:"",played:[],state:"paused"};mode="queue";selected=[{id:"a",song_id:"1",title:"合成云盘",state:"ready",lyric_state:-1,lyrics_pending:false}];render();updatePlayer()');
 assert.equal(h.element('queue-count').textContent,1);
 assert.equal(h.element('results').children[0].className,'queue-group failed');
 assert.equal(h.element('now-title').textContent,'等待可唱歌曲');
 assert.equal(h.element('skip-button').disabled,true);
 assert.equal(h.element('play-toggle').disabled,true);
 h.run('playback.state="playing";updatePlayer()');
 assert.equal(h.element('play-toggle').disabled,false,'the listener can pause an existing warmup');
 h.run('playback.state="paused";selected.push({id:"b",song_id:"2",title:"下首",state:"ready",lyric_state:1});updatePlayer()');
 assert.equal(h.element('now-title').textContent,'接下来：下首');
 assert.equal(h.element('play-toggle').disabled,false);
 assert.equal(h.element('skip-button').disabled,false);
});

test('frequent songs count a personally requested playback once and stay re-addable',()=>{
 const h=harness();h.run('identity.id="mine";playback.current="one";var tracks=[{id:"one",guest_id:"mine",song_id:"101",source:"song",title:"常唱",state:"ready"}];rememberFrequent(tracks);rememberFrequent(tracks);playback.current="someone-else";rememberFrequent([{...tracks[0],id:"someone-else",guest_id:"another"}]);playback.current="cloud";rememberFrequent([{...tracks[0],id:"cloud",source:"cloud"}]);playback.current="two";rememberFrequent([{...tracks[0],id:"two"}]);setView("frequent")');
 assert.equal(h.run('frequentSongs.length'),1);assert.equal(h.run('frequentSongs[0].plays'),2);
 assert.equal(h.element('page-title').textContent,'常唱歌曲');
 assert.equal(h.element('results').children[0].children.at(-1).dataset.song,'song:101');
});

test('a download tick updates the existing progress bar and preserves the action button, focus and history',()=>{
 const h=harness();
 h.run(`earEnabled=false;mode="queue";playback.current="";playback.played=["old"];
  selected=[{id:"b",song_id:"2",title:"已就绪",state:"ready"},
    {id:"a",song_id:"1",title:"下载",state:"acquiring",progress:{
      stage:"acquiring",source_phase:"source_downloading",source_bytes:10,source_total:20}},
    {id:"old",song_id:"3",title:"已唱",state:"ready"}];render()`);
 const root=h.element('results'),[pending,preparing,history]=root.children;
 const [heading,row]=preparing.children, count=heading.firstElementChild;
 const [label,steps,bar,amount]=row.children[1].children.find(node=>node.className==='preparation').children;
 const action=row.children.at(-1).children[0], next=pending.children[1];
 history.open=true;
 action.focus();
 assert.equal(bar.value,10);
 h.run('selected[1]={...selected[1],progress:{...selected[1].progress,source_bytes:15}};render()');
 assert.deepEqual(root.children,[pending,preparing,history]);
 assert.equal(history.open,true);
 assert.equal(pending.children[1],next);
 assert.equal(heading.firstElementChild,count);
 assert.equal(preparing.children[1],row);
 assert.equal(row.children.at(-1).children[0],action);
 assert.equal(h.context.document.activeElement,action);
 assert.equal(bar.value,15);
 assert.equal(amount.textContent,'已下载 75%');
 assert.deepEqual(steps.children.map(step=>step.textContent),['原曲','伴奏','播放文件']);
 h.run('selected[1]={...selected[1],progress:{stage:"waiting_storage"}};render()');
 assert.equal(preparing.children[1],row);
 assert.equal(label.textContent,'等待 Mac 腾出缓存空间');
 assert.equal(bar.hidden,true);
 assert.equal(amount.hidden,true);
});

test('the queue follows a changed NAS playable order without waiting for another song update',()=>{
 const h=harness();
 h.context.roomStatus={songs:[{id:'a',song_id:'1',title:'候唱示例',state:'ready'}],playable:[],
  queue_revision:4,mac_online:true,playback:{online:true,current:'',played:[],state:'idle'},controls:{}};
 h.run('earEnabled=false;mode="queue";applyStatus(roomStatus)');
 const results=h.element('results');
 assert.equal(results.children[0].className,'queue-group preparing');
 h.context.roomStatus.playable=['a'];
 h.run('applyStatus(roomStatus)');
 assert.equal(results.children[0].className,'queue-group pending');
 assert.equal(h.element('now-title').textContent,'接下来：候唱示例');
});

test('TV takeover and a connection timeout update the current group without a changed queue',()=>{
 const h=harness();
 h.context.roomStatus={
  songs:[{id:'a',song_id:'1',title:'当前歌曲',state:'ready'},{id:'b',song_id:'2',title:'下一首歌曲',state:'ready'}],
  playable:['b'],queue_revision:4,mac_online:true,
  playback:{online:true,current:'a',played:[],state:'playing'},controls:{}
 };
 h.run('earEnabled=false;mode="queue";applyStatus(roomStatus)');
 const [current,next]=h.element('results').children;
 const heading=current.children[0], nextRow=next.children[1];
 assert.equal(heading.textContent,'正在唱');
 h.context.roomStatus.playback.online=false;
 h.run('applyStatus(roomStatus)');
 assert.equal(heading.textContent,'上次播放');
 assert.equal(h.element('skip-button').disabled,true);
 assert.equal(next.children[1],nextRow);

 h.context.roomStatus.playback.online=true;
 h.run('applyStatus(roomStatus)');
 assert.equal(heading.textContent,'正在唱');
 assert.equal(h.element('skip-button').disabled,false);
 h.run('lastStatusSuccess=roomResumeAt=performance.now()-13000;statusFailure({code:"request_timeout"})');
 assert.equal(heading.textContent,'上次播放');
 assert.equal(h.element('skip-button').disabled,true);
 assert.equal(next.children[1],nextRow);
});

test('now playing stays in the same dialog when the selected track loses and regains its route',async()=>{
 const h=harness({validRoom:true}), dialog=h.element('song-dialog'), play=h.element('sing-now'), commands=[];
 dialog.showModal=()=>{dialog.open=true;};
 dialog.close=()=>{dialog.open=false;};
 dialog.querySelector=()=>h.element('song-action-title');
 h.context.roomStatus={
  songs:[{id:'a',song_id:'1',title:'测试歌曲',state:'ready',lyric_state:1}],
  playable:['a'],queue_revision:4,mac_online:true,
  playback:{online:true,current:'',played:[],state:'idle'},controls:{}
 };
 h.context.submit=async(operation,body)=>{
  assert.equal(operation,'command');
  commands.push(body);
  if(commands.length===1)throw Object.assign(new Error(),{code:'media_not_ready'});
  return {seq:8};
 };
 h.run('earEnabled=false;api=submit;waitForTV=async()=>{};refresh=async()=>{};applyStatus(roomStatus);songActions(selected[0])');
 assert.equal(dialog.open,true);
 assert.equal(play.hidden,false);
 assert.equal(play.disabled,false);
 await play.onclick();
 assert.equal(dialog.open,true);
 assert.equal(h.element('song-error').textContent,'所选音轨暂时不可用。');
 h.context.roomStatus.playable=[];
 h.run('applyStatus(roomStatus)');
 assert.equal(play.hidden,false);
 assert.equal(play.disabled,true);
 await play.onclick();
 assert.equal(commands.length,1);
 h.context.roomStatus.playable=['a'];
 h.run('applyStatus(roomStatus)');
 assert.equal(play.disabled,false);
 assert.equal(h.element('song-error').textContent,'');
 await play.onclick();
 assert.equal(dialog.open,false);
 assert.deepEqual(commands.map(command=>[command.action,command.song]),[['play','a'],['play','a']]);
});

test('IME search waits for composition and debounces the committed text once',async()=>{
 const h=harness(),queries=[];h.context.searchApi=async(operation,body,query)=>{queries.push(query);return {songs:[],has_more:false};};h.run('earEnabled=false;mode="find";api=searchApi;$("query").oncompositionstart();$("query").value="zhou";$("query").oninput({isComposing:true})');
 await new Promise(resolve=>setTimeout(resolve,280));assert.equal(queries.length,0);
 h.run('$("query").value="周杰伦";$("query").oncompositionend();$("query").oninput({isComposing:false})');await new Promise(resolve=>setTimeout(resolve,280));assert.equal(queries.length,1);assert.match(queries[0],/q=%E5%91%A8%E6%9D%B0%E4%BC%A6/);
});

test('typing cancels the old search fetch immediately while independent controls still leave',async()=>{
 const h=harness({validRoom:true}),requests=[];h.context.fetch=async(url,options)=>{
  requests.push({url,options});if(url.includes('/search')&&url.includes('q=old'))return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Object.assign(new Error(),{name:'AbortError'})),{once:true}));
  return {ok:true,status:200,json:async()=>url.includes('/controls')?{revision:1}:{songs:[{song_id:'2',title:'new'}],has_more:false}};
 };
 h.run('earEnabled=false;mode="find";api=actualApi;setControl=actualSetControl;waitForTV=async()=>{};$("query").value="old"');const first=h.run('findSongs()');await flush();
 await h.run('setControl({music_gain:0})');assert.equal(requests.some(r=>r.url.includes('/controls')),true);
 h.run('$("query").value="new";$("query").oninput({isComposing:false})');assert.equal(requests[0].options.signal.aborted,true);await first;await new Promise(resolve=>setTimeout(resolve,280));assert.equal(h.run('songs[0].title'),'new');assert.equal(requests.filter(r=>r.url.includes('/search')).length,2);
});

test('event status advances UI with calibration explicitly disabled',()=>{
 const h=harness();let options,calibration;h.context.FridayStatusFeed=class{constructor(value){options=value;}start(){}close(){}};h.context.clockObserve=(value,start,end,mode)=>{calibration=mode;return true;};
 h.run('earEnabled=false;earClock.observe=clockObserve;startStatusFeed()');options.onStatus({server_time_ms:1000,mac_online:true,songs:[],playback:{online:true,current:"",played:[],state:"idle",mic_muted:true},controls:{},theme:'content'});
 assert.equal(calibration.calibrate,false);assert.equal(h.run('online'),true);assert.equal(h.element('connection-status').hidden,true);
});

test('hidden/pagehide cancel subscriptions and pending query work without starting background searches',async()=>{
 const h=harness();h.run('earEnabled=false;mode="find";var feedStops=0;statusFeed={stop(){feedStops++;}};var observer=new AbortController();searchController=observer;$("query").value="pending";document.hidden=true');
 h.emit('document','visibilitychange');assert.equal(h.run('observer.signal.aborted'),true);assert.equal(h.run('feedStops'),1);
 h.run('document.hidden=false;observer=new AbortController();searchController=observer');h.emit('window','pagehide');assert.equal(h.run('observer.signal.aborted'),true);assert.equal(h.run('feedStops'),2);
 h.run('mode="home";var searches=0;api=async()=>{searches++;return {songs:[]};};$("query").oncompositionend()');await new Promise(resolve=>setTimeout(resolve,280));assert.equal(h.run('searches'),0);
});

test('event progress leaves the active drag alone and refreshes the original row after release',()=>{
 const h=harness();h.run('earEnabled=false;mode="queue";playback.current="";selected=[{id:"a",song_id:"1",state:"acquiring",progress:{source_bytes:10}}];render();drag={id:"a"}');const group=h.element('results').children[0],row=group.children[1];
 h.context.progressStatus={mac_online:true,theme:'content',songs:[{id:'a',song_id:'1',state:'acquiring',progress:{source_bytes:20}}],playback:{online:true,current:'',played:[],state:'idle'},controls:{}};
 h.run('applyStatus(progressStatus)');assert.equal(group.children[1],row);assert.equal(h.run('selected[0].progress.source_bytes'),10);
 h.run('drag=null;applyStatus(progressStatus)');assert.equal(group.children[1],row);assert.equal(h.run('selected[0].progress.source_bytes'),20);
});

test('the guide verifies and plays only the original track without any microphone or reference request',async()=>{
 const h=harness(),bytes=Buffer.alloc(2048,2);
 h.context.playableSpec={sha256:Buffer.from(await webcrypto.subtle.digest('SHA-256',bytes)).toString('hex'),bytes:2048,duration_ms:5000};
 const requested=[];
 h.context.fetch=async(url,options)=>{requested.push({url,options});return {ok:true,arrayBuffer:async()=>Uint8Array.from(bytes).buffer};};
 h.run('playback.guide=playableSpec');await h.run('loadGuide()');
 assert.equal(requested.length,1);assert.match(requested[0].url,/\/guide\?id=song-a$/);
 assert.equal(requested[0].options.method,undefined);
 assert.equal(h.run('earCanPlay'),true);
 h.element('guide-delay').onclick();assert.equal(h.run('earClock.adjustment'),-50);
 h.emit('window','pagehide');assert.equal(h.run('earEnabled'),false);assert.equal(h.run('ear.buffer'),null);
 assert.doesNotMatch(source,/getUserMedia|sync-capture|sync\.mjs/);
});

test('a decoded duration mismatch never reaches speaker playback',async()=>{
 const h=harness(),bytes=Buffer.alloc(2048,3);
 h.context.playableSpec={sha256:Buffer.from(await webcrypto.subtle.digest('SHA-256',bytes)).toString('hex'),bytes:2048,duration_ms:6000};
 h.context.fetch=async()=>({ok:true,arrayBuffer:async()=>Uint8Array.from(bytes).buffer});
 h.run('playback.guide=playableSpec');await h.run('loadGuide()');
 assert.equal(h.run('ear.syncs'),0);assert.equal(h.run('earEnabled'),false);
});

test('a partial status snapshot does not corrupt the room state used by auto join or search',async()=>{
 const h=harness({validRoom:true});
 h.run('selected=[{id:"keep",song_id:"123",state:"ready"}];applyStatus({mac_online:true})');
 h.context.fetch=async()=>({ok:true,status:200,json:async()=>({server_time_ms:1,playback:{played:[]}})});
 await assert.rejects(h.run('actualApi("status")'),{code:'service_unavailable'});
 assert.equal(h.run('selected[0].id'),'keep');h.run('tabs()');
 assert.equal(h.element('search-entry').hidden,false);
});

test('裸入口一次加入请求恢复身份并直接显示返回的歌单快照',async()=>{
 const storage=memoryStorage(),seed='12'.repeat(32),reply=currentRoomReply(),requests=[];
 storage.setItem('friday-ktv-guest-seed-v1',seed);
 reply.status.songs=[{id:'44444444-4444-4444-8444-444444444444',song_id:'123',title:'返回的歌单',state:'ready'}];
 const h=harness({localStorage:storage});
 h.context.fetch=async(url,options)=>{
  requests.push({url,options});
  return {ok:true,status:201,json:async()=>reply};
 };
 h.run('earEnabled=false;identity=null;api=actualApi;startStatusFeed=loadDiscover=initAddQueue=refresh=()=>{}');
 await h.run('enterCurrentRoom()');
 assert.equal(requests.length,1);
 assert.equal(requests[0].url,'./?room=current');
 assert.equal(requests[0].options.method,'POST');
 const payload=JSON.parse(requests[0].options.body);
 assert.equal(payload.snapshot,true);
 assert.equal(payload.device_key,seed);
 assert.equal(h.run('room'),secondRoom);
 assert.equal(h.run('identity.id'),reply.guest.id);
 assert.equal(h.run('selected[0].title'),'返回的歌单');
 assert.equal(h.element('my-name').textContent,'新场朋友');
});

test('新页面遇到旧 NAS 时在字段明确被拒绝后按原凭据兼容加入', {timeout:1500},async()=>{
 const storage=memoryStorage(),requests=[],joined=deferred();
 storage.setItem('friday-ktv-guest-profile',JSON.stringify({nickname:'保留的名字',avatar:3}));
 const h=harness({localStorage:storage});
 h.context.fetch=async(url,options)=>{
  const body=options.body?JSON.parse(options.body):null;requests.push({url,body});
  if(url==='./?room=current'){
   if(body)return {ok:false,status:404,json:async()=>({code:'not_found'})};
   return {ok:true,status:200,json:async()=>({room:firstRoom,key:'a'.repeat(43)})};
  }
  assert.match(url,/\/join$/);
  if(body.device_key)return {ok:false,status:400,json:async()=>({code:'invalid_request'})};
  joined.resolve();
  return {ok:true,status:201,json:async()=>({id:'33333333-3333-4333-8333-333333333333',key:body.guest_key,...body.profile})};
 };
 h.run('earEnabled=false;identity=null;api=actualApi;startStatusFeed=loadDiscover=initAddQueue=refresh=()=>{}');
 await h.run('enterCurrentRoom()');await joined.promise;await flush();
 const joins=requests.filter(request=>request.url.endsWith('/join'));
 assert.equal(joins.length,2);
 assert.equal(joins[0].body.guest_key,joins[1].body.guest_key);
 assert.deepEqual(Object.keys(joins[1].body).sort(),['guest_key','profile']);
 assert.equal(h.run('identity.nickname'),'保留的名字');
 assert.equal(h.run('identity.avatar'),3);
});

test('已点封面延迟返回时设置立即响应并抵抗旧风格快照',async()=>{
 const h=harness(),covers=deferred(),theme=deferred(),requests=[];
 h.context.coverApi=async(operation,_body,query,options)=>{
  requests.push({operation,query,priority:options?.priority});
  if(operation==='artwork')return covers.promise;
  if(operation==='theme')return theme.promise;
  throw Error('unexpected operation');
 };
 h.run('earEnabled=false;mode="queue";playback.current="";api=coverApi;themeRevision=4;selected=[{id:"queued",song_id:"123",source:"song",title:"已点歌曲",state:"ready"}];render()');
 assert.equal(h.run('resultRowCache.get("queue:queued").row.firstElementChild.className'),'cover placeholder');
 const changing=h.run('chooseTheme("festival",$("theme-button"))');
 assert.equal(h.context.document.body.dataset.theme,'festival');
 assert.deepEqual(requests.map(request=>request.operation),['artwork','theme']);
 assert.equal(requests[1].priority,'high');
 h.context.oldStatus={theme:'content',theme_revision:4,songs:[{id:'queued',song_id:'123',source:'song',title:'已点歌曲',state:'ready'}],
  mac_online:true,playback:{online:false,current:'',played:[],state:'idle'},controls:{}};
 h.run('applyStatus(oldStatus)');
 assert.equal(h.context.document.body.dataset.theme,'festival');
 theme.resolve({theme:'festival',theme_revision:5});await changing;
 h.run('applyStatus(oldStatus)');
 assert.equal(h.context.document.body.dataset.theme,'festival');
 covers.resolve({covers:{'123':'data:image/jpeg;base64,AAAA'},pending:[]});
 await flush();
 assert.equal(h.run('resultRowCache.get("queue:queued").row.firstElementChild.tagName'),'IMG');
 assert.equal(h.run('resultRowCache.get("queue:queued").row.firstElementChild.src'),'data:image/jpeg;base64,AAAA');
});

test('电视离线后歌单显示上次播放，重新在线时恢复实际状态',()=>{
 const h=harness();
 h.run('earEnabled=false;mode="queue";playback={online:false,current:"one",played:[],state:"playing"};selected=[{id:"one",song_id:"123",title:"本场歌曲",state:"ready"}];render()');
 assert.equal(h.run('resultGroupCache.get("current").children[0].textContent'),'上次播放');
 const old=h.run('resultRowCache.get("queue:one").row');
 h.run('playback.online=true;playback.state="paused";render()');
 assert.equal(h.run('resultGroupCache.get("current").children[0].textContent'),'已暂停');
 assert.notEqual(h.run('resultRowCache.get("queue:one").row'),old);
});

test('相同数值 ID 的云盘歌曲与曲库歌曲分别补封面且不会串图',async()=>{
 const h=harness(),requests=[];
 h.context.coverApi=async(_operation,_body,query)=>{
  const params=new URLSearchParams(query),source=params.get('source')||'song';
  requests.push({source,ids:params.get('ids')});
  return {covers:{'123':source==='cloud'?'data:image/jpeg;base64,BBBB':'data:image/jpeg;base64,AAAA'},pending:[],
   tracks:{'123':{artists:[source==='cloud'?'云盘歌手':'曲库歌手'],album:source==='cloud'?'云盘专辑':'曲库专辑'}}};
 };
 h.run('earEnabled=false;mode="queue";playback.current="";api=coverApi;selected=[{id:"public",song_id:"123",source:"song",title:"曲库版本",state:"ready"},{id:"cloud",song_id:"123",source:"cloud",title:"云盘版本",state:"ready"}];render()');
 await flush();
 assert.deepEqual(requests.map(request=>request.source).sort(),['cloud','song']);
 assert.ok(requests.every(request=>request.ids==='123'));
 assert.equal(h.run('resultRowCache.get("queue:public").row.firstElementChild.src'),'data:image/jpeg;base64,AAAA');
 assert.equal(h.run('resultRowCache.get("queue:cloud").row.firstElementChild.src'),'data:image/jpeg;base64,BBBB');
 const walk=node=>[node,...node.children.flatMap(walk)];
 const metadata=key=>walk(h.run(`resultRowCache.get("queue:${key}").row`)).find(node=>node.className==='artist').textContent;
 assert.equal(metadata('public'),'曲库歌手 · 曲库专辑');
 assert.equal(metadata('cloud'),'云盘歌手 · 云盘专辑');
});

test('封面解码失败后退避重取并恢复显示，重复渲染不会密集重试',async()=>{
 const h=harness(),timers=[];let clock=1000,requests=0;
 const first='data:image/jpeg;base64,AAAA',recovered='data:image/jpeg;base64,BBBB';
 h.context.performance={now:()=>clock};
 h.context.setTimeout=(run,delay)=>{timers.push({run,delay});return timers.length;};
 h.context.coverApi=async()=>({covers:{'123':++requests===1?first:recovered},pending:[]});
 h.run('earEnabled=false;mode="queue";playback.current="";api=coverApi;selected=[{id:"queued",song_id:"123",source:"song",title:"重试封面",state:"ready"}];render()');
 await flush();
 const row=h.run('resultRowCache.get("queue:queued").row');
 assert.equal(row.firstElementChild.src,first);
 row.firstElementChild.onerror();
 assert.equal(row.firstElementChild.className,'cover placeholder');
 h.run('render();render()');await flush();
 assert.equal(requests,1);
 const retry=timers.find(timer=>timer.delay>0);
 assert.ok(retry,'解码失败后应安排后台重试');
 clock+=retry.delay;retry.run();await flush();
 assert.equal(requests,2);
 assert.equal(row.firstElementChild.tagName,'IMG');
 assert.equal(row.firstElementChild.src,recovered);
});

test('坏图退避期间新出现的健康封面立即加载并独立完成',async()=>{
 const h=harness(),timers=[],requests=[];let clock=1000;
 h.context.performance={now:()=>clock};
 h.context.setTimeout=(run,delay)=>{timers.push({run,delay});return timers.length;};
 h.context.coverApi=async(_operation,_body,query)=>{
  const ids=new URLSearchParams(query).get('ids').split(',');requests.push(ids);
  return {covers:Object.fromEntries(ids.map(id=>[id,id==='456'?'data:image/jpeg;base64,BBBB':'data:image/jpeg;base64,AAAA'])),pending:[]};
 };
 h.run('earEnabled=false;mode="queue";playback.current="";api=coverApi;selected=[{id:"broken",song_id:"123",source:"song",state:"ready"}];render()');
 await flush();
 h.run('resultRowCache.get("queue:broken").row.firstElementChild.onerror();selected.push({id:"healthy",song_id:"456",source:"song",state:"ready"});render()');
 await flush();
 assert.deepEqual(requests,[['123'],['456']]);
 assert.equal(h.run('resultRowCache.get("queue:healthy").row.firstElementChild.src'),'data:image/jpeg;base64,BBBB');
 assert.equal(h.run('resultRowCache.get("queue:broken").row.firstElementChild.className'),'cover placeholder');
 assert.equal(clock,1000);
 const retry=timers.at(-1);clock+=retry.delay;retry.run();await flush();
 assert.deepEqual(requests,[['123'],['456'],['123']]);
});

test('常唱从服务端恢复手机离开期间的云盘演唱并合并旧的本机记录',async()=>{
 const h=harness(),history=deferred();
 h.context.historyApi=async(operation)=>operation==='profile'?history.promise:{covers:{},pending:[]};
 h.run('earEnabled=false;api=historyApi;frequentSongs=[{song_id:"111",source:"song",title:"本机旧记录",plays:2}];setView("frequent")');
 assert.equal(h.element('page-title').textContent,'常唱歌曲');
 history.resolve({songs:[{song_id:'222',source:'cloud',title:'离开期间唱过',plays:5,cached_accompaniment:true}]});
 await h.run('frequentRequest.task');
 assert.deepEqual(Array.from(h.run('frequentRows().map(song=>song.title)')),['离开期间唱过','本机旧记录']);
 assert.equal(h.element('results').children.length,2);
 assert.equal(h.element('empty').hidden,true);
 assert.equal(h.run('resultRowCache.get("frequent:cloud:222").row.children.at(-1).dataset.song'),'cloud:222');
});

test('常唱因访客凭据失效而重新加入后自动恢复读取', {timeout:1500},async()=>{
 const h=harness({validRoom:true}),restored=deferred();let reads=0,joins=0;
 h.context.recoveryApi=async operation=>{
  if(operation==='profile'){
   reads++;
   if(reads===1)throw Object.assign(new Error(),{code:'guest_unavailable',status:403});
   restored.resolve();
   return {songs:[{song_id:'222',source:'cloud',title:'恢复后的常唱',plays:4}]};
  }
  if(operation==='join'){
   joins++;
   return {...currentRoomReply().guest,status:currentRoomReply().status};
  }
  if(operation==='artwork')return {covers:{},pending:[]};
  throw Error('unexpected operation');
 };
 h.run('earEnabled=false;api=recoveryApi;startStatusFeed=loadDiscover=initAddQueue=refresh=()=>{};mode="frequent"');
 await h.run('loadFrequent()');await restored.promise;await flush();
 assert.equal(joins,1);
 assert.equal(reads,2);
 assert.equal(h.run('frequentRows()[0].title'),'恢复后的常唱');
 assert.equal(h.element('empty').hidden,true);
});
