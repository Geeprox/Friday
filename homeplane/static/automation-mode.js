const node=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
export function mountAutomationMode(root,{api,toast,onStatus=null}){
 let alive=true,busy=false,report=null,timer,generation=0;
 const section=node('section',null,{class:'automation-mode','aria-label':'自动运行设置'});
 const heading=node('div'),title=node('h3','家庭状态'),note=node('p','正在读取…',{class:'caption',role:'status'});
 heading.append(title,note);
 const modes=node('div',null,{class:'segmented','aria-label':'选择家庭状态'}),buttons=[];
 const toggle=node('button','读取中…',{type:'button',role:'switch','aria-label':'自动运行','aria-checked':'false'});
 toggle.disabled=true;
 section.append(heading,modes,toggle);root.append(section);
 function draw(){
  const state=report?.runtime,source=report?.mode_source,mode=source?.state==='current'?source.mode:'unknown';
  const text=!report?'状态暂未更新':!state?.available?'家庭服务暂未就绪，自动任务保持暂停。':source?.state==='changed'?'家庭连接或操作权限已变化，请重新确认状态。':report.enabled===false?'自动运行已暂停':source?.state==='current'&&!state.mode_window_current?'正在恢复已保存的家庭状态…':({ready:'已启用的规则会在条件满足时运行。',manual:state.manual_hold_active?'手动控制后稍候恢复自动化。':'手动接管中，自动化暂停。',sleeping:'休息中，普通自动化暂停。',starting:'正在准备自动运行。',awaiting_mode:'选择家庭状态，将保持到你更改。'}[state.execution_state]||'选择家庭状态，将保持到你更改。');
  if(note.textContent!==text)note.textContent=text;
  buttons.forEach(([key,b])=>{b.setAttribute('aria-pressed',String(mode===key));b.disabled=busy||!state?.available||!source?.context;});
  const toggleText=report?.enabled?'自动运行已开启':'自动运行已暂停';if(toggle.textContent!==toggleText)toggle.textContent=toggleText;
  toggle.setAttribute('aria-checked',String(report?.enabled===true));toggle.disabled=busy||!report||!Number.isInteger(report.revision);
 }
 async function refresh(){if(!alive||busy||document.hidden)return;const ticket=++generation;let next;try{next=await api('/api/home/automatic-status');}catch{next=null;}if(alive&&!busy&&ticket===generation){report=next;draw();onStatus?.(report?.runtime||null);}}
 async function change(path,body,message){if(busy)return;busy=true;generation++;draw();try{await api(path,body);if(alive)toast(message);}catch{if(alive)toast('设置未保存，请重新读取后重试。',true);}finally{busy=false;if(alive)await refresh();}}
 for(const[key,label]of [['awake','日常'],['sleep','休息'],['manual','手动接管']]){
  const b=node('button',label,{type:'button','aria-pressed':'false'});b.disabled=true;
  b.onclick=()=>{if(report?.mode_source?.context)return change('/api/home/mode',{mode:key,expected_context:report.mode_source.context},{awake:'已切换为日常状态',sleep:'已进入休息状态',manual:'已暂停自动化，交由你控制'}[key]);};
  buttons.push([key,b]);modes.append(b);
 }
 toggle.onclick=()=>{if(report)void change('/api/home/preferences',{expected_revision:report.revision,automatic_running:!report.enabled},report.enabled?'自动运行已暂停':'自动运行已开启');};
 void refresh();timer=setInterval(()=>void refresh(),onStatus?2000:10000);
 return{dispose(){alive=false;generation++;clearInterval(timer);section.remove();}};
}
