import {referenceFeaturesAsync,matchFeaturesAsync} from './sync-dsp.mjs';
const syncError=code=>Object.assign(new Error(code),{code});
export function alignmentOffset(referenceTime,unadjustedTime){
 if(!Number.isFinite(referenceTime)||!Number.isFinite(unadjustedTime))throw syncError('changed');
 const offset=Math.round((referenceTime-unadjustedTime)*1000/10)*10;
 if(Math.abs(offset)>5000)throw syncError('ambiguous');
 return offset;
}
export class FridayAcousticSync{
 constructor(){this.session=null;}
 cancel(){this.cancelSession(this.session);}
 cancelSession(session,error=syncError('cancelled')){
  if(!session||session.closed||this.session!==session)return;
  session.cancelled=true;session.reject?.(error);this.release(session);
 }
 stopStream(session,stream){
  if(session.releasedStreams.has(stream))return;session.releasedStreams.add(stream);
  for(const track of stream.getTracks()){session.tracks.add(track);session.ear.watchCaptureTrack?.(track);try{track.stop();}catch{}}
 }
 release(session=this.session){
  if(!session)return;
  if(session.node){session.node.port.onmessage=null;try{session.node.port.postMessage('stop');}catch{}try{session.node.disconnect();}catch{}session.node=null;}
  if(session.input){try{session.input.disconnect();}catch{}session.input=null;}
  if(session.stream){this.stopStream(session,session.stream);session.stream=null;}
 }
 check(session){
  if(session.cancelled||session.closed||this.session!==session)throw syncError('cancelled');
  const {ear,clock}=session,p=clock.playback;
  if(ear.version!==session.version||ear.buffer!==session.buffer||ear.calibrationOwner!==session||
     !p?.online||p.current!==session.song||p.state!=='playing'||clock.adjustment!==session.adjustment||clock.generation!==session.generation||
     (typeof document!=='undefined'&&document.hidden))throw syncError('changed');
 }
 wait(session,value,timeout=0,code='audio_start_blocked'){
  return new Promise((resolve,reject)=>{
   let timer,watchdog,done=false;
   const finish=(callback,result)=>{if(done)return;done=true;clearTimeout(timer);clearInterval(watchdog);if(session.reject===cancel)session.reject=null;callback(result);};
   const cancel=error=>finish(reject,error);session.reject=cancel;
   Promise.resolve(value).then(result=>finish(resolve,result),error=>finish(reject,error));
   try{this.check(session);}catch(error){cancel(error);return;}
   // Awaited permission/module/resume promises can stall without another frame.
   // A timeline change must still release any already-granted capture promptly.
   watchdog=setInterval(()=>{try{this.check(session);}catch(error){this.cancelSession(session,error);}},100);
   if(timeout)timer=setTimeout(()=>cancel(syncError(code)),timeout);
  });
 }
 async run(ear,clock,changed=()=>{},diagnostic=()=>{},{referenceBuffer,referenceSong}={}){
  if(this.session)throw syncError('busy');
  if(!navigator.mediaDevices?.getUserMedia||!ear.context?.audioWorklet)throw syncError('unsupported');
  if(!referenceBuffer||typeof referenceBuffer.getChannelData!=='function'||!Number.isFinite(referenceBuffer.duration)||referenceBuffer.duration<=0||!referenceSong||referenceSong!==clock.playback?.current)throw syncError('reference_unavailable');
  if(!ear.buffer||typeof clock.unadjusted!=='function')throw syncError('changed');
  if(ear.calibrationOwner)throw syncError('busy');
  const context=ear.context,session={ear,clock,version:ear.version,buffer:ear.buffer,song:referenceSong,adjustment:clock.adjustment,generation:clock.generation,cancelled:false,closed:false,reject:null,stream:null,node:null,input:null,tracks:new Set(),releasedStreams:new WeakSet(),cancel:()=>this.cancelSession(session)};
  const recorded=[];let reference=[];this.session=session;ear.calibrationOwner=session;
  const check=()=>this.check(session);
  const hidden=()=>{if(document.hidden)this.cancelSession(session,syncError('changed'));},pagehide=()=>this.cancelSession(session,syncError('changed'));
  const interrupted=()=>{if(session.node&&context.state!=='running')this.cancelSession(session,syncError('audio_start_blocked'));};
  try{
   check();
   if(ear.beginCalibration)ear.beginCalibration(session);else{ear.calibrating=true;ear.halt();ear.unlock?.pause();}
   if(typeof document!=='undefined')document.addEventListener?.('visibilitychange',hidden);
   if(typeof window!=='undefined')window.addEventListener?.('pagehide',pagehide);
   context.addEventListener?.('statechange',interrupted);
   diagnostic({phase:'requesting'});
   const request=navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:false,noiseSuppression:false,autoGainControl:false,latency:{ideal:0}},video:false});
   // A permission prompt may resolve after cancellation or timeout. Its stream
   // still belongs to this run and must never reach a later capture session.
   request.then(stream=>{if(session.cancelled||session.closed)this.stopStream(session,stream);},()=>{});
   session.stream=await this.wait(session,request,25000,'permission_timeout');check();diagnostic({phase:'settling'});
   try{if(navigator.audioSession)navigator.audioSession.type='play-and-record';}catch{}
   await this.wait(session,context.resume(),3500);check();
   if(context.state!=='running')throw syncError('audio_start_blocked');
   await this.wait(session,context.audioWorklet.addModule('./sync-capture.mjs'),10000,'capture_timeout');check();
   const track=session.stream.getAudioTracks()[0];if(!track)throw syncError('capture_timeout');
   const settings=track.getSettings(),latency=Number.isFinite(settings.latency)?Math.max(0,Math.min(.5,settings.latency)):0;
   const position=clock.unadjusted(performance.now());if(!Number.isFinite(position))throw syncError('changed');
   reference=await referenceFeaturesAsync(referenceBuffer,Math.max(0,position-8),Math.min(referenceBuffer.duration,position+15),{check});check();
   session.input=context.createMediaStreamSource(session.stream);
   session.node=new AudioWorkletNode(context,'friday-sync-capture',{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1]});
   let firstPerformance=null;
   await this.wait(session,new Promise((resolve,reject)=>{
    session.node.port.onmessage=event=>{
     try{
      check();if(context.state!=='running')throw syncError('audio_start_blocked');const data=event.data;
      if(data.type==='feature'){
       if(firstPerformance===null)firstPerformance=performance.now()+(data.time-context.currentTime)*1000-latency*1000;
       recorded.push(data);if(recorded.length%50===0)diagnostic({phase:'capturing',frames:recorded.length});changed(Math.max(0,6-Math.floor(recorded.length*.02)));
      }else if(data.type==='error')reject(syncError('capture_timeout'));else if(data.type==='done')resolve();
     }catch(error){reject(error);}
    };
    session.input.connect(session.node);session.node.connect(context.destination);
   }),10000,'capture_timeout');
   this.release(session);check();
   if([...session.tracks].some(track=>track.readyState!=='ended'))throw syncError('capture_release_failed');
   diagnostic({phase:'matching',frames:recorded.length});
   const result=await matchFeaturesAsync(reference,recorded,{check});check();
   if(!result.ok)throw Object.assign(syncError(result.reason),{score:result.score||0,margin:result.margin||0,frames:recorded.length});
   const offset=alignmentOffset(result.time,clock.unadjusted(firstPerformance));
   try{if(navigator.audioSession)navigator.audioSession.type='playback';}catch{}
   // Only a successful, still-current calibration resumes its output context.
   // Cancellation never waits for a suspended browser context in cleanup.
   await this.wait(session,context.resume(),3500);check();
   if(context.state!=='running')throw syncError('audio_start_blocked');
   const aligned={offset,score:result.score,margin:result.margin,frames:recorded.length};
   ear.stageAlignment?.(session,aligned);return aligned;
  }finally{
   session.closed=true;session.reject=null;this.release(session);recorded.length=0;reference.length=0;
   if(typeof document!=='undefined')document.removeEventListener?.('visibilitychange',hidden);
   if(typeof window!=='undefined')window.removeEventListener?.('pagehide',pagehide);
   context.removeEventListener?.('statechange',interrupted);
   if(ear.calibrationOwner===session){ear.calibrationOwner=null;if(ear.version===session.version){ear.calibrating=false;try{if(navigator.audioSession)navigator.audioSession.type='playback';}catch{}}}
   if(this.session===session)this.session=null;
  }
 }
}
