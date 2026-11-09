const test=require('node:test'),assert=require('node:assert/strict');
const {FridayGuideClock}=require('../integrations/ktv_public/web/guide.js');
test('guide follows server clock, pause and a user offset without depending on phone wall time',()=>{const clock=new FridayGuideClock();const p={online:true,current:'synthetic',state:'playing',position_ms:20000,position_at:1000000};clock.observe({server_time_ms:1000100,playback:p},1000,1200);assert.equal(clock.target(2100),21.1);clock.adjustment=-200;assert.ok(Math.abs(clock.target(2100)-20.9)<1e-10);clock.playback={...p,state:'paused'};assert.equal(clock.target(10000),19.8);clock.playback={...p,current:''};assert.equal(clock.target(2000),null);});
test('slow status round trips do not disturb an established clock estimate',()=>{const clock=new FridayGuideClock();const p={online:true,current:'synthetic',state:'playing',position_ms:0,position_at:1000000};clock.observe({server_time_ms:1000000,playback:p},1000,1020);const offset=clock.offset;clock.observe({server_time_ms:1001000,playback:p},2000,2800);assert.equal(clock.offset,offset);});
test('mobile guide starts a user-activated playback session and clears the old buffer before loading another track',async()=>{const {FridayEarMonitor}=require('../integrations/ktv_public/web/guide.js');const saved={window:global.window,Audio:global.Audio,document:global.document};const nav=Object.getOwnPropertyDescriptor(global,'navigator');let primed=false;class Audio{setAttribute(){}play(){primed=true;return Promise.resolve();}pause(){primed=false;}}class Context{constructor(){this.currentTime=1;this.baseLatency=.01;this.state='suspended';}resume(){this.state='running';return Promise.resolve();}decodeAudioData(){return Promise.resolve({duration:60});}createBufferSource(){return {connect(){},disconnect(){},start(){},stop(){},playbackRate:{setValueAtTime(){}}};}createGain(){return {connect(){},disconnect(){},gain:{setValueAtTime(){},linearRampToValueAtTime(){},setTargetAtTime(){}}};}}global.window={AudioContext:Context};global.Audio=Audio;global.document={hidden:false};Object.defineProperty(global,'navigator',{value:{audioSession:{type:'auto'}},configurable:true});try{const c={playback:{online:true,current:'a',state:'playing'},target:()=>10};const monitor=new FridayEarMonitor(c,()=>{});await monitor.start(async()=>new ArrayBuffer(10));assert.equal(navigator.audioSession.type,'playback');assert.equal(primed,true);assert.equal(monitor.stats().node,true);let finish;const pending=monitor.start(()=>new Promise(r=>{finish=r;}));assert.equal(monitor.buffer,null);await new Promise(r=>setImmediate(r));finish(new ArrayBuffer(10));await pending;monitor.stop();assert.equal(primed,false);assert.equal(monitor.buffer,null);}finally{Object.assign(global,saved);if(nav)Object.defineProperty(global,'navigator',nav);else delete global.navigator;}});


test('initial guide loading remains silent until acoustic alignment releases playback',async()=>{
 const {FridayEarMonitor}=require('../integrations/ktv_public/web/guide.js');
 const previous={navigator:global.navigator,Audio:global.Audio,window:global.window};
 Object.defineProperty(global,'navigator',{configurable:true,value:{}});global.Audio=class{setAttribute(){}play(){return Promise.resolve();}};
 let starts=0;const buffer={duration:20};
 global.window={AudioContext:class{constructor(){this.currentTime=0;this.state='running';}resume(){return Promise.resolve();}decodeAudioData(){return Promise.resolve(buffer);}}};
 try{const ear=new FridayEarMonitor({},()=>{});ear.sync=()=>starts++;await ear.start(async()=>new ArrayBuffer(0),{deferPlayback:true});assert.equal(starts,0);assert.equal(ear.buffer,buffer);ear.sync(true);assert.equal(starts,1);}
 finally{Object.defineProperty(global,'navigator',{configurable:true,value:previous.navigator});global.Audio=previous.Audio;global.window=previous.window;}
});

test('new status reports preserve the historical capture time on the same song timeline',()=>{
 const clock=new FridayGuideClock();
 clock.observe({server_time_ms:1000000,playback:{online:true,current:'song',state:'playing',position_ms:20000,position_at:1000000}},1000,1000);
 const first=clock.unadjusted(1500);
 clock.observe({server_time_ms:1006000,playback:{online:true,current:'song',state:'playing',position_ms:26000,position_at:1006000}},7000,7000);
 assert.equal(first,20.5);assert.equal(clock.unadjusted(1500),first);assert.equal(clock.generation,0);
});
test('old responses cannot rewind playback, including equal-server-time requests returned out of order',()=>{
 const clock=new FridayGuideClock(),base={online:true,current:'new',state:'playing',position_ms:2000,position_at:1001000};
 assert.equal(clock.observe({server_time_ms:1001000,playback:base},2000,2020),true);
 assert.equal(clock.observe({server_time_ms:1000000,playback:{...base,current:'old'}},1000,2100),false);
 assert.equal(clock.observe({server_time_ms:1001000,playback:{...base,current:'old'}},1900,2200),false);
 assert.equal(clock.playback.current,'new');assert.equal(clock.generation,0);
});
test('a same-song restart invalidates calibration, ordinary continuous position reports do not',()=>{
 const clock=new FridayGuideClock(),p={online:true,current:'song',state:'playing',position_ms:20000,position_at:1000000};
 clock.observe({server_time_ms:1000000,playback:p},1000,1000);
 clock.observe({server_time_ms:1001000,playback:{...p,position_ms:21000,position_at:1001000}},2000,2000);
 assert.equal(clock.generation,0);
 clock.observe({server_time_ms:1002000,playback:{...p,position_ms:0,position_at:1002000}},3000,3000);
 assert.equal(clock.generation,1);
 clock.observe({server_time_ms:1003000,playback:{...p,state:'paused',position_ms:1000,position_at:1003000}},4000,4000);
 assert.equal(clock.generation,1,'continuous pause preserves the restarted timeline');
});
test('manual negative adjustment clamps output without destroying the unadjusted time',()=>{
 const clock=new FridayGuideClock();clock.adjustment=-400;
 clock.observe({server_time_ms:1000000,playback:{online:true,current:'song',state:'playing',position_ms:100,position_at:1000000}},1000,1000);
 assert.equal(clock.target(1000),0);assert.equal(clock.unadjusted(1000),.1);
});

function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
function fakeAudio(t,{resume,decode,primerPlay}={}){
 const saved=new Map(['navigator','Audio','window','document'].map(name=>[name,Object.getOwnPropertyDescriptor(globalThis,name)])),events=[];
 const context={currentTime:1,state:'suspended',baseLatency:.01,
  resume(){events.push('resume');if(resume)return resume(this);this.state='running';return Promise.resolve();},
  decodeAudioData(bytes){events.push('decode');return decode?decode(bytes):Promise.resolve({duration:60});},
  createBufferSource(){return {connect(){},disconnect(){},start(){events.push('node.start');},stop(){events.push('node.stop');},playbackRate:{setValueAtTime(){}}};},
  createGain(){return {connect(){},disconnect(){},gain:{setValueAtTime(){},linearRampToValueAtTime(){},setTargetAtTime(){}}};}
 };
 for(const [name,value] of Object.entries({navigator:{audioSession:{type:'auto'}},document:{hidden:false},window:{AudioContext:class{constructor(){return context;}}},Audio:class{setAttribute(){}play(){events.push('primer.play');return primerPlay?primerPlay():Promise.resolve();}pause(){events.push('primer.pause');}}}))Object.defineProperty(globalThis,name,{configurable:true,writable:true,value});
 t.after(()=>{for(const [name,descriptor] of saved){if(descriptor)Object.defineProperty(globalThis,name,descriptor);else delete globalThis[name];}});
 const {FridayEarMonitor}=require('../integrations/ktv_public/web/guide.js');const states=[];
 const ear=new FridayEarMonitor({playback:{online:true,current:'song',state:'playing'},target:()=>10},state=>states.push(state));
 return {ear,context,events,states};
}
test('stopping while browser activation is pending rejects immediately and never invokes the loader',async t=>{
 const resumed=deferred(),{ear,events,states,context}=fakeAudio(t,{resume:()=>resumed.promise});let loaded=0;
 const start=ear.start(()=>{loaded++;return Promise.resolve(new ArrayBuffer(1));});const rejected=assert.rejects(start,{code:'cancelled'});
 assert.deepEqual(events,['primer.play','resume']);ear.stop();await rejected;
 context.state='running';resumed.resolve();await new Promise(r=>setImmediate(r));
 assert.equal(loaded,0);assert.equal(ear.buffer,null);assert.deepEqual(states,['start','off']);
});
test('new loads abort old fetches and late old decode completion cannot replace the current track',async t=>{
 const decoded=deferred();let decodes=0;const {ear}=fakeAudio(t,{decode:()=>++decodes===1?decoded.promise:Promise.resolve({duration:90})});
 let oldSignal;const first=ear.start(async signal=>{oldSignal=signal;return new ArrayBuffer(1);});const rejected=assert.rejects(first,{code:'cancelled'});
 await new Promise(r=>setImmediate(r));const second=ear.start(async()=>new ArrayBuffer(2));await rejected;await second;
 assert.equal(oldSignal.aborted,true);assert.equal(ear.buffer.duration,90);decoded.resolve({duration:60});await new Promise(r=>setImmediate(r));assert.equal(ear.buffer.duration,90);ear.stop();
});
test('a resolved but still suspended AudioContext is not reported as playing or used to load media',async t=>{
 const {ear,states}=fakeAudio(t,{resume:()=>Promise.resolve()});let loaded=0;
 await assert.rejects(ear.start(async()=>{loaded++;return new ArrayBuffer(1);}),{code:'audio_start_blocked'});
 assert.equal(loaded,0);assert.equal(states.includes('playing'),false);assert.equal(ear.node,null);
});
test('a direct retry activates the existing playback context before yielding and does not start the original',async t=>{
 const {ear,events}=fakeAudio(t);await ear.start(async()=>new ArrayBuffer(1),{deferPlayback:true});events.length=0;
 const resumed=ear.resumePlayback();assert.deepEqual(events,['primer.play','resume']);await resumed;assert.equal(ear.node,null);ear.stop();
});
test('a paused timeline cannot be reported as playing after a completed media download',async t=>{
 const {ear,states}=fakeAudio(t);ear.clock.playback.state='paused';await ear.start(async()=>new ArrayBuffer(1));assert.equal(ear.node,null);assert.equal(states.includes('playing'),false);ear.stop();
});
test('a deferred guide cannot start before alignment and becomes silent again on same-song restart',async t=>{
 const {ear}=fakeAudio(t);ear.clock.generation=0;
 await ear.start(async()=>new ArrayBuffer(1),{deferPlayback:true});assert.equal(ear.needsAlignment(),true);assert.equal(ear.sync(true),false);
 ear.setManualAdjustment(50);assert.equal(ear.needsAlignment(),false);assert.equal(ear.sync(true),true);
 ear.clock.generation++;assert.equal(ear.needsAlignment(),true);assert.equal(ear.sync(true),false);assert.equal(ear.node,null);ear.stop();
});
test('a delayed primer play completion cannot mark calibration as playing',async t=>{
 const primer=deferred(),{ear,events}=fakeAudio(t,{primerPlay:()=>primer.promise});
 await ear.start(async()=>new ArrayBuffer(1),{deferPlayback:true});const owner={cancel(){}};ear.beginCalibration(owner);
 assert.equal(events.at(-1),'primer.pause');primer.resolve();await Promise.resolve();assert.equal(ear.primed,false);
 await assert.rejects(ear.resumePlayback(),{code:'busy'});assert.equal(events.filter(e=>e==='primer.play').length,1);
 ear.calibrationOwner=null;ear.stop();
});


test('calibrated pause and resume keep alignment and resume without another microphone session',async t=>{
 const {ear,events}=fakeAudio(t);let now=1000;const descriptor=Object.getOwnPropertyDescriptor(globalThis,'performance');Object.defineProperty(globalThis,'performance',{configurable:true,value:{now:()=>now}});t.after(()=>Object.defineProperty(globalThis,'performance',descriptor));
 const clock=new FridayGuideClock(),p={online:true,current:'song',state:'playing',position_ms:20000,position_at:1000000};ear.clock=clock;
 clock.observe({server_time_ms:1000000,playback:p},now,now);await ear.start(async()=>new ArrayBuffer(1),{deferPlayback:true});ear.setManualAdjustment(-200);assert.equal(ear.sync(true),true);
 now=2000;clock.observe({server_time_ms:1001000,playback:{...p,state:'paused',position_ms:20300,position_at:1001000}},now,now);
 assert.equal(clock.generation,0);assert.equal(ear.needsAlignment(),false);assert.equal(ear.sync(),false);
 now=3000;clock.observe({server_time_ms:1002000,playback:{...p,position_ms:20500,position_at:1002000}},now,now);
 assert.equal(clock.generation,0);assert.equal(clock.adjustment,-200);assert.equal(ear.needsAlignment(),false);assert.equal(ear.sync(),true);assert.equal(events.filter(v=>v==='node.start').length,2);ear.stop();
});

test('real seeks while paused and same-song media replacement invalidate calibration',()=>{
 const clock=new FridayGuideClock(),p={online:true,current:'song',state:'playing',position_ms:20000,position_at:1000000,guide:{sha256:'a',reference:{sha256:'b'}}};
 clock.observe({server_time_ms:1000000,playback:p},1000,1000);
 clock.observe({server_time_ms:1001000,playback:{...p,state:'paused',position_ms:0,position_at:1001000}},2000,2000);assert.equal(clock.generation,1);
 clock.observe({server_time_ms:1002000,playback:{...p,state:'paused',position_ms:0,position_at:1002000,guide:{sha256:'c',reference:{sha256:'b'}}}},3000,3000);assert.equal(clock.generation,2);
});

test('a route latency jump reschedules immediately instead of spending seconds at a 2 percent correction',async t=>{
 const {ear,context}=fakeAudio(t);const starts=[];context.outputLatency=.04;context.createBufferSource=()=>({connect(){},disconnect(){},start(...args){starts.push(args);},stop(){},playbackRate:{setValueAtTime(){}}});
 await ear.start(async()=>new ArrayBuffer(1));assert.equal(starts.length,1);assert.ok(Math.abs(starts[0][1]-10.06)<1e-10);
 context.outputLatency=.14;assert.equal(ear.sync(),true);assert.equal(starts.length,2);assert.ok(Math.abs(starts[1][1]-10.16)<1e-10);ear.stop();
});

test('near EOF compensated future playback never replays the clamped final fragment',async t=>{
 const {ear,context,events}=fakeAudio(t);await ear.start(async()=>new ArrayBuffer(1),{deferPlayback:true});ear.buffer={duration:10.05};context.outputLatency=.1;
 for(let i=0;i<3;i++)assert.equal(ear.sync(),false);assert.equal(events.filter(v=>v==='node.start').length,0);ear.stop();
});


test('explicit playback epochs catch short same-song replays and reject retired timeline reports',()=>{
 const c=new FridayGuideClock(),p={online:true,current:'same',state:'playing',position_ms:300,position_at:1000000,playback_epoch:'first-uuid'};
 c.observe({server_time_ms:1000000,playback:p},1000,1000);
 c.observe({server_time_ms:1000100,playback:{...p,position_ms:0,position_at:1000100,playback_epoch:'second-uuid'}},1100,1100);assert.equal(c.generation,1,'sub-750ms replay still invalidates alignment');
 assert.equal(c.observe({server_time_ms:1000200,playback:{...p,position_ms:500,position_at:1000200}},1200,1200),false);assert.equal(c.playback.playback_epoch,'second-uuid');
 const paused={...c.playback,state:'paused',position_ms:50,position_at:1000300};c.observe({server_time_ms:1000300,playback:paused},1300,1300);assert.equal(c.generation,1);
 c.observe({server_time_ms:1000400,playback:{...paused,state:'playing',position_ms:100,position_at:1000400}},1400,1400);assert.equal(c.generation,1);
});

test('an epoch appearing or disappearing invalidates prior calibration while legacy streams retain position heuristics',()=>{
 const c=new FridayGuideClock(),p={online:true,current:'song',state:'playing',position_ms:10000,position_at:1000000};c.observe({server_time_ms:1000000,playback:p},1000,1000);
 c.observe({server_time_ms:1000100,playback:{...p,position_ms:10100,position_at:1000100,playback_epoch:'new-uuid'}},1100,1100);assert.equal(c.generation,1);
 c.observe({server_time_ms:1000200,playback:{...p,position_ms:10200,position_at:1000200}},1200,1200);assert.equal(c.generation,2);
});

test('event timelines never calibrate using long-poll waiting time; independent short probes still calibrate',()=>{
 const c=new FridayGuideClock(),p={online:true,current:'song',state:'playing',position_ms:0,position_at:1000000,playback_epoch:'one'};
 c.observe({server_time_ms:1000000,playback:p},1000,1020);const estimate=[c.offset,c.best,c.receipt];
 c.observe({server_time_ms:1005000,playback:{...p,position_ms:0,position_at:1005000,playback_epoch:'two'}},1100,6100,{calibrate:false});assert.deepEqual([c.offset,c.best,c.receipt],estimate);assert.equal(c.generation,1);
 c.observe({server_time_ms:1005100,playback:{...p,position_ms:100,position_at:1005100,playback_epoch:'two'}},6100,6110);assert.equal(c.best,10);assert.equal(c.receipt,6110);
});
