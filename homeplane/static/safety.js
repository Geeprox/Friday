/* Local safety review. Untrusted names and observations are always text nodes. */
export function mountSafety(container,{go=()=>{}}={}){
 const base='/api/embodied/safety';let alive=true,report=null,busy=false,version=0,lastEvents='',selected=null,timer;
 const el=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
 const add=(n,...children)=>{n.append(...children);return n;};
 const button=(text,run)=>{const n=el('button',text,{type:'button'});n.onclick=run;return n;};
 const api=async(path,body)=>{const r=await fetch(base+path,{headers:{'Content-Type':'application/json'},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});if(!r.ok)throw Error('unavailable');return r.json();};
 const root=el('div',null,{class:'safety-page'});container.append(root);
 if(!document.getElementById('safety-style'))document.head.append(el('link',null,{id:'safety-style',rel:'stylesheet',href:'/static/safety.css'}));
 const status=el('p','正在读取监测状态…',{role:'status','aria-live':'polite'}),enable=button('开启监测',()=>change('enabled')),announce=button('开启语音提醒',()=>change('announce'));
 const test=button('试听跌倒提醒',async()=>{if(busy)return;busy=true;test.disabled=true;status.textContent='正在发送测试提醒…';try{await api('/test',{});await refresh();}catch{status.textContent='试听未发送，请检查播报音箱连接。';}finally{busy=false;test.disabled=false;}});
 const intro=add(el('section',null,{class:'safety-intro'}),el('h2','及时发现，及时查看'),el('p','发现疑似跌倒时记录事件，按需通过选定音箱提醒家人。'),status,
   add(el('div',null,{class:'safety-actions'}),enable,announce,test,button('选择播报音箱',()=>go('Settings'))),
   el('p','实验性提醒，可能漏报或误报。请保持日常照护。开启后会在本机保存私密关键帧，最多 100 条、64 MB，保存 30 天。',{class:'caption'}));
 const capabilities=el('section',null,{class:'safety-capabilities','aria-label':'监测能力'}),list=el('section',null,{class:'safety-events','aria-label':'预警记录'}),detail=el('section',null,{class:'safety-detail','aria-label':'事件详情'});
 root.append(intro,capabilities,add(el('div',null,{class:'safety-history'}),add(el('div'),add(el('div',null,{class:'safety-actions'}),el('h2','预警记录'),button('刷新',()=>refresh())),list),detail));
 const reviewNames={unreviewed:'待查看',acknowledged:'已查看',false_alarm:'已标记误报'};
 const audioNames={not_requested:'未请求播报',requesting:'正在发送播报',accepted:'音箱已接受，实际声音待确认',paused:'安静模式，未播报',busy:'音箱忙，未播报',unavailable:'播报不可用',cancelled:'已取消播报',interrupted:'播报已中断'};
 async function change(key){if(busy||!report)return;busy=true;enable.disabled=announce.disabled=true;try{await api('/policy',{...report.policy,[key]:!report.policy[key]});await refresh();}catch{status.textContent='设置未保存，请刷新后重试。';}finally{busy=false;enable.disabled=announce.disabled=false;}}
 async function refresh(){const epoch=++version;try{const [next,history]=await Promise.all([api(''),api('/events')]);if(!alive||epoch!==version)return;report=next;
   enable.textContent=next.policy.enabled?'暂停监测':'开启监测';enable.setAttribute('role','switch');enable.setAttribute('aria-checked',String(next.policy.enabled));
   announce.textContent=next.policy.announce?'关闭语音提醒':'开启语音提醒';announce.setAttribute('role','switch');announce.setAttribute('aria-checked',String(next.policy.announce));
   const count=next.worker.cameras?.filter(c=>c.fresh).length||0;
   status.textContent=next.worker.error?'预警留档暂不可用，请检查本机存储':!next.policy.enabled?'监测已暂停':count?`${count} 台摄像头正在监测 · ${next.policy.announce?'单设备语音提醒':'只记录事件'}`:'监测已开启，等待清晰、稳定的摄像头画面';
   capabilities.replaceChildren(el('h2','监测场景'));
   for(const c of next.capabilities)capabilities.append(add(el('article'),el('strong',c.name),el('span',c.status==='experimental'?'实验性可用':'尚未接入',{class:'caption'}),el('p',c.basis,{class:'caption'})));
   const fingerprint=JSON.stringify(history.events);if(fingerprint!==lastEvents){lastEvents=fingerprint;list.replaceChildren();
     if(!history.events.length)list.append(el('p','还没有预警记录。',{class:'product-empty'}));
     for(const e of history.events){const row=button('',()=>show(e));row.className='safety-event';row.append(el('strong',e.simulated?'播报测试':e.person_name?e.person_name+' · 疑似跌倒':'有人疑似跌倒'),el('span',new Date(e.at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})),el('span',reviewNames[e.review]||'待查看'));list.append(row);}
     if(selected){const current=history.events.find(e=>e.id===selected);if(current)show(current);else{selected=null;detail.replaceChildren();}}
   }
 }catch{if(alive&&epoch===version)status.textContent='预警服务暂时不可用，请刷新重试。';}}
 function show(event){selected=event.id;detail.replaceChildren(el('h2',event.simulated?'播报测试':'疑似跌倒'),el('p',event.message),el('p',audioNames[event.announcement.status]||'播报状态未知',{role:'status'}),el('p',reviewNames[event.review]||'待查看'));
   if(event.camera)detail.append(el('p','来源：'+event.camera+' · '+new Date(event.at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}),{class:'caption'}));
   const actions=el('div',null,{class:'safety-actions'});for(const[value,label]of [['acknowledged','已查看'],['false_alarm','标记误报']])actions.append(button(label,async()=>{try{await api('/events/'+event.id+'/review',{review:value});await refresh();}catch{status.textContent='标记未保存，请重试。';}}));
   actions.append(button('导出事件记录',()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(event,null,2)],{type:'application/json'}));const a=el('a',null,{href:url,download:'Friday-safety-'+event.id+'.json'});a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}));detail.append(actions);
   if(event.frames.length){detail.append(el('h3','事件前后关键帧'),el('p','本机私密图像；以下为关键帧序列，不是连续录像。',{class:'caption'}));const frames=el('div',null,{class:'safety-frames'});for(const f of event.frames)frames.append(add(el('figure'),el('img',null,{src:base+'/events/'+event.id+'/frames/'+f.ordinal,alt:'事件关键帧 '+f.ordinal,loading:'lazy'}),el('figcaption',new Date(f.at*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai'}))));detail.append(frames);}else detail.append(el('p',event.simulated?'播报测试不采集摄像头画面。':'没有可用关键帧。',{class:'caption'}));
 }
 void refresh();timer=setInterval(()=>{if(!busy)void refresh();},5000);
 return{dispose(){alive=false;version++;clearInterval(timer);root.remove();}};
}
