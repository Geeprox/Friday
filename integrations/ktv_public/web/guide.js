"use strict";
const FRIDAY_SILENT_PRIMER="data:audio/mpeg;base64,SUQzBAAAAAAAIlRTU0UAAAAOAAADTGF2ZjYzLjEuMTAyAAAAAAAAAAAAAAD/+0DAAAAAAAAAAAAAAAAAAAAAAABJbmZvAAAADwAAACgAABEKABAQFhYdHR0jIykpKS8vNTU1OztBQUFISE5OTlRUWlpaYGBmZmZsbHJycnl5f39/hYWLi4uRkZeXl52do6OjqqqwsLC2try8vMLCyMjIzs7V1dXb2+Hh4efn7e3t8/P5+fn//wAAAABMYXZjNjMuMS4AAAAAAAAAAAAAAAAkBXwAAAAAAAARCiWRafkAAAAAAP/7EMQAA8AAAaQAAAAgAAA0gAAABExBTUU0LjBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVV//sSxCmDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVV//sQxFODwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUU0LjBVVVX/+xLEfQPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUU0LjBVVVX/+xDEpwPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQuMFVVVf/7EsTQg8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQuMFVVVf/7EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVV//sSxNWDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUU0LjBVVVX/+xLE1YPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUU0LjBVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQuMFVVVf/7EsTVg8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQuMFVVVf/7EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVV//sSxNWDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUU0LjBVVVX/+xLE1YPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUU0LjBVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQuMFVVVf/7EsTVg8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQuMFVVVf/7EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVV//sSxNWDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUU0LjBVVVX/+xLE1YPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUU0LjBVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQuMFVVVf/7EsTVg8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQuMFVVVf/7EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVV//sSxNWDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUU0LjBVVVX/+xLE1YPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUU0LjBVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQuMFVVVf/7EsTVg8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQuMFVVVf/7EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVV//sSxNWDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUU0LjBVVVX/+xLE1YPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUU0LjBVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/7EsTVg8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/7EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//sSxNWDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/+xLE1YPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVU=";
function guideError(code){return Object.assign(new Error(code),{code});}
function guideWait(value,signal,timeout=0,code="audio_start_blocked"){
 return new Promise((resolve,reject)=>{
  let timer,finished=false;
  const finish=(callback,result)=>{if(finished)return;finished=true;clearTimeout(timer);signal.removeEventListener("abort",abort);callback(result);};
  const abort=()=>finish(reject,guideError("cancelled"));
  signal.addEventListener("abort",abort,{once:true});
  Promise.resolve(value).then(result=>finish(resolve,result),error=>finish(reject,error));
  if(signal.aborted)abort();else if(timeout)timer=setTimeout(()=>finish(reject,guideError(code)),timeout);
 });
}
class FridayGuideClock {
 constructor(){this.offset=0;this.best=Infinity;this.receipt=0;this.playback=null;this.adjustment=0;this.generation=0;this.lastServerTime=-Infinity;this.lastResponseStart=-Infinity;this.retiredEpochs=[];}
 observe(value,start,end,{calibrate=true}={}){
  const server=value?.server_time_ms,p=value?.playback,elapsed=end-start;
  if(!Number.isFinite(server)||!Number.isFinite(start)||!Number.isFinite(end)||elapsed<0||!p)return false;
  // An older in-flight status response must not rewind the song or the clock.
  if(server<this.lastServerTime||(server===this.lastServerTime&&start<this.lastResponseStart))return false;
  const previous=this.playback,epoch=typeof p.playback_epoch==="string"&&p.playback_epoch?p.playback_epoch:null;
  // UUID epochs are never reused by the TV. A delayed old source report must
  // not bring a retired play/seek session back after its replacement was seen.
  if(epoch&&this.retiredEpochs.includes(epoch))return false;
  if(previous){
   const before=this.positionAt(previous,server),after=this.positionAt(p,server);
   const stateChange=previous.state!==p.state,continuousPause=stateChange&&[previous.state,p.state].every(state=>state==="playing"||state==="paused");
   const oldPosition=this.positionAt(previous,this.lastServerTime),elapsedSeconds=Math.max(0,server-this.lastServerTime)/1000;
   // A pause can occur anywhere between reports. Comparing the paused position
   // with a projection that kept playing until this response invents a seek.
   const jumped=before!==null&&after!==null&&(continuousPause&&oldPosition!==null
    ?after<oldPosition-.25||after>oldPosition+elapsedSeconds+.25:Math.abs(after-before)>.75);
   const previousEpoch=typeof previous.playback_epoch==="string"&&previous.playback_epoch?previous.playback_epoch:null;
   const changedEpoch=(previousEpoch!==null||epoch!==null)&&previousEpoch!==epoch;
   if(previousEpoch&&epoch&&previousEpoch!==epoch){this.retiredEpochs.push(previousEpoch);if(this.retiredEpochs.length>16)this.retiredEpochs.shift();}
   const changedMedia=previous.guide?.sha256!==p.guide?.sha256||previous.guide?.reference?.sha256!==p.guide?.reference?.sha256;
   if(previous.current!==p.current||previous.online!==p.online||changedEpoch||changedMedia||jumped||(stateChange&&!continuousPause))this.generation++;
  }
  if(calibrate&&(elapsed<this.best||end-this.receipt>30000)){this.offset=server-(start+end)/2;this.best=elapsed;this.receipt=end;}
  this.lastServerTime=server;this.lastResponseStart=start;this.playback=p;return true;
 }
 positionAt(p,server){
  if(!p?.online||!p.current||!Number.isFinite(p.position_ms)||!Number.isFinite(p.position_at))return null;
  // Calibration asks for the time of an earlier captured frame after newer
  // status reports arrive. Preserve backwards extrapolation on this timeline.
  return (p.position_ms+(p.state==="playing"?server-p.position_at:0))/1000;
 }
 unadjusted(now){return Number.isFinite(now)?this.positionAt(this.playback,now+this.offset):null;}
 target(now){const position=this.unadjusted(now);return position===null?null:Math.max(0,position+this.adjustment/1000);}
}
class FridayEarMonitor {
 constructor(clock,changed){this.clock=clock;this.changed=changed;this.context=null;this.node=null;this.buffer=null;this.version=0;this.operation=null;this.rate=1;this.anchor=0;this.started=0;this.level=.35;this.primed=false;this.sessionPlayback=false;this.unlock=null;this.primerVersion=0;this.calibrating=false;this.calibrationOwner=null;this.requireAlignment=false;this.alignment=null;this.pendingAlignment=null;this.captureTracks=new Set();this.outputDelay=null;}
 check(operation){if(this.operation!==operation||operation.controller.signal.aborted)throw guideError("cancelled");}
 begin(){if(this.calibrationOwner){this.calibrationOwner.cancel();this.calibrationOwner=null;this.calibrating=false;}this.operation?.controller.abort();const operation={version:++this.version,controller:new AbortController()};this.operation=operation;return operation;}
 async resumePlayback(){
  const operation=this.operation||this.begin();this.check(operation);
  if(this.calibrationOwner)throw guideError("busy");
  if(this.capturePending())throw guideError("capture_release_failed");
  try{if(navigator.audioSession){navigator.audioSession.type="playback";this.sessionPlayback=navigator.audioSession.type==="playback";}}catch{}
  if(!this.unlock){this.unlock=new Audio(FRIDAY_SILENT_PRIMER);this.unlock.loop=true;this.unlock.setAttribute("playsinline","");}
  // Keep both activation calls before the first await, in the user's click task.
  const primerVersion=++this.primerVersion;
  try{Promise.resolve(this.unlock.play()).then(()=>{if(this.operation===operation&&!operation.controller.signal.aborted&&primerVersion===this.primerVersion)this.primed=true;},()=>{if(this.operation===operation&&!operation.controller.signal.aborted&&primerVersion===this.primerVersion)this.primed=false;});}catch{this.primed=false;}
  if(!this.context)this.context=new (window.AudioContext||window.webkitAudioContext)();
  await guideWait(this.context.resume(),operation.controller.signal,3500);
  this.check(operation);if(this.context.state!=="running")throw guideError("audio_start_blocked");
 }
 async start(loader,{deferPlayback=false}={}){
  const operation=this.begin();this.halt();this.buffer=null;this.requireAlignment=deferPlayback;this.alignment=null;this.pendingAlignment=null;this.changed("start");
  try{
   await this.resumePlayback();this.check(operation);this.changed("loading");
   const bytes=await guideWait(loader(operation.controller.signal),operation.controller.signal);this.check(operation);
   const buffer=await guideWait(this.context.decodeAudioData(bytes),operation.controller.signal);this.check(operation);
   this.buffer=buffer;
   if(deferPlayback)this.changed("loading");else if(this.sync(true))this.changed("playing");
   return buffer;
  }catch(error){
   if(this.operation===operation){this.halt();this.buffer=null;this.pausePrimer();}
   throw error;
  }
 }
 position(){return this.anchor+(this.context.currentTime-this.started)*this.rate;}
 halt(){
  if(this.node){try{this.node.stop();}catch{}try{this.node.disconnect();}catch{}this.node=null;}
  if(this.gain){try{this.gain.disconnect();}catch{}this.gain=null;}
 }
 pausePrimer(){this.primerVersion++;this.unlock?.pause();this.primed=false;}
 watchCaptureTrack(track){this.captureTracks.add(track);}
 capturePending(){for(const track of this.captureTracks)if(track.readyState==="ended")this.captureTracks.delete(track);return this.captureTracks.size>0;}
 beginCalibration(owner){
  if(this.calibrationOwner&&this.calibrationOwner!==owner)throw guideError("busy");
  if(this.capturePending())throw guideError("capture_release_failed");
  this.calibrationOwner=owner;this.calibrating=true;this.requireAlignment=true;this.alignment=null;this.pendingAlignment=null;
  this.halt();this.pausePrimer();
 }
 alignmentCurrent(value){return !!value&&value.version===this.version&&value.buffer===this.buffer&&value.generation===this.clock.generation&&value.song===this.clock.playback?.current;}
 stageAlignment(owner,result){
  const value={result,version:owner.version,buffer:owner.buffer,generation:owner.generation,song:owner.song};
  if(this.calibrationOwner!==owner||owner.cancelled||!this.alignmentCurrent(value)||this.capturePending())throw guideError("changed");
  this.pendingAlignment=value;
 }
 acceptAlignment(result){
  const value=this.pendingAlignment;
  if(this.calibrationOwner||this.capturePending()||value?.result!==result||!this.alignmentCurrent(value)||!Number.isFinite(result.offset))throw guideError("changed");
  this.clock.adjustment=result.offset;this.alignment=value;this.pendingAlignment=null;return true;
 }
 setManualAdjustment(value){
  if(!Number.isFinite(value)||!this.buffer||!this.clock.playback?.online||!this.clock.playback.current||this.calibrationOwner||this.capturePending())throw guideError("changed");
  this.clock.adjustment=Math.max(-5000,Math.min(5000,value));
  this.alignment={version:this.version,buffer:this.buffer,generation:this.clock.generation,song:this.clock.playback.current};this.pendingAlignment=null;return true;
 }
 needsAlignment(){return this.requireAlignment&&!!this.buffer&&!this.alignmentCurrent(this.alignment);}
 stop(){this.calibrationOwner?.cancel();this.operation?.controller.abort();this.operation=null;this.version++;this.halt();this.buffer=null;this.calibrating=false;this.alignment=null;this.pendingAlignment=null;this.outputDelay=null;this.pausePrimer();this.changed("off");}
 sync(force=false){
  // A caller's UI flag cannot override the actual capture owner or live tracks.
  if(this.calibrating||this.calibrationOwner||this.capturePending()||this.needsAlignment()){this.halt();return false;}
  const target=this.clock.target(performance.now()),p=this.clock.playback;
  if(!this.buffer||target===null||!p?.online||p.state!=="playing"||document.hidden){this.halt();return false;}
  if(this.context.state!=="running"){this.halt();this.changed("suspended");return false;}
  const now=this.context.currentTime,measured=this.context.outputLatency,base=this.context.baseLatency;
  const latency=Number.isFinite(measured)&&measured>=0?measured:Number.isFinite(base)&&base>=0?base:0;
  const latencyChanged=this.outputDelay!==null&&Math.abs(latency-this.outputDelay)>.03;this.outputDelay=latency;
  const aim=target+latency;
  // There is no future media beyond EOF. Clamping and scheduling the final
  // 10 ms would replay that fragment on each status update until TV reaches EOF.
  if(!Number.isFinite(aim)||aim<0||aim+.02>=this.buffer.duration){this.halt();return false;}
  if(!this.node||force||latencyChanged||Math.abs(aim-this.position())>.25){
   this.halt();const node=this.context.createBufferSource(),gain=this.context.createGain();this.node=node;this.gain=gain;node.buffer=this.buffer;
   gain.gain.setValueAtTime(0,now);gain.gain.linearRampToValueAtTime(this.level,now+.025);node.connect(gain);gain.connect(this.context.destination);
   this.anchor=aim+.02;this.started=now+.02;this.rate=1;
   node.onended=()=>{if(this.node===node){this.node=null;this.gain=null;node.disconnect();gain.disconnect();}};
   node.start(this.started,Math.min(this.buffer.duration-.01,this.anchor));return true;
  }
  const position=this.position(),drift=aim-position;this.anchor=position;this.started=now;
  this.rate=Math.abs(drift)<.02?1:Math.max(.98,Math.min(1.02,1+drift/5));this.node.playbackRate.setValueAtTime(this.rate,now);return true;
 }
 stats(){const bound=v=>Math.max(0,Math.min(1810000,Math.round(v)));return {context:this.context?.state||"unknown",primed:this.primed,session_playback:this.sessionPlayback,node:!!this.node,buffer_ms:bound((this.buffer?.duration||0)*1000),position_ms:bound(this.context&&this.node?this.position()*1000:0),target_ms:bound((this.clock.target(performance.now())||0)*1000)};}
 volume(value){if(!Number.isFinite(value))return;this.level=Math.max(0,Math.min(1,value));if(this.gain)this.gain.gain.setTargetAtTime(this.level,this.context.currentTime,.02);}
}
if(typeof module!=="undefined")module.exports={FridayGuideClock,FridayEarMonitor};
if(typeof window!=="undefined"){window.FridayGuideClock=FridayGuideClock;window.FridayEarMonitor=FridayEarMonitor;}
