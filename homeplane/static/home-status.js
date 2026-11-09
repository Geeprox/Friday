import {arlRuntimePresentation} from './arl-status.js';
import {safetyEventTitle} from './safety-event-labels.js';
const h=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=String(text);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
const add=(n,...children)=>{n.append(...children);return n;};
const button=(text,run,attrs={})=>{const n=h('button',text,{type:'button',...attrs});n.onclick=run;return n;};
const icon=name=>h('span',null,{class:'icon icon-'+name,'aria-hidden':'true'});
const service=(label,state,text)=>({label,state,text});
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
 const rows=[];
 rows.push(service('家庭感知',!worker?'unknown':!worker.configured?'off':worker.capture_enabled===false?'off':worker.error_class||['failed','unavailable'].includes(worker.state)?'warning':worker.owned&&['running','live','active'].includes(worker.state)?'on':'waiting',!worker?'未更新':!worker.configured?'未启用':worker.capture_enabled===false?'已暂停':worker.error_class?'需要检查':worker.owned&&['running','live','active'].includes(worker.state)?'运行中':'等待画面'));
 rows.push(voiceService(worker));
 const automatic=rules?.execution_state,clockRecovering=rules?.clock?.scheduled_count>0&&rules.clock.state==='recovering';
 rows.push(service('自动任务',!rules?'unknown':rules.worker_failure?'warning':!rules.available?'waiting':rules.automatic_enabled===false?'off':automatic==='ready'&&!clockRecovering?'on':'waiting',!rules?'未更新':rules.worker_failure?'需要检查':!rules.available?'未就绪':rules.automatic_enabled===false?'已暂停':clockRecovering?'定时任务恢复中':({ready:'运行中',manual:'手动控制中',sleeping:'睡眠模式',starting:'正在启动',awaiting_mode:'等待家庭状态'}[automatic]||'等待家庭状态')));
 if(worker?.configured){const recording=worker.safety_recording,state=recording?.state;rows.push(service('异常记录',worker.capture_enabled===false?'off':!worker.owned?'waiting':state==='unavailable'?'warning':state==='off'?'off':state==='ready'?'on':['saving','recovering'].includes(state)?'waiting':'unknown',worker.capture_enabled===false?'已暂停':!worker.owned?'待命':({unavailable:'需要检查',off:'已暂停',ready:'已就绪',saving:'正在保存',recovering:'正在恢复'}[state]||'未更新')));}
 const agent=arl?arlRuntimePresentation(arl):null;
 rows.push(service('智能服务',!agent?'unknown':agent.connected?'on':['disabled','sdk_missing','not_running'].includes(agent.state)?'off':['disconnected','unsafe_configuration'].includes(agent.state)?'warning':'waiting',!agent?'未更新':agent.connected?'已连接':['disabled','sdk_missing','not_running'].includes(agent.state)?'未启用':({registering:'正在连接',handshake_required:'尚未连接',upgrade_required:'需要更新'}[agent.state]||agent.label)));
 const issue=infrastructure?.failure||rows.some(r=>r.state==='warning');
 const unknown=rows.some(r=>r.state==='unknown');
 return {rows,label:simulated?'示例运行状态':issue?'有服务需要检查':unknown?'部分状态未更新':rows.some(r=>r.state==='on')?'按配置运行中':'按配置待命',state:issue?'warning':unknown?'unknown':'on'};
}
export function homeAttention(devices,events=[],runs=[],recording=null,proposals=[],taskAttention=null){
 const items=[];
 if(recording?.state==='unavailable')items.push({key:'safety-recording',title:recording.dropped>0?'有提醒未能保存':recording.unconfirmed>0?'有提醒的保存结果待核对':recording.partial>0?'有提醒画面未完整保存':'提醒记录暂不可用',detail:recording.unconfirmed>0?'查看记录与本机存储':recording.partial>0?'查看已保存的记录':'检查本机存储',priority:0,page:'Safety'});
 for(const event of events.filter(e=>e.review==='unreviewed'&&!e.simulated).slice(0,3))items.push({key:'safety:'+event.id,eventId:event.id,title:safetyEventTitle(event,{includePerson:false}),detail:(event.kind==='fall'?event.person_name:null)||event.camera_name||'查看事件与关键画面',priority:0,page:'Safety'});
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
export function mountHomeStatus(root,{api,go,devices=[],simulated=false,onDevice=()=>go('Devices')}={}){
 let alive=true,busy=false,controller=null,timer=null,expiry=null,currentDevices=devices;
 const section=h('section',null,{class:'home-runtime','aria-label':'Friday 运行状态'}),status=h('span','正在读取',{class:'runtime-overall',role:'status'}),list=h('dl',null,{class:'runtime-services'}),resources=h('div',null,{class:'runtime-resources'});
 section.append(add(h('div',null,{class:'home-aside-heading'}),h('h2','Friday'),button(null,()=>go('Settings'),{'aria-label':'Friday 设置',class:'quiet-icon'})),status,list,resources);
 section.querySelector('button').append(icon('settings'));
 const attention=h('section',null,{class:'home-attention','aria-label':'需要关注'}),count=h('span','',{class:'attention-count'}),items=h('div',null,{class:'home-attention-items'});
 attention.append(add(h('div',null,{class:'home-aside-heading'}),h('h2','需要关注'),count),items);
 root.append(section,attention);
 function draw(report){
  const summary=homeServiceSummary({...report,simulated});status.textContent=summary.label;status.dataset.state=summary.state;
  list.replaceChildren();for(const row of summary.rows)list.append(add(h('div',null,{'data-state':row.state}),h('dt',row.label),h('dd',row.text)));
  const sample=report?.resources,age=sample?Date.now()-Date.parse(sample.sampled_at):NaN;
  const fresh=sample?.available&&Number.isFinite(age)&&age>=-2000&&age<=Math.min(15,sample.valid_for_seconds||0)*1000;
  resources.replaceChildren();
  const cpu=fresh&&Number.isFinite(sample.cpu_percent)?sample.cpu_percent:null;
  const used=fresh&&Number.isFinite(sample.memory_used_bytes)?sample.memory_used_bytes:null,total=fresh&&Number.isFinite(sample.memory_total_bytes)?sample.memory_total_bytes:null;
  for(const[label,value,ratio]of [['CPU',cpu==null?'—':cpu.toFixed(0)+'%',cpu],['内存',used==null||!total?'—':(used/1073741824).toFixed(1)+' / '+(total/1073741824).toFixed(0)+' GB',used==null||!total?null:used/total*100]]){
   const row=add(h('div',null,{class:'runtime-resource'}),add(h('div'),h('span',label),h('strong',value)));
   if(ratio!=null)row.append(h('meter',null,{min:0,max:100,value:Math.max(0,Math.min(100,ratio)),'aria-label':label+'使用率'}));resources.append(row);
  }
  if(Number.isFinite(report?.worker?.latency_ms))resources.append(h('p','画面处理 '+Math.round(report.worker.latency_ms)+' ms',{class:'runtime-latency'}));
  if(report?.worker?.latency_ms>1000)resources.append(h('p','画面处理较慢',{class:'runtime-performance-note'}));
  const notices=homeAttention(report?currentDevices:[],report?.events?.events||[],report?.rules?.runs||[],report?.worker?.owned?report.worker.safety_recording:null,report?.proposals?.proposals||[],report?.rules?.task_attention);count.textContent=notices.length?String(notices.length):'';items.replaceChildren();
  if(!report)items.append(h('p','正在更新待处理事项…',{class:'home-attention-empty'}));
  else if(!notices.length)items.append(add(h('div',null,{class:'home-attention-empty'}),icon('check'),h('p',report.events?'暂无需要你处理的事项':'事件记录暂未更新')));
  for(const item of notices){
   const row=button(null,()=>{if(item.proposalRef){try{sessionStorage.setItem('Friday.taskReview',item.proposalRef);}catch{}go('Ask Home');}else if(item.device)onDevice(item.device);else if(item.eventId)go(item.page,{eventId:item.eventId});else if(item.attentionId)go(item.page,{attentionId:item.attentionId});else go(item.page);},{class:'home-attention-item','data-priority':item.priority});row.append(icon(item.priority===0?'alert-circle':item.device?'devices':'clock'),add(h('span'),h('strong',item.title),h('small',item.detail)),icon('chevron-right'));items.append(row);
  }
  if(notices.length>1){const expanded=attention.dataset.expanded==='true',more=button(expanded?'收起提醒':'展开 '+notices.length+' 项提醒',()=>{const expanded=attention.dataset.expanded!=='true';attention.dataset.expanded=String(expanded);more.textContent=expanded?'收起提醒':'展开 '+notices.length+' 项提醒';more.setAttribute('aria-expanded',String(expanded));},{class:'home-attention-expand text-button'+(notices.length>4?' has-overflow':''),'aria-expanded':String(expanded)});items.append(more);}
 }
 async function refresh(){
  if(!alive||busy||document.hidden)return;busy=true;controller=new AbortController();
  const timeout=setTimeout(()=>controller?.abort(),7000);
  try{
   const paths={worker:'/api/embodied/status',infrastructure:'/api/embodied/infrastructure',rules:'/api/rules/core/runtime',arl:'/api/settings/arl/status',resources:'/api/home/resources',events:'/api/embodied/safety/events',proposals:'/api/home-flow/task-proposals'};
   const keys=Object.keys(paths),values=await Promise.allSettled(keys.map(k=>api(paths[k],undefined,{signal:controller.signal})));
   if(!alive)return;const report={};values.forEach((v,i)=>report[keys[i]]=v.status==='fulfilled'?v.value:null);draw(report);
   clearTimeout(expiry);expiry=setTimeout(()=>{if(alive)draw(null);},16000);
  }finally{clearTimeout(timeout);busy=false;}
 }
 draw(null);void refresh();timer=setInterval(()=>void refresh(),10000);
 return{refresh,update(next){currentDevices=next;},dispose(){alive=false;controller?.abort();clearInterval(timer);clearTimeout(expiry);section.remove();attention.remove();}};
}
