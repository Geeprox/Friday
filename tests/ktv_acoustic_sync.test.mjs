import test from 'node:test';
import assert from 'node:assert/strict';
import {referenceFeatures,matchFeatures,referenceFeaturesAsync,matchFeaturesAsync} from '../integrations/ktv_public/web/sync-dsp.mjs';
import {FridayAcousticSync,alignmentOffset} from '../integrations/ktv_public/web/sync.mjs';
import {FridayEarMonitor} from '../integrations/ktv_public/web/guide.js';
function synthetic(seed,seconds=24){const rate=8000,data=new Float32Array(rate*seconds);let x=seed,held=0;const random=()=>{x=(x*1664525+1013904223)>>>0;return x/4294967296;};let notes=[];for(let i=0;i<data.length;i++){if(i%1600===0){held=.1+random()*.3;notes=Array.from({length:6},()=>100+random()*3400);}data[i]=held*notes.reduce((sum,f,k)=>sum+Math.sin(i*2*Math.PI*f/rate)/(k+1),0)/3;}return {sampleRate:rate,length:data.length,duration:seconds,numberOfChannels:1,getChannelData:()=>data};}
test('acoustic spectral match finds a delayed room excerpt despite gain, noise and reflected sound',()=>{const source=synthetic(12);const start=7.38,rate=8000,input=source.getChannelData(0),data=new Float32Array(6*rate);let seed=7;for(let i=0;i<data.length;i++){seed=(seed*1664525+1013904223)>>>0;const at=Math.round(start*rate)+i;data[i]=input[at]*.3+(i>320?input[at-320]*.08:0)+(seed/4294967296-.5)*.003;}const heard={sampleRate:rate,length:data.length,duration:6,numberOfChannels:1,getChannelData:()=>data};const reference=referenceFeatures(source,0,24),recorded=referenceFeatures(heard,0,6),result=matchFeatures(reference,recorded);assert.equal(result.ok,true,JSON.stringify(result));assert.ok(Math.abs(result.time-(start+.032))<=.025,JSON.stringify(result));});
test('silence and an unrelated song never overwrite an existing calibration',()=>{const source=synthetic(12),other=synthetic(79,6);const reference=referenceFeatures(source,0,24);assert.equal(matchFeatures(reference,referenceFeatures(other,0,6)).ok,false);const silent={sampleRate:8000,length:48000,duration:6,numberOfChannels:1,getChannelData:()=>new Float32Array(48000)};assert.equal(matchFeatures(reference,referenceFeatures(silent,0,6)).reason,'quiet');});
test('repeated music is refused when two placements are equally plausible',()=>{const part=synthetic(12,6),data=new Float32Array(18*8000);for(let i=0;i<3;i++)data.set(part.getChannelData(0),i*48000);const repeated={sampleRate:8000,length:data.length,duration:18,numberOfChannels:1,getChannelData:()=>data};assert.equal(matchFeatures(referenceFeatures(repeated,0,18),referenceFeatures(part,0,6)).ok,false);});
test('cancelling a pending microphone grant stops a late stream without starting capture',async()=>{const saved=Object.getOwnPropertyDescriptor(globalThis,'navigator');let grant,stopped=0,requested=0;Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getUserMedia:()=>{requested++;return new Promise(resolve=>{grant=resolve;});}}}});try{const sync=new FridayAcousticSync();assert.equal(requested,0);const result=sync.run({version:1,context:{state:'running',audioWorklet:{},resume:()=>Promise.resolve()},buffer:{},halt:()=>{},sync:()=>{}},{playback:{online:true,current:'synthetic',state:'playing'},adjustment:0,generation:0,unadjusted:()=>10},()=>{},()=>{},{referenceBuffer:synthetic(12),referenceSong:'synthetic'}).catch(e=>e.code);sync.cancel();grant({getTracks:()=>[{stop:()=>stopped++}]});assert.equal(await result,'cancelled');await Promise.resolve();assert.equal(stopped,1);}finally{if(saved)Object.defineProperty(globalThis,'navigator',saved);else delete globalThis.navigator;}});

test('phone original is stopped before permission is requested and stays silent on rejection',async()=>{
 const saved=Object.getOwnPropertyDescriptor(globalThis,'navigator'),events=[];let resumes=0;
 Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getUserMedia:()=>{events.push('request');return Promise.reject(Object.assign(new Error('denied'),{name:'NotAllowedError'}));}}}});
 try{const ear={version:1,context:{state:'running',audioWorklet:{},resume:()=>Promise.resolve()},buffer:{},halt:()=>events.push('halt'),sync:()=>resumes++};
  await assert.rejects(new FridayAcousticSync().run(ear,{playback:{online:true,current:'synthetic',state:'playing'},adjustment:0,generation:0,unadjusted:()=>10},()=>{},()=>{},{referenceBuffer:synthetic(12),referenceSong:'synthetic'}),{name:'NotAllowedError'});
  assert.deepEqual(events,['halt','request']);assert.equal(resumes,0);assert.equal(ear.calibrating,false);
 }finally{if(saved)Object.defineProperty(globalThis,'navigator',saved);else delete globalThis.navigator;}
});

function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
function audioBuffer(data,rate=8000){return {sampleRate:rate,length:data.length,duration:data.length/rate,numberOfChannels:1,getChannelData:()=>data};}
function excerpt(source,start,seconds=6){return audioBuffer(source.getChannelData(0).slice(Math.round(start*source.sampleRate),Math.round((start+seconds)*source.sampleRate)),source.sampleRate);}
function browserFixture(t,{permission,resume,addModule,onConnect,original,clock:providedClock,realEar=false,trackStop}={}){
 const names=['navigator','AudioWorkletNode','document','performance','window'],saved=new Map(names.map(name=>[name,Object.getOwnPropertyDescriptor(globalThis,name)]));
 const listeners=new Map();
 const target=name=>({addEventListener(type,callback){const key=name+':'+type;if(!listeners.has(key))listeners.set(key,new Set());listeners.get(key).add(callback);},removeEventListener(type,callback){listeners.get(name+':'+type)?.delete(callback);}});
 const emit=(name,type)=>{for(const callback of [...(listeners.get(name+':'+type)||[])])callback();};
 const events=[],state={now:10000,stops:0,requests:0},source=synthetic(12),track={readyState:'live',stop(){state.stops++;events.push('track.stop');if(trackStop)trackStop(this);else this.readyState='ended';},getSettings(){return {latency:0};}},stream={getTracks:()=>[track],getAudioTracks:()=>[track]};
 const clock=providedClock||{playback:{online:true,current:'song',state:'playing'},generation:0,adjustment:0,unadjusted:time=>7.78+(time-10000)/1000};
 const context={...target('context'),state:'running',currentTime:10,destination:{},
  resume(){events.push('resume');return resume?resume():Promise.resolve();},
  audioWorklet:{addModule(){events.push('module');return addModule?addModule():Promise.resolve();}},
  createMediaStreamSource(){events.push('input');return {connect(){events.push('input.connect');},disconnect(){events.push('input.disconnect');}};},
  createBufferSource(){return {connect(){events.push('guide.connect');},disconnect(){events.push('guide.disconnect');},start(){events.push('guide.start');},stop(){events.push('guide.stop');},playbackRate:{setValueAtTime(){}}};},
  createGain(){return {connect(){},disconnect(){events.push('gain.disconnect');},gain:{setValueAtTime(){},linearRampToValueAtTime(){},setTargetAtTime(){}}};}
 };
 const ear=realEar?new FridayEarMonitor(clock,state=>events.push(state)):{halt(){events.push('halt');},sync(){events.push('play-original');}};
 Object.assign(ear,{context,version:1,buffer:original||synthetic(79)});
 if(realEar){ear.requireAlignment=true;ear.unlock={pause(){events.push('primer.pause');}};clock.target=()=>Math.max(0,clock.unadjusted(state.now)+clock.adjustment/1000);}
 const values={navigator:{audioSession:{type:'playback'},mediaDevices:{getUserMedia(){state.requests++;events.push('permission');return permission?permission():Promise.resolve(stream);}}},document:{...target('document'),hidden:false},window:target('window'),performance:{now:()=>state.now},AudioWorkletNode:class{
  constructor(){events.push('node');this.port={onmessage:null,postMessage(value){events.push(value);}};}
  connect(){events.push('capture');if(onConnect)queueMicrotask(()=>onConnect(this));}
  disconnect(){events.push('node.disconnect');}
 }};
 for(const [name,value] of Object.entries(values))Object.defineProperty(globalThis,name,{configurable:true,writable:true,value});
 t.after(()=>{for(const [name,descriptor] of saved){if(descriptor)Object.defineProperty(globalThis,name,descriptor);else delete globalThis[name];}});
 return {ear,clock,source,stream,track,state,events,context,emit,listeners,options:{referenceBuffer:source,referenceSong:'song'}};
}

test('a missing accompaniment reference fails before requesting a microphone and never substitutes the original',async t=>{
 const f=browserFixture(t);await assert.rejects(new FridayAcousticSync().run(f.ear,f.clock),{code:'reference_unavailable'});assert.equal(f.state.requests,0);
 await assert.rejects(new FridayAcousticSync().run(f.ear,f.clock,()=>{},()=>{},{...f.options,referenceSong:'other'}),{code:'reference_unavailable'});assert.equal(f.state.requests,0);
});
test('cancel interrupts a never-resolving context resume and promptly releases its granted input',async t=>{
 const entered=deferred(),hanging=deferred(),f=browserFixture(t,{resume:()=>{entered.resolve();return hanging.promise;}}),sync=new FridayAcousticSync();
 const run=sync.run(f.ear,f.clock,()=>{},()=>{},f.options),rejected=assert.rejects(run,{code:'cancelled'});await entered.promise;sync.cancel();await rejected;
 assert.equal(f.state.stops,1);assert.equal(f.events.includes('module'),false);assert.equal(f.ear.calibrating,false);assert.equal(f.events.filter(x=>x==='resume').length,1);
 hanging.resolve();await Promise.resolve();assert.equal(f.events.includes('input'),false);
});
test('cancel interrupts a never-resolving worklet load without starting a node or waiting in cleanup',async t=>{
 const entered=deferred(),hanging=deferred(),f=browserFixture(t,{addModule:()=>{entered.resolve();return hanging.promise;}}),sync=new FridayAcousticSync();
 const run=sync.run(f.ear,f.clock,()=>{},()=>{},f.options),rejected=assert.rejects(run,{code:'cancelled'});await entered.promise;sync.cancel();await rejected;
 assert.equal(f.state.stops,1);assert.equal(f.events.includes('input'),false);assert.equal(f.events.filter(x=>x==='resume').length,1);hanging.resolve();
});
test('a second concurrent alignment cannot replace the first session or acquire another input',async t=>{
 const grant=deferred(),f=browserFixture(t,{permission:()=>grant.promise}),sync=new FridayAcousticSync();
 const run=sync.run(f.ear,f.clock,()=>{},()=>{},f.options),rejected=assert.rejects(run,{code:'cancelled'});
 await assert.rejects(sync.run(f.ear,f.clock,()=>{},()=>{},f.options),{code:'busy'});assert.equal(f.state.requests,1);sync.cancel();await rejected;grant.resolve(f.stream);await Promise.resolve();assert.equal(f.state.stops,1);
});
test('a late permission grant from an old run cannot stop or activate a replacement session',async t=>{
 const first=deferred(),second=deferred();let requests=0;const f=browserFixture(t,{permission:()=>++requests===1?first.promise:second.promise}),sync=new FridayAcousticSync();
 const run1=sync.run(f.ear,f.clock,()=>{},()=>{},f.options),reject1=assert.rejects(run1,{code:'cancelled'});sync.cancel();await reject1;
 const run2=sync.run(f.ear,f.clock,()=>{},()=>{},f.options),reject2=assert.rejects(run2,{code:'cancelled'});first.resolve(f.stream);await Promise.resolve();assert.equal(f.state.stops,1);assert.equal(f.ear.calibrating,true);assert.equal(f.events.includes('input'),false);
 sync.cancel();await reject2;let secondStops=0;second.resolve({getTracks:()=>[{stop:()=>secondStops++}]});await Promise.resolve();assert.equal(secondStops,1);
});
test('an old cancellation cannot clear a replacement ear session calibration state',async t=>{
 const grant=deferred(),f=browserFixture(t,{permission:()=>grant.promise}),sync=new FridayAcousticSync();
 const run=sync.run(f.ear,f.clock,()=>{},()=>{},f.options),rejected=assert.rejects(run,{code:'cancelled'});
 f.ear.version++;const replacement={};f.ear.calibrationOwner=replacement;f.ear.calibrating=true;sync.cancel();await rejected;
 assert.equal(f.ear.calibrationOwner,replacement);assert.equal(f.ear.calibrating,true);grant.resolve(f.stream);await Promise.resolve();
});
test('same-song restart during capture rejects the result and releases the microphone',async t=>{
 let f;const rows=referenceFeatures(excerpt(synthetic(12),7.38),0,6);
 f=browserFixture(t,{onConnect:node=>{node.port.onmessage({data:{type:'feature',...rows[0],time:10+rows[0].time}});f.clock.generation++;node.port.onmessage({data:{type:'done'}});}});
 await assert.rejects(new FridayAcousticSync().run(f.ear,f.clock,()=>{},()=>{},f.options),{code:'changed'});assert.equal(f.state.stops,1);assert.equal(f.events.includes('play-original'),false);
});
test('absolute offsets do not accumulate the old offset or clamp away a negative correction near song start',()=>{
 assert.equal(alignmentOffset(7.412,7.812),-400);assert.equal(alignmentOffset(8.012,7.812),200);assert.equal(alignmentOffset(.032,.132),-100);
 assert.throws(()=>alignmentOffset(0,6),{code:'ambiguous'});assert.throws(()=>alignmentOffset(0,null),{code:'changed'});
});
test('same-timeline accompaniment matches while the original contains a strong independent vocal stem',async t=>{
 const source=synthetic(12),vocal=synthetic(79),mixed=new Float32Array(source.length);
 for(let i=0;i<mixed.length;i++)mixed[i]=source.getChannelData(0)[i]*.15+vocal.getChannelData(0)[i];
 const original=audioBuffer(mixed),rows=referenceFeatures(excerpt(source,7.38),0,6),wrong=matchFeatures(referenceFeatures(original,0,24),rows);
 // A mixed original can sometimes still match. It is not the reference that
 // was heard, and this fixture measures the lost confidence without assuming
 // that every different stem must fail the matcher.
 assert.ok(wrong.score<.8,JSON.stringify(wrong));
 const f=browserFixture(t,{original,onConnect:node=>{for(const row of rows)node.port.onmessage({data:{type:'feature',...row,time:10+row.time}});node.port.onmessage({data:{type:'done'}});}});
 const result=await new FridayAcousticSync().run(f.ear,f.clock,()=>{},()=>{},f.options);
 assert.equal(result.offset,-400);assert.ok(result.score>wrong.score+.2);assert.ok(result.score>.9);assert.equal(f.ear.buffer,original);assert.equal(f.events.includes('play-original'),false);assert.equal(f.state.stops,1);assert.equal(f.ear.calibrating,false);
});
test('status updates throughout recording cannot shift an accepted offset by the capture duration',async t=>{
 const {FridayGuideClock}=await import('../integrations/ktv_public/web/guide.js'),clock=new FridayGuideClock();
 const playback={online:true,current:'song',state:'playing',position_ms:7780,position_at:1000000};clock.observe({server_time_ms:1000000,playback},10000,10000);clock.adjustment=-250;
 const rows=referenceFeatures(excerpt(synthetic(12),7.38),0,6);
 const f=browserFixture(t,{clock,onConnect:node=>{
  rows.forEach((row,index)=>{if(index===150)clock.observe({server_time_ms:1006000,playback:{...playback,position_ms:13780,position_at:1006000}},16000,16000);node.port.onmessage({data:{type:'feature',...row,time:10+row.time}});});node.port.onmessage({data:{type:'done'}});
 }});
 const result=await new FridayAcousticSync().run(f.ear,clock,()=>{},()=>{},f.options);assert.equal(result.offset,-400);assert.equal(clock.adjustment,-250,'run only returns a result; the caller owns committing it');assert.equal(clock.generation,0);
});
test('speaker calibration disconnects the real guide before capture and only plays after tracks end and result is accepted',async t=>{
 const rows=referenceFeatures(excerpt(synthetic(12),7.38),0,6);
 const f=browserFixture(t,{realEar:true,onConnect:node=>{for(const row of rows)node.port.onmessage({data:{type:'feature',...row,time:10+row.time}});node.port.onmessage({data:{type:'done'}});}});
 f.ear.setManualAdjustment(0);assert.equal(f.ear.sync(true),true);f.events.length=0;
 const result=await new FridayAcousticSync().run(f.ear,f.clock,()=>{},()=>{},f.options);
 for(const event of ['guide.stop','guide.disconnect','gain.disconnect','primer.pause'])assert.ok(f.events.indexOf(event)>=0&&f.events.indexOf(event)<f.events.indexOf('permission'),event);
 assert.equal(f.events.includes('guide.start'),false);assert.equal(f.track.readyState,'ended');
 assert.ok(f.events.lastIndexOf('resume')>f.events.indexOf('track.stop'),'playback session resumes after input ends');
 assert.equal(f.ear.sync(true),false,'return alone cannot activate a result that app has not accepted');
 assert.equal(f.clock.adjustment,0);f.ear.acceptAlignment(result);assert.equal(f.clock.adjustment,-400);assert.equal(f.ear.sync(true),true);
 assert.ok(f.events.indexOf('guide.start')>f.events.indexOf('track.stop'));
 for(let i=0;i<20;i++)f.ear.sync();assert.equal(f.state.requests,1,'ordinary playback updates do not recapture the phone speaker');
 f.ear.stop();
});
test('the capture owner blocks speaker output even if an outer UI clears its calibrating boolean',async t=>{
 const grant=deferred(),f=browserFixture(t,{realEar:true,permission:()=>grant.promise}),sync=new FridayAcousticSync();
 f.ear.setManualAdjustment(0);assert.equal(f.ear.sync(),true);
 const run=sync.run(f.ear,f.clock,()=>{},()=>{},f.options),rejected=assert.rejects(run,{code:'cancelled'});
 f.ear.calibrating=false;assert.equal(f.ear.sync(true),false);assert.equal(f.ear.node,null);
 assert.throws(()=>f.ear.setManualAdjustment(100),{code:'changed'});
 sync.cancel();await rejected;grant.resolve(f.stream);await Promise.resolve();assert.equal(f.track.readyState,'ended');assert.equal(f.ear.sync(),false);f.ear.stop();
});
test('a track that does not end prevents both accepted and manual speaker playback after cleanup',async t=>{
 const rows=referenceFeatures(excerpt(synthetic(12),7.38),0,6);
 const f=browserFixture(t,{realEar:true,trackStop:()=>{},onConnect:node=>{for(const row of rows)node.port.onmessage({data:{type:'feature',...row,time:10+row.time}});node.port.onmessage({data:{type:'done'}});}});
 await assert.rejects(new FridayAcousticSync().run(f.ear,f.clock,()=>{},()=>{},f.options),{code:'capture_release_failed'});
 assert.equal(f.ear.calibrating,false);assert.equal(f.ear.capturePending(),true);assert.equal(f.ear.sync(true),false);
 assert.throws(()=>f.ear.setManualAdjustment(0),{code:'changed'});assert.equal(f.events.filter(x=>x==='resume').length,1);
 f.ear.stop();assert.equal(f.ear.capturePending(),true,'stop must not erase unresolved ownership');
 f.track.readyState='ended';assert.equal(f.ear.capturePending(),false);
});
test('a completed match becomes unusable after same-song replay and cannot restart output',async t=>{
 const rows=referenceFeatures(excerpt(synthetic(12),7.38),0,6);
 const f=browserFixture(t,{realEar:true,onConnect:node=>{for(const row of rows)node.port.onmessage({data:{type:'feature',...row,time:10+row.time}});node.port.onmessage({data:{type:'done'}});}});
 const result=await new FridayAcousticSync().run(f.ear,f.clock,()=>{},()=>{},f.options);
 f.clock.generation++;assert.throws(()=>f.ear.acceptAlignment(result),{code:'changed'});assert.equal(f.ear.needsAlignment(),true);assert.equal(f.ear.sync(true),false);assert.equal(f.state.requests,1);f.ear.stop();
});
test('same-song replay while worklet loading is stalled releases capture without waiting for its timeout',async t=>{
 const entered=deferred(),module=deferred(),f=browserFixture(t,{addModule:()=>{entered.resolve();return module.promise;}});
 const run=new FridayAcousticSync().run(f.ear,f.clock,()=>{},()=>{},f.options),rejected=assert.rejects(run,{code:'changed'});
 await entered.promise;f.clock.generation++;await rejected;assert.equal(f.state.stops,1);assert.equal(f.events.includes('input'),false);
 assert.ok([...f.listeners.values()].every(set=>set.size===0));module.resolve();
});
test('pagehide during a permission prompt cancels the run and releases a later microphone grant',async t=>{
 const grant=deferred(),f=browserFixture(t,{permission:()=>grant.promise});
 const run=new FridayAcousticSync().run(f.ear,f.clock,()=>{},()=>{},f.options),rejected=assert.rejects(run,{code:'changed'});
 f.emit('window','pagehide');await rejected;grant.resolve(f.stream);await Promise.resolve();
 assert.equal(f.state.stops,1);assert.equal(f.events.includes('input'),false);assert.ok([...f.listeners.values()].every(set=>set.size===0));
});
test('an interrupted output context while capturing immediately ends the microphone and never resumes output',async t=>{
 let f;f=browserFixture(t,{onConnect:()=>{f.context.state='interrupted';f.emit('context','statechange');}});
 await assert.rejects(new FridayAcousticSync().run(f.ear,f.clock,()=>{},()=>{},f.options),{code:'audio_start_blocked'});
 assert.equal(f.track.readyState,'ended');assert.equal(f.events.filter(x=>x==='resume').length,1);assert.ok([...f.listeners.values()].every(set=>set.size===0));
});
test('cooperative feature and match paths preserve the existing result and can cancel between computation slices',async()=>{
 const source=synthetic(12),heard=excerpt(source,7.38),recorded=referenceFeatures(heard,0,6);let yields=0;
 const options={yieldTask:async()=>{yields++;}};const reference=await referenceFeaturesAsync(source,0,24,options);
 assert.deepEqual(reference,referenceFeatures(source,0,24));assert.ok(yields>0);const result=await matchFeaturesAsync(reference,recorded,options);assert.deepEqual(result,matchFeatures(reference,recorded));
 let cancelled=false,slices=0;await assert.rejects(matchFeaturesAsync(reference,recorded,{check(){if(cancelled)throw Object.assign(new Error(),{code:'cancelled'});},yieldTask:async()=>{slices++;cancelled=true;}}),{code:'cancelled'});assert.equal(slices,1);
});
test('capture timestamps include the partially skipped settling block at 44.1 kHz',async t=>{
 const names=['AudioWorkletProcessor','sampleRate','currentFrame','registerProcessor'],saved=new Map(names.map(name=>[name,Object.getOwnPropertyDescriptor(globalThis,name)])),messages=[];let Processor;
 const values={AudioWorkletProcessor:class{constructor(){this.port={onmessage:null,postMessage:value=>messages.push(value)};}},sampleRate:44100,currentFrame:0,registerProcessor:(name,type)=>{assert.equal(name,'friday-sync-capture');Processor=type;}};
 for(const [name,value] of Object.entries(values))Object.defineProperty(globalThis,name,{configurable:true,writable:true,value});
 t.after(()=>{for(const [name,descriptor] of saved){if(descriptor)Object.defineProperty(globalThis,name,descriptor);else delete globalThis[name];}});
 await import('../integrations/ktv_public/web/sync-capture.mjs?capture-timestamp-test');const processor=new Processor();
 for(let frame=0;frame<44100&&!messages.length;frame+=128){globalThis.currentFrame=frame;const output=new Float32Array(128).fill(1);processor.process([[new Float32Array(128).fill(.1)]],[[output]]);assert.ok(output.every(value=>value===0),'capture worklet never monitors microphone input to the speaker');}
 assert.equal(messages[0].type,'feature');assert.ok(Math.abs(messages[0].time-.532)<1e-10,JSON.stringify(messages[0].time));processor.port.onmessage({data:'stop'});assert.equal(processor.process([]),false);
});


test('phase-inverted stereo reference retains its signal and matches the mono room excerpt',()=>{
 const source=synthetic(12),left=source.getChannelData(0),right=Float32Array.from(left,v=>-v),stereo={...source,numberOfChannels:2,getChannelData:channel=>channel?right:left};
 const reference=referenceFeatures(stereo,0,24),recorded=referenceFeatures(excerpt(source,7.38),0,6);assert.ok(reference.some(row=>row.rms>.01));assert.equal(matchFeatures(reference,recorded).ok,true);
 const silent=new Float32Array(left.length),rightOnly={...source,numberOfChannels:2,getChannelData:channel=>channel?left:silent};assert.equal(matchFeatures(referenceFeatures(rightOnly,0,24),recorded).ok,true);
});

test('capture frame gaps stop the worklet instead of compressing missing audio time',async t=>{
 const names=['AudioWorkletProcessor','sampleRate','currentFrame','registerProcessor'],saved=new Map(names.map(name=>[name,Object.getOwnPropertyDescriptor(globalThis,name)])),messages=[];let Processor;
 const values={AudioWorkletProcessor:class{constructor(){this.port={onmessage:null,postMessage:value=>messages.push(value)};}},sampleRate:48000,currentFrame:0,registerProcessor:(name,type)=>{Processor=type;}};
 for(const [name,value] of Object.entries(values))Object.defineProperty(globalThis,name,{configurable:true,writable:true,value});
 t.after(()=>{for(const [name,value]of saved){if(value)Object.defineProperty(globalThis,name,value);else delete globalThis[name];}});
 await import('../integrations/ktv_public/web/sync-capture.mjs?gap-regression');const processor=new Processor();
 for(let frame=0;frame<26000;frame+=128){globalThis.currentFrame=frame;processor.process([[new Float32Array(128).fill(.1)]]);}
 globalThis.currentFrame+=256;assert.equal(processor.process([[new Float32Array(128).fill(.1)]]),false);assert.deepEqual(messages.at(-1),{type:'error',code:'capture_timeout'});assert.equal(processor.process([]),false);
});

test('a worklet discontinuity immediately ends capture and cannot commit a timing offset',async t=>{
 const f=browserFixture(t,{onConnect:node=>node.port.onmessage({data:{type:'error',code:'capture_timeout'}})});
 await assert.rejects(new FridayAcousticSync().run(f.ear,f.clock,()=>{},()=>{},f.options),{code:'capture_timeout'});assert.equal(f.track.readyState,'ended');assert.equal(f.state.stops,1);assert.equal(f.events.includes('play-original'),false);assert.equal(f.clock.adjustment,0);
});
