import {arlRuntimePresentation} from './arl-status.js';
import {isPendingFall,isPendingSafetyEvent,safetyEventTitle} from './safety-event-labels.js';
const h=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=String(text);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
const add=(n,...children)=>{n.append(...children);return n;};
const button=(text,run,attrs={})=>{const n=h('button',text,{type:'button',...attrs});n.onclick=run;return n;};
const icon=name=>h('span',null,{class:'icon icon-'+name,'aria-hidden':'true'});
const service=(label,state,text)=>({label,state,text});
const reviews={visual:'画面复核',sound:'声音复核'};

function reviewServices(worker){
 if(!worker?.owned||worker.capture_enabled===false||worker.state==='unavailable')return [];
 return Object.entries(reviews).flatMap(([review,label])=>{
  const value=worker.safety_review?.[review];
  const states={
   waiting_input:['waiting',review==='visual'?'等待画面':'等待声音'],
   waiting:['waiting','等待复核'],running:['on','运行中'],paused:['off','已暂停'],
   delayed:['degraded','复核延迟'],resource_limited:['degraded','资源不足'],
   recovering:['degraded','正在恢复'],unavailable:['warning','暂不可用'],
  };
  return Object.hasOwn(states,value)?[{...service(label,...states[value]),review}]:[];
 });
}

function perceptionService(worker) {
 const row=(state,text)=>service('家庭感知',state,text);
 if(!worker)return row('unknown','未更新');
 if(!worker.configured)return row('off','未启用');
 if(worker.capture_enabled===false)return row('off','已暂停');
 if(worker.recovery_pending)return row('warning','需要检查');
 const unavailable=['unavailable','failed'].includes(worker.state);
 if(!worker.owned)return unavailable?row('degraded','暂不可用'):row('waiting','待命');
 const sessions=(Array.isArray(worker.camera_sessions)?worker.camera_sessions:[])
  .filter(session=>typeof session?.camera==='string'&&session.camera);
 if(!sessions.length)return unavailable?row('degraded','暂不可用'):row('unknown','未更新');
 const open=sessions.filter(session=>session.state!=='shutter_closed');
 if(!open.length)return row('off','镜头已关闭');

 const recognizing=session=>session.state==='live'&&session.recognition==='running';
 const active=open.filter(recognizing);
 const waiting=open.filter(session=>!recognizing(session));
 const interrupted=waiting.find(session=>['stale','unavailable','failed'].includes(session.state));
 const recovering=waiting.find(session=>session.recognition==='recovering');
 const measured=active.filter(session=>Number.isFinite(session.latency_ms)&&session.latency_ms>=0);
 const slowest=measured.reduce((latest,session)=>!latest||session.latency_ms>latest.latency_ms?session:latest,null);
 const slow=slowest?.latency_ms>1000;
 const source=interrupted||recovering||waiting[0]||(slow?slowest:null);
 const sourceText=interrupted?'画面中断':recovering?'感知恢复中':waiting.length?({
  connecting:'正在连接',stopped:'待命',live:'等待画面',
 }[source.state]||'状态未更新'):'处理较慢';

 let summary;
 if(active.length) {
  const count=waiting.length?active.length+' / '+open.length:active.length;
  summary=row(interrupted||recovering||slow?'degraded':waiting.length?'waiting':'on',count+' 台感知中');
 }else{
  summary=interrupted?row('degraded','暂不可用'):recovering?row('degraded','正在恢复'):row('waiting','等待画面');
 }
 return {...summary,source:source?{camera:source.camera,text:sourceText}:null,
  latency:slowest?{ms:slowest.latency_ms,slow,multiple:measured.length>1}:null};
}

function voiceService(worker){
 if(!worker)return service('语音交互','unknown','未更新');
 if(!worker.configured)return service('语音交互','off','未启用');
 if(worker.voice_input===false||worker.interaction_enabled===false||worker.capture_enabled===false)return service('语音交互','off','已暂停');
 if(!worker.owned)return service('语音交互','waiting','待命');
 const sources=(Array.isArray(worker.voice_sessions)?worker.voice_sessions:worker.home?[worker.home]:[]).filter(s=>s?.enabled===true);
 if(!sources.length)return service('语音交互','waiting','待命');
 const failed=s=>s.voice==='unavailable'||s.audio_input?.status==='unavailable'||s.audio_analysis?.status==='unavailable';
 const ready=s=>['ready','transcribing'].includes(s.voice)&&!failed(s);
 const healthy=sources.filter(ready).length;
 if(sources.some(failed))return service('语音交互','warning',healthy?'部分可用':'暂不可用');
 if(healthy===sources.length)return service('语音交互','on',sources.some(s=>s.voice==='transcribing')?'处理中':'已就绪');
 if(healthy)return service('语音交互','waiting','部分就绪');
 if(sources.some(s=>s.voice==='loading_local_models'))return service('语音交互','waiting','正在准备');
 if(sources.every(s=>s.voice==='paused'))return service('语音交互','off','已暂停');
 if(sources.some(s=>!['not_started','stopped','paused'].includes(s.voice)))return service('语音交互','unknown','未更新');
 return service('语音交互','waiting','待命');
}
export function homeServiceSummary({worker=null,infrastructure=null,rules=null,arl=null,simulated=false}={}){
 const rows=[perceptionService(worker),voiceService(worker)];
 const automatic=rules?.execution_state,clockRecovering=rules?.clock?.scheduled_count>0&&rules.clock.state==='recovering';
 rows.push(service('自动任务',!rules?'unknown':rules.worker_failure?'warning':!rules.available?'waiting':rules.automatic_enabled===false?'off':automatic==='ready'&&!clockRecovering?'on':'waiting',!rules?'未更新':rules.worker_failure?'需要检查':!rules.available?'未就绪':rules.automatic_enabled===false?'已暂停':clockRecovering?'定时任务恢复中':({ready:'运行中',manual:'手动控制中',sleeping:'睡眠模式',starting:'正在启动',awaiting_mode:'等待家庭状态'}[automatic]||'等待家庭状态')));
 if(worker?.configured){const recording=worker.safety_recording,state=recording?.state;rows.push(service('异常记录',worker.capture_enabled===false?'off':!worker.owned?'waiting':state==='unavailable'?'warning':state==='off'?'off':state==='ready'?'on':['saving','recovering'].includes(state)?'waiting':'unknown',worker.capture_enabled===false?'已暂停':!worker.owned?'待命':({unavailable:'需要检查',off:'已暂停',ready:'已就绪',saving:'正在保存',recovering:'正在恢复'}[state]||'未更新')));}
 const agent=arl?arlRuntimePresentation(arl):null;
 rows.push(service('智能服务',!agent?'unknown':agent.connected?'on':['disabled','sdk_missing','not_running'].includes(agent.state)?'off':['disconnected','unsafe_configuration'].includes(agent.state)?'warning':'waiting',!agent?'未更新':agent.connected?'已连接':['disabled','sdk_missing','not_running'].includes(agent.state)?'未启用':({registering:'正在连接',handshake_required:'尚未连接',upgrade_required:'需要更新'}[agent.state]||agent.label)));
 rows.push(...reviewServices(worker));
 const issue=infrastructure?.failure||rows.some(r=>r.state==='warning');
 const degraded=rows.some(r=>r.state==='degraded');
 const unknown=rows.some(r=>r.state==='unknown');
 return {rows,label:simulated?'示例运行状态':issue?'有服务需要检查':degraded?'部分服务受影响':unknown?'部分状态未更新':rows.some(r=>r.state==='on')?'按配置运行中':'按配置待命',state:issue||degraded?'warning':unknown?'unknown':'on'};
}
export function homeAttention(devices,events=[],runs=[],recording=null,proposals=[],taskAttention=null){
 const items=[];
 const pending=events.filter(isPendingSafetyEvent)
  .sort((a,b)=>Number(isPendingFall(b))-Number(isPendingFall(a)));
 for(const event of pending.slice(0,3)){
  items.push({key:'safety:'+event.id,eventId:event.id,title:safetyEventTitle(event,{includePerson:false}),
   detail:event.camera_name||'查看事件与关键画面',priority:isPendingFall(event)?0:1,page:'Safety'});
 }
 if(recording?.state==='unavailable')items.push({key:'safety-recording',title:recording.dropped>0?'有提醒未能保存':recording.unconfirmed>0?'有提醒的保存结果待核对':recording.partial>0?'有提醒画面未完整保存':'提醒记录暂不可用',detail:recording.unconfirmed>0?'查看记录与本机存储':recording.partial>0?'查看已保存的记录':'检查本机存储',priority:0,page:'Safety'});
 for(const proposal of proposals.filter(p=>typeof p.id==='string'&&/^[a-f0-9]{64}$/.test(p.id)&&Date.parse(p.expires_at)>Date.now()).slice(0,8))items.push({key:'proposal:'+proposal.id,proposalRef:proposal.id,title:'有一项语音任务待确认',detail:proposal.task_review?.title||'核对任务后启用',priority:1,page:'Ask Home'});
 for(const d of devices){
  if(d.available===false){items.push({key:'device:'+d.id,title:d.name+'暂时不可用',detail:d.room,priority:2,device:d});continue;}
  const battery=(d.readings||[]).find(r=>r.available!==false&&r.value!=null&&r.device_class==='battery'&&Number.isFinite(Number(r.value))&&Number(r.value)<=20);
  if(battery)items.push({key:'battery:'+d.id,title:d.name+'电量偏低',detail:String(battery.value)+(battery.unit||'%')+' · '+d.room,priority:1,device:d});
 }
 if(taskAttention?.available===true&&Array.isArray(taskAttention.items)){
  for(const item of taskAttention.items.filter(item=>/^[a-f0-9]{64}$/.test(item.id)&&['missed','not_executed','unconfirmed'].includes(item.kind)))items.push({key:'task:'+item.id,attentionId:item.id,title:item.title||'自动任务',detail:({missed:'错过执行时间 · 重新安排',not_executed:'本次未执行 · 查看安排',unconfirmed:'执行结果待核对 · 查看详情'})[item.kind],priority:2,page:'Automations'});
 }else for(const run of runs.filter(r=>['failed','unknown_outcome'].includes(r.status)).slice(0,2))items.push({key:'task:'+run.id,title:run.status==='failed'?'有任务未能完成':'有任务结果需要核对',detail:'查看任务记录',priority:2,page:'Automations'});
 return items.sort((a,b)=>a.priority-b.priority);
}
export function mountHomeStatus(root,{api,go,devices=[],simulated=false,onDevice=()=>go('Devices'),onCamera=()=>go('Cameras')}={}){
 let alive=true,busy=false,controller=null,timer=null,expiry=null,currentDevices=devices;
 const section=h('section',null,{class:'home-runtime','aria-label':'Friday 运行状态'}),status=h('span','正在读取',{class:'runtime-overall',role:'status'}),list=h('dl',null,{class:'runtime-services'}),resources=h('div',null,{class:'runtime-resources'});
 const heading=h('h2','Friday',{tabindex:'-1'});
 section.append(add(h('div',null,{class:'home-aside-heading'}),heading,button(null,()=>go('Settings'),{'aria-label':'Friday 设置',class:'quiet-icon'})),status,list,resources);
 section.querySelector('button').append(icon('settings'));
 const perceptionTitle=h('dt','家庭感知',{tabindex:'-1'}),perceptionValue=h('dd');
 const sourceName=h('span',null,{class:'runtime-camera-name'}),sourceState=h('small');
 let cameraId=null;
 const sourceButton=add(button(null,()=>{if(cameraId)onCamera(cameraId);},{class:'runtime-camera-open'}),
  icon('video'),sourceName,sourceState,icon('chevron-right'));
 const sourceDetail=add(h('dd',null,{class:'runtime-camera-detail'}),sourceButton);
 const perception=add(h('div',null,{class:'runtime-perception'}),perceptionTitle,perceptionValue,sourceDetail);
 list.append(perception);
 const serviceRows=new Map();
 for(const [label,review] of [['语音交互'],['自动任务'],['异常记录'],['智能服务'],
    ...Object.entries(reviews).map(([review,label])=>[label,review])]){
  const value=h(review?'span':'dd');
  const control=review?add(button(null,()=>go('Safety',{review}),{class:'runtime-review-open'}),value,icon('chevron-right')):null;
  const group=add(h('div',null,review?{class:'runtime-review'}:{}),h('dt',label),control?add(h('dd'),control):value);
  serviceRows.set(label,{group,value,control});
  list.append(group);
 }
 const attention=h('section',null,{class:'home-attention','aria-label':'需要关注'}),count=h('span','',{class:'attention-count'}),items=h('div',null,{class:'home-attention-items'});
 const attentionHeading=h('h2','需要关注',{tabindex:'-1'});
 const allAlerts=button(null,()=>go('Safety'),{class:'home-alerts-link text-button',hidden:''});
 allAlerts.append(h('span'),icon('chevron-right'));
 const expand=button(null,()=>{
  attention.dataset.expanded=String(attention.dataset.expanded!=='true');
  updateExpansion();
 },{class:'home-attention-expand text-button','aria-expanded':'false',hidden:''});
 const attentionRows=new Map();
 const empty=h('div',null,{class:'home-attention-empty',role:'status',hidden:''});
 let visibleCount=0;
 function updateExpansion(){
  const expanded=attention.dataset.expanded==='true';
  const label=expanded?'收起提醒':'展开 '+visibleCount+' 项提醒';
  if(expand.textContent!==label)expand.textContent=label;
  expand.setAttribute('aria-expanded',String(expanded));
 }
 attention.append(add(h('div',null,{class:'home-aside-heading'}),attentionHeading,count),empty,items,
  add(h('div',null,{class:'home-attention-actions'}),expand,allAlerts));
 root.append(section,attention);
 function draw(report,pending=false){
  const summary=homeServiceSummary({...report,simulated});
  if(status.textContent!==summary.label)status.textContent=summary.label;
  status.dataset.state=summary.state;
  const [camera,...otherServices]=summary.rows;
  perception.dataset.state=camera.state;
  perceptionValue.textContent=camera.text;
  const nextCameraId=camera.source?.camera??null;
  if(cameraId!==nextCameraId&&document.activeElement===sourceButton)perceptionTitle.focus({preventScroll:true});
  cameraId=nextCameraId;
  sourceDetail.hidden=!cameraId;
  if(cameraId){
   const name=currentDevices.find(d=>d.kind==='camera'&&d.id===cameraId)?.name;
   const caption=name||'查看镜头';
   if(sourceName.textContent!==caption)sourceName.textContent=caption;
   if(sourceState.textContent!==camera.source.text)sourceState.textContent=camera.source.text;
   const label=(name?'查看'+name:caption)+'，'+camera.source.text;
   if(sourceButton.getAttribute('aria-label')!==label)sourceButton.setAttribute('aria-label',label);
  }
  for(const [label,{group,value,control}] of serviceRows){
   const row=otherServices.find(item=>item.label===label);
   if(!row&&control&&document.activeElement===control)heading.focus({preventScroll:true});
   group.hidden=!row;
   if(!row)continue;
   group.dataset.state=row.state;
   if(value.textContent!==row.text){
    value.textContent=row.text;
    control?.setAttribute('aria-label','查看'+label+'，'+row.text);
   }
  }
  const sample=report?.resources,age=sample?Date.now()-Date.parse(sample.sampled_at):NaN;
  const fresh=sample?.available&&Number.isFinite(age)&&age>=-2000&&age<=Math.min(15,sample.valid_for_seconds||0)*1000;
  resources.replaceChildren();
  const cpu=fresh&&Number.isFinite(sample.cpu_percent)?sample.cpu_percent:null;
  const used=fresh&&Number.isFinite(sample.memory_used_bytes)?sample.memory_used_bytes:null,total=fresh&&Number.isFinite(sample.memory_total_bytes)?sample.memory_total_bytes:null;
  for(const[label,value,ratio]of [['CPU',cpu==null?'—':cpu.toFixed(0)+'%',cpu],['内存',used==null||!total?'—':(used/1073741824).toFixed(1)+' / '+(total/1073741824).toFixed(0)+' GB',used==null||!total?null:used/total*100]]){
   const row=add(h('div',null,{class:'runtime-resource'}),add(h('div'),h('span',label),h('strong',value)));
   if(ratio!=null)row.append(h('meter',null,{min:0,max:100,value:Math.max(0,Math.min(100,ratio)),'aria-label':label+'使用率'}));resources.append(row);
  }
  if(camera.latency){
   const {ms,slow,multiple}=camera.latency;
   const label=slow?'画面处理较慢 · ':multiple?'最慢画面处理 ':'画面处理 ';
   resources.append(h('p',label+Math.round(ms)+' ms',{class:'runtime-latency','data-state':slow?'degraded':'on'}));
  }
  const feed=report?.events;
  const alerts=Array.isArray(feed?.events)&&feed.events.length<=3&&Number.isSafeInteger(feed.pending)
   &&feed.pending>=feed.events.length?feed:null;
  const notices=homeAttention(report?currentDevices:[],alerts?.events||[],report?.rules?.runs||[],report?.worker?.owned?report.worker.safety_recording:null,report?.proposals?.proposals||[],report?.rules?.task_attention);
  const extra=alerts?alerts.pending-alerts.events.length:0,noticeCount=notices.length+extra;
  const focused=document.activeElement;
  const message=!report?(pending?'正在更新待处理事项…':'提醒暂未更新'):
   !alerts?'事件记录暂未更新':!notices.length?'暂无需要你处理的事项':'';
  if(empty.textContent!==message)empty.textContent=message;
  empty.hidden=!message;
  const countLabel=noticeCount?String(noticeCount):'';
  if(count.textContent!==countLabel)count.textContent=countLabel;
  const keys=new Set(notices.map(item=>item.key));
  for(const [key,{row}] of attentionRows){
   if(keys.has(key))continue;
   row.remove();
   attentionRows.delete(key);
  }
  for(const item of notices){
   let entry=attentionRows.get(item.key);
   if(!entry){
    const mark=icon(item.key.startsWith('safety:')||item.priority===0?'alert-circle':item.device?'devices':'clock');
    const title=h('strong'),detail=h('small');
    const row=add(button(null,null,{class:'home-attention-item'}),mark,add(h('span'),title,detail),icon('chevron-right'));
    entry={row,title,detail};
    attentionRows.set(item.key,entry);
   }
   const {row,title,detail}=entry;
   row.onclick=()=>{if(item.proposalRef){try{sessionStorage.setItem('Friday.taskReview',item.proposalRef);}catch{}go('Ask Home');}else if(item.device)onDevice(item.device);else if(item.eventId)go(item.page,{eventId:item.eventId});else if(item.attentionId)go(item.page,{attentionId:item.attentionId});else go(item.page);};
   if(title.textContent!==item.title)title.textContent=item.title;
   if(detail.textContent!==(item.detail||''))detail.textContent=item.detail||'';
   row.setAttribute('data-priority',item.priority);
  }
  const ordered=notices.map(item=>attentionRows.get(item.key).row);
  if(ordered.some((row,index)=>row!==items.children[index]))items.replaceChildren(...ordered);
  allAlerts.hidden=extra===0;
  const pendingCount=alerts?.pending||0;
  allAlerts.setAttribute('aria-label','查看全部 '+pendingCount+' 条待查看提醒');
  const linkLabel='全部提醒 · '+pendingCount;
  if(allAlerts.children[0].textContent!==linkLabel)allAlerts.children[0].textContent=linkLabel;
  visibleCount=notices.length;
  expand.hidden=visibleCount<2||(extra>0&&notices.every(item=>item.eventId));
  expand.className='home-attention-expand text-button'+(visibleCount>4?' has-overflow':'');
  updateExpansion();
  if(focused&&document.activeElement!==focused&&ordered.includes(focused))focused.focus({preventScroll:true});
  else if((focused===allAlerts&&allAlerts.hidden)||(focused===expand&&expand.hidden)||
   (focused?.className==='home-attention-item'&&!ordered.includes(focused)))attentionHeading.focus({preventScroll:true});
 }
 async function refresh(force=false){
  if(!alive||busy||(!force&&document.hidden))return;busy=true;controller=new AbortController();
  const timeout=setTimeout(()=>controller?.abort(),7000);
  try{
   const paths={worker:'/api/embodied/status',infrastructure:'/api/embodied/infrastructure',rules:'/api/rules/core/runtime',arl:'/api/settings/arl/status',resources:'/api/home/resources',events:'/api/embodied/safety/attention',proposals:'/api/home-flow/task-proposals'};
   const keys=Object.keys(paths),values=await Promise.allSettled(keys.map(k=>api(paths[k],undefined,{signal:controller.signal})));
   if(!alive)return;const report={};values.forEach((v,i)=>report[keys[i]]=v.status==='fulfilled'?v.value:null);draw(report);
   clearTimeout(expiry);expiry=setTimeout(()=>{if(alive)draw(null);},16000);
  }finally{clearTimeout(timeout);busy=false;}
 }
 const visibility=()=>{if(!document.hidden)void refresh();};
 document.addEventListener('visibilitychange',visibility);
 draw(null,true);void refresh(true);timer=setInterval(()=>void refresh(),10000);
 return{refresh:()=>refresh(true),update(next){currentDevices=next;},dispose(){alive=false;controller?.abort();document.removeEventListener('visibilitychange',visibility);clearInterval(timer);clearTimeout(expiry);section.remove();attention.remove();}};
}
