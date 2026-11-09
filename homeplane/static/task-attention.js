const taskNode=(tag,text,attrs={})=>{const node=document.createElement(tag);if(text!=null)node.textContent=text;for(const[key,value]of Object.entries(attrs))node.setAttribute(key,value);return node;};
const taskButton=(label,run,attrs={})=>{const node=taskNode('button',label,{type:'button',...attrs});node.onclick=run;return node;};
const attentionKey=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value)?value:null;
const taskTime=item=>{try{return new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',timeStyle:'short',...(item.timezone?{timeZone:item.timezone}:{})}).format(new Date(item.at));}catch{return '';}};

export function mountTaskAttention(root,{api,go,toast,getRecord,onEdit,onReload=null,onEmpty=()=>{},attentionId=null}){
 let alive=true,report=null,requested=attentionKey(attentionId),focused=null;
 const seen=new Set(),rows=new Map();
 const section=taskNode('section',null,{class:'automation-task-attention','aria-label':'任务提醒'}),heading=taskNode('h3','任务提醒'),note=taskNode('p','',{class:'caption',role:'status',tabindex:'-1'}),list=taskNode('div',null,{class:'automation-task-issues'});
 const reload=taskButton('重新读取',async()=>{reload.disabled=true;try{if(onReload){await onReload();return;}const next=await api('/api/rules/core/runtime');if(alive){for(const value of rows.values())value.error.textContent='';update(next.task_attention||null);}}catch{if(alive)note.textContent='任务结果暂时无法读取，请稍后重试。';}finally{if(alive)reload.disabled=false;}});
 const all=taskButton('查看全部自动化',()=>{requested=null;focused=null;go('Automations');update(report);},{class:'text-button'});
 section.append(heading,note,list,reload,all);root.append(section);
 function row(item){
  const article=taskNode('article',null,{class:'automation-task-issue'}),info=taskNode('div'),title=taskNode('h4','',{tabindex:'-1'}),time=taskNode('p','',{class:'caption'}),detail=taskNode('p'),actions=taskNode('div',null,{class:'product-actions'}),error=taskNode('p','',{role:'status',class:'caption'});
  const value={article,title,time,detail,error,item,busy:false};
  const primary=taskButton('',()=>{const current=value.item;if(current.kind!=='unconfirmed'){const record=getRecord(current.record_id);if(current.record_current&&record?.fingerprint===current.fingerprint)onEdit(record);else void reload.onclick();}else go('Devices');});
  const acknowledge=taskButton('已了解',async()=>{
   if(value.busy||!alive)return;const current=value.item;value.busy=true;acknowledge.disabled=true;error.textContent='';
   try{
    const result=await api('/api/home/automations/records/attention/'+current.id+'/acknowledge',{record_id:current.record_id,expected_fingerprint:current.fingerprint,run_id:current.run_id||null});
    if(result?.status!=='acknowledged'||result.id!==current.id||result.device_actions!==false)throw Error();
    if(!alive)return;seen.add(current.id);if(seen.size>1024)seen.delete(seen.values().next().value);if(requested===current.id){requested=null;focused=null;}update(report);toast('已从首页提醒移除');
   }catch{if(alive&&rows.get(current.id)===value){error.textContent='提醒未更新，请重新读取后再试。';reload.hidden=false;}}
   finally{value.busy=false;if(alive)acknowledge.disabled=false;}
  },{class:'text-button'});
  value.primary=primary;value.acknowledge=acknowledge;
  info.append(title,time,detail,error);actions.append(primary,acknowledge);article.append(info,actions);return value;
 }
 function update(next){
  if(!alive)return;report=next;const available=report?.available===true;
  const items=available&&Array.isArray(report.items)?report.items.filter(item=>attentionKey(item.id)&&['missed','not_executed','unconfirmed'].includes(item.kind)&&!seen.has(item.id)):[];
  let lostFocus=false;const wanted=new Set(items.map(item=>item.id));for(const[id,value]of rows)if(!wanted.has(id)){lostFocus||=value.article.contains(document.activeElement);value.article.remove();rows.delete(id);}
  for(const[index,item]of items.entries()){
   let value=rows.get(item.id);if(!value){value=row(item);rows.set(item.id,value);}value.item=item;
   for(const[node,text]of [[value.title,item.title||'自动任务'],[value.time,taskTime(item)],[value.detail,({missed:'已错过执行时间。',not_executed:'这次安排未能执行。',unconfirmed:'这次执行的设备结果尚未确认。'})[item.kind]],[value.primary,item.kind!=='unconfirmed'?'重新安排':'查看设备']])if(node.textContent!==text)node.textContent=text;
   if(item.kind!=='unconfirmed'&&(!item.record_current||getRecord(item.record_id)?.fingerprint!==item.fingerprint))value.primary.textContent='重新读取';
   value.article.setAttribute('data-attention-id',item.id);value.article.setAttribute('data-focused',String(requested===item.id));
   if(list.children[index]!==value.article)list.insertBefore(value.article,list.children[index]||null);
  }
  const missing=available&&requested&&!wanted.has(requested);section.hidden=!items.length&&!requested;note.hidden=!missing&&available;reload.hidden=available&&![...rows.values()].some(value=>value.error.textContent);all.hidden=!missing;
  const text=missing?'这条提醒已处理，或任务已更新。':!available?'任务结果暂时无法读取。':'';if(note.textContent!==text)note.textContent=text;
  if(requested&&requested!==focused&&wanted.has(requested)){const value=rows.get(requested);focused=requested;value.title.focus();value.article.scrollIntoView?.({block:'center',behavior:'instant'});}
  else if(lostFocus){const next=rows.values().next().value;if(next)next.title.focus();else onEmpty();}
 }
 update(null);
 return{update,select(id){requested=attentionKey(id);focused=null;update(report);},dispose(){alive=false;rows.clear();section.remove();}};
}
