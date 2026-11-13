const {test}=require('node:test');
const assert=require('node:assert/strict');
const {makeClient,game,packet}=require('./game_stage_harness.cjs');
const vm=require('node:vm');
const {part}=require('./game_stage_harness.cjs');

test('a rejected mode selection preserves the current game and selected tab',async()=>{
 const h=makeClient({fetch:async()=>({ok:false})}),old=h.c.meta.game;
 await h.c.chooseMode('ski');assert.equal(h.c.selected,'quest');assert.equal(h.c.meta.game,old);
 assert.equal(h.node('gameNotice').textContent,'暂时无法切换游戏，请重试。');
});

test('a late stop reply cannot overwrite a newer round already received from the stream',async()=>{
 let finish;const h=makeClient({fetch:()=>new Promise(r=>finish=r)}),c=h.c;
 const pending=c.stopGame(),newer=game({mode:'ski',selected_mode:'ski',round_id:3,control_revision:3});
 c.meta={game:newer};c.selected='ski';finish({ok:true,json:async()=>game({mode:'off',round_id:2,control_revision:2})});
 await pending;assert.equal(c.meta.game,newer);assert.equal(c.selected,'ski');
});

test('confirmed selection consumes the bound response; double clicks make one request',async()=>{
 let finish;const requests=[],h=makeClient({fetch:(p,o)=>{requests.push(JSON.parse(o.body));return new Promise(r=>finish=r);}});
 const pending=h.c.chooseMode('ski');await h.c.startGame();assert.equal(requests.length,1);assert.equal(h.c.selected,'quest');
 assert.deepEqual(requests[0],{action:'select',mode:'ski',expected_context:'a'.repeat(32),expected_revision:1});
 finish({ok:true,json:async()=>game({mode:'off',selected_mode:'ski',round_id:2,control_revision:2})});
 await pending;assert.equal(h.c.selected,'ski');assert.equal(h.c.commandPending,null);assert.equal(h.node('start').disabled,false);
});
test('a tennis variant change binds the option and current revision without starting a round',async()=>{
 const old=game({mode:'off',selected_mode:'tennis',tennis_variant:'match'});
 const requests=[];
 const h=makeClient({selected:'tennis',meta:{game:old},fetch:async(_,options)=>{
  const body=JSON.parse(options.body);requests.push(body);
  return {ok:true,json:async()=>({...old,control_revision:2,round_id:2,
    tennis_variant:requests.length===1?'match':'practice'})};
 }});
 assert.equal(await h.c.chooseTennisVariant('practice'),false);
 assert.equal(h.c.meta.game,old);
 assert.equal(await h.c.chooseTennisVariant('practice'),true);
 assert.equal(h.c.meta.game.tennis_variant,'practice');
 assert.deepEqual(requests,[1,2].map(()=>({mode:'tennis',action:'select',tennis_variant:'practice',
  expected_context:'a'.repeat(32),expected_revision:1})));
 assert.equal(await h.c.chooseTennisVariant('practice'),false);
 h.c.meta.game={...h.c.meta.game,mode:'tennis',status:'playing'};
 assert.equal(await h.c.chooseTennisVariant('match'),false);
 assert.equal(requests.length,2);
});
test('a chapter changes only when the current scoped receipt confirms that chapter',async()=>{
 const routes=[{id:'forest',name:'林间入口',stations:3},{id:'stream',name:'溪畔步道',stations:4}];
 const old=game({status:'complete',quest_route:'forest',quest_routes:routes});
 const requests=[];
 const h=makeClient({meta:{game:old},fetch:async(_,options)=>{
  const body=JSON.parse(options.body);requests.push(body);
  return {ok:true,json:async()=>({...old,mode:'off',status:'off',control_revision:2,round_id:2,
   quest_route:requests.length===1?'forest':'stream'})};
 }});
 assert.equal(await h.c.chooseQuestRoute('stream'),false);
 assert.equal(h.c.meta.game,old);
 assert.equal(await h.c.chooseQuestRoute('stream'),true);
 assert.equal(h.c.meta.game.quest_route,'stream');
 assert.deepEqual(requests,[1,2].map(()=>({mode:'quest',action:'select',quest_route:'stream',
  expected_context:'a'.repeat(32),expected_revision:1})));
});
test('same-revision live score is newer than the HTTP transition snapshot',async()=>{
 let finish;const h=makeClient({fetch:()=>new Promise(r=>finish=r)}),pending=h.c.startGame();
 const live=game({round_id:2,control_revision:2,elapsed:2.3});h.c.meta={game:live};
 finish({ok:true,json:async()=>game({round_id:2,control_revision:2,status:'waiting',elapsed:0})});
 await pending;assert.equal(h.c.meta.game,live);
});
test('conflict and disconnected controls never retry or change the round',async()=>{
 let requests=0;const h=makeClient({fetch:async()=>{requests++;return {ok:false,status:409};}}),old=h.c.meta.game;
 await h.c.stopGame();assert.equal(requests,1);assert.equal(h.c.meta.game,old);
 h.c.streamOpen=false;await h.c.startGame();assert.equal(requests,1);assert.equal(h.c.meta.game,old);
});
test('request timeout releases busy controls without claiming the command succeeded',async()=>{
 const h=makeClient({fetch:(_,o)=>new Promise((_,reject)=>o.signal.addEventListener('abort',()=>reject(Error('timeout'))))}),old=h.c.meta.game;
 const pending=h.c.startGame();h.fire(5000);await pending;
 assert.equal(h.c.commandPending,null);assert.equal(h.c.meta.game,old);assert.equal(h.node('gameNotice').textContent,'暂时无法开始本局，请重试。');
});
test('a pause reply must confirm the requested pause state',async()=>{
 const h=makeClient({fetch:async()=>({ok:true,json:async()=>game({control_revision:2,manually_paused:false})})}),old=h.c.meta.game;
 await h.c.pauseGame();assert.equal(h.c.meta.game,old);assert.equal(h.node('gameNotice').textContent,'暂时无法暂停，请重试。');
});
test('new connection needs its own fresh packet and ignores retired socket messages',()=>{
 const h=makeClient(),c=h.c;c.connect();const first=h.sockets[0];first.onopen();
 assert.equal(c.gameViewCurrent(),false);first.onmessage({data:packet(game())});assert.equal(c.gameViewCurrent(),true);
 c.reconnectGame();const next=h.sockets[1];next.onopen();assert.equal(c.gameViewCurrent(),false);
 const before=c.meta;first.onmessage({data:packet(game({control_revision:999}))});assert.equal(c.meta,before);
 next.onmessage({data:packet(game({control_context:'b'.repeat(32)}))});assert.equal(c.gameViewCurrent(),true);
});
test('a lower revision frame cannot rewind score or refresh the current input age',()=>{
 const h=makeClient(),c=h.c;c.connect();const ws=h.sockets[0];ws.onopen();ws.onmessage({data:packet(game({control_revision:3,elapsed:9}))});
 const live=c.meta.game;h.setNow(1000);ws.onmessage({data:packet(game({control_revision:2,elapsed:0}))});
 assert.equal(c.meta.game,live);assert.equal(c.received,0);h.setNow(1600);assert.equal(c.gameViewCurrent(),false);
 assert.equal(c.presentedGame().status,'paused');assert.equal(c.presentedGame().elapsed,9);
});
for(const data of [new ArrayBuffer(2),new ArrayBuffer(8),packet({...game(),control_revision:true})]){
 test('malformed binary metadata retires its connection without a control request',()=>{
  const h=makeClient(),c=h.c;c.connect();const ws=h.sockets[0];ws.onopen();ws.onmessage({data});
  assert.equal(ws.closed,true);assert.equal(c.gameViewCurrent(),false);assert.equal(h.timers.size,1);
 });
}
test('late image callbacks cannot release or replace a newer decode',()=>{
 const h=makeClient(),c=h.c;c.connect();let ws=h.sockets[0];ws.onopen();c.togglePlayerView();ws.onmessage({data:packet(game(),1)});h.readers[0].onload();
 c.reconnectGame();ws=h.sockets[1];ws.onopen();ws.onmessage({data:packet(game({control_context:'b'.repeat(32)}),1)});h.readers[1].onload();
 h.images[0].onload();assert.equal(c.decoding,true);assert.equal(c.debugFrame,null);
 h.images[1].onload();assert.equal(c.decoding,false);assert.equal(c.debugFrame,h.images[1]);
});
test('隐藏动作画面会撤下已解码画面并拒绝晚到的画面',()=>{
 const h=makeClient(),c=h.c;c.connect();const ws=h.sockets[0];ws.onopen();
 ws.onmessage({data:packet(game(),1)});
 assert.equal(h.images.length,0);
 c.togglePlayerView();ws.onmessage({data:packet(game(),1)});h.readers[0].onload();
 c.togglePlayerView();h.images[0].onload();
 assert.equal(c.debugFrame,null);
 assert.deepEqual(h.calls.filter(row=>row[0]==='preview').map(row=>row[1]),
  ['hide-player-view','show-player-view','hide-player-view']);
});
test('socket error creates one reconnect and closing the view retires every callback',()=>{
 const h=makeClient(),c=h.c;c.connect();const ws=h.sockets[0];ws.onopen();ws.onerror();ws.onclose();
 assert.equal(h.timers.size,1);c.closeGameView();assert.equal(h.timers.size,0);assert.equal(c.stageClosed,true);
 const count=h.sockets.length;c.connect();assert.equal(h.sockets.length,count);
});

test('source recovery disables only continue until real readiness arrives',()=>{
 const h=makeClient(),c=h.c;
 const resume=h.node('resume');resume.attrs['data-click']='resumeGame()';
 c.document.querySelectorAll=()=>[h.node('mode'),resume,h.node('stopToggle')];
 c.meta.game=game({manually_paused:true,status:'paused',source_recovery:{required:true,ready:false}});
 c.syncGameControls();assert.equal(resume.disabled,true);
 assert.equal(h.node('stopToggle').disabled,false);assert.equal(h.node('mode').disabled,false);
 c.meta.game.source_recovery.ready=true;c.syncGameControls();assert.equal(resume.disabled,false);
 h.setNow(1600);c.syncGameControls();assert.equal(resume.disabled,true);
});
test('resume readiness rejection offers a useful action without replaying the command',async()=>{
 let requests=0;const h=makeClient({fetch:async()=>{requests++;return {ok:false,status:409,json:async()=>({detail:'game_input_not_ready'})};}});
 const old=h.c.meta.game=game({manually_paused:true,status:'paused'});await h.c.resumeGame();assert.equal(requests,1);assert.equal(h.c.meta.game,old);
 assert.equal(h.node('resumeNotice').textContent,'请先回到镜头前站定，再继续。');assert.equal(h.node('gameNotice').textContent,'');
});
test('an outdated pause or continue button cannot reverse the current intent',async()=>{
 const sent=[],h=makeClient({fetch:async(_,o)=>{sent.push(JSON.parse(o.body));return{ok:false};}}),c=h.c;
 await c.resumeGame();assert.equal(sent.length,0);
 c.meta.game=game({manually_paused:true,status:'paused',source_recovery:{required:true,ready:false}});
 await c.pauseGame();await c.resumeGame();assert.equal(sent.length,0);
 c.meta.game.source_recovery.ready=true;await c.resumeGame();
 assert.equal(sent.length,1);assert.equal(sent[0].action,'resume');
});
test('an unchanged stage message preserves its control during live updates',()=>{
 const h=makeClient(),overlay=h.node('overlay');let replacements=0,html='';overlay.querySelector=()=>null;
 Object.defineProperty(overlay,'innerHTML',{get:()=>html,set:value=>{replacements++;html=value;}});
 const markup='<h2>游戏已暂停</h2><button>继续本局</button>';
 h.c.showOverlay(markup);html=html.replace('<button>','<button disabled="">');h.c.showOverlay(markup);
 assert.equal(replacements,1);assert.match(html,/disabled/);
 h.c.showOverlay('');assert.equal(replacements,2);assert.equal(html,'');
});
test('a late conflict detail cannot report an old recovery failure in a new connection',async()=>{
 let finish,entered;const reading=new Promise(r=>entered=r);
 const h=makeClient({fetch:async()=>({ok:false,status:409,json:()=>{entered();return new Promise(r=>finish=r);}})});
 const work=h.c.pauseGame();await reading;h.c.streamGeneration++;finish({detail:'game_input_not_ready'});
 await work;assert.equal(h.node('gameNotice').textContent,'');
});
test('recovery guidance distinguishes camera loss, player confirmation and a deliberate countdown',()=>{
 const h=makeClient(),c=h.c;vm.runInContext(part('function gamePhaseMessage(', '\nconst artRoot'),c);
 c.selected='tennis';c.meta.status='unavailable';let g=game({manually_paused:true,source_recovery:{required:true,phase:'camera'}});
 assert.match(c.gamePhaseMessage(g),/进度已保留/);
 c.meta.status='live';g.source_recovery.phase='players';assert.match(c.gamePhaseMessage(g),/两位玩家/);
 g.source_recovery.ready=true;assert.equal(c.gamePhaseMessage(g),'本局进度已保留。');
 g=game({status:'calibrating',resume_seconds:3});assert.equal(c.gamePhaseMessage(g),'准备继续本局');
 g.manually_paused=true;assert.equal(c.gamePhaseMessage(g),'');
});
test('站位提示采用当前规则回读，并让位给手动暂停和重新确认',()=>{
 const {c}=makeClient({selected:'ski'});
 vm.runInContext(part('function gamePhaseMessage(', '\nconst artRoot'),c);
 const g=game({mode:'ski',selected_mode:'ski',status:'waiting',framing:'feet'});
 assert.match(c.stageOverlayMarkup(g,true,true),/让双脚入镜.*选好站位/);
 assert.match(c.stageOverlayMarkup({...g,elapsed:3,status:'paused'},true,true),/自动继续/);
 assert.doesNotMatch(c.stageOverlayMarkup(g,false,true),/让双脚入镜/);
 assert.doesNotMatch(c.stageOverlayMarkup({...g,status:'paused',manually_paused:true},true,true),/让双脚入镜/);
 assert.doesNotMatch(c.stageOverlayMarkup({...g,source_recovery:{required:true,phase:'camera'}},true,true),/让双脚入镜/);
});
test('重新确认站位保留一处继续入口，OK 保持进度不会盖住入口',()=>{
 const h=makeClient(),c=h.c;vm.runInContext(part('function gamePhaseMessage(', '\nconst artRoot'),c);
 c.selected='tennis';c.meta.status='live';
 const g=game({mode:'tennis',selected_mode:'tennis',status:'paused',manually_paused:true,tennis_variant:'match',
  source_recovery:{required:true,phase:'calibrating',ready:false,accepts_ok:true}});
 const waiting=c.stageOverlayMarkup(g,true,true);
 assert.match(waiting,/重新确认站位.*两位玩家回到原站位/);
 assert.doesNotMatch(waiting,/继续本局/);
 Object.assign(g.source_recovery,{ready:true,phase:'ready'});
 const ready=c.stageOverlayMarkup(g,true,true);
 assert.match(ready,/站位已确认.*比分与球路已保留.*继续本局/);
 g.gesture_control={gesture:'ok',progress:.45,latched:false};
 assert.equal(c.stageOverlayMarkup(g,true,true),ready);
});
