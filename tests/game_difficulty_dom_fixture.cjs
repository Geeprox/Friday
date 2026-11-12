/* Actual library component consuming original ASGI receipts, inert DOM only. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const{Element}=require('./games_library_harness.cjs');
const packet=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),walk=n=>[n,...n.children.flatMap(walk)],settle=()=>new Promise(setImmediate);
async function run({wrongReply=false,stageChange=false}={}){
 let catalogue=structuredClone(packet.catalogue),writes=[],interval;
 const root=new Element('main'),response=(value,status=200)=>({ok:status===200,status,json:async()=>structuredClone(value)});
 const context=vm.createContext({root,URL,Date,console,AbortController,AbortSignal,
  document:{createElement:t=>new Element(t),querySelector:()=>null,head:new Element('head'),body:new Element('body')},
  setInterval:fn=>{interval=fn;return 1;},clearInterval(){interval=null;},
  motionTile:()=>{const n=new Element('article');n.dispose=()=>{};return n;},mountPractice:()=>({dispose(){}}),
  mountWerewolf:()=>{throw Error('party was not selected');},mountKtv:()=>{throw Error('KTV was not selected');},
  fetch:async(path,options={})=>{
   if(options.method==='POST'){
    assert.equal(path,'/api/embodied/start');const data=JSON.parse(options.body);writes.push(data);
    assert.deepEqual(data,packet.command);
    const result=structuredClone(packet.started);if(wrongReply)result.game.difficulty='standard';
    catalogue={...catalogue,service:result};return response(result);
   }
   if(path==='/api/games')return response(catalogue);
   if(path==='/api/embodied/cameras')return response({cameras:[{id:packet.command.camera,room:'合成房间',name:'合成镜头'}]});
   if(path==='/api/games/ski/open')return response(packet.opened);
   throw Error('unowned read '+path);
  }});
 vm.runInContext(fs.readFileSync('homeplane/static/games-library.js','utf8').replace(/^import.*$/gm,'').replace('export function mountGames','function mountGames'),context);
 const owner=vm.runInContext('mountGames(root)',context),nodes=()=>walk(root),button=text=>nodes().find(n=>n.tagName==='button'&&n.textContent===text);
 await settle();button('林间滑雪').onclick();
 const choice=nodes().find(n=>n.tagName==='select');assert(choice);assert.equal(choice.children.length,3);
 choice.value=packet.command.difficulty;choice.onchange();assert.equal(writes.length,0,'selection alone cannot change an active round');
 const details=nodes().find(n=>n.className==='game-difficulty-note');assert.equal(details.textContent,packet.catalogue.difficulties.find(d=>d.id===packet.command.difficulty).description.ski);
 await button('重新开始').onclick();await settle();
 assert.equal(writes.length,1);
 if(wrongReply){assert(!nodes().some(n=>n.tagName==='iframe'));assert(root.textContent.includes('游戏状态已变化'));}
 else{
  assert(nodes().some(n=>n.tagName==='iframe'));
  if(stageChange)catalogue={...catalogue,service:packet.peer};
  button('返回详情').onclick();await settle();
  const expected=stageChange?packet.peer.game.difficulty:packet.command.difficulty;
  const label=packet.catalogue.difficulties.find(d=>d.id===expected).label;
  assert(button('进入游戏 · '+label));assert.equal(choice.value,expected,'the confirmed choice follows changes made inside the stage');
  const alternate=packet.command.difficulty==='challenge'?'easy':'challenge';choice.value=alternate;choice.onchange();
  assert(button('进入游戏 · '+label),'continue identifies the current difficulty, independent of the next requested round');
  await button('进入游戏 · '+label).onclick();await settle();assert.equal(writes.length,1,'continue does not apply pending difficulty');
 }
 owner.dispose();assert.equal(interval,null);assert.equal(root.children.length,0);
}
(async()=>{await run();await run({stageChange:true});await run({wrongReply:true});console.log(JSON.stringify({actual_asgi_receipts:true,selection_is_local:true,confirmed_difficulty:true,stage_choice_follows:true,continue_preserves_current:true,wrong_reply_rejected:true,disposed:true}));})().catch(e=>{console.error(e);process.exitCode=1;});
