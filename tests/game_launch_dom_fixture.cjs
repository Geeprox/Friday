/* Original library driven by original ASGI receipts; no browser/network effects. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const{Element}=require('./games_library_harness.cjs');
const packet=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const walk=n=>[n,...n.children.flatMap(walk)],settle=()=>new Promise(setImmediate);
(async()=>{
 let catalogue=packet.catalogue,tick,writes=0;
 const root=new Element('main'),response=data=>({ok:true,json:async()=>structuredClone(data)});
 const context=vm.createContext({root,URL,Date,console,AbortController,AbortSignal,
  document:{get activeElement(){return Element.active;},createElement:t=>new Element(t),querySelector:()=>null,head:new Element('head'),body:new Element('body')},
  setInterval:fn=>{tick=fn;return 1;},clearInterval(){tick=null;},
  motionTile:()=>{const n=new Element('article');n.dispose=()=>{};return n;},mountPractice:()=>({dispose(){}}),
  mountWerewolf:()=>{throw Error('unselected party');},mountKtv:()=>{throw Error('unselected KTV');},
  fetch:async(path,options={})=>{
   if(options.method==='POST'){
    assert.equal(path,'/api/embodied/start');assert.deepEqual(JSON.parse(options.body),packet.command);writes++;
    catalogue={...catalogue,service:packet.started};return response(packet.started);
   }
   if(path==='/api/games')return response(catalogue);
   if(path==='/api/embodied/cameras')return response({cameras:[{id:packet.command.camera,room:'合成房间',name:'合成镜头'}]});
   if(path==='/api/games/ski/open')return response(packet.opened);
   throw Error('unowned read '+path);
  }});
 vm.runInContext(fs.readFileSync('homeplane/static/games-library.js','utf8').replace(/^import.*$/gm,'').replace('export function mountGames','function mountGames'),context);
 const owner=vm.runInContext('mountGames(root)',context),nodes=()=>walk(root),button=text=>nodes().find(n=>n.tagName==='button'&&n.textContent===text);
 await settle();assert.equal(packet.catalogue.service.owned,false);button('林间滑雪').onclick();
 const trail=id=>nodes().find(n=>n.dataset.skiTrail===id);
 assert.equal(trail('summit').getAttribute('aria-pressed'),'true');
 trail(packet.command.ski_trail).onclick();assert.equal(writes,0);
 await button('开始游戏').onclick();await settle();
 assert.equal(writes,1);assert(nodes().some(n=>n.tagName==='iframe'),'first launch must retain its frame');
 assert.equal(nodes().find(n=>n.tagName==='dialog').open,true);
 catalogue=packet.stopped;tick();await settle();
 assert(!nodes().some(n=>n.tagName==='iframe'));const exit=button('返回游戏详情');assert(exit);assert.equal(Element.active,exit);
 exit.onclick();await settle();assert.equal(button('开始游戏').disabled,false);assert.equal(writes,1);
 assert.equal(Element.active.tagName,'h2');owner.dispose();assert.equal(tick,null);assert.equal(root.children.length,0);
 console.log(JSON.stringify({actual_asgi_receipts:true,first_launch_retained:true,confirmed_stop_exit:true,focus_restored:true,no_implicit_restart:true,disposed:true}));
})().catch(e=>{console.error(e);process.exitCode=1;});
