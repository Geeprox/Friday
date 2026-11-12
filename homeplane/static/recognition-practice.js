import {MOTION_GUIDES,createMotionDiagram,motionTile} from './motion-guide.js';
const h=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
const button=(text,fn,cls)=>{const b=h('button',text,cls);b.type='button';b.onclick=fn;return b;};
const request=async(path,body)=>{const r=await fetch('/api/embodied/practice/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(5000),keepalive:path==='stop'});const data=await r.json();if(!r.ok)throw Error(data.detail||'unavailable');return data;};
// 页面内切换动作后仍持有取消责任；开始回执丢失也不丢弃原请求。
const pendingPractice=new Set();
function stopPractice(session){
 if(!pendingPractice.has(session))return Promise.resolve();
 if(session.stopping)return session.stopping;
 const task=request('stop',{request_id:session.id,...(session.token?{token:session.token}:{})}).then(result=>{
  if(result.phase!=='ended'||result.request_id!==session.id)throw Error('stop_unconfirmed');
  pendingPractice.delete(session);
 });
 session.stopping=task;void task.finally(()=>{if(session.stopping===task)session.stopping=null;}).catch(()=>{});return task;
}
export function mountPractice(root,{gesture,go=()=>{},onClose=()=>{},cameras=[],paused=false}){
 const guide=MOTION_GUIDES[gesture];if(!guide)return{dispose(){}};const returnFocus=document.activeElement;
 let alive=true,current=null,generation=0,timer=null,announced=null,audio=null,starting=false,closing=false;
 const panel=h('section',null,'practice-panel');panel.setAttribute('aria-label',guide.name+'体验');const diagram=createMotionDiagram(gesture),content=h('div',null,'practice-content');diagram.setPaused?.(paused);panel.append(diagram,content);root.replaceChildren(panel);
 const heading=h('h2',guide.name+' · 试一试'),instruction=h('p',guide.instruction,'practice-instruction'),state=h('p',null,'practice-state');state.setAttribute('role','status');state.setAttribute('aria-live','polite');const symbol=h('span',null,'practice-symbol'),mark=h('span',null,'icon icon-eye');mark.setAttribute('aria-hidden','true');symbol.append(mark);const stateText=h('strong','准备好后，走到任一已开启的镜头前');state.append(symbol,stateText);
 const sourceText=h('p',null,'practice-source');sourceText.hidden=true;
 const progress=h('progress',null,'practice-progress');progress.max=1;progress.value=0;progress.setAttribute('aria-label','动作识别进度');progress.hidden=true;
 const hint=h('p','只练习识别，练习期间暂停手势设备控制。','practice-hint'),option=h('label',null,'practice-options'),sound=h('input');sound.type='checkbox';sound.checked=true;option.append(sound,h('span','识别成功时，在这台设备播放提示音'));
 const actions=h('div',null,'practice-actions'),start=button('开始体验',()=>begin(),'primary'),end=button('收起',()=>close()),recovery=button('查看摄像头',()=>go(recovery.dataset.destination));recovery.hidden=true;actions.append(start,end,recovery);const remaining=h('p',null,'practice-time');content.append(heading,instruction,state,sourceText,progress,hint,option,actions,remaining);heading.tabIndex=-1;heading.focus({preventScroll:true});
 async function readySound(){if(!sound.checked)return;try{const Audio=globalThis.AudioContext||globalThis.webkitAudioContext;if(!Audio)throw Error();audio??=new Audio();await audio.resume();if(audio.state!=='running')throw Error();}catch{hint.textContent='此设备暂时无法播放提示音，仍会显示识别结果。';}}
 function successSound(){if(!sound.checked)return;try{if(audio?.state!=='running')throw Error();const at=audio.currentTime;for(const[f,delay]of [[659.25,0],[880,.13]]){const o=audio.createOscillator(),gain=audio.createGain();o.type='sine';o.frequency.value=f;gain.gain.setValueAtTime(0,at+delay);gain.gain.linearRampToValueAtTime(.16,at+delay+.015);gain.gain.exponentialRampToValueAtTime(.001,at+delay+.28);o.connect(gain);gain.connect(audio.destination);o.start(at+delay);o.stop(at+delay+.3);}}catch{hint.textContent='动作已识别，提示音未能播放。';}}
 function show(phase,text,location=''){panel.dataset.state=state.dataset.state=phase;if(stateText.textContent!==text)stateText.textContent=text;sourceText.hidden=!location;if(sourceText.textContent!==location)sourceText.textContent=location;sourceText.title=location;mark.className='icon icon-'+(phase==='success'?'check':phase==='recognizing'?'scan':'eye');}
 function recover(destination){recovery.hidden=!destination;if(destination){recovery.dataset.destination=destination;recovery.textContent=destination==='Games'?'前往家庭游戏':'查看摄像头';}}
 function cancellationPending(){show('blocked','尚未确认体验已结束');hint.textContent='请重试结束，再开始下一次体验。';start.hidden=true;end.textContent='重试结束';progress.hidden=true;remaining.textContent='';recover('Cameras');}
 async function release(){const old=current;if(!old)return true;try{await stopPractice(old);if(current===old)current=null;return true;}catch{return false;}}
 async function begin(){
  if(!alive||starting||closing)return;starting=true;const round=++generation;clearTimeout(timer);start.disabled=true;recover(null);progress.hidden=true;remaining.textContent='';hint.textContent='';show('preparing','正在准备体验…');
  try{
   const bytes=crypto.getRandomValues(new Uint8Array(32)),session={id:Array.from(bytes,n=>n.toString(16).padStart(2,'0')).join(''),token:null,stopping:null};
   const released=await Promise.allSettled([...pendingPractice].map(stopPractice));
   if(!alive||round!==generation)return;
   if(released.some(result=>result.status==='rejected')){current=[...pendingPractice][0]||current;cancellationPending();return;}
   current=session;pendingPractice.add(session);await readySound();
   if(!alive||round!==generation)return;
   const result=await request('start',{gesture,request_id:session.id,issued_at_ms:Date.now()});
   if(!alive||round!==generation){void stopPractice(session).catch(()=>{});return;}
   if(result.request_id!==session.id)throw Error('start_unconfirmed');
   if(result.phase==='blocked'||result.phase==='ended'){
    pendingPractice.delete(session);current=null;start.textContent='重新体验';end.textContent='收起';option.hidden=false;
    if(result.phase==='ended'){update(result);return;}
    const game=result.reason==='game_active',active=result.reason==='practice_active';show('blocked',game?'先结束当前游戏，再开始体验':active?'另一项体验还未结束':'还没有可用的识别镜头');hint.textContent=game?'返回游戏结束本局后，再来试一试。':active?'请先结束当前体验，再来试一试。':'在摄像头页开启镜头与识别后，即可开始。';recover(active?null:game?'Games':'Cameras');return;
   }
   if(!/^[a-f0-9]{32}$/.test(result.token||''))throw Error('start_unconfirmed');
   session.token=result.token;announced=null;start.hidden=true;end.textContent='结束体验';option.hidden=true;update(result);void poll(round,session);
  }catch(error){
   if(!alive||round!==generation)return;
   show('ending','正在结束本次体验…');const ended=await release();
   if(!alive||round!==generation)return;
   if(!ended){cancellationPending();return;}
   const active=error.message==='practice_already_active';show('blocked',active?'另一项体验还未结束':'体验暂未开始');hint.textContent=active?'请先结束当前体验，再来试一试。':'请检查摄像头与识别服务后重试。';start.hidden=false;start.textContent='重新体验';end.textContent='收起';option.hidden=false;recover(active?null:'Cameras');
  }finally{if(alive&&round===generation){starting=false;start.disabled=false;(start.hidden?end:start).focus({preventScroll:true});}}
 }
 function update(result){if(!alive)return;const phase=result.phase;progress.hidden=!['seen','recognizing'].includes(phase);progress.value=result.progress||0;remaining.textContent=result.remaining?`本次体验剩余 ${result.remaining} 秒` :'';const texts={waiting:'等待你进入画面',multiple:'请先由一人站在镜头前',seen:'看到你了，跟着示意做动作',recognizing:'识别中，继续保持',success:'识别成功',ended:'本次体验已结束'};const source=cameras.find(c=>c.id===result.camera);show(phase,texts[phase]||'等待识别',source&&['seen','recognizing','success'].includes(phase)?source.room||source.name||'':'');
  hint.textContent=phase==='success'?'已收到这个动作。练习期间没有控制设备。':phase==='seen'?guide.instruction:phase==='multiple'?'其他人可以先离开画面，随后可分别体验。':phase==='ended'?'需要时可以再试一次。':'手掌、肩膀或膝盖请按示意清晰入镜。';
  if(phase==='success'&&result.event?.id&&announced!==result.event.id){announced=result.event.id;successSound();start.hidden=false;start.textContent='再试一次';}
  if(phase==='ended'){if(current){pendingPractice.delete(current);current=null;}start.hidden=false;start.textContent='重新体验';end.textContent='收起';option.hidden=false;}
 }
 async function poll(round,session){if(!alive||round!==generation||current!==session)return;try{const result=await request('status',{request_id:session.id,token:session.token});if(!alive||round!==generation||current!==session)return;if(result.request_id!==session.id)throw Error('status_unconfirmed');update(result);}catch{if(alive&&round===generation){show('reconnecting','连接暂时中断');hint.textContent='正在重新连接，尚未确认识别结果。';}}if(alive&&round===generation&&current===session)timer=setTimeout(()=>poll(round,session),400);}
 async function close(){if(closing||!alive)return;closing=true;starting=false;const round=++generation;clearTimeout(timer);start.disabled=end.disabled=true;if(current)show('ending','正在结束体验…');const ended=await release();if(!alive||round!==generation)return;closing=false;if(ended){dispose();onClose();if(returnFocus?.isConnected)returnFocus.focus({preventScroll:true});}else{cancellationPending();end.disabled=false;end.focus({preventScroll:true});}}
 function pagehide(){const round=++generation;starting=closing=false;clearTimeout(timer);void release().then(ended=>{if(!alive||round!==generation)return;start.disabled=end.disabled=false;if(ended)update({phase:'ended'});else cancellationPending();});}
 globalThis.addEventListener?.('pagehide',pagehide);
 function dispose(){if(!alive)return;alive=false;generation++;clearTimeout(timer);void release();void audio?.close?.();diagram.dispose();globalThis.removeEventListener?.('pagehide',pagehide);panel.remove();}
 return{dispose,setPaused(value){diagram.setPaused?.(value);}};
}
export function mountMotionLibrary(root,{go=()=>{},onChoose,keys=null,cameras=[],heading='手势与动作',description='看示意，试一次，再设置你希望发生的事。'}={}){
 let practice=null,tiles=[],group='hand',paused=false;const section=h('section',null,'motion-library'),head=h('div',null,'motion-library-heading'),titles=h('div');titles.append(h('h2',heading));if(description)titles.append(h('p',description));const playback=button('暂停示意动画',()=>{paused=!paused;playback.textContent=paused?'播放示意动画':'暂停示意动画';playback.setAttribute('aria-pressed',String(paused));practice?.setPaused?.(paused);for(const tile of tiles)tile.querySelector('.motion-diagram')?.setPaused?.(paused);});playback.setAttribute('aria-pressed','false');head.append(titles,playback);const filters=h('div',null,'motion-filter'),practiceSlot=h('div'),grid=h('div',null,'motion-grid');section.append(head,filters,practiceSlot,grid);root.append(section);
 const available=keys?keys.filter(k=>MOTION_GUIDES[k]):Object.keys(MOTION_GUIDES);group=MOTION_GUIDES[available[0]]?.group||'hand';
 const tryIt=key=>{practice?.dispose();practice=mountPractice(practiceSlot,{gesture:key,go,cameras,paused,onClose:()=>{practice=null;practiceSlot.replaceChildren();}});if(onChoose&&key!=='hand.point'&&MOTION_GUIDES[key].group!=='game'){const use=button('设置这个动作的设备控制',()=>onChoose(key));practiceSlot.querySelector('.practice-actions')?.append(use);}practiceSlot.scrollIntoView?.({block:'nearest',behavior:globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});};
 function draw(){for(const tile of tiles)tile.dispose();tiles=[];grid.replaceChildren();filters.replaceChildren();if(!keys)for(const[key,name]of [['hand','手部手势'],['body','身体动作'],['game','游戏动作']]){const b=button(name,()=>{group=key;draw();});b.setAttribute('aria-pressed',String(group===key));filters.append(b);}for(const key of available.filter(k=>keys||MOTION_GUIDES[k].group===group)){const tile=motionTile(key,tryIt);tiles.push(tile);grid.append(tile);tile.querySelector('.motion-diagram')?.setPaused?.(paused);}}
 draw();return{tryIt,dispose(){practice?.dispose();for(const tile of tiles)tile.dispose();section.remove();}};
}
