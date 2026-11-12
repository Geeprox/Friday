/* Real stage functions consume original worker receipts; no browser/media. */
const fs=require('node:fs'),assert=require('node:assert/strict');
const{Element}=require('./games_library_harness.cjs'),{makeClient}=require('./game_stage_harness.cjs');
const packet=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
function stage({wrongReply=false}={}){
 const nodes=new Map(),writes=[];
 const node=id=>{if(!nodes.has(id)){const n=new Element(id==='difficultySelect'?'select':'div');n.id=id;nodes.set(id,n);}return nodes.get(id);};
 const h=makeClient({meta:{game:structuredClone(packet.initial)},selected:'ski',$:node,
  document:{hidden:false,createElement:tag=>new Element(tag),querySelectorAll:()=>[],querySelector:()=>node('actions')},
  fetch:async(_path,options)=>{
   const body=JSON.parse(options.body);writes.push(body);
   const result=structuredClone(writes.length===1?packet.changed:writes.length===2?packet.started:packet.stopped);
   if(wrongReply)result.difficulty='challenge';return{ok:true,json:async()=>result};
  }});
 return{...h,node,writes};
}
(async()=>{
 const h=stage(),c=h.c,field=h.node('difficultySelect');c.syncGameControls();
 assert.equal(field.value,'standard');assert.equal(field.children.length,3);assert.equal(field.disabled,false);
 field.value='easy';assert.equal(await c.changeDifficulty(),true);
 assert.deepEqual(h.writes[0],packet.command);assert.equal(field.value,'easy');
 assert.equal(c.meta.game.difficulty,'easy');assert.equal(h.node('difficultySetting').hidden,false);
 await c.startGame();assert.equal(c.meta.game.difficulty,'easy');assert.equal(field.disabled,true);
 field.value='challenge';assert.equal(await c.changeDifficulty(),false);assert.equal(h.writes.length,2);assert.equal(field.value,'easy');
 await c.stopGame();assert.equal(c.meta.game.difficulty,'easy');assert.equal(c.meta.game.mode,'off');assert.equal(field.disabled,false);
 h.setNow(1600);field.value='challenge';assert.equal(await c.changeDifficulty(),false);assert.equal(h.writes.length,3);assert.equal(field.disabled,true);assert.equal(field.value,'easy');
 assert.equal(c.validGameState({...packet.initial,difficulty:'expert'}),false);
 const bad=stage({wrongReply:true});bad.c.syncGameControls();bad.node('difficultySelect').value='easy';
 assert.equal(await bad.c.changeDifficulty(),false);assert.equal(bad.c.meta.game.difficulty,'standard');assert.equal(bad.node('difficultySelect').value,'standard');
 console.log(JSON.stringify({actual_worker_receipts:true,confirmed_choice:true,replay_retains:true,active_change_rejected:true,stop_retains:true,stale_change_rejected:true,wrong_ack_rejected:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
