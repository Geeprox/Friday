const test=require('node:test'),assert=require('node:assert/strict');
const{library}=require('./games_library_harness.cjs');

test('电视墙在线时，只有当前游戏舞台的就绪回执才点亮电视状态',async()=>{
 const h=library();
 h.service.tv_viewer={mode:'native_video_wall',reported_at:100};
 await h.settle();h.button('体感闯关').onclick();
 const status=()=>h.all().find(n=>n.className==='game-tv-status').textContent;
 assert.match(status(),/在电视的 Friday/);
 h.service.game.presentation={television_ready:true};h.tick();await h.settle();
 assert.equal(status(),'电视游戏画面已就绪');
 h.service.game.selected_mode='ski';h.tick();await h.settle();
 assert.match(status(),/在电视的 Friday/);
 h.owner.dispose();
});

test('one slow library read owns its slot until it settles',async()=>{
 const h=library();await h.settle();const pending=h.defer();h.tick();await h.settle();
 const before=h.requests.filter(r=>r.path==='/api/games').length;
 h.tick();h.tick();await h.settle();assert.equal(h.requests.filter(r=>r.path==='/api/games').length,before);
 pending.resolve();await h.settle();h.owner.dispose();
});

for(const outcome of['late-success','late-error'])test('a '+outcome+' cannot undo a confirmed stop',async()=>{
 const h=library();await h.settle();h.button('体感闯关').onclick();
 const old=h.defer();h.tick();await h.settle();
 await h.button('结束游戏').onclick();await h.settle();
 assert.equal(h.service.game.mode,'off');assert.equal(h.button('继续游戏').hidden,true);
 if(outcome==='late-success')old.resolve();else old.reject(Error('retired synthetic read'));
 await h.settle();assert.equal(h.button('继续游戏').hidden,true);assert.equal(h.button('开始游戏').disabled,false);
 assert(!h.all().some(n=>n.ownText==='游戏库暂时无法连接'));
 assert(old.request.options.signal.aborted);h.owner.dispose();
});

test('returning from the iframe disables old controls until a current readback',async()=>{
 const h=library();await h.settle();h.button('体感闯关').onclick();
 await h.button('继续游戏').onclick();await h.settle();assert(h.all().some(n=>n.tagName==='iframe'));
 h.service.game.mode='off';h.service.game.game_input_enabled=false;const next=h.defer();h.button('返回详情').onclick();await h.settle();
 assert(h.button('继续游戏').disabled);assert(h.button('重新开始').disabled);
 assert(next.request);next.resolve();await h.settle();
 assert.equal(h.button('继续游戏').hidden,true);assert.equal(h.button('开始游戏').disabled,false);
 h.owner.dispose();
});

test('the running banner updates in place when another view ends the round',async()=>{
 const h=library();await h.settle();assert(h.button('继续游戏'));
 h.service.game.mode='off';h.service.game.game_input_enabled=false;h.tick();await h.settle();assert(!h.button('继续游戏'));
 assert(h.button('体感闯关'));h.owner.dispose();
});

test('failed or expired readback cannot dispatch an old start or continue',async()=>{
 const h=library();await h.settle();h.button('体感闯关').onclick();
 const failed=h.defer();h.tick();await h.settle();failed.reject(Error('synthetic unavailable'));await h.settle();
 assert(h.button('继续游戏').disabled);assert(h.button('重新开始').disabled);
 const before=h.requests.filter(r=>r.options.method==='POST').length;
 await h.button('重新开始').onclick();await h.settle();
 assert.equal(h.requests.filter(r=>r.options.method==='POST').length,before);
 assert.equal(h.button('继续游戏').disabled,false);
 h.advance(9000);const slow=h.defer();h.tick();await h.settle();assert(h.button('继续游戏').disabled);
 slow.resolve();await h.settle();assert.equal(h.button('继续游戏').disabled,false);
 h.owner.dispose();
});

test('a recovered initial catalogue repopulates the store',async()=>{
 const h=library({firstError:true});await h.settle();assert(!h.button('体感闯关'));
 h.tick();await h.settle();assert(h.button('体感闯关'));assert(h.button('继续游戏'));
 h.owner.dispose();
});

test('catalogue recovery preserves an independently opened KTV player',async()=>{
 const h=library({firstError:true});await h.settle();h.button('家庭 KTV').onclick();await h.settle();
 const player=h.all().find(n=>n.tagName==='ktv-player');assert(player);
 h.tick();await h.settle();assert(h.all().includes(player));
 assert.equal(h.requests.filter(r=>r.options.method==='POST').length,0);
 player.options.onBack();await h.settle();assert(h.button('体感闯关'));
 h.owner.dispose();
});

test('a disposed library ignores a deferred failure and never remounts',async()=>{
 const h=library();await h.settle();const pending=h.defer();h.tick();await h.settle();
 h.owner.dispose();const oldText=h.root.textContent;pending.reject(Error('synthetic disposed'));await h.settle();
 assert.equal(h.root.textContent,oldText);assert(pending.request.options.signal.aborted);
});

for(const mode of ['quest','hiit','ski'])test('returning from '+mode+' detail restores its visible store entry',async()=>{
 const h=library();await h.settle();h.button(mode==='ski'?'林间滑雪':'体感闯关').onclick();
 if(mode==='hiit')h.button('运动训练').onclick();
 h.button('全部游戏').onclick();
 assert(h.root.contains(h.focused()),'focus belongs to the reconstructed store');
 assert.equal(h.focused().getAttribute('aria-label'),mode==='ski'?'查看林间滑雪':'查看体感闯关');
 assert.equal(h.requests.filter(r=>r.options.method==='POST').length,0);
 h.owner.dispose();
});

test('a featured game outside the retained filter restores its hero entry',async()=>{
 const h=library();await h.settle();h.button('轻运动').onclick();
 const hero=h.all().find(n=>n.className==='games-feature-image');hero.onclick();
 h.button('全部游戏').onclick();
 assert(h.root.contains(h.focused()));assert.equal(h.focused().className,'games-feature-image');
 assert(h.all().some(n=>n.tagName==='button'&&n.textContent==='轻运动'&&n.getAttribute('aria-pressed')==='true'));
 h.owner.dispose();
});

for(const operation of['开始游戏','结束游戏'])test('a displayed '+operation+' cannot act on a peer round and refresh never retries it',async()=>{
 const h=library();await h.settle();h.button('体感闯关').onclick();
 const expected=h.service.game_intent;
 const control=h.button(operation==='开始游戏'?'重新开始':operation);
 h.service.game={...h.service.game,mode:'ski',selected_mode:'ski',control_revision:2,round_id:2};
 const before=JSON.stringify(h.service.game);
 await control.onclick();await h.settle();
 assert.equal(JSON.stringify(h.service.game),before,'the peer round is untouched');
 const writes=h.requests.filter(r=>r.options.method==='POST');assert.equal(writes.length,1);
 assert.equal(JSON.parse(writes[0].options.body).expected_intent,expected,'send exactly the rendered intent');
 assert(h.root.textContent.includes('游戏状态已变化，请确认后再操作。'));
 assert(!h.all().some(n=>n.tagName==='iframe'),'a rejected launch never opens another game');
 assert.equal(h.button('开始游戏').disabled,false,'a current read restores explicit controls');
 assert(!h.root.textContent.includes('本局已结束'));h.owner.dispose();
});

test('same-game restart advances the scope and the next stop uses the new readback',async()=>{
 const h=library();await h.settle();h.button('体感闯关').onclick();const old=h.service.game_intent;
 await h.button('重新开始').onclick();await h.settle();
 assert(h.all().some(n=>n.tagName==='iframe'));assert.notEqual(h.service.game_intent,old);
 assert.equal(h.button('结束游戏').hidden,false,'the armed preparation is still a cancellable game');
 h.button('返回详情').onclick();await h.settle();assert.equal(h.button('进入游戏').hidden,false);
 await h.button('结束游戏').onclick();await h.settle();
 const writes=h.requests.filter(r=>r.options.method==='POST');assert.equal(writes.length,2);
 assert.notEqual(JSON.parse(writes[0].options.body).expected_intent,JSON.parse(writes[1].options.body).expected_intent);
 assert.equal(h.service.game.mode,'off');h.owner.dispose();
});

test('a fresh catalogue without a usable game intent cannot send a control',async()=>{
 const h=library();await h.settle();h.intentUnavailable();h.tick();await h.settle();
 h.button('体感闯关').onclick();assert(h.button('重新开始').disabled);assert(h.button('结束游戏').disabled);
 await h.button('重新开始').onclick();await h.button('结束游戏').onclick();await h.settle();
 assert.equal(h.requests.filter(r=>r.options.method==='POST').length,0);h.owner.dispose();
});

test('a peer conflict in the store remains visible after the running banner refreshes',async()=>{
 const h=library();await h.settle();h.service.game.control_revision++;
 await h.button('结束游戏').onclick();await h.settle();
 assert(h.root.textContent.includes('游戏状态已变化，请确认后再操作。'));
 assert.equal(h.service.game.mode,'quest');assert.equal(h.requests.filter(r=>r.options.method==='POST').length,1);
 h.owner.dispose();
});

test('a failed conflict readback keeps controls disabled until a later current read',async()=>{
 const h=library();await h.settle();h.button('体感闯关').onclick();h.service.game.control_revision++;
 const pending=h.defer(),command=h.button('结束游戏').onclick();await h.settle();
 pending.reject(Error('synthetic conflict refresh failure'));await command;await h.settle();
 assert(h.button('重新开始').disabled);assert(h.button('结束游戏').disabled);
 assert.equal(h.requests.filter(r=>r.options.method==='POST').length,1);
 h.tick();await h.settle();await h.button('重新开始').onclick();await h.settle();
 assert(h.all().some(n=>n.tagName==='iframe'));
 assert(!h.root.textContent.includes('游戏状态已变化，请确认后再操作。'),'a later successful explicit command clears the old notice');
 h.owner.dispose();
});

test('preparation survives returning to the store and entering it never restarts',async()=>{
 const h=library();await h.settle();h.button('体感闯关').onclick();await h.button('重新开始').onclick();await h.settle();
 h.button('返回详情').onclick();await h.settle();h.button('全部游戏').onclick();await h.settle();
 const row=h.all().find(node=>node.className.includes('game-running-banner'));
 assert.equal(row.querySelector('strong').textContent,'山径冒险');
 assert.equal(row.querySelector('p').textContent,'准备中');
 const before=h.requests.filter(r=>r.options.method==='POST').length;
 h.button('进入游戏').onclick();await h.settle();
 assert(h.all().some(n=>n.tagName==='iframe'));
 assert.equal(h.requests.filter(r=>r.options.method==='POST').length,before);
 h.owner.dispose();
});

test('launch follows the latest displayed camera instead of the original detail closure',async()=>{
 const h=library();await h.settle();h.button('体感闯关').onclick();
 h.service.camera='cam_2222222222';h.tick();await h.settle();
 await h.button('重新开始').onclick();await h.settle();
 const write=h.requests.find(r=>r.options.method==='POST');assert.equal(JSON.parse(write.options.body).camera,h.service.camera);
 h.owner.dispose();
});
