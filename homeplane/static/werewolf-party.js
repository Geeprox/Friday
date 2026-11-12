const root=document.getElementById('party'),notice=document.getElementById('notice'),roomLabel=document.getElementById('roomLabel');
const display=location.pathname==='/display',fragment=new URLSearchParams(location.hash.slice(1));
const invitationCode=fragment.get('code')||'';
const storeKey=display?'Friday.werewolf.display':'Friday.werewolf.player';
const joinStoreKey='Friday.werewolf.join';
let credential='',code=invitationCode,alive=true,busy=false,readRequest=null,commandRequest=null,player=null,game=null,viewKey='',selectedAction=null,selectedTarget=null,selectedSeat=null,peeked=false,roleNode=null,pending=null,nextPollAt=0,pollNotice='',sessionGeneration=0;
let joinPending=null,joinName='';
try{credential=display?(fragment.get('token')||sessionStorage.getItem(storeKey)||''):(sessionStorage.getItem(storeKey)||'');if(credential)sessionStorage.setItem(storeKey,credential);}catch{}
if(!display)try{if(credential)sessionStorage.removeItem(joinStoreKey);else{const raw=sessionStorage.getItem(joinStoreKey);if(raw&&raw.length<=2048)joinPending=joinAttempt(JSON.parse(raw));if(joinPending&&invitationCode&&joinPending.code!==invitationCode)joinPending=null;if(joinPending){code=joinPending.code;joinName=joinPending.name;}else sessionStorage.removeItem(joinStoreKey);}}catch{}
history.replaceState(null,'',location.pathname);
const names={attack:'袭击',check:'查验',guard:'守护',save:'使用解药',poison:'使用毒药',vote:'投票',shoot:'开枪',pass:'放弃本次行动'};
const problems={room_not_found:'没有找到这个房间，请核对房间号。',seat_unavailable:'这个座位刚被选走，请换一个。',join_closed:'本局已经开始，请等下一局。',join_recovery_full:'暂时无法入座，请让主持人重新创建房间。',phase_changed:'已进入下一环节，这次选择没有提交。',action_unavailable:'当前环节无法进行这项操作。',target_invalid:'请选择当前可用的座位。',party_rate_limited:'连接正在恢复，请稍后再试。',event_conflict:'选择已变化，请重新确认。',network:'连接暂时中断，正在重试。'};
const h=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
const button=(text,fn,cls)=>{const n=h('button',text,cls);n.type='button';n.onclick=e=>{if(!busy&&!n.disabled)return fn(e);};return n;};
const retired=e=>e.message==='session_changed'||e.message==='response_retired';
function joinAttempt(value){return value&&typeof value==='object'&&typeof value.code==='string'&&typeof value.session==='string'&&typeof value.attempt_id==='string'&&/^[0-9]{6}$/.test(value.code)&&/^[a-f0-9]{32}$/.test(value.session)&&/^[a-f0-9]{64}$/.test(value.attempt_id)&&Number.isInteger(value.seat)&&value.seat>=1&&value.seat<=12&&typeof value.name==='string'&&value.name.trim().length>0&&value.name.length<=20?{code:value.code,session:value.session,seat:value.seat,name:value.name,attempt_id:value.attempt_id}:null;}
function forgetJoin(){joinPending=null;try{sessionStorage.removeItem(joinStoreKey);}catch{}}
function joinRecovery(){
 if(!joinPending||!alive)return;root.replaceChildren();document.body.dataset.night='false';roomLabel.textContent='狼人杀';
 const intro=h('section',null,'join-intro');intro.append(h('span',null,'moon'),h('h1','入座尚未确认'),h('p',joinPending.seat+' 号 · '+joinPending.name));
 const actions=h('div',null,'join-form');actions.append(button('重试入座',()=>{void submitJoin(joinPending);},'primary'),button('更换房间',()=>{forgetJoin();code='';game=null;selectedSeat=null;notice.textContent='';joinForm();}));root.append(intro,actions);
}
async function submitJoin(value){
 if(busy||!alive)return;const payload=joinAttempt(value);if(!payload){notice.textContent='入座暂不可用，请重新载入。';return;}
 joinPending=payload;joinName=payload.name;try{sessionStorage.setItem(joinStoreKey,JSON.stringify(payload));}catch{}
 const generation=sessionGeneration;
 await perform(async()=>{try{await api('join',payload,result=>{
  if(!/^[a-f0-9]{64}$/.test(result.token)||result.session!==payload.session||result.player?.seat!==payload.seat)throw Error('network');
  credential=result.token;let saved=false;try{sessionStorage.setItem(storeKey,credential);saved=true;}catch{}
  joinPending=null;if(saved)try{sessionStorage.removeItem(joinStoreKey);}catch{}
  player=result.player;game=player.public;viewKey='';renderPlayer(true);
 });}catch(e){if(['room_not_found','seat_unavailable','join_closed','event_conflict','join_recovery_full'].includes(e.message))forgetJoin();throw e;}});
 if(!alive||generation!==sessionGeneration||credential)return;
 if(joinPending)joinRecovery();else joinForm();
}
async function api(path,body,apply=()=>{}){
 const generation=sessionGeneration,request=body===undefined?readRequest:commandRequest;
 const current=()=>request&&(body===undefined?readRequest:commandRequest)===request;
 try{
  if(!current())throw Error('response_retired');
  const response=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json',...(credential?{Authorization:'Bearer '+credential}:{})},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.any([AbortSignal.timeout(6000),request.controller.signal]),cache:'no-store'}),data=await response.json();
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
 const controls=Array.from(root.querySelectorAll('button,input')).filter(n=>n.className!=='reveal-role').map(n=>[n,n.disabled]);for(const[n]of controls)n.disabled=true;
 try{await fn();}catch(e){if(e.message.endsWith('access_required'))closedSession();else if(!retired(e))sayError(e);}finally{if(commandRequest===request){commandRequest=null;busy=false;for(const[n,disabled]of controls)n.disabled=disabled;root.removeAttribute('aria-busy');}}
}
function phaseHeader(g){const header=h('div',null,'phase');header.append(h('span',null,'moon'),h('h1',g.phase_name),h('p',g.round?'第 '+g.round+' 轮':'手机入座 · 私下行动'));const timer=h('strong',g.paused?'已暂停':g.seconds_left==null?'':g.seconds_left+' 秒','timer');timer.dataset.timer='true';header.append(timer);return header;}
function seatButton(seat,selected,fn,extra=''){const p=game?.players.find(x=>x.seat===seat),b=button('',fn,'seat-choice');b.append(h('strong',String(seat)),h('span',p?.name||extra||'空位'));b.setAttribute('aria-pressed',String(selected));return b;}
function joinForm(){
 root.replaceChildren();document.body.dataset.night='false';roomLabel.textContent='狼人杀';const intro=h('div',null,'join-intro');intro.append(h('span',null,'moon'),h('h1','一起入座'),h('p','每人一部手机，身份与行动只给自己看。'));root.append(intro);
 let loadedCode='';const form=h('form',null,'join-form'),room=h('label','房间号'),roomInput=h('input');roomInput.type='text';roomInput.inputMode='numeric';roomInput.pattern='[0-9]{6}';roomInput.maxLength=6;roomInput.autocomplete='off';roomInput.value=code;roomInput.required=true;room.append(roomInput);const find=button('查看座位',lookup);form.append(room,find);const seats=h('div',null,'join-seats');form.append(seats);const name=h('label','你的名字'),nameInput=h('input');nameInput.maxLength=20;nameInput.autocomplete='nickname';nameInput.required=true;name.append(nameInput);const join=h('button','坐好了','primary');join.type='submit';join.disabled=true;form.append(name,join);
 roomInput.addEventListener('input',()=>{code=roomInput.value;loadedCode='';selectedSeat=null;game=null;notice.textContent='';renderSeats();});
 nameInput.value=joinName;
 form.onsubmit=e=>{e.preventDefault();if(busy||!selectedSeat||!loadedCode||loadedCode!==roomInput.value)return;void submitJoin({code:loadedCode,session:game?.session,seat:selectedSeat,name:nameInput.value.trim(),attempt_id:Array.from(crypto.getRandomValues(new Uint8Array(32)),b=>b.toString(16).padStart(2,'0')).join('')});};
 function lookup(){void perform(async()=>{const requested=roomInput.value;code=requested;loadedCode='';selectedSeat=null;game=null;renderSeats();await api('lobby',{code:requested},data=>{if(!credential&&roomInput.value===requested){loadedCode=requested;game=data;}});}).finally(renderSeats);}
 function renderSeats(){seats.replaceChildren();join.disabled=!selectedSeat||loadedCode!==roomInput.value||!game||game.players.some(p=>p.seat===selectedSeat);if(!game||loadedCode!==roomInput.value)return;seats.append(h('p','选一个空座位，和电视上的编号一致。'));const grid=h('div',null,'seat-grid');for(let i=1;i<=game.player_count;i++){const occupied=game.players.some(p=>p.seat===i),b=seatButton(i,selectedSeat===i,()=>{if(!game||loadedCode!==roomInput.value)return;selectedSeat=i;renderSeats();});b.disabled=occupied;grid.append(b);}seats.append(grid);}
 root.append(form);if(code.length===6)lookup();
}
function hideRole(){peeked=false;if(roleNode){roleNode.replaceChildren(h('span',null,'role-seal'),h('strong','身份已收起'),h('span','按住下方按钮查看'));}document.body.classList.remove('peeking');}
function closedSession(){
 forgetJoin();joinName='';
 sessionGeneration++;const read=readRequest,command=commandRequest;readRequest=null;commandRequest=null;read?.controller.abort();command?.controller.abort();busy=false;root.removeAttribute('aria-busy');hideRole();credential='';code='';player=null;game=null;roleNode=null;viewKey='';selectedSeat=null;selectedAction=null;selectedTarget=null;pending=null;nextPollAt=0;pollNotice='';notice.textContent='';document.body.dataset.night='false';roomLabel.textContent='狼人杀';
 try{sessionStorage.removeItem(storeKey);}catch{}
 if(display)root.replaceChildren(h('h1','公共画面已关闭'),h('p','请从 Friday 重新打开。'));
 else{const intro=h('div',null,'join-intro');intro.append(h('span',null,'moon'),h('h1','本局已关闭'),h('p','重新扫码，加入下一局。'));root.replaceChildren(intro,button('输入房间号',joinForm,'primary'));}
}
function revealRole(){if(!player?.role||!roleNode)return;peeked=true;roleNode.replaceChildren(h('strong',player.role_name));if(player.team)roleNode.append(h('p','狼人同伴：'+player.team.filter(p=>p.seat!==player.seat).map(p=>p.seat+' 号 · '+p.name).join('、')));document.body.classList.add('peeking');}
function identity(){const section=h('section',null,'identity');roleNode=h('div',null,'role-card');hideRole();const reveal=button('按住查看身份',()=>{},'reveal-role');reveal.setAttribute('aria-label','按住查看自己的身份，松开收起');reveal.onpointerdown=e=>{reveal.setPointerCapture?.(e.pointerId);revealRole();};for(const event of ['pointerup','pointercancel','lostpointercapture'])reveal.addEventListener(event,hideRole);reveal.onkeydown=e=>{if(e.key===' '||e.key==='Enter'){e.preventDefault();revealRole();}};reveal.onkeyup=e=>{if(e.key===' '||e.key==='Enter')hideRole();};reveal.onblur=hideRole;section.append(roleNode,reveal);return section;}
function actionPanel(){
 const section=h('section',null,'action-panel');if(game.paused){section.append(h('h2','稍等片刻'),h('p','主持端暂停了本局。'));return section;}if(!player.alive&&!player.actions.length){section.append(h('h2','你已出局'),h('p','可以继续观看，请保留本局身份。'));return section;}if(!player.actions.length){section.append(h('h2',game.phase==='discussion'?'轮到大家推理':'等待你的回合'),h('p',game.phase==='discussion'?'举手参与发言，随后在手机上投票。':'留意 Friday 的主持提示。'));return section;}
 section.append(h('h2','轮到你行动'));const actions=h('div',null,'action-options');for(const option of player.actions){const b=button(names[option.action],()=>{selectedAction=option.action;selectedTarget=option.action==='save'?option.targets[0]:null;pending=null;renderPlayer(true);});b.setAttribute('aria-pressed',String(option.action===selectedAction));actions.append(b);}section.append(actions);const option=player.actions.find(a=>a.action===selectedAction);if(option?.action==='save'&&selectedTarget)section.append(h('p','将救起 '+selectedTarget+' 号。'));if(option&&option.action!=='pass'&&option.action!=='save'){const targets=h('div',null,'seat-grid');for(const seat of option.targets)targets.append(seatButton(seat,selectedTarget===seat,()=>{selectedTarget=seat;pending=null;renderPlayer(true);}));section.append(targets);}if(player.role==='witch'){const victim=player.attacked_seat;section.append(h('p',Object.hasOwn(player,'attacked_seat')?(victim?'本夜被袭击：'+victim+' 号':'本夜无人被袭击'):'解药已用尽，不再显示被袭击者。','private-hint'));}
 if(player.submitted)section.append(h('p','已提交：'+names[player.submitted[0]]+(player.submitted[1]?' '+player.submitted[1]+' 号':'')+'。环节结束前可以修改。','submitted'));
 const submit=button(player.submitted?'更新选择':'确认选择',()=>{void perform(async()=>{const payload={action:selectedAction,target:selectedTarget,revision:game.revision};if(!pending||pending.action!==payload.action||pending.target!==payload.target||pending.revision!==payload.revision)pending={...payload,event_id:Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('')};await api('choice',pending,result=>{player=result;game=result.public;pending=null;notice.textContent='已确认，Friday 收到了你的选择。';navigator.vibrate?.(35);renderPlayer(true);});});},'primary');submit.disabled=!option||option.action!=='pass'&&selectedTarget==null;section.append(submit);return section;
}
function renderPlayer(force=false){
 if(!player||!alive)return;const g=player.public;game=g;const key=[g.revision,JSON.stringify(g.players),JSON.stringify(player.submitted),JSON.stringify(player.checks),JSON.stringify(player.team_choices)].join('|');if(!force&&key===viewKey){updateTimer(g);return;}const phaseChanged=!viewKey.startsWith(g.revision+'|');if(phaseChanged){selectedAction=null;selectedTarget=null;pending=null;}viewKey=key;hideRole();root.replaceChildren();document.body.dataset.night=String(['guard','wolves','seer','witch'].includes(g.phase));roomLabel.textContent=player.seat+' 号 · '+player.name;root.append(phaseHeader(g));
 if(g.phase==='lobby'){const waiting=h('section',null,'lobby-wait');waiting.append(h('div',String(player.seat),'my-seat'),h('h2','你的座位已留好'),h('p',g.players.filter(p=>p.ready).length+' / '+g.player_count+' 人准备好了'));waiting.append(button(player.ready?'取消准备':'准备好了',()=>{void perform(async()=>{await api('ready',{ready:!player.ready},data=>{player=data;renderPlayer(true);});});},player.ready?'':'primary'));root.append(waiting);return;}
 if(g.phase==='finished'){const result=h('section',null,'result');result.append(h('h2',g.winner==='village'?'好人阵营获胜':g.winner==='wolves'?'狼人阵营获胜':'本局已结束'));const list=h('ul');for(const p of g.players)list.append(h('li',p.seat+' 号 · '+p.name+'　'+p.role_name));result.append(list,h('p','由主持端创建下一局后，重新扫码入座。'));root.append(result);return;}
 root.append(identity(),actionPanel());if(player.checks?.length){const checks=h('details',null,'private-records');checks.append(h('summary','我的查验记录'));for(const check of player.checks)checks.append(h('p','第 '+check.round+' 夜 · '+check.seat+' 号是'+(check.wolf?'狼人':'好人')));root.append(checks);}if(player.team_choices?.length){const team=h('details',null,'private-records');team.append(h('summary','同伴的本夜选择'));for(const c of player.team_choices)team.append(h('p',c.seat+' 号 → '+(c.target?c.target+' 号':'放弃')));root.append(team);}
}
function updateTimer(g){const timer=root.querySelector('[data-timer]');if(timer)timer.textContent=g.paused?'已暂停':g.seconds_left==null?'':Math.max(0,g.seconds_left)+' 秒';}
function renderDisplay(report){
 if(!report.active){root.replaceChildren(h('h1','房间已关闭'),h('p','请从 Friday 打开新的公共画面。'));return;}const g=report.game,key=JSON.stringify([g.revision,g.players,g.vote_result,report.camera_hints]);if(key===viewKey){updateTimer(g);return;}viewKey=key;root.replaceChildren();document.body.dataset.night=String(['guard','wolves','seer','witch'].includes(g.phase));roomLabel.textContent=g.player_count+' 人 · 狼人杀';root.append(phaseHeader(g));const roster=h('ol',null,'display-roster');for(let seat=1;seat<=g.player_count;seat++){const p=g.players.find(x=>x.seat===seat),item=h('li');item.dataset.alive=String(p?.alive!==false);item.append(h('span',String(seat),'display-seat'),h('strong',p?.name||'等待入座'),h('small',g.phase==='lobby'?p?.ready?'已准备':'':p?.alive?'在场':'已出局'));if(p?.role_name)item.append(h('span',p.role_name));const hint=report.camera_hints?.find(x=>x.seat===seat);if(hint?.hand_raised)item.append(h('small','正在举手'));if(hint?.pointing_at)item.append(h('small','指向 '+hint.pointing_at+' 号'));roster.append(item);}root.append(roster);const script=g.host_script||g.announcements.at(-1);if(script)root.append(h('p',script.text,'public-script'));if(g.vote_result){const votes=h('div',null,'public-votes');for(const vote of g.vote_result.votes)votes.append(h('span',vote.seat+' → '+(vote.target||'弃票')));root.append(votes);}root.append(h('p',g.roles.map(r=>r.name+' × '+r.count).join('　'),'composition'));
}
async function poll(){
 if(!alive||busy||readRequest||document.hidden||!credential||performance.now()<nextPollAt)return;const request={controller:new AbortController()};readRequest=request;
 try{await api(display?'public':'player',undefined,data=>{if(display)renderDisplay(data);else{player=data;renderPlayer();}});if(readRequest!==request)return;if(notice.textContent===pollNotice||notice.textContent===problems.party_rate_limited)notice.textContent='';pollNotice='';}
 catch(e){if(e.message.endsWith('access_required'))closedSession();else if(!retired(e)){const message=e.message==='party_rate_limited'?'正在同步对局…':problems.network;if(!notice.textContent||notice.textContent===pollNotice)notice.textContent=message;pollNotice=message;}}
 finally{if(readRequest===request)readRequest=null;}
}
document.body.classList.toggle('display',display);window.addEventListener('blur',hideRole);document.addEventListener('visibilitychange',()=>{hideRole();if(!document.hidden)void poll();});window.addEventListener('pagehide',()=>{alive=false;hideRole();});
if(credential)void poll();else if(display)root.replaceChildren(h('h1','请从 Friday 打开公共画面'));else if(joinPending){joinRecovery();void submitJoin(joinPending);}else joinForm();
setInterval(()=>{void poll();},1000);
