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

test('preparation and point states hide the flight and the receiving marker',()=>{
 const s=scene();
 for(const current of [originalPacket('ready'),originalPacket('point'),game(.5,.5,'complete')]){
  s.receive(current,1000);
  const drawn=s.paint(current,1000);
  assert.equal(ball(drawn),undefined);
  assert(!drawn.some(c=>c[0]==='text'&&c[1]==='挥拍'));
 }
});

test('the stationary service marker tracks the designated server and survives reduced motion',()=>{
 const parked=originalPacket('serve');
 for(const server of [0,1]){
  const current=structuredClone(parked),s=scene(true);
  current.tennis.serve.player=server;
  current.tennis.ball.z=server===0?.12:.88;
  s.setPortrait(true);
  const drawn=s.paint(current,1000),label=`${server+1} 号 · 发球`;
  const callout=drawn.find(c=>c[0]==='text'&&c[1]===label);
  assert(callout);
  const ring=drawn.find(c=>c[0]==='ellipse');
  assert.equal(ring[2],840-current.tennis.ball.z*460);
  assert((callout[2]-ring[1])*(server===0?-1:1)>ring[3],'发球提示放在球拍外侧');
  assert.deepEqual(ball(s.paint(current,5000)),ball(drawn),'等待期间球保持在发球位置');
  assert(!s.paint({...current,status:'paused'},5050).some(c=>c[0]==='text'&&c[1]===label));
 }
});

test('the ball touches its shadow once between two observed frames and lifts again',()=>{
 const packet=originalPacket('launched'),s=scene(),z=packet.tennis.arc.bounce_z;
 const observed=depth=>{
  const sample=structuredClone(packet);
  sample.tennis.ball.z=depth;
  return sample;
 };
 const before=observed(z-.028),after=observed(z+.028);
 s.receive(before,1000);s.receive(after,1100);
 const lift=at=>{
  const drawn=s.paint(after,at),shadow=drawn.filter(item=>item[0]==='ellipse').at(-1);
  const depth=(840-shadow[2])/460;
  return shadow[2]+(47-depth*13)*.25-ball(drawn)[2];
 };
 assert(lift(1100)>12);
 assert(Math.abs(lift(1150))<1e-8);
 assert(lift(1200)>9);
 const displayed=ball(s.paint(after,1175));
 const unseenReturn={...originalPacket('hit'),round_id:packet.round_id,control_context:packet.control_context,status:'paused'};
 s.receive(unseenReturn,1190);
 assert.deepEqual(ball(s.paint(unseenReturn,1250)),displayed);
});

test('both landing circles use the source strip and the swing cue stays in front of the actors',()=>{
 for(const portrait of [false,true])for(const receiver of [0,1]){
  const s=scene(true),current=game(.5,.5),window=.14,target=.62;
  s.setPortrait(portrait);
  current.tennis.guide={active:true,receiver,target_x:target,reach:.2,seconds:.1,window,cue:'swing'};
  const draws=s.paint(current,1000),ring=draws.find(row=>row[0]==='ellipse');
  assert.equal(ring[2],840-(receiver?1-window:window)*460);
  const [kind,label,x]=draws.at(-1);
  assert.equal(kind,'text');assert.equal(label,'挥拍');
  assert(Math.abs(x-ring[1])>ring[3],'cue sits beside the landing circle');
  assert(draws.findIndex(row=>row[0]==='ball')<draws.length-1);
  assert(!s.paint({...current,status:'paused'},1050).some(row=>row[0]==='text'&&row[1]===label));
 }
});

const code=fs.readFileSync('homeplane/embodied/static/games.js','utf8');
const context=vm.createContext({});
vm.runInContext(code.slice(code.indexOf('function tennisPresentation('),code.indexOf('function updateTennisHUD(')),context);
function originalPacket(stage){
 return JSON.parse(execFileSync(process.env.FRIDAY_TEST_PYTHON||path.join(process.cwd(),'.venv/bin/python'),['-B','-c',`
import json
from homeplane.embodied.motion_games import MotionGames
game=MotionGames();game.reset('tennis')
players=[dict(lateral=0,last_swing=10,swing_direction=1,observation='fresh'),dict(lateral=0,last_swing=-100,swing_direction=-1,observation='fresh')]
if '${stage}' in {'serve','launched'}:
 game.round_pause=0
 for player in players:player.update(observed_wrists=2,foot_observation='fresh',arm_speed=0,last_swing=-100)
 for i in range(7):game.tennis(players,.05,10+i*.05)
 if '${stage}'=='launched':
  players[0]['last_swing']=10.4
  game.tennis(players,.05,10.4)
if '${stage}'=='hit':
 game.round_pause=0;game.ball.update(x=.5,z=.06,toward=0);game.tennis(players,.01,10)
if '${stage}' in {'point','position','unobserved'}:
 if '${stage}'=='unobserved':players[0]['observation']='brief_joint_gap'
 game.round_pause=0;game.ball.update(x=.88 if '${stage}'=='position' else .5,z=-.01,toward=0);game.tennis(players,.01,10)
print(json.dumps(game.publish(players,'playing','')))
`],{encoding:'utf8'}));
}
test('original rule packets connect serve, receiving and change of service to the HUD',()=>{
 const ready=context.tennisPresentation(originalPacket('ready'));
 assert.equal(ready.title,'1 号准备发球');assert(ready.instruction.startsWith('2 秒'));
 const serve=context.tennisPresentation(originalPacket('serve'));
 assert.equal(serve.title,'1 号发球');assert.equal(serve.instruction,'现在挥拍！');
 const launched=context.tennisPresentation(originalPacket('launched'));
 assert.equal(launched.title,'2 号接球');assert.equal(launched.event,'1 号 · 发球成功');
 const hit=context.tennisPresentation(originalPacket('hit'));
 assert.equal(hit.title,'2 号接球');assert.equal(hit.event,'1 号 · 漂亮回击');
 const point=context.tennisPresentation(originalPacket('point'));
 assert.equal(point.title,'2 号得分');assert.equal(point.instruction,'2 号准备发球 · 2 秒');assert.equal(point.event,'1 号 · 看到挥拍提示再出手');
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
 const scoreboard=node('score');assert.match(scoreboard.innerHTML,/data-player="1" data-serving="true"/);
 const replacement=originalPacket('point');assert.notEqual(replacement.control_context,packet.control_context);
 assert.equal(replacement.round_id,packet.round_id);assert.equal(replacement.tennis.event.sequence,packet.tennis.event.sequence);
 hud.updateTennisHUD(replacement);assert.equal(feedback.textContent,'1 号 · 看到挥拍提示再出手','a new source must not retain the old source\'s coaching');
 hud.updateTennisHUD({...replacement,status:'paused'});assert.equal(feedback.hidden,true);
 assert.doesNotMatch(scoreboard.innerHTML,/data-(receiving|serving)/);
 hud.updateTennisHUD(originalPacket('ready'));assert.equal(feedback.hidden,true);assert.equal(feedback.textContent,'');assert.equal(feedback.dataset.event,'');
});

test('the stage presents fixed player numbers and does not compete with a recovery prompt',()=>{
 const {makeClient,part,game:stageGame}=require('./game_stage_harness.cjs');
 const {c}=makeClient({selected:'tennis',teachingWaitSince:null});
 vm.runInContext('function modeDescription(){return ["", "双人网球", ""]}',c);
 vm.runInContext(part('function gamePhaseMessage(', '\nconst artRoot'),c);
 const initial=stageGame({mode:'off',selected_mode:'tennis'});
 const intro=c.stageOverlayMarkup(initial,true,false);
 assert.match(intro,/双人对打.*两位玩家并排站好，双脚入镜。左侧先发球。/);
 assert.match(intro,/data-click="startGame\(\)"/);
 const practice=c.stageOverlayMarkup({...initial,tennis_variant:'practice'},true,false);
 assert.match(practice,/data-tennis-variant="practice" aria-pressed="true">单人练习/);
 assert.match(practice,/陪练发 9 球/);
 const waiting={...initial,mode:'tennis',status:'waiting',tennis:{lineup:['missing','ready']}};
 const waitingUI=c.stageOverlayMarkup(waiting,true,true);
 assert.match(waitingUI,/1 号玩家<\/dt><dd>请回到画面.*2 号玩家<\/dt><dd>已就位/);
 const hands={...waiting,status:'paused',tennis:{lineup:['ready','hands']}};
 assert.match(c.stageOverlayMarkup(hands,true,true),/1 号玩家<\/dt><dd>已就位.*2 号玩家<\/dt><dd>让挥拍手入镜/);
 assert.doesNotMatch(c.stageOverlayMarkup({...waiting,source_recovery:{required:true,phase:'camera'}},true,true),/tennis-lineup/);
});
test('practice shows one player and labels each confirmed outcome without using match points',()=>{
 const current=game(.5,.5);current.tennis_variant='practice';
 Object.assign(current.tennis,{practice:{balls:9,results:[true,false,true],feed_x:.26},
  guide:{active:true,receiver:0,target_x:.26,reach:.2,seconds:.1,window:.1,cue:'swing'}});
 const score={dataset:{}};context.tennisPracticeScore(current.tennis,current,score);
 assert.match(score.innerHTML,/命中<\/small><b>2<span> \/ 9/);
 assert.match(score.innerHTML,/第 1 球，命中.*第 2 球，未命中.*第 4 球，接球中/);
 assert.equal(context.tennisPresentation(current).title,'左右回球');
 const names=scene(true).paint(current,1000).filter(item=>item[0]==='text').map(item=>item[1]);
 assert.deepEqual(names,['陪练','你','挥拍']);
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
