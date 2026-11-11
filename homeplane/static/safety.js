/* Local safety review. Untrusted names and observations are always text nodes. */
import {soundReviewNames as soundNames,isSafetyOutputTest as isOutputTest,safetyEventTitle as eventTitle} from './safety-event-labels.js';
export function mountSafety(container,{go=()=>{},onOpenRecording=()=>{},onArchiveSettings=()=>go('Settings'),initialEventId=null,onEventSelected=()=>{}}={}){
 const base='/api/embodied/safety';let alive=true,report=null,busy=false,version=0,lastEvents='',selected=null,selectionVersion=0,timer,events=[],historyLoaded=false,retrying=false,retryController=null,retryTimer=null;
 const validEventId=id=>typeof id==='string'&&/^[a-f0-9]{32}$/.test(id)?id:null;let requested=validEventId(initialEventId);
 const eventTime=event=>Number.isFinite(event.captured_at)?event.captured_at:event.at;
 const el=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
 const add=(n,...children)=>{n.append(...children);return n;};
 const button=(text,run)=>{const n=el('button',text,{type:'button'});n.onclick=run;return n;};
 const api=async(path,body,options={})=>{const r=await fetch(base+path,{headers:{'Content-Type':'application/json'},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)}),...options});if(!r.ok)throw Error('unavailable');return r.json();};
 const root=el('div',null,{class:'safety-page','data-detail-open':'false'});container.append(root);
 if(!document.getElementById('safety-style'))document.head.append(el('link',null,{id:'safety-style',rel:'stylesheet',href:'/static/safety.css'}));
 const status=el('p','正在读取监测状态…',{role:'status','aria-live':'polite',tabindex:'-1'}),enable=button('开启监测',()=>change('enabled')),announce=button('开启语音提醒',()=>change('announce'));
 const test=button('试听跌倒提醒',async()=>{if(busy)return;busy=true;test.disabled=true;status.textContent='正在发送测试提醒…';try{await api('/test',{});await refresh();}catch{status.textContent='试听未发送，请检查播报音箱连接。';}finally{busy=false;test.disabled=false;}});
 const intro=add(el('section',null,{class:'safety-intro'}),el('h2','家中提醒'),el('p','发现疑似跌倒时记录事件，按需通过选定音箱提醒家人。'),status,
   add(el('div',null,{class:'safety-actions'}),enable,announce,test,button('选择播报音箱',()=>go('Settings'))),
   el('p','此功能仍在试用，可能漏报或误报。请保持日常照护。事件画面仅保存在本机，保留 30 天。',{class:'caption'}));
 const visualStatus=el('p','正在检查本机画面复核…',{role:'status','aria-live':'polite'});
 const visualToggle=button('本机画面复核',()=>change('visual_review'));visualToggle.setAttribute('role','switch');visualToggle.setAttribute('aria-checked','false');visualToggle.disabled=true;
 const visual=add(el('section',null,{class:'safety-visual'}),el('h2','本机画面复核'),el('p','补充查看面部遮挡、快速搬动等可见变化。两段画面一致后保存待复核记录，由你查看，不自动播报。'),visualStatus,
   add(el('div',null,{class:'safety-actions'}),visualToggle),el('p','画面在本机处理。资源不足时暂缓复核，隐私暂停会清空待处理画面。短暂动作仍可能漏过。',{class:'caption'}));
 const soundStatus=el('p','正在读取声音复核状态…',{role:'status','aria-live':'polite'});
 const soundToggle=button('开启声音复核',()=>change('sound_review'));soundToggle.setAttribute('role','switch');soundToggle.setAttribute('aria-checked','false');soundToggle.disabled=true;
 const sound=add(el('section',null,{class:'safety-sound'}),el('h2','声音复核'),el('p','留意疑似哭声、尖叫、破碎声和警报，记录后由你查看。'),soundStatus,
   add(el('div',null,{class:'safety-actions'}),soundToggle),el('p','声音仅在本机内存处理。暂停声音输入会同时暂停复核。',{class:'caption'}));
 const capabilities=el('section',null,{class:'safety-capabilities','aria-label':'监测能力'}),list=el('section',null,{class:'safety-events','aria-label':'预警记录'}),detail=el('section',null,{class:'safety-detail','aria-label':'事件详情'});
 const detailBody=el('div'),recordings=el('div',null,{class:'safety-recording','aria-label':'事件附近录像'}),back=button('全部提醒',backToList);back.className='safety-back';detail.append(back,detailBody,recordings);detail.hidden=true;
 const stopRecording=()=>{try{const pending=onOpenRecording(recordings,null);pending?.catch?.(()=>{});}catch{recordings.replaceChildren();}};
 const limits=add(el('details',null,{class:'safety-limits'}),el('summary','监测范围与限制'),capabilities);
 const historyView=el('div',null,{class:'safety-history'}),settings=add(el('details',null,{class:'safety-settings'}),el('summary','监测设置'),intro,visual,sound,limits);historyView.append(add(el('div',null,{class:'safety-list-panel'}),add(el('div',null,{class:'safety-actions'}),el('h2','近期提醒'),button('刷新记录',()=>refresh())),list),detail);
 const reconnect=button('重试连接',()=>retryConnection()),cameraCheck=button('查看摄像头',()=>go('Cameras')),recoveryActions=add(el('div',null,{class:'safety-actions',role:'group','aria-label':'监测连接恢复'}),reconnect,cameraCheck);recoveryActions.hidden=true;root.append(status,recoveryActions,historyView,settings);
 const reviewNames={unreviewed:'待查看',acknowledged:'已查看',false_alarm:'已标记误报'};
 const eventRows=new Map();
 const audioNames={not_requested:'未请求播报',requesting:'正在发送播报',accepted:'音箱已接受，实际声音待确认',unconfirmed:'播报结果待确认',paused:'安静模式，未播报',busy:'音箱忙，未播报',unavailable:'播报不可用',cancelled:'已取消播报',interrupted:'播报已中断'};
 function availability(){enable.disabled=announce.disabled=busy;visualToggle.disabled=busy||(!report?.policy?.visual_review&&(!report?.policy?.enabled||!report?.visual_review?.available));soundToggle.disabled=busy||(!report?.policy?.sound_review&&(!report?.policy?.enabled||!report?.sound_review?.available));reconnect.disabled=busy||retrying||!report?.supervision?.recovery?.can_retry;}
 async function retryConnection(){
  const revision=report?.supervision?.policy?.revision;
  if(!alive||busy||retrying||!report?.supervision?.recovery?.can_retry||!Number.isInteger(revision))return;
  const focused=recoveryActions.contains(document.activeElement);retrying=true;availability();recoveryActions.setAttribute('aria-busy','true');status.textContent='正在重试连接…';if(focused)status.focus({preventScroll:true});
  const controller=typeof AbortController==='function'?new AbortController():null,timeout=controller?setTimeout(()=>controller.abort(),5000):null;retryController=controller;retryTimer=timeout;
  try{await api('/retry',{expected_revision:revision},controller?{signal:controller.signal}:{});if(alive)await refresh();}
  catch{if(alive){status.textContent='暂未确认连接结果，正在读取状态…';await refresh();}}
  finally{clearTimeout(timeout);retryTimer=null;retryController=null;retrying=false;if(alive){recoveryActions.setAttribute('aria-busy','false');availability();}}
 }
 async function change(key){if(busy||!report)return;busy=true;availability();try{await api('/policy',{...report.policy,[key]:!report.policy[key]});await refresh();}catch{if(alive)status.textContent='设置未保存，请刷新后重试。';}finally{busy=false;if(alive)availability();}}
 async function refresh(){if(!alive)return;const epoch=++version;try{const [next,history]=await Promise.all([api(''),api('/events')]);if(!alive||epoch!==version)return;report=next;
   enable.textContent=next.policy.enabled?'暂停监测':'开启监测';enable.setAttribute('role','switch');enable.setAttribute('aria-checked',String(next.policy.enabled));
   announce.textContent=next.policy.announce?'关闭语音提醒':'开启语音提醒';announce.setAttribute('role','switch');announce.setAttribute('aria-checked',String(next.policy.announce));
   const model=next.visual_review||{};visualToggle.setAttribute('aria-checked',String(Boolean(next.policy.visual_review)));visualToggle.disabled=busy||(!next.policy.visual_review&&(!next.policy.enabled||!model.available));
   const modelReasons={model_not_installed:'本机模型尚未安装，完成本地模型配置后可开启。',model_pin_mismatch:'本机模型需要核对，重新检查模型文件后重试。',local_runtime_missing:'本机运行环境尚未就绪，完成本地模型配置后重试。',apple_silicon_required:'当前主机不支持这套本地模型。',memory_pressure:'本机内存紧张，画面复核已暂缓。',memory_capacity:'本机可用内存容量不足，画面复核已暂缓。',memory_unknown:'暂时无法检查本机内存，稍后自动重试。',cpu_busy:'本机正在忙，稍后继续复核。',model_busy:'本机模型正在处理其他请求，稍后重试。',visual_review_deferred:'画面复核暂不可用，请检查本机模型后重试。'};
   visualStatus.textContent=!model.available?(modelReasons[model.reason]||'本机模型尚未就绪，完成配置后可开启。'):!next.policy.enabled?'先开启监测，再选择是否使用画面复核。':!next.policy.visual_review?'画面复核已关闭，仅使用跌倒时序检测。':model.error?(modelReasons[model.error]||'画面复核已暂缓，稍后重试。'):model.state==='preparing'?'正在准备画面复核':model.state==='reviewing'?'正在复核画面':'画面复核已开启，等待连续、清晰的画面。';
   const soundModel=next.sound_review||{};soundToggle.textContent=next.policy.sound_review?'关闭声音复核':'开启声音复核';soundToggle.setAttribute('aria-checked',String(Boolean(next.policy.sound_review)));availability();
   const supported=(soundModel.supported_types||[]).filter(kind=>soundNames[kind]).map(kind=>soundNames[kind]);
   soundStatus.textContent=!soundModel.available?'当前版本尚未提供本机声音复核。':!next.policy.enabled?'开启监测后可使用声音复核。':!next.policy.sound_review?'声音复核已关闭。':soundModel.input_paused?'声音输入已暂停。':soundModel.error?({'sound_memory_pressure':'本机内存紧张，稍后自动重试。','sound_cpu_busy':'本机正忙，稍后自动重试。','sound_labels_unavailable':'本机分类器尚未提供这些声音类别。'}[soundModel.error]||'声音复核暂缓，稍后自动重试。'):supported.length?'已开启 · '+supported.join('、'):'已开启，等待摄像头声音。';
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
   const fingerprint=JSON.stringify(history.events);historyLoaded=true;events=history.events;historyView.dataset.empty=String(!events.length);
   if(fingerprint!==lastEvents){lastEvents=fingerprint;list.replaceChildren();eventRows.clear();
     if(!events.length)list.append(el('p','暂无提醒记录',{class:'product-empty'}));
     for(const e of events){const row=button('',()=>choose(e.id));row.className='safety-event';row.setAttribute('aria-current',String(e.id===selected));eventRows.set(e.id,row);row.append(el('strong',eventTitle(e)),el('span',(e.camera_name?e.camera_name+' · ':'')+new Date(eventTime(e)*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})),el('span',reviewNames[e.review]||'待查看'));list.append(row);}
     if(requested){const id=requested;requested=null;selectEvent(id);}
     else if(selected){const current=events.find(e=>e.id===selected);if(current)show(current);else missingEvent(selected);}
     else if(events.length)show(events.find(e=>e.review==='unreviewed')||events[0]);
   }
 }catch{if(alive&&epoch===version)status.textContent='预警服务暂时不可用，请刷新重试。';}}
 function backToList(){selectionVersion++;stopRecording();recordings.replaceChildren();root.dataset.detailOpen='false';if(!events.length){selected=null;detail.hidden=true;}onEventSelected(null);eventRows.get(selected)?.focus?.({preventScroll:true});}
 function selectEvent(id){if(!alive||!validEventId(id))return;requested=id;if(!historyLoaded)return;requested=null;const event=events.find(e=>e.id===id);if(event)show(event,{focus:true});else missingEvent(id,{focus:true});}
 function choose(id){selectEvent(id);onEventSelected(id);}
 function focusDetail(heading){root.dataset.detailOpen='true';heading.focus?.({preventScroll:true});detail.scrollIntoView?.({block:'start',behavior:'instant'});}
 function missingEvent(id,{focus=false}={}){selected=id;selectionVersion++;stopRecording();recordings.replaceChildren();detail.hidden=false;for(const row of eventRows.values())row.setAttribute('aria-current','false');const heading=el('h2','这条提醒已不在记录中',{tabindex:'-1'});detailBody.replaceChildren(heading,button('查看近期提醒',()=>{const current=events.find(e=>e.review==='unreviewed')||events[0];if(current)choose(current.id);else backToList();}));if(focus)focusDetail(heading);}
 function show(event,{focus=false}={}){const focusedReview=selected===event.id&&document.activeElement&&detailBody.contains(document.activeElement)?document.activeElement.getAttribute('data-review'):null;
   if(selected!==event.id){selectionVersion++;stopRecording();recordings.replaceChildren();}selected=event.id;detail.hidden=false;const heading=el('h2',eventTitle(event),{tabindex:'-1'});detailBody.replaceChildren(heading,el('p',event.message),el('p',reviewNames[event.review]||'待查看'));
   for(const[id,row]of eventRows)row.setAttribute('aria-current',String(id===event.id));
   if(event.kind!=='sound_review')detailBody.append(el('p',audioNames[event.announcement.status]||'播报状态未知',{role:'status'}));
   if(event.observation)detailBody.append(el('h3','模型观察 · 待你核对'),el('p',event.observation));
   if(event.camera)detailBody.append(el('p',(event.camera_name||'摄像头记录')+' · '+new Date(eventTime(event)*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}),{class:'caption'}));
   const actions=el('div',null,{class:'safety-actions'});
   if(!event.simulated&&event.camera){const recording=button('查看附近录像',()=>void openRecordings(event));recording.className='primary';actions.append(recording);}
   let restoreFocus;
   for(const[value,label]of [['acknowledged',event.review==='acknowledged'?'已查看':'我已查看'],['false_alarm','标记误报']]){const action=button(label,async()=>{try{await api('/events/'+event.id+'/review',{review:value});await refresh();}catch{status.textContent='标记未保存，请重试。';}});action.setAttribute('data-review',value);action.setAttribute('aria-pressed',String(event.review===value));if(value===focusedReview)restoreFocus=action;actions.append(action);}
   detailBody.append(actions);
   if(event.frames.length){detailBody.append(el('h3','关键帧'));const frames=el('div',null,{class:'safety-frames'});for(const f of event.frames)frames.append(add(el('figure'),el('img',null,{src:base+'/events/'+event.id+'/frames/'+f.ordinal,alt:'事件关键帧 '+f.ordinal,loading:'lazy'}),el('figcaption',new Date(f.at*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai'}))));detailBody.append(frames);}else detailBody.append(el('p',isOutputTest(event)?'播报测试不采集摄像头画面。':'没有可用关键帧。',{class:'caption'}));
   const exportEvent=button('导出事件记录',()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(event,null,2)],{type:'application/json'}));const a=el('a',null,{href:url,download:'Friday-safety-'+event.id+'.json'});a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});exportEvent.className='text-button safety-export';detailBody.append(exportEvent);
   if(focus)focusDetail(heading);else restoreFocus?.focus({preventScroll:true});
 }
 async function openRecordings(event){
  const token=++selectionVersion;stopRecording();recordings.replaceChildren(el('p','正在查找对应录像…',{role:'status','aria-live':'polite'}));
  try{
   const found=await api('/events/'+event.id+'/recordings');if(!alive||selected!==event.id||token!==selectionVersion)return;
   if(found.recordings?.length===1){await onOpenRecording(recordings,found.recordings[0]);return;}
   recordings.replaceChildren();
   if(found.recordings?.length){recordings.append(el('h3','选择录像来源'));for(const item of found.recordings)recordings.append(button(item.camera_name,()=>onOpenRecording(recordings,item)));return;}
   const reasons={event_source_unverified:'这条记录缺少可核对的镜头来源，可查看已保存的关键帧。',event_time_unavailable:'这条记录的画面时间不可用，可查看已保存的关键帧。',event_recording_not_applicable:'这条记录不关联摄像头录像。'};
   recordings.append(el('p',reasons[found.code]||'先在录像设置中对应摄像头并核对时间。',{role:'status'}));
   if(!reasons[found.code])recordings.append(button('录像设置',onArchiveSettings));
  }catch{if(alive&&selected===event.id&&token===selectionVersion)recordings.replaceChildren(el('p','录像暂不可读取，请刷新记录后重试。',{role:'status'}));}
 }
 void refresh();timer=setInterval(()=>{if(!busy)void refresh();},5000);
 return{selectEvent,dispose(){alive=false;version++;selectionVersion++;clearInterval(timer);clearTimeout(retryTimer);retryController?.abort();stopRecording();root.remove();}};
}
