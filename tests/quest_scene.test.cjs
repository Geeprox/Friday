/* Canvas command/state checks only: no browser, camera, model or device. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const test=require('node:test'),{execFileSync}=require('node:child_process');
const source=fs.readFileSync('homeplane/embodied/static/game-scenes.js','utf8');
const stages=JSON.parse(execFileSync('.venv/bin/python',['-B','-c','import json;from homeplane.embodied.quest import STAGES;print(json.dumps([s.public() for s in STAGES]))'],{encoding:'utf8'}));
const contextId='a'.repeat(32);
function game(level,extra={}){
 return{mode:'quest',status:level===stages.length?'complete':'playing',round_id:1,control_context:contextId,
  quest:{level,stages,phase:level===stages.length?'complete':'work',reps:0,hold:0,...extra}};
}
function scene({reduced=false,autoLoad=true}={}){
 const calls=[],stack=[],media={matches:reduced},images=new Map();
 let transform=[1,0,0,1,0,0];
 const target={globalAlpha:1};
 const c=new Proxy(target,{get(obj,key){
  if(key in obj)return obj[key];
  if(key==='save')return()=>stack.push({alpha:obj.globalAlpha,transform:[...transform]});
  if(key==='restore')return()=>{const old=stack.pop();obj.globalAlpha=old.alpha;transform=old.transform;};
  if(key==='translate')return(x,y)=>{transform[4]+=transform[0]*x+transform[2]*y;transform[5]+=transform[1]*x+transform[3]*y;};
  if(key==='scale')return(x,y)=>{transform[0]*=x;transform[1]*=x;transform[2]*=y;transform[3]*=y;};
  if(key==='rotate')return(angle)=>{const[a,b,d,e,x,y]=transform,cos=Math.cos(angle),sin=Math.sin(angle);transform=[a*cos+d*sin,b*cos+e*sin,d*cos-a*sin,e*cos-b*sin,x,y];};
  if(key==='drawImage')return(image,...args)=>{
   const sprite=args.length===8&&image.name==='quest-props.png'?Math.floor(args[0]/(1774/4))+4*Math.floor(args[1]/(887/2)):null;
   calls.push({asset:image.name,sprite,args:[...args],transform:[...transform],alpha:obj.globalAlpha});
  };
  return()=>{};
 }});
 class Image{
  constructor(){this.complete=false;this.naturalWidth=this.naturalHeight=0;}
  set src(value){this.url=value;this.name=new URL(value).pathname.split('/').at(-1);images.set(this.name,this);this.complete=autoLoad;this.naturalWidth=autoLoad?(this.name==='quest-props.png'?1774:1672):0;this.naturalHeight=autoLoad?(this.name==='quest-props.png'?887:941):0;}
  get src(){return this.url;}
 }
 const scope=vm.createContext({Image,URL,matchMedia:()=>media});
 vm.runInContext(source,scope);
 const api=scope.createGameScenery(c,{base:'https://example.invalid/assets/'});api.prepare('quest');
 const out={...api,images,media,world:g=>structuredClone(scope.questWorld(g)),
  paint(g,at){calls.length=0;api.adventure(g,at);assert.equal(stack.length,0);return structuredClone(calls.filter(call=>call.alpha>0));},
  tick(g,at){api.receive(g,at);return this.paint(g,at);},
  settle(g,at){this.tick(g,at);let frame;for(let t=at+50;t<=at+600;t+=50)frame=this.paint(g,t);return frame;}};
 return out;
}
const sprites=(draws,index)=>draws.filter(call=>call.sprite===index&&call.alpha>.99);
const plate=draws=>draws.filter(call=>call.sprite===null&&call.asset.endsWith('scene.png')).at(-1)?.asset;

if(process.argv.includes('--packets')){
 const packets=JSON.parse(fs.readFileSync(0,'utf8')),s=scene();let at=1000,checked=0;
 for(const g of packets){
  const before=structuredClone(g),draws=s.settle(g,at),q=g.quest,current=q.stage||q.stages.at(-1),world=s.world(g);
  assert.equal(plate(draws),{forest:'summer-scene.png',stream:'quest-stream-scene.png',camp:'quest-camp-scene.png'}[current.scene]);
  const count=action=>q.history.find(item=>item.action===action)?.reps||
   (q.stage?.action===action&&q.phase==='work'?q.reps:0);
  if(world.scene==='stream'){
   assert.equal(sprites(draws,0).length,count('side_step'));
   assert.equal(sprites(draws,1).length,count('high_knee'));
   assert.equal(sprites(draws,5).length,count('jack'));
  }
  if(world.scene==='camp'){
   assert.equal(sprites(draws,0).length,count('lunge'));
   assert.equal(sprites(draws,4).length,count('reach_up'));
   assert.equal(sprites(draws,6).length,count('arm_open'));
   assert.equal(sprites(draws,7).length,q.phase==='complete'?1:0);
  }
  for(const draw of draws.filter(call=>call.sprite!==null)){
   const[,,,,x,y,w,h]=draw.args,[a,b,c,d,tx,ty]=draw.transform;
   const corners=[[x,y],[x+w,y],[x,y+h],[x+w,y+h]].map(([px,py])=>[a*px+c*py+tx,b*px+d*py+ty]);
   assert(corners.every(([px,py])=>px>=320&&px<=1280&&py>=0&&py<=900),JSON.stringify({difficulty:g.difficulty,level:q.level,sprite:draw.sprite,corners}));
  }
  assert.deepEqual(g,before,'scenery changed authoritative rule state');at+=1000;checked++;
 }
 console.log(JSON.stringify({original_rule_packets:checked,completed:packets.some(g=>g.quest.phase==='complete'),replayed:packets.at(-1).quest.level===0}));
}else{
 test('each source chapter loads its own scene and only confirmed cycles build its objects',()=>{
  for(const[index,name,sprite]of [[3,'quest-stream-scene.png',0],[4,'quest-stream-scene.png',1],[8,'quest-camp-scene.png',4],[9,'quest-camp-scene.png',6]]){
   const s=scene();let before=s.settle(game(index),1000);assert.equal(plate(before),name);assert.equal(sprites(before,sprite).length,0);
   const current=game(index,{reps:1});const after=s.settle(current,2000);assert.equal(sprites(after,sprite).length,1);
   assert.deepEqual(s.settle(current,3000),after,'repeated reads replayed a reward');
   assert.equal(sprites(s.settle(game(index,{phase:'transition',reps:0}),4000),sprite).length,0);
  }
 });
 test('remaining branches clear one by one while completed flowers stay',()=>{
  const s=scene();const before=s.settle(game(2),1000),one=s.settle(game(2,{reps:1}),2000);
  assert.equal(sprites(before,2).length,3);assert.equal(sprites(before,5).length,0);
  assert.equal(sprites(one,2).length,2);assert.equal(sprites(one,5).length,1);
 });
 test('all full props stay within the shared portrait crop throughout the source route',()=>{
  const s=scene();let at=1000;
  for(let level=0;level<=stages.length;level++)for(const fraction of [0,.5,1]){
   const stage=stages[level],current=game(level,{reps:Math.floor((stage?.reps||0)*fraction),hold:(stage?.hold||0)*fraction});
   const draws=s.settle(current,at);at+=1000;
   for(const draw of draws.filter(call=>call.sprite!==null)){
    const[,,,,x,y,w,h]=draw.args,[a,b,c,d,tx,ty]=draw.transform;
    const corners=[[x,y],[x+w,y],[x,y+h],[x+w,y+h]].map(([px,py])=>[a*px+c*py+tx,b*px+d*py+ty]);
    assert(corners.every(([px,py])=>px>=320&&px<=1280&&py>=0&&py<=900),JSON.stringify({level,sprite:draw.sprite,corners}));
   }
  }
 });
 test('pause and hidden time freeze earned settlement and chapter continuity',()=>{
  const s=scene(),first=game(3),earned=game(3,{reps:1});s.tick(first,1000);s.paint(first,1050);s.tick(earned,1100);
  const moving=s.paint(earned,1150),paused={...earned,status:'paused',quest:{...earned.quest,reps:0}};
  assert.deepEqual(s.tick(paused,1200),moving);assert.deepEqual(s.paint(paused,10000),moving);
  assert.deepEqual(s.tick(earned,10050),moving,'resume spent suspended time');
  assert.deepEqual(s.paint(earned,30000),moving,'hidden interval spent wall time');
  assert.notDeepEqual(s.paint(earned,30050),moving,'visible feedback never continued');
  s.settle(game(2,{reps:2}),31000);const next=game(3,{phase:'transition'});s.tick(next,31700);
  const during=s.paint(next,31750),held={...next,status:'paused'};
  assert.equal(during.filter(draw=>draw.sprite===null).length,2,'chapter dissolve was not exercised');
  assert.deepEqual(s.tick(held,31800),during);assert.deepEqual(s.paint(held,40000),during);
  assert.deepEqual(s.tick(next,40050),during);assert.equal(plate(s.settle(next,41000)),'quest-stream-scene.png');
 });
 test('new context/round clears old rewards and joining a completed route does not replay them',()=>{
  const s=scene();s.settle(game(9,{reps:3}),1000);s.tick(game(10),2000);
  const fresh={...game(3),round_id:2};const first=s.tick(fresh,2050);
  assert.equal(sprites(first,4).length,0);assert.equal(sprites(first,6).length,0);assert.equal(sprites(first,7).length,0);
  assert.equal(plate(first),'quest-stream-scene.png');
  const other={...game(8),round_id:2,control_context:'b'.repeat(32)};
  assert.equal(sprites(s.tick(other,2100),4).length,0);
  const joined=scene(),done=game(10),direct=joined.tick(done,1000);
  assert.equal(sprites(direct,4).length,4);assert.equal(sprites(direct,6).length,4);assert.equal(sprites(direct,7).length,1);
  assert.deepEqual(joined.paint(done,1050),direct);
 });
 test('returning to a route advanced offscreen presents current rewards without replaying missed work',()=>{
  const s=scene();s.tick(game(3),1000);s.paint(game(3),1050);
  const current=game(8,{reps:3}),now=s.tick(current,30000);
  assert.equal(plate(now),'quest-camp-scene.png');assert.equal(sprites(now,4).length,3);
  assert.deepEqual(s.paint(current,30050),now);
 });
 test('reduced motion shows the same earned world immediately without spatial transitions',()=>{
  const s=scene({reduced:true});s.tick(game(2,{reps:2}),1000);
  const next=game(3,{reps:1}),instant=s.tick(next,1050);
  assert.equal(plate(instant),'quest-stream-scene.png');assert.equal(sprites(instant,0).length,1);
  assert.deepEqual(s.paint(next,1100),instant);
  const done=game(10),result=s.tick(done,1150);
  assert.equal(sprites(result,7).length,1);assert.deepEqual(s.paint(done,1200),result);
 });
 test('new chapter errors stay recoverable and retry covers the exact public art set',()=>{
  const s=scene({autoLoad:false});assert.equal(s.ready('quest'),false);
  const names=['summer-scene.png','quest-stream-scene.png','quest-camp-scene.png','quest-props.png'];
  assert.deepEqual([...s.images.keys()],names);
  for(const name of names){const im=s.images.get(name);im.complete=true;im.naturalWidth=100;im.onload();}
  assert.equal(s.ready('quest'),true);
  const missing=s.images.get('quest-camp-scene.png');missing.naturalWidth=0;missing.onerror();
  assert.equal(s.failed('quest'),true);assert.equal(s.ready('quest'),false);
  s.reload('quest');assert.equal(s.failed('quest'),false);assert.equal(s.ready('quest'),false);
  assert([...s.images.values()].every(im=>new URL(im.src).searchParams.has('retry')));
 });
 test('legacy three-station packets keep their actual forest route',()=>{
  const s=scene(),g={...game(2),quest:{level:2,reps:1,hold:0,targets:['horizontal','overhead','squat']}};
  const draws=s.settle(g,1000);assert.equal(plate(draws),'summer-scene.png');
  assert.equal(sprites(draws,2).length,2);assert.equal(sprites(draws,5).length,1);
 });
 test('an already-running ten-station backend retains its actual scene without the new art metadata',()=>{
  const s=scene(),g=game(8,{reps:2});
  g.quest.stages=stages.map(({scene,landmark,...stage})=>stage);
  const draws=s.settle(g,1000);assert.equal(plate(draws),'quest-camp-scene.png');
  assert.equal(sprites(draws,4).length,2);assert.equal(sprites(draws,0).length,4);
 });
}
