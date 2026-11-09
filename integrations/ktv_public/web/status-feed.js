"use strict";
(function(scope){
 class FridayStatusFeed {
  constructor({events,probe,onStatus,onFailure=()=>{},visible=()=>true,setTimer=setTimeout,clearTimer=clearTimeout,now=()=>performance.now(),probeEvery=2500}){
   Object.assign(this,{events,probe,onStatus,onFailure,visible,setTimer,clearTimer,now,probeEvery});
   this.cursor=0;this.active=false;this.closed=false;this.legacy=false;this.generation=0;this.failures=0;this.controller=null;this.pollTimer=null;this.probeTimer=null;this.probing=false;this.probeController=null;
  }
  current(generation){return this.active&&!this.closed&&this.visible()&&generation===this.generation;}
  start(){if(this.closed||this.active||!this.visible())return;this.active=true;const generation=++this.generation;this.poll(generation);if(!this.legacy)this.scheduleProbe(generation);}
  stop(){this.active=false;this.generation++;this.controller?.abort();this.probeController?.abort();this.clearTimer(this.pollTimer);this.clearTimer(this.probeTimer);this.pollTimer=this.probeTimer=null;}
  close(){this.closed=true;this.stop();}
  schedulePoll(generation,delay){if(!this.current(generation))return;this.clearTimer(this.pollTimer);this.pollTimer=this.setTimer(()=>{this.pollTimer=null;this.poll(generation);},delay);}
  scheduleProbe(generation){if(!this.current(generation)||this.legacy)return;this.clearTimer(this.probeTimer);this.probeTimer=this.setTimer(async()=>{this.probeTimer=null;await this.sample(generation);this.scheduleProbe(generation);},this.probeEvery);}
  async sample(generation){if(!this.current(generation)||this.probing)return;this.probing=true;const controller=new AbortController();this.probeController=controller;try{await this.probe({signal:controller.signal});}catch(error){if(this.current(generation)&&!controller.signal.aborted)this.onFailure(error);}finally{if(this.probeController===controller)this.probeController=null;this.probing=false;}}
  async poll(generation){
   if(!this.current(generation))return;
   // A stop/start does not open another connection before the aborted request
   // settles. Its old response can never publish into the replacement run.
   if(this.controller){this.schedulePoll(generation,50);return;}
   if(this.legacy){await this.sample(generation);this.schedulePoll(generation,1000);return;}
   const controller=new AbortController();this.controller=controller;const started=this.now();let delay=100;
   try{
    const value=await this.events({cursor:this.cursor,signal:controller.signal});
    if(!this.current(generation)||controller.signal.aborted)return;
    if(!Number.isSafeInteger(value?.cursor)||!value.status||!Number.isFinite(value.status.server_time_ms))throw Object.assign(new Error(),{code:"service_unavailable"});
    this.cursor=value.cursor;this.failures=0;this.onStatus(value.status);
    delay=Math.max(0,100-(this.now()-started));
   }catch(error){
    if(!this.current(generation)||controller.signal.aborted)return;
    if((error.status===404&&!['room_closed','room_unavailable','guest_unavailable'].includes(error.code))
       ||(error.status===429&&error.code==='events_capacity')){
     this.legacy=true;this.clearTimer(this.probeTimer);this.probeTimer=null;delay=0;
    }else{
     this.failures++;delay=Math.min(4000,250*Math.pow(2,Math.min(4,this.failures-1)));this.onFailure(error);
     if(['room_closed','room_unavailable','guest_unavailable'].includes(error.code)){this.close();return;}
    }
   }finally{
    if(this.controller===controller)this.controller=null;
    if(this.current(generation))this.schedulePoll(generation,delay);
   }
  }
 }
 if(typeof module!=="undefined")module.exports=FridayStatusFeed;
 scope.FridayStatusFeed=FridayStatusFeed;
})(typeof window!=="undefined"?window:globalThis);
