const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const {readClient}=require('./werewolf_client_fixture.cjs');
const source=fs.readFileSync('homeplane/static/werewolf-camera-view.js','utf8').replace('export function mountSeatCamera','function mountSeatCamera')+'\n'+readClient('host').replace("import {mountSeatCamera} from './werewolf-camera-view.js';",'').replace('export function mountWerewolf','function mountWerewolf');
const settle=()=>new Promise(setImmediate),all=node=>[node,...node.children.flatMap(all)];
const reply=data=>({ok:true,json:async()=>structuredClone(data)});
function report({paused=false,revision=1,active=true}={}){
 return active?{active:true,session:'SYNTHETIC-party',game:{revision,phase:'wolves',phase_name:'狼人行动',round:1,paused,seconds_left:30,player_count:6,players:[],roles:[],rules:[],announcements:[{id:1,text:'狼人请行动。'}]},effects:{narration:false,voice:'off',lights:{}},camera_hints:[],camera_candidates:[]}:{active:false};
}
function client({initial=report(),delayInitial=false}={}){
 let document;
 class Element{
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attrs={};this._text='';this.disabled=false;}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(n=>n.textContent).join(' ');}
  append(...nodes){for(const node of nodes){if(node.parent)node.parent.children=node.parent.children.filter(n=>n!==node);node.parent=this;this.children.push(node);if(this.tagName==='SELECT'&&(this.value===undefined||node.selected))this.value=node.value;}}
  insertBefore(node,before){if(before==null){this.append(node);return;}if(node.parent)node.parent.children=node.parent.children.filter(n=>n!==node);const index=this.children.indexOf(before);assert(index>=0);node.parent=this;this.children.splice(index,0,node);}
  replaceChildren(...nodes){if(document.activeElement&&this.contains(document.activeElement))document.activeElement=null;this._text='';this.children=[];this.append(...nodes);}
  setAttribute(key,value){this.attrs[key]=String(value);if(key.startsWith('data-'))this.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=String(value);}
  getAttribute(key){return key.startsWith('data-')?this.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]??null:this.attrs[key]??null;}
  removeAttribute(key){delete this.attrs[key];}
  querySelectorAll(selector){return all(this).slice(1).filter(n=>/^[a-z]+$/.test(selector)?n.tagName===selector.toUpperCase():selector.startsWith('.')?(n.className||'').split(' ').includes(selector.slice(1)):selector.startsWith('[data-')&&selector.endsWith(']')?selector.slice(6,-1).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()) in n.dataset:false);}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  contains(node){return all(this).includes(node);}
  set disabled(value){this._disabled=Boolean(value);if(value&&document?.activeElement===this)document.activeElement=document.body;}
  get disabled(){return this._disabled;}
  focus(){if(!this.disabled)document.activeElement=this;}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);}
 }
 const root=new Element('main'),body=new Element('body');document={body,activeElement:body,createElement:tag=>new Element(tag),addEventListener(){},removeEventListener(){}};
 const requests=[],timers=[];let next=structuredClone(initial),contextRead,heldRead,heldWrite;
 let deferRead=false,deferWrite=false,failContext=false;
 const contextData=()=>({addresses:['192.168.50.20'],lights:[],speech:{configured:false,quiet:false},rules:[],current:structuredClone(next)});
 const context=vm.createContext({document,console,AbortController,AbortSignal,JSON,setTimeout,clearTimeout,crypto:{randomUUID:()=> 'SYNTHETIC-command-0001'},
  setInterval:fn=>{timers.push(fn);return timers.length;},clearInterval(){},
  fetch:async(path,options)=>{
   const item={path,options};requests.push(item);
   if(path.endsWith('/context')){
    if(failContext)throw Error('synthetic setup choices unavailable');
    if(delayInitial)return new Promise((resolve,reject)=>contextRead={resolve,reject,item});
    return reply(contextData());
   }
   if(path.endsWith('/host')){
    if(deferRead)return new Promise((resolve,reject)=>heldRead={resolve,reject,item});
    return reply(next);
   }
   if(path.endsWith('/control')||path.endsWith('/camera-bind')||path.endsWith('/camera-unbind')){
    if(deferWrite)return new Promise((resolve,reject)=>heldWrite={resolve,reject,item});
    return reply(next);
   }
   throw Error('Unexpected request '+path);
  }});
 vm.runInContext(source,context);
 const mounted=context.mountWerewolf(root);
 return {root,requests,timers,mounted,contextData,reply,activeElement:()=>document.activeElement,
  focusOutside:()=>{const outside=new Element('button');body.append(outside);outside.focus();return outside;},
  button:name=>all(root).find(n=>n.tagName==='BUTTON'&&n.textContent===name),
  setNext:value=>next=structuredClone(value),
  deferRead:()=>deferRead=true,deferWrite:()=>deferWrite=true,
  failContext:()=>failContext=true,
  heldRead:()=>heldRead,heldWrite:()=>heldWrite,contextRead:()=>contextRead,
  tick:async()=>{timers[0]();await settle();},
 };
}

test('an older poll cannot undo an acknowledged host pause even if fetch ignores abort',async()=>{
 const c=client();await settle();c.deferRead();await c.tick();
 c.setNext(report({paused:true,revision:2}));c.button('暂停对局').onclick();await settle();
 assert(c.button('继续对局'));
 c.heldRead().resolve(reply(report()));await settle();
 assert(c.button('继续对局'),'the acknowledged pause must remain visible');
 assert(!c.button('暂停对局'));c.mounted.dispose();
});

test('the host can start the first night only after reading all identity acknowledgements',async()=>{
 const initial=report({revision:4});
 Object.assign(initial.game,{phase:'identity',phase_name:'查看身份',round:0,seconds_left:null,identities_confirmed:5});
 const c=client({initial});await settle();
 const start=c.button('开始首夜');assert(start.disabled);assert(c.button('返回入座'));
 assert.match(c.root.textContent,/5 \/ 6 人已确认/);
 c.setNext({...initial,game:{...initial.game,identities_confirmed:6}});
 await c.tick();assert.equal(c.button('开始首夜'),start);assert(!start.disabled);
 assert.match(c.root.textContent,/准备入夜.*等候首夜/);
 c.setNext(report({revision:5}));
 c.button('开始首夜').onclick();await settle();
 const body=JSON.parse(c.requests.find(r=>r.path.endsWith('/control')).options.body);
 assert.equal(body.action,'advance');assert.equal(body.revision,4);
 assert.match(c.root.textContent,/狼人行动/);
 c.mounted.dispose();
});

function recoveringPhones(waiting){
 const value=report({paused:true,revision:5});
 value.game.players=[1,2,3].map(seat=>({seat,name:'示例玩家 '+seat,ready:true,alive:true}));
 value.phone_url='http://example.test:18750/phone#code=123456';
 value.network={state:'ready',revision:2,addresses:[],waiting_seats:waiting};
 return value;
}

test('玩家返回后在原位置启用继续对局',async()=>{
 const c=client({initial:recoveringPhones([1,2,3])});await settle();
 const recovery=c.root.querySelector('.ww-phone-recovery');
 const resume=c.button('继续对局'),find=c.button('找回座位');
 assert(resume.hidden&&resume.disabled&&!find.hidden);
 assert.match(recovery.textContent,/1、2、3 号尚未返回/);
 c.setNext(recoveringPhones([3]));await c.tick();
 assert.equal(c.root.querySelector('.ww-phone-recovery'),recovery);
 assert.match(recovery.textContent,/3 号尚未返回/);
 find.focus();
 c.setNext(recoveringPhones([]));await c.tick();
 assert.equal(c.button('继续对局'),resume);
 assert(!resume.disabled&&!resume.hidden&&find.hidden);
 assert.equal(c.activeElement(),resume);
 c.setNext(report({revision:6}));await resume.onclick();await settle();
 const body=JSON.parse(c.requests.find(r=>r.path.endsWith('/control')).options.body);
 assert.equal(body.action,'resume');assert.equal(body.revision,5);
 assert(!Object.hasOwn(body,'continue_without_phones'));c.mounted.dispose();
});

test('还有玩家未返回时需要确认才会恢复倒计时',async()=>{
 const c=client({initial:recoveringPhones([2])});await settle();
 const direct=c.button('直接继续');
 direct.onclick();
 assert.equal(c.button('确认继续'),direct);
 assert.match(c.root.textContent,/倒计时会恢复/);
 assert(!c.requests.some(r=>r.path.endsWith('/control')));
 c.setNext(report({revision:6}));await direct.onclick();await settle();
 const body=JSON.parse(c.requests.find(r=>r.path.endsWith('/control')).options.body);
 assert.equal(body.action,'resume');assert.equal(body.continue_without_phones,true);
 c.mounted.dispose();
});

function cameraLobby(){
 const first=report();first.game.phase='lobby';first.game.phase_name='等待大家入座';
 first.game.players=[{seat:1,name:'合成玩家1',alive:true,ready:false},{seat:2,name:'合成玩家2',alive:true,ready:false}];
 first.camera_enabled=true;first.camera_available=true;first.camera_candidates=[7,8].map(track=>({camera:'cam_1234567890',session:'SYNTHETIC-camera',track}));
 return first;
}

test('主持端二维码随空位恢复，旧图片失败不影响本次邀请',async()=>{
 const full=cameraLobby();
 full.phone_url='http://example.test:18750/phone#code=123456';
 full.join_code='123456';
 full.game.players=Array.from({length:6},(_,i)=>({seat:i+1,name:'合成玩家'+(i+1),alive:true,ready:false}));
 const c=client({initial:full});await settle();
 const invitation=c.root.querySelector('.ww-join');
 assert(invitation.hidden);
 assert(!invitation.querySelector('img'));

 const vacant=structuredClone(full);vacant.game.players.pop();vacant.game.revision++;
 c.setNext(vacant);await c.tick();
 assert.equal(invitation.hidden,false);
 const original=invitation.querySelector('img');
 assert.match(original.src,/join-qr\?session=SYNTHETIC-party/);
 original.onerror();
 assert.equal(original.hidden,true);
 assert.match(invitation.textContent,/123456/);
 assert.equal(invitation.querySelector('a').href,full.phone_url);

 vacant.game.revision++;
 c.setNext(vacant);await c.tick();
 const recovered=invitation.querySelector('img');
 assert.notEqual(recovered,original);
 assert(!recovered.hidden);
 original.onerror();
 assert(!recovered.hidden);

 full.game.revision=vacant.game.revision+1;
 c.setNext(full);await c.tick();
 assert(invitation.hidden&&!invitation.querySelector('img'));
 c.mounted.dispose();
});

test('occupied numbers stay visible but cannot submit a second seat for the same person',async()=>{
 const first=cameraLobby();first.camera_bindings=[{seat:1,...first.camera_candidates[0]}];
 const c=client({initial:first});await settle();const fields=c.root.querySelectorAll('select');
 const own=fields[0].children.find(o=>o.value===JSON.stringify(['cam_1234567890','SYNTHETIC-camera',7,0]));
 const occupied=fields[1].children.find(o=>o.value===own.value);
 assert.equal(own.disabled,false);assert.equal(occupied.disabled,true);assert.match(occupied.textContent,/1 号/);
 fields[1].value=occupied.value;fields[1].onchange();await settle();
 assert(!c.requests.some(r=>r.path.endsWith('/camera-bind')));
 assert.equal(fields[1].value,'');c.mounted.dispose();
});

test('clearing an offline mapping uses its exact source and current room and restores field focus',async()=>{
 const first=cameraLobby();first.camera_bindings=[{seat:1,...first.camera_candidates[0]}];first.camera_candidates=[];first.camera_available=false;
 const c=client({initial:first});await settle();const select=c.root.querySelector('select');
 const clear=select.children.find(o=>o.value==='__clear__');assert(clear,'the existing mapping can be released without a running camera');
 select.focus();c.deferWrite();select.value=clear.value;select.onchange();await settle();assert.equal(select.disabled,true);
 select.onchange();await settle();const writes=c.requests.filter(r=>r.path.endsWith('/camera-unbind'));assert.equal(writes.length,1);
 assert.deepEqual(JSON.parse(writes[0].options.body),{seat:1,...first.camera_bindings[0],game_session:'SYNTHETIC-party',revision:1,event_id:'SYNTHETIC-command-0001'});
 const next=structuredClone(first);next.camera_bindings=[];next.game.revision=2;c.heldWrite().resolve(reply(next));await settle();
 assert.equal(c.activeElement(),select);assert.equal(select.disabled,false);assert.equal(select.value,'');assert(!select.children.some(o=>o.value==='__clear__'));
 assert.equal(c.root.querySelectorAll('.ww-camera-binding').length,2);c.mounted.dispose();
});

test('camera waiting and fresh absence are distinct without changing the saved mapping or field',async()=>{
 const first=cameraLobby(),mapped={seat:1,...first.camera_candidates[0]};first.camera_bindings=[mapped];first.camera_candidates=[];first.camera_available=false;
 const c=client({initial:first});await settle();const select=c.root.querySelector('select');select.focus();
 assert.match(select.children[0].textContent,/恢复/);assert(!select.children[0].textContent.includes('变化'));
 const empty=structuredClone(first);empty.camera_available=true;c.setNext(empty);await c.tick();
 assert.equal(c.root.querySelector('select'),select);assert.equal(c.activeElement(),select);assert.match(select.children[0].textContent,/原位置/);assert(!select.children[0].textContent.includes('恢复'));
 const recovered=structuredClone(first);recovered.camera_available=true;recovered.camera_candidates=[mapped];c.setNext(recovered);await c.tick();
 assert.equal(select.value,JSON.stringify(['cam_1234567890','SYNTHETIC-camera',7,0]));assert.equal(c.activeElement(),select);
 assert(!c.requests.some(r=>r.options.method==='POST'));c.mounted.dispose();
});

test('releasing a number in another seat reenables the same existing option without replacing focus',async()=>{
 const first=cameraLobby();first.camera_bindings=[{seat:1,...first.camera_candidates[0]}];
 const c=client({initial:first});await settle();const select=c.root.querySelectorAll('select')[1],option=select.children[1];select.focus();assert.equal(option.disabled,true);
 const next=structuredClone(first);next.camera_bindings=[];next.game.revision=2;c.setNext(next);await c.tick();
 assert.equal(c.root.querySelectorAll('select')[1],select);assert.equal(select.children[1],option);assert.equal(c.activeElement(),select);assert.equal(option.disabled,false);assert(!option.textContent.includes('已对应'));c.mounted.dispose();
});

test('a reused camera number asks for a new correspondence without moving keyboard focus',async()=>{
 const first=cameraLobby(),original={...first.camera_candidates[0],track_revision:0};
 first.camera_candidates[0]=original;first.camera_bindings=[{seat:1,...original}];
 const c=client({initial:first});await settle();const select=c.root.querySelector('select');select.focus();
 const originalKey=select.value;
 const replacement={...original,track_revision:1},next=structuredClone(first);
 next.camera_candidates[0]=replacement;next.camera_rebind_seats=[1];
 c.setNext(next);await c.tick();
 assert.equal(c.activeElement(),select);
 assert.equal(select.value,'');
 assert.equal(select.children[0].textContent,'请重新对应');
 assert(!select.children.some(option=>option.value===originalKey));
 const returned=structuredClone(next);returned.camera_bindings=[{seat:1,...replacement}];returned.camera_rebind_seats=[];
 c.setNext(returned);
 select.value=select.children.find(option=>option.textContent==='镜头中的 #7').value;
 select.onchange();await settle();
 const request=c.requests.find(row=>row.path.endsWith('/camera-bind'));
 assert.equal(JSON.parse(request.options.body).track_revision,1);
 assert.equal(c.activeElement(),select);assert.notEqual(select.value,originalKey);
 c.mounted.dispose();
});

test('a native pending choice survives camera refresh until change submits and reads it back',async()=>{
 const first=cameraLobby(),c=client({initial:first});await settle();const select=c.root.querySelectorAll('select')[1];select.focus();
 const choice=JSON.stringify(['cam_1234567890','SYNTHETIC-camera',8,0]);select.value=choice;
 const polled=structuredClone(first);polled.camera_candidates.reverse();c.setNext(polled);await c.tick();
 assert.equal(select.value,choice,'camera refresh must preserve the highlighted native draft before its change event');
 assert(!c.requests.some(r=>r.options.method==='POST'));
 const saved=structuredClone(polled);saved.camera_bindings=[{seat:2,...first.camera_candidates[1]}];saved.game.revision=2;c.setNext(saved);select.onchange();await settle();
 const request=c.requests.find(r=>r.path.endsWith('/camera-bind'));assert.equal(JSON.parse(request.options.body).track,8);
 assert.equal(select.value,choice);assert.equal(c.activeElement(),select);c.mounted.dispose();
});

test('a failed native draft restores the acknowledged mapping instead of displaying success',async()=>{
 const first=cameraLobby();first.camera_bindings=[{seat:2,...first.camera_candidates[0]}];const c=client({initial:first});await settle();
 const select=c.root.querySelectorAll('select')[1];select.focus();c.deferWrite();select.value=JSON.stringify(['cam_1234567890','SYNTHETIC-camera',8,0]);select.onchange();await settle();
 c.heldWrite().reject(Error('camera_track_unavailable'));await settle();
 assert.equal(select.value,JSON.stringify(['cam_1234567890','SYNTHETIC-camera',7,0]));assert.equal(c.activeElement(),select);assert.equal(select.disabled,false);c.mounted.dispose();
});

test('a pending draft is withdrawn when its source disappears before submission',async()=>{
 const first=cameraLobby(),c=client({initial:first});await settle();const select=c.root.querySelectorAll('select')[1];select.focus();select.value=JSON.stringify(['cam_1234567890','SYNTHETIC-camera',8,0]);
 const next=structuredClone(first);next.camera_candidates=next.camera_candidates.filter(c=>c.track!==8);c.setNext(next);await c.tick();
 assert.equal(select.value,'');assert.equal(c.activeElement(),select);select.onchange();await settle();assert(!c.requests.some(r=>r.options.method==='POST'));c.mounted.dispose();
});

test('a retired clear field cannot release a mapping in a replacement room',async()=>{
 const first=cameraLobby();first.camera_bindings=[{seat:1,...first.camera_candidates[0]}];
 const c=client({initial:first});await settle();const select=c.root.querySelector('select');assert(select.children.some(o=>o.value==='__clear__'));
 const next=structuredClone(first);next.session='SYNTHETIC-new-room';c.setNext(next);await c.tick();select.value='__clear__';select.onchange();await settle();
 assert(!c.requests.some(r=>r.path.endsWith('/camera-unbind')));c.mounted.dispose();
});

test('lobby ready and camera polls preserve the live seat form, focus and expanded rules',async()=>{
 const first=cameraLobby(),c=client({initial:first});await settle();
 const select=c.root.querySelector('select'),rules=c.root.querySelector('details');rules.open=true;select.focus();
 const next=structuredClone(first);next.game.revision=2;next.game.players[1].ready=true;
 next.camera_candidates.reverse();next.camera_candidates.push({...next.camera_candidates[0],track:9});c.setNext(next);await c.tick();
 assert.equal(c.root.querySelector('select'),select,'polling must not replace the field being used');
 assert.equal(c.activeElement(),select);assert.equal(c.root.querySelector('details'),rules);assert.equal(rules.open,true);
 assert.match(c.root.textContent,/1 \/ 6 人已准备/);c.mounted.dispose();
});

test('a changed candidate order cannot redirect an existing option to another camera track',async()=>{
 const first=cameraLobby(),c=client({initial:first});await settle();
 const select=c.root.querySelector('select'),chosen=select.children[1].value;
 const next=structuredClone(first);next.camera_candidates.reverse();c.setNext(next);await c.tick();
 const current=c.root.querySelector('select');current.value=chosen;current.onchange();await settle();
 const request=c.requests.find(r=>r.path.endsWith('/camera-bind'));assert(request);
 assert.equal(JSON.parse(request.options.body).track,7,'the displayed option identifies its source, never its list offset');c.mounted.dispose();
});

test('a retired seat field cannot bind its old selection into a replacement room',async()=>{
 const first=cameraLobby(),c=client({initial:first});await settle();
 const old=c.root.querySelector('select'),chosen=old.children[1].value;
 const next=structuredClone(first);next.session='SYNTHETIC-replacement-room';c.setNext(next);await c.tick();
 old.value=chosen;old.onchange();await settle();
 assert(!c.requests.some(r=>r.path.endsWith('/camera-bind')));c.mounted.dispose();
});

test('a vanished camera option cannot bind the candidate now occupying its former position',async()=>{
 const first=cameraLobby(),c=client({initial:first});await settle();
 const old=c.root.querySelector('select'),chosen=old.children[1].value;
 const next=structuredClone(first);next.camera_candidates.shift();c.setNext(next);await c.tick();
 old.value=chosen;old.onchange();await settle();
 assert(!c.requests.some(r=>r.path.endsWith('/camera-bind')));c.mounted.dispose();
});

test('joining and leaving update lobby seats and keep alignment rows in seat order',async()=>{
 const first=cameraLobby(),c=client({initial:first});await settle();
 const select=c.root.querySelector('select');select.focus();
 const next=structuredClone(first);next.game.revision=2;next.game.players.push({seat:3,name:'合成玩家3',alive:true,ready:false});
 c.setNext(next);await c.tick();assert.equal(c.root.querySelector('select'),select);assert.equal(c.activeElement(),select);
 next.game.revision=3;next.game.players=next.game.players.filter(p=>p.seat!==2);c.setNext(next);await c.tick();
 assert(!c.root.querySelectorAll('.ww-camera-binding').some(n=>n.textContent.includes('合成玩家2')));
 next.game.revision=4;next.game.players.push({seat:2,name:'新入座玩家',alive:true,ready:false});c.setNext(next);await c.tick();
 assert.deepEqual(c.root.querySelectorAll('.ww-camera-binding').map(n=>n.children[0].textContent),['1 号 · 合成玩家1','2 号 · 新入座玩家','3 号 · 合成玩家3']);
 assert.equal(c.activeElement(),select);c.mounted.dispose();
});

for(const moveFocus of [false,true])test('binding acknowledgement restores its field only if focus was not moved: '+moveFocus,async()=>{
 const first=cameraLobby(),c=client({initial:first});await settle();
 const select=c.root.querySelector('select');select.focus();c.deferWrite();
 select.value=select.children[1].value;select.onchange();await settle();assert.equal(select.disabled,true);
 const outside=moveFocus?c.focusOutside():null;
 const next=structuredClone(first);next.camera_bindings=[{seat:1,...first.camera_candidates[0]}];
 c.heldWrite().resolve(reply(next));await settle();assert.equal(select.disabled,false);
 assert.equal(c.activeElement(),outside||select);c.mounted.dispose();
});

test('an older poll cannot reopen a room after acknowledged close',async()=>{
 const c=client();await settle();c.deferRead();await c.tick();
 c.button('关闭房间').onclick();c.setNext(report({active:false}));c.button('确认关闭房间').onclick();await settle();
 assert(c.button('创建房间'));
 c.heldRead().resolve(reply(report()));await settle();
 assert(c.button('创建房间'),'closed room must stay closed');
 assert(!c.button('暂停对局'));c.mounted.dispose();
});

test('大厅关闭确认在原按钮回显，确认后才关闭房间',async()=>{
 const c=client({initial:cameraLobby()});await settle();
 const close=c.button('关闭房间');close.focus();close.onclick();
 assert.equal(c.button('确认关闭房间'),close);
 assert.equal(c.activeElement(),close);
 assert.equal(c.requests.filter(r=>r.path.endsWith('/control')).length,0);
 await c.tick();assert.equal(c.button('确认关闭房间'),close);
 c.setNext(report({active:false}));close.onclick();await settle();
 assert(c.button('创建房间'));
 assert.equal(c.requests.filter(r=>r.path.endsWith('/control')).length,1);
 c.mounted.dispose();
});

test('a slow first context read is not overtaken or repeatedly aborted by polling',async()=>{
 const c=client({delayInitial:true});await settle();
 await c.tick();await c.tick();
 assert.equal(c.requests.length,1);assert.equal(c.contextRead().item.options.signal.aborted,false);
 c.contextRead().resolve(reply(c.contextData()));await settle();assert(c.button('暂停对局'));c.mounted.dispose();
});

test('slow background reads never create an overlapping poll queue',async()=>{
 const c=client();await settle();c.deferRead();await c.tick();await c.tick();await c.tick();
 assert.equal(c.requests.filter(r=>r.path.endsWith('/host')).length,1);
 c.heldRead().resolve(reply(report()));await settle();await c.tick();
 assert.equal(c.requests.filter(r=>r.path.endsWith('/host')).length,2);c.mounted.dispose();
 c.heldRead().resolve(reply(report()));await settle();
});

test('a retired poll failure does not add a connection notice after a successful command',async()=>{
 const c=client();await settle();c.deferRead();await c.tick();
 c.setNext(report({paused:true,revision:2}));c.button('暂停对局').onclick();await settle();
 c.heldRead().reject(Error('synthetic delayed network fault'));await settle();
 assert(!c.root.textContent.includes('正在重试'));assert(c.button('继续对局'));c.mounted.dispose();
});

test('successful synchronization clears its own failure notice without replacing the board',async()=>{
 const c=client();await settle();const pause=c.button('暂停对局');c.deferRead();await c.tick();
 c.heldRead().reject(Error('synthetic offline'));await settle();assert.match(c.root.textContent,/正在重试/);
 await c.tick();c.heldRead().resolve(reply(report()));await settle();
 assert(!c.root.textContent.includes('正在重试'));assert.equal(c.button('暂停对局'),pause);c.mounted.dispose();
});

test('disposing a pending command aborts ownership and ignores its eventual reply',async()=>{
 const c=client();await settle();c.deferWrite();c.button('暂停对局').onclick();await settle();
 c.mounted.dispose();assert.equal(c.heldWrite().item.options.signal.aborted,true);
 c.heldWrite().resolve(reply(report({paused:true,revision:2})));await settle();
 assert.equal(c.root.children.length,0);await c.tick();assert.equal(c.requests.length,2);
});

test('a confirmed close remains closed if refreshing setup choices fails',async()=>{
 const c=client();await settle();c.failContext();
 c.button('关闭房间').onclick();c.setNext(report({active:false}));c.button('确认关闭房间').onclick();await settle();
 assert(c.button('创建房间'));assert(!c.button('暂停对局'));c.mounted.dispose();
});

test('host commands carry their displayed room and revision and disable duplicate input',async()=>{
 const c=client();await settle();c.deferWrite();const pause=c.button('暂停对局');
 pause.onclick();await settle();assert.equal(pause.disabled,true);pause.onclick();await settle();
 const writes=c.requests.filter(r=>r.path.endsWith('/control'));assert.equal(writes.length,1);
 assert.deepEqual(JSON.parse(writes[0].options.body),{action:'pause',session:'SYNTHETIC-party',revision:1,event_id:'SYNTHETIC-command-0001'});
 c.heldWrite().resolve(reply(report({paused:true,revision:2})));await settle();assert(c.button('继续对局'));c.mounted.dispose();
});

test('camera seat selection reads back the actual mapping and carries the current game context',async()=>{
 const first=report();first.game.phase='lobby';first.game.players=[{seat:1,name:'合成玩家1',alive:true}];
 first.camera_enabled=true;first.camera_candidates=[7,8].map(track=>({camera:'cam_1234567890',session:'SYNTHETIC-camera',track}));
 first.camera_bindings=[{seat:1,...first.camera_candidates[1]}];
 const c=client({initial:first});await settle();
 const optionFor=(select,track)=>select.children.find(o=>o.textContent.endsWith('#'+track));
 let select=c.root.querySelector('select');assert.equal(select.value,optionFor(select,8).value);
 select.value='';select.onchange();await settle();assert.equal(c.requests.length,1,'placeholder never binds the first camera track');
 const next=structuredClone(first);next.camera_bindings=[{seat:1,...first.camera_candidates[0]}];c.setNext(next);
 select.value=optionFor(select,7).value;select.onchange();await settle();
 const request=c.requests.find(r=>r.path.endsWith('/camera-bind'));assert(request);
 assert.deepEqual(JSON.parse(request.options.body),{seat:1,...first.camera_candidates[0],game_session:'SYNTHETIC-party',revision:1,event_id:'SYNTHETIC-command-0001'});
 select=c.root.querySelector('select');assert.equal(select.value,optionFor(select,7).value);c.mounted.dispose();
});
