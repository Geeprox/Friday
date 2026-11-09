const assert=require('node:assert/strict'),test=require('node:test');
const{scene}=require('./tennis_scene_harness.cjs');
const source=require('./tennis_animation_packets.cjs');
const pose=(draws,i=0)=>draws.find(x=>x[0]==='player'&&x[1]===i)?.at(-1);
function packet(swings=0,seconds=2){return{mode:'tennis',status:'playing',round_id:1,control_context:'a'.repeat(32),
 players:[{lateral:0,swings,observation:'fresh'},{lateral:0,swings:0,observation:'fresh'}],
 tennis:{phase:'rally',flight:0,ball:{x:.5,z:.5},guide:{active:true,receiver:0,target_x:.5,reach:.2,in_position:true,seconds},score:[0,0]}};}
for(const receiver of [0,1])test(`actual recognized swing animates player ${receiver+1} through impact and recovery without changing the rally`,()=>{
 const s=scene(),p=source(receiver),copy=structuredClone(p);
 s.receive(p.before,1000);const prepare=pose(s.paint(p.before,1000),receiver);assert(prepare>=1&&prepare<=4,'known incoming flight prepares the receiver');
 s.receive(p.lead,1020);assert.equal(pose(s.paint(p.lead,1020),receiver),5,'observed swing reaches contact immediately, before a rule hit is known');
 assert.equal(p.lead.tennis.rally,0,'pose acknowledgement is separate from a successful return');
 s.receive(p.hit,1140);const first=pose(s.paint(p.hit,1140),receiver);assert(first>5&&first<11);
 const later=pose(s.paint(p.hit,1260),receiver);assert(later>first&&later<=11);
 s.paint(p.hit,1380);assert.equal(pose(s.paint(p.hit,1440),receiver),0,'recovered non-receiver returns to ready');
 assert.deepEqual(p,copy,'art cannot mutate actual rule packets or scoring');
});
test('source guide prepares only its positioned receiver; reduced motion skips anticipation',()=>{
 const s=scene();
 for(const [seconds,expected]of [[2,0],[.6,1],[.4,2],[.2,3],[.01,4]]){
  const g=packet(0,seconds);s.receive(g,1000);const drawn=s.paint(g,1000);assert.equal(pose(drawn),expected);assert.equal(pose(drawn,1),0);
 }
 const wrong=packet(0,.1);wrong.tennis.guide.in_position=false;s.receive(wrong,1020);assert.equal(pose(s.paint(wrong,1020)),0);
 const still=scene(true),g=packet(0,.1);still.receive(g,1000);assert.equal(pose(still.paint(g,1000)),0);
 const swung=packet(1,.1);still.receive(swung,1020);assert.equal(pose(still.paint(swung,1020)),5);
 assert.equal(pose(still.paint(swung,1140)),0,'bounded impact confirmation remains, without a moving sequence');
});
test('joining a live game, stale counts, joint gaps and a new context cannot replay a past swing',()=>{
 const s=scene(),first=packet(30);s.receive(first,1000);assert.equal(pose(s.paint(first,1000)),0);
 const missed=packet(31);missed.players[0].observation='brief_joint_gap';s.receive(missed,1020);assert.equal(pose(s.paint(missed,1020)),0);
 const fresh=packet(31);s.receive(fresh,1040);assert.equal(pose(s.paint(fresh,1040)),0,'cached counts are not new input');
 const late=packet(32);s.receive(late,2000);assert.equal(pose(s.paint(late,2000)),0,'a source gap cannot queue a previous stroke');
 const next=packet(33);next.control_context='b'.repeat(32);s.receive(next,2020);assert.equal(pose(s.paint(next,2020)),0);
 const replay={...next,round_id:2};s.receive(replay,2040);assert.equal(pose(s.paint(replay,2040)),0);
});
test('pause freezes the exact cell; resume finishes the held stroke and ignores swings made while paused',()=>{
 const s=scene(),first=packet();s.receive(first,1000);s.paint(first,1000);
 const swing=packet(1);s.receive(swing,1020);s.paint(swing,1020);const before=s.paint(swing,1080);
 const paused={...swing,status:'paused',players:[]};s.receive(paused,1100);
 const actors=draws=>draws.filter(x=>x[0]==='player');assert.deepEqual(actors(s.paint(paused,1100)),actors(before));
 assert.deepEqual(actors(s.paint(paused,8000)),actors(before));
 const resumed=packet(8);s.receive(resumed,8020);assert.deepEqual(actors(s.paint(resumed,8020)),actors(before));
 assert(pose(s.paint(resumed,8080))>pose(before),'continue the existing admitted sequence, do not replay contact');
});
test('hidden-time discontinuity and delayed first paint discard a queued visual stroke',()=>{
 for(const delayed of [false,true]){
  const s=scene(),a=packet(),b=packet(1);s.receive(a,1000);s.paint(a,1000);s.receive(b,1020);
  if(!delayed)s.paint(b,1020);
  assert.equal(pose(s.paint(b,5000)),0,'no old stroke resumes after an unseen gap');
 }
});
test('both pose sheets participate in loading, error and explicit retry, without loading them for other games',()=>{
 const s=scene(false,{pending:['tennis-near-v1.png','tennis-far-v1.png']});s.prepare('hiit');assert(!s.images.has('tennis-near-v1.png'));
 s.prepare('tennis');assert.equal(s.ready('tennis'),false);
 s.finish('tennis-near-v1.png');assert.equal(s.ready('tennis'),false);s.finish('tennis-far-v1.png',true);assert.equal(s.failed('tennis'),true);
 s.reload('tennis');assert.equal(s.failed('tennis'),false);s.finish('tennis-near-v1.png');s.finish('tennis-far-v1.png');assert.equal(s.ready('tennis'),true);
 assert(s.images.get('tennis-far-v1.png').url.includes('?retry='));
});

test('actual binary metadata admission reaches the original scene, and a retired socket cannot swing the actor',()=>{
 const {makeClient,packet:wire}=require('./game_stage_harness.cjs');
 const s=scene(),p=source(),owner=makeClient({scenery:s,meta:null,selected:'tennis',lastBackendMode:'tennis',lastBackendRound:'tennis'}),c=owner.c;
 c.connect();const socket=owner.sockets[0];socket.onopen();
 owner.setNow(1000);socket.onmessage({data:wire(p.before)});assert.equal(c.gameViewCurrent(),true);
 assert(pose(s.paint(c.presentedGame(),1000))>0);
 owner.setNow(1020);socket.onmessage({data:wire(p.lead)});assert.equal(pose(s.paint(c.presentedGame(),1020)),5);
 c.reconnectGame();const other=owner.sockets[1];other.onopen();owner.setNow(1040);
 const retired=structuredClone(p.lead);retired.players[0].swings+=1;socket.onmessage({data:wire(retired)});
 assert.equal(c.gameViewCurrent(),false);assert.equal(c.meta.game.players[0].swings,p.lead.players[0].swings);
 assert.equal(pose(s.paint(c.presentedGame(),1040)),5);assert.equal(pose(s.paint(c.presentedGame(),5000)),5,'source loss holds the displayed cel');
});
