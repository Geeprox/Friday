const test=require('node:test'),assert=require('node:assert/strict');
const{library}=require('./games_library_harness.cjs');

test('a first launch keeps its new game view while replacing the pre-launch stopped readback',async()=>{
 const h=library({mode:'off',owned:false});await h.settle();
 h.button('林间滑雪').onclick();await h.button('开始游戏').onclick();await h.settle();
 assert.equal(h.requests.filter(r=>r.options.method==='POST').length,1);
 assert(h.all().some(n=>n.tagName==='iframe'),'the pre-launch catalogue must not remove the newly opened stage');
 assert.equal(h.all().find(n=>n.className==='games-library').dataset.playing,'true');
 assert(!h.root.textContent.includes('游戏已停止，可以重新开始。'));
 h.owner.dispose();assert.equal(h.root.children.length,0);
});

for(const retainedMode of ['off','quest'])test('a confirmed service stop with '+retainedMode+' leaves an actionable exit and returns to the same game detail',async()=>{
 const h=library();await h.settle();h.button('体感闯关').onclick();
 await h.button('继续游戏').onclick();await h.settle();
 h.service.owned=false;h.service.game.mode=retainedMode;h.service.game.game_input_enabled=false;
 h.tick();await h.settle();
 assert(!h.all().some(n=>n.tagName==='iframe'),'retired gameplay must be removed');
 const exit=h.button('返回游戏详情');assert(exit,'a stopped stage needs an accessible way back');
 assert.equal(h.focused(),exit,'focus must not remain on the removed stage controls');
 h.tick();await h.settle();assert.equal(h.button('返回游戏详情'),exit,'later reads preserve the exit and focus');
 const before=h.requests.filter(r=>r.options.method==='POST').length;
 exit.onclick();await h.settle();
 assert.equal(h.all().find(n=>n.className==='games-library').dataset.playing,'false');
 assert(h.root.contains(h.focused()));assert.equal(h.focused().tagName,'h2');
 assert.equal(h.button('开始游戏').disabled,false);
 assert.equal(h.requests.filter(r=>r.options.method==='POST').length,before,'returning must not restart the camera service');
 h.owner.dispose();
});

test('a failed service read leaves the running game and its back control intact',async()=>{
 const h=library();await h.settle();h.button('体感闯关').onclick();
 await h.button('继续游戏').onclick();await h.settle();
 const frame=h.all().find(n=>n.tagName==='iframe'),back=h.button('返回详情');
 const pending=h.defer();h.tick();await h.settle();pending.reject(Error('SYNTHETIC offline'));await h.settle();
 assert(h.all().includes(frame),'a failed status read is not proof of a stopped service');
 assert.equal(h.button('返回详情'),back);assert(!h.button('返回游戏详情'));
 assert.equal(h.requests.filter(r=>r.options.method==='POST').length,0);
 h.owner.dispose();
});

test('tennis can choose practice before starting and continue the confirmed court',async()=>{
 const h=library({mode:'off',owned:false});await h.settle();
 h.button('体感网球').onclick();
 const radio=value=>h.all().find(node=>node.type==='radio'&&node.value===value);
 assert.equal(radio('match').checked,true);
 radio('practice').checked=true;radio('practice').onchange();
 assert.equal(radio('match').checked,false);
 assert.equal(h.requests.filter(item=>item.options.method==='POST').length,0);
 await h.button('进入单人练习').onclick();await h.settle();
 const commands=()=>h.requests.filter(item=>item.options.method==='POST').map(item=>JSON.parse(item.options.body));
 const opens=()=>h.requests.filter(item=>item.path==='/api/games/tennis/open');
 assert.equal(commands().length,1);
 assert.equal(commands()[0].tennis_variant,'practice');
 assert.equal(opens()[0].options.headers['X-Friday-Game-Intent'],h.service.game_intent);
 assert(h.all().some(node=>node.tagName==='iframe'));
 h.button('返回详情').onclick();await h.settle();
 radio('match').checked=true;radio('match').onchange();
 assert(h.button('换玩法并开局'));
 await h.button('进入单人练习').onclick();await h.settle();
 assert.equal(commands().length,1,'continue may not apply the pending choice');
 assert.equal(h.service.game.tennis_variant,'practice');
 assert.equal(opens()[1].options.headers['X-Friday-Game-Intent'],h.service.game_intent);
 h.owner.dispose();
});

test('the library keeps a late handoff off screen when another viewer changes the round',async()=>{
 const h=library({mode:'tennis'});await h.settle();
 h.button('体感网球').onclick();
 h.changeBeforeOpen(()=>{h.service.game.control_revision++;h.service.game.tennis_variant='practice';});
 await h.button('继续双人对打').onclick();await h.settle();
 assert(!h.all().some(node=>node.tagName==='iframe'));
 assert(h.root.textContent.includes('游戏状态已变化'));
 assert(h.button('继续单人练习'));
 assert.equal(h.requests.filter(item=>item.options.method==='POST').length,0);
 h.owner.dispose();
});

for(const [mode,replay] of [['ski','再滑一局'],['hiit','再练一轮'],['quest','再玩一局'],['tennis','再开双人对打']]){
 test('已完成的 '+mode+' 可以查看原局成绩，明确重玩后才会准备新局',async()=>{
  const h=library({mode,status:'complete'});await h.settle();
  const row=h.all().find(node=>node.className.includes('game-running-banner'));
  assert(row.querySelector('p').textContent.endsWith('已完成'));
  const round=h.service.game.round_id;
  await h.button('查看成绩').onclick();await h.settle();
  assert(h.all().some(node=>node.tagName==='iframe'));
  assert.equal(h.service.game.round_id,round);
  assert.equal(h.requests.filter(item=>item.options.method==='POST').length,0);

  h.button('返回详情').onclick();await h.settle();
  const result=h.button('查看本局成绩'),restart=h.button(replay);
  assert(result&&!result.disabled&&!result.hidden);
  assert.equal(restart.className,'primary');
  assert.equal(result.className,'');
  await restart.onclick();await h.settle();
  assert(h.all().some(node=>node.tagName==='iframe'));
  assert.equal(h.service.game.round_id,round+1);
  assert.deepEqual(h.requests.filter(item=>item.options.method==='POST').map(item=>item.path),['/api/embodied/start']);
  h.owner.dispose();
 });
}

test('本局完成后原焦点可查看成绩，退出后焦点回到原游戏封面',async()=>{
 const h=library({mode:'ski'});await h.settle();
 const focused=h.button('继续游戏');focused.focus();
 h.service.game.status='complete';h.tick();await h.settle();
 assert.equal(h.button('查看成绩'),focused);
 assert.equal(h.focused(),focused);
 const exit=h.button('退出游戏');exit.focus();
 await exit.onclick();await h.settle();
 assert(!h.button('查看成绩'));
 assert.equal(h.focused().dataset.game,'ski');
 assert.deepEqual(h.requests.filter(item=>item.options.method==='POST').map(item=>item.path),['/api/embodied/game/stop']);
 h.owner.dispose();
});
