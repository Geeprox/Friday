import {mountSeatCamera} from './werewolf-camera-view.js';
import {makeWerewolfRecap} from './werewolf-recap.js';

const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
const button=(text,fn,cls)=>{const n=node('button',text,cls);n.type='button';n.onclick=fn;return n;};
const errors={players_not_ready:'等大家准备好后再发身份。',identities_pending:'等大家确认身份后，再开始首夜。',phone_address_unavailable:'局域网连接发生变化，请重新选择。',phone_network_changed:'连接状态已更新，请核对当前房间。',party_link_unavailable:'手机与电视已断开，请先恢复连接。',players_reconnecting:'还有玩家未返回。请用「找回座位」让他们重新加入。',game_already_exists:'已有一个房间，请先结束当前房间。',scene_recovery_required:'请先恢复上局灯光。',scene_light_brightness_unavailable:'这盏灯暂时无法读回亮度，请取消选择后重试。',scene_light_binding_changed:'氛围灯连接已变化，请重新创建房间并选择灯具。',scene_light_unavailable:'暂时读不到氛围灯，请检查连接后重试。',rematch_unavailable:'本局结束后可以再开一局。',phase_changed:'对局已经更新，请重新操作。',speaker_unavailable:'当前无法安排发言，请查看对局进度。',camera_binding_changed:'座位对应已经更新，请重新选择。',room_changed:'房间已经更新，请在当前对局重试。',event_conflict:'这次操作已更新，请重新选择。',camera_track_unavailable:'画面中的位置已经变化，请重新选择。',game_unavailable:'暂时无法完成，请重试。'};

export function mountWerewolf(root,{go=()=>{}}={}){
 let alive=true,busy=false,context=null,current=null,revision='',lastNotice='',confirmation=null;
 let generation=0,readRequest=null,commandRequest=null,syncFailed=false,lobbyUI=null,roundCamera=null,seatRecovery=null,displayPair=null;
 const syncNotice='对局服务暂时无法连接，正在重试。';
 const networkInterrupted=()=>current?.network?.state==='interrupted';
 const waitingPhones=()=>current?.network?.waiting_seats?.length>0;
 const returningPhones=()=>waitingPhones()||current?.game?.paused&&Array.isArray(current.network?.waiting_seats);
 const abort=new AbortController(),wrap=node('section',null,'werewolf-host'),notice=node('p',null,'ww-notice'),content=node('div');notice.setAttribute('role','status');wrap.append(notice,content);root.append(wrap);
 async function api(path,body,signal){const signals=[abort.signal,AbortSignal.timeout(10000)];if(signal)signals.push(signal);const r=await fetch('/api/games/werewolf/'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.any(signals)});const data=await r.json();if(!r.ok)throw Error(data.detail||'game_unavailable');return data;}
 async function perform(fn){
  if(busy||!alive)return;
  const request={generation:++generation,controller:new AbortController(),session:current?.session,revision:current?.game?.revision,bindingRevision:current?.camera_binding_revision,event:null,focused:document.activeElement};
  readRequest?.controller.abort();readRequest=null;commandRequest=request;
  const owns=()=>alive&&commandRequest===request&&generation===request.generation;
  const commandApi=(path,body)=>{
   const mapping=path==='camera-bind'||path==='camera-unbind';
   if(path==='control'||path==='reconnect'||mapping){
    request.event??=crypto.randomUUID();
    body={...body,[mapping?'game_session':'session']:request.session,revision:request.revision,event_id:request.event};
    if(mapping&&Number.isSafeInteger(request.bindingRevision))body.binding_revision=request.bindingRevision;
    if(path==='reconnect')request.reconnect=true;
   }
   return api(path,body,request.controller.signal);
  };
  busy=true;syncFailed=false;lastNotice='';notice.textContent='';content.setAttribute('aria-busy','true');updateControls();
  try{
   const data=await fn(commandApi);if(!owns())return;
   if(request.reconnect)confirmation=null;
   if(request.reconnect&&!Array.isArray(data.network?.waiting_seats)){
    lastNotice='连接已恢复，可以扫码入座。';
   }
   current=data;render();
   // An acknowledged close stays visible even if refreshing setup choices fails.
   if(!data.active){const nextContext=await commandApi('context');if(!owns())return;context=nextContext;current=nextContext.current;render();}
  }catch(e){if(owns()){lastNotice=errors[e.message]||'暂时无法完成，请稍后重试。';notice.textContent=lastNotice;}}
  finally{
   if(commandRequest===request){
    commandRequest=null;busy=false;
    if(alive){
     content.removeAttribute('aria-busy');
     if(lobbyUI?.alignment)updateAlignment(lobbyUI.alignment,true);
     if(roundCamera?.alignment)updateAlignment(roundCamera.alignment,true);
     updateControls();
     restoreControlFocus(request);
    }
   }
  }
 }
 function restoreControlFocus(request){
  if(current?.session!==request.session||document.activeElement!==document.body)return;
  const previous=request.focused;
  if(request.reconnect&&!networkInterrupted()){
   const recover=content.querySelector('[data-seat-recovery]');
   (recover&&!recover.hidden&&!recover.disabled?recover:content.querySelector('.ww-phase-heading h3'))?.focus({preventScroll:true});
  }else if(previous?.dataset.returnResume||previous?.dataset.returnOverride){
   content.querySelector('.ww-phase-heading h3')?.focus({preventScroll:true});
  }else if(previous?.tagName==='SELECT'&&content.contains(previous)){
   previous.focus({preventScroll:true});
  }else if(previous?.dataset.speakerCommand){
   const action=content.querySelector('[data-speaker-command]');
   const next=action&&!action.disabled?action:content.querySelector('[data-speaker-picker]');
   next?.focus({preventScroll:true});
  }else if(previous?.dataset.turnCommand){
   const next=content.querySelector('[data-turn-command]:not(:disabled)')||content.querySelector('.ww-phase-heading h3')||content.querySelector('.ww-recap-title');
   next?.focus({preventScroll:true});
  }else if(previous?.dataset.rematch){
   content.querySelector('.ww-phase-heading h3')?.focus({preventScroll:true});
  }else if(previous?.dataset.cameraApply){
   content.querySelector('[data-camera-person]')?.focus({preventScroll:true});
  }
 }
 function rules(game){const details=node('details',null,'ww-rules'),summary=node('summary','本局规则');details.append(summary);const list=node('ul');for(const text of game.rules||[])list.append(node('li',text));details.append(list);return details;}
 function check(label,value=false){const row=node('label',null,'ww-check'),input=node('input');input.type='checkbox';input.checked=value;row.append(input,node('span',label));return{row,input};}
 function recoveryQr(matrix){
  if(!Array.isArray(matrix)||!matrix.length||matrix.length>185||
     matrix.some(row=>typeof row!=='string'||row.length!==matrix.length||!/^[01]+$/.test(row)))return null;
  const canvas=node('canvas',null,'ww-seat-recovery-qr'),scale=4;
  canvas.width=canvas.height=matrix.length*scale;
  canvas.setAttribute('role','img');
  canvas.setAttribute('aria-label','找回座位二维码');
  const pen=canvas.getContext('2d');
  if(!pen)return null;
  pen.fillStyle='#fff';
  pen.fillRect(0,0,canvas.width,canvas.height);
  pen.fillStyle='#191919';
  for(const [y,row] of matrix.entries())for(let x=0;x<row.length;x++){
   if(row[x]==='1')pen.fillRect(x*scale,y*scale,scale,scale);
  }
  return canvas;
 }
 function openSeatRecovery(){
  if(seatRecovery||syncFailed||!current?.phone_url||!current.game.players.length)return;
  const dialog=node('dialog',null,'ww-seat-recovery'),heading=node('h3','找回座位');
  heading.id='ww-seat-recovery-title';dialog.setAttribute('aria-labelledby',heading.id);
  const field=node('label','玩家','ww-field'),picker=node('select');
  picker.autofocus=true;
  for(const player of current.game.players){
   const option=node('option',player.seat+' 号 · '+player.name);
   option.value=String(player.seat);picker.append(option);
  }
  if(waitingPhones())picker.value=String(current.network.waiting_seats[0]);
  field.append(picker);
  const result=node('div',null,'ww-seat-recovery-result'),status=node('p',null,'ww-field-help');
  status.setAttribute('role','status');
  const actions=node('div',null,'ww-seat-recovery-actions');
  const done=button('关闭',()=>dialog.close());
  const show=button('生成二维码',issue,'primary');
  actions.append(done,show);dialog.append(heading,field,result,status,actions);wrap.append(dialog);
  const session=current.session,lobbyRevision=current.game.lobby_revision;
  const recovery={session,lobbyRevision,dialog,phoneUrl:current.phone_url};
  seatRecovery=recovery;
  let request=null,expiry=null;
  function clear(){
   request?.abort();request=null;clearTimeout(expiry);expiry=null;
   result.replaceChildren();status.textContent='';
   show.textContent='生成二维码';show.disabled=false;
  }
  picker.addEventListener('change',clear);
  dialog.addEventListener('close',()=>{
   clear();dialog.remove();
   if(seatRecovery===recovery)seatRecovery=null;
   if(alive&&current?.session===session){
    const entry=content.querySelector('[data-seat-recovery]'),resume=content.querySelector('[data-return-resume]');
    const next=entry&&!entry.hidden&&!entry.disabled?entry:
     resume&&!resume.hidden&&!resume.disabled?resume:content.querySelector('[data-return-title]');
    next?.focus({preventScroll:true});
   }
  },{once:true});
  async function issue(){
   if(request||current?.session!==session||current.game.lobby_revision!==lobbyRevision)return;
   if(syncFailed){status.textContent='连接恢复后再试。';return;}
   clear();
   const pending=new AbortController(),seat=Number(picker.value);
   request=pending;show.disabled=true;status.textContent='正在生成…';
   try{
    const data=await api('seat-recovery',{
     seat,session,revision:current.game.revision,event_id:crypto.randomUUID(),
    },pending.signal);
    if(request!==pending||!dialog.open||seatRecovery!==recovery)return;
    const url=new URL(data.url);
    if(data.seat!==seat||url.origin!==new URL(current.phone_url).origin||
       url.pathname!=='/phone/recover'||!Number.isSafeInteger(data.seconds_left)||data.seconds_left<=0)throw Error('invalid_recovery');
    const qr=recoveryQr(data.qr),copy=button('复制链接',async()=>{
     try{
      await navigator.clipboard.writeText(url.href);
      if(result.contains(copy))status.textContent='链接已复制。';
     }catch{
      if(!result.contains(copy))return;
      const input=node('input');input.readOnly=true;input.value=url.href;
      input.setAttribute('aria-label','座位恢复链接');
      copy.replaceWith(input);input.focus();input.select();
     }
    },'ww-text-button');
    if(qr)result.append(qr);
    result.append(copy);
    status.textContent=qr?'请让 '+seat+' 号用手机扫码，2 分钟内有效。':'请把恢复链接发给 '+seat+' 号，2 分钟内有效。';
    show.textContent='重新生成';
    expiry=setTimeout(()=>{clear();status.textContent='恢复链接已过期，请重新生成。';},data.seconds_left*1000);
   }catch(error){
    if(request===pending&&dialog.open)status.textContent=errors[error.message]||'暂时无法生成二维码，请重试。';
   }finally{
    if(request===pending){request=null;show.disabled=false;}
   }
  }
  dialog.showModal();
 }
 function setup(){
  const intro=node('div',null,'ww-intro');intro.append(node('h2','狼人杀'),node('p','每人用手机入座。身份和夜间行动只在自己的手机上显示。'));content.append(intro);
  const launch=node('div',null,'ww-launch-layout'),cover=node('img',null,'ww-launch-cover');cover.src='/static/games-art/werewolf.png';cover.alt='狼人杀封面插画';cover.width=1672;cover.height=941;launch.append(cover);const form=node('form',null,'ww-setup'),fields=node('div',null,'ww-setup-fields');
  const orderedAvailable=Number.isSafeInteger(context.speaker_seconds)&&context.speaker_seconds>0;
  form.onsubmit=e=>{
   e.preventDefault();
   void perform(api=>api('create',{players:Number(count.value),address:network.value||null,
    ...(orderedAvailable?{discussion_mode:discussion.value}:{}),narration:speech.input.checked,camera:camera.input.checked,
    lights:lights.filter(x=>x.input.checked).map(x=>x.id)}));
  };
  const countLabel=node('label','本局人数','ww-field'),count=node('select');for(let i=6;i<=12;i++){const o=node('option',i+' 人');o.value=String(i);count.append(o);}count.value='9';countLabel.append(count);
  const networkLabel=node('label','手机加入','ww-field'),network=node('select');for(const address of context.addresses||[]){const o=node('option',(context.addresses||[]).length===1?'当前网络':'局域网 · '+address);o.value=address;network.append(o);}if(!network.children.length){const o=node('option','暂未连接局域网');o.value='';network.append(o);}networkLabel.append(network);
  const discussionLabel=node('label','发言方式','ww-field ww-discussion-mode'),discussion=node('select');
  for(const[value,text]of[['turns','轮流发言 · 每人 '+context.speaker_seconds+' 秒'],['free','自由发言']]){
   const option=node('option',text);option.value=value;discussion.append(option);
  }
  discussion.value='turns';discussionLabel.append(discussion);
  fields.append(countLabel,networkLabel);if(orderedAvailable)fields.append(discussionLabel);
  form.append(fields);
  const speech=check('Friday 语音主持',Boolean(context.speech?.configured&&!context.speech?.quiet));speech.input.disabled=!context.speech?.configured||context.speech?.quiet;const camera=check('使用已运行的摄像头辅助举手与指认');form.append(speech.row);if(speech.input.disabled){const hint=node('p',null,'ww-field-help');hint.append(node('span',context.speech?.quiet?'播报已静音。':'还未设置播报设备。'),button('设置播报',()=>go('Settings'),'ww-text-button'));form.append(hint);}form.append(camera.row);
  const lights=[];if(context.lights?.length){const group=node('fieldset',null,'ww-light-choice');group.append(node('legend','氛围灯'));for(const item of context.lights){const c=check((item.room?item.room+' · ':'')+item.name);lights.push({...c,id:item.id});group.append(c.row);}group.append(node('p','夜间调暗，天亮恢复明亮；本局结束后恢复原状。','ww-field-help'));form.append(group);}
  if(context.recovery?.recovery_pending){const recovery=node('div',null,'ww-recovery');recovery.append(node('p','上局灯光还有待恢复。'),button('恢复灯光',()=>perform(async api=>{await api('restore-lights',{});return api('host');})));form.append(recovery);}
  const create=node('button','创建房间','primary');create.type='submit';create.dataset.unavailable=String(!(context.addresses||[]).length);create.disabled=create.dataset.unavailable==='true';form.append(create);if(create.disabled)form.append(node('p','将手机与 Friday 连到同一局域网后，即可创建房间。','ww-field-help'));launch.append(form);content.append(launch);
  const how=node('div',null,'ww-how');for(const [title,text] of [['手机入座','每人用自己的手机选择座位。'],['私下行动','身份与夜间选择，只在自己的手机上显示。'],['一起推理','Friday 计时主持，电视展示公开进展。']]){const item=node('div');item.append(node('h3',title),node('p',text));how.append(item);}content.append(how,rules({rules:context.rules}));
 }
 function openDisplayPair(){
  if(displayPair||syncFailed||!current?.display_entry)return;
  const session=current.session,dialog=node('dialog',null,'ww-display-pair'),title=node('h3','连接电视');
  title.id='ww-display-pair-title';dialog.setAttribute('aria-labelledby',title.id);
  const form=node('form'),addressLabel=node('label','电视浏览器地址','ww-field'),address=node('input');
  address.readOnly=true;address.value=current.display_entry;address.addEventListener('focus',()=>address.select());
  addressLabel.append(address);
  const codeLabel=node('label','电视上的六位码','ww-field'),input=node('input');
  input.type='text';input.inputMode='numeric';input.autocomplete='off';input.pattern='[0-9]{6}';
  input.maxLength=6;input.required=true;input.setAttribute('autofocus','');codeLabel.append(input);
  const status=node('p',null,'ww-field-help');
  status.setAttribute('role','status');
  const actions=node('div',null,'ww-seat-recovery-actions');
  const dismiss=button('取消',()=>dialog.close()),submit=node('button','确认连接','primary');
  submit.type='submit';actions.append(dismiss,submit);
  const pair={session,dialog,input,status,dismiss,submit,code:null,request:null,connected:false,entry:current.display_entry};
  displayPair=pair;
  form.append(addressLabel,codeLabel,status,actions);dialog.append(title,form);wrap.append(dialog);
  dialog.addEventListener('close',()=>{
   pair.request?.abort();dialog.remove();
   if(displayPair===pair)displayPair=null;
   if(alive&&current?.session===session)content.querySelector('[data-display-pair]')?.focus({preventScroll:true});
  },{once:true});
  input.addEventListener('input',()=>{
   pair.code=null;
   status.textContent='';
  });
  form.onsubmit=async event=>{
   event.preventDefault();
   if(pair.request||pair.connected||syncFailed||current?.session!==session||!/^[0-9]{6}$/.test(input.value))return;
   const code=input.value,request=new AbortController();
   pair.code=code;pair.request=request;submit.disabled=true;input.readOnly=true;
   status.textContent='正在连接电视…';
   const owns=()=>displayPair===pair&&dialog.open&&current?.session===session&&pair.request===request;
   try{
    const result=await api('display-pairing',{session,code},request.signal);
    if(!owns()||pair.connected)return;
    if(result.session!==session)throw Error('room_changed');
    status.textContent='请看电视，公屏即将打开。';
    void refresh();
   }catch(error){
    if(!owns()||pair.connected)return;
    if(error.message==='display_pairing_expired')pair.code=null;
    status.textContent=error.message==='display_pairing_expired'?'请核对电视上的连接码。':
     errors[error.message]||'暂时无法确认，请重试。';
   }finally{
    if(owns()){
     pair.request=null;input.readOnly=pair.connected;
     submit.disabled=syncFailed||pair.connected;
    }
   }
  };
  dialog.showModal();
 }
 function updateDisplayConnection(){
  const codes=current?.connected_display_codes??[];
  const indicator=content.querySelector('[data-display-connected]');
  if(indicator)indicator.hidden=syncFailed||!codes.length;
  if(!displayPair)return;
  const pair=displayPair;
  if(current?.session!==pair.session||current.display_entry!==pair.entry){pair.dialog.close();return;}
  if(pair.connected)return;
  if(!syncFailed&&pair.code&&codes.includes(pair.code)){
   pair.connected=true;pair.input.readOnly=true;pair.submit.hidden=true;
   pair.status.textContent='电视已连接。';pair.dismiss.textContent='完成';pair.dismiss.focus();
  }else pair.submit.disabled=syncFailed||Boolean(pair.request);
 }
 function televisionControls(){
  if(!current.display_url)return null;
  const panel=node('div',null,'ww-tv-link');
  panel.append(node('h3','大屏同玩'));
  if(current.display_entry){
   const connect=button('连接电视',openDisplayPair),indicator=node('p','电视已连接','ww-display-connected');
   connect.dataset.displayPair='true';indicator.dataset.displayConnected='true';indicator.hidden=true;
   panel.append(connect,indicator);
  }
  const link=node('a','打开公共画面');
  link.href=current.display_url;link.target='_blank';link.rel='noopener noreferrer';panel.append(link);
  return panel;
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
  const result=game.phase==='vote_reveal'?game.vote_result:null;
  scene.dataset.count=String(game.player_count);scene.dataset.night=String(['guard','wolves','seer','witch'].includes(game.phase));
  const title=node('div',null,'ww-phase-heading');
  const tied=result?.ballot==='vote'&&result.tied.length;
  const outcome=result?.eliminated?result.eliminated+' 号被放逐':tied?'平票':'无人被放逐';
  const phaseTitle=node('h3',result?outcome:game.phase_name);
  phaseTitle.setAttribute('tabindex','-1');
  if(identity)phaseTitle.dataset.identityTitle='true';
  if(!result)title.append(node('span',null,'ww-moon'));
  title.append(phaseTitle);
  const turn=game.paused?null:game.speaking_turn;
  if(game.round||lobby)title.append(node('p',game.round?'第 '+game.round+' 轮'+(turn?' · 发言 '+turn.position+' / '+turn.total:''):'等大家准备好'));
  const time=node('strong',game.paused?'已暂停':game.seconds_left==null?'':Math.max(0,game.seconds_left)+' 秒','ww-timer');
  time.hidden=!game.paused&&game.seconds_left==null;time.dataset.timer='true';title.append(time);scene.append(title);
  if(result){
   scene.dataset.voteReveal='true';
   const names=new Map(game.players.map(player=>[player.seat,player.name]));
   const ledger=node('ul',null,'ww-ballot-ledger');
   ledger.setAttribute('aria-label','本次票型');
   for(const vote of result.votes){
    const item=node('li');
    item.append(node('span',vote.seat+' 号 · '+(names.get(vote.seat)||'')),node('strong',vote.target?'投给 '+vote.target+' 号':'弃票'));
    ledger.append(item);
   }
   scene.append(ledger);return scene;
  }
  const roster=node('ol',null,'ww-roster');
  const discussing=game.phase==='discussion'||game.phase==='runoff_discussion';
  for(let seat=1;seat<=game.player_count;seat++){
   const p=game.players.find(x=>x.seat===seat),item=node('li',null,'ww-seat');
   item.dataset.seat=String(seat);item.dataset.alive=String(p?.alive!==false);item.dataset.ready=String(lobby&&Boolean(p?.ready));
   const speaking=!game.paused&&game.speaker_seat===seat;
   item.dataset.speaking=String(speaking);
   const state=!p||identity?'':lobby?p.ready?'已准备':'未准备':!p.alive?'已出局':speaking?'发言中':turn?.next_seat===seat?'下一位':discussing?'':'在场';
   item.append(node('span',String(seat),'ww-seat-number'),node('strong',p?.name||'等待入座'),node('span',state,'ww-seat-state'));
   if(lobby&&p)item.append(button('移除',()=>perform(api=>api('control',{action:'remove',seat})),'ww-remove'));
   if(!lobby&&!identity){
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
 function visibleCandidates(ui){
  const fallback=current.camera_candidates||[];
  return ui.view?.candidates(fallback,current.camera_source)||fallback;
 }
 function applyAlignment(ui,seat,select){
  if(!ui.isCurrent()||busy||syncFailed||!content.contains(select))return;
  const binding=current.camera_bindings?.find(b=>b.seat===seat);
  if(select.value==='__clear__'){
   if(binding)void perform(api=>api('camera-unbind',{...binding}));
   return;
  }
  const candidate=visibleCandidates(ui).find(c=>candidateKey(c)===select.value);
  if(!candidate||current.camera_bindings?.some(b=>b.seat!==seat&&candidateKey(b)===select.value)){
   updateAlignment(ui,true);return;
  }
  void perform(api=>api('camera-bind',{seat,...candidate}));
 }
 function alignmentPlayers(ui,candidates){
  const players=current.game.players;
  if(!ui.seatPicker)return players;
  const eligible=players.filter(p=>current.camera_binding_seats?.includes(p.seat));
  const stamp=JSON.stringify(eligible.map(p=>[p.seat,p.name]));
  if(ui.seatStamp!==stamp){
   const selected=Number(ui.seatPicker.value);
   const visible=new Set(candidates.map(candidateKey));
   const bound=new Set((current.camera_bindings||[]).filter(b=>visible.has(candidateKey(b))).map(b=>b.seat));
   const initial=eligible.find(p=>p.seat===selected)||eligible.find(p=>!bound.has(p.seat))||eligible[0];
   ui.seatPicker.replaceChildren(...eligible.map(p=>{
    const option=node('option',p.seat+' 号 · '+p.name);option.value=String(p.seat);return option;
   }));
   if(initial)ui.seatPicker.value=String(initial.seat);
   ui.seatStamp=stamp;
  }
  return eligible.filter(p=>String(p.seat)===ui.seatPicker.value);
 }
 function updateAlignment(ui,force=false){
  const candidates=visibleCandidates(ui),players=alignmentPlayers(ui,candidates);
  const available=ui.view?.available(current.camera_available)??current.camera_available===true;
  for(const [seat,row]of ui.rows){if(!players.some(p=>p.seat===seat&&p.name===row.name)){row.label.remove();row.action?.remove();ui.rows.delete(seat);}}
  for(const player of players){
   let row=ui.rows.get(player.seat);
   if(!row){
    const label=node('label',null,'ww-camera-binding'),title=node('span',ui.seatPicker?'画面中的人物':player.seat+' 号 · '+player.name),select=node('select'),empty=node('option','选择镜头中的编号');empty.value='';empty.disabled=true;label.append(title,select);select.append(empty);
    select.dataset.cameraPicker='true';
    const next=[...ui.rows].filter(([seat])=>seat>player.seat).sort((a,b)=>a[0]-b[0])[0]?.[1].label;
    row={label,select,empty,name:player.name,options:new Map()};ui.rows.set(player.seat,row);ui.grid.insertBefore(label,next||null);
    if(ui.seatPicker){
     select.dataset.cameraPerson='true';
     row.action=button('确认对应',()=>applyAlignment(ui,player.seat,select),'primary');
     row.action.dataset.cameraApply='true';
     ui.grid.append(row.action);
     select.onchange=()=>{updateAlignment(ui);updateControls();};
    }else select.onchange=()=>applyAlignment(ui,player.seat,select);
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
   if(row.action){
    const applied=value&&row.select.value===value;
    const label=applied?'已对应':row.select.value==='__clear__'?'取消对应':'确认对应';
    if(row.action.textContent!==label)row.action.textContent=label;
    row.action.dataset.unavailable=String(syncFailed||!row.select.value||row.select.value===value);
   }
  }
  if(!ui.seatPicker)ui.hint.textContent=!players.length?'大家入座后，在这里对应镜头中的编号。':!candidates.length?available?'还没有可对应的编号。':'等画面恢复后，可继续对应。':'投票仍在各自手机上完成。';
 }
 function cameraAlignment(inRound=false){
  const section=node('section',null,'ww-camera-alignment'),heading=node('div',null,'ww-camera-align-heading'),title=node('h3','对齐镜头中的座位'),hint=node('p',null,'ww-field-help'),grid=node('div',null,'ww-camera-seat-grid');
  const session=current.session,phase=current.game.phase,round=current.game.round;
  const ui={section,hint,grid,rows:new Map(),isCurrent:()=>alive&&!syncFailed&&current?.session===session&&
   (inRound?roundCamera?.alignment===ui&&roundCameraKey()===roundCamera.key:current?.game?.phase==='lobby'&&lobbyUI?.alignment===ui)};
  section.append(heading);
  if(inRound){
   section.id='ww-round-camera-panel';
   section.className+=' ww-round-camera-panel';
   const seatLabel=node('label','座位','ww-camera-binding');
   ui.seatPicker=node('select');ui.seatPicker.dataset.cameraPicker='true';seatLabel.append(ui.seatPicker);grid.append(seatLabel);
   ui.seatPicker.onchange=()=>{updateAlignment(ui,true);updateControls();};
  }else{heading.append(title);section.append(hint);}
  ui.view=mountSeatCamera(section,{heading,session,...(inRound?{phase,round,hostOpens:true}:{}),
   api:(body,signal)=>api('camera-view',body,signal),isCurrent:ui.isCurrent,
   onChange:()=>{updateAlignment(ui);updateControls();},go});
  section.append(grid);updateAlignment(ui);return ui;
 }
 function roundCameraKey(){
  const game=current?.game;
  return current?.camera_binding_open===true&&['discussion','runoff_discussion'].includes(game?.phase)
   ?JSON.stringify([current.session,game.round,game.phase]):null;
 }
 function closeRoundPanel(ui){
  ui.alignment?.view.dispose();
  ui.alignment?.section.remove();
  ui.alignment=null;
  ui.layout.dataset.cameraOpen='false';
  ui.toggle.setAttribute('aria-expanded','false');
  ui.toggle.removeAttribute('aria-controls');
  ui.toggle.textContent='调整座位';
 }
 function mountRoundCamera(aside,layout){
  const key=roundCameraKey();
  if(!key)return;
  if(!roundCamera){
   const section=node('section',null,'ww-round-camera'),heading=node('div',null,'ww-round-camera-heading');
   const title=node('div'),status=node('p',null,'ww-field-help');
   status.setAttribute('role','status');
   title.append(node('h3','镜头座位'),status);
   const toggle=button('调整座位',()=>{
    if(roundCamera!==ui||roundCameraKey()!==key||syncFailed)return;
    if(ui.alignment){closeRoundPanel(ui);ui.toggle.focus();return;}
    ui.alignment=cameraAlignment(true);ui.section.append(ui.alignment.section);
    ui.layout.dataset.cameraOpen='true';ui.toggle.setAttribute('aria-expanded','true');
    ui.toggle.setAttribute('aria-controls',ui.alignment.section.id);ui.toggle.textContent='完成';
    ui.alignment.view.show();
    ui.alignment.seatPicker.focus();
   },'ww-text-button');
   toggle.setAttribute('aria-expanded','false');
   heading.append(title,toggle);section.append(heading);
   const ui={key,section,status,toggle,alignment:null,layout};
   roundCamera=ui;
  }
  roundCamera.layout=layout;
  layout.dataset.cameraOpen=String(Boolean(roundCamera.alignment));
  layout.dataset.roundCamera='true';
  aside.append(roundCamera.section);
 }
 function updateRoundCamera(){
  const ui=roundCamera;
  if(!ui)return;
  const eligible=current.camera_binding_seats||[];
  const candidates=new Set((current.camera_candidates||[]).map(candidateKey));
  const count=(current.camera_bindings||[]).filter(b=>eligible.includes(b.seat)&&candidates.has(candidateKey(b))).length;
  const text=syncFailed?'正在同步对局':current.camera_available?'已对应 '+count+' / '+eligible.length:'等待镜头恢复';
  if(ui.status.textContent!==text)ui.status.textContent=text;
  ui.toggle.dataset.unavailable=String(syncFailed&&!ui.alignment);
  if(ui.alignment){ui.alignment.view.recheck();updateAlignment(ui.alignment);}
 }
 function updateLobbyInvitation(){
  const ui=lobbyUI,game=current.game;
  const available=!networkInterrupted()&&game.players.length<game.player_count;
  ui.join.hidden=!available;
  if(!available){
   ui.qr?.remove();
   ui.qr=null;
   return;
  }
  if(!ui.phoneLink||(ui.qr&&(!ui.qr.hidden||ui.qrRevision===game.revision)))return;
  ui.qr?.remove();
  const qr=node('img');
  qr.alt='手机扫码加入本局';
  qr.width=180;
  qr.height=180;
  qr.onerror=()=>{qr.hidden=true;};
  ui.qr=qr;
  ui.qrRevision=game.revision;
  ui.join.insertBefore(qr,ui.phoneLink);
  qr.src='/api/games/werewolf/join-qr?session='+encodeURIComponent(current.session);
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
  updateLobbyInvitation();
  lobbyUI.ready.textContent=ready===g.player_count?'大家都准备好了':ready+' / '+g.player_count+' 人已准备';
  const closeLabel=confirmation==='close'?'确认关闭房间':'关闭房间';
  if(lobbyUI.close.textContent!==closeLabel)lobbyUI.close.textContent=closeLabel;
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
  const turn=content.querySelector('[data-turn-command]');
  if(turn)turn.disabled=busy||syncFailed||networkInterrupted()||waitingPhones()||!(current?.game?.seconds_left>0);
  const select=content.querySelector('[data-speaker-picker]'),submit=content.querySelector('[data-speaker-command]');
  if(!select||!submit)return;
  select.disabled=busy||syncFailed||networkInterrupted()||waitingPhones();
  const text=select.value||!current.game.speaker_seat?'请发言':'结束发言';
  if(submit.textContent!==text)submit.textContent=text;
  submit.disabled=select.disabled||(!select.value&&!current.game.speaker_seat);
 }
 function updateSeatSignals(){
  const hints=new Map((syncFailed?[]:current?.camera_hints||[]).map(hint=>[hint.seat,hint]));
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
 function live(){
  const g=current.game,finished=g.phase==='finished';
  const title=node('div',null,'ww-live-title');
  title.append(node('h2','狼人杀'));
  if(!finished)title.append(node('span',g.player_count+' 人 · Friday 主持'));
  content.append(title);
  if(networkInterrupted())content.append(networkRecovery());
  else if(returningPhones())content.append(phoneRecovery());
  const layout=node('div',null,'ww-live-layout');
  layout.dataset.playing=String(g.phase!=='lobby');
  layout.dataset.aligning=String(current.camera_enabled&&g.phase==='lobby');
  layout.dataset.finished=String(finished);
  const scene=finished?makeWerewolfRecap(g,{art:'/static/games-art/werewolf.png',heading:'h3'}):board(g);
  layout.append(scene);
  if(g.phase==='lobby')lobbyUI={session:current.session,count:g.player_count,cameraEnabled:Boolean(current.camera_enabled),networkRevision:current.network?.revision,scene,ready:null,alignment:null};
  const aside=node('aside',null,'ww-room-tools');
  if(g.phase==='lobby'){
   const join=node('div',null,'ww-join');
   lobbyUI.join=join;
   join.append(node('h3','用手机入座'));
   if(current.phone_url){
    const link=node('a','打开手机入口');link.href=current.phone_url;link.target='_blank';link.rel='noopener noreferrer';
    lobbyUI.phoneLink=link;
    join.append(link,button('复制加入地址',async()=>{
     try{await navigator.clipboard.writeText(current.phone_url);notice.textContent='加入地址已复制。';}
     catch{notice.textContent=current.phone_url;}
    },'ww-text-button'));
   }
   join.append(node('p','房间号'),node('strong',current.join_code,'ww-room-code'));aside.append(join);
   updateLobbyInvitation();
  }
  const tv=televisionControls();
  if(tv&&g.phase==='lobby')aside.append(tv);
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
  }else if(!finished){
   if(current.speaker_seats?.length)controls.append(speakerControls(g,current.speaker_seats));
   if(g.speaking_turn&&!g.paused){
    const next=button(g.speaking_turn.next_seat?'下一位':'开始投票',()=>perform(api=>api('control',{action:'advance'})),'primary');
    next.dataset.turnCommand='true';controls.append(next);
   }
   if(g.phase==='vote_reveal'&&!g.paused){
    const tied=g.vote_result?.ballot==='vote'&&g.vote_result.tied.length;
    const next=button(tied?'平票发言':'下一环节',()=>perform(api=>api('control',{action:'advance'})),'primary');
    next.dataset.turnCommand='true';controls.append(next);
   }
   if(!g.paused||!returningPhones()){
    const pause=button(g.paused?'继续对局':'暂停对局',()=>perform(api=>api('control',{action:g.paused?'resume':'pause'})));
    pause.dataset.networkRequired=String(g.paused);controls.append(pause);
   }
   if(!g.paused&&!g.speaking_turn&&g.phase!=='vote_reveal')controls.append(button(confirmation==='advance'?'确认进入下一环节':'提前结束本环节',()=>confirmControl('advance'),'ww-text-button'));
  }
  if(finished){
   const rematch=button('再开一局',()=>perform(api=>api('control',{action:'rematch'})),'primary');
   rematch.dataset.rematch='true';rematch.dataset.networkRequired='true';
   controls.append(rematch);
  }
  if(current.phone_url&&!returningPhones()){
   const recover=button('找回座位',openSeatRecovery,'ww-text-button');
   recover.dataset.seatRecovery='true';controls.append(recover);
  }
  const close=button(confirmation==='close'?'确认关闭房间':'关闭房间',()=>confirmControl('close'),'ww-text-button');
  if(lobbyUI)lobbyUI.close=close;
  controls.append(close);aside.append(controls);
  mountRoundCamera(aside,layout);
  if(tv&&g.phase!=='lobby')aside.append(tv);
  const effects=node('div',null,'ww-effects'),voice=node('p',null,'ww-field-help');voice.dataset.voiceState='true';effects.append(voice);const recovery=node('div',null,'ww-recovery');recovery.dataset.lightRecovery='true';recovery.append(node('p','灯光有待恢复。'));const restore=button('恢复灯光',()=>perform(async api=>{await api('restore-lights',{});return api('host');}));restore.dataset.restoreLights='true';recovery.append(restore);effects.append(recovery);aside.append(effects);layout.append(aside);content.append(layout);
  if(!finished){
   if(g.vote_result&&g.phase!=='vote_reveal'){
    const votes=node('section',null,'ww-vote-results');
    votes.append(node('h3','最近一次投票'));
    const line=node('div');
    for(const vote of g.vote_result.votes)line.append(node('span',vote.seat+' 号 → '+(vote.target?vote.target+' 号':'弃票')));
    votes.append(line);
    content.append(votes);
   }
   content.append(node('p',g.roles.map(r=>r.name+' × '+r.count).join('　'),'ww-role-line'),rules(g));
  }
  if(current.camera_enabled&&g.phase==='lobby'){lobbyUI.alignment=cameraAlignment();layout.append(lobbyUI.alignment.section);}
 }
 function networkRecovery(){
  const form=node('form',null,'ww-network-recovery'),copy=node('div',null,'ww-network-copy');
  const title=node('h3','手机与电视已断开'),status=node('p');
  title.id='ww-network-title';status.id='ww-network-summary';
  status.setAttribute('role','status');status.dataset.networkStatus='true';
  form.setAttribute('aria-labelledby',title.id);
  form.setAttribute('aria-describedby',status.id);
  copy.append(title,status);
  const actions=node('div',null,'ww-network-actions'),field=node('label','局域网','ww-field'),select=node('select');
  field.dataset.networkField='true';field.append(select);
  const submit=node('button','恢复连接','primary');submit.type='submit';submit.dataset.networkSubmit='true';
  actions.append(field,submit);form.append(copy,actions);
  form.onsubmit=event=>{
   event.preventDefault();
   if(!submit.disabled&&networkInterrupted())void perform(api=>api('reconnect',{
    address:select.value,network_revision:current.network.revision}));
  };
  return form;
 }
 function updateNetworkRecovery(){
  const form=content.querySelector('.ww-network-recovery');
  if(!networkInterrupted()||!form)return;
  const select=form.querySelector('select'),addresses=current.network?.addresses||[];
  const key=JSON.stringify(addresses);
  if(select.dataset.networkOptions!==key){
   const previous=select.value;
   select.replaceChildren(...addresses.map(address=>{const option=node('option',address);option.value=address;return option;}));
   select.value=addresses.includes(previous)?previous:(addresses[0]||'');
   select.dataset.networkOptions=key;
  }
  form.querySelector('[data-network-field]').hidden=addresses.length<2;
  const submit=form.querySelector('[data-network-submit]');
  submit.hidden=!addresses.length;submit.disabled=busy||syncFailed||!addresses.length;
  const status=form.querySelector('[data-network-status]');
  const text=!addresses.length?'请让 Friday 连回局域网。':
   current.game.paused?'本局已暂停，恢复连接后可从这里继续。':'恢复连接后，玩家可以回到原座位。';
  if(status.textContent!==text)status.textContent=text;
 }
 function phoneRecovery(){
  const section=node('section',null,'ww-network-recovery ww-phone-recovery');
  const copy=node('div',null,'ww-network-copy');
  const title=node('h3',null),status=node('p');
  title.id='ww-phone-return-title';title.dataset.returnTitle='true';title.tabIndex=-1;
  status.dataset.returnStatus='true';status.setAttribute('role','status');
  copy.append(title,status);section.setAttribute('aria-labelledby',title.id);
  const actions=node('div',null,'ww-network-actions');
  const recover=button('找回座位',openSeatRecovery,'primary');recover.dataset.seatRecovery='true';
  actions.append(recover);
  if(current.game.paused){
   const resume=button('继续对局',()=>perform(api=>api('control',{action:'resume'})),'primary');
   resume.dataset.returnResume='true';
   const without=button('直接继续',()=>{
    if(confirmation!=='resume-without-phones'){
     confirmation='resume-without-phones';
     updatePhoneRecovery();
     return;
    }
    confirmation=null;
    void perform(api=>api('control',{action:'resume',continue_without_phones:true}));
   },'ww-text-button');
   without.dataset.returnOverride='true';
   actions.append(resume,without);
  }
  section.append(copy,actions);return section;
 }
 function updatePhoneRecovery(){
  const section=content.querySelector('.ww-phone-recovery');
  if(!section)return;
  const missing=current.network.waiting_seats;
  const title=section.querySelector('[data-return-title]'),status=section.querySelector('[data-return-status]');
  const waiting=missing.length>0;
  if(!waiting&&confirmation==='resume-without-phones')confirmation=null;
  const heading=waiting?'等玩家返回':'玩家已返回';
  if(title.textContent!==heading)title.textContent=heading;
  const confirming=confirmation==='resume-without-phones';
  const detail=waiting?missing.join('、')+' 号尚未返回。'+
   (confirming?'直接继续后，倒计时会恢复。':''):
   current.game.paused?'可以继续当前环节。':'可以继续本局。';
  if(status.textContent!==detail)status.textContent=detail;
  const recover=section.querySelector('[data-seat-recovery]');
  const focus=document.activeElement;
  recover.hidden=!waiting;recover.disabled=busy||syncFailed;
  const resume=section.querySelector('[data-return-resume]'),without=section.querySelector('[data-return-override]');
  if(resume&&without){
   resume.hidden=waiting;resume.disabled=busy||syncFailed||waiting;
   without.hidden=!waiting;without.disabled=busy||syncFailed||!waiting;
   const label=confirming?'确认继续':'直接继续';
   if(without.textContent!==label)without.textContent=label;
  }
  if(!waiting&&(focus===recover||focus===without)){
   (resume&&!resume.disabled?resume:title).focus({preventScroll:true});
  }
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
  return [current.session,current.game.revision,current.network?.revision,Boolean(returningPhones()),JSON.stringify(current.game.players),roundCameraKey()].join('|');
 }
 function render(){
  if(!alive)return;
  if(seatRecovery&&(current?.session!==seatRecovery.session||current.phone_url!==seatRecovery.phoneUrl||current.game.lobby_revision!==seatRecovery.lobbyRevision)){
   seatRecovery.dialog.close();
  }
  revision=renderKey();
  const focus=document.activeElement;
  const cameraFocused=Boolean(roundCamera?.section.contains(focus));
  if(roundCamera&&roundCamera.key!==roundCameraKey()){
   closeRoundPanel(roundCamera);roundCamera=null;
  }
  if(current?.active&&current.game.phase==='lobby'&&lobbyUI?.session===current.session&&
     lobbyUI.count===current.game.player_count&&lobbyUI.cameraEnabled===Boolean(current.camera_enabled)&&
     lobbyUI.networkRevision===current.network?.revision&&
     Boolean(content.querySelector('.ww-phone-recovery'))===Boolean(returningPhones())){
   updateLobby();
  }else{
   lobbyUI?.alignment?.view.dispose();lobbyUI=null;
   const focused=wrap.contains(focus)?focus?.textContent:null;
   content.replaceChildren();
   if(current?.active)live();else setup();
   if(cameraFocused){
    const next=content.contains(focus)?focus:content.querySelector('.ww-phase-heading h3');
    next?.focus({preventScroll:true});
   }else if(focused){
    const candidate=focus?.dataset.turnCommand?
     content.querySelector('[data-turn-command]')||content.querySelector('.ww-phase-heading h3')||content.querySelector('.ww-recap-title'):
     [...content.querySelectorAll('button')].find(b=>b.textContent===focused);
    candidate?.focus({preventScroll:true});
   }
  }
  notice.textContent=lastNotice||notice.textContent;updateRoundCamera();updateControls();updateEffects();updateSeatSignals();
 }
 function updateIdentityProgress(){
  const g=current?.game;
  if(g?.phase!=='identity')return;
  const progress=content.querySelector('.ww-identity-count'),start=content.querySelector('[data-start-night]');
  const title=content.querySelector('[data-identity-title]'),prompt=content.querySelector('.ww-host-line');
  const confirmed=g.identities_confirmed??0,text=confirmed+' / '+g.player_count+' 人已确认';
  const ready=confirmed===g.player_count;
  if(progress&&progress.textContent!==text)progress.textContent=text;
  if(start)start.disabled=busy||syncFailed||!ready||networkInterrupted()||waitingPhones();
  const phaseTitle=ready?'准备入夜':g.phase_name,publicPrompt=ready?'请把手机放低，等候首夜。':g.host_script?.text||g.announcements?.at(-1)?.text||'';
  if(title&&title.textContent!==phaseTitle)title.textContent=phaseTitle;
  if(prompt&&prompt.textContent!==publicPrompt)prompt.textContent=publicPrompt;
 }
 function updateControls(){
  const advancingBlocked=syncFailed||networkInterrupted()||waitingPhones();
  for(const b of content.querySelectorAll('button')){
   if(b.dataset.start==='true'){
    b.disabled=busy||advancingBlocked||current.game.players.filter(p=>p.ready).length!==current.game.player_count;
   }else if(b.dataset.seatRecovery){
    b.hidden=!current.game.players.length;
    b.disabled=busy||syncFailed;
   }else if(b.dataset.displayPair)b.disabled=busy||syncFailed;
   else if(!b.dataset.startNight&&!b.dataset.speakerCommand){
    b.disabled=busy||b.dataset.unavailable==='true'||b.dataset.networkRequired==='true'&&networkInterrupted();
   }
  }
  for(const select of content.querySelectorAll('select')){
   if(!select.dataset.speakerPicker)select.disabled=busy||Boolean(select.dataset.cameraPicker&&syncFailed);
  }
  updateIdentityProgress();updateSpeakerControls();updateDisplayConnection();updateNetworkRecovery();updatePhoneRecovery();
 }
 async function refresh(first=false){
  if(!alive||busy||readRequest)return;
  const request={generation:++generation,controller:new AbortController()};readRequest=request;
  const owns=()=>alive&&!busy&&readRequest===request&&generation===request.generation;
  try{
   const needsContext=first||context===null,previousSession=current?.session,previousRevision=current?.game?.revision;
   const previousNetwork=current?.network?.revision;
   const data=await api(needsContext?'context':'host',undefined,request.controller.signal);if(!owns())return;
   if(needsContext){context=data;current=data.current;}else current=data;
   if(syncFailed&&notice.textContent===syncNotice)notice.textContent=lastNotice;
   syncFailed=false;
   const next=renderKey();
   if(needsContext||revision!==next){if(needsContext||previousSession!==current?.session||previousRevision!==current?.game?.revision||previousNetwork!==current?.network?.revision)confirmation=null;render();}
   else if(current.active){const timer=content.querySelector('[data-timer]');if(timer)timer.textContent=current.game.paused?'已暂停':current.game.seconds_left==null?'':Math.max(0,current.game.seconds_left)+' 秒';if(lobbyUI?.alignment){lobbyUI.alignment.view.recheck();updateAlignment(lobbyUI.alignment);}updateRoundCamera();updateEffects();updateSeatSignals();updateControls();}
  }catch{if(owns()){syncFailed=true;notice.textContent=syncNotice;lobbyUI?.alignment?.view.recheck();updateRoundCamera();updateSeatSignals();updateControls();}}
  finally{if(readRequest===request)readRequest=null;}
 }
 content.append(node('p','正在准备房间…','ww-field-help'));void refresh(true);const timer=setInterval(()=>{if(!busy)void refresh();},1000);
 return{dispose(){alive=false;seatRecovery?.dialog.close();displayPair?.dialog.close();lobbyUI?.alignment?.view.dispose();if(roundCamera)closeRoundPanel(roundCamera);roundCamera=null;generation++;readRequest?.controller.abort();commandRequest?.controller.abort();readRequest=commandRequest=lobbyUI=null;abort.abort();clearInterval(timer);wrap.remove();}};
}
