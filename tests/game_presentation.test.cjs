const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const {makeClient,game}=require('./game_stage_harness.cjs');
const settle=()=>new Promise(setImmediate),source=fs.readFileSync('homeplane/embodied/static/game-presentation.js','utf8');
function fixture({defer=false}={}){
 let now=0,visible=true,assets=false,current=true,index=0;const calls=[],timers=new Map();
 const g=game({presentation:{required:true,ready:false,context:'a'.repeat(32),window:'b'.repeat(32)}});
 const scope=vm.createContext({AbortController,performance:{now:()=>now}});vm.runInContext(source,scope);
 const reply=body=>({accepted:true,view_id:body.view_id,sequence:body.sequence,expected_context:body.expected_context,expected_round:body.expected_round,expected_view:body.expected_view,mode:body.mode,view_ready:body.ready,ready:body.ready});
 const owner=scope.createGamePresentation({state:()=>g,current:()=>current,visible:()=>visible,assetsReady:()=>assets,clock:()=>now,
  randomId:()=>String(++index).padStart(32,'0'),schedule:fn=>{timers.set(++index,fn);return index;},cancel:id=>timers.delete(id),
  send(body,options){let resolve;const promise=defer?new Promise(done=>{resolve=done;}):Promise.resolve(reply(body));calls.push({body,options,resolve:()=>resolve?.(reply(body))});return promise;}});
 return{owner,g,calls,timers,reply,setNow:value=>now=value,setAssets:value=>assets=value,setVisible:value=>visible=value,setCurrent:value=>current=value};
}

test('display readiness waits for the actual asset state and its exact server reply',async()=>{
 const f=fixture();f.owner.tick();await settle();assert.equal(f.calls[0].body.ready,false);assert.equal(f.owner.ready(),false);
 f.setAssets(true);f.owner.tick();await settle();assert.equal(f.calls[1].body.ready,true);assert.equal(f.owner.ready(),true);
 f.owner.close();await settle();assert.equal(f.owner.ready(),false);assert.equal(f.calls.at(-1).body.visible,false);assert.equal(f.timers.size,0);
});

test('hidden/unready reports supersede pending positives without an older reply enabling play',async()=>{
 const f=fixture({defer:true});f.setAssets(true);f.owner.tick();await settle();const old=f.calls[0];
 f.setAssets(false);f.owner.tick();await settle();const newer=f.calls[1];assert(newer.body.sequence>old.body.sequence);assert.equal(newer.body.ready,false);assert(old.options.signal.aborted);
 old.resolve();await settle();assert.equal(f.owner.ready(),false);newer.resolve();await settle();assert.equal(f.owner.ready(),false);
 f.setVisible(false);f.owner.tick();await settle();const release=f.calls.at(-1);assert.equal(release.body.visible,false);release.resolve();await settle();assert.equal(f.timers.size,0);f.owner.close();
});

test('an old-round reply and an expired request cannot revive a retired screen',async()=>{
 const f=fixture({defer:true});f.setAssets(true);f.owner.tick();await settle();const old=f.calls[0];
 f.g.round_id=2;f.owner.tick();await settle();assert.equal(f.calls.at(-1).body.expected_round,2);old.resolve();await settle();assert.equal(f.owner.ready(),false);
 const active=f.calls.at(-1);active.resolve();await settle();assert.equal(f.owner.ready(),true);
 f.setNow(800);f.owner.tick();await settle();const pending=f.calls.at(-1);f.setNow(2100);[...f.timers.values()].at(-1)();assert(pending.options.signal.aborted);assert.equal(f.owner.ready(),false);
 f.g.presentation.window='c'.repeat(32);f.owner.tick();await settle();pending.resolve();await settle();assert.equal(f.owner.ready(),false);
 f.owner.close();for(const call of f.calls)call.resolve();await settle();assert.equal(f.timers.size,0);
});

test('the original stage never posts start while artwork is unavailable, and stop stays usable',async()=>{
 let ready=false;const requests=[];
 const f=makeClient({scenery:{receive(){},ready:()=>ready},fetch:async(path,request)=>{requests.push(JSON.parse(request.body));const body=requests.at(-1);return{ok:true,json:async()=>game({mode:body.mode,selected_mode:body.mode==='off'?'quest':body.mode,control_revision:2})};}});
 f.c.syncGameControls();assert.equal(f.node('start').disabled,true);assert.equal(await f.c.startGame(),false);assert.equal(requests.length,0);
 await f.c.stopGame();assert.equal(requests.length,1);assert.equal(requests[0].mode,'off');
 f.c.meta={game:game()};ready=true;f.c.syncGameControls();assert.equal(f.node('start').disabled,false);
});

test('a new exercise presentation retires the old instruction inside the same round',async()=>{
 const f=fixture({defer:true});f.setAssets(true);f.owner.tick();await settle();const old=f.calls[0];
 f.g.presentation.context='e'.repeat(32);f.owner.tick();await settle();old.resolve();await settle();assert.equal(f.owner.ready(),false);
 const latest=f.calls.at(-1);assert.equal(latest.body.expected_view,'e'.repeat(32));assert.equal(latest.body.sequence,0);latest.resolve();await settle();assert.equal(f.owner.ready(),true);
 f.owner.close();for(const call of f.calls)call.resolve();await settle();assert.equal(f.timers.size,0);
});

test('a required but incomplete display context cannot be treated as ready',async()=>{
 const f=fixture();f.setAssets(true);f.owner.tick();await settle();assert.equal(f.owner.ready(),true);
 f.g.presentation.context='invalid';assert.equal(f.owner.ready(),false);f.owner.tick();await settle();assert.equal(f.calls.at(-1).body.visible,false);
 f.owner.close();assert.equal(f.timers.size,0);
});
