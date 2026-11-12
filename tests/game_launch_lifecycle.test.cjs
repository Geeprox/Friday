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
