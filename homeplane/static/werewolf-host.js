import {mountSeatCamera} from './werewolf-camera-view.js';

const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
const button=(text,fn,cls)=>{const n=node('button',text,cls);n.type='button';n.onclick=fn;return n;};
const errors={players_not_ready:'等大家准备好后再发身份。',identities_pending:'等大家确认身份后，再开始首夜。',phone_address_unavailable:'局域网连接发生变化，请重新选择。',game_already_exists:'已有一个房间，请先结束当前房间。',scene_recovery_required:'请先恢复上局灯光，或取消选择氛围灯。',scene_light_brightness_unavailable:'这盏灯暂时无法读回亮度，请取消选择后重试。',phase_changed:'对局已经更新，请重新操作。',speaker_unavailable:'当前无法安排发言，请查看对局进度。',camera_binding_changed:'座位对应已经更新，请重新选择。',room_changed:'房间已经更新，请在当前对局重试。',event_conflict:'这次操作已更新，请重新选择。',camera_track_unavailable:'画面中的位置已经变化，请重新选择。',game_unavailable:'暂时无法完成，请重试。'};

export function mountWerewolf(root,{go=()=>{}}={}){
 let alive=true,busy=false,context=null,current=null,revision='',lastNotice='',confirmation=null,preferredCount=9;
 let generation=0,readRequest=null,commandRequest=null,syncFailed=false,lobbyUI=null;
 const syncNotice='对局服务暂时无法连接，正在重试。';
 const abort=new AbortController(),wrap=node('section',null,'werewolf-host'),notice=node('p',null,'ww-notice'),content=node('div');notice.setAttribute('role','status');wrap.append(notice,content);root.append(wrap);
 async function api(path,body,signal){const signals=[abort.signal,AbortSignal.timeout(10000)];if(signal)signals.push(signal);const r=await fetch('/api/games/werewolf/'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.any(signals)});const data=await r.json();if(!r.ok)throw Error(data.detail||'game_unavailable');return data;}
 async function perform(fn){
  if(busy||!alive)return;
  const request={generation:++generation,controller:new AbortController(),session:current?.session,revision:current?.game?.revision,event:null,focused:document.activeElement};
  readRequest?.controller.abort();readRequest=null;commandRequest=request;
  const owns=()=>alive&&commandRequest===request&&generation===request.generation;
  const commandApi=(path,body)=>{if(path==='control'||path==='camera-bind'||path==='camera-unbind'){request.event??=crypto.randomUUID();body={...body,[path==='control'?'session':'game_session']:request.session,revision:request.revision,event_id:request.event};}return api(path,body,request.controller.signal);};
  busy=true;syncFailed=false;lastNotice='';notice.textContent='';content.setAttribute('aria-busy','true');updateControls();
  try{
   const data=await fn(commandApi);if(!owns())return;
   current=data;render();
   // An acknowledged close stays visible even if refreshing setup choices fails.
   if(!data.active){const nextContext=await commandApi('context');if(!owns())return;context=nextContext;current=nextContext.current;render();}
  }catch(e){if(owns()){lastNotice=errors[e.message]||'暂时无法完成，请稍后重试。';notice.textContent=lastNotice;}}
  finally{
   if(commandRequest===request){
    commandRequest=null;busy=false;
    if(alive){
     content.removeAttribute('aria-busy');updateControls();
     if(lobbyUI?.alignment)updateAlignment(lobbyUI.alignment,true);
     restoreControlFocus(request);
    }
   }
  }
 }
 function restoreControlFocus(request){
  if(current?.session!==request.session||document.activeElement!==document.body)return;
  const previous=request.focused;
  if(previous?.tagName==='SELECT'&&content.contains(previous)){
   previous.focus({preventScroll:true});
  }else if(previous?.dataset.speakerCommand){
   const action=content.querySelector('[data-speaker-command]');
   const next=action&&!action.disabled?action:content.querySelector('[data-speaker-picker]');
   next?.focus({preventScroll:true});
  }
 }
 function rules(game){const details=node('details',null,'ww-rules'),summary=node('summary','本局规则');details.append(summary);const list=node('ul');for(const text of game.rules||[])list.append(node('li',text));details.append(list);return details;}
 function check(label,value=false){const row=node('label',null,'ww-check'),input=node('input');input.type='checkbox';input.checked=value;row.append(input,node('span',label));return{row,input};}
 function setup(){
  const intro=node('div',null,'ww-intro');intro.append(node('h2','狼人杀'),node('p','手机入座，私下查看身份。Friday 陪大家玩完整局。'));content.append(intro);
  const launch=node('div',null,'ww-launch-layout'),cover=node('img',null,'ww-launch-cover');cover.src='/static/games-art/werewolf.png';cover.alt='狼人杀封面插画';cover.width=1672;cover.height=941;launch.append(cover);const form=node('form',null,'ww-setup'),fields=node('div',null,'ww-setup-fields');form.onsubmit=e=>{e.preventDefault();void perform(api=>api('create',{players:Number(count.value),address:network.value||null,narration:speech.input.checked,camera:camera.input.checked,lights:lights.filter(x=>x.input.checked).map(x=>x.id)}));};
  const countLabel=node('label','本局人数','ww-field'),count=node('select');for(let i=6;i<=12;i++){const o=node('option',i+' 人');o.value=String(i);count.append(o);}count.value=String(preferredCount);countLabel.append(count);
  const networkLabel=node('label','手机加入','ww-field'),network=node('select');for(const address of context.addresses||[]){const o=node('option',(context.addresses||[]).length===1?'当前网络':'局域网 · '+address);o.value=address;network.append(o);}if(!network.children.length){const o=node('option','暂未连接局域网');o.value='';network.append(o);}networkLabel.append(network);fields.append(countLabel,networkLabel);form.append(fields);
  const speech=check('Friday 语音主持',Boolean(context.speech?.configured&&!context.speech?.quiet));speech.input.disabled=!context.speech?.configured||context.speech?.quiet;const camera=check('使用已运行的摄像头辅助举手与指认');form.append(speech.row);if(speech.input.disabled){const hint=node('p',null,'ww-field-help');hint.append(node('span',context.speech?.quiet?'播报已静音。':'还未设置播报设备。'),button('设置播报',()=>go('Settings'),'ww-text-button'));form.append(hint);}form.append(camera.row);
  const lights=[];if(context.lights?.length){const group=node('fieldset',null,'ww-light-choice');group.append(node('legend','氛围灯'));for(const item of context.lights){const c=check((item.room?item.room+' · ':'')+item.name);lights.push({...c,id:item.id});group.append(c.row);}group.append(node('p','夜间调暗，天亮恢复明亮；本局结束后恢复原状。','ww-field-help'));form.append(group);}
  if(context.recovery?.recovery_pending){const recovery=node('div',null,'ww-recovery');recovery.append(node('p','上局灯光还有待恢复。'),button('恢复灯光',()=>perform(async api=>{await api('restore-lights',{});return api('host');})));form.append(recovery);}
  const create=node('button','创建房间','primary');create.type='submit';create.dataset.unavailable=String(!(context.addresses||[]).length);create.disabled=create.dataset.unavailable==='true';form.append(create);if(create.disabled)form.append(node('p','将手机与 Friday 连到同一局域网后，即可创建房间。','ww-field-help'));launch.append(form);content.append(launch);
  const how=node('div',null,'ww-how');for(const [title,text] of [['手机入座','每人用自己的手机选择座位。'],['私下行动','身份与夜间选择，只在自己的手机上显示。'],['一起推理','Friday 计时主持，电视展示公开进展。']]){const item=node('div');item.append(node('h3',title),node('p',text));how.append(item);}content.append(how,rules({rules:context.rules}));
 }
 function confirmControl(action){
  if(confirmation!==action){
   confirmation=action;lastNotice='';
   notice.textContent=({close:'关闭房间后，手机将退出本局。',reseat:'返回入座后会重新发牌。',advance:'提前结束后，尚未提交的行动将按放弃处理。'})[action];
   render();if(action==='reseat')content.querySelector('[data-reseat]')?.focus({preventScroll:true});return;
  }
  confirmation=null;void perform(api=>api('control',{action}));
 }
 function board(game){
  const scene=node('section',null,'ww-scene'),lobby=game.phase==='lobby',identity=game.phase==='identity';
  scene.dataset.count=String(game.player_count);scene.dataset.night=String(['guard','wolves','seer','witch'].includes(game.phase));
  const title=node('div',null,'ww-phase-heading');
  const phaseTitle=node('h3',game.phase_name);
  if(identity)phaseTitle.dataset.identityTitle='true';
  title.append(node('span',null,'ww-moon'),phaseTitle);
  if(game.round||lobby)title.append(node('p',game.round?'第 '+game.round+' 轮':'等大家准备好'));
  const time=node('strong',game.paused?'已暂停':game.seconds_left==null?'':Math.max(0,game.seconds_left)+' 秒','ww-timer');
  time.hidden=!game.paused&&game.seconds_left==null;time.dataset.timer='true';title.append(time);scene.append(title);
  const roster=node('ol',null,'ww-roster');
  const discussing=game.phase==='discussion'||game.phase==='runoff_discussion';
  for(let seat=1;seat<=game.player_count;seat++){
   const p=game.players.find(x=>x.seat===seat),item=node('li',null,'ww-seat');
   item.dataset.seat=String(seat);item.dataset.alive=String(p?.alive!==false);item.dataset.ready=String(lobby&&Boolean(p?.ready));
   const speaking=!game.paused&&game.speaker_seat===seat;
   item.dataset.speaking=String(speaking);
   const state=!p||identity?'':lobby?p.ready?'已准备':'未准备':!p.alive?'已出局':speaking?'发言中':discussing?'':'在场';
   item.append(node('span',String(seat),'ww-seat-number'),node('strong',p?.name||'等待入座'),node('span',state,'ww-seat-state'));
   if(game.phase==='finished'&&p?.role_name)item.append(node('span',p.role_name,'ww-seat-role'));
   if(lobby&&p)item.append(button('移除',()=>perform(api=>api('control',{action:'remove',seat})),'ww-remove'));
   if(!lobby&&!identity&&game.phase!=='finished'){
    const signal=node('span',null,'ww-seat-signal');
    signal.hidden=true;
    item.append(signal);
   }
   roster.append(item);
  }
  scene.append(roster);
  const latest=game.host_script||game.announcements?.at(-1);
  if(latest)scene.append(node('p',game.paused?'':latest.text,'ww-host-line'));
  return scene;
 }
 function candidateKey(candidate){return JSON.stringify([candidate.camera,candidate.session,candidate.track]);}
 function updateAlignment(ui,force=false){
  const candidates=ui.view?.candidates(current.camera_candidates||[])||current.camera_candidates||[],players=current.game.players,available=ui.view?.available(current.camera_available)??current.camera_available===true;
  for(const [seat,row]of ui.rows){if(!players.some(p=>p.seat===seat&&p.name===row.name)){row.label.remove();ui.rows.delete(seat);}}
  for(const player of players){
   let row=ui.rows.get(player.seat);
   if(!row){
    const label=node('label',null,'ww-camera-binding'),title=node('span',player.seat+' 号 · '+player.name),select=node('select'),empty=node('option','选择镜头中的编号');empty.value='';empty.disabled=true;label.append(title,select);select.append(empty);
    const next=[...ui.rows].filter(([seat])=>seat>player.seat).sort((a,b)=>a[0]-b[0])[0]?.[1].label;
    row={label,select,empty,name:player.name,options:new Map()};ui.rows.set(player.seat,row);ui.grid.insertBefore(label,next||null);
    select.onchange=()=>{
     if(!alive||!current?.active||current.session!==ui.session||current.game.phase!=='lobby'||!content.contains(select))return;
     if(select.value==='__clear__'){
      const binding=current.camera_bindings?.find(b=>b.seat===player.seat);
      if(binding)void perform(api=>api('camera-unbind',{...binding}));
      return;
     }
     const candidate=(ui.view?.candidates(current.camera_candidates||[])||current.camera_candidates||[]).find(c=>candidateKey(c)===select.value);
     if(!candidate){updateAlignment(ui,true);return;}
     if(current.camera_bindings?.some(b=>b.seat!==player.seat&&candidateKey(b)===candidateKey(candidate))){updateAlignment(ui,true);return;}
     void perform(api=>api('camera-bind',{seat:player.seat,...candidate}));
    };
   }
   const keys=new Set(candidates.map(candidateKey));
   for(const [key,option]of row.options){if(!keys.has(key)){option.remove();row.options.delete(key);}}
   for(const candidate of candidates){
    const key=candidateKey(candidate);
    if(!row.options.has(key)){const option=node('option');option.value=key;row.options.set(key,option);row.select.insertBefore(option,row.clear||null);}
    const option=row.options.get(key),owner=current.camera_bindings?.find(b=>b.seat!==player.seat&&candidateKey(b)===key);
    if(option.disabled!==Boolean(owner))option.disabled=Boolean(owner);
    const text='镜头中的 #'+candidate.track+(owner?' · 已对应 '+owner.seat+' 号':'');if(option.textContent!==text)option.textContent=text;
   }
   const binding=current.camera_bindings?.find(b=>b.seat===player.seat),key=binding?candidateKey(binding):'';
   if(binding&&!row.clear){row.clear=node('option','取消当前对应');row.clear.value='__clear__';row.select.append(row.clear);}
   if(!binding&&row.clear){row.clear.remove();row.clear=null;}
   const placeholder=binding&&!keys.has(key)?available?'原位置暂不可见':'等待画面恢复':'选择镜头中的编号';if(row.empty.textContent!==placeholder)row.empty.textContent=placeholder;
   const value=keys.has(key)?key:'',draft=row.select.value,valid=draft===''||(draft==='__clear__'?Boolean(binding):row.options.has(draft)&&!row.options.get(draft).disabled);
   if(force||row.readbackValue!==value||!valid){if(row.select.value!==value)row.select.value=value;row.readbackValue=value;}
  }
  ui.hint.textContent=!players.length?'大家入座后，在这里对应镜头中的编号。':!candidates.length?available?'还没有可对应的编号。':'等画面恢复后，可继续对应。':'投票仍在各自手机上完成。';
 }
 function cameraAlignment(){
  const section=node('section',null,'ww-camera-alignment'),heading=node('div',null,'ww-camera-align-heading'),title=node('h3','对齐镜头中的座位'),hint=node('p',null,'ww-field-help'),grid=node('div',null,'ww-camera-seat-grid');
  heading.append(title);section.append(heading,hint);
  const ui={section,hint,grid,session:current.session,rows:new Map()};
  ui.view=mountSeatCamera(section,{heading,session:ui.session,api:(body,signal)=>api('camera-view',body,signal),isCurrent:()=>alive&&current?.session===ui.session&&current?.game?.phase==='lobby'&&lobbyUI?.alignment===ui,onChange:()=>{updateAlignment(ui);updateControls();},go});
  section.append(grid);updateAlignment(ui);return ui;
 }
 function updateLobby(){
  const g=current.game;
  for(const item of lobbyUI.scene.querySelectorAll('[data-seat]')){
   const seat=Number(item.dataset.seat),player=g.players.find(p=>p.seat===seat);
   item.dataset.ready=String(Boolean(player?.ready));item.dataset.alive=String(player?.alive!==false);
   item.querySelector('strong').textContent=player?.name||'等待入座';
   item.querySelector('.ww-seat-state').textContent=player?(player.ready?'已准备':'未准备'):'';
   const remove=item.querySelector('.ww-remove');
   if(player&&!remove)item.append(button('移除',()=>perform(api=>api('control',{action:'remove',seat})),'ww-remove'));
   if(!player&&remove)remove.remove();
  }
  const ready=g.players.filter(p=>p.ready).length;
  lobbyUI.ready.textContent=ready===g.player_count?'大家都准备好了':ready+' / '+g.player_count+' 人已准备';
  if(lobbyUI.alignment)updateAlignment(lobbyUI.alignment);
 }
 function speakerControls(game, seats){
  const form=node('form',null,'ww-speaker-controls');
  const label=node('label','点名发言','ww-field');
  const select=node('select');
  select.dataset.speakerPicker='true';
  const placeholder=node('option',game.speaker_seat?game.speaker_seat+' 号发言中':'选择座位');
  placeholder.value='';
  select.append(placeholder);
  for(const player of game.players){
   if(!seats.includes(player.seat)||player.seat===game.speaker_seat)continue;
   const text=player.seat+' 号 · '+player.name;
   const option=node('option',text);
   option.value=String(player.seat);
   option.dataset.label=text;
   select.append(option);
  }
  const submit=node('button','请发言','primary');
  submit.type='submit';
  submit.dataset.speakerCommand='true';
  label.append(select);form.append(label,submit);
  select.onchange=updateSpeakerControls;
  form.onsubmit=event=>{
   event.preventDefault();
   if(!submit.disabled)void perform(api=>api('control',{action:'speaker',seat:select.value?Number(select.value):null}));
  };
  return form;
 }
 function updateSpeakerControls(){
  const select=content.querySelector('[data-speaker-picker]'),submit=content.querySelector('[data-speaker-command]');
  if(!select||!submit)return;
  select.disabled=busy||syncFailed;
  const text=select.value||!current.game.speaker_seat?'请发言':'结束发言';
  if(submit.textContent!==text)submit.textContent=text;
  submit.disabled=select.disabled||(!select.value&&!current.game.speaker_seat);
 }
 function updateSeatSignals(){
  const hints=new Map((current?.camera_hints||[]).map(hint=>[hint.seat,hint]));
  for(const seat of content.querySelectorAll('[data-seat]')){
   const signal=seat.querySelector('.ww-seat-signal');
   if(!signal)continue;
   const hint=hints.get(Number(seat.dataset.seat));
   const labels=[];
   if(hint?.hand_raised&&seat.dataset.speaking!=='true')labels.push('举手');
   if(hint?.pointing_at)labels.push('指向 '+hint.pointing_at+' 号');
   const text=labels.join(' · ');
   if(signal.textContent!==text)signal.textContent=text;
   signal.hidden=!text;
  }
  for(const option of content.querySelectorAll('[data-speaker-picker] option[data-label]')){
   const text=option.dataset.label+(hints.get(Number(option.value))?.hand_raised?' · 举手':'');
   if(option.textContent!==text)option.textContent=text;
  }
 }
 function live(){const g=current.game;const title=node('div',null,'ww-live-title');title.append(node('h2','狼人杀'),node('span',g.player_count+' 人 · Friday 主持'));content.append(title);
  const layout=node('div',null,'ww-live-layout');layout.dataset.playing=String(g.phase!=='lobby');layout.dataset.aligning=String(current.camera_enabled&&g.phase==='lobby');const scene=board(g);layout.append(scene);if(g.phase==='lobby')lobbyUI={session:current.session,count:g.player_count,cameraEnabled:Boolean(current.camera_enabled),scene,ready:null,alignment:null};const aside=node('aside',null,'ww-room-tools');
  if(g.phase==='lobby'){const join=node('div',null,'ww-join');join.append(node('h3','用手机入座'));if(current.phone_url){const qr=node('img');qr.src='/api/games/werewolf/join-qr?session='+encodeURIComponent(current.session);qr.alt='手机扫码加入本局';qr.width=180;qr.height=180;qr.onerror=()=>{qr.hidden=true;};join.append(qr);const link=node('a','打开手机入口');link.href=current.phone_url;link.target='_blank';link.rel='noopener noreferrer';join.append(link,button('复制加入地址',async()=>{try{await navigator.clipboard.writeText(current.phone_url);notice.textContent='加入地址已复制。';}catch{notice.textContent=current.phone_url;}},'ww-text-button'));}join.append(node('p','房间号'),node('strong',current.join_code,'ww-room-code'));aside.append(join);}
  const controls=node('div',null,'ww-host-controls');
  if(g.phase==='lobby'){
   const ready=g.players.filter(p=>p.ready).length,readyLine=node('p',ready===g.player_count?'大家都准备好了':ready+' / '+g.player_count+' 人已准备');
   lobbyUI.ready=readyLine;
   const start=button('发放身份',()=>perform(api=>api('control',{action:'start'})),'primary');
   start.dataset.start='true';controls.append(readyLine,start);
  }else if(g.phase==='identity'){
   const confirmed=g.identities_confirmed??0,progress=node('p',confirmed+' / '+g.player_count+' 人已确认','ww-identity-count');
   const start=button('开始首夜',()=>perform(api=>api('control',{action:'advance'})),'primary');
   start.dataset.startNight='true';
   const reseat=button(confirmation==='reseat'?'确认返回入座':'返回入座',()=>confirmControl('reseat'),'ww-text-button');
   reseat.dataset.reseat='true';controls.append(progress,start,reseat);
  }else if(g.phase!=='finished'){
   if(current.speaker_seats?.length)controls.append(speakerControls(g,current.speaker_seats));
   controls.append(button(g.paused?'继续对局':'暂停对局',()=>perform(api=>api('control',{action:g.paused?'resume':'pause'}))));
   if(!g.paused)controls.append(button(confirmation==='advance'?'确认进入下一环节':'提前结束本环节',()=>confirmControl('advance'),'ww-text-button'));
  }
  if(g.phase==='finished')controls.append(button('再开一局',()=>{preferredCount=g.player_count;void perform(api=>api('control',{action:'close'}));},'primary'));const close=button(confirmation==='close'?'确认关闭房间':'关闭房间',()=>confirmControl('close'),'ww-text-button');controls.append(close);aside.append(controls);
  if(current.display_url){const tv=node('div',null,'ww-tv-link');tv.append(node('h3','大屏同玩'),node('p','电视仅显示座位和公开进展。'));const link=node('a','打开公共画面');link.href=current.display_url;link.target='_blank';link.rel='noopener noreferrer';tv.append(link,button('复制大屏地址',async()=>{try{await navigator.clipboard.writeText(current.display_url);notice.textContent='大屏地址已复制。';}catch{notice.textContent=current.display_url;}},'ww-text-button'));aside.append(tv);}const effects=node('div',null,'ww-effects'),voice=node('p',null,'ww-field-help');voice.dataset.voiceState='true';effects.append(voice);const recovery=node('div',null,'ww-recovery');recovery.dataset.lightRecovery='true';recovery.append(node('p','灯光有待恢复。'));const restore=button('恢复灯光',()=>perform(async api=>{await api('restore-lights',{});return api('host');}));restore.dataset.restoreLights='true';recovery.append(restore);effects.append(recovery);aside.append(effects);layout.append(aside);content.append(layout);
  if(g.vote_result){const votes=node('section',null,'ww-vote-results');votes.append(node('h3','上轮票型'));const line=node('div');for(const vote of g.vote_result.votes)line.append(node('span',vote.seat+' 号 → '+(vote.target?vote.target+' 号':'弃票')));votes.append(line);content.append(votes);}const roleLine=node('p',g.roles.map(r=>r.name+' × '+r.count).join('　'),'ww-role-line');content.append(roleLine,rules(g));
  if(current.camera_enabled&&g.phase==='lobby'){lobbyUI.alignment=cameraAlignment();layout.append(lobbyUI.alignment.section);}
 }
 function updateEffects(){
  const effects=current?.effects||{},voice=content.querySelector('[data-voice-state]');
  const text=effects.voice==='quiet'?'语音主持已静音':['unavailable','unconfirmed'].includes(effects.voice)?'播报暂不可用，屏幕主持继续':effects.narration?'Friday 语音主持已开启':'使用屏幕主持';
  if(voice&&voice.textContent!==text)voice.textContent=text;
  const recovery=content.querySelector('[data-light-recovery]'),restore=content.querySelector('[data-restore-lights]');
  if(recovery)recovery.hidden=!effects.lights?.recovery_pending;
  if(restore)restore.hidden=current?.game?.phase!=='finished';
 }
 function renderKey(){
  if(!current?.active)return 'setup';
  return [current.session,current.game.revision,JSON.stringify(current.game.players),JSON.stringify(current.camera_candidates),
   JSON.stringify(current.camera_bindings),current.camera_available].join('|');
 }
 function render(){
  if(!alive)return;
  revision=renderKey();
  if(current?.active&&current.game.phase==='lobby'&&lobbyUI?.session===current.session&&
     lobbyUI.count===current.game.player_count&&lobbyUI.cameraEnabled===Boolean(current.camera_enabled)){
   updateLobby();
  }else{
   lobbyUI?.alignment?.view.dispose();lobbyUI=null;
   const focus=document.activeElement,focused=wrap.contains(focus)?focus?.textContent:null;
   content.replaceChildren();
   if(current?.active)live();else setup();
   if(focused){
    const candidate=[...content.querySelectorAll('button')].find(b=>b.textContent===focused);
    candidate?.focus({preventScroll:true});
   }
  }
  notice.textContent=lastNotice||notice.textContent;updateControls();updateEffects();updateSeatSignals();
 }
 function updateIdentityProgress(){
  const g=current?.game;
  if(g?.phase!=='identity')return;
  const progress=content.querySelector('.ww-identity-count'),start=content.querySelector('[data-start-night]');
  const title=content.querySelector('[data-identity-title]'),prompt=content.querySelector('.ww-host-line');
  const confirmed=g.identities_confirmed??0,text=confirmed+' / '+g.player_count+' 人已确认';
  const ready=confirmed===g.player_count;
  if(progress&&progress.textContent!==text)progress.textContent=text;
  if(start)start.disabled=busy||!ready;
  const phaseTitle=ready?'准备入夜':g.phase_name,publicPrompt=ready?'请把手机放低，等候首夜。':g.host_script?.text||g.announcements?.at(-1)?.text||'';
  if(title&&title.textContent!==phaseTitle)title.textContent=phaseTitle;
  if(prompt&&prompt.textContent!==publicPrompt)prompt.textContent=publicPrompt;
 }
 function updateControls(){for(const b of content.querySelectorAll('button')){if(b.dataset.start==='true'){b.disabled=busy||current.game.players.filter(p=>p.ready).length!==current.game.player_count;}else if(!b.dataset.startNight&&!b.dataset.speakerCommand)b.disabled=busy||b.dataset.unavailable==='true';}for(const select of content.querySelectorAll('select'))if(!select.dataset.speakerPicker)select.disabled=busy;updateIdentityProgress();updateSpeakerControls();}
 async function refresh(first=false){
  if(!alive||busy||readRequest)return;
  const request={generation:++generation,controller:new AbortController()};readRequest=request;
  const owns=()=>alive&&!busy&&readRequest===request&&generation===request.generation;
  try{
   const needsContext=first||context===null,previousSession=current?.session,previousRevision=current?.game?.revision;
   const data=await api(needsContext?'context':'host',undefined,request.controller.signal);if(!owns())return;
   if(needsContext){context=data;current=data.current;}else current=data;
   if(syncFailed&&notice.textContent===syncNotice)notice.textContent=lastNotice;
   syncFailed=false;
   const next=renderKey();
   if(needsContext||revision!==next){if(needsContext||previousSession!==current?.session||previousRevision!==current?.game?.revision)confirmation=null;render();}
   else if(current.active){const timer=content.querySelector('[data-timer]');if(timer)timer.textContent=current.game.paused?'已暂停':current.game.seconds_left==null?'':Math.max(0,current.game.seconds_left)+' 秒';if(current.game.phase==='identity')updateIdentityProgress();updateEffects();updateSeatSignals();updateSpeakerControls();}
  }catch{if(owns()){syncFailed=true;notice.textContent=syncNotice;updateSpeakerControls();}}
  finally{if(readRequest===request)readRequest=null;}
 }
 content.append(node('p','正在准备房间…','ww-field-help'));void refresh(true);const timer=setInterval(()=>{if(!busy)void refresh();},1000);
 return{dispose(){alive=false;lobbyUI?.alignment?.view.dispose();generation++;readRequest?.controller.abort();commandRequest?.controller.abort();readRequest=commandRequest=lobbyUI=null;abort.abort();clearInterval(timer);wrap.remove();}};
}
