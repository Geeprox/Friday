/* Local safety review. Untrusted names and observations are always text nodes. */
import {soundReviewNames as soundNames,isSafetyOutputTest as isOutputTest,safetyEventTitle as eventTitle,isRapidMovementReview} from './safety-event-labels.js';
export function mountSafety(container,{go=()=>{},onOpenRecording=()=>{},onArchiveSettings=()=>go('Settings'),initialEventId=null,initialReview=null,onEventSelected=()=>{}}={}){
 const base='/api/embodied/safety';let alive=true,report=null,busy=false,lastEvents='',lastDetail='',selected=null,selectionVersion=0,timer,events=[],historyLoaded=false,statusFresh=false,retrying=false,retryController=null,retryTimer=null;
 const statusFeed={read:null},historyFeed={read:null};
 const validEventId=id=>typeof id==='string'&&/^[a-f0-9]{32}$/.test(id)?id:null;let requested=validEventId(initialEventId);
 const eventTime=event=>Number.isFinite(event.captured_at)?event.captured_at:event.at;
 const el=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
 const add=(n,...children)=>{n.append(...children);return n;};
 const button=(text,run)=>{const n=el('button',text,{type:'button'});n.onclick=run;return n;};
 const api=async(path,body,options={})=>{const r=await fetch(base+path,{headers:{'Content-Type':'application/json'},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)}),...options});if(!r.ok)throw Error('unavailable');return r.json();};
 const root=el('div',null,{class:'safety-page','data-detail-open':'false'});container.append(root);
 if(!document.getElementById('safety-style'))document.head.append(el('link',null,{id:'safety-style',rel:'stylesheet',href:'/static/safety.css'}));
 const status=el('p','正在读取监测状态…',{role:'status','aria-live':'polite',tabindex:'-1'}),enable=button('开启监测',()=>change('enabled')),announce=button('开启语音提醒',()=>change('announce'));
 const test=button('试听跌倒提醒',async()=>{if(!alive||busy||!statusFresh)return;busy=true;availability();status.textContent='正在发送测试提醒…';try{await api('/test',{});await Promise.all([refreshStatus(true),refreshHistory(true)]);}catch{if(alive)status.textContent='试听未发送，请检查播报音箱连接。';}finally{busy=false;if(alive)availability();}});
 const intro=add(el('section',null,{class:'safety-intro'}),el('h2','家中提醒'),el('p','发现疑似跌倒时记录事件，按需通过选定音箱提醒家人。'),status,
   add(el('div',null,{class:'safety-actions'}),enable,announce,test,button('选择播报音箱',()=>go('Settings'))),
   el('p','此功能仍在试用，可能漏报或误报。请保持日常照护。事件画面仅保存在本机，保留 30 天。',{class:'caption'}));
 const visualStatus=el('p','正在检查本机画面复核…',{role:'status','aria-live':'polite'});
 const visualToggle=button('开启画面复核',()=>change('visual_review'));visualToggle.setAttribute('role','switch');visualToggle.setAttribute('aria-checked','false');visualToggle.disabled=true;
 const postureSelect=el('select',null,{id:'safety-posture-camera','aria-describedby':'safety-posture-status'});
 const postureNote=el('p','',{id:'safety-posture-status',role:'status',class:'caption'});
 const postureField=add(el('div',null,{class:'safety-posture'}),el('label','趴卧复核镜头',{for:'safety-posture-camera'}),postureSelect,postureNote);
 postureField.hidden=true;postureSelect.disabled=true;let postureOptions='';
 postureSelect.onchange=()=>void selectPostureCamera();
 const visualTitle=el('h2','本机画面复核',{tabindex:'-1'}),soundTitle=el('h2','声音复核',{tabindex:'-1'});
 const visual=add(el('section',null,{class:'safety-visual'}),visualTitle,el('p','查看面部遮挡、快速搬动等画面线索。'),visualStatus,
   add(el('div',null,{class:'safety-actions'}),visualToggle),postureField);
 const soundStatus=el('p','正在读取声音复核状态…',{role:'status','aria-live':'polite'});
 const soundToggle=button('开启声音复核',()=>change('sound_review'));soundToggle.setAttribute('role','switch');soundToggle.setAttribute('aria-checked','false');soundToggle.disabled=true;
 const soundSearch=button('开启异响后环视',()=>change('sound_search'));
 soundSearch.setAttribute('role','switch');soundSearch.setAttribute('aria-checked','false');
 soundSearch.setAttribute('aria-describedby','safety-sound-search-note');soundSearch.hidden=true;
 const soundSearchNote=el('p','自动镜头在异响留档后查看附近画面。',{class:'caption',id:'safety-sound-search-note'});
 soundSearchNote.hidden=true;
 const sound=add(el('section',null,{class:'safety-sound'}),soundTitle,el('p','留意疑似哭声、尖叫、破碎声和警报，记录后由你查看。'),soundStatus,
   add(el('div',null,{class:'safety-actions'}),soundToggle,soundSearch),soundSearchNote,
   el('p','声音仅在本机内存处理。暂停声音输入会同时暂停复核。',{class:'caption'}));
 const capabilities=el('section',null,{class:'safety-capabilities','aria-label':'监测能力'}),list=el('section',null,{class:'safety-events','aria-label':'预警记录'}),detail=el('section',null,{class:'safety-detail','aria-label':'事件详情'});
 const detailBody=el('div'),detailFooter=el('div'),recordings=el('section',null,{class:'safety-recording','aria-label':'事件附近录像',tabindex:'-1'}),back=button('全部提醒',backToList);
 back.className='safety-back';detail.append(back,detailBody,recordings,detailFooter);detail.hidden=true;
 const frameButtons=new Map();
 let selectedFrame=null;
 function selectRecordingFrame(ordinal){
  selectedFrame=ordinal==null?null:String(ordinal);
  for(const [key,control] of frameButtons)control.setAttribute('aria-pressed',String(key===selectedFrame));
 }
 const stopRecording=()=>{selectRecordingFrame(null);try{const pending=onOpenRecording(recordings,null);pending?.catch?.(()=>{});}catch{recordings.replaceChildren();}};
 const limits=add(el('details',null,{class:'safety-limits'}),el('summary','监测范围与限制'),capabilities);
 const historyStatus=el('p','正在读取提醒…',{class:'safety-history-status',role:'status'});
 const historyView=el('div',null,{class:'safety-history'}),settings=add(el('details',null,{class:'safety-settings'}),el('summary','监测设置'),intro,visual,sound,limits);historyView.append(historyStatus,add(el('div',null,{class:'safety-list-panel'}),add(el('div',null,{class:'safety-actions'}),el('h2','近期提醒'),button('刷新记录',()=>refreshHistory())),list),detail);
 const reconnect=button('重试连接',()=>retryConnection()),cameraCheck=button('查看摄像头',()=>go('Cameras')),recoveryActions=add(el('div',null,{class:'safety-actions',role:'group','aria-label':'监测连接恢复'}),reconnect,cameraCheck);recoveryActions.hidden=true;root.append(status,recoveryActions,historyView,settings);
 const reviewNames={unreviewed:'待查看',acknowledged:'已查看',false_alarm:'已标记误报'};
 const eventRows=new Map();
 const audioNames={not_requested:'未请求播报',requesting:'正在发送播报',accepted:'音箱已接受，实际声音待确认',unconfirmed:'播报结果待确认',paused:'安静模式，未播报',busy:'音箱忙，未播报',unavailable:'播报不可用',cancelled:'已取消播报',interrupted:'播报已中断'};
 function availability(){
  const disabled=busy||!statusFresh;
  enable.disabled=announce.disabled=test.disabled=disabled;
  visualToggle.disabled=disabled||(!report?.policy?.visual_review&&(!report?.policy?.enabled||!report?.visual_review?.available));
  soundToggle.disabled=disabled||(!report?.policy?.sound_review&&(!report?.policy?.enabled||!report?.sound_review?.available));
  reconnect.disabled=disabled||retrying||!report?.supervision?.recovery?.can_retry;
  soundSearch.disabled=disabled||!report?.sound_search&&!report?.policy?.enabled;
  const posture=report?.posture_review;postureSelect.disabled=disabled||(!posture?.camera&&!posture?.cameras?.length);
 }
 function renderPosture(){
  const posture=report?.posture_review;postureField.hidden=!posture;if(!posture)return;
  const replaced=posture.state==='source_changed',value=replaced?'previous':posture.camera||'';
  const options=JSON.stringify([replaced,posture.cameras]);
  if(options!==postureOptions){
   postureOptions=options;const nodes=[el('option','关闭',{value:''})];
   if(replaced){const previous=el('option','请重新选择',{value:'previous'});previous.disabled=true;nodes.push(previous);}
   for(const source of posture.cameras)nodes.push(el('option',source.name,{value:source.id}));
   postureSelect.replaceChildren(...nodes);
  }
  if(postureSelect.value!==value)postureSelect.value=value;
  const message=({source_changed:'摄像头来源已变化，请重新选择。',paused:'开启画面复核后继续。',unavailable:'等待本机画面复核就绪。',deferred:'暂缓复核，稍后自动重试。',observing:'已收到清晰画面。',waiting:'等待这台摄像头的清晰画面。'})[posture.state]||
    (posture.cameras.length?'试用。出现疑似趴卧时保存关键帧。':'添加摄像头后可选择。');
  if(postureNote.textContent!==message)postureNote.textContent=message;
 }
 async function selectPostureCamera(){
  const revision=report?.policy?.revision,value=postureSelect.value,posture=report?.posture_review;
  if(!alive||busy||!statusFresh||!Number.isInteger(revision)||!posture||value&&!posture.cameras.some(row=>row.id===value))return;
  const camera=value||null,focused=document.activeElement===postureSelect,previous=report;
  busy=true;availability();postureNote.textContent='正在保存…';
  try{await api('/posture',{camera,expected_revision:revision});await refreshStatus(true);if(alive&&report===previous)postureNote.textContent='已保存，等待状态更新。';}
  catch{await refreshStatus(true);if(alive&&(report===previous||report.posture_review?.camera!==camera||report.posture_review?.state==='source_changed'))postureNote.textContent='设置未确认，请重新选择。';}
  finally{busy=false;if(alive){availability();if(focused&&(document.activeElement===document.body||document.activeElement===postureSelect))postureSelect.focus({preventScroll:true});}}
 }
 async function retryConnection(){
  const revision=report?.supervision?.policy?.revision;
  if(!alive||busy||!statusFresh||retrying||!report?.supervision?.recovery?.can_retry||!Number.isInteger(revision))return;
  const focused=recoveryActions.contains(document.activeElement);retrying=true;availability();recoveryActions.setAttribute('aria-busy','true');status.textContent='正在重试连接…';if(focused)status.focus({preventScroll:true});
  const controller=typeof AbortController==='function'?new AbortController():null,timeout=controller?setTimeout(()=>controller.abort(),5000):null;retryController=controller;retryTimer=timeout;
  try{await api('/retry',{expected_revision:revision},controller?{signal:controller.signal}:{});if(alive)await refreshStatus(true);}
  catch{if(alive){status.textContent='暂未确认连接结果，正在读取状态…';await refreshStatus(true);}}
  finally{clearTimeout(timeout);retryTimer=null;retryController=null;retrying=false;if(alive){recoveryActions.setAttribute('aria-busy','false');availability();}}
 }
 async function change(key){
  if(!alive||busy||!statusFresh||!report)return;
  const search=key==='sound_search',focused=search&&document.activeElement===soundSearch;
  if(search&&(soundSearch.hidden||soundSearch.disabled))return;
  busy=true;availability();
  try{
   if(search)await api('/sound-search',{enabled:!report.sound_search,expected_revision:report.policy.revision});
   else await api('/policy',{...report.policy,[key]:!report.policy[key]});
   await refreshStatus(true);
  }catch{if(alive)(search?soundStatus:status).textContent='设置未保存，请刷新后重试。';}
  finally{
   busy=false;
   if(alive){availability();if(focused&&!soundSearch.hidden&&!soundSearch.disabled&&
       (document.activeElement===document.body||document.activeElement===soundSearch))soundSearch.focus({preventScroll:true});}
  }
 }
 function renderStatus(next){
   if(!next?.policy||!next.worker||!Array.isArray(next.capabilities))throw Error('unavailable');
   report=next;statusFresh=true;
   enable.textContent=next.policy.enabled?'暂停监测':'开启监测';enable.setAttribute('role','switch');enable.setAttribute('aria-checked',String(next.policy.enabled));
   announce.textContent=next.policy.announce?'关闭语音提醒':'开启语音提醒';announce.setAttribute('role','switch');announce.setAttribute('aria-checked',String(next.policy.announce));
   const model=next.visual_review||{};visualToggle.textContent=next.policy.visual_review?'关闭画面复核':'开启画面复核';visualToggle.setAttribute('aria-checked',String(Boolean(next.policy.visual_review)));visualToggle.disabled=busy||(!next.policy.visual_review&&(!next.policy.enabled||!model.available));
   renderPosture();
   const modelReasons={model_not_installed:'本机模型尚未安装，完成本地模型配置后可开启。',model_pin_mismatch:'本机模型需要核对，重新检查模型文件后重试。',local_runtime_missing:'本机运行环境尚未就绪，完成本地模型配置后重试。',apple_silicon_required:'当前主机不支持这套本地模型。',memory_pressure:'本机内存紧张，画面复核已暂缓。',memory_capacity:'本机可用内存容量不足，画面复核已暂缓。',memory_unknown:'暂时无法检查本机内存，稍后自动重试。',cpu_busy:'本机正在忙，稍后继续复核。',model_busy:'本机模型正在处理其他请求，稍后重试。',visual_review_deferred:'画面复核暂不可用，请检查本机模型后重试。'};
   const visualMessage=!model.available?(modelReasons[model.reason]||'本机模型尚未就绪，完成配置后可开启。'):!next.policy.enabled?'先开启监测。':next.policy.visual_review?(model.error?(modelReasons[model.error]||'画面复核已暂缓，稍后重试。'):({waiting_input:'等待摄像头画面。',waiting:'等待首轮画面复核。',delayed:'画面复核延迟较高。'}[model.progress]||'')):'';
   if(visualStatus.textContent!==visualMessage)visualStatus.textContent=visualMessage;visualStatus.hidden=!visualMessage;
   const soundModel=next.sound_review||{};soundToggle.textContent=next.policy.sound_review?'关闭声音复核':'开启声音复核';soundToggle.setAttribute('aria-checked',String(Boolean(next.policy.sound_review)));availability();
   soundSearch.hidden=soundSearchNote.hidden=typeof next.sound_search!=='boolean'||
    !next.sound_search&&(!next.policy.sound_review||!soundModel.available);
   soundSearch.textContent=next.sound_search?'关闭异响后环视':'开启异响后环视';
   soundSearch.setAttribute('aria-checked',String(next.sound_search===true));
   const supported=(soundModel.supported_types||[]).filter(kind=>soundNames[kind]).map(kind=>soundNames[kind]);
   soundStatus.textContent=!soundModel.available?'当前版本尚未提供本机声音复核。':!next.policy.enabled?'开启监测后可使用声音复核。':!next.policy.sound_review?'声音复核已关闭。':soundModel.input_paused?'声音输入已暂停。':soundModel.error?({'sound_memory_pressure':'本机内存紧张，稍后自动重试。','sound_cpu_busy':'本机正忙，稍后自动重试。','sound_labels_unavailable':'本机分类器尚未提供这些声音类别。'}[soundModel.error]||'声音复核暂缓，稍后自动重试。'):({waiting_input:'等待摄像头声音。',waiting:'等待首轮声音复核。',delayed:'声音复核延迟较高。'}[soundModel.progress]||(supported.length?'已开启 · '+supported.join('、'):'已开启，等待摄像头声音。'));
   const count=next.worker.cameras?.filter(c=>c.fresh).length||0;
   const recording=next.worker.recording;
   const recordingFailure=recording?.dropped>0?'有提醒未能保存，请检查本机存储。':recording?.unconfirmed>0?'有提醒的保存结果尚未确认，请查看记录并检查本机存储。':recording?.partial>0?'有提醒的画面未完整保存，可查看已保存的记录。':'提醒记录暂不可用，请检查本机存储。';
   const supervision=next.supervision,recovery=supervision?.recovery,paused=!next.policy.enabled||Boolean(supervision?.policy&&(!supervision.policy.enabled||!supervision.policy.recognition));
   const disconnected=['not_running','unavailable'].includes(next.worker.status),recovering=recovery?.automatic&&(recovery.in_progress||supervision.failure==='service_unavailable');
   const recoveryFocused=recoveryActions.contains(document.activeElement);recoveryActions.hidden=paused||!disconnected;reconnect.hidden=!recovery?.automatic||supervision?.failure!=='service_unavailable';availability();
   const message=next.worker.error||recording?.state==='unavailable'?recordingFailure:paused?'监测已暂停':disconnected?(recovering?'正在恢复监测，稍后自动重试。':next.worker.status==='not_running'?'监测尚未运行':'暂时无法读取监测状态'):next.worker.status!=='running'?'监测状态尚未更新':recording?.state==='recovering'?'提醒正在保存，稍后自动重试。':recording?.state==='saving'?'提醒正在保存。':count?`${count} 台摄像头正在监测 · ${next.policy.announce?'单设备语音提醒':'只记录事件'}`:'监测已开启，等待清晰、稳定的摄像头画面';if(status.textContent!==message)status.textContent=message;
   if(recoveryFocused&&(recoveryActions.hidden||document.activeElement===reconnect&&reconnect.disabled))status.focus({preventScroll:true});
   capabilities.replaceChildren(el('h2','监测场景'));
   const descriptions={rough_handling:'目前无法可靠判断照护动作的力度或意图。'};
   for(const c of next.capabilities){
    let label=c.status==='experimental'?'试用功能':'尚未提供';
    if(c.id==='visual_review'&&!model.available)label='需要设置';
    if(c.id==='sound_review'&&!soundModel.available)label='尚未提供';
    if(c.id==='crying'&&c.status==='experimental'){
     if(!soundModel.available||soundModel.infant_cry_supported===false)label='尚未提供';
     else if(!next.policy.enabled||!next.policy.sound_review)label='随声音复核开启';
     else if(soundModel.infant_cry_supported!==true)label='等待声音输入';
    }
    capabilities.append(add(el('article'),el('strong',c.name),el('span',label,{class:'caption'}),el('p',descriptions[c.id]||c.basis,{class:'caption'})));
   }
 }
 function renderHistory(history){
   if(!Array.isArray(history?.events))throw Error('unavailable');
   const fingerprint=JSON.stringify(history.events);historyLoaded=true;events=history.events;historyView.dataset.empty=String(!events.length);historyStatus.hidden=true;
   if(fingerprint!==lastEvents){lastEvents=fingerprint;list.replaceChildren();eventRows.clear();
     if(!events.length)list.append(el('p','暂无提醒记录',{class:'product-empty'}));
     for(const e of events){const row=button('',()=>choose(e.id));row.className='safety-event';row.setAttribute('aria-current',String(e.id===selected));eventRows.set(e.id,row);row.append(el('strong',eventTitle(e)),el('span',(e.camera_name?e.camera_name+' · ':'')+new Date(eventTime(e)*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})),el('span',reviewNames[e.review]||'待查看'));list.append(row);}
     if(requested){const id=requested;requested=null;selectEvent(id);}
     else if(selected){const current=events.find(e=>e.id===selected);if(current)show(current);else missingEvent(selected);}
     else if(events.length)show(events.find(e=>e.review==='unreviewed')||events[0]);
   }
 }
 function retireRead(feed){
  const read=feed.read;
  if(!read)return;
  feed.read=null;
  clearTimeout(read.timeout);
  read.controller?.abort();
 }
 function readFeed(feed,path,render,unavailable,replace){
  if(!alive)return;
  if(feed.read&&!replace)return feed.read.done;
  retireRead(feed);
  const controller=typeof AbortController==='function'?new AbortController():null;
  const read={controller,timeout:controller?setTimeout(()=>controller.abort(),6500):null};
  feed.read=read;
  const owns=()=>alive&&feed.read===read;
  read.done=api(path,undefined,controller?{signal:controller.signal}:{})
   .then(data=>{if(owns())render(data);})
   .catch(()=>{if(owns())unavailable();})
   .finally(()=>{clearTimeout(read.timeout);if(feed.read===read)feed.read=null;});
  return read.done;
 }
 function refreshStatus(replace=false){
  return readFeed(statusFeed,'',renderStatus,()=>{
   const focused=recoveryActions.contains(document.activeElement);
   statusFresh=false;recoveryActions.hidden=true;availability();
   status.textContent='暂时无法读取监测状态。';
   if(focused)status.focus({preventScroll:true});
  },replace);
 }
 function refreshHistory(replace=false){
  return readFeed(historyFeed,'/events',renderHistory,()=>{
   historyStatus.hidden=false;
   historyStatus.textContent=historyLoaded?'暂时无法刷新，显示上次读取的提醒。':'暂时无法读取提醒，请重试。';
  },replace);
 }
 function refresh(){void refreshStatus();void refreshHistory();}
 function backToList(){selectionVersion++;stopRecording();recordings.replaceChildren();root.dataset.detailOpen='false';if(!events.length){selected=null;detail.hidden=true;}onEventSelected(null);eventRows.get(selected)?.focus?.({preventScroll:true});}
 function selectEvent(id){if(!alive||!validEventId(id))return;requested=id;if(!historyLoaded)return;requested=null;const event=events.find(e=>e.id===id);if(event)show(event,{focus:true});else missingEvent(id,{focus:true});}
 function choose(id){selectEvent(id);onEventSelected(id);}
 function focusDetail(heading){root.dataset.detailOpen='true';heading.focus?.({preventScroll:true});detail.scrollIntoView?.({block:'start',behavior:'instant'});}
 function missingEvent(id,{focus=false}={}){selected=id;lastDetail='';selectionVersion++;stopRecording();recordings.replaceChildren();frameButtons.clear();detailFooter.replaceChildren();detail.hidden=false;for(const row of eventRows.values())row.setAttribute('aria-current','false');const heading=el('h2','这条提醒已不在记录中',{tabindex:'-1'});detailBody.replaceChildren(heading,button('查看近期提醒',()=>{const current=events.find(e=>e.review==='unreviewed')||events[0];if(current)choose(current.id);else backToList();}));if(focus)focusDetail(heading);}
 const frameTime=frame=>new Date(frame.at*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai'});
 function frameGrid(event,frames){
   const grid=el('div',null,{class:'safety-frames'});
   const sequence=isRapidMovementReview(event)&&frames.length===4;
   if(sequence)grid.dataset.sequence='rapid';
   for(const frame of frames){
     const time=frameTime(frame);
     const phase=sequence?['动作前','动作前','动作中','动作后段'][frame.ordinal]:null;
     const caption=phase?phase+' · '+(frame.at-frames[0].at).toFixed(2)+' 秒':time;
     const playable=frame.recording_anchor===true&&Number.isSafeInteger(frame.ordinal)&&frame.ordinal>=0;
     const image=el('img',null,{src:base+'/events/'+event.id+'/frames/'+frame.ordinal,alt:playable?'':(phase||time)+'的关键帧',loading:'lazy'});
     if(playable){
       const play=button('',()=>void openRecordings(event,frame));
       play.className='safety-frame-play';
       play.setAttribute('aria-label','回看 '+(phase?caption+'，':'')+time+' 附近录像');
       play.setAttribute('aria-pressed',String(selectedFrame===String(frame.ordinal)));
       play.setAttribute('data-recording-frame',String(frame.ordinal));
       play.append(image,add(el('span',null,{class:'safety-frame-caption'}),el('span',caption),el('span','回看')));
       frameButtons.set(String(frame.ordinal),play);
       grid.append(add(el('figure'),play));
     }else grid.append(add(el('figure'),image,el('figcaption',caption)));
   }
   return grid;
 }
 function soundFrameGroup(title,event,frames){
   return add(el('section',null,{class:'safety-sound-evidence'}),el('h3',title),frameGrid(event,frames));
 }
 function show(event,{focus=false}={}){
   const fingerprint=JSON.stringify(event);
   if(selected===event.id&&lastDetail===fingerprint){if(focus)focusDetail(detailBody.querySelector('h2'));return;}
   lastDetail=fingerprint;
   const focusedReview=selected===event.id&&document.activeElement&&detailBody.contains(document.activeElement)?document.activeElement.getAttribute('data-review'):null;
   const focusedRecording=selected===event.id&&document.activeElement===detailBody.querySelector('.primary');
   const focusedFrame=selected===event.id&&detailBody.contains(document.activeElement)?document.activeElement.getAttribute('data-recording-frame'):null;
   const extraOpen=selected===event.id&&detailBody.querySelector('.safety-extra-frames')?.open;
   if(selected!==event.id){selectionVersion++;stopRecording();recordings.replaceChildren();}
   selected=event.id;detail.hidden=false;frameButtons.clear();
   const heading=el('h2',eventTitle(event),{tabindex:'-1'});
   const rapid=isRapidMovementReview(event),review=reviewNames[event.review]||'待查看';
   const sustained=event.kind==='sound_review'&&event.sound_kind==='crying'&&event.sound_evidence?.pattern==='sustained_infant_cry';
   const source=event.camera?(event.camera_name||'摄像头记录')+' · '+new Date(eventTime(event)*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}):'';
   const searchOrdinals=new Set(event.kind==='sound_review'&&Array.isArray(event.sound_search_views)?
    event.sound_search_views.map(view=>view?.ordinal):[]);
   const searchFrames=event.frames.filter(frame=>searchOrdinals.has(frame.ordinal)).sort((a,b)=>a.at-b.at);
   const eventFrames=event.frames.filter(frame=>!searchOrdinals.has(frame.ordinal));
   const compared=event.kind==='visual_review'&&eventFrames.length===8&&
     ['two_disjoint_local_windows','same_track_two_local_windows'].includes(event.basis);
   const compact=rapid||sustained||compared||searchFrames.length>0;
   detailBody.replaceChildren(heading);
   if(compact)detailBody.append(el('p',[source,review].filter(Boolean).join(' · '),{class:'caption'}));
   else detailBody.append(el('p',event.message),el('p',review));
   if(sustained||compared)detailBody.append(el('p',event.message));
   for(const[id,row]of eventRows)row.setAttribute('aria-current',String(id===event.id));
   if(event.kind==='fall'||isOutputTest(event))detailBody.append(el('p',audioNames[event.announcement.status]||'播报状态未知',{role:'status'}));
   if(event.observation&&!rapid)detailBody.append(el('h3','模型观察 · 待你核对'),el('p',event.observation));
   if(source&&!compact)detailBody.append(el('p',source,{class:'caption'}));
   const actions=el('div',null,{class:'safety-actions'});
   let restoreFocus;
   if(!event.simulated&&event.camera){const recording=button('查看附近录像',()=>void openRecordings(event));recording.className='primary';actions.append(recording);if(focusedRecording)restoreFocus=recording;}
   for(const[value,label]of [['acknowledged',event.review==='acknowledged'?'已查看':'我已查看'],['false_alarm','标记误报']]){const action=button(label,async()=>{try{await api('/events/'+event.id+'/review',{review:value});await refreshHistory(true);}catch{if(alive){historyStatus.hidden=false;historyStatus.textContent='标记未保存，请重试。';}}});action.setAttribute('data-review',value);action.setAttribute('aria-pressed',String(event.review===value));if(value===focusedReview)restoreFocus=action;actions.append(action);}
   if(!rapid&&!compared)detailBody.append(actions);
   if(searchFrames.length||sustained&&eventFrames.length){
     if(eventFrames.length)detailBody.append(soundFrameGroup(sustained?'哭声时的画面':'异响时的画面',event,eventFrames.sort((a,b)=>a.at-b.at)));
     if(searchFrames.length)detailBody.append(soundFrameGroup('环视画面',event,searchFrames));
   }else if(eventFrames.length){
     if(compared){
       const comparison=el('div',null,{class:'safety-review-windows'});
       const first=eventFrames[0],second=eventFrames[eventFrames.length/2];
       for(const [label,frame] of [['首次观察',first],['再次观察',second]]){
         comparison.append(add(el('section'),el('h3',label),frameGrid(event,[frame])));
       }
       detailBody.append(comparison);
       const extra=add(el('details',null,{class:'safety-extra-frames'}),el('summary','查看其余关键帧'),
         frameGrid(event,eventFrames.filter(frame=>frame!==first&&frame!==second)));
       extra.open=Boolean(extraOpen);detailBody.append(extra);
     }else detailBody.append(el('h3','关键帧'),frameGrid(event,eventFrames));
   }else detailBody.append(el('p',isOutputTest(event)?'播报测试不采集摄像头画面。':'没有可用关键帧。',{class:'caption'}));
   if(rapid||compared)detailBody.append(actions);
   const exportEvent=button('导出事件记录',()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(event,null,2)],{type:'application/json'}));const a=el('a',null,{href:url,download:'Friday-safety-'+event.id+'.json'});a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});exportEvent.className='text-button safety-export';detailFooter.replaceChildren(exportEvent);
   if(focus)focusDetail(heading);else (restoreFocus||frameButtons.get(focusedFrame))?.focus({preventScroll:true});
 }
 async function openRecordings(event,frame=null){
  if(!alive||selected!==event.id)return;
  const token=++selectionVersion,owns=()=>alive&&selected===event.id&&token===selectionVersion;
  const moment=frame?frameTime(frame):null;
  stopRecording();selectRecordingFrame(frame?.ordinal);
  recordings.setAttribute('aria-label',moment?moment+' 附近录像':'事件附近录像');
  recordings.replaceChildren(el('p',moment?'正在查找 '+moment+' 附近的录像…':'正在查找对应录像…',{role:'status'}));
  recordings.focus({preventScroll:true});recordings.scrollIntoView?.({block:'nearest',behavior:'instant'});
  const unavailable=()=>{
   if(owns())recordings.replaceChildren(el('p','暂时无法读取录像。',{role:'status'}),button('重试回看',()=>void openRecordings(event,frame)));
  };
  const play=async item=>{if(owns())try{await onOpenRecording(recordings,item);}catch{unavailable();}};
  try{
   const found=await api('/events/'+event.id+'/recordings'+(frame?'?ordinal='+frame.ordinal:''));if(!owns())return;
   if(found.event_id!==event.id||frame&&found.frame_ordinal!==frame.ordinal)throw Error('unavailable');
   if(found.recordings?.length===1){await play(found.recordings[0]);return;}
   recordings.replaceChildren();
   if(found.recordings?.length){recordings.append(el('h3','选择录像来源'));for(const item of found.recordings)recordings.append(button(item.camera_name,()=>void play(item)));return;}
   const reasons={event_source_unverified:'这条记录缺少可核对的镜头来源，可查看已保存的关键帧。',event_time_unavailable:'这条记录的画面时间不可用，可查看已保存的关键帧。',event_recording_not_applicable:'这条记录不关联摄像头录像。',event_frame_unavailable:'这张画面暂时无法关联录像。'};
   recordings.append(el('p',reasons[found.code]||'先在录像设置中对应摄像头并核对时间。',{role:'status'}));
   if(!reasons[found.code])recordings.append(button('录像设置',onArchiveSettings));
  }catch{unavailable();}
 }
 function focusReview(review){
  const title=review==='visual'?visualTitle:review==='sound'?soundTitle:null;
  if(!alive||!title)return;
  requested=null;
  settings.open=true;
  title.focus();
 }
 if(initialReview&&!requested)focusReview(initialReview);
 availability();refresh();timer=setInterval(()=>{if(!busy)refresh();},5000);
 return{selectEvent,focusReview,dispose(){alive=false;retireRead(statusFeed);retireRead(historyFeed);selectionVersion++;clearInterval(timer);clearTimeout(retryTimer);retryController?.abort();stopRecording();root.remove();}};
}
