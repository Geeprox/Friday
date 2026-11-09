const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),{webcrypto}=require('node:crypto');
const web=path.join(__dirname,'../integrations/ktv_public/web');
const source=fs.readFileSync(path.join(web,'app.js'),'utf8').replace('import("./sync.mjs")','Promise.resolve({FridayAcousticSync:TestSync})').replace('join();document.addEventListener','document.addEventListener');
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function harness({validRoom=false,deferFrames=false}={}){
 const nodes=new Map(),trace=[],alignments=[],listeners=new Map(),frames=[];
 const events=name=>({addEventListener(type,callback){const key=name+':'+type;if(!listeners.has(key))listeners.set(key,new Set());listeners.get(key).add(callback);}});
 const emit=(name,type,event={})=>{for(const callback of [...(listeners.get(name+':'+type)||[])])callback(event);};
 class Node{constructor(id=''){this.id=id;this.children=[];this.dataset={};this.style={};this.value='';this.textContent='';this.hidden=false;this.isConnected=true;this.classList={add(){},remove(){},toggle(){}};this.attributes={};this.parentElement=null;}
  append(...nodes){for(const n of nodes){if(n.parentElement)n.parentElement.removeChild(n);this.children.push(n);n.parentElement=this;}}replaceChildren(...n){for(const old of this.children)old.parentElement=null;this.children=[];this.append(...n);}removeChild(n){const at=this.children.indexOf(n);if(at>=0)this.children.splice(at,1);n.parentElement=null;return n;}setAttribute(k,v){this.attributes[k]=v;}getAttribute(k){return this.attributes[k];}querySelector(q){return q==='button'?(this.button??=new Node()):null;}querySelectorAll(){return [];}closest(){return null;}addEventListener(){}focus(){document.activeElement=this;}insertBefore(n,before=null){if(n.parentElement)n.parentElement.removeChild(n);const at=before?this.children.indexOf(before):-1;this.children.splice(at<0?this.children.length:at,0,n);n.parentElement=this;}}
 const element=id=>{if(!nodes.has(id))nodes.set(id,new Node(id));return nodes.get(id);};
 const document={...events('document'),hidden:false,body:new Node('body'),getElementById:element,createElement:()=>new Node(),querySelector:q=>q==='dialog[open]'?null:element(q),querySelectorAll:()=>[],activeElement:null};
 class Ear{constructor(clock,changed){this.clock=clock;this.changed=changed;this.stops=0;this.syncs=0;this.version=0;this.alignedGeneration=null;this.buffer=null;this.context={state:'running',decodeAudioData:async bytes=>({duration:5,marker:new Uint8Array(bytes)[0]})};}
  start(loader){trace.push('start');this.controller=new AbortController();return Promise.resolve().then(()=>loader(this.controller.signal)).then(bytes=>{this.buffer={duration:5,marker:new Uint8Array(bytes)[0]};return this.buffer;});}
  resumePlayback(){trace.push('resume');return Promise.resolve();}stop(){this.stops++;this.version++;this.controller?.abort();this.buffer=null;this.alignedGeneration=null;this.pendingResult=null;}halt(){trace.push('halt');}sync(){this.syncs++;return !this.needsAlignment()&&!this.capturePending();}stats(){return {};}volume(){}
  needsAlignment(){return !!this.buffer&&this.alignedGeneration!==this.clock.generation;}
  capturePending(){return !!this.liveCapture;}
  acceptAlignment(result){trace.push('accept');const pending=this.pendingResult;if(!pending||pending.result!==result||pending.generation!==this.clock.generation||pending.version!==this.version||pending.buffer!==this.buffer||this.capturePending())throw Object.assign(new Error(),{code:'changed'});this.clock.adjustment=result.offset;this.alignedGeneration=this.clock.generation;this.pendingResult=null;}
  setManualAdjustment(value){trace.push('manual');if(this.capturePending())throw Object.assign(new Error(),{code:'changed'});this.clock.adjustment=Math.max(-5000,Math.min(5000,value));this.alignedGeneration=this.clock.generation;}}
 class Clock{constructor(){this.adjustment=0;this.generation=0;}observe(){return true;}}
 class Sync{run(ear,clock,countdown,diagnostic,options){const d=deferred(),owner={version:ear.version,buffer:ear.buffer,generation:clock.generation};alignments.push({d,ear,clock,options});return d.promise.then(result=>{ear.pendingResult={...owner,result};return result;});}cancel(){trace.push('cancel');}}
 const context=vm.createContext({document,location:{hash:validRoom?'#r=11111111-1111-4111-8111-111111111111&k='+ 'a'.repeat(43):'',pathname:'/'},sessionStorage:{getItem:()=>null,setItem(){},removeItem(){}},history:{state:null,replaceState(value){this.state=value;},pushState(value){this.state=value;},back(){}},window:{...events('window'),scrollY:0,scrollTo(v){this.scrollY=v.top||0;}},URLSearchParams,AbortController,crypto:webcrypto,performance,setTimeout,clearTimeout,setInterval:()=>0,clearInterval(){},requestAnimationFrame:fn=>deferFrames?frames.push(fn):fn(),FridayGuideClock:Clock,FridayEarMonitor:Ear,FridayPartyAudio:require(path.join(web,'party-audio.js')),FridayAddQueue:require(path.join(web,'add-queue.js')),FridayStatusFeed:require(path.join(web,'status-feed.js')),TestSync:Sync,fetch:async()=>{throw Error('unexpected fetch');}});
 vm.runInContext(source,context);
 const run=code=>vm.runInContext(code,context);
 run('var actualSetControl=setControl,actualApi=api;api=async()=>({});setControl=async()=>{controls.original=0;};identity={key:"fixture"};playback={online:true,current:"song-a",played:[],state:"playing",guide:{sha256:"a".repeat(64),bytes:2048,duration_ms:5000,reference:{kind:"accompaniment",sha256:"b".repeat(64),bytes:2048,duration_ms:5000}}};earEnabled=true;controls.original=0;');
 return {context,run,nodes,trace,alignments,element,emit,flushFrames(){while(frames.length)frames.shift()();}};
}

test('guide and app scripts share globals without duplicate declarations',()=>{
 new vm.Script(fs.readFileSync(path.join(web,'guide.js'),'utf8')+'\n'+source);
});

test('phone distinguishes a retained room from incomplete TV and worker readiness',()=>{
 const h=harness();
 h.context.state={mac_online:true,readiness:{state:'degraded',ready:false,gates:{link_tv:true,tv_audio:true,link_worker:false}}};
 assert.match(h.run('readinessHint(state)'),/歌曲准备服务正在连接/);
 h.context.state.readiness.gates={link_tv:false};assert.match(h.run('readinessHint(state)'),/电视暂未连接/);
 h.context.state.readiness.gates={link_tv:true,tv_audio:false};assert.match(h.run('readinessHint(state)'),/电视正在准备/);
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

test('old matching result cannot apply offset or resume a newer session',async()=>{
 const h=harness();h.run('var firstOp=beginGuide();ear.buffer={};referenceBuffer={};referenceSong="song-a"');
 const first=h.run('prepareAlignment(firstOp)').catch(e=>e);await flush();
 h.run('playback.current="song-b";var secondOp=beginGuide();ear.buffer={};referenceBuffer={};referenceSong="song-b"');
 const second=h.run('prepareAlignment(secondOp)');await flush();
 h.alignments[0].d.resolve({offset:1700,frames:100,score:.8,margin:.3});await first;
 assert.equal(h.run('earClock.adjustment'),0);assert.equal(h.run('alignLoading'),true);assert.equal(h.run('ear.syncs'),0);
 h.alignments[1].d.resolve({offset:25,frames:100,score:.8,margin:.3});await second;
 assert.equal(h.run('earClock.adjustment'),25);assert.equal(h.run('earCanPlay'),true);
});

test('reference bytes are verified and kept separate from the original output buffer',async()=>{
 const h=harness(),original=new Uint8Array(2048).fill(1),reference=new Uint8Array(2048).fill(2),hash=async b=>Buffer.from(await webcrypto.subtle.digest('SHA-256',b)).toString('hex');
 h.context.spec={sha256:await hash(original),bytes:2048,duration_ms:5000,reference:{kind:'accompaniment',sha256:await hash(reference),bytes:2048,duration_ms:5000}};
 const fetched=[];h.context.fetch=async(url,options)=>{fetched.push([url,options.signal]);return {ok:true,arrayBuffer:async()=>(url.includes('variant=reference')?reference:original).slice().buffer};};
 h.run('playback.guide=spec');const pending=h.run('loadGuide()');assert.equal(h.trace[0],'start');
 for(let i=0;i<20&&!h.alignments.length;i++)await flush();
 assert.equal(h.alignments.length,1);assert.equal(h.alignments[0].ear.buffer.marker,1);assert.equal(h.alignments[0].options.referenceBuffer.marker,2);assert.equal(h.alignments[0].options.referenceSong,'song-a');
 assert.equal(fetched.length,2);assert.equal(fetched[0][1],fetched[1][1]);
 h.alignments[0].d.resolve({offset:0,frames:100,score:.9,margin:.4});await pending;
 assert.equal(h.run('earCanPlay'),true);
});

test('retry resumes audio in the click task before alignment waits',async()=>{
 const h=harness();h.run('beginGuide();ear.buffer={};referenceBuffer={};referenceSong="song-a";earCanPlay=false');
 h.trace.length=0;h.element('guide-toggle').onclick();assert.equal(h.trace[0],'resume');
 await flush();assert.equal(h.alignments.length,1);h.run('invalidateGuide({disable:true})');
 h.alignments[0].d.resolve({offset:500,frames:100,score:.9,margin:.3});await flush();assert.equal(h.run('earClock.adjustment'),0);
});

test('search and queue remain distinct while preserving query and scroll',()=>{
 const h=harness();h.run('mode="find";selected=[{id:"queued",song_id:"123",title:"已点歌曲",state:"ready"}];songs=[{song_id:"456",title:"搜索结果"}];searchQuery="保留搜索词";$("query").value=searchQuery;window.scrollY=90;render()');
 assert.equal(h.element('results').children.length,1);assert.equal(h.element('search').hidden,false);
 h.run('setView("queue")');assert.equal(h.element('search').hidden,true);assert.equal(h.element('results').children[0].className,'queue-group pending');
 h.run('window.scrollY=35;setView("find")');assert.equal(h.element('query').value,'保留搜索词');assert.equal(h.context.window.scrollY,90);assert.equal(h.element('results').children.length,1);
});


test('a retry owns pending resume immediately and a second click cancels instead of starting another matcher',async()=>{
 const h=harness(),resume=deferred();h.context.resume=resume;
 h.run('beginGuide();ear.buffer={};referenceBuffer={};referenceSong="song-a";earCanPlay=false;ear.resumePlayback=()=>resume.promise');
 h.element('guide-toggle').onclick();assert.equal(h.run('earLoading'),true);
 h.element('guide-toggle').onclick();assert.equal(h.run('earEnabled'),false);assert.equal(h.run('earLoading'),false);
 resume.resolve();await flush();assert.equal(h.alignments.length,0);assert.equal(h.run('earCanPlay'),false);
});

test('same-song guide version changes invalidate the pending result',async()=>{
 const h=harness();h.run('var op=beginGuide();ear.buffer={};referenceBuffer={};referenceSong="song-a"');
 const pending=h.run('prepareAlignment(op)').catch(e=>e);await flush();
 h.run('playback.guide={...playback.guide,sha256:"c".repeat(64)}');
 h.alignments[0].d.resolve({offset:800,frames:100,score:.9,margin:.3});await pending;
 assert.equal(h.run('earClock.adjustment'),0);assert.equal(h.run('ear.syncs'),0);
 h.run('invalidateGuide({disable:true})');
});

test('failed alignment attempts do not recapture on status refresh, and user retry activates audio synchronously',async()=>{
 const h=harness();h.run('var op=beginGuide();ear.buffer={};referenceBuffer={};referenceSong="song-a"');
 const pending=h.run('prepareAlignment(op)');await flush();h.alignments[0].d.reject(Object.assign(new Error(),{code:'quiet'}));await pending;
 for(let i=0;i<20;i++)h.run('updateGuide()');assert.equal(h.alignments.length,1);assert.equal(h.run('earCanPlay'),false);assert.equal(h.element('guide-toggle').textContent,'重试');
 h.trace.length=0;h.element('guide-toggle').onclick();assert.equal(h.trace[0],'resume');await flush();assert.equal(h.alignments.length,2);
 h.alignments[1].d.resolve({offset:-150,frames:296,score:.9,margin:.2});await flush();assert.equal(h.run('earCanPlay'),true);assert.equal(h.run('earClock.adjustment'),-150);
 for(let i=0;i<20;i++)h.run('updateGuide()');assert.equal(h.alignments.length,2);h.run('invalidateGuide({disable:true})');
});

test('same-song replay requests one new alignment and stops the prior speaker output immediately',async()=>{
 const h=harness();h.run('var op=beginGuide();ear.buffer={};referenceBuffer={};referenceSong="song-a"');
 const first=h.run('prepareAlignment(op)');await flush();h.alignments[0].d.resolve({offset:100,frames:296,score:.9,margin:.2});await first;
 h.trace.length=0;h.run('earClock.generation++;updateGuide()');assert.equal(h.trace[0],'halt');assert.equal(h.run('earCanPlay'),false);assert.equal(h.run('alignLoading'),true);
 for(let i=0;i<20;i++)h.run('updateGuide()');await flush();assert.equal(h.alignments.length,2);assert.equal(h.run('op.alignmentGeneration'),1);
 h.alignments[1].d.resolve({offset:-40,frames:296,score:.9,margin:.2});await flush();assert.equal(h.run('earClock.adjustment'),-40);assert.equal(h.run('earCanPlay'),true);
 h.run('invalidateGuide({disable:true})');
});

test('replay during initial alignment preserves the replacement attempt UI through the old loader finally',async()=>{
 const h=harness();h.run('ear.start=async()=>{ear.buffer={duration:5};referenceBuffer={};referenceSong=playback.current;}');
 const load=h.run('loadGuide()');await flush();assert.equal(h.alignments.length,1);
 h.run('earClock.generation++;updateGuide()');h.alignments[0].d.reject(Object.assign(new Error(),{code:'changed'}));await load;await flush();
 assert.equal(h.alignments.length,2);assert.equal(h.run('alignLoading'),true);assert.equal(h.element('guide-align-cancel').hidden,false);assert.equal(h.element('guide-toggle').getAttribute('aria-busy'),'true');assert.equal(h.element('guide-toggle').textContent,'取消');
 h.alignments[1].d.resolve({offset:80,frames:296,score:.9,margin:.2});await flush();assert.equal(h.run('earCanPlay'),true);h.run('invalidateGuide({disable:true})');
});

test('replay during a retry preserves the replacement attempt UI through the old retry finally',async()=>{
 const h=harness();h.run('var op=beginGuide();ear.buffer={};referenceBuffer={};referenceSong="song-a";earCanPlay=false');
 h.element('guide-toggle').onclick();await flush();assert.equal(h.alignments.length,1);
 h.run('earClock.generation++');h.alignments[0].d.reject(Object.assign(new Error(),{code:'changed'}));await flush();
 assert.equal(h.alignments.length,2);assert.equal(h.run('alignLoading'),true);assert.equal(h.element('guide-align-cancel').hidden,false);assert.equal(h.element('guide-toggle').getAttribute('aria-busy'),'true');
 h.alignments[1].d.resolve({offset:0,frames:296,score:.9,margin:.2});await flush();assert.equal(h.run('earCanPlay'),true);h.run('invalidateGuide({disable:true})');
});

test('pagehide cancels alignment and returning to the page does not start microphone work without another click',async()=>{
 const h=harness();h.run('var op=beginGuide();ear.buffer={};referenceBuffer={};referenceSong="song-a"');
 const pending=h.run('prepareAlignment(op)').catch(error=>error);await flush();h.emit('window','pagehide');
 assert.equal(h.run('earEnabled'),false);assert.equal(h.run('alignLoading'),false);assert.ok(h.trace.includes('cancel'));assert.match(h.element('guide-status').textContent,/点开启继续/);
 h.alignments[0].d.resolve({offset:500,frames:296,score:.9,margin:.2});await pending;h.run('updateGuide()');assert.equal(h.alignments.length,1);assert.equal(h.run('earClock.adjustment'),0);assert.equal(h.run('earCanPlay'),false);
});

test('capture cleanup failure has a specific message and manual adjustment cannot bypass the capture gate',async()=>{
 const h=harness();h.run('var op=beginGuide();ear.buffer={};referenceBuffer={};referenceSong="song-a"');
 const pending=h.run('prepareAlignment(op)');await flush();h.run('ear.liveCapture=true');h.alignments[0].d.reject(Object.assign(new Error(),{code:'capture_release_failed'}));await pending;
 assert.equal(h.run('alignmentState.code'),'capture_release_failed');assert.match(h.element('guide-status').textContent,/麦克风尚未释放/);
 h.element('guide-advance').onclick();assert.equal(h.run('earClock.adjustment'),0);assert.equal(h.run('earCanPlay'),false);assert.match(h.element('guide-status').textContent,/关闭页面/);assert.equal(h.alignments.length,1);
 h.run('ear.liveCapture=false');h.element('guide-advance').onclick();assert.equal(h.run('earClock.adjustment'),50);assert.equal(h.run('earCanPlay'),true);assert.ok(h.trace.includes('manual'));assert.equal(h.alignments.length,1);h.run('invalidateGuide({disable:true})');
});

test('a hidden page cannot start a guide loader or a new automatic alignment',async()=>{
 const h=harness();h.context.document.hidden=true;await h.run('loadGuide()');assert.equal(h.trace.includes('start'),false);assert.equal(h.alignments.length,0);
 h.run('var op=beginGuide();ear.buffer={};referenceBuffer={};referenceSong="song-a";updateGuide()');await flush();assert.equal(h.alignments.length,0);h.emit('document','visibilitychange');assert.equal(h.run('earEnabled'),false);
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

test('microphone icon follows actual mute while music remains unconfirmed until the applied revision',()=>{
 const h=harness();h.run('controls={...controls,mic_muted:0,music_gain:0,control_revision:3};playback={...playback,mic_muted:true,applied_revision:2};updateMuteButtons()');
 assert.equal(h.element('mute-button').getAttribute('aria-pressed'),'true');
 assert.equal(h.element('music-mute-button').getAttribute('aria-pressed'),'mixed');
 assert.equal(h.element('music-mute-button').dataset.muted,'true');
 assert.match(h.element('music-mute-state').textContent,/等待电视确认/);
 h.run('playback.applied_revision=3;updateMuteButtons()');assert.equal(h.element('music-mute-button').getAttribute('aria-pressed'),'true');
 h.run('playback.mic_muted=false;updateMuteButtons()');assert.equal(h.element('mute-button').getAttribute('aria-pressed'),'false');
 assert.equal(h.element('mute-button').textContent,'');assert.equal(h.element('music-mute-button').textContent,'');
});

test('unavailable microphone keeps its explanation and cannot be unmuted from the icon',()=>{
 const h=harness();h.run('var writes=[];setControl=async value=>writes.push(value);micAvailable=false;playback.mic_muted=true;updateMuteButtons();toggleMic()');
 assert.equal(h.element('mute-button').disabled,true);assert.equal(h.run('writes.length'),0);
 assert.match(h.element('sound-status').textContent,/暂不可用/);
 // Muting an already active microphone remains available even when acquisition is unavailable.
 h.run('playback.mic_muted=false;updateMuteButtons();toggleMic()');
 assert.equal(h.element('mute-button').disabled,false);assert.equal(h.run('writes[0].mic_muted'),1);
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
 const h=harness();h.context.fetch=async()=>({ok:false,status:429,headers:{get:()=>"12"},json:async()=>({code:"rate_limited"})});
 const error=await h.run('actualApi("songs",{song_id:"1"})').catch(e=>e);
 assert.equal(error.code,'rate_limited');assert.equal(error.status,429);assert.equal(error.retryAfterMs,12000);
 h.context.fetch=async()=>({ok:false,status:503,headers:{get:()=>null},json:async()=>{throw Error('HTML gateway error');}});
 const unavailable=await h.run('actualApi("status")').catch(e=>e);assert.equal(unavailable.code,'service_unavailable');assert.equal(unavailable.status,503);
});

test('cancelling a guide control aborts its HTTP and lets a later mute leave the serialized queue',async()=>{
 const h=harness({validRoom:true}),requests=[];let rejectFirst;
 h.context.fetch=async(url,options)=>{
  const payload=JSON.parse(options.body);requests.push({payload,signal:options.signal});
  if(payload.original===0)return new Promise((resolve,reject)=>{rejectFirst=reject;options.signal.addEventListener('abort',()=>reject(Object.assign(new Error(),{name:'AbortError'})),{once:true});});
  return {ok:true,status:200,json:async()=>({revision:2})};
 };
 h.run('api=actualApi;setControl=actualSetControl;waitForTV=async()=>{};updatePlayer=()=>{};var aborter=new AbortController();controls.original=1;controls.mic_muted=0;');
 const first=h.run('setControl({original:0},{signal:aborter.signal,propagate:true})').catch(error=>error.code);
 const second=h.run('setControl({mic_muted:1})');
 try{
  await flush();assert.equal(requests.length,1);h.run('aborter.abort()');assert.equal(requests[0].signal.aborted,true);
  assert.equal(await first,'cancelled');await second;assert.equal(requests.length,2);assert.deepEqual(requests[1].payload,{mic_muted:1});
  assert.equal(h.run('controls.original'),1);assert.equal(h.run('controls.mic_muted'),1);
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
 await h.run('refresh()');assert.match(h.element('connection-status').textContent,/重新连接/);
 h.run('api=async()=>({mac_online:true,songs:[],playback:{online:true,current:"",played:[],state:"idle",mic_muted:true},controls:{},theme:"content"})');
 await h.run('refresh()');assert.equal(h.element('connection-status').hidden,true);assert.equal(h.element('connection-status').textContent,'');
 assert.equal(h.element('message').textContent,'这首的版本暂不可用');assert.equal(h.run('online'),true);assert.equal(h.element('queue-count').textContent,0);
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

test('secondary search paginates the selected source and preserves results through queue navigation',async()=>{
 const h=harness(),requests=[];h.context.searchApi=async(operation,body,query)=>{
  requests.push([operation,query]);return requests.length===1?{songs:[{source:'mv',song_id:'1',title:'首个MV'}],has_more:true,next_offset:30}:{songs:[{source:'mv',song_id:'1',title:'首个MV'},{source:'mv',song_id:'2',title:'第二个MV'}],has_more:false,next_offset:60};
 };
 h.run('mode="find";source="mv";api=searchApi;$("query").value="歌手"');await h.run('findSongs()');await h.run('findSongs(true)');
 assert.match(requests[0][1],/source=mv&offset=0/);assert.match(requests[1][1],/source=mv&offset=30/);assert.equal(h.run('songs.length'),2);
 h.run('setView("queue");setView("find")');assert.equal(h.run('songs.length'),2);assert.equal(h.element('query').value,'歌手');assert.equal(h.element('source-picker').hidden,false);
});

test('queue reordering after secondary navigation keeps the current and sung songs outside pending moves',()=>{
 const h=harness();h.run('selected=[{id:"current",state:"ready"},{id:"a",state:"ready"},{id:"b",state:"ready"},{id:"sung",state:"ready"}];playback.current="current";playback.played=["sung"];var ordered=null;saveOrder=ids=>{ordered=ids;};setView("queue");moveSong("b","next")');
 assert.deepEqual(Array.from(h.run('ordered')),['current','b','a','sung']);
 h.run('ordered=null;moveSong("sung","next");moveSong("current","next")');assert.equal(h.run('ordered'),null);
});


test('browser back restores the search secondary page without adding another history entry',()=>{
 const h=harness();let pushes=0;h.context.history.pushState=function(value){this.state=value;pushes++;};
 h.run('setView("find");source="cloud";$("query").value="保留云盘搜索";window.scrollY=120;setView("queue")');
 assert.equal(pushes,2);h.emit('window','popstate',{state:{ktvView:'find'}});
 assert.equal(h.run('mode'),'find');assert.equal(pushes,2);assert.equal(h.context.window.scrollY,120);assert.equal(h.element('query').value,'保留云盘搜索');assert.equal(h.element('source-picker').hidden,false);
});

test('emergency mute is one-way and requires actual mute plus matching TV revision',async()=>{
 const h=harness(),ack=deferred();let confirms,calls=[];h.context.muteApi=async(operation,value)=>{calls.push(value);return {revision:4};};h.context.muteAck=check=>{confirms=check;return ack.promise;};
 h.run('earEnabled=false;updateGuide=()=>{};updatePartyAudio=()=>{};setControl=actualSetControl;api=muteApi;waitForTV=muteAck;playback.mic_muted=false;playback.applied_revision=3;updateMuteButtons()');
 assert.equal(h.element('quick-mute-button').disabled,false);assert.equal(h.element('quick-mute-button').textContent,'麦克风静音');
 const pending=h.element('quick-mute-button').onclick();await flush();
 assert.deepEqual(calls.map(v=>({...v})),[{mic_muted:1}]);assert.equal(h.element('quick-mute-button').textContent,'静音待确认');assert.equal(h.element('quick-mute-button').disabled,true);assert.match(h.element('quick-mute-status').textContent,/等待电视确认/);
 await h.element('quick-mute-button').onclick();assert.equal(calls.length,1);
 h.run('playback.applied_revision=4');assert.equal(confirms(),false);
 h.run('playback.mic_muted=true;playback.applied_revision=3');assert.equal(confirms(),false);
 h.run('playback.applied_revision=4');assert.equal(confirms(),true);ack.resolve();await pending;
 assert.equal(h.element('quick-mute-button').textContent,'麦克风已静音');assert.equal(h.element('quick-mute-button').disabled,true);assert.match(h.element('quick-mute-status').textContent,/电视已确认/);
 assert.equal(h.run('controls.music_gain'),70);assert.equal(h.run('controls.mic_gain'),50);
});

test('emergency mute explains offline or unknown microphone state and reports failure without claiming silence',async()=>{
 const h=harness();h.run('earEnabled=false;playback={...playback,online:false,mic_muted:false};var muteWrites=[];setControl=async v=>muteWrites.push(v);updateMuteButtons()');
 assert.equal(h.element('quick-mute-button').disabled,true);assert.match(h.element('quick-mute-status').textContent,/未连接/);await h.element('quick-mute-button').onclick();assert.equal(h.run('muteWrites.length'),0);
 h.run('playback.online=true;delete playback.mic_muted;updateMuteButtons()');assert.equal(h.element('quick-mute-button').disabled,false);assert.match(h.element('quick-mute-status').textContent,/状态未确认/);
 h.run('setControl=async()=>{throw new Error("unconfirmed")};');await h.element('quick-mute-button').onclick();assert.equal(h.element('quick-mute-button').textContent,'麦克风静音');assert.match(h.element('quick-mute-status').textContent,/静音尚未确认/);assert.doesNotMatch(h.element('quick-mute-status').textContent,/电视已确认/);
});

test('late microphone slider writes never undo emergency mute or reset the selected gain',async()=>{
 const h=harness(),writes=[];h.context.muteApi=async(operation,value)=>{writes.push({...value});return {revision:writes.length};};
 h.run('earEnabled=false;updateGuide=()=>{};updatePartyAudio=()=>{};setControl=actualSetControl;api=muteApi;waitForTV=async()=>{};playback.mic_muted=false;changeGain("mic","mic_gain",80,20);updateMuteButtons()');
 await h.element('quick-mute-button').onclick();await new Promise(resolve=>setTimeout(resolve,40));await flush();
 assert.deepEqual(writes,[{mic_muted:1},{mic_gain:80}]);assert.equal(h.run('controls.mic_muted'),1);assert.equal(h.run('controls.mic_gain'),80);assert.equal(h.run('controls.music_gain'),70);
});

test('emergency mute remains available to supersede an earlier unmute awaiting TV confirmation',async()=>{
 const h=harness(),unmuteAck=deferred(),muteAck=deferred(),writes=[];h.context.emergencyApi=async(operation,value)=>{writes.push({...value});return {revision:writes.length};};h.context.emergencyWait=()=>writes.length===1?unmuteAck.promise:muteAck.promise;
 h.run('earEnabled=false;updateGuide=()=>{};updatePartyAudio=()=>{};setControl=actualSetControl;api=emergencyApi;waitForTV=emergencyWait;playback.mic_muted=true;playback.applied_revision=0');
 const prior=h.run('setControl({mic_muted:0})');await flush();h.run('updateMuteButtons()');assert.equal(h.element('quick-mute-button').disabled,false);
 const emergency=h.element('quick-mute-button').onclick();await flush();assert.deepEqual(writes,[{mic_muted:0},{mic_muted:1}]);
 h.run('playback.mic_muted=true;playback.applied_revision=2');muteAck.resolve();await emergency;unmuteAck.resolve();await prior;
 assert.equal(h.run('controls.mic_muted'),1);assert.equal(h.element('quick-mute-button').textContent,'麦克风已静音');
});

test('phone presents protection preparation, attenuation and latch without changing saved gains',()=>{
 const h=harness();h.run('earEnabled=false;controls.mic_gain=70;playback.audio_capabilities={vocal_presets:["dry"],sfx:true,voice_protection:{state:"preparing",effective_gain:0}};updatePlayer()');
 assert.equal(h.element('player-status').textContent,'正在准备语音保护');
 h.run('playback.audio_capabilities.voice_protection={state:"attenuated",effective_gain:26};updatePlayer()');
 assert.match(h.element('player-status').textContent,/已下调人声设定/);assert.equal(h.run('controls.mic_gain'),70);
 h.run('playback.safety_latched=true;playback.mic_muted=true;updatePlayer()');
 assert.match(h.element('player-status').textContent,/需手动恢复/);assert.equal(h.run('controls.mic_gain'),70);
});

test('local mute intent and revision cannot acknowledge an unconfirmed Guard mute',async()=>{
 const h=harness();h.run('earEnabled=false;playback.applied_revision=5;playback.mic_muted=true;playback.audio_capabilities={vocal_presets:["dry"],sfx:true,voice_protection:{state:"muted",effective_gain:70,mute_confirmed:false}};updateMuteButtons()');
 assert.equal(h.element('mute-button').getAttribute('aria-pressed'),'mixed');assert.equal(h.element('quick-mute-button').disabled,false);
 h.run('playback.safety_latched=true;updatePlayer()');assert.match(h.element('player-status').textContent,/正在静音/);
 h.run('playback.audio_capabilities.voice_protection.mute_confirmed=true;updateMuteButtons()');
 assert.equal(h.element('mute-button').getAttribute('aria-pressed'),'true');assert.equal(h.element('quick-mute-button').disabled,true);
});

test('queue progress changes replace only the changed row and retain stable groups, focus and history disclosure',()=>{
 const h=harness();h.run('earEnabled=false;mode="queue";playback.current="";playback.played=["old"];selected=[{id:"a",song_id:"1",title:"下载",state:"acquiring",progress:{source_bytes:10}},{id:"b",song_id:"2",title:"已就绪",state:"ready"},{id:"old",song_id:"3",title:"已唱",state:"ready"}];render()');
 const root=h.element('results'),pending=root.children[0],history=root.children[1],a=pending.children[1],b=pending.children[2];history.open=true;const focused=b.children.at(-1).children[0];focused.focus();const insertions=[];const insert=pending.insertBefore.bind(pending);pending.insertBefore=(node,before)=>{insertions.push(node);insert(node,before);};
 h.run('selected[0]={...selected[0],progress:{source_bytes:20}};render()');assert.equal(root.children[0],pending);assert.equal(root.children[1],history);assert.equal(history.open,true);assert.notEqual(pending.children[1],a);assert.equal(pending.children[2],b);assert.equal(h.context.document.activeElement,focused);assert.equal(insertions.includes(b),false,'an unchanged row is not detached/moved just because its sibling changes');
});

test('IME search waits for composition and debounces the committed text once',async()=>{
 const h=harness(),queries=[];h.context.searchApi=async(operation,body,query)=>{queries.push(query);return {songs:[],has_more:false};};h.run('earEnabled=false;mode="find";api=searchApi;$("query").oncompositionstart();$("query").value="zhou";$("query").oninput({isComposing:true})');
 await new Promise(resolve=>setTimeout(resolve,280));assert.equal(queries.length,0);
 h.run('$("query").value="周杰伦";$("query").oncompositionend();$("query").oninput({isComposing:false})');await new Promise(resolve=>setTimeout(resolve,280));assert.equal(queries.length,1);assert.match(queries[0],/q=%E5%91%A8%E6%9D%B0%E4%BC%A6/);
});

test('typing cancels the old search fetch immediately while independent controls still leave',async()=>{
 const h=harness(),requests=[];h.context.fetch=async(url,options)=>{
  requests.push({url,options});if(url.includes('/search')&&url.includes('q=old'))return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Object.assign(new Error(),{name:'AbortError'})),{once:true}));
  return {ok:true,status:200,json:async()=>url.includes('/controls')?{revision:1}:{songs:[{song_id:'2',title:'new'}],has_more:false}};
 };
 h.run('earEnabled=false;mode="find";api=actualApi;setControl=actualSetControl;waitForTV=async()=>{};$("query").value="old"');const first=h.run('findSongs()');await flush();
 await h.run('setControl({mic_muted:1})');assert.equal(requests.some(r=>r.url.includes('/controls')),true);
 h.run('$("query").value="new";$("query").oninput({isComposing:false})');assert.equal(requests[0].options.signal.aborted,true);await first;await new Promise(resolve=>setTimeout(resolve,280));assert.equal(h.run('songs[0].title'),'new');assert.equal(requests.filter(r=>r.url.includes('/search')).length,2);
});

test('original decoded duration mismatch prevents alignment or speaker output',async()=>{
 const h=harness();h.run('playback.guide.duration_ms=5150;verifiedGuide=async()=>new Uint8Array(2048).buffer');await h.run('loadGuide()');assert.equal(h.run('earEnabled'),false);assert.equal(h.run('earCanPlay'),false);assert.equal(h.alignments.length,0);assert.equal(h.run('ear.buffer'),null);
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
