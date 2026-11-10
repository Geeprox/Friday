import {FeatureStream} from './sync-dsp.mjs';
class FridaySyncCapture extends AudioWorkletProcessor{
 constructor(){super();this.origin=null;this.stopped=false;this.stream=new FeatureStream(sampleRate,row=>this.port.postMessage({type:'feature',...row,time:this.origin+row.time}));this.frames=0;this.expectedFrame=null;this.settleFrames=Math.ceil(sampleRate*.5);this.port.onmessage=event=>{if(event.data==='stop'){this.stopped=true;this.stream.clear();}};}
 process(inputs,outputs=[]){for(const output of outputs)for(const channel of output)channel.fill(0);if(this.stopped)return false;let block=inputs[0]?.[0],skipped=0;
  if(this.origin!==null&&(!block||!block.length||currentFrame!==this.expectedFrame)){this.stopped=true;this.stream.clear();this.port.postMessage({type:'error',code:'capture_timeout'});return false;}
  if(block){this.expectedFrame=currentFrame+block.length;if(this.settleFrames>0){skipped=Math.min(this.settleFrames,block.length);this.settleFrames-=skipped;block=block.subarray(skipped);if(!block.length)return true;}if(this.origin===null)this.origin=(currentFrame+skipped)/sampleRate;this.stream.push(block);this.frames+=block.length;if(this.frames>=sampleRate*6){this.stream.clear();this.port.postMessage({type:'done'});return false;}}return true;}
}
registerProcessor('friday-sync-capture',FridaySyncCapture);
