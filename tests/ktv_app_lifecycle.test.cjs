const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),{webcrypto}=require('node:crypto');
const web=path.join(__dirname,'../integrations/ktv_public/web');
const source=fs.readFileSync(path.join(web,'app.js'),'utf8').replace('join();document.addEventListener','document.addEventListener');
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const firstRoom='11111111-1111-4111-8111-111111111111';
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
 class Node{constructor(id=''){this.id=id;this.children=[];this.dataset={};this.style={setProperty(name,value){this[name]=value;}};this.value='';this.textContent='';this.hidden=false;this.isConnected=true;this.classList={add(){},remove(){},toggle(){}};this.attributes={};this.parentElement=null;}
  append(...nodes){for(const n of nodes){if(n.parentElement)n.parentElement.removeChild(n);this.children.push(n);n.parentElement=this;}}replaceChildren(...n){for(const old of this.children)old.parentElement=null;this.children=[];this.append(...n);}removeChild(n){const at=this.children.indexOf(n);if(at>=0)this.children.splice(at,1);n.parentElement=null;return n;}setAttribute(k,v){this.attributes[k]=v;}getAttribute(k){return this.attributes[k];}querySelector(q){return q==='button'?(this.button??=new Node()):null;}querySelectorAll(){return [];}closest(){return null;}addEventListener(){}focus(){document.activeElement=this;}insertBefore(n,before=null){if(n.parentElement)n.parentElement.removeChild(n);const at=before?this.children.indexOf(before):-1;this.children.splice(at<0?this.children.length:at,0,n);n.parentElement=this;}}
 const element=id=>{if(!nodes.has(id))nodes.set(id,new Node(id));return nodes.get(id);};
 const document={...events('document'),hidden:false,body:new Node('body'),getElementById:element,createElement:()=>new Node(),querySelector:q=>q==='dialog[open]'?null:element(q),querySelectorAll:()=>[],activeElement:null};
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
 h.context.state.readiness.gates.qr=false;assert.match(h.run('readinessHint(state)'),/正在准备本场/);
 h.context.state.readiness.ready=true;assert.equal(h.run('readinessHint(state)'),'');
 assert.doesNotMatch(h.run('readinessHint({mac_online:false})'),/房间.*离线|本场.*结束/);
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
 const second=h.run('setControl({music_gain:0})');
 try{
  await flush();assert.equal(requests.length,1);h.run('aborter.abort()');assert.equal(requests[0].signal.aborted,true);
  assert.equal(await first,'cancelled');await second;assert.equal(requests.length,2);assert.deepEqual(requests[1].payload,{music_gain:0});
  assert.equal(h.run('controls.original'),1);assert.equal(h.run('controls.music_gain'),0);
 }finally{rejectFirst?.(Object.assign(new Error(),{name:'AbortError'}));}
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

test('search shows the first page before requesting the next page near the list end',async()=>{
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
 assert.equal(requests.filter(r=>r.operation==='search').length,1);
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
 const h=harness({validRoom:true}),held=deferred();h.context.held=held;
 h.run('earEnabled=false;setControl=actualSetControl;waitForTV=async()=>{};join=async()=>{};controlWrites=held.promise;var writes=[];api=async(operation,value)=>{if(operation==="current")return {room:"22222222-2222-4222-8222-222222222222",key:"b".repeat(43)};writes.push({room,value});return {revision:1};};');
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
 const h=harness({validRoom:true});
 h.run('earEnabled=false;setControl=actualSetControl;join=async()=>{};lastStatusSuccess=performance.now();api=async(operation)=>operation==="current"?{room:"22222222-2222-4222-8222-222222222222",key:"b".repeat(43)}:{revision:3};');
 const waiting=h.run('setControl({original:1},{propagate:true})').catch(error=>error.code);
 await flush();assert.equal(h.run('receiptListeners.size'),1);
 await h.run('enterCurrentRoom(true)');
 assert.equal(await waiting,'cancelled');
 assert.equal(h.run('receiptListeners.size'),0);
 assert.equal(h.run('modeIntent'),null);
 assert.equal(h.element('sound-error').textContent,'');
});

test('automatic room recovery cancels the old transport command without disabling the new room',async()=>{
 const h=harness({validRoom:true});
 h.run('earEnabled=false;join=async()=>{};lastStatusSuccess=performance.now();api=async(operation)=>operation==="current"?{room:"22222222-2222-4222-8222-222222222222",key:"b".repeat(43)}:{seq:3};');
 const old=h.run('sendCommand("pause")');
 await flush();assert.equal(h.run('commandBusy'),true);
 assert.equal(h.run('receiptListeners.size'),1);
 await h.run('enterCurrentRoom(true)');await old;
 assert.equal(h.run('commandBusy'),false);
 assert.equal(h.run('receiptListeners.size'),0);
 assert.equal(h.element('play-toggle').getAttribute('aria-busy'),'false');
});

test('pending thumbnails back off until actual artwork arrives and respect Retry-After',async()=>{
 const h=harness(),timers=[];
 h.context.setTimeout=(run,delay)=>{timers.push({run,delay});return timers.length;};
 h.run('earEnabled=false;mode="find";artworkPending.add("1");api=async()=>({covers:{},pending:["1"]});');
 await h.run('enrichArtwork()');
 for(let i=0;i<3;i++){timers.at(-1).run();await flush();}
 assert.deepEqual(timers.map(timer=>timer.delay),[650,1300,2600,5200]);
 h.run('api=async()=>{throw Object.assign(new Error(),{code:"rate_limited",retryAfterMs:12000});}');
 timers.at(-1).run();await flush();assert.equal(timers.at(-1).delay,12000);
 h.run('api=async()=>({covers:{"1":"data:image/jpeg;base64,AAAA"},pending:[]});');
 timers.at(-1).run();await flush();
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
 assert.deepEqual(Object.keys(server.requests.at(-1).payload).sort(),['guest_key','profile']);
 assert.equal(JSON.stringify(server.requests).includes(seed),false);
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
 const h=harness();h.run('earEnabled=false;selected=[{id:playback.current,title:"当前歌曲"}];playback.safety_latched=true;playback.audio_capabilities={sfx:true,voice_protection:{state:"failed"}};updatePlayer()');
 assert.match(h.element('player-status').textContent,/正在唱/);
});

test('failed songs no longer count as upcoming and warmup can cut into the actual queue',()=>{
 const h=harness();h.run('earEnabled=false;playback.current="";mode="queue";selected=[{id:"a",song_id:"1",title:"失败",state:"failed"},{id:"b",song_id:"2",title:"下首",state:"ready"},{id:"c",song_id:"3",title:"待制备",state:"acquiring"}];render();updatePlayer()');
 assert.equal(h.element('queue-count').textContent,2);
 assert.equal(h.element('results').children[0].className,'queue-group pending');
 assert.equal(h.element('results').children[1].className,'queue-group preparing');
 assert.equal(h.element('results').children[2].className,'queue-group failed');
 assert.equal(h.element('now-title').textContent,'接下来：下首');
 assert.equal(h.element('skip-button').disabled,false);
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

test('queue progress changes replace only the changed row and retain stable groups, focus and history disclosure',()=>{
 const h=harness();h.run('earEnabled=false;mode="queue";playback.current="";playback.played=["old"];selected=[{id:"a",song_id:"1",title:"下载",state:"acquiring",progress:{source_bytes:10}},{id:"b",song_id:"2",title:"已就绪",state:"ready"},{id:"old",song_id:"3",title:"已唱",state:"ready"}];render()');
 const root=h.element('results'),pending=root.children[0],preparing=root.children[1],history=root.children[2],a=preparing.children[1],b=pending.children[1];history.open=true;const focused=b.children.at(-1).children[0];focused.focus();const insertions=[];const insert=pending.insertBefore.bind(pending);pending.insertBefore=(node,before)=>{insertions.push(node);insert(node,before);};
 h.run('selected[0]={...selected[0],progress:{source_bytes:20}};render()');assert.equal(root.children[0],pending);assert.equal(root.children[1],preparing);assert.equal(root.children[2],history);assert.equal(history.open,true);assert.notEqual(preparing.children[1],a);assert.equal(pending.children[1],b);assert.equal(h.context.document.activeElement,focused);assert.equal(insertions.includes(b),false,'an unchanged row is not detached/moved just because another progress state changes');
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

test('event progress cannot replace selected rows while a queue drag owns the DOM',()=>{
 const h=harness();h.run('earEnabled=false;mode="queue";playback.current="";selected=[{id:"a",song_id:"1",state:"acquiring",progress:{source_bytes:10}}];render();drag={id:"a"}');const group=h.element('results').children[0],row=group.children[1];
 h.context.progressStatus={mac_online:true,theme:'content',songs:[{id:'a',song_id:'1',state:'acquiring',progress:{source_bytes:20}}],playback:{online:true,current:'',played:[],state:'idle'},controls:{}};
 h.run('applyStatus(progressStatus)');assert.equal(group.children[1],row);assert.equal(h.run('selected[0].progress.source_bytes'),10);
 h.run('drag=null;applyStatus(progressStatus)');assert.notEqual(group.children[1],row);assert.equal(h.run('selected[0].progress.source_bytes'),20);
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
