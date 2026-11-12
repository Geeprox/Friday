import {catalogSourceName,mountAutomationCatalog} from './automations-catalog.js';
import {mountAutomationMode} from './automation-mode.js';
import {mountTaskAttention} from './task-attention.js';
const h=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
const add=(n,...children)=>{children.forEach(c=>c&&n.append(c));return n;};
const button=(label,run,attrs={})=>{const n=h('button',label,{type:'button',...attrs});n.onclick=run;return n;};
const select=(items,label)=>{const n=h('select',null,{'aria-label':label});for(const[value,text]of items)n.append(h('option',text,{value}));return n;};
const field=(label,input)=>add(h('label',null,{class:'product-field'}),h('span',label),input);
const labels={'humidifier.power':'电源','humidifier.humidity':'设定湿度','humidifier.mode':'模式','water_heater.power':'电源','water_heater.mode':'加热档位','light.set':'电源与亮度','light.color_temperature':'色温','switch.set':'电源','fan.set':'电源','fan.percentage':'风速','climate.mode':'模式','climate.power':'电源','climate.temperature':'目标温度','climate.fan_mode':'风速','cover.position':'开合程度','cover.tilt':'叶片角度','cover.state':'打开或关闭','lock.set':'门锁','media_player.power':'电源','media_player.volume':'音量','media_player.mute':'静音','media_player.playback':'播放状态'};
const valueNames={on:'开启',off:'关闭',open:'打开',closed:'关闭',stopped:'停止',cool:'制冷',heat:'制热',auto:'自动',heat_cool:'自动冷暖',dry:'除湿',fan_only:'送风',locked:'上锁',unlocked:'解锁',playing:'播放',paused:'暂停'};
const parameter={'humidifier.power':'power','humidifier.humidity':'humidity','humidifier.mode':'mode','water_heater.power':'power','water_heater.mode':'operation_mode','light.set':'power','light.color_temperature':'color_temp_kelvin','switch.set':'power','fan.set':'power','fan.percentage':'percentage','climate.mode':'hvac_mode','climate.power':'power','climate.temperature':'temperature','climate.fan_mode':'fan_mode','cover.position':'position','cover.tilt':'tilt_position','cover.state':'state','lock.set':'lock','media_player.power':'power','media_player.volume':'volume_level','media_player.mute':'is_volume_muted','media_player.playback':'state'};
const comparisons={lt:'低于',le:'不高于',gt:'高于',ge:'不低于',eq:'等于'};
const binaryStates={occupancy:['无人','有人'],motion:['无人移动','有人移动'],moisture:['未漏水','检测到漏水'],door:['已关闭','已打开'],opening:['已关闭','已打开']};
const modeNames={'home.sleep':'睡眠模式','home.manual':'手动优先','home.present':'有人在家'};
const reviewErrors={
 schedule_time_unavailable:'执行时间已过，请重新选择日期与时间。',
 catalog_source_changed:'原规则记录已更新，请返回列表刷新后重新打开。',
 trigger_required:'请选择触发设备和状态变化。',
 target_required:'请为每个动作选择设备。',
 action_required:'请至少添加一个动作。',
 numeric_required:'请为每个数值条件选择读数和比较方式，并填写数值。',
 schedule_required:'请选择执行时间和重复日期。',
 once_required:'请选择有效的执行日期与时间。',
 timezone_required:'家庭时区尚未就绪，请先连接家庭服务。',
 brightness_required:'请输入 0.1–100 的亮度百分比。',
 delay_required:'请输入有效的等待秒数。',
 delay_total:'等待总时长需小于 30 秒，请缩短等待后重试。',
};
function sourceReference(entry){
 const heading=h('h3','原规则',{id:'automation-original-title'});
 const detail=add(h('aside',null,{class:'automation-source-reference','aria-labelledby':'automation-original-title'}),
  heading, h('strong',entry.name,{class:'automation-source-name'}),
  h('p',catalogSourceName(entry.source)+' · '+(entry.evidence==='ui_name_index'?'仅记录名称':'已记录内容')));
 if(entry.evidence==='ui_name_index')return detail;
 const list=h('dl');
 for(const[key,label]of [['triggers','触发'],['conditions','条件'],['actions','动作'],['issues','待核对']]){
  if(!entry[key]?.length)continue;
  const values=h('ul');entry[key].forEach(value=>values.append(h('li',value)));
  list.append(h('dt',label),add(h('dd'),values));
 }
 detail.append(list);
 return detail;
}
function scheduleTimezoneName(zone){try{return new Intl.DateTimeFormat('zh-CN',{timeZone:zone,timeZoneName:'longGeneric'}).formatToParts(new Date()).find(part=>part.type==='timeZoneName')?.value||zone;}catch{return zone||'家庭时间';}}
function onceLocalValue(trigger){
 try{if(!/(?:Z|[+-]\d{2}:\d{2})$/.test(trigger.at))return '';const parts=new Intl.DateTimeFormat('en-CA',{timeZone:trigger.timezone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(new Date(trigger.at)),part=key=>parts.find(p=>p.type===key)?.value;return `${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}:${part('second')}`;}catch{return '';}
}
export function scheduleDescription(trigger){
 if(trigger?.type==='once'){const local=onceLocalValue(trigger);if(!local)return '执行时间待核对';const offset=new Intl.DateTimeFormat('zh-CN',{timeZone:trigger.timezone,timeZoneName:'shortOffset'}).formatToParts(new Date(trigger.at)).find(p=>p.type==='timeZoneName')?.value.replace('GMT','UTC')||'';return `${local.replace('T',' ')}（${scheduleTimezoneName(trigger.timezone)} ${offset}）· 仅一次`;}
 const days=Array.isArray(trigger?.weekdays)?[...new Set(trigger.weekdays)].sort((a,b)=>a-b):[];
 if(!days.length||days.some(n=>!Number.isInteger(n)||n<1||n>7)||!/^([01]\d|2[0-3]):[0-5]\d$/.test(trigger.at))return '执行时间待核对';
 const period=days.length===7?'每天':days.join(',')==='1,2,3,4,5'?'周一至周五':days.join(',')==='6,7'?'周末':'每周'+days.map(n=>'一二三四五六日'[n-1]).join('、');
 return `${period} ${trigger.at}（${scheduleTimezoneName(trigger.timezone)}）`;
}
export function mountAutomations(root,{api,data,snapshot,go,toast,legacyRules,attentionId=null}){
 let alive=true,tab='scenes',listing=null,scenes=null,loading=false,editor=null,dialog=null,query='',modeView=null;
 let focusRecord=null;
 let clockReport=null,taskAttentionView=null,requestedAttention=attentionId;const clockViews=new Map();
 if(attentionId)tab='owned';
 function updateClock(report){clockReport=report?.clock?.one_off_tasks||{};for(const[id,show]of clockViews)show(clockReport[id]);taskAttentionView?.update(report?.task_attention||null);}
 const view=h('div',null,{class:'automation-workspace'});root.append(view);
 const controls=data.devices.flatMap(d=>d.controls.map(c=>({...c,room:d.room}))),byControl=new Map(controls.map(c=>[c.id,c]));
 const readings=new Map(data.devices.flatMap(d=>d.readings.map(r=>[r.id,r])));
 const names=new Map(data.devices.flatMap(d=>[...d.controls,...d.readings].map(c=>[c.id,d.room+' · '+c.name])));
 const triggerEntities=(snapshot?.entities||[]).filter(e=>names.has(e.id)&&['light','switch','fan','binary_sensor','cover','climate'].includes(e.kind)&&e.capabilities.some(c=>c.access==='event'&&c.support==='supported'&&c.name.endsWith('.transition')));
 const observedNumbers=new Map((snapshot?.observations||[]).filter(o=>Number.isFinite(o.value)).map(o=>[o.entity_id+':'+o.capability,o]));
 const canonicalNumbers=new Map((snapshot?.states||[]).filter(state=>Number.isFinite(state.value)).map(state=>[state.id,state]));
 const measurementNames=new Map(names);
 for(const entity of snapshot?.derived_entities||[]){
  measurementNames.set(entity.id,[entity.room,entity.name].filter(Boolean).join(' · '));
 }
 const measurements=new Map();
 for(const entity of [...(snapshot?.entities||[]),...(snapshot?.derived_entities||[])]){
  if(!measurementNames.has(entity.id))continue;
  for(const cap of entity.capabilities){
   if(cap.access!=='read'||cap.support!=='supported')continue;
   const key=entity.id+':'+cap.name,reading=canonicalNumbers.get(key)||observedNumbers.get(key);
   if(reading&&(reading.unit??null)===(cap.unit??null)){
    measurements.set(key,{entity:entity.id,capability:cap.name,unit:cap.unit??null});
   }
  }
 }
 function triggerValue(entity,value){
  if(entity?.kind==='binary_sensor')return (binaryStates[readings.get(entity.id)?.device_class]||['未触发','已触发'])[value==='on'?1:0];
  return valueNames[value]??String(value);
 }
 function describeTrigger(trigger){
  if(['schedule','once'].includes(trigger.type))return scheduleDescription(trigger);
  const entity=triggerEntities.find(e=>e.id===trigger.entity),label=names.get(trigger.entity)||'触发设备待核对';
  if(trigger.type==='state_transition')return label+' 从'+triggerValue(entity,trigger.from_value)+'变为'+triggerValue(entity,trigger.to_value);
  return label+' · '+(valueNames[trigger.value]??String(trigger.value));
 }
 function describeCondition(condition){
  if(condition.type==='numeric'){
   const name=measurementNames.get(condition.entity)||'原传感器';
   return name+' '+comparisons[condition.operator]+' '+condition.value+(condition.unit?' '+condition.unit:'');
  }
  if(condition.type==='time_window')return condition.start+'–'+condition.end+'（'+scheduleTimezoneName(condition.timezone)+'）';
  if(condition.type==='mode')return modeNames[condition.mode]+' · '+(condition.equals?'是':'否');
  return '原有判断条件';
 }
 const describeAction=a=>{if(a.type==='delay')return `等待 ${a.milliseconds/1000} 秒`;if(a.type==='cancel_pending')return '停止这条自动化尚未执行的动作';if(a.type==='feedback.speak')return '播报：'+a.text;const c=byControl.get(a.target),desired=a.desired||{power:a.power,...(a.brightness!=null?{brightness:a.brightness}:{})};return (c?c.room+' · '+c.name:'设备需重新选择')+' · '+Object.entries(desired).map(([key,value])=>key==='brightness'?value==='minimum_verified'?'设备最低亮度':'亮度 '+Number((value/255*100).toFixed(2))+'%':['position','tilt_position'].includes(key)?value+'%':key==='temperature'?value+'°':key==='volume_level'?'音量 '+Math.round(value*100)+'%':valueNames[value]??String(value)).join('，')+(a.require_no_bright_flash?' · 平稳点亮':'');};
 function closeDialog(){if(dialog){dialog.close();dialog.remove();dialog=null;}}
 function confirm(title,lines,run,{label='运行',failure='结果尚未确认，请先查看设备状态。',onDone}={}){
  closeDialog();
  const trigger=document.activeElement,activeDialog=add(h('dialog',null,{class:'product-dialog automation-confirm','aria-labelledby':'automation-confirm-title'}),h('h2',title,{id:'automation-confirm-title'}));
  dialog=activeDialog;
  lines.forEach(line=>activeDialog.append(h('p',line)));
  const close=()=>{closeDialog();trigger?.focus?.();};
  const note=h('p','',{role:'status'}),cancel=button('取消',close);
  const apply=button(label,async()=>{
   apply.disabled=cancel.disabled=true;
   note.textContent='正在提交…';
   try{
    await run();
    if(dialog===activeDialog){close();void onDone?.();}
   }catch(_){
    if(dialog!==activeDialog)return;
    note.textContent=failure;apply.hidden=true;cancel.disabled=false;cancel.textContent='关闭';cancel.focus();
   }
  },{class:'primary'});
  activeDialog.append(note,add(h('div',null,{class:'product-actions'}),cancel,apply));
  activeDialog.addEventListener('cancel',event=>{event.preventDefault();if(!cancel.disabled)close();});
  document.body.append(activeDialog);activeDialog.showModal();cancel.focus();
 }
 async function refresh(){if(loading)return;loading=true;try{const values=await Promise.allSettled([api('/api/rules/core/records'),api('/api/embodied/scenes')]);if(!alive)return;listing=values[0].status==='fulfilled'?values[0].value:null;scenes=values[1].status==='fulfilled'?values[1].value:null;loading=false;draw();}finally{loading=false;}}
 function newest(){const grouped=new Map();for(const record of listing?.records||[]){const old=grouped.get(record.rule.id);if(!old||old.rule.revision<record.rule.revision)grouped.set(record.rule.id,record);}return [...grouped.values()];}
 function draw(){if(!alive)return;modeView?.dispose();modeView=null;taskAttentionView?.dispose();taskAttentionView=null;clockViews.clear();clockReport=null;view.replaceChildren();const head=add(h('div',null,{class:'product-section-head'}),add(h('div'),h('h2','家里的场景与自动安排'),h('p','一键运行常用场景，或设置设备在条件满足时自动响应。',{class:'caption'})),button('新建自动化',()=>openEditor(),{class:'primary'}));view.append(head);
  const tabs=h('div',null,{class:'category-tabs','aria-label':'自动化分类'});for(const[key,label]of [['scenes','一键场景'],['owned','自动运行'],['sources','其他平台的规则']])tabs.append(button(label,()=>{tab=key;query='';draw();},{'aria-pressed':String(tab===key)}));view.append(tabs);
  const content=h('section',null,{class:'automation-content'});view.append(content);
  if(tab==='sources'){mountAutomationCatalog(content,{api,isAlive:()=>alive,externalOnly:true,onUseEntry:source=>openEditor(null,source)});return;}
  if(tab==='scenes'){
   if(!scenes){content.append(add(h('div',null,{class:'automation-empty'}),h('h3',loading?'正在读取米家场景…':'米家场景暂时无法读取'),h('p','连接恢复后会自动显示已保存的一键场景。'),button('重新读取',()=>refresh())));return;}
   const search=h('input',null,{type:'search',placeholder:'搜索场景','aria-label':'搜索一键场景'});search.value=query;const list=h('div',null,{class:'manual-scenes'});search.oninput=()=>{query=search.value;renderScenes(list);};content.append(search,list);renderScenes(list);return;
  }
  const attentionSlot=h('div');content.append(attentionSlot);taskAttentionView=mountTaskAttention(attentionSlot,{api,go,toast,getRecord:id=>newest().find(record=>record.id===id),onEdit:openEditor,onReload:refresh,onEmpty:()=>{const title=view.querySelector('h2');title?.setAttribute('tabindex','-1');title?.focus();},attentionId:requestedAttention});
  modeView=mountAutomationMode(content,{api,toast,onStatus:updateClock});
  if(!listing){content.append(h('p','自动化状态暂时无法读取，请重新读取。'),button('重新读取',()=>refresh()));return;}
  const list=h('div',null,{class:'owned-automation-list'});content.append(list);
  if(!newest().length)list.append(add(h('div',null,{class:'automation-empty'}),h('h3','还没有 Friday 自动化'),h('p','例如：玄关有人时开灯，或主卧窗帘关闭后关灯。'),button('设置第一条自动化',()=>openEditor(),{class:'primary'})));
  for(const record of newest()){
   const oneOff=record.rule.trigger.type==='once',row=h('article',null,{class:'owned-automation'});
   const title=h('h3',record.rule.intent,{tabindex:'-1'}),origin=record.catalog_origin;
   const info=add(h('div'),title,h('p',describeTrigger(record.rule.trigger),{class:'caption'}));
   for(const action of record.rule.actions)info.append(h('p',describeAction(action),{class:'caption'}));
   if(origin)info.append(h('p','来自'+catalogSourceName(origin.source)+(origin.name!==record.rule.intent?' · '+origin.name:''),{class:'caption'}));
   row.append(info);
   const setState=async()=>{
    const updated=await api('/api/home/automations/records/'+encodeURIComponent(record.id)+'/state',{
     enabled:!record.enabled,expected_fingerprint:record.fingerprint,expected_context:listing.fingerprint,
    });
    if(updated.id!==record.id||updated.enabled!==!record.enabled)throw Error('state_unverified');
    focusRecord=record.id;
    toast(updated.enabled?'自动化已启用':'自动化已暂停');
   };
   const state=button(record.enabled?'已启用':'已暂停',async()=>{
    if(!record.enabled&&origin?.kind==='automation'){
     confirm('启用这条自动化？',['请先在'+catalogSourceName(origin.source)+'暂停「'+origin.name+'」，以免重复执行。'],setState,
      {label:'原规则已暂停，启用',failure:'启用状态待确认，请重新读取规则。',onDone:refresh});
     return;
    }
    state.disabled=true;
    try{await setState();await refresh();}
    catch(_){toast('规则状态待确认，请重新读取。',true);}
    finally{state.disabled=false;}
   },{role:'switch','aria-checked':String(record.enabled),'aria-label':record.rule.intent+'是否启用'});
   const run=button('执行一次',()=>confirm('执行“'+record.rule.intent+'”？',['这次直接执行以下动作，不等待触发条件。',...record.rule.actions.filter(a=>a.target).map(describeAction)],async()=>{const result=await api(`/api/home/automations/records/${encodeURIComponent(record.id)}/run`,{expected_fingerprint:record.fingerprint,expected_context:listing.fingerprint,event_id:crypto.randomUUID()});toast(result.status==='reported_match'?'设备已回报更新':'已发送，部分设备结果尚未确认',result.status!=='reported_match');}));
   const supported=record.rule.actions.every(a=>a.type==='device.set'||a.type==='light.set'&&a.brightness!=='minimum_verified'&&!a.require_no_bright_flash);run.disabled=!listing.available||!supported;
   const edit=button('编辑',()=>openEditor(record));
   if(oneOff){const status=h('p','正在核对执行状态…',{class:'caption',role:'status'});info.append(status);run.hidden=true;
    const show=report=>{const valid=report?.fingerprint===record.fingerprint,key=valid?report.state:'unavailable',terminal=['completed','finished','missed','unconfirmed','not_executed','cancelled'].includes(key);const text=({scheduled:'等待执行',running:'正在执行',paused:'已暂停',completed:'已执行',finished:'已结束',missed:'已错过执行时间',unconfirmed:'执行结果待确认',not_executed:'未执行',cancelled:'已取消'})[key]||'执行状态待核对';if(status.textContent!==text)status.textContent=text;state.hidden=terminal;state.disabled=terminal||!valid||key==='unavailable';edit.textContent=terminal?'重新安排':'编辑';};clockViews.set(record.id,show);show(clockReport?.[record.id]);}
   row.append(add(h('div',null,{class:'product-actions'}),state,edit,run));if(!supported)row.append(h('p','这条自动化按设定条件运行。',{class:'caption'}));list.append(row);
   if(focusRecord===record.id){focusRecord=null;title.focus();}
  }
  const advanced=add(h('details',null,{class:'automation-advanced'}),h('summary','运行记录与高级设置'));advanced.addEventListener('toggle',()=>{if(advanced.open&&!advanced.loaded){advanced.loaded=true;legacyRules(advanced);}});content.append(advanced);
 }
 function renderScenes(list){list.replaceChildren();const items=(scenes?.scenes||[]).filter(s=>s.name.toLowerCase().includes(query.toLowerCase()));if(!items.length)list.append(h('p',query?'没有符合条件的场景':'米家中还没有一键场景。',{class:'automation-empty'}));for(const scene of items){const status=h('p',scene.enabled===false?'米家中已停用':scene.device_count?`米家 · ${scene.device_count} 个设备`:'米家场景',{class:'caption',role:'status'}),run=button('运行',()=>confirm('运行“'+scene.name+'”？',['将按照米家中保存的设置执行'+(scene.device_count?'，涉及 '+scene.device_count+' 个设备。':'。')],async()=>{const result=await api('/api/embodied/scenes/run',{scene:scene.id,revision:scene.revision,event_id:crypto.randomUUID()});status.textContent=result.acknowledged?'已发送 · 请查看设备响应':'结果尚未确认，请查看设备状态';toast(result.acknowledged?'场景指令已发送':'场景结果尚未确认',!result.acknowledged);}));run.disabled=scene.enabled===false;list.append(add(h('article',null,{class:'manual-scene'}),add(h('div'),h('h3',scene.name),status),run));}}
 function openEditor(record=null,source=null){
  closeDialog();editor?.remove();
  const trigger=document.activeElement,entry=source?.entry;
  const startPaused=Boolean(entry||record?.enabled===false);
  const activeEditor=h('dialog',null,{class:'product-dialog automation-editor','aria-labelledby':'automation-editor-title'});
  editor=activeEditor;
  const title=h('h2',entry?'在 Friday 重建':record?'编辑自动化':'新建自动化',{id:'automation-editor-title',tabindex:'-1'});
  activeEditor.append(title);
  const draft=record?structuredClone(record.rule):{
   id:'owned:'+crypto.randomUUID(),revision:1,intent:entry?.name||'',trigger:null,conditions:[],actions:[],
   execution:{preferred_backend:'home_core',max_event_age_ms:3000},simulated:false,
  };
  if(record)draft.revision++;
  const form=h('form'),fields=h('div',null,{class:'automation-editor-fields'});
  const name=h('input',null,{required:'',maxlength:160,placeholder:'例如：玄关有人时开灯','aria-label':'自动化名称'});
  name.value=draft.intent;
  fields.append(field('名称',name));
  const triggerChoice=select([['','选择触发设备'],...triggerEntities.map(e=>[e.id,names.get(e.id)||e.name])],'触发设备'),edge=select([],'触发变化');triggerChoice.value=draft.trigger?.entity||'';
  function valuesFor(entity){if(entity?.kind==='cover')return['closed','open'];if(entity?.kind==='climate')return byControl.get(entity.id)?.capabilities.find(c=>c.name==='climate.mode')?.choices||['off','cool'];return['off','on'];}
  function edges(){
   const entity=triggerEntities.find(e=>e.id===triggerChoice.value),values=valuesFor(entity);
   edge.replaceChildren();
   if(entry)edge.append(h('option','选择状态变化',{value:''}));
   for(const from of values)for(const to of values)if(from!==to)edge.append(
    h('option',triggerValue(entity,from)+' → '+triggerValue(entity,to),{value:JSON.stringify([from,to])}));
   if(draft.trigger?.type==='state_transition'&&draft.trigger.entity===entity?.id){
    const value=JSON.stringify([draft.trigger.from_value,draft.trigger.to_value]);
    if([...edge.options].some(o=>o.value===value))edge.value=value;
   }
  }
  triggerChoice.onchange=edges;edges();
  let householdZone=['schedule','once'].includes(draft.trigger?.type)?draft.trigger.timezone:null;
  const originalOnce=draft.trigger?.type==='once'?structuredClone(draft.trigger):null,originalOnceLocal=originalOnce?onceLocalValue(originalOnce):'';
  const kindOptions=[['state_transition','设备状态变化'],['once','只执行一次'],['schedule','每天或每周'],...(draft.trigger?.type==='event'?[['event','原有事件']]:[])],triggerKind=select(kindOptions,'触发方式');triggerKind.value=draft.trigger?.type||'state_transition';fields.append(field('何时执行',triggerKind));
  const eventFields=add(h('div',null,{class:'automation-trigger-fields'}),field('当',triggerChoice),field('变化为',edge));
  const clockTime=h('input',null,{type:'time',required:'','aria-label':'执行时间'});clockTime.value=draft.trigger?.at||'';
  const onceTime=h('input',null,{type:'datetime-local',step:'1',required:'','aria-label':'执行日期与时间'});onceTime.value=originalOnceLocal;
  const weekdayGroup=h('fieldset',null,{class:'automation-weekdays'}),weekdays=[];weekdayGroup.append(h('legend','每周重复'));
  for(let n=1;n<=7;n++){const input=h('input',null,{type:'checkbox','aria-label':'周'+'一二三四五六日'[n-1]});input.checked=(draft.trigger?.weekdays||[1,2,3,4,5,6,7]).includes(n);weekdays.push(input);weekdayGroup.append(add(h('label',null,{class:'automation-time-toggle'}),input,h('span','周'+'一二三四五六日'[n-1])));}
  const recurringFields=add(h('div',null,{class:'automation-recurring-fields'}),field('执行时间',clockTime),weekdayGroup),onceFields=field('日期与时间',onceTime);onceFields.setAttribute('class','product-field automation-once-fields');
  const timezoneNote=h('p','正在读取家庭时区…',{class:'caption'}),clockFields=add(h('div',null,{class:'automation-clock-fields'}),onceFields,recurringFields,timezoneNote),retained=h('p','保留原有事件触发条件。',{class:'caption'});
  function drawTrigger(){const scheduled=triggerKind.value==='schedule',once=triggerKind.value==='once';eventFields.hidden=triggerKind.value!=='state_transition';triggerChoice.disabled=edge.disabled=eventFields.hidden;clockFields.hidden=!scheduled&&!once;recurringFields.hidden=!scheduled;onceFields.hidden=!once;onceTime.disabled=!once;clockTime.disabled=!scheduled;weekdays.forEach(input=>input.disabled=!scheduled);retained.hidden=triggerKind.value!=='event';timezoneNote.textContent=householdZone?'按'+scheduleTimezoneName(householdZone)+'执行':'家庭时区尚未就绪，请先连接家庭服务。';}
  fields.append(eventFields,clockFields,retained);drawTrigger();triggerKind.onchange=drawTrigger;
  const scheduleOptions=[...triggerKind.options].filter(option=>['schedule','once'].includes(option.value));scheduleOptions.forEach(option=>option.disabled=!householdZone);
  void api('/api/rules/core/context').then(context=>{if(!alive||editor!==activeEditor||!activeEditor.isConnected)return;if(!['schedule','once'].includes(draft.trigger?.type))householdZone=context.household_timezone||null;scheduleOptions.forEach(option=>option.disabled=!householdZone);drawTrigger();}).catch(()=>{if(alive&&editor===activeEditor){scheduleOptions.forEach(option=>option.disabled=!householdZone);drawTrigger();}});
  const actions=h('section',null,{class:'automation-action-list'});const inputs=[];
  function addAction(action=null){const row=h('div',null,{class:'automation-action-row'}),target=select([['','选择设备'],...controls.filter(c=>c.capabilities.some(cap=>labels[cap.name])).map(c=>[c.id,c.room+' · '+c.name])],'动作设备'),operation=select([],'设备操作'),parameters=h('div',null,{class:'automation-parameters'});target.value=action?.target||'';const originalDesired=()=>structuredClone(action?.desired||{power:action?.power||'on',...(action?.brightness!=null?{brightness:action.brightness}:{})});let desired=originalDesired(),read=()=>desired;
   const remove=()=>{row.remove();changed();};
   if(action?.type==='delay'){const seconds=h('input',null,{type:'number',min:.001,max:30,step:.001,required:'','aria-label':'等待秒数'});seconds.value=action.milliseconds/1000;row.append(field('等待（秒）',seconds),button('移除此动作',remove));inputs.push({row,read:()=>{const milliseconds=Math.round(Number(seconds.value)*1000);if(!Number.isInteger(milliseconds)||milliseconds<1||milliseconds>30000)throw Error('delay_required');return{...action,milliseconds};}});actions.append(row);return;}
   if(action&&!['device.set','light.set'].includes(action.type)){row.append(h('p',describeAction(action),{class:'caption'}),button('移除此动作',remove));inputs.push({row,read:()=>action});actions.append(row);return;}
   function drawParameters(){parameters.replaceChildren();const cap=byControl.get(target.value)?.capabilities.find(c=>c.name===operation.value),key=parameter[operation.value];if(!cap){read=()=>({});return;}let options=cap.choices,input;if(key==='power')options=['on','off'];if(key==='is_volume_muted')options=[true,false];if(options){input=select(options.map(v=>[String(v),typeof v==='boolean'?v?'静音':'取消静音':valueNames[v]||v]),'动作值');input.value=String(desired[key]??options[0]);parameters.append(field('设为',input));read=()=>({[key]:key==='is_volume_muted'?input.value==='true':input.value});}else{input=h('input',null,{type:'number',required:'',min:cap.minimum??0,max:cap.maximum??100,step:cap.step??1,'aria-label':'动作值'});input.value=desired[key]??cap.minimum??0;parameters.append(field(labels[operation.value]+(cap.unit?'（'+cap.unit+'）':''),input));read=()=>({[key]:Number(input.value)});}
    if(operation.value==='light.set'&&(cap.maximum!=null||desired.brightness!=null)){
     const choices=[['keep','保持设备当前亮度'],['level','指定亮度']];if(action?.brightness==='minimum_verified')choices.push(['minimum','设备最低亮度']);const mode=select(choices,'亮度方式'),brightness=h('input',null,{type:'number',min:.1,max:100,step:'any','aria-label':'亮度百分比'});mode.value=desired.brightness==='minimum_verified'?'minimum':typeof desired.brightness==='number'?'level':'keep';brightness.value=typeof desired.brightness==='number'?Number((desired.brightness/255*100).toFixed(2)):'';const originalLevel=brightness.value,levelField=field('亮度 %',brightness),brightnessFields=add(h('div'),field('亮度',mode),levelField);parameters.append(brightnessFields);if(action?.require_no_bright_flash)brightnessFields.append(h('p','保持平稳点亮设置',{class:'caption'}));
     const sync=()=>{const on=input.value==='on';brightnessFields.hidden=!on;mode.disabled=!on;levelField.hidden=mode.value!=='level';brightness.disabled=!on||mode.value!=='level';brightness.required=!brightness.disabled;};mode.onchange=sync;input.addEventListener('change',sync);sync();const base=read;read=()=>{const value=base();if(value.power!=='on'||mode.value==='keep')return value;if(mode.value==='minimum')return{...value,brightness:'minimum_verified'};const level=Number(brightness.value);if(!brightness.value||!Number.isFinite(level)||level<.1||level>100)throw Error('brightness_required');return{...value,brightness:brightness.value===originalLevel&&typeof desired.brightness==='number'?desired.brightness:Math.max(1,Math.round(level*255/100))};};
    }}
   function operations(){operation.replaceChildren();for(const cap of byControl.get(target.value)?.capabilities||[])if(labels[cap.name])operation.append(h('option',labels[cap.name],{value:cap.name}));if(action?.operation&&[...operation.options].some(o=>o.value===action.operation))operation.value=action.operation;else if(action?.type==='light.set')operation.value='light.set';drawParameters();}target.onchange=()=>{desired=action?.type==='light.set'?originalDesired():{};operations();};operation.onchange=()=>{desired=action?.type==='light.set'&&operation.value==='light.set'?originalDesired():{};drawParameters();};operations();
   row.append(field('设备',target),field('操作',operation),parameters,button('移除此动作',remove));inputs.push({row,read:()=>{if(!target.value)throw Error('target_required');const selected=read();if(action?.type==='light.set'&&operation.value==='light.set')return{...action,target:target.value,power:selected.power,brightness:selected.brightness,require_no_bright_flash:selected.power==='on'&&action.require_no_bright_flash===true};return{id:action?.id||'effect:'+crypto.randomUUID(),type:'device.set',target:target.value,operation:operation.value,desired:selected};}});actions.append(row);
  }
  (draft.actions.length?draft.actions:[null]).forEach(addAction);
  const conditions=add(h('details',null,{class:'automation-conditions'}),h('summary','附加条件（可选）'));
  conditions.open=Boolean(entry?.conditions?.length||draft.conditions.length);
  const conditionRows=h('div'),conditionInputs=[];
  conditions.append(conditionRows);
  function addCondition(condition){
   const box=h('div',null,{class:'automation-condition-row'});
   let read,firstField;
   if(!condition||condition.type==='numeric'){
    const number=h('input',null,{type:'number',step:'any','aria-label':'条件数值'});
    const operator=select([['','选择比较方式'],...Object.entries(comparisons)],'比较方式');
    let selected=()=>condition;
    if(condition){
     number.value=condition.value;
     operator.value=condition.operator;
     box.append(h('p',measurementNames.get(condition.entity)||'原传感器',{class:'automation-condition-name'}));
    }else{
     const measurement=select([['','选择传感器读数'],...[...measurements].map(([key,source])=>
      [key,measurementNames.get(source.entity)+(source.unit?'（'+source.unit+'）':'')])],'条件读数');
     firstField=measurement;
     selected=()=>measurements.get(measurement.value);
     const readingField=field('读数',measurement);
     readingField.setAttribute('class','product-field automation-condition-reading');
     box.append(readingField);
    }
    const unit=h('span','数值'+(condition?.unit?'（'+condition.unit+'）':''));
    firstField?.addEventListener('change',()=>{
     const value=selected()?.unit;
     unit.textContent=value?'数值（'+value+'）':'数值';
    });
    box.append(field('比较',operator),add(h('label',null,{class:'product-field'}),unit,number));
    read=()=>{
     const source=selected();
     if(!source||!operator.value||!number.value.trim()||!Number.isFinite(Number(number.value)))throw Error('numeric_required');
     return {type:'numeric',entity:source.entity,capability:source.capability,unit:source.unit,operator:operator.value,value:Number(number.value)};
    };
   }else if(condition.type==='time_window'){
    const start=h('input',null,{type:'time','aria-label':'开始时间'}),end=h('input',null,{type:'time','aria-label':'结束时间'});
    start.value=condition.start;end.value=condition.end;
    box.append(field('从',start),field('到',end));
    read=()=>({...condition,start:start.value,end:end.value});
   }else{
    box.append(h('p',describeCondition(condition)));
    read=()=>condition;
   }
   box.append(button('移除此条件',()=>{box.remove();changed();}));
   conditionInputs.push(()=>box.isConnected?read():null);
   conditionRows.append(box);
   return firstField;
  }
  draft.conditions.forEach(addCondition);
  if(measurements.size)conditions.append(button('添加数值条件',()=>{addCondition(null)?.focus();changed();}));
  const timeToggle=h('input',null,{type:'checkbox','aria-label':'只在指定时段运行'});
  const start=h('input',null,{type:'time','aria-label':'新条件开始时间'}),end=h('input',null,{type:'time','aria-label':'新条件结束时间'});
  start.value='08:00';end.value='22:00';
  const timeFields=add(h('div',null,{class:'automation-time-fields',hidden:''}),field('从',start),field('到',end));
  timeToggle.onchange=()=>{timeFields.hidden=!timeToggle.checked;};
  conditions.append(add(h('label',null,{class:'automation-time-toggle'}),timeToggle,h('span','只在指定时段运行')),timeFields);
  fields.append(conditions,add(h('section',null,{class:'automation-actions-editor'}),h('h3','就执行'),actions,add(h('div',null,{class:'product-actions'}),
   button('添加一个动作',()=>{addAction();changed();}),
   button('添加等待',()=>{addAction({id:'wait:'+crypto.randomUUID(),type:'delay',milliseconds:1000,cancel_on_new_event:true});changed();}))));
  const output=h('div',null,{class:'automation-review'});
  const review=h('button',entry?'核对规则':'核对并保存',{type:'submit',class:'primary'});
  let saving=false,draftVersion=0;
  const dismiss=()=>{
   if(editor!==activeEditor||saving)return;
   editor=null;activeEditor.close();activeEditor.remove();trigger?.focus?.();
  };
  fields.append(add(h('div',null,{class:'product-actions'}),review,button('取消',dismiss)));
  form.append(fields,output);
  if(entry){
   activeEditor.setAttribute('data-rebuild','');
   activeEditor.append(add(h('div',null,{class:'automation-rebuild-grid'}),sourceReference(entry),form));
  }else activeEditor.append(form);
  const saveStatus=h('p','',{class:'automation-save-status',role:'status',tabindex:'-1'});
  activeEditor.append(saveStatus);
  document.body.append(activeEditor);activeEditor.showModal();
  (entry?title:name).focus();
  activeEditor.addEventListener('cancel',event=>{event.preventDefault();dismiss();});
  function changed(){draftVersion++;fields.hidden=false;output.replaceChildren();}
  form.addEventListener('input',changed);
  form.addEventListener('change',changed);
  const current=version=>alive&&editor===activeEditor&&activeEditor.isConnected&&activeEditor.open&&version===draftVersion;
  const showError=(error,fallback)=>{
   fields.hidden=false;
   output.replaceChildren(h('p',reviewErrors[error.code||error.message]||fallback,{role:'alert'}));
  };
  async function save(plan,version){
   if(saving||!current(version))return;
   saving=form.inert=true;
   saveStatus.textContent='正在保存…';saveStatus.focus();
   let saved;
   try{
    saved=await api('/api/rules/core/plans/apply',{plan_id:plan.id,approval_digest:plan.approval_digest});
   }catch(error){
    if(!reviewErrors[error.code||error.message]&&current(version)){
     try{saved=await api('/api/rules/core/plans/'+encodeURIComponent(plan.id));}catch(_){}
    }
    if(!saved&&current(version))showError(error,'保存结果待确认，请在「自动运行」中查看。');
   }finally{
    saving=form.inert=false;saveStatus.textContent='';
   }
   if(!current(version))return;
   if(saved?.id===plan.id&&saved.status==='verified'&&saved.approval_digest===plan.approval_digest&&
      saved.new?.id===plan.new.id&&saved.new.enabled===!startPaused){
    dismiss();tab='owned';focusRecord=saved.new.id;
    toast(startPaused?'已保存为暂停':'自动化已保存并启用');
    await refresh();
   }else if(saved)showError(Error('save_unverified'),'保存结果待确认，请在「自动运行」中查看。');
   if(current(version))review.focus();
  }
  form.onsubmit=async event=>{
   event.preventDefault();
   if(saving||review.disabled||!current(draftVersion))return;
   const version=draftVersion;
   review.disabled=true;output.replaceChildren(h('p','正在核对设备与规则…',{role:'status'}));
   try{
    draft.intent=name.value.trim();
    if(triggerKind.value==='schedule'){
     const days=weekdays.flatMap((input,index)=>input.checked?[index+1]:[]);
     if(!householdZone)throw Error('timezone_required');
     if(!clockTime.value||!days.length)throw Error('schedule_required');
     draft.trigger={type:'schedule',at:clockTime.value,weekdays:days,timezone:householdZone};
    }else if(triggerKind.value==='once'){
     if(!householdZone)throw Error('timezone_required');
     if(!onceTime.value)throw Error('once_required');
     const at=originalOnce&&onceTime.value===originalOnceLocal&&householdZone===originalOnce.timezone?originalOnce.at:onceTime.value;
     draft.trigger={type:'once',at,timezone:householdZone};
    }else if(triggerKind.value==='state_transition'){
     const entity=triggerEntities.find(e=>e.id===triggerChoice.value);
     if(!entity||!edge.value)throw Error('trigger_required');
     const[from,to]=JSON.parse(edge.value),cap=entity.capabilities.find(c=>c.access==='event'&&c.support==='supported'&&c.name.endsWith('.transition'));
     draft.trigger={type:'state_transition',entity:entity.id,capability:cap.name.slice(0,-11),from_value:from,to_value:to};
    }
    draft.actions=inputs.filter(input=>input.row.isConnected).map(input=>input.read());
    if(!draft.actions.length)throw Error('action_required');
    const waits=draft.actions.filter(a=>a.type==='delay'),delay=waits.reduce((sum,action)=>sum+action.milliseconds,0);
    if(delay>=30000)throw Error('delay_total');
    if(JSON.stringify(waits)!==JSON.stringify((record?.rule.actions||[]).filter(a=>a.type==='delay'))){
     draft.execution={...draft.execution,max_event_age_ms:Math.max(draft.execution.max_event_age_ms,Math.min(30000,delay+3000))};
    }
    draft.conditions=conditionInputs.map(read=>read()).filter(Boolean);
    const submitted=structuredClone(draft),context=await api('/api/rules/core/context');
    if(!current(version))return;
    if(timeToggle.checked){
     if(!context.household_timezone)throw Error('timezone_required');
     submitted.conditions.push({type:'time_window',start:start.value,end:end.value,timezone:context.household_timezone});
    }
    const created=await api('/api/rules/core/plans',{
     rule:submitted,expected_context:context.fingerprint,idempotency_key:crypto.randomUUID(),expires_at:new Date(Date.now()+60000).toISOString(),
     ...(record?{record_id:record.id,expected_fingerprint:record.fingerprint}:{}),
     ...(entry?{catalog_selection:{key:entry.key,entry_digest:source.entryDigest}}:{}),
     ...(startPaused?{start_paused:true}:{}),
    });
    if(!current(version))return;
    const plan=await api('/api/rules/core/plans/trial',{plan_id:created.id});
    if(!current(version))return;
    if(plan.status!=='awaiting_approval'){
     const unavailable=submitted.conditions.find(condition=>condition.type==='numeric'&&
      plan.trial?.reasons?.includes('unknown_numeric_quality:'+condition.entity));
     const message=unavailable?'「'+(measurementNames.get(unavailable.entity)||'所选传感器')+'」暂无可用读数，请检查读数或更换传感器。':
      '当前设备状态不足以核对规则，请检查连接后重试。';
     output.replaceChildren(h('p',message,{role:'alert'}));return;
    }
    const reviewed=plan.new?.rule;
    if(!reviewed||plan.id!==created.id||plan.start_paused!==startPaused)throw Error('plan_mismatch');
    if(entry&&(plan.new.catalog_origin?.key!==entry.key||plan.new.catalog_origin.entry_digest!==source.entryDigest))throw Error('catalog_source_changed');
    if(submitted.trigger.type==='once'){
     const asked=/(?:Z|[+-]\d{2}:\d{2})$/.test(submitted.trigger.at)?onceLocalValue(submitted.trigger):submitted.trigger.at+(submitted.trigger.at.length===16?':00':'');
     if(reviewed.trigger.type!=='once'||reviewed.trigger.timezone!==submitted.trigger.timezone||onceLocalValue(reviewed.trigger)!==asked)throw Error('once_required');
    }
    const summary=h('dl',null,{class:'automation-review-summary'});
    const groups=[['何时执行',[describeTrigger(reviewed.trigger)]],
     ['且满足',reviewed.conditions.map(describeCondition)],['就执行',reviewed.actions.map(describeAction)]];
    for(const[label,items]of groups){
     if(!items.length)continue;
     const list=h('ul');items.forEach(item=>list.append(h('li',item)));
     summary.append(add(h('div'),h('dt',label),add(h('dd'),list)));
    }
    const heading=h('h3','核对「'+reviewed.intent+'」',{tabindex:'-1'});
    const saveButton=button(startPaused?'保存为暂停':'确认保存并启用',()=>save(plan,version),{class:'primary'});
    const back=button('返回修改',()=>{changed();review.focus();});
    fields.hidden=true;
    output.replaceChildren(heading,summary,add(h('div',null,{class:'product-actions'}),saveButton,back));
    heading.focus();
   }catch(error){
    if(current(version))showError(error,'无法核对规则，请检查设备连接后重试。');
   }finally{review.disabled=false;}
  };
 }
 view.append(h('p','正在读取场景与自动化…',{role:'status'}));void refresh();
 return{selectAttention(id){requestedAttention=id;if(id&&tab!=='owned'){tab='owned';draw();}else taskAttentionView?.select(id);},dispose(){alive=false;modeView?.dispose();taskAttentionView?.dispose();closeDialog();editor?.close();editor?.remove();view.remove();}};
}
