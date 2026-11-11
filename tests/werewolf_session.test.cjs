const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const source=fs.readFileSync('homeplane/static/werewolf-party.js','utf8');
const all=node=>[node,...node.children.flatMap(all)],settle=()=>new Promise(setImmediate);

function client({publicDisplay=false,phase='wolves',joined=true,lobby=null,privateFields={}}={}){
 class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.dataset={};this._text='';this.events={};this.disabled=false;this.classList={toggle(){},add(){},remove(){}};}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(n=>n.textContent).join(' ');}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[];this._text='';this.append(...nodes);}
  setAttribute(k,v){this.attrs[k]=String(v);}
  removeAttribute(k){delete this.attrs[k];}
  addEventListener(name,fn){this.events[name]=fn;}
  querySelector(selector){return all(this).find(n=>selector==='[data-timer]'&&n.dataset.timer);}
  querySelectorAll(selector){return all(this).filter(n=>selector.split(',').includes(n.tagName));}
 }
 const nodes=Object.fromEntries(['party','notice','roomLabel'].map(id=>[id,new Element('div')]));
 const storage=new Map(joined?[[publicDisplay?'Friday.werewolf.display':'Friday.werewolf.player','a'.repeat(64)]]:[]),requests=[],calls=[],pending={};
 const game={revision:1,phase,phase_name:phase==='lobby'?'等待大家入座':'狼人行动',player_count:6,round:1,paused:false,seconds_left:30,
  players:[1,2,3,4,5,6].map(seat=>({seat,name:'合成玩家'+seat,alive:true})),roles:[],announcements:[]};
 const player={seat:1,name:'合成玩家1',role:'wolf',role_name:'狼人',alive:true,actions:[{action:'attack',targets:[2,3,4,5,6]},{action:'pass',targets:[]}],...privateFields,public:game};
 const events={};
 let deferPoll=false,deferChoice=false,reads=0;
 const reply=(status,data)=>({ok:status===200,status,headers:{get:()=>null},json:async()=>data});
 const context=vm.createContext({console,URLSearchParams,AbortController,AbortSignal,performance:{now:()=>0},crypto:{getRandomValues:a=>a.fill(1)},navigator:{},
  location:{pathname:publicDisplay?'/display':'/phone',hash:'#code=123456'},history:{replaceState(){}},sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  document:{body:new Element('body'),hidden:false,getElementById:id=>nodes[id],createElement:tag=>new Element(tag),addEventListener(){}},window:{addEventListener:(name,fn)=>events[name]=fn},setInterval(){},
  fetch:async(path,options)=>{requests.push(path);calls.push({path,options});if(path===(publicDisplay?'/api/public':'/api/player')){reads++;if(reads>1&&deferPoll)return new Promise(resolve=>pending.poll=resolve);return reply(200,structuredClone(publicDisplay?{active:true,game}:player));}
   if(path==='/api/choice'){if(deferChoice)return new Promise((resolve,reject)=>{pending.choice=resolve;pending.choiceReject=reject;});return reply(200,structuredClone(player));}
   if(path==='/api/ready')return reply(403,{detail:'player_access_required'});
   if(path==='/api/lobby')return lobby?reply(200,structuredClone(lobby)):reply(409,{detail:'room_not_found'});throw Error('unexpected path '+path);}});
 vm.runInContext(source,context);
 const button=text=>all(nodes.party).find(n=>n.tagName==='button'&&n.textContent===text);
 return{context,nodes,storage,requests,calls,pending,reply,player,game,button,events,defer(){deferPoll=deferChoice=true;},
  async choose(){button('放弃本次行动').onclick();button('确认选择').onclick();await settle();}};
}

test('closed phone clears its identity and offers a new room without retrying the old code',async()=>{
 const c=client();await settle();vm.runInContext('revealRole()',c.context);assert.match(c.nodes.party.textContent,/狼人/);
 c.defer();const job=vm.runInContext('poll()',c.context);await settle();c.pending.poll(c.reply(403,{detail:'player_access_required'}));await job;
 assert.match(c.nodes.party.textContent,/本局已关闭/);assert.equal(vm.runInContext('player',c.context),null);assert.equal(vm.runInContext('game',c.context),null);
 assert.equal(c.storage.size,0);assert.equal(c.requests.filter(p=>p==='/api/lobby').length,0);assert.equal(c.nodes.notice.textContent,'');
 c.button('输入房间号').onclick();await settle();assert.match(c.nodes.party.textContent,/一起入座/);
 assert.equal(vm.runInContext('code',c.context),'');assert.equal(c.requests.filter(p=>p==='/api/lobby').length,0);
});

test('a late choice response cannot restore identity after the background poll closed the room',async()=>{
 const c=client();await settle();c.defer();const poll=vm.runInContext('poll()',c.context);await settle();await c.choose();
 c.pending.poll(c.reply(403,{detail:'player_access_required'}));await poll;assert.match(c.nodes.party.textContent,/本局已关闭/);
 c.pending.choice(c.reply(200,structuredClone(c.player)));await settle();await settle();
 assert.match(c.nodes.party.textContent,/本局已关闭/);assert.equal(vm.runInContext('player',c.context),null);assert.equal(c.nodes.notice.textContent,'');
});

test('a late background poll cannot restore identity after a rejected choice closed the room',async()=>{
 const c=client();await settle();c.defer();const poll=vm.runInContext('poll()',c.context);await settle();await c.choose();
 c.pending.choice(c.reply(403,{detail:'player_access_required'}));await settle();assert.match(c.nodes.party.textContent,/本局已关闭/);
 c.pending.poll(c.reply(200,structuredClone(c.player)));await poll;
 assert.match(c.nodes.party.textContent,/本局已关闭/);assert.equal(vm.runInContext('player',c.context),null);assert.equal(c.nodes.notice.textContent,'');
});

for(const denied of ['poll','choice'])test('same-turn '+denied+' revocation wins over an already-resolved private response',async()=>{
 const c=client();await settle();c.defer();const poll=vm.runInContext('poll()',c.context);await settle();await c.choose();
 c.pending[denied](c.reply(403,{detail:'player_access_required'}));
 c.pending[denied==='poll'?'choice':'poll'](c.reply(200,structuredClone(c.player)));
 await poll;await settle();
 assert.match(c.nodes.party.textContent,/本局已关闭/);assert.equal(vm.runInContext('player',c.context),null);assert.equal(c.nodes.notice.textContent,'');
});

test('a late network failure from the retired session does not add an error to the closed screen',async()=>{
 const c=client();await settle();c.defer();const poll=vm.runInContext('poll()',c.context);await settle();await c.choose();
 c.pending.poll(c.reply(403,{detail:'player_access_required'}));await poll;
 c.pending.choiceReject(Error('network'));await settle();await settle();
 assert.match(c.nodes.party.textContent,/本局已关闭/);assert.equal(c.nodes.notice.textContent,'');
});

test('revoked ready operation retires the phone immediately without waiting for polling',async()=>{
 const c=client({phase:'lobby'});await settle();c.button('准备好了').onclick();await settle();
 assert.match(c.nodes.party.textContent,/本局已关闭/);assert.equal(vm.runInContext('player',c.context),null);assert.equal(c.storage.size,0);
 assert.equal(c.nodes.notice.textContent,'');assert.equal(c.nodes.roomLabel.textContent,'狼人杀');
});

test('revoked public display clears its token and night appearance without offering a private room form',async()=>{
 const c=client({publicDisplay:true});await settle();assert.match(c.nodes.party.textContent,/狼人行动/);c.defer();
 const poll=vm.runInContext('poll()',c.context);await settle();c.pending.poll(c.reply(403,{detail:'display_access_required'}));await poll;
 assert.match(c.nodes.party.textContent,/公共画面已关闭/);assert.equal(c.storage.size,0);assert.equal(c.nodes.notice.textContent,'');
 assert.equal(c.nodes.party.textContent.includes('输入房间号'),false);assert.equal(vm.runInContext('document.body.dataset.night',c.context),'false');
});

test('public stage keeps the prompt above seats and refreshes the clock without rebuilding the stage', async () => {
 const c = client({publicDisplay:true});
 await settle();
 const stage = c.nodes.party.children[0];
 assert.equal(stage.className, 'public-stage');
 c.game.host_script = {id:1, text:'其他玩家请闭眼。'};
 c.game.players[0].role_name = '守卫';
 c.game.seconds_left = 27;
 await vm.runInContext('poll()', c.context);
 assert.equal(c.nodes.party.children[0], stage);
 assert.match(stage.textContent, /00:27.*其他玩家请闭眼/);
 assert.doesNotMatch(c.nodes.party.textContent, /守卫/);
 c.game.paused = true;
 await vm.runInContext('poll()', c.context);
 assert.match(stage.textContent, /已暂停.*等待主持人恢复游戏/);
 assert.doesNotMatch(stage.textContent, /其他玩家请闭眼/);
 c.game.paused = false;
 c.game.phase = 'finished';
 c.game.phase_name = '对局结束';
 await vm.runInContext('poll()', c.context);
 assert.match(c.nodes.party.textContent, /身份揭晓.*守卫/);
});

test('phone offers the sole target action first and sends it only after selecting a seat and confirming', async () => {
 const c = client();
 await settle();
 assert.deepEqual(c.nodes.party.children.slice(0,3).map(node => node.className), ['phase', 'action-panel', 'identity']);
 assert.equal(c.button('袭击').attrs['aria-pressed'], 'true');
 assert.equal(c.button('确认选择').disabled, true);
 all(c.nodes.party).find(node => node.dataset.choiceFocus === 'seat-2').onclick();
 assert.equal(c.button('确认选择').disabled, false);
 assert.equal(c.requests.includes('/api/choice'), false);
 c.button('确认选择').onclick();
 await settle();
 const sent = c.calls.find(call => call.path === '/api/choice');
 assert.equal(JSON.parse(sent.options.body).target, 2);
 assert.equal(JSON.parse(sent.options.body).action, 'attack');
});

test('an older poll cannot erase an acknowledged choice or rewind a newer phase',async()=>{
 for(const nextPhase of [false,true]){
  const c=client();await settle();c.defer();const poll=vm.runInContext('poll()',c.context);await settle();const oldReply=structuredClone(c.player);await c.choose();
  const acknowledged=structuredClone(c.player);acknowledged.submitted=['pass',null];
  if(nextPhase){acknowledged.public.revision=2;acknowledged.public.phase='discussion';acknowledged.public.phase_name='自由讨论';acknowledged.actions=[];}
  c.pending.choice(c.reply(200,acknowledged));await settle();
  c.pending.poll(c.reply(200,oldReply));await poll;
  assert.equal(vm.runInContext('player.submitted?.[0]',c.context),'pass');
  assert.equal(vm.runInContext('game.revision',c.context),nextPhase?2:1);
  assert.match(c.nodes.notice.textContent,/已确认/);
  if(nextPhase)assert.match(c.nodes.party.textContent,/自由讨论/);else assert.match(c.nodes.party.textContent,/已提交 · 放弃本次行动/);
 }
});

test('retired polling errors cannot replace an acknowledged choice or impose stale backoff',async()=>{
 for(const status of [429,500]){
  const c=client();await settle();c.defer();const poll=vm.runInContext('poll()',c.context);await settle();await c.choose();
  const acknowledged=structuredClone(c.player);acknowledged.submitted=['pass',null];c.pending.choice(c.reply(200,acknowledged));await settle();
  c.pending.poll(c.reply(status,{detail:status===429?'party_rate_limited':'network'}));await poll;
  assert.match(c.nodes.notice.textContent,/已确认/);assert.equal(vm.runInContext('nextPollAt',c.context),0);
 }
});

test('a failed choice can refresh immediately while an older transport is still unresolved',async()=>{
 const c=client();await settle();c.defer();const oldPoll=vm.runInContext('poll()',c.context);await settle();const oldResolve=c.pending.poll;
 const oldReply=structuredClone(c.player);await c.choose();c.pending.choice(c.reply(409,{detail:'phase_changed'}));await settle();
 const refresh=vm.runInContext('poll()',c.context);await settle();assert.notEqual(c.pending.poll,oldResolve,'A retired transport does not own the next read');
 const current=structuredClone(c.player);current.public.revision=2;current.public.phase='discussion';current.public.phase_name='自由讨论';current.actions=[];
 c.pending.poll(c.reply(200,current));await refresh;oldResolve(c.reply(200,oldReply));await oldPoll;
 assert.match(c.nodes.party.textContent,/自由讨论/);assert.equal(vm.runInContext('game.revision',c.context),2);
});

test('pending choices keep their controls stable and leave private identity viewing available',async()=>{
 const c=client();await settle();c.defer();await c.choose();const attack=c.button('袭击');
 assert.equal(attack.disabled,true);assert.equal(c.button('确认选择').disabled,true);
 assert.notEqual(c.button('按住查看身份').disabled,true);attack.onclick();assert.equal(vm.runInContext('selectedAction',c.context),'pass');
 c.pending.choice(c.reply(409,{detail:'phase_changed'}));await settle();assert.equal(attack.disabled,false);
});

test('returning to the phone reconciles a lost choice across phases without sending another choice', async () => {
 const session='b'.repeat(32);
 const c=client({privateFields:{session,choice_version:3}});
 await settle();c.defer();await c.choose();
 const choice=JSON.parse(c.calls.find(call=>call.path==='/api/choice').options.body);
 assert.equal(choice.expected_choice_version,3);
 const stamp=JSON.parse(c.storage.get('Friday.werewolf.choice'));
 assert.deepEqual(stamp,{session,seat:1,revision:1,event_id:choice.event_id});
 vm.runInContext('revealRole()',c.context);
 c.events.pagehide();
 assert.match(c.nodes.party.textContent,/身份已收起/);
 c.events.pageshow({persisted:true});await settle();
 assert.match(c.nodes.party.textContent,/正在同步对局/);
 assert.equal(c.button('确认选择'),undefined);
 assert.equal(c.calls.at(-1).options.headers['X-Friday-Choice'],choice.event_id);
 const current=structuredClone(c.player);
 current.public={...current.public,revision:2,phase:'discussion',phase_name:'自由发言'};
 current.actions=[];current.choice_version=4;
 current.choice_receipt={event_id:choice.event_id,recorded:true};
 c.pending.poll(c.reply(200,current));await settle();
 c.pending.choice(c.reply(500,{detail:'network'}));await settle();
 assert.match(c.nodes.party.textContent,/自由发言/);
 assert.match(c.nodes.notice.textContent,/已确认/);
 assert.equal(c.storage.has('Friday.werewolf.choice'),false);
 assert.equal(c.requests.filter(path=>path==='/api/choice').length,1);
});

test('public display drops a stale clock and narration, then restores the newest phase', async () => {
 const c=client({publicDisplay:true});await settle();
 c.game.host_script={id:1,text:'狼人请睁眼。'};
 await vm.runInContext('poll()',c.context);
 const stage=c.nodes.party.children[0];
 assert.match(stage.textContent,/00:30.*狼人请睁眼/);
 c.defer();
 const broken=vm.runInContext('poll()',c.context);await settle();
 c.pending.poll(c.reply(503,{detail:'network'}));await broken;
 assert.equal(c.nodes.party.children[0],stage);
 assert.match(stage.textContent,/正在同步对局.*连接恢复后会自动更新/);
 assert.doesNotMatch(stage.textContent,/00:30|狼人请睁眼/);
 const fresh=vm.runInContext('poll()',c.context);await settle();
 const game={...c.game,revision:2,phase:'discussion',phase_name:'自由发言',seconds_left:90,
  host_script:{id:2,text:'请轮流发言。'}};
 c.pending.poll(c.reply(200,{active:true,game}));await fresh;
 assert.match(stage.textContent,/自由发言.*01:30.*请轮流发言/);
 assert.doesNotMatch(stage.textContent,/正在同步|狼人请睁眼/);
});

test('editing the room number retires seats and prevents joining an unviewed room',async()=>{
 const c=client({joined:false,lobby:{revision:1,phase:'lobby',player_count:6,players:[]}});await settle();
 c.button('1 空位').onclick();assert.equal(c.button('坐好了').disabled,false);
 const room=all(c.nodes.party).find(n=>n.tagName==='input'&&n.inputMode==='numeric');room.value='654321';room.events.input?.();
 assert.equal(c.button('坐好了').disabled,true);assert.equal(vm.runInContext('selectedSeat',c.context),null);
 const form=all(c.nodes.party).find(n=>n.tagName==='form');form.onsubmit({preventDefault(){}});await settle();
 assert.equal(c.requests.includes('/api/join'),false);assert.equal(c.button('1 空位'),undefined);
});
