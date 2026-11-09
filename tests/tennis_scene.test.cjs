const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),test=require('node:test');
const {execFileSync}=require('node:child_process');
const path=require('node:path');

const {scene}=require('./tennis_scene_harness.cjs');

function game(x,z,phase='rally',round=1,flight=0){return{mode:'tennis',status:'playing',round_id:round,players:[],tennis:{phase,flight,ball:{x,z},score:[0,0]}};}
const ball=draws=>draws.find(c=>c[0]==='ball');

test('ball blends only between observed positions from the same flight',()=>{
 const s=scene(),first=game(.3,.7),second=game(.5,.6),copy=structuredClone(second);
 s.receive(first,1000);s.receive(second,1100);
 const direct=scene();
 const actual=ball(s.paint(second,1150)),expected=ball(direct.paint(game(.4,.65),1150));
 assert(actual.slice(1).every((value,i)=>Math.abs(value-expected[i+1])<1e-8));
 assert.deepEqual(ball(s.paint(second,5000)),ball(direct.paint(second,5000)),'never predicts a missing future packet');
 assert.deepEqual(second,copy,'drawing cannot mutate source state');
});

test('pause, new return, replay and reduced motion never tween through an old ball',()=>{
 const s=scene(),direct=scene(),first=game(.2,.7),second=game(.5,.6);
 s.receive(first,1000);s.receive(second,1100);
 for(const current of [{...second,status:'paused'},game(.5,.12,'rally',1,1),game(.5,.5,'rally',2)]){
  s.receive(current,1200);
  assert.deepEqual(ball(s.paint(current,1200)),ball(direct.paint(current,1200)));
  assert.deepEqual(ball(s.paint(current,5000)),ball(direct.paint(current,5000)));
 }
 const still=scene(true);still.receive(first,1000);still.receive(second,1100);
 assert.deepEqual(ball(still.paint(second,1100)),ball(direct.paint(second,1100)));
});

test('preparation and point states show a landing cue and no flying ball',()=>{
 const s=scene();
 for(const phase of ['ready','point','complete']){
  const current=game(.5,.5,phase);current.tennis.ready_seconds=1.5;
  current.tennis.guide={active:phase!=='complete',receiver:0,target_x:.25,reach:.2,seconds:.8,cue:'prepare'};
  s.receive(current,1000);
  const drawn=s.paint(current,1000);
  assert.equal(ball(drawn),undefined);
  if(phase!=='complete'){
   const ring=drawn.find(c=>c[0]==='ellipse');
   assert(Math.abs(ring[1]-(800+(.25-.5)*(1120-.1*580)))<1e-7,'marker uses the source intercept');
   assert(drawn.some(c=>c[0]==='text'&&c[1]==='2'),'source countdown, not a local timer');
  }
 }
});

const code=fs.readFileSync('homeplane/embodied/static/games.js','utf8');
const context=vm.createContext({});
vm.runInContext(code.slice(code.indexOf('function tennisPresentation('),code.indexOf('function updateTennisHUD(')),context);
function originalPacket(stage){
 return JSON.parse(execFileSync(path.join(process.cwd(),'.venv/bin/python'),['-B','-c',`
import json
from homeplane.embodied.motion_games import MotionGames
game=MotionGames();game.reset('tennis')
players=[dict(lateral=0,last_swing=10,swing_direction=1,observation='fresh'),dict(lateral=0,last_swing=-100,swing_direction=-1,observation='fresh')]
if '${stage}'=='hit':
 game.round_pause=0;game.ball.update(x=.5,z=.06,toward=0);game.tennis(players,.01,10)
if '${stage}' in {'point','position','unobserved'}:
 if '${stage}'=='unobserved':players[0]['observation']='brief_joint_gap'
 game.round_pause=0;game.ball.update(x=.88 if '${stage}'=='position' else .5,z=-.01,toward=0);game.tennis(players,.01,10)
print(json.dumps(game.publish(players,'playing','')))
`],{encoding:'utf8'}));
}
test('original rule packet reaches the hit acknowledgement and point/ready prompts',()=>{
 const ready=context.tennisPresentation(originalPacket('ready'));
 assert.equal(ready.title,'1 号准备接球');assert(ready.instruction.startsWith('2 秒'));
 const hit=context.tennisPresentation(originalPacket('hit'));
 assert.equal(hit.title,'2 号接球');assert.equal(hit.event,'1 号 · 漂亮回击');
 const point=context.tennisPresentation(originalPacket('point'));
 assert.equal(point.title,'2 号得分');assert(point.instruction.startsWith('下一球：1 号接球'));assert.equal(point.event,'1 号 · 等光圈收拢，再挥拍');
});
test('point coaching uses known source evidence and stays quiet when input was not observed',()=>{
 const packet=originalPacket('position');assert.equal(packet.tennis.event.next_focus,'position');
 assert.equal(context.tennisPresentation(packet).event,'1 号 · 先站进光圈，再挥拍');
 for(const current of [{...packet,status:'paused'},{...packet,tennis:{...packet.tennis,phase:'rally'}},
  {...packet,tennis:{...packet.tennis,event:{...packet.tennis.event,next_focus:null}}}]){
  assert.equal(context.tennisPresentation(current).event,'');
 }
 assert.equal(context.tennisPresentation(originalPacket('unobserved')).event,'');
});
test('the original HUD exposes coaching once, hides it during pause and clears it on replay',()=>{
 const nodes=new Map();
 function node(id){if(!nodes.has(id)){let text='',writes=0;nodes.set(id,{dataset:{},parentElement:{},classList:{toggle(){}},
  get textContent(){return text;},set textContent(value){text=value;writes++;},get writes(){return writes;}});}return nodes.get(id);}
 const hud=vm.createContext({$:node,selected:'tennis'});
 vm.runInContext(code.slice(code.indexOf('function tennisPresentation('),code.indexOf('function streamLost(')),hud);
 const packet=originalPacket('position');hud.updateTennisHUD(packet);
 const feedback=node('tennisEvent');assert.equal(feedback.hidden,false);assert.equal(feedback.textContent,'1 号 · 先站进光圈，再挥拍');
 const writes=feedback.writes;hud.updateTennisHUD(packet);assert.equal(feedback.writes,writes,'polling cannot repeat the same announcement');
 const replacement=originalPacket('point');assert.notEqual(replacement.control_context,packet.control_context);
 assert.equal(replacement.round_id,packet.round_id);assert.equal(replacement.tennis.event.sequence,packet.tennis.event.sequence);
 hud.updateTennisHUD(replacement);assert.equal(feedback.textContent,'1 号 · 等光圈收拢，再挥拍','a new source must not retain the old source\'s coaching');
 hud.updateTennisHUD({...replacement,status:'paused'});assert.equal(feedback.hidden,true);
 hud.updateTennisHUD(originalPacket('ready'));assert.equal(feedback.hidden,true);assert.equal(feedback.textContent,'');assert.equal(feedback.dataset.event,'');
});
test('old hit events and unknown completion never fabricate a new result',()=>{
 const packet=originalPacket('hit');packet.tennis.event.age=2;
 assert.equal(context.tennisPresentation(packet).event,'');
 packet.status='complete';packet.tennis.winner=null;
 assert.equal(context.tennisPresentation(packet).title,'双人网球');
 assert.equal(context.tennisPresentation(packet).instruction,'');
});
test('actual paused match packets retain the displayed players, rackets and ball without input',()=>{
 const[live,paused]=require('./game_scene_packets.cjs')('tennis'),s=scene();
 s.receive(live,1000);s.paint(live,1000);const before=s.paint(live,1100);
 s.receive(paused,1150);const first=s.paint(paused,1150),later=s.paint(paused,5000);
 const actors=draws=>draws.filter(c=>['player','ball'].includes(c[0]));
 assert.deepEqual(actors(first),actors(before),'pause cannot move either player or the ball');
 assert.deepEqual(actors(later),actors(before),'paused rackets cannot keep swinging');
});
test('a new worker with the same round number cannot inherit another match position',()=>{
 const s=scene(),first=game(.2,.7),fresh=game(.8,.3);first.control_context='a'.repeat(32);fresh.control_context='b'.repeat(32);
 first.players=[{lateral:.8},{lateral:-.8}];fresh.players=[];
 s.receive(first,1000);s.paint(first,1000);s.paint(first,1100);s.receive(fresh,1200);
 const direct=scene();direct.receive(fresh,1200);
 assert.deepEqual(s.paint(fresh,1200),direct.paint(fresh,1200));
});
