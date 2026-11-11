const root=document.getElementById('party'),notice=document.getElementById('notice'),roomLabel=document.getElementById('roomLabel');
const display=location.pathname==='/display',fragment=new URLSearchParams(location.hash.slice(1));
const roomCode=params=>{const value=params.get('code')||'';return /^[0-9]{6}$/.test(value)?value:'';};
let invitationCode=display?'':roomCode(fragment);
const storeKey=display?'Friday.werewolf.display':'Friday.werewolf.player';
const joinStoreKey='Friday.werewolf.join';
const choiceStoreKey='Friday.werewolf.choice';
let credential='',code=invitationCode,alive=true,busy=false,readRequest=null,commandRequest=null,player=null,game=null,viewKey='',selectedAction=null,selectedTarget=null,selectedSeat=null,peeked=false,roleNode=null,nextPollAt=0,pollNotice='',sessionGeneration=0;
let viewedIdentity=null,identityConfirmation=null;
let joinPending=null,joinName='';
let choiceStamp=null,choiceNoticeRevision=null,syncing=false;
let displayView=null,lastDisplay=null;
let viewUpdatedAt=0;
try{credential=display?(fragment.get('token')||sessionStorage.getItem(storeKey)||''):(sessionStorage.getItem(storeKey)||'');if(credential)sessionStorage.setItem(storeKey,credential);}catch{}
if(!display)try{if(credential)sessionStorage.removeItem(joinStoreKey);else{const raw=sessionStorage.getItem(joinStoreKey);if(raw&&raw.length<=2048)joinPending=joinAttempt(JSON.parse(raw));if(joinPending&&invitationCode&&joinPending.code!==invitationCode)joinPending=null;if(joinPending){code=joinPending.code;joinName=joinPending.name;}else sessionStorage.removeItem(joinStoreKey);}}catch{}
if (!display) try {
 const raw=credential&&sessionStorage.getItem(choiceStoreKey);
 if (raw && raw.length<=240) choiceStamp=choiceReference(JSON.parse(raw));
} catch {}
if (!display&&!choiceStamp) forgetChoice();
history.replaceState(null,'',location.pathname);
const names={attack:'袭击',check:'查验',guard:'守护',save:'使用解药',poison:'使用毒药',vote:'投票',shoot:'开枪',pass:'放弃本次行动'};
const nightPhases = new Set(['guard', 'wolves', 'seer', 'witch']);
const targetPrompts = {attack:'选择袭击对象', check:'选择查验对象', guard:'选择守护对象',
 poison:'选择用药对象', vote:'选择投票对象', shoot:'选择开枪目标'};
const problems={room_not_found:'没有找到这个房间，请核对房间号。',seat_unavailable:'这个座位刚被选走，请换一个。',join_closed:'本局已经开始，请等下一局。',join_recovery_full:'暂时无法入座，请让主持人重新创建房间。',phase_changed:'环节已更新，这次选择未提交。',action_unavailable:'当前环节无法进行这项操作。',target_invalid:'请选择当前可用的座位。',party_rate_limited:'连接正在恢复，请稍后再试。',event_conflict:'选择已变化，请重新确认。',choice_changed:'你的选择已更新，请重新确认。',network:'连接暂时中断，正在重试。'};
const choiceRejected=new Set(['phase_changed','action_unavailable','target_invalid','event_conflict','choice_changed']);
const h=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
const button=(text,fn,cls)=>{const n=h('button',text,cls);n.type='button';n.onclick=e=>{if(!busy&&!n.disabled)return fn(e);};return n;};
const retired=e=>e.message==='session_changed'||e.message==='response_retired';
function choiceReference(value) {
 if (!value || typeof value.session!=='string' || !/^[a-f0-9]{32}$/.test(value.session)
     || !Number.isInteger(value.seat) || value.seat<1 || value.seat>12
     || !Number.isSafeInteger(value.revision) || value.revision<0
     || typeof value.event_id!=='string' || !/^[a-zA-Z0-9_-]{12,64}$/.test(value.event_id)) return null;
 const {session,seat,revision,event_id}=value;
 return {session,seat,revision,event_id};
}
function forgetChoice() {
 choiceStamp=null;
 try {sessionStorage.removeItem(choiceStoreKey);} catch {}
}
function rememberChoice(payload) {
 choiceStamp=choiceReference({...payload,session:player.session,seat:player.seat});
 if (choiceStamp) try {sessionStorage.setItem(choiceStoreKey,JSON.stringify(choiceStamp));} catch {}
}
function joinAttempt(value){return value&&typeof value==='object'&&typeof value.code==='string'&&typeof value.session==='string'&&typeof value.attempt_id==='string'&&/^[0-9]{6}$/.test(value.code)&&/^[a-f0-9]{32}$/.test(value.session)&&/^[a-f0-9]{64}$/.test(value.attempt_id)&&Number.isInteger(value.seat)&&value.seat>=1&&value.seat<=12&&typeof value.name==='string'&&value.name.trim().length>0&&value.name.length<=20?{code:value.code,session:value.session,seat:value.seat,name:value.name,attempt_id:value.attempt_id}:null;}
function forgetJoin(){joinPending=null;try{sessionStorage.removeItem(joinStoreKey);}catch{}}
function joinRecovery(){
 if(!joinPending||!alive)return;root.replaceChildren();document.body.dataset.night='false';roomLabel.textContent='狼人杀';
 const intro=h('section',null,'join-intro');intro.append(h('span',null,'moon'),h('h1','入座尚未确认'),h('p',joinPending.seat+' 号 · '+joinPending.name));
 const actions=h('div',null,'join-form');actions.append(button('重试入座',()=>{void submitJoin(joinPending);},'primary'),button('更换房间',()=>{forgetJoin();invitationCode=code='';game=null;selectedSeat=null;notice.textContent='';joinForm();}));root.append(intro,actions);
}
async function submitJoin(value){
 if(busy||!alive)return;const payload=joinAttempt(value);if(!payload){notice.textContent='入座暂不可用，请重新载入。';return;}
 joinPending=payload;joinName=payload.name;try{sessionStorage.setItem(joinStoreKey,JSON.stringify(payload));}catch{}
 const generation=sessionGeneration;
 await perform(async()=>{try{await api('join',payload,result=>{
  if(!/^[a-f0-9]{64}$/.test(result.token)||result.session!==payload.session||result.player?.seat!==payload.seat)throw Error('network');
  credential=result.token;let saved=false;try{sessionStorage.setItem(storeKey,credential);saved=true;}catch{}
  joinPending=null;if(saved)try{sessionStorage.removeItem(joinStoreKey);}catch{}
  viewKey='';updatePlayer(result.player,true);
 });}catch(e){if(['room_not_found','seat_unavailable','join_closed','event_conflict','join_recovery_full'].includes(e.message))forgetJoin();throw e;}});
 if(!alive||generation!==sessionGeneration||credential)return;
 if(joinPending)joinRecovery();else joinForm();
}
async function api(path,body,apply=()=>{}){
 const generation=sessionGeneration,request=body===undefined?readRequest:commandRequest;
 const current=()=>request&&(body===undefined?readRequest:commandRequest)===request;
 try{
  if(!current())throw Error('response_retired');
  const headers={'Content-Type':'application/json'};
  if (credential) headers.Authorization='Bearer '+credential;
  if (path==='player' && choiceStamp) headers['X-Friday-Choice']=choiceStamp.event_id;
  const response=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers,body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.any([AbortSignal.timeout(6000),request.controller.signal]),cache:'no-store'}),data=await response.json();
  if(generation!==sessionGeneration||!alive)throw Error('session_changed');
  // Revocation of this credential remains authoritative even if an ordinary read was retired.
  if(!response.ok&&typeof data.detail==='string'&&data.detail.endsWith('access_required'))throw Error(data.detail);
  if(!current())throw Error('response_retired');
  if(!response.ok){if(response.status===429){const retry=Number(response.headers.get('Retry-After'));nextPollAt=performance.now()+Math.min(60,Math.max(1,Number.isFinite(retry)?retry:5))*1000;}throw Error(data.detail||'network');}
  apply(data);return data;
 }catch(e){if(generation!==sessionGeneration||!alive)throw Error('session_changed');if(!e.message.endsWith('access_required')&&!current())throw Error('response_retired');throw e;}
}
function sayError(e){notice.textContent=problems[e.message]||'暂时无法完成，请重试。';}
async function perform(fn){
 if(busy||!alive)return;const request={controller:new AbortController()},oldRead=readRequest;readRequest=null;oldRead?.controller.abort();commandRequest=request;busy=true;notice.textContent='';root.setAttribute('aria-busy','true');
 choiceNoticeRevision=null;
 const controls=Array.from(root.querySelectorAll('button,input')).filter(n=>n.className!=='reveal-role').map(n=>[n,n.disabled]);for(const[n]of controls)n.disabled=true;
 request.release=()=>{for(const[n,disabled]of controls)n.disabled=disabled;root.removeAttribute('aria-busy');};
 try{await fn();}catch(e){if(e.message.endsWith('access_required'))closedSession();else if(!retired(e))sayError(e);}finally{if(commandRequest===request){commandRequest=null;busy=false;request.release();updateIdentityConfirmation();}}
}
function phaseHeader(g) {
 const header = h('div', null, 'phase');
 const heading = h('div', null, 'phase-heading');
 heading.append(h('h1', syncing ? '正在同步对局' : g.phase_name));
 if (!syncing && g.round) heading.append(h('p', '第 ' + g.round + ' 轮'));
 const timer = h('strong', syncing ? '' : g.paused ? '已暂停' : g.seconds_left == null ? '' : g.seconds_left + ' 秒', 'timer');
 timer.hidden=syncing||g.seconds_left==null&&!g.paused;
 timer.dataset.timer = 'true';
 header.append(heading, timer);
 return header;
}
function seatButton(seat,selected,fn,extra=''){const p=game?.players.find(x=>x.seat===seat),b=button('',fn,'seat-choice');b.append(h('strong',String(seat)),h('span',p?.name||extra||'空位'));b.setAttribute('aria-pressed',String(selected));return b;}
function joinForm(){
 root.replaceChildren();document.body.dataset.night='false';roomLabel.textContent='狼人杀';const intro=h('div',null,'join-intro');intro.append(h('span',null,'moon'),h('h1','一起入座'),h('p','每人一部手机，身份与行动只给自己看。'));root.append(intro);
 let loadedCode='';const form=h('form',null,'join-form'),room=h('label','房间号'),roomInput=h('input');roomInput.type='text';roomInput.inputMode='numeric';roomInput.pattern='[0-9]{6}';roomInput.maxLength=6;roomInput.autocomplete='off';roomInput.value=code;roomInput.required=true;room.append(roomInput);const find=button('查看座位',lookup);form.append(room,find);const seats=h('div',null,'join-seats');form.append(seats);const name=h('label','你的名字'),nameInput=h('input');nameInput.maxLength=20;nameInput.autocomplete='nickname';nameInput.required=true;name.append(nameInput);const join=h('button','坐好了','primary');join.type='submit';join.disabled=true;form.append(name,join);
 roomInput.addEventListener('input',()=>{invitationCode='';code=roomInput.value;loadedCode='';selectedSeat=null;game=null;notice.textContent='';renderSeats();});
 nameInput.value=joinName;
 form.onsubmit=e=>{e.preventDefault();if(busy||!selectedSeat||!loadedCode||loadedCode!==roomInput.value)return;void submitJoin({code:loadedCode,session:game?.session,seat:selectedSeat,name:nameInput.value.trim(),attempt_id:Array.from(crypto.getRandomValues(new Uint8Array(32)),b=>b.toString(16).padStart(2,'0')).join('')});};
 function lookup(){void perform(async()=>{const requested=roomInput.value;code=requested;loadedCode='';selectedSeat=null;game=null;renderSeats();await api('lobby',{code:requested},data=>{if(!credential&&roomInput.value===requested){loadedCode=requested;game=data;}});}).finally(renderSeats);}
 function renderSeats(){seats.replaceChildren();join.disabled=!selectedSeat||loadedCode!==roomInput.value||!game||game.players.some(p=>p.seat===selectedSeat);if(!game||loadedCode!==roomInput.value)return;seats.append(h('p','选一个空座位，和电视上的编号一致。'));const grid=h('div',null,'seat-grid');for(let i=1;i<=game.player_count;i++){const occupied=game.players.some(p=>p.seat===i),b=seatButton(i,selectedSeat===i,()=>{if(!game||loadedCode!==roomInput.value)return;selectedSeat=i;renderSeats();});b.disabled=occupied;grid.append(b);}seats.append(grid);}
 root.append(form);if(code.length===6)lookup();
}
function identityStamp(){
 return player?.public?.phase==='identity'?player.session+':'+player.public.revision:null;
}
function updateIdentityConfirmation(){
 const stamp=identityStamp();
 if(identityConfirmation)identityConfirmation.disabled=!stamp||busy||syncing||peeked||document.hidden||viewedIdentity!==stamp;
}
function hideRole(){
 peeked=false;
 if(roleNode)roleNode.replaceChildren(h('span',null,'role-seal'),h('strong',identityStamp()?'身份牌':'身份已收起'));
 document.body.classList.remove('peeking');updateIdentityConfirmation();
}
function retireRequests() {
 sessionGeneration++;
 const read=readRequest,command=commandRequest;
 readRequest=null;commandRequest=null;busy=false;
 read?.controller.abort();command?.controller.abort();command?.release();
 retireInvitation(displayView);
}
function closedSession(){
 const reopenInvitation=!display&&invitationCode;
 invitationCode='';
 forgetJoin();joinName='';
 if (!display) forgetChoice();
 retireRequests();hideRole();credential='';code='';player=null;game=null;roleNode=null;viewedIdentity=null;identityConfirmation=null;viewKey='';displayView=null;lastDisplay=null;selectedSeat=null;selectedAction=null;selectedTarget=null;choiceNoticeRevision=null;syncing=false;nextPollAt=0;pollNotice='';notice.textContent='';document.body.dataset.night='false';roomLabel.textContent='狼人杀';
 try{sessionStorage.removeItem(storeKey);}catch{}
 if(display)root.replaceChildren(h('h1','公共画面已关闭'),h('p','请从 Friday 重新打开。'));
 else if(reopenInvitation){code=reopenInvitation;joinForm();}
 else{const intro=h('div',null,'join-intro');intro.append(h('span',null,'moon'),h('h1','本局已关闭'),h('p','重新扫码，加入下一局。'));root.replaceChildren(intro,button('输入房间号',joinForm,'primary'));}
}
function revealRole(){
 if(!player?.role||!roleNode||document.hidden||identityStamp()&&syncing)return;
 peeked=true;roleNode.replaceChildren(h('strong',player.role_name));
 if(player.team)roleNode.append(h('p','狼人同伴 · '+player.team.filter(p=>p.seat!==player.seat).map(p=>p.seat+' 号 · '+p.name).join('、')));
 if(identityStamp())viewedIdentity=identityStamp();
 document.body.classList.add('peeking');updateIdentityConfirmation();
}
async function confirmIdentity(){
 const stamp=identityStamp();
 if(!stamp||viewedIdentity!==stamp||syncing||peeked||document.hidden||player.identity_confirmed)return;
 const {session,seat,public:{revision}}=player;
 await perform(async()=>{
  try{
   await api('identity',{revision},result=>{
    if(result.session!==session||result.seat!==seat||result.public?.revision!==revision||
       result.public.phase!=='identity'||result.identity_confirmed!==true)throw Error('identity_unavailable');
    updatePlayer(result,true);
   });
  }catch(error){
   if(error.message==='phase_changed'||error.message==='identity_unavailable'){
    holdView();
    return;
   }
   throw error;
  }
 });
}
function identity(){
 const section=h('section',null,'identity'),dealing=identityStamp()!==null;
 if(dealing)section.dataset.deal='true';
 roleNode=h('div',null,'role-card');hideRole();
 const reveal=button('按住查看身份',()=>{},'reveal-role');
 reveal.setAttribute('aria-label','按住查看自己的身份，松开收起');
 reveal.onpointerdown=e=>{reveal.setPointerCapture?.(e.pointerId);revealRole();};
 for(const event of ['pointerup','pointercancel','lostpointercapture'])reveal.addEventListener(event,hideRole);
 reveal.onkeydown=e=>{if(e.key===' '||e.key==='Enter'){e.preventDefault();if(!e.repeat)revealRole();}};
 reveal.onkeyup=e=>{if(e.key===' '||e.key==='Enter')hideRole();};
 reveal.onblur=hideRole;section.append(roleNode,reveal);
 if(dealing){
  if(player.identity_confirmed){
   const acknowledged=h('p','已确认身份，等待首夜。','identity-confirmed');
   acknowledged.setAttribute('role','status');section.append(acknowledged);
  }else{
   identityConfirmation=button('我已看完',()=>{void confirmIdentity();},'primary');
   updateIdentityConfirmation();section.append(identityConfirmation);
  }
 }
 return section;
}
function confirmChoice(result) {
 forgetChoice();
 choiceNoticeRevision=result.public.revision;
 notice.textContent='已确认，Friday 收到了你的选择。';
}
function updatePlayer(result, force=false) {
 invitationCode='';
 if(player?.public?.phase==='identity'&&(result.identity_confirmed||result.public.revision!==player.public.revision)){
  notice.textContent='';
 }
 if (choiceNoticeRevision!==null && choiceNoticeRevision!==result.public.revision) {
  notice.textContent='';choiceNoticeRevision=null;
 }
 const stamp=choiceStamp;
 if (stamp && (stamp.session!==result.session || stamp.seat!==result.seat)) forgetChoice();
 else if(stamp && result.public.phase==='lobby' && result.public.lobby_revision>stamp.revision){
  forgetChoice();notice.textContent='';
 }
 else if (stamp && result.choice_receipt?.event_id===stamp.event_id) {
  if (result.choice_receipt.recorded===true) {
   if (syncing) viewKey='';
   confirmChoice(result);
  }
  else if (stamp.revision!==result.public.revision || !result.actions.length) {
   forgetChoice();
   choiceNoticeRevision=result.public.revision;
   notice.textContent='上次选择未确认，请以当前环节为准。';
  }
 }
 player=result;
 if(viewedIdentity!==identityStamp())viewedIdentity=null;
 const resumed=syncing;
 syncing=false;
 viewUpdatedAt=performance.now();
 renderPlayer(force||resumed||stamp!==choiceStamp);
}
async function submitChoice() {
 if (syncing || !player) return;
 await perform(async () => {
  const payload={action:selectedAction,target:selectedTarget,revision:game.revision,
   event_id:Array.from(crypto.getRandomValues(new Uint8Array(16)), b=>b.toString(16).padStart(2,'0')).join('')};
  if (Number.isSafeInteger(player.choice_version)) payload.expected_choice_version=player.choice_version;
  rememberChoice(payload);
  try {
   await api('choice', payload, result => {
    if (choiceStamp && (result.session!==choiceStamp.session || result.seat!==choiceStamp.seat
        || result.choice_receipt?.event_id!==payload.event_id || result.choice_receipt.recorded!==true)) throw Error('network');
    if (!choiceStamp) confirmChoice(result);
    updatePlayer(result,true);
    navigator.vibrate?.(35);
   });
  } catch (error) {
   if (retired(error) || error.message.endsWith('access_required')) throw error;
   if (choiceRejected.has(error.message)) {
    forgetChoice();
    holdView();
    choiceNoticeRevision=game.revision;
    if (error.message==='choice_changed') viewKey='';
    throw error;
   }
   holdView();
  }
 });
}
async function finishSpeaking(revision) {
 if(syncing)return;
 await perform(async()=>{
  try{
   await api('speaker-done',{revision},data=>updatePlayer(data,true));
  }catch(error){
   if(retired(error)||error.message.endsWith('access_required'))throw error;
   holdView();
  }
 });
}
function actionPanel() {
 const section = h('section', null, 'action-panel');
 if (game.paused) {
  section.append(h('h2', '等待主持恢复'));
  return section;
 }
 if (!player.actions.length) {
  if (!player.alive) section.append(h('h2', '你已出局'), h('p', '可以继续观看，请保留本局身份。'));
  else if (game.phase === 'discussion' || game.phase === 'runoff_discussion') {
   const seat = game.speaker_seat;
   const turn = game.speaking_turn;
   const prompt = seat === player.seat ? '轮到你发言' : seat ? seat + ' 号正在发言' :
    game.phase === 'runoff_discussion' ? '请平票玩家发言' : '现在可以发言';
   section.append(h('h2', prompt));
   if(turn&&seat===player.seat){
    const revision=game.revision;
    section.append(button('发言完毕',()=>finishSpeaking(revision),'primary'));
   }else if(turn?.next_seat===player.seat){
    section.append(h('p','你是下一位。'));
   }
  }
  else section.append(h('h2', '等待你的回合'));
  return section;
 }
 const option = player.actions.find(entry => entry.action === selectedAction);
 const needsTarget = option && option.action !== 'pass' && option.action !== 'save';
 section.append(h('h2', needsTarget ? targetPrompts[option.action] : '轮到你行动'));
 if (choiceStamp?.revision===game.revision) {
  const status=h('p','上次提交尚未确认。','choice-status');
  status.setAttribute('role','status');
  section.append(status);
 }
 const actions = h('div', null, 'action-options');
 for (const entry of player.actions) {
  const action = button(names[entry.action], () => {
   if (selectedAction === entry.action) return;
   selectedAction = entry.action;
   selectedTarget = entry.action === 'save' ? entry.targets[0] : null;
   renderPlayer(true);
  });
  action.dataset.choiceFocus = 'action-' + entry.action;
  action.setAttribute('aria-pressed', String(entry.action === selectedAction));
  actions.append(action);
 }
 section.append(actions);
 if (option?.action === 'save' && selectedTarget) section.append(h('p', '将救起 ' + selectedTarget + ' 号。'));
 if (needsTarget) {
  const targets = h('div', null, 'seat-grid');
  for (const seat of option.targets) {
   const target = seatButton(seat, selectedTarget === seat, () => {
    selectedTarget = seat;
    renderPlayer(true);
   });
   target.dataset.choiceFocus = 'seat-' + seat;
   targets.append(target);
  }
  section.append(targets);
 }
 if (player.role === 'witch') {
  const victim = player.attacked_seat;
  const hint = Object.hasOwn(player, 'attacked_seat') ? (victim ? '本夜被袭击：' + victim + ' 号' : '本夜无人被袭击') : '解药已用尽，不再显示被袭击者。';
  section.append(h('p', hint, 'private-hint'));
 }
 if (player.submitted) {
  const [action, target] = player.submitted;
  section.append(h('p', '已提交 · ' + names[action] + (target ? ' ' + target + ' 号' : ''), 'submitted'));
 }
 const submit = button(player.submitted ? '更新选择' : '确认选择', () => {void submitChoice();}, 'primary');
 submit.dataset.choiceFocus = 'submit';
 submit.disabled = !option || option.action !== 'pass' && !option.targets.includes(selectedTarget);
 section.append(submit);
 return section;
}
function renderPlayer(force=false){
 if(!player||!alive)return;const g=player.public;game=g;const key=[g.revision,JSON.stringify(g.players),JSON.stringify(player.actions),JSON.stringify(player.submitted),JSON.stringify(player.checks),JSON.stringify(player.team_choices),player.identity_confirmed].join('|');if(!force&&key===viewKey){updateTimer(g);return;}
 const phaseChanged = !viewKey.startsWith(g.revision + '|');
 const focusChoice = phaseChanged ? null : document.activeElement?.dataset?.choiceFocus;
 if (phaseChanged) {
  const meaningful = player.actions.filter(entry => entry.action !== 'pass');
  const only = meaningful.length === 1 ? meaningful[0] : null;
  selectedAction = player.submitted?.[0] ?? (only && only.action !== 'save' && only.targets.length ? only.action : null);
  selectedTarget = choiceStamp?.revision===g.revision ? null : player.submitted?.[1] ?? null;
 }
 viewKey=key;hideRole();roleNode=null;identityConfirmation=null;root.replaceChildren();document.body.dataset.night=String(nightPhases.has(g.phase));roomLabel.textContent=player.seat+' 号 · '+player.name;root.append(phaseHeader(g));
 if (syncing) {
  const status=h('p','连接恢复后会自动更新。','party-sync');
  status.setAttribute('role','status');
  root.append(status);
  if (player.role && g.phase!=='finished' && g.phase!=='identity') root.append(identity());
  return;
 }
 if(g.phase==='lobby'){
  const waiting=h('section',null,'lobby-wait');waiting.append(h('div',String(player.seat),'my-seat'),h('h2','你的座位已留好'),h('p',g.players.filter(p=>p.ready).length+' / '+g.player_count+' 人准备好了'));
  waiting.append(button(player.ready?'取消准备':'准备好了',()=>perform(async()=>{
   await api('ready',{ready:!player.ready,lobby_revision:g.lobby_revision},data=>updatePlayer(data,true));
  }),player.ready?'':'primary'));
  root.append(waiting);return;
 }
 if(g.phase==='identity'){root.append(identity());return;}
 if(g.phase==='finished'){const result=h('section',null,'result');result.append(h('h2',g.winner==='village'?'好人阵营获胜':g.winner==='wolves'?'狼人阵营获胜':'本局已结束'));const list=h('ul');for(const p of g.players)list.append(h('li',p.seat+' 号 · '+p.name+'　'+p.role_name));result.append(list,h('p','留在这个页面，等待主持人开启下一局。'));root.append(result);return;}
 root.append(actionPanel(),identity());if(player.checks?.length){const checks=h('details',null,'private-records');checks.append(h('summary','我的查验记录'));for(const check of player.checks)checks.append(h('p','第 '+check.round+' 夜 · '+check.seat+' 号是'+(check.wolf?'狼人':'好人')));root.append(checks);}if(player.team_choices?.length){const team=h('details',null,'private-records');team.append(h('summary','同伴的本夜选择'));for(const c of player.team_choices)team.append(h('p',c.seat+' 号 → '+(c.target?c.target+' 号':'放弃')));root.append(team);}
 if (focusChoice) root.querySelector('[data-choice-focus="' + focusChoice + '"]')?.focus({preventScroll:true});
}
function updateTimer(g){const timer=root.querySelector('[data-timer]');if(timer)timer.textContent=g.paused?'已暂停':g.seconds_left==null?'':Math.max(0,g.seconds_left)+' 秒';}
function createDisplay() {
 const stage = h('section', null, 'public-stage');
 const content = h('div', null, 'public-stage-content');
 const heading = h('div', null, 'public-heading');
 const title = h('h1');
 const clock = h('span', null, 'public-clock');
 clock.setAttribute('role', 'timer');
 heading.append(title, clock);
 const script = h('p', null, 'public-script');
 script.setAttribute('aria-live', 'polite');
 const invitation = h('section', null, 'public-invitation');
 invitation.hidden = true;
 const qr = h('img', null, 'public-join-qr');
 qr.alt = '扫码加入本局';
 const instructions = h('div', null, 'public-join-info');
 const prompt = h('h2');
 const room = h('div', null, 'public-join-code');
 const number = h('strong');
 const address = h('p', null, 'public-join-address');
 room.append(h('span', '房间号'), number);
 instructions.append(prompt, room, address);
 invitation.append(qr, instructions);
 content.append(heading, script, invitation);
 const art = h('img', null, 'public-art');
 art.setAttribute('src', '/assets/werewolf-scene.png');
 art.setAttribute('alt', '');
 art.setAttribute('width', '1672');
 art.setAttribute('height', '941');
 const illustration = h('div', null, 'public-illustration');
 illustration.append(art);
 stage.append(content, illustration);

 const seating = h('section', null, 'public-seating');
 const seatHeading = h('div', null, 'public-seat-heading');
 const seatTitle = h('h2');
 const seatSummary = h('p');
 seatHeading.append(seatTitle, seatSummary);
 const roster = h('ol', null, 'display-roster');
 roster.setAttribute('role', 'list');
 const composition = h('p', null, 'composition');
 seating.append(seatHeading, roster, composition);

 const voting = h('section', null, 'public-voting');
 const votes = h('ul', null, 'public-votes');
 votes.setAttribute('role', 'list');
 voting.append(h('h2', '最近一次投票'), votes);
 root.replaceChildren(stage, seating, voting);
 qr.onerror = () => {
  if (invitation.hidden) return;
  qr.hidden = true;
  prompt.textContent = '用手机加入';
  address.hidden = false;
 };
 return {title, clock, script, seatTitle, seatSummary, roster, composition, voting, votes,
  invitation, qr, prompt, number, address, invitationKey:null, invitationRequest:null, invitationRetryAt:0};
}

function retireInvitation(view) {
 if (!view?.invitationKey) return;
 view.invitationRequest?.abort();
 view.invitationKey = view.invitationRequest = null;
 view.invitationRetryAt = 0;
 view.invitation.hidden = true;
 view.script.hidden = false;
 view.qr.removeAttribute('src');
 view.number.textContent = '';
 view.address.textContent = '';
}

async function updateInvitation(view, report, waiting) {
 const game = report.game;
 if (waiting || document.hidden || game.phase!=='lobby' || game.players.length>=game.player_count
     || typeof report.session!=='string' || !Number.isSafeInteger(game.lobby_revision)) {
  retireInvitation(view);
  return;
 }
 const key = report.session+':'+game.lobby_revision;
 if (view.invitationKey!==key) {
  retireInvitation(view);
  view.invitationKey=key;
 }
 if (!view.invitation.hidden || view.invitationRequest || performance.now()<view.invitationRetryAt) return;
 const controller = new AbortController();
 view.invitationRequest = controller;
 view.invitationRetryAt = performance.now()+4000;
 const current = () => view===displayView && view.invitationRequest===controller && !document.hidden;
 try {
  const response = await fetch('/api/invitation', {
   headers:{Authorization:'Bearer '+credential}, cache:'no-store',
   signal:AbortSignal.any([controller.signal, AbortSignal.timeout(6000)]),
  });
  if (!current()) return;
  if (response.status===429) {
   const delay = Number(response.headers.get('Retry-After'));
   view.invitationRetryAt = performance.now()+Math.max(4,Math.min(60,Number.isFinite(delay)?delay:5))*1000;
  }
  if (!response.ok) return;
  const invite = await response.json();
  if (!current() || invite?.session!==report.session || invite.lobby_revision!==game.lobby_revision
      || typeof invite.room_code!=='string' || !/^[0-9]{6}$/.test(invite.room_code)) return;
  const svg = invite.qr_svg;
  const qrReady = typeof svg==='string' && svg.length<=65_536 && /^[A-Za-z0-9+/]+={0,2}$/.test(svg);
  view.prompt.textContent = qrReady?'手机扫码入座':'用手机加入';
  view.number.textContent = invite.room_code;
  view.address.textContent = location.origin+'/phone';
  view.address.hidden = qrReady;
  view.qr.hidden = !qrReady;
  view.invitation.hidden = false;
  view.script.hidden = true;
  if (qrReady) view.qr.src = 'data:image/svg+xml;base64,'+svg;
 } catch {
  // 公屏仍按原节奏更新；下一次有效大厅状态可重试邀请。
 } finally {
  if (view.invitationRequest===controller) view.invitationRequest=null;
 }
}

function renderDisplay(report, waiting=false) {
 if (!report.active) {
  retireInvitation(displayView);
  displayView = null;
  document.body.dataset.night = 'false';
  roomLabel.textContent = '狼人杀';
  root.replaceChildren(h('h1','房间已关闭'), h('p','请从 Friday 打开新的公共画面。'));
  return;
 }
 const g = report.game;
 displayView ??= createDisplay();
 const view = displayView;
 const night = nightPhases.has(g.phase);
 const lobby = g.phase === 'lobby';
 const dealing = g.phase === 'identity';
 const readyForNight = dealing && g.identities_confirmed === g.player_count;
 const finished = g.phase === 'finished';
 document.body.dataset.night = String(night);
 roomLabel.textContent = g.round ? '狼人杀 · 第 ' + g.round + ' 轮' : '狼人杀 · ' + g.player_count + ' 人局';
 const speaker = waiting || g.paused ? null : g.speaker_seat;
 const turn = waiting || g.paused ? null : g.speaking_turn;
 const upcoming = turn?.next_seat;
 const turnPrompt = (g.phase === 'runoff_discussion' ? '平票发言 · ' : '') +
  (upcoming ? '下一位 ' + upcoming + ' 号' : '发言后进入投票');
 const title = waiting ? '正在同步对局' : readyForNight ? '准备入夜' : turn ? speaker + ' 号发言' : g.phase_name;
 const script = waiting ? '连接恢复后会自动更新。' : readyForNight ? '请把手机放低，等候首夜。' :
  g.paused ? '等待主持人恢复游戏。' : turn ? turnPrompt : (g.host_script || g.announcements?.at(-1))?.text || '';
 if (view.title.textContent !== title) view.title.textContent = title;
 if (view.script.textContent !== script) view.script.textContent = script;
 void updateInvitation(view, report, waiting);
 const seconds = Math.max(0, g.seconds_left ?? 0);
 view.clock.hidden = waiting || g.seconds_left == null && !g.paused;
 view.clock.textContent = view.clock.hidden ? '' : g.paused ? '已暂停' : String(Math.floor(seconds / 60)).padStart(2,'0') + ':' + String(seconds % 60).padStart(2,'0');
 if (view.clock.hidden) view.clock.removeAttribute('aria-label');
 else view.clock.setAttribute('aria-label', g.paused ? '本局已暂停' : '剩余 ' + seconds + ' 秒');

 const hints = waiting ? [] : report.camera_hints || [];
 const summary=finished||dealing&&waiting?'':dealing?(g.identities_confirmed??0)+' / '+g.player_count+' 已确认':
  lobby?g.players.filter(p=>p.ready).length+' / '+g.player_count+' 已准备':
   turn?'发言 '+turn.position+' / '+turn.total:g.players.filter(p=>p.alive).length+' / '+g.player_count+' 在场';
 if(view.seatSummary.textContent!==summary)view.seatSummary.textContent=summary;
 const rosterKey = JSON.stringify([lobby, finished, night, g.player_count, g.players, g.roles, hints, speaker, upcoming]);
 if (view.rosterKey !== rosterKey) {
  view.rosterKey = rosterKey;
  view.seatTitle.textContent = finished ? '身份揭晓' : '座位';
  const players = new Map(g.players.map(p => [p.seat, p]));
  const advice = new Map(hints.map(hint => [hint.seat, hint]));
  const seats = [];
  for (let seat = 1; seat <= g.player_count; seat++) {
   const p = players.get(seat);
   const hint = advice.get(seat);
   const item = h('li');
   item.dataset.state = !p ? 'empty' : p.alive === false ? 'out' : seat === speaker ? 'speaking' :
    seat === upcoming ? 'upcoming' : lobby && p.ready ? 'ready' : 'present';
   const label = h('div', null, 'public-seat-label');
   const name = h('strong', p?.name || '空位');
   if (p) name.title = p.name;
   label.append(name);
   const status = [];
   if (finished && p?.role_name) status.push(p.role_name);
   if (p?.alive === false) status.push('出局');
   else if (lobby && p) status.push(p.ready ? '已准备' : '未准备');
   else if (p && !night && !finished) {
    if (seat === speaker) status.push('发言中');
    else if (seat === upcoming) status.push('下一位');
    else if (hint?.hand_raised) status.push('举手');
    if (hint?.pointing_at) status.push('指向 ' + hint.pointing_at + ' 号');
   }
   if (status.length) label.append(h('small', status.join(' · '), hint || seat === speaker ? 'public-hint' : ''));
   item.append(h('span', String(seat), 'display-seat'), label);
   seats.push(item);
  }
  view.roster.replaceChildren(...seats);
  view.composition.hidden = !lobby;
  view.composition.textContent = lobby ? (g.roles || []).map(r => r.name + ' × ' + r.count).join('　') : '';
 }
 const votes = g.vote_result?.votes || [];
 const voteKey = JSON.stringify(votes);
 if (view.voteKey !== voteKey) {
  view.voteKey = voteKey;
  view.voting.hidden = !votes.length;
  view.votes.replaceChildren(...votes.map(vote => h('li', vote.seat + ' 号 → ' + (vote.target ? vote.target + ' 号' : '弃票'))));
 }
}
function holdView() {
 if (!credential || syncing) return;
 syncing=true;
 if (lastDisplay?.active) renderDisplay(lastDisplay,true);
 else if (player) renderPlayer(true);
}
async function poll() {
 if (!alive || busy || readRequest || document.hidden || !credential || performance.now()<nextPollAt) return;
 const request={controller:new AbortController()};
 readRequest=request;
 try {
  await api(display?'public':'player',undefined,data=>{
   if (notice.textContent===pollNotice || notice.textContent===problems.party_rate_limited) notice.textContent='';
   pollNotice='';
   if (display) {
    lastDisplay=data;syncing=false;viewUpdatedAt=performance.now();
    renderDisplay(data);
   } else updatePlayer(data);
  });
 } catch (error) {
  if (error.message.endsWith('access_required')) closedSession();
  else if (!retired(error)) {
   holdView();
   if (!player && !lastDisplay) {
    pollNotice=error.message==='party_rate_limited'?'正在同步对局…':problems.network;
    notice.textContent=pollNotice;
   }
  }
 } finally {
  if (readRequest===request) readRequest=null;
 }
}
document.body.classList.toggle('display',display);
if (display) {
 const brand = document.getElementById('partyHome');
 brand?.removeAttribute('href');
 brand?.removeAttribute('aria-label');
}
function refresh() {
 if (document.hidden || !alive) return;
 if (!busy && (player || lastDisplay) && performance.now()-viewUpdatedAt>3000) holdView();
 void poll();
}
function reopenScannedInvitation() {
 if (!alive) return;
 const scanned = roomCode(new URLSearchParams(location.hash.slice(1)));
 if (!scanned) return;
 history.replaceState(null,'',location.pathname);
 invitationCode=scanned;
 if (credential) {
  // 先读取现有座位；有效座位继续使用，撤销后才回到选座。
  const oldRead=readRequest;
  readRequest=null;
  oldRead?.controller.abort();
  void poll();
  return;
 }
 if (joinPending?.code===scanned) {
  if (!busy) {joinRecovery();void submitJoin(joinPending);}
  return;
 }
 retireRequests();
 forgetJoin();
 code=scanned;
 notice.textContent='';
 joinForm();
}
if(!display)window.addEventListener('hashchange',reopenScannedInvitation);
window.addEventListener('blur',hideRole);
document.addEventListener('visibilitychange',()=>{hideRole();if(document.hidden)retireInvitation(displayView);refresh();});
window.addEventListener('offline',holdView);
window.addEventListener('online',refresh);
window.addEventListener('pagehide',()=>{alive=false;retireRequests();hideRole();});
window.addEventListener('pageshow',event=>{
 if (!event.persisted) return;
 alive=true;
 if (credential) {holdView();refresh();}
 else if (joinPending) joinRecovery();
});
if(credential)void poll();else if(display)root.replaceChildren(h('h1','请从 Friday 打开公共画面'));else if(joinPending){joinRecovery();void submitJoin(joinPending);}else joinForm();
setInterval(refresh,1000);
