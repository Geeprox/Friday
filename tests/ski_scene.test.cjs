const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),test=require('node:test');
const fingerprint=value=>require('node:crypto').createHash('sha256').update(JSON.stringify(value)).digest('hex');
function scene(){
 const calls=[],texture={save(){},restore(){},translate(){},scale(){},drawImage(){}},pattern={setTransform(m){calls.push(['uv',...m.values]);}};
 const c={save(){},restore(){},beginPath(){},closePath(){},moveTo(...p){calls.push(['move',...p]);},lineTo(...p){calls.push(['line',...p]);},quadraticCurveTo(...p){calls.push(['curve',...p]);},stroke(){},fill(){calls.push(['fill',this.fillStyle]);},fillRect(...p){calls.push(['snow',...p]);},drawImage(im,...p){calls.push([im.id,...p]);},createPattern(){return pattern;}};
 const context=vm.createContext({document:{createElement(){return{getContext:()=>texture};}},DOMMatrix:class{constructor(values){this.values=values;}}});
 vm.runInContext(fs.readFileSync('homeplane/embodied/static/ski-scene.js','utf8'),context);
 const api=context.createSkiScenery(c,{scene:{id:'background',src:'snow.png',complete:true,naturalWidth:1600,naturalHeight:900},props:{id:'tree',complete:true,naturalWidth:1600,naturalHeight:800}});
 return{...api,paint(g,at){calls.length=0;api.draw(g,at);return structuredClone(calls);}};
}
const game=(distance,status='playing')=>({mode:'ski',status,players:[{lateral:0}],ski:{distance,obstacles:[{x:0,z:12-distance*3}]}});
test('only observed progress moves the snow, tracks and trees; wall time does not',()=>{
 const s=scene(),g=game(0);s.receive(g,1000);const first=s.paint(g,1000);
 assert.deepEqual(s.paint(g,1400),first);
 const moved=s.paint(game(1),3000);
 assert.deepEqual(first.find(c=>c[0]==='background'),moved.find(c=>c[0]==='background'),'mountains and horizon are fixed');
 assert.notDeepEqual(first.filter(c=>c[0]==='uv'),moved.filter(c=>c[0]==='uv'),'snow UVs travel');
 assert.notDeepEqual(first.filter(c=>c[0]==='line'),moved.filter(c=>c[0]==='line'),'snow grooves travel');
 const obstacle=draws=>draws.filter(c=>c[0]==='tree').find(c=>Math.abs(c[5]+c[7]/2-800)<1e-6);
 const a=obstacle(first),b=obstacle(moved);assert(a&&b);
 assert(Math.abs(a[8]-350/(12+1.8)*3.1)<1e-8);assert(Math.abs(b[8]-350/(9+1.8)*3.1)<1e-8);
 const oldUv=first.find(c=>c[0]==='uv'),newUv=moved.find(c=>c[0]==='uv');
 const wrapped=((newUv[6]-oldUv[6]+3/18*512)%512+512)%512;
 assert(Math.min(wrapped,512-wrapped)<1e-8,'ground uses the same three world units as an obstacle');
});
test('packet interpolation is bounded by observations, pauses and missing input',()=>{
 const s=scene();s.receive(game(0),1000);s.receive(game(1),1100);
 const direct=scene();assert.deepEqual(s.paint(game(1),1150),direct.paint(game(.5),1150),'halfway between known positions');
 assert.deepEqual(s.paint(game(1),1400),direct.paint(game(1),1400),'never extrapolate past the newest observation');
 const paused=game(1,'paused');s.receive(paused,1500);const frame=s.paint(paused,1500);assert.deepEqual(s.paint(paused,5000),frame);
 s.receive(game(0),5100);assert.deepEqual(s.paint(game(0),5100),direct.paint(game(0),5100),'restarting a course does not tween backward');
});
test('actual pause packets preserve the rendered steering and snow position without player data',()=>{
 const[live,paused]=require('./game_scene_packets.cjs')('ski'),s=scene();
 s.receive(live,1000);const before=s.paint(live,1100);s.receive(paused,1150);
 assert.equal(fingerprint(s.paint(paused,1150)),fingerprint(before),'pause cannot recenter the camera');
 assert.equal(fingerprint(s.paint(paused,10000)),fingerprint(before),'paused geometry remains still');
});
test('a pause during interpolation freezes the displayed frame and a new scope discards it',()=>{
 const s=scene(),first=game(0),next=game(1);first.round_id=next.round_id=1;first.control_context=next.control_context='a'.repeat(32);
 first.players[0].lateral=.3;next.players[0].lateral=.7;
 s.receive(first,1000);s.receive(next,1100);const before=s.paint(next,1150);
 const paused={...next,status:'paused',players:[]};s.receive(paused,1160);
 assert.equal(fingerprint(s.paint(paused,1160)),fingerprint(before));
 const fresh={...paused,round_id:2};s.receive(fresh,1200);assert.equal(fingerprint(s.paint(fresh,1200)),fingerprint(scene().paint(fresh,1200)));
 const worker={...next,control_context:'b'.repeat(32),players:[{lateral:-.2}]};s.receive(worker,1250);
 assert.equal(fingerprint(s.paint(worker,1250)),fingerprint(scene().paint(worker,1250)));
});
test('illustrated gates share observed ground motion and their opening follows rule clearance',()=>{
 const s=scene(),g=game(0);g.ski.gates=[{x:0,z:24,width:.48}];
 s.receive(g,1000);const first=s.paint(g,1000),gate=draws=>draws.find(c=>c[0]==='tree'&&c[1]===1200&&c[2]===400);
 const a=gate(first);assert(a,'uses the existing illustrated arch');
 const next=game(1);next.ski.gates=[{x:0,z:21,width:.48}];s.receive(next,2000);const b=gate(s.paint(next,3000));
 assert(b[8]>a[8],'the same three world units bring the arch closer');
 assert.equal(fingerprint(s.paint(next,5000)),fingerprint(s.paint(next,6000)),'wall time cannot move a gate');
 const narrow=game(0);narrow.ski.gates=[{x:0,z:24,width:.34}];const n=gate(scene().paint(narrow,1000));
 assert(Math.abs(n[8]/a[8]-(.34+.1)/(.48+.1))<1e-9,'drawn opening tracks each section clearance');
});
test('a preview and the moving snow grooves go through the authored gate at its true width',()=>{
 const route=[[0,0,0],[6,-.48,.43],[14,.48,.34],[180,0,0]],s=scene();
 const first=game(0,'off');first.mode='off';first.ski.route=route;
 const gate=draws=>draws.find(c=>c[0]==='tree'&&c[1]===1200&&c[2]===400);
 const path=(draws,color)=>{
  const end=draws.findIndex(c=>c[0]==='fill'&&c[1]===color);
  assert(end>0);return draws.slice(draws.findLastIndex((c,i)=>i<end&&c[0]==='move'),end);
 };
 const measure=(g,at)=>{
  const draws=s.paint(g,at),z=(6-g.ski.distance)*3,scale=350/(z+1.8),y=335+scale*2.45;
  const x=800-.48*1.9*scale,arch=gate(draws);
  assert(arch);assert(Math.abs(arch[5]+arch[7]/2-x)<1e-8);
  const tracks=path(draws,'rgba(255,248,240,.24)').filter(p=>Math.abs(p[2]-y)<1e-8);
  assert.equal(tracks.length,2);assert(Math.abs((tracks[0][1]+tracks[1][1])/2-x)<1e-8);
  const crossing=path(draws,'rgba(100,77,56,.38)');
  assert(Math.abs(crossing[0][1]-(x-.43*1.9*scale))<1e-8);
  assert(Math.abs(crossing[1][1]-(x+.43*1.9*scale))<1e-8);
  return arch[8];
 };
 const before=measure(first,1000),next=game(3);next.ski.route=route;
 assert(measure(next,2000)>before);
 assert.notDeepEqual(path(s.paint(first,1000),'rgba(88,108,119,.23)'),
  path(s.paint(next,2000),'rgba(88,108,119,.23)'),'snow marks advance even on the straight approach');
 const finish=game(175);finish.ski.route=route;
 const end=s.paint(finish,3000);
 assert(end.some(c=>c[0]==='tree'&&c[1]===800&&c[2]===400),'the finish lodge approaches before completion');
 assert(!gate(end),'the finish line cannot be mistaken for another scoring gate');
});
