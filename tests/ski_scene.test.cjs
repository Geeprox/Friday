const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),test=require('node:test');
const fingerprint=value=>require('node:crypto').createHash('sha256').update(JSON.stringify(value)).digest('hex');
function scene(){
 const calls=[],texture={save(){},restore(){},translate(){},scale(){},drawImage(){}},pattern={setTransform(m){calls.push(['uv',...m.values]);}};
 const c={save(){},restore(){},beginPath(){},closePath(){},moveTo(...p){calls.push(['move',...p]);},lineTo(...p){calls.push(['line',...p]);},stroke(){},fill(){},fillRect(...p){calls.push(['snow',...p]);},drawImage(im,...p){calls.push([im.id,...p]);},createPattern(){return pattern;}};
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
