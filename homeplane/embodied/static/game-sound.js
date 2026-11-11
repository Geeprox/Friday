/* Opt-in page sound. Only the original rules can earn an outcome. */
function gameSoundFrame(g){
 if(!g||!/^[a-f0-9]{32}$/.test(g.control_context)||!Number.isSafeInteger(g.round_id)||g.round_id<1||!['ski','tennis','quest','hiit'].includes(g.mode)||g.mode!==g.selected_mode)return null;
 const value=g[g.mode]||{},count=n=>Number.isSafeInteger(n)&&n>=0?n:null,event=value.event;
 return{key:g.control_context+':'+g.round_id+':'+g.mode,mode:g.mode,status:g.status,elapsed:Number.isFinite(g.elapsed)&&g.elapsed>=0?g.elapsed:null,
  held:g.manually_paused===true||g.source_recovery?.required===true||g.resume_seconds>0||!['playing','complete'].includes(g.status),
  level:count(value.level),reps:count(value.total_reps),passed:count(value.gates_passed),missed:count(value.gates_missed),collisions:count(value.collisions),
  sequence:count(event?.sequence)||0,event:event&&typeof event==='object'?{...event}:null};
}
function gameSoundCue(before,after){
 if(!before||!after||before.key!==after.key||before.held||after.held||before.status!=='playing'||before.elapsed!==null&&after.elapsed!==null&&after.elapsed<before.elapsed)return null;
 if(after.status==='complete')return'finish';
 if(after.mode==='quest'||after.mode==='hiit'){
  if(before.level!==null&&after.level===before.level+1)return'stage';
  if(before.level===after.level&&before.reps!==null&&after.reps===before.reps+1)return'rep';
  return null;
 }
 const event=after.event;
 if(!event||after.sequence!==before.sequence+1||!Number.isFinite(event.age)||event.age<0||event.age>.35)return null;
 if(after.mode==='tennis'&&[0,1].includes(event.player))return ({serve:'racket',hit:'racket',point:'point',miss:'miss'})[event.kind]||null;
 if(after.mode==='ski'){
  if(event.kind==='gate'&&before.passed!==null&&after.passed===before.passed+1)return'gate';
  if(event.kind==='miss'&&before.missed!==null&&after.missed===before.missed+1)return'miss';
  if(event.kind==='collision'&&before.collisions!==null&&after.collisions===before.collisions+1)return'bump';
 }
 return null;
}

function createGameSound({state,current,visible,onChange=()=>{},clock=()=>performance.now(),schedule=setTimeout,cancel=clearTimeout,makeContext=()=>{
 const Audio=globalThis.AudioContext||globalThis.webkitAudioContext;
 if(!Audio)throw Error('sound_unavailable');
 return new Audio();
}}){
 let audio=null,pending=null,status='off',generation=0,closed=false,previous=null,lastInput=null,observedAt=null,lastCue=-Infinity;
 const voices=new Set();
 const summary=()=>({state:status,enabled:status==='on',pending:status==='starting'});
 function notify(){onChange(summary());}
 function stop(){
  for(const voice of voices){try{voice.oscillator.stop();}catch{}try{voice.oscillator.disconnect();voice.gain.disconnect();}catch{}}
  voices.clear();
 }
 function disable(next='off'){
  generation++;previous=null;lastInput=null;observedAt=null;lastCue=-Infinity;stop();
  if(pending){cancel(pending.timer);pending.resolve(false);pending=null;}
  const old=audio;audio=null;
  if(old){old.onstatechange=null;try{Promise.resolve(old.close()).catch(()=>{});}catch{}}
  status=next;notify();
 }
 /* Short, original procedural cues: soft wooden taps and restrained chimes.
    One cue at a time, at most three voices; no music, samples or output routing. */
 const patterns={
  on:[[523.25,0,.09,'sine'],[659.25,.08,.13,'sine']],
  rep:[[523.25,0,.11,'triangle']],
  stage:[[523.25,0,.12,'sine'],[783.99,.1,.22,'sine']],
  gate:[[659.25,0,.1,'triangle'],[987.77,.06,.14,'sine']],
  racket:[[196,0,.055,'triangle'],[392,0,.04,'sine']],
  point:[[523.25,0,.12,'triangle'],[659.25,.1,.17,'sine']],
  miss:[[293.66,0,.1,'sine']],bump:[[130.81,0,.09,'triangle']],
  finish:[[523.25,0,.18,'sine'],[659.25,.14,.18,'sine'],[783.99,.28,.32,'sine']],
 };
 function play(kind){
  if(status!=='on'||!audio||audio.state!=='running'||!visible())return;
  const pattern=patterns[kind];if(!pattern)return;
  try{
   stop();const at=audio.currentTime;
   for(const[frequency,delay,duration,type]of pattern){
    const oscillator=audio.createOscillator(),gain=audio.createGain(),voice={oscillator,gain};voices.add(voice);
    oscillator.type=type;oscillator.frequency.setValueAtTime(frequency,at+delay);
    gain.gain.setValueAtTime(0,at+delay);gain.gain.linearRampToValueAtTime(.075,at+delay+.008);gain.gain.exponentialRampToValueAtTime(.0001,at+delay+duration);
    oscillator.connect(gain);gain.connect(audio.destination);
    oscillator.onended=()=>{voices.delete(voice);oscillator.disconnect();gain.disconnect();};
    oscillator.start(at+delay);oscillator.stop(at+delay+duration+.01);
   }
  }catch{disable('blocked');}
 }
 function seed(){lastInput=state();previous=current()&&visible()?gameSoundFrame(lastInput):null;observedAt=clock();}
 function enable(){
  if(closed)return Promise.resolve(false);
  disable();const token=generation;status='starting';notify();
  return new Promise(resolve=>{
   const request={resolve,timer:null};pending=request;
   request.timer=schedule(()=>{if(pending===request)disable('blocked');},1500);
   try{
    const context=makeContext();audio=context;
    // Invoke resume synchronously in the explicit click's activation window.
    const resumed=context.resume();
    context.onstatechange=()=>{if(audio===context&&status==='on'&&context.state!=='running')disable('blocked');};
    Promise.resolve(resumed).then(()=>{
     if(closed||generation!==token||pending!==request||audio!==context)return;
     if(context.state!=='running'||!visible()){disable('blocked');return;}
     cancel(request.timer);pending=null;status='on';seed();notify();play('on');resolve(status==='on');
    },()=>{if(pending===request)disable('blocked');});
   }catch{if(pending===request)disable('blocked');}
  });
 }
 function tick(){
  if(closed||status!=='on')return;
  if(!audio||audio.state!=='running'){disable('blocked');return;}
  const at=clock(),input=state(),frame=current()&&visible()?gameSoundFrame(input):null;
  if(!frame||frame.held){stop();previous=frame;lastInput=null;observedAt=null;return;}
  if(input===lastInput)return;
  if(previous?.key===frame.key&&previous.elapsed!==null&&frame.elapsed!==null&&frame.elapsed<previous.elapsed)return;
  const cue=observedAt!==null&&at>=observedAt&&at-observedAt<=750?gameSoundCue(previous,frame):null;
  previous=frame;lastInput=input;observedAt=at;
  if(cue&&at-lastCue>=100){lastCue=at;play(cue);}
 }
 return{enable,disable,tick,status:summary,quiet(){stop();previous=null;lastInput=null;observedAt=null;if(status==='starting')disable();},close(){closed=true;disable();},reopen(){closed=false;}};
}
