const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('homeplane/embodied/static/game-sound.js','utf8');
const g=(mode='ski',value={})=>({control_context:'a'.repeat(32),round_id:1,mode,selected_mode:mode,status:'playing',elapsed:1,[mode]:mode==='ski'?{gates_passed:0,gates_missed:0,collisions:0}:mode==='tennis'?{}:{level:0,total_reps:0},...value});
function fixture({resume,failPlay=false}={}){
 let game=g(),now=0,live=true,shown=true,created=0,next=0;const timers=new Map(),contexts=[],notes=[],states=[];
 const context=vm.createContext({performance:{now:()=>now}});vm.runInContext(source,context);
 const makeContext=()=>{created++;const c={state:'suspended',currentTime:0,destination:{},closed:0,nodes:[],resume(){if(resume)return resume(c);c.state='running';return Promise.resolve();},close(){c.closed++;c.state='closed';return Promise.resolve();},createOscillator(){if(failPlay)throw Error('synthetic render failure');const n={frequency:{setValueAtTime(f,t){n.frequencyValue=f;}},connect(){},disconnect(){n.disconnected=true;},start(at){notes.push({frequency:n.frequencyValue,at,type:n.type});},stop(){n.stopped=true;}};c.nodes.push(n);return n;},createGain(){return{gain:{setValueAtTime(){},linearRampToValueAtTime(value){assert(value<=.075);},exponentialRampToValueAtTime(){}},connect(){},disconnect(){}};}};contexts.push(c);return c;};
 const sound=context.createGameSound({state:()=>game,current:()=>live,visible:()=>shown,clock:()=>now,makeContext,onChange:s=>states.push(s.state),schedule:(fn,ms)=>{const id=++next;timers.set(id,{fn,ms});return id;},cancel:id=>timers.delete(id)});
 return{context,sound,contexts,notes,states,timers,get created(){return created;},set(packet,dt=150){game=structuredClone(packet);now+=dt;sound.tick();},now(at){now=at;},live(v){live=v;},visible(v){shown=v;},tick:()=>sound.tick(),game:()=>game,expire(){const t=[...timers.values()][0];assert(t);t.fn();}};
}
const frame=(f,x)=>f.context.gameSoundFrame(x),cue=(f,a,b)=>f.context.gameSoundCue(frame(f,a),frame(f,b));

test('no audio context, timers, voices or events are created until explicit activation',()=>{
 const f=fixture();f.tick();f.set(g('ski',{ski:{gates_passed:1,event:{sequence:1,kind:'gate',age:0}}}));
 assert.equal(f.created,0);assert.equal(f.notes.length,0);assert.equal(f.timers.size,0);assert.equal(f.sound.status().enabled,false);
});
test('activation is synchronous, bounded and seeds the existing progress without replay',async()=>{
 const f=fixture();f.set(g('ski',{ski:{gates_passed:4,event:{sequence:4,kind:'gate',age:0}}}));const p=f.sound.enable();assert.equal(f.created,1);assert(f.sound.status().pending);assert(await p);assert.equal(f.timers.size,0);assert.equal(f.notes.length,2);
 f.tick();assert.equal(f.notes.length,2);f.sound.close();assert.equal(f.contexts[0].closed,1);
});
test('current gate, miss and collision use only adjacent original event and counter increments',()=>{
 const f=fixture(),before=g();
 for(const[kind,key,sound]of [['gate','gates_passed','gate'],['miss','gates_missed','miss'],['collision','collisions','bump']]){
  const after=g('ski',{ski:{...before.ski,[key]:1,event:{sequence:1,kind,age:0}}});
  assert.equal(cue(f,before,after),sound);assert.equal(cue(f,after,after),null);
  assert.equal(cue(f,before,{...after,ski:{...after.ski,[key]:0}}),null);
 }
});
test('ski warmup sounds once on each freshly recognized step',()=>{
 const f=fixture(),state=(step,extra={})=>g('ski',{ski:{warmup:{step}},...extra});
 assert.equal(cue(f,state('left'),state('right')),'stage');
 assert.equal(cue(f,state('arms'),state('ready')),'stage');
 assert.equal(cue(f,state('right'),state('right')),null);
 assert.equal(cue(f,state('left'),state('arms')),null);
 assert.equal(cue(f,state('arms',{status:'paused'}),state('ready')),null);
 assert.equal(cue(f,state('arms'),state('ready',{round_id:2})),null);
 assert.equal(cue(f,state('ready'),g()),null);
});
test('tennis uses rule serve/hit/point events, never a swing, score change or predicted contact',()=>{
 const f=fixture(),before=g('tennis');
 for(const[kind,sound]of [['serve','racket'],['hit','racket'],['point','point']])assert.equal(cue(f,before,g('tennis',{tennis:{event:{sequence:1,kind,player:0,age:.1}}})),sound);
 assert.equal(cue(f,before,g('tennis',{tennis:{score:[1,0],rally:1}})),null);
 assert.equal(cue(f,before,g('tennis',{tennis:{event:{sequence:1,kind:'hit',player:2,age:0}}})),null);
});
test('body repetition, station and completion are distinct with completion taking precedence',()=>{
 const f=fixture();for(const mode of ['quest','hiit']){
  const before=g(mode);assert.equal(cue(f,before,g(mode,{[mode]:{level:0,total_reps:1}})),'rep');
  assert.equal(cue(f,before,g(mode,{[mode]:{level:1,total_reps:1}})),'stage');
  assert.equal(cue(f,before,g(mode,{status:'complete',[mode]:{level:1,total_reps:1}})),'finish');
  assert.equal(cue(f,before,g(mode,{[mode]:{level:0,total_reps:3}})),null);
 }
});
test('wrong round, source, mode, old event and unobserved event jumps stay silent',()=>{
 const f=fixture(),before=g(),after=g('ski',{ski:{gates_passed:1,event:{sequence:1,kind:'gate',age:0}}});
 for(const value of [{...after,round_id:2},{...after,control_context:'b'.repeat(32)},g('tennis'),{...after,elapsed:.5},{...after,ski:{...after.ski,event:{...after.ski.event,age:.36}}},{...after,ski:{...after.ski,event:{...after.ski.event,sequence:3}}},{...after,mode:'home',selected_mode:'home'}])assert.equal(cue(f,before,value),null);
});
test('pause, player/source recovery and preparation cannot produce a result cue',()=>{
 const f=fixture(),before=g(),after=g('ski',{status:'complete'});
 for(const patch of [{manually_paused:true},{source_recovery:{required:true}},{resume_seconds:1},{status:'paused'},{status:'waiting'}]){
  assert.equal(cue(f,{...before,...patch},after),null);assert.equal(cue(f,before,{...after,...patch}),null);
 }
});
test('one fresh outcome plays once and repeated paints or packets never replay it',async()=>{
 const f=fixture();await f.sound.enable();f.notes.length=0;f.set(g('ski',{ski:{gates_passed:1,gates_missed:0,collisions:0,event:{sequence:1,kind:'gate',age:0}}}));assert.equal(f.notes.length,2);
 for(let i=0;i<20;i++){f.tick();f.set(f.game(),10);}assert.equal(f.notes.length,2);assert(f.contexts[0].nodes.length<=4);f.sound.close();
});
test('render-loop paints cannot hide a gap between actual rule packets',async()=>{
 const f=fixture();await f.sound.enable();f.notes.length=0;
 for(let i=1;i<40;i++){f.now(i*25);f.tick();}
 f.set(g('ski',{ski:{gates_passed:1,event:{sequence:1,kind:'gate',age:0}}}),25);assert.equal(f.notes.length,0);f.sound.close();
});
test('hidden, unavailable and unready periods stop all voices and seed return silently',async()=>{
 for(const port of ['visible','live']){
  const f=fixture();await f.sound.enable();f.notes.length=0;f[port](false);f.tick();assert(f.contexts[0].nodes.every(n=>n.stopped));
  f.set(g('ski',{ski:{gates_passed:1,event:{sequence:1,kind:'gate',age:0}}}));assert.equal(f.notes.length,0);
  f[port](true);f.tick();assert.equal(f.notes.length,0);f.sound.close();
 }
});
test('a paused outcome and first resumed packet never replay, but the next fresh outcome does',async()=>{
 const f=fixture();await f.sound.enable();f.notes.length=0;f.set(g('ski',{manually_paused:true}));
 f.set(g('ski',{ski:{gates_passed:1,gates_missed:0,collisions:0,event:{sequence:1,kind:'gate',age:0}}}));assert.equal(f.notes.length,0);
 f.set(g('ski',{ski:{gates_passed:2,gates_missed:0,collisions:0,event:{sequence:2,kind:'gate',age:0}}}));assert.equal(f.notes.length,2);f.sound.close();
});
test('cancel, timeout and late activation cannot reopen or play on a retired page',async()=>{
 for(const action of ['disable','close','timeout','quiet']){
  let release;const f=fixture({resume:c=>new Promise(resolve=>{release=()=>{c.state='running';resolve();};})});
  const p=f.sound.enable();if(action==='timeout')f.expire();else f.sound[action]();assert.equal(await p,false);
  release();await new Promise(setImmediate);assert.equal(f.sound.status().enabled,false);assert.equal(f.notes.length,0);assert.equal(f.timers.size,0);assert.equal(f.contexts[0].closed,1);
 }
});
test('native interruption and failed rendering release owned resources and allow explicit retry',async()=>{
 const f=fixture();await f.sound.enable();f.contexts[0].state='interrupted';f.contexts[0].onstatechange();assert.equal(f.sound.status().state,'blocked');assert.equal(f.contexts[0].closed,1);assert(await f.sound.enable());assert.equal(f.created,2);f.sound.close();
 const failed=fixture({failPlay:true});assert.equal(await failed.sound.enable(),false);assert.equal(failed.sound.status().state,'blocked');assert.equal(failed.contexts[0].closed,1);
});
test('page close is final until explicit BFCache reopen, which remains off',async()=>{
 const f=fixture();await f.sound.enable();f.sound.close();assert.equal(await f.sound.enable(),false);assert.equal(f.created,1);f.sound.reopen();f.tick();assert.equal(f.created,1);assert.equal(f.sound.status().state,'off');assert(await f.sound.enable());f.sound.close();
});
test('an older elapsed packet cannot reset the baseline to replay a counted outcome',async()=>{
 const f=fixture();await f.sound.enable();f.set(g('ski',{elapsed:2,ski:{gates_passed:1,event:{sequence:1,kind:'gate',age:0}}}));f.notes.length=0;
 f.set(g('ski',{elapsed:1}));f.set(g('ski',{elapsed:2,ski:{gates_passed:1,event:{sequence:1,kind:'gate',age:0}}}));assert.equal(f.notes.length,0);f.sound.close();
});
test('the actual game entry invokes the control and retires sound with its lifecycle',async()=>{
 const text=fs.readFileSync('homeplane/embodied/static/games.js','utf8'),nodes=new Map(),listeners={},docListeners={};
 const node=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',setAttribute(k,v){this[k]=v;}});return nodes.get(id);};
 let enabled=false,quiet=0,closed=0,reopened=0;
 const c=vm.createContext({$:node,gameSound:{status:()=>({enabled,pending:false,state:enabled?'on':'off'}),enable:async()=>{enabled=true;return true;},disable(){enabled=false;},quiet(){quiet++;},close(){closed++;},reopen(){reopened++;}},stageClosed:false,presentationClient:{release(){},close(){}},streamGeneration:0,reconnectTimer:null,commandPending:null,gameSocket:null,streamOpen:true,stageFrame:null,clearTimeout(){},cancelAnimationFrame(){},document:{hidden:true,querySelector:()=>null,addEventListener:(k,f)=>docListeners[k]=f},window:{addEventListener:(k,f)=>listeners[k]=f},updateHUD(){},scheduleStage(){},connect(){},performance:{now:()=>0}});
 vm.runInContext(text.slice(text.indexOf('function updateGameSound('),text.indexOf('function winterReady(')),c);
 await c.toggleGameSound();assert(enabled);await c.toggleGameSound();assert(!enabled);c.updateGameSound({enabled:true,pending:false});assert.equal(node('soundToggle')['aria-pressed'],'true');
 const action=text.slice(text.indexOf('function delegatedCall('),text.indexOf("for(const kind of ['click'"));vm.runInContext(action,c);c.delegatedCall({},'toggleGameSound()');await new Promise(setImmediate);assert(enabled);
 vm.runInContext(text.slice(text.indexOf("document.addEventListener('visibilitychange'"),text.indexOf('/* Canvas-only recording')),c);
 docListeners.visibilitychange();assert.equal(quiet,1);listeners.pagehide();assert.equal(closed,1);listeners.pageshow({persisted:true});assert.equal(reopened,1);
});

module.exports={fixture};
